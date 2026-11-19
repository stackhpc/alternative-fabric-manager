"""Class keys (specs/secured-profile.md, Class keys): the group's options,
the seed rule, the turn-off guard, and what OpenSM's key managers report"""

import logging

import pytest

from alternative_fabric_manager import class_keys, db, opensm_conf
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric, LogFindings
from alternative_fabric_manager.drivers import docker as docker_driver
from tests import conftest

SWITCH = 0xEC0D9A0300B12560
OWN = 0x0002C903000E0B72
PORT = 0x0002C90300001234
SEED = "0x00000000000000ee"


def test_class_keys_is_experimental():
    assert "class_keys" in config_mod.HARDENING_GROUPS
    assert "class_keys" not in config_mod.ALL_HARDENING_GROUPS
    assert "class_keys" not in config_mod.parse_hardening("all")
    assert "class_keys" in config_mod.parse_hardening("all, class_keys")


def test_ha_refuses_class_keys():
    config = config_mod.Config(
        token="x",
        ha=True,
        connection="mysql+pymysql://u:p@h/afm",
        job_timeout=400,
        hardening="all, class_keys",
    )
    assert any("class keys" in p for p in config_mod._ha_problems(config))


def test_the_group_renders_every_class_with_the_seed_and_the_lease():
    content = opensm_conf.render(
        {},
        hardening={"class_keys"},
        keys={"key_mgr_seed": SEED},
        key_lease_period=600,
    )
    lines = content.splitlines()
    for name in ("vs", "cc", "n2n"):
        assert f"{name}_key_enable 2" in lines
        assert f"{name}_key_lease_period 600" in lines
    assert "vs_key_ci_protect_bits 1" in lines
    assert "cc_key_protect_bit 1" in lines
    assert "n2n_key_protect_bit 1" in lines
    assert f"key_mgr_seed {SEED}" in lines
    # An override wins, as for any option.
    content = opensm_conf.render(
        {"cc_key_enable": "1"}, hardening={"class_keys"}, keys={"key_mgr_seed": SEED}
    )
    assert "cc_key_enable 1" in content.splitlines()
    assert "cc_key_enable 2" not in content


@pytest.mark.parametrize(
    "groups, overrides, expected",
    [
        ({"class_keys"}, {}, {"vs": 2, "cc": 2, "n2n": 2}),
        (set(), {}, {"vs": None, "cc": None, "n2n": None}),
        ({"class_keys"}, {"vs_key_enable": "1"}, {"vs": 1, "cc": 2, "n2n": 2}),
        (set(), {"cc_key_enable": "0x2"}, {"vs": None, "cc": 2, "n2n": None}),
        ({"class_keys"}, {"n2n_key_enable": "on"}, {"vs": 2, "cc": 2, "n2n": 2}),
    ],
)
def test_enables(groups, overrides, expected):
    assert class_keys.enables(groups, overrides) == expected


@pytest.mark.parametrize(
    "recorded, groups, overrides, problem",
    [
        (set(), set(), {}, None),
        ({"vs"}, {"class_keys"}, {}, None),
        ({"vs"}, {"class_keys"}, {"vs_key_enable": "1"}, None),
        ({"vs"}, set(), {"vs_key_enable": "1"}, "without the class_keys group"),
        ({"vs", "cc"}, set(), {}, "without the class_keys group"),
        ({"vs"}, {"class_keys"}, {"vs_key_enable": "0"}, "vs keys"),
        ({"vs"}, {"class_keys"}, {"cc_key_enable": "0"}, None),
    ],
)
def test_guard_problem(recorded, groups, overrides, problem):
    found = class_keys.guard_problem(recorded, groups, overrides)
    if problem is None:
        assert found is None
    else:
        assert problem in found


def test_keys_enabled_by_hand_without_a_seed_can_be_cleared_without_the_group():
    # Naming the group would write the stored seed, not the one the keys came
    # from: enable 1 without the group is the turn-off there.
    clearing = {"vs_key_enable": "1"}
    assert class_keys.guard_problem({"vs"}, set(), clearing, seeded=False) is None
    # Only when every recorded class is cleared
    found = class_keys.guard_problem({"vs", "cc"}, set(), clearing, seeded=False)
    assert "without the class_keys group" in found
    # With a seed in the last opensm.conf, the group's procedure still applies.
    found = class_keys.guard_problem({"vs"}, set(), clearing, seeded=True)
    assert "without the class_keys group" in found


