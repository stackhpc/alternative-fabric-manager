"""The secured profile's groups in HA mode (specs/secured-profile.md, HA
mode): allowed_sm's list from the node rows, the managed-node SM rule, and
the coordinator recording its node's ports"""

import json

import pytest

from alternative_fabric_manager import db
from alternative_fabric_manager.drivers import Fabric, ReloadError
from alternative_fabric_manager.ha import coordinator as coordinator_mod
from tests.test_allowed_sm import (
    OWN_A,
    OWN_B,
    SWITCH,
    TENANT,
    _conf,
    sm_config,  # noqa: F401 - fixtures
    sm_driver,  # noqa: F401
    sm_manager,  # noqa: F401
)
from tests.test_epochs import clock, lease, make  # noqa: F401 - fixtures

OTHER = 0x0002C90300009999


def _node(manager, name, guids):
    with manager.sessionmaker() as session:
        session.merge(
            db.Node(
                name=name, port_guids=json.dumps([f"{g:016x}" for g in sorted(guids)])
            )
        )
        session.commit()


@pytest.fixture
def ha(sm_config, sm_manager):  # noqa: F811
    sm_config.ha = True
    sm_config.node = "a"
    sm_manager.term = 1
    return sm_manager


def test_the_list_is_every_node_rows_ports(ha, sm_config):  # noqa: F811
    _node(ha, "a", {OWN_A, OWN_B})
    _node(ha, "b", {OTHER})
    ha._start_opensm()
    assert _conf(sm_config)["allowed_sm_guids"] == ",".join(
        f"0x{g:016x}" for g in sorted({OWN_A, OWN_B, OTHER})
    )


def test_a_node_row_without_ports_refuses_the_start(ha, sm_driver):  # noqa: F811
    _node(ha, "b", {OTHER})
    with pytest.raises(ReloadError, match="this node's row has no port GUIDs"):
        ha._start_opensm()
    assert sm_driver.starts == 0


def test_a_target_without_ports_refuses_the_start(ha, sm_driver):  # noqa: F811
    _node(ha, "a", {OWN_A, OWN_B})
    _node(ha, "old", set())
    with ha.sessionmaker() as session:
        session.add(db.TargetEntry(node="old", instance_uuid="u", term=0))
        session.commit()
    with pytest.raises(ReloadError, match="node old may run OpenSM"):
        ha._start_opensm()


def test_an_override_must_hold_every_node_rows_ports(ha, sm_config):  # noqa: F811
    _node(ha, "a", {OWN_A, OWN_B})
    _node(ha, "b", {OTHER})
    sm_config.opensm_conf["allowed_sm_guids"] = f"0x{OWN_A:x},0x{OWN_B:x}"
    with pytest.raises(ReloadError, match="leaves out node ports"):
        ha._start_opensm()
    sm_config.opensm_conf["allowed_sm_guids"] += f",0x{OTHER:x},0x{TENANT:x}"
    ha._start_opensm()
    assert f"0x{TENANT:016x}" in _conf(sm_config)["allowed_sm_guids"]


def test_another_managed_nodes_sm_fails_the_check(ha, sm_driver):  # noqa: F811
    _node(ha, "a", {OWN_A, OWN_B})
    _node(ha, "b", {OTHER})
    fabric = Fabric({SWITCH}, [(OWN_A, 3), (OTHER, 1)], {OWN_A, OWN_B})
    ok, detail = ha._judge(fabric)
    assert not ok
    assert "node b" in detail
    # An unknown SM, not master, is only logged.
    fabric = Fabric({SWITCH}, [(OWN_A, 3), (TENANT, 1)], {OWN_A, OWN_B})
    assert ha._judge(fabric)[0]


class PortsRunner:
    def __init__(self, ports):
        self.ports = ports
        self.calls = 0

    def node_ports(self):
        self.calls += 1
        if isinstance(self.ports, Exception):
            raise self.ports
        return self.ports


def test_the_coordinator_records_its_ports_once_per_instance(lease, clock):  # noqa: F811
    a = make(lease, "a", "")
    a._rejoin()
    a.runner.node_ports = PortsRunner(ReloadError("no image")).node_ports
    a._record_ports()
    assert lease.node_ports()["a"] == frozenset()
    runner = PortsRunner({OWN_A})
    a.runner.node_ports = runner.node_ports
    a._record_ports()
    a._record_ports()
    assert lease.node_ports()["a"] == {OWN_A}
    assert runner.calls == 1
    # A probe that finds nothing changes nothing.
    a.uuid = "new"
    lease.register("a", "new", "", [])
    a.runner.node_ports = PortsRunner(set()).node_ports
    a._record_ports()
    assert lease.node_ports()["a"] == {OWN_A}


def test_without_port_guids_a_node_isnt_a_candidate(lease, clock, caplog):  # noqa: F811
    a = make(lease, "a", "", hardening="all, allowed_sm")
    a._rejoin()
    a._watch()
    clock.now += 100
    a._watch()
    assert a.role == coordinator_mod.Role.CANDIDATE
    assert "no port GUIDs yet" in caplog.text
    lease.record_ports("a", a.uuid, {OWN_A})
    a._watch()
    assert a.role in (coordinator_mod.Role.TAKING_OVER, coordinator_mod.Role.LEADER)
