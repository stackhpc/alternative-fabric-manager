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

    def start(self):
        self.status = "running"

    def kill(self, **_kwargs):
        self.killed = True

    def remove(self, force=False, v=False):
        self.removed = True


class Image:
    # OpenSM's image declares these, as the real one does.
    attrs = {"Config": {"Volumes": {"/var/cache/opensm/": {}, "/var/log/": {}}}}


class Images:
    def __init__(self):
        self.present = True

    def get(self, _name):
        if not self.present:
            raise ha_docker.docker.errors.ImageNotFound("no image")
        return Image()

    def pull(self, *_args, **_kwargs):
        raise AssertionError("the reach probe mustn't pull")


class Client:
    def __init__(self, probe=b""):
        self.containers = self
        self.existing = None
        # The reach probe's containers, and OpenSM's
        self.runs = []
        self.created = []
        self.probe = probe
        self.images = Images()
        self.api = self.images

    def get(self, _name):
        if self.existing is None:
            raise ha_docker.docker.errors.NotFound("none")
        return self.existing

    def run(self, **kwargs):
        self.runs.append(kwargs)
        assert kwargs.get("remove")
        return self.probe

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.existing = Container(status="created")
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
    (run,) = driver.client.created
    assert driver.client.existing.status == "running"
    assert run["entrypoint"][:2] == ["python3", f"{config.ha_state_dir}/watchdog.py"]
    assert run["command"][0] == "/start.sh"
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


def test_the_start_request_follows_what_prepare_wrote(driver, config, monkeypatch):
    # With per-port M_Keys, prepare rewrites opensm.conf and renders the
    # cache after the container is created: the request's hashes must be of
    # those, or the watchdog refuses the start.
    import hashlib

    written = {}
    monkeypatch.setattr(
        ha_docker.docker_driver.ManagedDocker,
        "write_cache",
        lambda self, container, files: written.update(files),
    )

    def prepare(container):
        assert not os.path.exists(os.path.join(config.ha_state_dir, wd.START))
        with open(config.opensm_conf_file, "w") as f:
            f.write("rewritten")
        driver.write_cache(container, {"neighbors": "n\n", "guid2mkey": "g\n"})

    driver.prepare("u", 3, 1e12)
    driver.start(prepare=prepare)
    with open(os.path.join(config.ha_state_dir, wd.START)) as f:
        hashes = json.load(f)["hashes"]
    assert hashes["/etc/opensm/opensm.conf"] == (
        hashlib.sha256(b"rewritten").hexdigest()
    )
    assert hashes["/var/cache/opensm/neighbors"] == hashlib.sha256(b"n\n").hexdigest()
    assert hashes["/var/cache/opensm/guid2mkey"] == hashlib.sha256(b"g\n").hexdigest()
    assert written == {"neighbors": "n\n", "guid2mkey": "g\n"}
    # The next start's request names only what it wrote.
    driver.stop()
    driver.prepare("u", 4, 1e12)
    driver.start()
    with open(os.path.join(config.ha_state_dir, wd.START)) as f:
        assert set(json.load(f)["hashes"]) == {
            "/etc/opensm/partitions.conf",
            "/etc/opensm/opensm.conf",
        }


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
    with open(os.path.join(config.host_proc, "4242", "stat"), "w") as f:
        f.write("4242 (opensm) S 1 0 0")
    assert not driver.verify_stopped()


def test_an_unreadable_proc_isnt_verified(driver, config):
    config.host_proc = "/nonexistent"
    assert not driver.verify_stopped()


IBSTAT_UP = b"""CA 'mlx5_0'
\tPort 1:
\t\tState: Initializing
\t\tPhysical state: LinkUp
\t\tLink layer: InfiniBand
"""
SWITCHES = b'Switch\t: 0xec0d9a0300b12560 ports 41 "SwitchIB" base port 0 lid 2\n'


def test_reach_counts_switches_from_a_port_that_is_up(driver):
    driver.m_key = lambda: "0x1"
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    assert driver.reach() == 1
    run = driver.client.runs[-1]
    # The key in the environment, never on the command
    assert '-y "$AFM_M_KEY"' in " ".join(run["command"])
    assert run["environment"] == {"AFM_M_KEY": "0x1"}


def test_reach_counts_only_the_expected_switches_found(driver):
    driver.m_key = lambda: "0x1"
    driver.client.probe = (
        IBSTAT_UP
        + b"===\n"
        + SWITCHES
        + b'Switch\t: 0x0000000000000053 ports 8 "New" base port 0 lid 9\n'
    )
    driver.expected_switches = lambda: {0xEC0D9A0300B12560}
    assert driver.reach() == 1
    # Until any switch is expected, every switch counts.
    driver._reach = (None, float("-inf"))
    driver.expected_switches = set
    assert driver.reach() == 2


def test_with_per_port_rows_reach_counts_the_probers_switches(driver):
    # ibswitches -y with one key can't pass a re-keyed port.
    driver.m_key = lambda: "0x1"
    driver.reach_switches = lambda: {0x10, 0x20}
    driver.client.probe = IBSTAT_UP + b"===\n"
    assert driver.reach() == 2
    run = driver.client.runs[-1]
    assert "ibswitches" not in " ".join(run["command"])
    assert run["environment"] == {}


def test_a_prober_that_fails_reaches_nothing(driver):
    def fail():
        raise RuntimeError("no route")

    driver.reach_switches = fail
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    assert driver.reach() is None


