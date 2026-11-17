"""Allowed SMs (specs/secured-profile.md, Allowed SM GUIDs): the group's
configuration, the list each start renders and the checks that refuse one,
the fabric check's rules for SMs that OpenSM ignores, the commands, and the
scripts, run for real by sh against a fake sysfs and fake tools"""

import logging
import os
import shutil
import subprocess
import time

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import allowed_sm, db, opensm_conf, port_keys
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric, LogFindings, ReloadError
from alternative_fabric_manager.drivers import docker as docker_driver
from tests import conftest, test_docker

OWN_A = 0x0002C903000E0B72
OWN_B = 0x0002C903000E0B73
TENANT = 0x0002C90300001234
OLD_HOST = 0x0002C90300005678
SWITCH = 0xEC0D9A0300B12560
KEY = "0x5ec12e7000c0ffee"
SECTIONS = "[api]\ntoken = x\n"


def _load(tmp_path, text):
    path = tmp_path / "afm.conf"
    path.write_text(SECTIONS + text)
    return config_mod.Config.load(str(path))


# Configuration


def test_allowed_sm_is_experimental(tmp_path):
    assert "allowed_sm" in config_mod.HARDENING_GROUPS
    assert "allowed_sm" not in config_mod.ALL_HARDENING_GROUPS
    config = _load(tmp_path, "[opensm]\nhardening = all\n")
    assert not config.allowed_sm_in_effect
    config = _load(tmp_path, "[opensm]\nhardening = all, allowed_sm\n")
    assert config.allowed_sm_in_effect


@pytest.mark.parametrize(
    "hardening, needs",
    [
        ("all, -m_key, allowed_sm", "m_key"),
        ("all, -sm_key, allowed_sm", "sm_key"),
        ("sa_key, allowed_sm", "m_key and sm_key"),
    ],
)
def test_allowed_sm_needs_the_m_key_and_sm_key(tmp_path, hardening, needs):
    with pytest.raises(config_mod.ConfigError, match=f"needs the {needs} group"):
        _load(tmp_path, f"[opensm]\nhardening = {hardening}\n")


def test_allowed_sm_needs_m_key_protection(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="protection level 1"):
        _load(
            tmp_path,
            "[opensm]\nhardening = all, allowed_sm\n"
            "[opensm_conf]\nm_key_protection_level = 0\n",
        )


@pytest.mark.parametrize("driver", ["docker", "none"])
def test_allowed_sm_needs_the_managed_driver(tmp_path, driver):
    with pytest.raises(config_mod.ConfigError, match="managed driver"):
        _load(tmp_path, f"[opensm]\ndriver = {driver}\nhardening = all, allowed_sm\n")


@pytest.mark.parametrize(
    "value, guids",
    [
        ("0", ()),
        ("0x0002c903000e0b72", (OWN_A,)),
        ("0x0002C903000E0B73,0x0002c903000e0b72", (OWN_A, OWN_B)),
        # OpenSM splits on commas, spaces and tabs
        ("0x0002c903000e0b72, 0x0002c903000e0b73", (OWN_A, OWN_B)),
        ("0x0002c903000e0b72 \t0x0002c903000e0b73,", (OWN_A, OWN_B)),
        ("0x1,0x1", (1,)),
    ],
)
def test_allowed_sm_guids_spellings_accepted(value, guids):
    assert config_mod.parse_allowed_sm_guids(value) == guids


@pytest.mark.parametrize(
    "value",
    [
        "",
        ",",
        "0x0002c903000e0b72,0",
        "0,0",
        "0x0",
        "0002c903000e0b72",
        "0x10002c903000e0b72",
        "0xg",
        "0x1;0x2",
        "-0x1",
    ],
)
def test_allowed_sm_guids_spellings_refused(tmp_path, value):
    assert config_mod.parse_allowed_sm_guids(value) is None
    if value.strip():
        with pytest.raises(config_mod.ConfigError, match="allowed_sm_guids must be"):
            _load(tmp_path, f"[opensm_conf]\nallowed_sm_guids = {value}\n")


def test_an_override_puts_the_list_in_effect_with_its_checks(tmp_path):
    config = _load(tmp_path, "[opensm_conf]\nallowed_sm_guids = 0x1\n")
    assert config.allowed_sm_in_effect
    with pytest.raises(config_mod.ConfigError, match="allowed_sm_guids needs the"):
        _load(
            tmp_path,
            "[opensm]\nhardening = all, -sm_key\n"
            "[opensm_conf]\nallowed_sm_guids = 0x1\n",
        )