def test_the_record_says_whether_a_seed_was_written(ck_config, sessionmaker):
    manager = manager_mod.Manager(ck_config, sessionmaker, FakeOpenSM())
    keeper = manager.class_keys
    keeper.record(set(), {"vs_key_enable": "2"})
    assert keeper.recorded() == {"vs"} and not keeper.seeded()
    keeper.record({"class_keys"}, {})
    assert keeper.seeded()
    keeper.record(set(), {"vs_key_enable": "2", "key_mgr_seed": "0x12"})
    assert keeper.seeded()
    # A record from before the bit, which has none, keeps the strict rule.
    with sessionmaker() as session:
        session.get(db.State, class_keys.RECORD).value = class_keys.CLASSES["vs"]
        session.commit()
    assert keeper.seeded()


class FakeOpenSM(conftest.FakeReloader):
    def __init__(self):
        super().__init__()
        self.findings = LogFindings()
        self.fabric_result = Fabric({SWITCH}, [(OWN, 3)], {OWN})

    def log_findings(self):
        findings, self.findings = self.findings, LogFindings()
        return findings


@pytest.fixture
def ck_config(config):
    config.driver = "managed"
    config.hardening = "all, class_keys"
    return config


@pytest.fixture
def sessionmaker(ck_config):
    sessionmaker = db.make_sessionmaker(ck_config.database_url)
    with sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid=f"{SWITCH:016x}"))
        session.commit()
    return sessionmaker


def _start(config, sessionmaker, driver=None):
    """A manager's start, as the process does it, its applier then stopped"""
    manager = manager_mod.Manager(config, sessionmaker, driver or FakeOpenSM())
    manager.start()
    manager.halt()
    return manager


def _conf(config):
    with open(config.opensm_conf_file) as f:
        return [line for line in f.read().splitlines() if not line.startswith("#")]


def test_a_start_that_stores_the_seed_withholds_the_group(
    ck_config, sessionmaker, caplog
):
    with caplog.at_level(logging.ERROR):
        manager = _start(ck_config, sessionmaker)
    assert manager.class_keys.withheld
    assert "which no backup holds yet" in caplog.text
    assert not any(
        "key_enable" in line or "key_mgr_seed" in line for line in _conf(ck_config)
    )
    assert manager.class_keys.recorded() == set()
    # The next start of the manager, after a backup, renders it.
    manager = _start(ck_config, sessionmaker)
    assert not manager.class_keys.withheld
    lines = _conf(ck_config)
    assert "vs_key_enable 2" in lines
    assert f"key_mgr_seed {manager.keys()['key_mgr_seed']}" in lines
    assert manager.class_keys.recorded() == {"vs", "cc", "n2n"}


def test_withheld_for_every_opensm_this_manager_starts(ck_config, sessionmaker):
    manager = _start(ck_config, sessionmaker)
    manager._start_opensm()
    assert not any("key_enable" in line for line in _conf(ck_config))


def test_dropping_the_group_while_ports_may_hold_keys_is_refused(
    ck_config, sessionmaker
):
    _start(ck_config, sessionmaker)
    _start(ck_config, sessionmaker)
    ck_config.hardening = "all"
    driver = FakeOpenSM()
    with pytest.raises(config_mod.ConfigError, match="without the class_keys group"):
        _start(ck_config, sessionmaker, driver)
    assert driver.starts == 0
    # Nor by enable 0
    ck_config.hardening = "all, class_keys"
    ck_config.opensm_conf = {"vs_key_enable": "0"}
    with pytest.raises(config_mod.ConfigError, match="enable 0"):
        _start(ck_config, sessionmaker, driver)
    assert driver.starts == 0


def test_disabling_first_then_dropping_the_group(ck_config, sessionmaker):
    _start(ck_config, sessionmaker)
    _start(ck_config, sessionmaker)
    # Step 1: disable every class, keeping the group and so the seed
    ck_config.opensm_conf = {f"{name}_key_enable": "1" for name in ("vs", "cc", "n2n")}
    manager = _start(ck_config, sessionmaker)
    lines = _conf(ck_config)
    assert "vs_key_enable 1" in lines and any(
        line.startswith("key_mgr_seed ") for line in lines
    )
    assert manager.class_keys.recorded() == set()
    # Step 3: drop the group
    ck_config.opensm_conf = {}
    ck_config.hardening = "all"
    manager = _start(ck_config, sessionmaker)
    assert not any("key_enable" in line for line in _conf(ck_config))


