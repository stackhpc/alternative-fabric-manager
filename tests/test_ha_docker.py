"""OpenSM's container under the watchdog, in HA mode"""

import json
import os

import pytest

from alternative_fabric_manager.drivers import ha_docker
from alternative_fabric_manager.ha import watchdog as wd

STARTED = "Nov 05 17:00:01 [275FE640] 0x02 -> SUBNET UP\n"


class Container:
    def __init__(self, status="running"):
        self.status = status
        self.killed = False
        self.removed = False

    def logs(self, **_kwargs):
        return STARTED.encode()

    def reload(self):
        pass

    def kill(self, **_kwargs):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class Client:
    def __init__(self, probe=b""):
        self.containers = self
        self.existing = None
        self.runs = []
        self.probe = probe

    def get(self, _name):
        if self.existing is None:
            raise ha_docker.docker.errors.NotFound("none")
        return self.existing

    def run(self, **kwargs):
        self.runs.append(kwargs)
        if kwargs.get("remove"):
            return self.probe
        self.existing = Container()
        return self.existing


@pytest.fixture
def driver(config, tmp_path):
    config.driver = "managed"
    config.start_timeout = 0.5
    config.ha_state_dir = str(tmp_path / "state")
    config.host_proc = str(tmp_path / "proc")
    os.makedirs(config.host_proc)
    for path in (config.partitions_file, config.opensm_conf_file):
        with open(path, "w") as f:
            f.write("x")
    return ha_docker.HAManagedDocker(config, client=Client())


def test_opensm_runs_under_the_watchdog_with_a_start_request(driver, config):
    driver.prepare("u", 3, 1e12)
    driver.write_lease("u", 3, 1e12)
    driver.start()
    run = driver.client.runs[-1]
    assert run["entrypoint"][:2] == ["python3", f"{config.ha_state_dir}/watchdog.py"]
    assert run["command"][0] == "/usr/sbin/opensm"
    assert f"{config.ha_state_dir}:{config.ha_state_dir}" in run["volumes"]
    assert os.path.exists(os.path.join(config.ha_state_dir, "watchdog.py"))
    with open(os.path.join(config.ha_state_dir, wd.START)) as f:
        request = json.load(f)
    # Hashes of the files at the paths OpenSM sees
    assert set(request["hashes"]) == {
        "/etc/opensm/partitions.conf",
        "/etc/opensm/opensm.conf",
    }
    assert (request["instance"], request["term"]) == ("u", 3)


def test_stop_asks_then_kills_and_clears_the_watchdogs_files(driver, config):
    driver.prepare("u", 3, 1e12)
    driver.write_lease("u", 3, 1e12)
    driver.start()
    container = driver.client.existing
    container.status = "paused"
    driver.stop()
    assert os.path.exists(os.path.join(config.ha_state_dir, wd.STOP))
    assert container.killed and container.removed
    assert not os.path.exists(os.path.join(config.ha_state_dir, wd.LEASE))
    # A later prepare removes the stop request again.
    driver.prepare("u", 4, 1e12)
    assert not os.path.exists(os.path.join(config.ha_state_dir, wd.STOP))


def test_verify_stopped_looks_at_every_process_on_the_host(driver, config):
    assert driver.verify_stopped()
    os.makedirs(os.path.join(config.host_proc, "4242"))
    with open(os.path.join(config.host_proc, "4242", "comm"), "w") as f:
        f.write("opensm\n")
    assert not driver.verify_stopped()


def test_an_unreadable_proc_isnt_verified(driver, config):
    config.host_proc = "/nonexistent"
    assert not driver.verify_stopped()


IBSTAT_UP = b"""CA 'mlx5_0'
\tPort 1:
\t\tState: Initializing
\t\tPhysical state: LinkUp
"""
SWITCHES = b'Switch\t: 0xec0d9a0300b12560 ports 41 "SwitchIB" base port 0 lid 2\n'


def test_reach_counts_switches_from_a_port_that_is_up(driver):
    driver.m_key = lambda: "0x1"
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    assert driver.reach() == 1
    assert "-y 0x1" in " ".join(driver.client.runs[-1]["command"])


def test_reach_is_none_when_the_port_is_down(driver):
    driver.client.probe = (
        b"\t\tState: Down\n\t\tPhysical state: Disabled\n===\n" + SWITCHES
    )
    assert driver.reach() is None


def test_the_watchdog_runs_on_the_images_python():
    # OpenSM's image has Python 3.9: the watchdog must stay compatible.
    import ast

    with open(wd.__file__) as f:
        tree = ast.parse(f.read(), feature_version=(3, 9))
    assert tree
