import types

import pytest

from alternative_fabric_manager.drivers import UNATTRIBUTED, ReloadError, SweepReport
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


def test_managed_start_as_standby_fails(managed_config):
    # Another subnet manager is master: this one must not count as applying
    # anything.
    client = FakeManagedClient(log="0x02 -> Entering STANDBY state")
    with pytest.raises(ReloadError):
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


# OpenSM 5.17's own messages, from its binary
PKEY_BY_DESCRIPTION = (
    "pkey_mgr_update_port: ERR 0505: Failed to set PKey 0x8064 in block 0 idx 2 "
    "for compute-0 mlx5_0"
)
PKEY_BY_NODE = (
    "pkey_mgr_update_port: ERR 0509: pkey_mgr_update_pkey_entry() failed to update "
    "pkey table block 0 for node 0x0002c90300001234 port 1 (compute-0 mlx5_0)"
)
PKEY_BY_PORT = (
    "osm_vm: ERR 5703: Failure initiating PKeyTable set request to VPort "
    "0x0002c90300005678, VPort index 1 of port 0x0002c90300001235 (compute-0)"
)
ENFORCEMENT = (
    "pkey_mgr_update_peer_port: ERR 0507: Not enough pkey entries (32 <= 33) on "
    "switch 0xec0d9a0300b12560 port 7 (SwitchIB). Clearing Enforcement bit"
)


def test_a_pkey_error_without_a_port_guid_fails_closed():
    for line in (PKEY_BY_DESCRIPTION, PKEY_BY_NODE):
        report = SweepReport()
        docker_driver._note_pkey_error(report, line)
        # A node GUID isn't the port's, so it mustn't count as one.
        assert report.pkey_errors == set()
        assert report.unattributed


def test_a_pkey_error_with_a_port_guid_names_it():
    report = SweepReport()
    docker_driver._note_pkey_error(report, PKEY_BY_PORT)
    assert 0x0002C90300001235 in report.pkey_errors
    assert not report.unattributed


def test_cleared_enforcement_names_the_switch_port():
    report = SweepReport()
    docker_driver._note_pkey_error(report, ENFORCEMENT)
    assert report.enforcement_errors == {"ec0d9a0300b12560:7"}
    report = SweepReport()
    docker_driver._note_pkey_error(report, "x: ERR 0513: something else")
    assert report.enforcement_errors == {UNATTRIBUTED}


def test_pkey_errors_are_not_partitions_errors():
    for line in (PKEY_BY_DESCRIPTION, PKEY_BY_PORT, ENFORCEMENT):
        assert not docker_driver._is_partitions_error(line.replace("osm_vm", "prtn"))
    assert docker_driver._is_partitions_error("osm_prtn_make_new: ERR 0A01: x")


IBSWITCHES = (
    'Switch\t: 0xec0d9a0300b12560 ports 41 "SwitchIB Mellanox Technologies" '
    "base port 0 lid 2 lmc 0\n"
)
SMIR = """SMInfoRecord dump:
\t\tRID
\t\tLID.....................1
\t\tSMInfo dump:
\t\tGUID....................0x040973ffffc75168
\t\tSM_Key..................0x0000000000000000
\t\tActCount................1234
\t\tPriority................15
\t\tSMState.................3
SMInfoRecord dump:
\t\tRID
\t\tLID.....................9
\t\tSMInfo dump:
\t\tGUID....................0x0002c90300001235
\t\tSM_Key..................0x0000000000000000
\t\tActCount................5
\t\tPriority................0
\t\tSMState.................2
"""
IBSTAT_P = (
    "0x040973ffffc75168\n0xffffffffffffffff\n0xffffffffffffffff\n0x0000000000000000\n"
)


class FakeExecContainer:
    status = "running"

    def __init__(self):
        self.commands = []

    def exec_run(self, command):
        self.commands.append(command)
        outputs = {"ibswitches": IBSWITCHES, "saquery": SMIR, "ibstat": IBSTAT_P}
        return 0, outputs[command[0]].encode()


def test_fabric_parses_the_tools_output():
    container = FakeExecContainer()
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    driver = docker_driver.DockerReloader("opensm", 30, client=client)
    fabric = driver.fabric(m_key="0x11", sa_key="0x22")
    assert fabric.switches == {0xEC0D9A0300B12560}
    assert fabric.sms == [(0x040973FFFFC75168, 3), (0x0002C90300001235, 2)]
    # Placeholder GUIDs of unassigned virtual functions aren't ours.
    assert fabric.own_guids == {0x040973FFFFC75168}
    assert ["ibswitches", "-y", "0x11"] in container.commands
    assert ["saquery", "--smkey", "0x22", "SMIR"] in container.commands


MAD_PKEY_FAILURES = (
    "sm_mad_ctrl_send_err_cb: ERR 3113: MAD completed in error (IB_TIMEOUT): "
    "SubnSet(PKeyTable), attr_mod 0x0, TID 0x1234, dest_guid 0x0002c90300001234",
    "sm_mad_ctrl_rcv_callback: ERR 3119: Set method failed for attribute 0x16 "
    "(PKeyTable)",
)


def test_mad_failures_setting_pkey_tables_fail_closed():
    for line in MAD_PKEY_FAILURES:
        report = SweepReport()
        docker_driver._note_pkey_error(report, line)
        assert report.unattributed, line
    # Other attributes' failures aren't pkey errors.
    report = SweepReport()
    docker_driver._note_pkey_error(
        report,
        "x: ERR 3113: MAD completed in error (IB_TIMEOUT): SubnGet(NodeInfo), "
        "attr_mod 0x0, TID 0x1, dest_guid 0x1",
    )
    assert not report.unattributed


def test_managed_start_reports_its_first_sweep(managed_config):
    client = FakeManagedClient(log=MAD_PKEY_FAILURES[1] + "\n" + STARTED)
    report = docker_driver.ManagedDocker(managed_config, client=client).start()
    assert report is not None and report.unattributed


@pytest.mark.parametrize(
    "line",
    [
        "pkey_mgr_process_partition_table: ERR 0503: Failed to obtain P_Key "
        "0x8064 block and index for compute-0 mlx5_0",
        "pkey_mgr_update_port: ERR 0511: Failed to set PortInfo for node "
        "0xec0d9a0300b12560 port 7 (SwitchIB)",
        "pkey_mgr_update_port: ERR 0514: Failed to allocate backup blocks",
    ],
)
def test_more_pkey_manager_errors_fail_closed(line):
    report = SweepReport()
    docker_driver._note_pkey_error(report, line)
    assert report.unattributed
    assert not docker_driver._is_partitions_error(line)
