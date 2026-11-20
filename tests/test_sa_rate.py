"""The SA request rate limit (specs/secured-profile.md, SA request rate)"""

import logging

import pytest

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, opensm_conf
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric, LogFindings
from alternative_fabric_manager.drivers import docker as docker_driver
from tests import conftest

SWITCH = 0xEC0D9A0300B12560
OWN = 0x0002C903000E0B72
GUEST = 0x12345600007821B6
LIMITED = (
    "Nov 17 10:00:00 000000 [1] 0x01 -> osm_sa_mad_ctrl: ERR 4C11: Number of sent "
    "SA queries 1204 for GUID 0x12345600007821b6 Port 0x1 is bigger than "
    "sa_rate_threshold: 1000"
)


def test_sa_rate_is_experimental():
    assert "sa_rate" in config_mod.HARDENING_GROUPS
    assert "sa_rate" not in config_mod.ALL_HARDENING_GROUPS
    assert "sa_rate" in config_mod.parse_hardening("all, sa_rate")


def test_ha_allows_sa_rate():
    config = config_mod.Config(
        token="x",
        ha=True,
        connection="mysql+pymysql://u:p@h/afm",
        job_timeout=400,
        hardening="all, sa_rate",
    )
    assert not any("SA request rate" in p for p in config_mod._ha_problems(config))


def test_the_group_renders_the_threshold():
    content = opensm_conf.render({}, hardening={"sa_rate"})
    assert "sa_rate_threshold 1000" in content.splitlines()
    content = opensm_conf.render({"sa_rate_threshold": "5000"}, hardening={"sa_rate"})
    assert "sa_rate_threshold 5000" in content.splitlines()
    assert "sa_rate_threshold 1000" not in content


def test_the_driver_reads_4c11():
    findings = LogFindings()
    docker_driver._note_finding(findings, LIMITED)
    assert findings.sa_limited == [(GUEST, 1, 1204, 1000)]


class FakeOpenSM(conftest.FakeReloader):
    def __init__(self):
        super().__init__()
        self.findings = LogFindings()
        self.fabric_result = Fabric({SWITCH}, [(OWN, 3)], {OWN})

    def log_findings(self):
        findings, self.findings = self.findings, LogFindings()
        return findings


@pytest.fixture
def sa_manager(config):
    config.driver = "managed"
    config.hardening = "all, sa_rate"
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), FakeOpenSM()
    )
    with manager.sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid=f"{SWITCH:016x}"))
        session.commit()
    return manager


def test_limited_clients_are_logged_once_per_opensm(sa_manager, caplog):
    driver = sa_manager.driver
    sa_manager._start_opensm()
    driver.findings = LogFindings(
        sa_limited=[(GUEST, 1, 1204, 1000), (GUEST, 1, 1500, 1000)]
    )
    with caplog.at_level(logging.WARNING):
        # It never fails the check.
        assert sa_manager.check_fabric()
    assert caplog.text.count("0x12345600007821b6") == 1
    assert "ERR 4C11" in caplog.text
    caplog.clear()
    driver.findings = LogFindings(sa_limited=[(GUEST, 1, 1204, 1000)])
    with caplog.at_level(logging.WARNING):
        sa_manager.check_fabric()
    assert "4C11" not in caplog.text
    sa_manager._start_opensm()
    driver.findings = LogFindings(sa_limited=[(GUEST, 1, 1204, 1000)])
    with caplog.at_level(logging.WARNING):
        sa_manager.check_fabric()
    assert "4C11" in caplog.text


@pytest.mark.parametrize(
    "hardening, opensm_conf, watching",
    [
        ("all, sa_rate", {}, True),
        ("all", {}, False),
        ("all", {"sa_rate_threshold": "100"}, True),
        ("all, sa_rate", {"sa_rate_threshold": "0"}, False),
    ],
)
def test_watching(sa_manager, hardening, opensm_conf, watching):
    sa_manager.config.hardening = hardening
    sa_manager.config.opensm_conf = opensm_conf
    assert sa_manager.sa_rate.watching() is watching