def test_disabling_one_class_keeps_the_guard_on_the_others(ck_config, sessionmaker):
    _start(ck_config, sessionmaker)
    _start(ck_config, sessionmaker)
    ck_config.opensm_conf = {"cc_key_enable": "1"}
    manager = _start(ck_config, sessionmaker)
    assert manager.class_keys.recorded() == {"vs", "n2n"}
    ck_config.opensm_conf = {}
    ck_config.hardening = "all"
    with pytest.raises(config_mod.ConfigError, match="n2n, vs"):
        _start(ck_config, sessionmaker)


def test_ha_mode_neither_guards_nor_records(ck_config, sessionmaker):
    ck_config.ha = True
    manager = manager_mod.Manager(ck_config, sessionmaker, FakeOpenSM())
    with sessionmaker() as session:
        session.add(db.State(name=class_keys.RECORD, value=7))
        session.commit()
    manager.class_keys.refuse_start(seed_kept=True)
    assert manager.class_keys.recorded() == set()


REJECTED = (
    "Nov 13 10:00:00 000000 [1] 0x01 -> osm_vs_key_mgr: ERR 9828: Received invalid "
    "status from port GUID 0x2c90300001234, status:0xc, attr_id:0x11"
)
CC_REJECTED = (
    "x: ERR C10A: Received invalid status from port GUID 0x2c903000e0b72, "
    "status:0xc, attr_id:0x11"
)
# The N2N manager's, in the same format as 9828
N2N_REJECTED = (
    "x: ERR 9B28: Received invalid status from port GUID 0x2c90300001234, "
    "status:0xc, attr_id:0x11"
)
FAILED = (
    "x: ERR 9803: failed to update port guid 0x0002c90300001234 vskeyinfo",
    "x: ERR C124: failed to update port (guid 0x0002c90300001234) by CC keyinfo",
    "x: ERR C125: failed to update port (guid 0x0002c90300001234) by CC keyinfo",
    "x: ERR 9823: VS keys configuration has failed",
)


def test_the_driver_reads_the_key_managers_lines():
    findings = LogFindings()
    for line in (
        REJECTED,
        CC_REJECTED,
        N2N_REJECTED,
        *FAILED,
        "x: ERR C10A: Unexpected MAD",
    ):
        docker_driver._note_finding(findings, line)
    assert findings.key_rejections == [
        ("9828", PORT, 0xC, 0x11),
        ("C10A", OWN, 0xC, 0x11),
        ("9B28", PORT, 0xC, 0x11),
    ]
    assert findings.key_errors == [
        ("9803", PORT),
        ("C124", PORT),
        ("C125", PORT),
        ("9823", None),
    ]
    assert findings.ignored_sms == []


def test_rejections_are_logged_as_they_grow_and_errors_once(
    ck_config, sessionmaker, caplog
):
    driver = FakeOpenSM()
    _start(ck_config, sessionmaker, driver)
    manager = _start(ck_config, sessionmaker, driver)
    assert manager.class_keys.watching()
    driver.findings = LogFindings(
        key_rejections=[("9828", PORT, 0xC, 0x11), ("9828", PORT, 0xC, 0x11)],
        key_errors=[("9803", PORT), ("9803", OWN)],
    )
    with caplog.at_level(logging.WARNING):
        # They don't fail the check.
        assert manager.check_fabric()
    assert caplog.text.count("reports 1 ports") == 1
    assert "VS key manager" in caplog.text
    assert caplog.text.count("ERR 9803") == 1
    caplog.clear()
    driver.findings = LogFindings(
        key_rejections=[("9828", PORT, 0xC, 0x11), ("9828", OWN, 0xC, 0x11)],
        key_errors=[("9803", PORT)],
    )
    with caplog.at_level(logging.WARNING):
        assert manager.check_fabric()
    assert "reports 2 ports" in caplog.text
    assert "ERR 9803" not in caplog.text
    # A new OpenSM's reports are logged afresh.
    manager._start_opensm()
    caplog.clear()
    driver.findings = LogFindings(key_errors=[("9803", PORT)])
    with caplog.at_level(logging.WARNING):
        manager.check_fabric()
    assert "ERR 9803" in caplog.text


def test_without_class_keys_the_log_isnt_read(config):
    config.driver = "managed"
    driver = FakeOpenSM()
    driver.log_findings = None
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), driver
    )
    manager.check_fabric()