def test_the_managers_spelling():
    assert config_mod.format_allowed_sm_guids(()) == "0"
    assert (
        config_mod.format_allowed_sm_guids([OWN_B, 0x1])
        == "0x0000000000000001,0x0002c903000e0b73"
    )


def _ha_config(**kwargs):
    return config_mod.Config(
        token="x",
        ha=True,
        connection="mysql+pymysql://u:p@h/afm",
        job_timeout=400,
        **kwargs,
    )


def test_ha_refuses_allowed_sm():
    config = _ha_config(hardening="all, allowed_sm")
    assert any("allowed SMs" in p for p in config_mod._ha_problems(config))
    config = _ha_config(opensm_conf={"allowed_sm_guids": "0x1"})
    assert any("allowed SMs" in p for p in config_mod._ha_problems(config))
    assert not any(
        "allowed SMs" in p for p in config_mod._ha_problems(_ha_config(hardening="all"))
    )


# Rendering


def test_the_group_renders_the_generated_list():
    content = opensm_conf.render(
        {},
        hardening={"m_key", "sm_key", "allowed_sm"},
        keys=dict(m_key="0x1", sm_key="0x2"),
        generated={"allowed_sm_guids": "0x0002c903000e0b72"},
    )
    assert "allowed_sm_guids 0x0002c903000e0b72" in content.splitlines()


def test_without_the_group_no_list_is_rendered():
    content = opensm_conf.render(
        {},
        hardening={"m_key"},
        keys=dict(m_key="0x1"),
        generated={"allowed_sm_guids": "0x1"},
    )
    assert "allowed_sm_guids" not in content


def test_the_group_without_a_generated_list_fails_loudly():
    with pytest.raises(KeyError):
        opensm_conf.render({}, hardening={"allowed_sm"}, keys={})


# The lock-out checks


@pytest.mark.parametrize(
    "listed, own, bound, problem",
    [
        ((OWN_A, OWN_B), {OWN_A, OWN_B}, None, None),
        ((OWN_A, OWN_B, TENANT), {OWN_A, OWN_B}, None, None),
        ((OWN_A,), {OWN_A, OWN_B}, None, "leaves out this host's physical ports"),
        ((OWN_A,), {OWN_A, OWN_B}, "0x0002c903000e0b72", None),
        ((OWN_A,), {OWN_A, OWN_B}, "0x0002c903000e0b73", "binds OpenSM to"),
        ((OWN_A,), {OWN_A}, "bad", "isn't a GUID"),
        ((), {OWN_A}, None, None),
        ((OWN_A,), set(), None, "found no physical InfiniBand port"),
        ((), set(), None, "found no physical InfiniBand port"),
    ],
)
def test_lockout_problem(listed, own, bound, problem):
    found = allowed_sm.lockout_problem(listed, own, bound)
    if problem is None:
        assert found is None
    else:
        assert problem in found


# The port probe's and the query's output


def _probe_output(*ports):
    lines = [f"PORT mlx5_{i} 1 fe80:0000:0000:0000:{gid}" for i, gid in ports]
    return "\n".join(lines + ["END"]) + "\n"


def test_parse_ports():
    output = _probe_output((0, "0002:c903:000e:0b72"), (1, "0002:c903:000e:0b73"))
    assert allowed_sm.parse_ports(output) == {OWN_A, OWN_B}
    assert allowed_sm.parse_ports("END\n") == set()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "PORT mlx5_0 1 fe80:0000:0000:0000:0002:c903:000e:0b72\n",
        "garbage\nEND\n",
        "PORT mlx5_0 1 fe80:0000:0000:0000:0000:0000:0000:0000\nEND\n",
        "PORT mlx5_0 1 fe80:0000:0000:0000:ffff:ffff:ffff:ffff\nEND\n",
        "PORT mlx5_0;x 1 fe80:0000:0000:0000:0002:c903:000e:0b72\nEND\n",
    ],
)
def test_a_probe_that_cant_be_read_is_no_evidence(output):
    with pytest.raises(allowed_sm.ProbeError):
        allowed_sm.parse_ports(output)


@pytest.mark.parametrize(
    "output, state, capmask, clear",
    [
        ("STATE 0\nCAPMASK 0x2651e84a\n", 0, 0x2651E84A, True),
        ("STATE 3\nCAPMASK 0x2651e848\n", 3, 0x2651E848, True),
        ("STATE 3\nCAPMASK 0x2651e84a\n", 3, 0x2651E84A, False),
        ("STATE 2\n", 2, None, False),
        ("CAPMASK 0x2651e848\n", None, 0x2651E848, True),
        ("", None, None, False),
        ("STATE x\nMkey 0x1\nCAPMASK 0x2\nCAPMASK 0x0\n", None, 0x2, False),
    ],
)
def test_parse_query(output, state, capmask, clear):
    result = allowed_sm.parse_query(output)
    assert (result.state, result.capmask, result.clear) == (state, capmask, clear)


