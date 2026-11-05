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

    def logs(self, since, stdout, stderr):
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
