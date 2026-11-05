import pytest

from alternative_fabric_manager.drivers import ReloadError
from alternative_fabric_manager.drivers import docker as docker_driver

BEFORE = "Nov 05 16:59:14 0x04 -> do_sweep:\n" + "*" * 10 + " SUBNET UP " + "*" * 10
# What OpenSM logs after SIGHUP when only the partitions file has changed
SWEEP = """\
Nov 05 16:59:16 190569 [275FE640] 0x04 -> handle_hup_signal:
********************* INITIATING PKEY SWEEP **********************
Nov 05 16:59:16 190584 [275FE640] 0x02 -> handle_hup_signal: Only \
partitions file changed, Running pkey sweep (pkey mgr only)
Nov 05 16:59:16 191669 [275FE640] 0x04 -> handle_hup_signal:
********************** PKEY SWEEP COMPLETE ***********************"""


class FakeContainer:
    def __init__(self, before="", after="", status="running"):
        self.status = status
        self.log = before
        self.after = after
        self.signals = []

    def kill(self, signal):
        self.signals.append(signal)
        self.log = "\n".join(filter(None, (self.log, self.after)))

    def logs(self, stdout, stderr, since=None):
        return self.log.encode()


class FakeClient:
    def __init__(self, container):
        self.containers = self
        self.container = container

    def get(self, name):
        assert name == "opensm"
        return self.container


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(docker_driver, "POLL_INTERVAL", 0)


def _reloader(container, timeout=1):
    return docker_driver.DockerReloader("opensm", timeout, client=FakeClient(container))


def test_waits_for_the_pkey_sweep():
    container = FakeContainer(after=SWEEP)
    _reloader(container).reload()
    assert container.signals == ["SIGHUP"]


def test_ignores_a_sweep_from_before_the_signal():
    container = FakeContainer(before=BEFORE)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_needs_the_signal_to_be_handled():
    # A sweep that was already running when the signal came doesn't count.
    container = FakeContainer(after=BEFORE)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_parse_error_fails():
    container = FakeContainer(
        after=SWEEP.replace(
            "Nov 05 16:59:16 191669",
            "Nov 05 16:59:16 191000 [275FE640] 0x01 -> PARSE ERROR line 3\n"
            "Nov 05 16:59:16 191669",
        )
    )
    with pytest.raises(ReloadError, match="couldn't read"):
        _reloader(container).reload()


def test_stopped_container_fails():
    container = FakeContainer(status="exited")
    with pytest.raises(ReloadError, match="exited"):
        _reloader(container).reload()
    assert container.signals == []


STARTED = """\
Nov 05 17:00:01 [275FE640] 0x02 -> SUBNET UP
*************************** SUBNET UP ****************************"""


class FakeManagedContainer(FakeContainer):
    def __init__(self, log="", status="running"):
        super().__init__(before=log, status=status)
        self.removed = False

    def reload(self):
        pass

    def remove(self, force):
        self.removed = True


class FakeManagedClient:
    def __init__(self, existing=None, log=STARTED, status="running"):
        self.containers = self
        self.existing = existing
        self.log = log
        self.status = status
        self.runs = []

    def get(self, name):
        if self.existing is None:
            raise docker_driver.docker.errors.NotFound("no container")
        return self.existing

    def run(self, **kwargs):
        self.runs.append(kwargs)
        self.existing = FakeManagedContainer(self.log, self.status)
        return self.existing


@pytest.fixture
def managed_config(config):
    config.driver = "managed"
    config.start_timeout = 0.05
    config.opensm_conf = {"sm_priority": "15"}
    return config


def test_managed_start(managed_config):
    old = FakeManagedContainer()
    client = FakeManagedClient(existing=old)
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    driver.start()

    assert old.removed
    (kwargs,) = client.runs
    assert kwargs["pid_mode"] == "container:alternative-fabric-manager"
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert kwargs["network_mode"] == "host"
    assert kwargs["command"][:2] == ["-F", "/etc/opensm/opensm.conf"]
    assert kwargs["volumes"][0] == "/etc/opensm:/etc/opensm:ro"
    assert driver.running()


def test_managed_start_without_manager_container(managed_config):
    managed_config.manager_container = ""
    client = FakeManagedClient()
    docker_driver.ManagedDocker(managed_config, client=client).start()
    assert "pid_mode" not in client.runs[0]


def test_managed_start_fails_if_opensm_exits(managed_config):
    client = FakeManagedClient(log="opensm: cannot bind", status="exited")
    with pytest.raises(ReloadError, match="exited"):
        docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_start_times_out(managed_config):
    client = FakeManagedClient(log="still discovering")
    with pytest.raises(ReloadError, match="didn't start"):
        docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_start_as_standby(managed_config):
    client = FakeManagedClient(log="0x02 -> Entering STANDBY state")
    docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_running(managed_config):
    client = FakeManagedClient()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    assert not driver.running()
    client.existing = FakeManagedContainer(status="exited")
    assert not driver.running()


def test_managed_stop(managed_config):
    container = FakeManagedContainer()
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    driver.stop()
    assert container.removed


def test_a_sweep_must_start_after_the_signal():
    # A heavy sweep already under way may finish after OpenSM logs the signal,
    # without having read the new file.
    already = (
        "Nov 05 16:59:16 190569 [275FE640] 0x04 -> handle_hup_signal:\n"
        "*************************** SUBNET UP ****************************"
    )
    container = FakeContainer(after=already)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_failed_start_removes_the_container(managed_config):
    client = FakeManagedClient(log="0x01 -> PARSE ERROR line 3")
    with pytest.raises(ReloadError, match="couldn't read"):
        docker_driver.ManagedDocker(managed_config, client=client).start()
    assert client.existing.removed


# What OpenSM 5.17 logged when SIGHUP found an unchanged file: a heavy sweep,
# which reads the partitions file after "HEAVY SWEEP COMPLETE"
HEAVY = """\
Nov 05 18:10:00 237426 [5A1FE640] 0x04 -> handle_hup_signal: No configuration \
changes, heavy sweep suggestion: TRUE
********************* INITIATING HEAVY SWEEP *********************
********************** HEAVY SWEEP COMPLETE **********************
Nov 05 18:10:00 240189 [5A1FE640] 0x01 -> parse_part_conf: PARSE ERROR: line 5: \
no partition definition found
PARSE ERROR: line 5: no partition definition found
Nov 05 18:10:00 242710 [5A1FE640] 0x02 -> SUBNET UP
*************************** SUBNET UP ****************************"""


def test_heavy_sweep_parse_error_fails():
    with pytest.raises(ReloadError, match="couldn't read"):
        _reloader(FakeContainer(after=HEAVY)).reload()


def test_heavy_sweep_ends_with_subnet_up():
    # Not with HEAVY SWEEP COMPLETE, which comes before the partitions file
    # is read
    partial = HEAVY.split("Nov 05 18:10:00 240189")[0]
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(FakeContainer(after=partial), timeout=0.05).reload()
    clean = "\n".join(line for line in HEAVY.splitlines() if "PARSE" not in line)
    _reloader(FakeContainer(after=clean)).reload()