# The scripts, for real


def _tools(tmp_path, names):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in names:
        real = shutil.which(name, path="/usr/bin:/bin")
        assert real, name
        real = os.path.realpath(real)
        assert not os.path.basename(real).startswith(("smp", "ib", "sa", "sm")), real
        if not (tools / name).exists():
            os.symlink(real, tools / name)
    return tools


def _device(root, name, ports, physfn=False, sf=False):
    """A device in a fake /sys/class/infiniband: ports as {n: (link layer,
    GID 0)}, its device a link to a PCI function or an SF"""
    devices = root.parent / "devices"
    target = devices / (f"mlx5_core.sf.{name[-1]}" if sf else f"0000:0{name[-1]}:00.0")
    target.mkdir(parents=True)
    if physfn:
        (target / "physfn").mkdir()
    (root / name).mkdir(parents=True)
    os.symlink(target, root / name / "device")
    for port, (layer, gid) in ports.items():
        directory = root / name / "ports" / str(port)
        (directory / "gids").mkdir(parents=True)
        (directory / "link_layer").write_text(layer + "\n")
        (directory / "gids" / "0").write_text(gid + "\n")


def _run_ports(tmp_path, root):
    tools = _tools(tmp_path, ("sh", "readlink", "cat"))
    return subprocess.run(
        [str(tools / "sh"), "-c", allowed_sm.PORTS_SCRIPT],
        env={"PATH": str(tools), "AFM_SYSFS": str(root), "LC_ALL": "C"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_port_probe_finds_only_physical_infiniband_ports(tmp_path):
    root = tmp_path / "sys" / "class" / "infiniband"
    _device(
        root,
        "mlx5_0",
        {
            1: ("InfiniBand", "fe80:0000:0000:0000:0002:c903:000e:0b72"),
            2: ("InfiniBand", "fe80:0000:0000:0000:0002:c903:000e:0b73"),
        },
    )
    # A virtual function, a sub-function, and an Ethernet port
    _device(
        root,
        "mlx5_1",
        {1: ("InfiniBand", "fe80:0000:0000:0000:1234:5600:0078:21b6")},
        physfn=True,
    )
    _device(
        root,
        "mlx5_2",
        {1: ("InfiniBand", "fe80:0000:0000:0000:1234:5600:0078:21b7")},
        sf=True,
    )
    _device(
        root,
        "mlx5_3",
        {1: ("Ethernet", "fe80:0000:0000:0000:0202:c9ff:fe00:0001")},
    )
    result = _run_ports(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert allowed_sm.parse_ports(result.stdout) == {OWN_A, OWN_B}


def test_the_port_probe_without_infiniband_fails(tmp_path):
    result = _run_ports(tmp_path, tmp_path / "nothing")
    assert result.returncode != 0
    root = tmp_path / "sys" / "class" / "infiniband"
    root.mkdir(parents=True)
    result = _run_ports(tmp_path, root)
    assert result.returncode == 0
    assert allowed_sm.parse_ports(result.stdout) == set()


FAKE_SMINFO = r"""#!/bin/sh
echo "sminfo $*" >> "$AFM_FAKE_DIR/invocations"
[ "$1" = -G ] && [ "$2" = -y ] || exit 9
echo "ibwarn: sending with m_key $3" >&2
head="sminfo: sm lid 7 sm guid $4, activity count 12 priority 15"
case ${AFM_FAKE_SM:-master} in
    master) echo "$head state 3 SMINFO_MASTER" ;;
    notactive) echo "$head state 0 SMINFO_NOTACT" ;;
    none) exit 255 ;;
esac
"""

FAKE_SMPQUERY = r"""#!/bin/sh
echo "smpquery $*" >> "$AFM_FAKE_DIR/invocations"
[ "$1" = -G ] && [ "$2" = -y ] && [ "$4" = portinfo ] || exit 9
echo "Mkey:............................$3"
echo "Lid:.............................7"
case ${AFM_FAKE_SM:-master} in
    none) echo "CapMask:.........................0x2651e848" ;;
    *) echo "CapMask:.........................0x2651e84a"
       echo "				IsSM" ;;
esac
"""


def _run_query(tmp_path, environment):
    tools = _tools(tmp_path, ("sh", "timeout", "awk", "grep", "cat"))
    fakes = tmp_path / "bin"
    fakes.mkdir(exist_ok=True)
    for name, text in (("sminfo", FAKE_SMINFO), ("smpquery", FAKE_SMPQUERY)):
        (fakes / name).write_text(text)
        (fakes / name).chmod(0o755)
    return subprocess.run(
        [str(tools / "sh"), "-c", allowed_sm.QUERY_SCRIPT],
        env={
            "PATH": f"{fakes}:{tools}",
            "AFM_FAKE_DIR": str(tmp_path),
            "LC_ALL": "C",
            **environment,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


QUERY_ENV = {
    "AFM_GUID": f"0x{OLD_HOST:016x}",
    "AFM_KEY": KEY,
    "AFM_QUERY_TIMEOUT": "5",
}


@pytest.mark.parametrize(
    "sm, state, clear",
    [("master", 3, False), ("notactive", 0, True), ("none", None, True)],
)
def test_the_query_prints_only_the_state_and_capability_mask(
    tmp_path, sm, state, clear
):
    result = _run_query(tmp_path, {**QUERY_ENV, "AFM_FAKE_SM": sm})
    assert result.returncode == 0
    assert result.stderr == ""
    assert KEY[2:] not in result.stdout.lower()
    assert "Mkey" not in result.stdout
    query = allowed_sm.parse_query(result.stdout)
    assert query.state == state
    assert query.clear is clear
    calls = (tmp_path / "invocations").read_text().splitlines()
    assert f"sminfo -G -y {KEY} 0x{OLD_HOST:016x}" in calls
    assert f"smpquery -G -y {KEY} portinfo 0x{OLD_HOST:016x}" in calls


@pytest.mark.parametrize(
    "environment",
    [
        {**QUERY_ENV, "AFM_GUID": "0x1; reboot"},
        {**QUERY_ENV, "AFM_KEY": "0x1"},
        {**QUERY_ENV, "AFM_QUERY_TIMEOUT": "x"},
        {"AFM_KEY": KEY, "AFM_QUERY_TIMEOUT": "5"},
    ],
)
def test_the_query_refuses_a_bad_environment(tmp_path, environment):
    result = _run_query(tmp_path, environment)
    assert result.returncode == 2
    assert not (tmp_path / "invocations").exists()


# The managed driver


def test_a_rejected_list_fails_a_start(managed_config):
    log = (
        "Nov 17 00:00:00 000000 [1] 0x01 -> osm_update_allowed_sm_guids: ERR 7549: "
        "allowed_sm_guids param is not valid. Should be GUIDs in comma separated "
        "values format. Disabling the feature\n" + test_docker.STARTED
    )
    client = test_docker.FakeManagedClient(log=log)
    with pytest.raises(ReloadError, match="ERR 7549"):
        docker_driver.ManagedDocker(managed_config, client=client).start()
    assert client.existing.removed


def test_a_rejected_list_fails_an_apply():
    after = test_docker.SWEEP.replace(
        "Nov 05 16:59:16 191669",
        "Nov 05 16:59:16 191000 [1] 0x01 -> osm_update_allowed_sm_guids: ERR 7549: "
        "allowed_sm_guids param is not valid. Disabling the feature\n"
        "Nov 05 16:59:16 191669",
    )
    container = test_docker.FakeContainer(after=after)
    with pytest.raises(ReloadError, match="ERR 7549"):
        test_docker._reloader(container).reload()
    # One logged before the signal isn't this apply's.
    container = test_docker.FakeContainer(
        before="x: ERR 7549: allowed_sm_guids param is not valid",
        after=test_docker.SWEEP,
    )
    test_docker._reloader(container).reload()


class LoggingContainer(test_docker.FakeManagedContainer):
    """OpenSM's container whose log has the daemon's timestamps"""

    def __init__(self, lines, container_id="c1"):
        super().__init__(status="running")
        self.id = container_id
        self.lines = list(lines)
        self.calls = []

    def logs(self, stdout, stderr, since=None, timestamps=False):
        self.calls.append((since, timestamps))
        assert timestamps
        shown = [
            line
            for line in self.lines
            if since is None
            or docker_driver._log_time(line.split()[0]) >= since * 10**9
        ]
        return "".join(line + "\n" for line in shown).encode()


def _ignored(guid, time):
    return (
        f"2026-11-17T10:00:{time} [1] 0x01 -> osm_sminfo_rcv: ERR 0F09: Ignoring SM "
        f"on port 0x{guid:x}, is not part of the allowed port GUIDs"
    )


def test_log_findings_reads_each_line_once(managed_config):
    container = LoggingContainer(
        [
            "2026-11-17T10:00:01.000000001Z Nov 17 10:00:01 SUBNET UP",
            _ignored(TENANT, "01.5Z").replace("10:00:01.5Z", "10:00:01.5Z"),
        ]
    )
    client = test_docker.FakeManagedClient(existing=container)
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    assert driver.log_findings().ignored_sms == [TENANT]
    assert container.calls[-1] == (None, True)
    assert driver.log_findings().ignored_sms == []
    container.lines.append(_ignored(OLD_HOST, "02.25Z"))
    container.lines.append(_ignored(TENANT, "03Z"))
    assert driver.log_findings().ignored_sms == [OLD_HOST, TENANT]
    # From the second before the last line read, in whole seconds
    assert container.calls[-1] == (
        docker_driver._log_time("2026-11-17T10:00:01.5Z") // 10**9,
        True,
    )
    # A new container's log is read from its start.
    client.existing = LoggingContainer([_ignored(TENANT, "00Z")], container_id="c2")
    assert driver.log_findings().ignored_sms == [TENANT]


def test_log_findings_needs_a_running_opensm(managed_config):
    client = test_docker.FakeManagedClient()
    with pytest.raises(ReloadError):
        docker_driver.ManagedDocker(managed_config, client=client).log_findings()


def test_log_time():
    assert docker_driver._log_time("2026-11-17T10:00:00Z") % 10**9 == 0
    assert docker_driver._log_time("2026-11-17T10:00:00.5Z") % 10**9 == 5 * 10**8
    assert docker_driver._log_time(
        "2026-11-17T10:00:00.123456789+00:00"
    ) == docker_driver._log_time("2026-11-17T10:00:00.123456789Z")
    assert docker_driver._log_time("Nov") is None


def test_query_sm_port_keeps_the_key_off_the_command_line(managed_config):
    container = test_docker.FakeManagedContainer()
    container.exec_result = (0, b"STATE 0\nCAPMASK 0x2651e84a\n")
    client = test_docker.FakeManagedClient(existing=container)
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    result = driver.query_sm_port(OLD_HOST, KEY)
    assert result.clear and result.state == 0
    ((command, environment, stdout, stderr),) = container.execs
    assert command == ["sh", "-c", allowed_sm.QUERY_SCRIPT]
    assert KEY not in " ".join(command)
    assert environment["AFM_KEY"] == KEY
    assert environment["AFM_GUID"] == f"0x{OLD_HOST:016x}"
    assert stdout and not stderr
    container.exec_result = (2, b"")
    with pytest.raises(ReloadError, match="status 2"):
        driver.query_sm_port(OLD_HOST, KEY)


def test_physical_ports_run_in_a_helper(managed_config):
    helper = test_docker.FakeHelper(
        output=_probe_output((0, "0002:c903:000e:0b72")).encode()
    )
    driver, client = test_docker._helper_driver(managed_config, helper)
    assert driver.physical_ports() == {OWN_A}
    (kwargs,) = client.created
    assert kwargs["command"] == [allowed_sm.PORTS_SCRIPT]
    assert kwargs["image"] == managed_config.image
    assert helper.events[-1] == ("remove", True)
    helper = test_docker.FakeHelper(code=4)
    driver, client = test_docker._helper_driver(managed_config, helper)
    with pytest.raises(allowed_sm.ProbeError, match="status 4"):
        driver.physical_ports()


def test_physical_ports_pull_a_missing_image_first(managed_config):
    helper = test_docker.FakeHelper(output=b"END\n")
    driver, client = test_docker._helper_driver(managed_config, helper, present=False)
    client.images.failures = 1
    with pytest.raises(allowed_sm.ProbeError, match="can't pull"):
        driver.physical_ports()
    assert client.created == []
    # The next start's probe pulls it, and runs.
    assert driver.physical_ports() == set()
    assert len(client.images.pulls) == 2


@pytest.fixture
def managed_config(config):
    config.driver = "managed"
    config.start_timeout = 0.05
    config.opensm_conf = {}
    return config


# The manager


class FakeOpenSM(conftest.FakeReloader):
    """The managed driver's allowed SM hooks, over the fake reloader"""

    def __init__(self):
        super().__init__()
        self.ports = {OWN_A, OWN_B}
        self.probe_error = None
        self.findings = []
        self.findings_error = None
        self.answers = {}
        self.queries = []
        self.fabric_result = Fabric({SWITCH}, [(OWN_A, 3), (TENANT, 3)], {OWN_A, OWN_B})

    def physical_ports(self):
        if self.probe_error is not None:
            raise self.probe_error
        return set(self.ports)

    def log_findings(self):
        if self.findings_error is not None:
            raise self.findings_error
        findings, self.findings = self.findings, []
        return LogFindings(findings)

    def query_sm_port(self, guid, m_key):
        self.queries.append((guid, m_key))
        answer = self.answers.get(guid, allowed_sm.PortQuery())
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def sm_driver():
    return FakeOpenSM()


@pytest.fixture
def sm_config(config):
    config.driver = "managed"
    config.hardening = "all, allowed_sm"
    return config


@pytest.fixture
def sm_manager(sm_config, sm_driver, monkeypatch):
    monkeypatch.setattr(manager_mod, "RETRY_DELAY", 0)
    manager = manager_mod.Manager(
        sm_config, db.make_sessionmaker(sm_config.database_url), sm_driver
    )
    with manager.sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid=f"{SWITCH:016x}"))
        session.commit()
    # Another master in the SA's records is judged as before: these tests
    # are about SMs OpenSM ignores, which the SA doesn't list.
    sm_driver.fabric_result = Fabric({SWITCH}, [(OWN_A, 3)], {OWN_A, OWN_B})
    return manager


def _conf(config):
    with open(config.opensm_conf_file) as f:
        return dict(
            line.split(" ", 1)
            for line in f.read().splitlines()
            if line and not line.startswith("#")
        )


def _known(manager):
    with manager.sessionmaker() as session:
        return {
            int(row.guid, 16): row.own
            for row in session.scalars(sa.select(db.KnownSMPort))
        }


def test_a_start_lists_this_hosts_physical_ports(sm_manager, sm_config, sm_driver):
    sm_manager._start_opensm()
    assert sm_driver.starts == 1
    assert (
        _conf(sm_config)["allowed_sm_guids"] == "0x0002c903000e0b72,0x0002c903000e0b73"
    )
    assert _known(sm_manager) == {OWN_A: True, OWN_B: True}
    # A port the next probe doesn't find stays known, but isn't this host's.
    sm_driver.ports = {OWN_A}
    sm_manager._start_opensm()
    assert _conf(sm_config)["allowed_sm_guids"] == "0x0002c903000e0b72"
    assert _known(sm_manager) == {OWN_A: True, OWN_B: False}


def test_an_override_is_written_in_the_managers_spelling(sm_manager, sm_config):
    sm_config.opensm_conf = {
        "allowed_sm_guids": "0x2C903000E0B73 0x0002c903000e0b72, 0x1234"
    }
    sm_manager._start_opensm()
    assert _conf(sm_config)["allowed_sm_guids"] == (
        "0x0000000000001234,0x0002c903000e0b72,0x0002c903000e0b73"
    )


def test_an_override_without_the_group_is_checked_too(sm_manager, sm_config, sm_driver):
    sm_config.hardening = "all"
    sm_config.opensm_conf = {"allowed_sm_guids": "0x0002c903000e0b72"}
    with pytest.raises(ReloadError, match="leaves out this host's physical ports"):
        sm_manager._start_opensm()
    assert sm_driver.starts == 0


@pytest.mark.parametrize(
    "opensm_conf, ports, probe_error, message",
    [
        (
            {"allowed_sm_guids": "0x0002c903000e0b72"},
            {OWN_A, OWN_B},
            None,
            "leaves out this host's physical ports 0x0002c903000e0b73",
        ),
        (
            {"allowed_sm_guids": "0x0002c903000e0b72", "guid": "0x0002c903000e0b73"},
            {OWN_A, OWN_B},
            None,
            r"the port \[opensm_conf\] guid binds OpenSM to",
        ),
        ({}, set(), None, "found no physical InfiniBand port"),
        ({}, None, allowed_sm.ProbeError("the port probe exited"), "can't read"),
    ],
)
def test_a_list_that_would_lock_opensm_out_refuses_the_start(
    sm_manager, sm_config, sm_driver, caplog, opensm_conf, ports, probe_error, message
):
    sm_config.opensm_conf = opensm_conf
    if ports is not None:
        sm_driver.ports = ports
    sm_driver.probe_error = probe_error
    with caplog.at_level(logging.ERROR), pytest.raises(ReloadError, match=message):
        sm_manager._start_opensm()
    assert sm_driver.starts == 0
    assert not os.path.exists(sm_config.opensm_conf_file)
    assert "Not starting OpenSM" in caplog.text


def test_a_list_bound_to_one_port_passes(sm_manager, sm_config, sm_driver):
    sm_config.opensm_conf = {
        "allowed_sm_guids": "0x0002c903000e0b72",
        "guid": "0x0002c903000e0b72",
    }
    sm_manager._start_opensm()
    assert sm_driver.starts == 1


def test_no_other_sm_passes_on_a_single_node(sm_manager, sm_config, sm_driver):
    sm_config.opensm_conf = {"allowed_sm_guids": "0"}
    sm_manager._start_opensm()
    assert _conf(sm_config)["allowed_sm_guids"] == "0"


def test_a_refused_start_doesnt_end_the_manager(sm_manager, sm_driver):
    sm_driver.ports = set()
    sm_driver.is_running = False
    sm_manager.config.check_interval = 0.01
    sm_manager.start()
    try:
        assert sm_driver.starts == 0
        # The applier tries again at every check, and starts once it can.
        sm_driver.ports = {OWN_A, OWN_B}
        deadline = time.monotonic() + 10
        while not sm_driver.starts and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sm_driver.starts
    finally:
        sm_manager.halt()


def test_without_the_list_nothing_is_probed(config, sm_driver):
    config.driver = "managed"
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), sm_driver
    )
    sm_driver.probe_error = AssertionError("probed")
    sm_driver.findings_error = AssertionError("read the log")
    manager._start_opensm()
    assert "allowed_sm_guids" not in _conf(config)
    manager.check_fabric()


def test_an_ignored_unknown_sm_is_alarmed_once(sm_manager, sm_driver, caplog):
    sm_manager._start_opensm()
    sm_driver.findings = [TENANT, TENANT]
    with caplog.at_level(logging.ERROR):
        assert sm_manager.check_fabric()
    assert caplog.text.count(f"0x{TENANT:016x}") == 1
    assert "isn't a known SM port" in caplog.text
    assert not sm_manager._contested_fabric
    caplog.clear()
    sm_driver.findings = [TENANT]
    with caplog.at_level(logging.ERROR):
        assert sm_manager.check_fabric()
    assert f"0x{TENANT:016x}" not in caplog.text
    assert sm_driver.queries == []
    # Again for the next OpenSM
    sm_manager._start_opensm()
    sm_driver.findings = [TENANT]
    with caplog.at_level(logging.ERROR):
        assert sm_manager.check_fabric()
    assert f"0x{TENANT:016x}" in caplog.text


def test_an_ignored_known_sm_contests_until_a_query_clears_it(sm_manager, sm_driver):
    sm_manager._start_opensm()
    assert sm_manager.allowed_sms.record(OLD_HOST)
    sm_driver.findings = [OLD_HOST]
    assert not sm_manager.check_fabric()
    assert sm_manager._contested_fabric
    with sm_manager.sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        assert applied.contested and "0F09" in applied.check_detail
    # No answer, or an SM that still acts, clears nothing; nor does a query
    # that can't be sent. Not logged again, it stays contested.
    for answer in (
        allowed_sm.PortQuery(),
        allowed_sm.PortQuery(state=3, capmask=0x2),
        ReloadError("no"),
    ):
        sm_driver.answers[OLD_HOST] = answer
        assert not sm_manager.check_fabric()
        assert sm_manager._contested_fabric
    assert all(guid == OLD_HOST for guid, _ in sm_driver.queries)
    assert {key for _, key in sm_driver.queries} == {sm_manager.keys()["m_key"]}
    # NOT-ACTIVE clears it, and the check passes.
    sm_driver.answers[OLD_HOST] = allowed_sm.PortQuery(state=0)
    assert sm_manager.check_fabric()
    assert not sm_manager._contested_fabric


def test_no_sm_bound_clears_a_contest(sm_manager, sm_driver):
    sm_manager._start_opensm()
    sm_manager.allowed_sms.record(OLD_HOST)
    sm_driver.findings = [OLD_HOST]
    sm_driver.answers[OLD_HOST] = allowed_sm.PortQuery(capmask=0x2651E848)
    # Cleared by the same check's query
    assert sm_manager.check_fabric()


def test_this_hosts_ports_are_known_sm_ports(sm_manager, sm_config, sm_driver):
    # An override that leaves one of them out, with OpenSM bound to the other:
    # an SM on the left-out port, on this host, holds the keys.
    sm_config.opensm_conf = {
        "allowed_sm_guids": "0x0002c903000e0b72",
        "guid": "0x0002c903000e0b72",
    }
    sm_manager._start_opensm()
    sm_driver.findings = [OWN_B]
    assert not sm_manager.check_fabric()
    assert sm_manager._contested_fabric


def test_with_overridden_keys_every_ignored_sm_contests(
    sm_manager, sm_config, sm_driver
):
    sm_config.opensm_conf = {"m_key": "0x00000000000000aa"}
    sm_manager._start_opensm()
    sm_driver.findings = [TENANT]
    assert not sm_manager.check_fabric()
    assert sm_manager._contested_fabric
    assert sm_driver.queries[-1] == (TENANT, "0x00000000000000aa")


def test_dropping_a_contested_port_ends_its_contest(sm_manager, sm_driver):
    sm_manager._start_opensm()
    sm_manager.allowed_sms.record(OLD_HOST)
    sm_driver.findings = [OLD_HOST]
    assert not sm_manager.check_fabric()
    sm_manager.allowed_sms.drop(OLD_HOST)
    assert sm_manager.check_fabric()
    assert not sm_manager._contested_fabric


def test_a_log_that_cant_be_read_fails_the_check(sm_manager, sm_driver):
    sm_manager._start_opensm()
    sm_driver.findings_error = ReloadError("Docker: can't read OpenSM's log")
    assert not sm_manager.check_fabric()
    with sm_manager.sessionmaker() as session:
        assert "can't read OpenSM's log" in session.get(db.Applied, 1).check_detail


def test_the_query_uses_a_per_port_rows_key(sm_manager):
    sms = sm_manager.allowed_sms
    keys = sm_manager.keys()
    assert sms._port_key(OLD_HOST) == keys["m_key"]
    base = port_keys.parse_key(keys["m_key_base"])
    with sm_manager.sessionmaker() as session:
        session.add(
            db.FabricPort(guid=port_keys.guid_text(OLD_HOST), holds=port_keys.DERIVED)
        )
        session.commit()
    assert sms._port_key(OLD_HOST) == port_keys.format_key(
        port_keys.port_m_key(base, OLD_HOST)
    )
    # A port with no row gets the M_Key OpenSM uses with per-port keys off.
    assert sms._port_key(TENANT) == keys["m_key"]
    # A port whose key isn't known isn't sent the stored M_Key by guess.
    with sm_manager.sessionmaker() as session:
        row = session.get(db.FabricPort, port_keys.guid_text(OLD_HOST))
        row.holds = port_keys.UNKNOWN
        session.commit()
    assert sms._port_key(OLD_HOST) is None
    assert sms._query(OLD_HOST) is None


# The commands


def test_the_commands(sm_manager):
    sms = sm_manager.allowed_sms
    sm_manager._start_opensm()
    assert sms.record(OLD_HOST)
    assert not sms.record(OLD_HOST)
    assert sms.known_ports() == [
        "0x0002c90300005678 recorded",
        "0x0002c903000e0b72 this host",
        "0x0002c903000e0b73 this host",
    ]
    with pytest.raises(allowed_sm.AllowedSMError, match="this host's port"):
        sms.drop(OWN_A)
    sms.drop(OLD_HOST)
    with pytest.raises(allowed_sm.AllowedSMError, match="isn't a known SM port"):
        sms.drop(OLD_HOST)


def test_the_commands_refuse_ha(sm_manager, sm_config):
    sm_config.ha = True
    for command in (
        sm_manager.allowed_sms.known_ports,
        lambda: sm_manager.allowed_sms.record(OLD_HOST),
        lambda: sm_manager.allowed_sms.drop(OLD_HOST),
    ):
        with pytest.raises(allowed_sm.AllowedSMError, match="HA mode"):
            command()


def test_the_cli_commands(tmp_path, capsys):
    from alternative_fabric_manager import cli

    path = tmp_path / "afm.conf"
    path.write_text(
        "[api]\ntoken = x\n[database]\n"
        f"database = {tmp_path / 'afm.sqlite'}\n"
        "[opensm]\nhardening = all, allowed_sm\n"
    )
    cli.main(["--config-file", str(path), "--record-sm-port", f"{OLD_HOST:x}"])
    cli.main(["--config-file", str(path), "--known-sm-ports"])
    assert "0x0002c90300005678 recorded" in capsys.readouterr().out
    cli.main(["--config-file", str(path), "--drop-sm-port", f"0x{OLD_HOST:x}"])
    with pytest.raises(SystemExit, match="isn't a known SM port"):
        cli.main(["--config-file", str(path), "--drop-sm-port", f"0x{OLD_HOST:x}"])
    with pytest.raises(SystemExit, match="isn't a GUID"):
        cli.main(["--config-file", str(path), "--record-sm-port", "nope"])