def test_reach_is_none_when_the_port_is_down(driver):
    driver.client.probe = (
        b"\tPort 1:\n\t\tState: Down\n\t\tPhysical state: Disabled\n"
        b"\t\tLink layer: InfiniBand\n===\n" + SWITCHES
    )
    assert driver.reach() is None


def test_reach_ignores_ethernet_ports(driver):
    driver.client.probe = (
        b"\tPort 1:\n\t\tState: Active\n\t\tPhysical state: LinkUp\n"
        b"\t\tLink layer: Ethernet\n===\n" + SWITCHES
    )
    assert driver.reach() is None


def test_the_watchdog_runs_on_the_images_python():
    # OpenSM's image has Python 3.9: the watchdog must stay compatible.
    import ast

    with open(wd.__file__) as f:
        tree = ast.parse(f.read(), feature_version=(3, 9))
    assert tree


# This host's own ibstat, which has a Port GUID line in each port's block
REAL_IBSTAT = b"""CA 'mlx5_0'
\tCA type: MT4123
\tNumber of ports: 1
\tPort 1:
\t\tState: Active
\t\tPhysical state: LinkUp
\t\tRate: 200
\t\tBase lid: 1
\t\tLMC: 0
\t\tSM lid: 1
\t\tCapability mask: 0xa651e84a
\t\tPort GUID: 0x040973ffffc75168
\t\tLink layer: InfiniBand
CA 'mlx5_1'
\tCA type: MT4124
\tNumber of ports: 1
\tPort 1:
\t\tState: Down
\t\tPhysical state: LinkUp
\t\tPort GUID: 0xffffffffffffffff
\t\tLink layer: InfiniBand
"""


def test_reach_reads_a_real_ibstat(driver):
    driver.client.probe = REAL_IBSTAT + b"===\n" + SWITCHES
    assert driver.reach() == 1


def test_reach_ignores_a_down_port_beside_an_up_one(driver):
    down_only = REAL_IBSTAT.split(b"CA 'mlx5_1'")[1]
    driver.client.probe = b"CA 'mlx5_1'" + down_only + b"===\n" + SWITCHES
    assert driver.reach() is None


def test_a_zombie_opensm_counts_as_stopped(driver, config):
    os.makedirs(os.path.join(config.host_proc, "4243"))
    with open(os.path.join(config.host_proc, "4243", "comm"), "w") as f:
        f.write("opensm\n")
    with open(os.path.join(config.host_proc, "4243", "stat"), "w") as f:
        f.write("4243 (opensm) Z 1 0 0")
    assert driver.verify_stopped()


def test_reach_is_none_without_the_image_and_never_pulls_it(driver):
    # Its first probe runs on the coordinator's lease thread, which mustn't
    # wait on a registry; a node without the image can't start OpenSM.
    driver.client.images.present = False
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    assert driver.reach() is None
    assert driver.client.runs == []


def test_reach_is_none_if_docker_cant_say_whether_the_image_is_here(driver):
    def get(_name):
        raise ha_docker.docker.errors.APIError("daemon unhappy")

    driver.client.images.get = get
    assert driver.reach() is None
    assert driver.client.runs == []


def test_a_missing_image_is_logged_once_until_it_returns(driver, caplog):
    caplog.set_level("INFO")
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    driver.client.images.present = False
    for _ in range(3):
        assert driver._probe_reach() is None
    assert caplog.text.count("No OpenSM image here") == 1
    driver.client.images.present = True
    assert driver._probe_reach() == 1
    assert caplog.text.count("OpenSM's image is here") == 1


def test_start_fails_rather_than_pull_a_missing_image(driver):
    # A takeover then steps down rather than wait on a registry holding the
    # lease (see specs/ha.md, Timeouts).
    driver.client.probe = IBSTAT_UP + b"===\n" + SWITCHES
    assert driver.reach() == 1
    probes = len(driver.client.runs)
    driver.prepare("u", 3, 1e12)
    driver.write_lease("u", 3, 1e12)
    driver.client.images.present = False
    with pytest.raises(ha_docker.docker_driver.ReloadError, match="isn't on this node"):
        driver.start()
    assert len(driver.client.runs) == probes
    assert driver.client.created == []
    # Not eligible again on the reach cached before the image went
    assert driver.reach() is None


def test_reach_leaves_no_anonymous_volumes(driver):
    # run()'s remove leaves the image's anonymous volumes, one set per
    # probe, every few seconds: they filled a host's Docker disk.
    driver.reach()
    run = driver.client.runs[-1]
    assert run["tmpfs"] == {"/var/cache/opensm": "", "/var/log": ""}


def test_a_failed_reach_probe_never_logs_the_m_key(driver, caplog):
    # A command that exits non-zero raises ContainerError, whose message is
    # the whole command: with the key on it, every failed probe logged it.
    key = "0x5ec12e7000c0ffee"
    driver.m_key = lambda: key

    def run(**kwargs):
        driver.client.runs.append(kwargs)
        raise ha_docker.docker.errors.ContainerError(
            "probe", 1, kwargs["command"], kwargs["image"], b"ibwarn: timeout"
        )

    driver.client.run = run
    assert driver.reach() is None
    (kwargs,) = driver.client.runs
    assert key not in " ".join(kwargs["command"])
    assert kwargs["environment"] == {"AFM_M_KEY": key}
    assert "exit status 1" in caplog.text
    assert "c0ffee" not in caplog.text
