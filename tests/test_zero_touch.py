# The world fixture, imported, is redefined as each test's parameter.
# ruff: noqa: F811
"""Zero-touch operation (specs/zero-touch.md): the expected switches kept by
the manager's checks, and the per-port M_Key turn-on armed and applied by
the manager, against the simulated fabric of tests/test_port_keeper.py"""

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, port_keys
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric
from tests.fabric_sim import OWN, S1, S2, M
from tests.test_port_keeper import (  # noqa: F401 - the fixture
    T3,
    expect,
    expected,
    world,
)

CHECKS = 3


@pytest.fixture
def stable(world):  # noqa: F811
    world.config.confirm_checks = CHECKS
    return world


def checks(world, count=CHECKS):
    for _ in range(count):
        world.check()


def nodes(world):
    with world.sessionmaker() as session:
        return {
            int(row.guid, 16): row.node_guid and int(row.node_guid, 16)
            for row in session.scalars(sa.select(db.ExpectedSwitch))
        }


def source(world):
    with world.sessionmaker() as session:
        row = session.get(db.State, manager_mod.EXPECTED_SOURCE)
        return row and row.text


# The expected switches


def test_confirm_checks_must_be_positive(config):
    config.confirm_checks = 0
    with pytest.raises(config_mod.ConfigError, match="confirm_checks"):
        config.validate()


def test_a_new_deployment_records_its_switches_once_stable(stable):
    world = stable
    world.manager.start()
    checks(world, CHECKS - 1)
    assert expected(world) == set()
    assert world.applied().check_detail == (
        f"No expected switches yet: recording them once the fabric has been "
        f"stable for {CHECKS} checks"
    )
    world.check()
    # The prober's port 0 GUIDs, with their node GUIDs beside them
    assert nodes(world) == {S1: S1 + 0x1000, S2: S2 + 0x1000}
    assert source(world) == manager_mod.PROBER
    assert world.applied().check_ok, world.applied().check_detail


def test_without_rows_ibswitches_records_node_guids(stable):
    world = stable
    world.config.hardening = "all"
    world.manager.start()
    checks(world)
    assert nodes(world) == {S1 + 0x1000: S1 + 0x1000, S2 + 0x1000: S2 + 0x1000}
    assert source(world) == manager_mod.IBSWITCHES
    assert world.applied().check_ok


def test_a_switch_must_be_seen_in_every_check_of_the_row(stable):
    world = stable
    world.manager.start()
    checks(world, CHECKS - 1)
    # A check that can't query the fabric starts the count again.
    world.driver.probe_error = manager_mod.ReloadError("the prober failed")
    world.check()
    world.driver.probe_error = None
    checks(world, CHECKS - 1)
    assert expected(world) == set()
    world.check()
    assert expected(world) == {S1, S2}


@pytest.mark.parametrize("sms", [[(OWN, 2)], [(OWN, 3), (0x77, 3)]])
def test_nothing_is_recorded_while_contested_or_not_master(stable, monkeypatch, sms):
    world = stable
    world.manager.start()
    real = world.driver.fabric

    def fabric(**kwargs):
        found = real(**kwargs)
        return Fabric(found.switches, sms, found.own_guids)

    monkeypatch.setattr(world.driver, "fabric", fabric)
    checks(world, CHECKS * 2)
    assert expected(world) == set()
    monkeypatch.undo()
    checks(world, CHECKS - 1)
    assert expected(world) == set()
    world.check()
    assert expected(world) == {S1, S2}


def test_a_new_switch_is_added_once_stable(stable):
    world = stable
    world.manager.start()
    checks(world)
    world.sim.add(0x53, switch=True, ports=2, key=M)
    world.sim.cable(S2, 3, 0x53, 1)
    world.driver.sweep()
    checks(world, CHECKS - 1)
    assert expected(world) == {S1, S2}
    world.check()
    assert expected(world) == {S1, S2, 0x53}


def test_a_change_of_source_translates_by_node_guid(stable):
    world = stable
    world.config.hardening = "all"
    world.manager.start()
    checks(world)
    # One that's missing already stays as it is, and keeps failing the check.
    with world.sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid="0000000000009999", node_guid=None))
        session.commit()
    assert expected(world) == {S1 + 0x1000, S2 + 0x1000, 0x9999}
    # Per-port M_Keys named: the prober's GUIDs from now on
    world.config.hardening = "all, m_key_per_port"
    world.restart()
    world.check()
    assert nodes(world) == {S1: S1 + 0x1000, S2: S2 + 0x1000, 0x9999: None}
    assert source(world) == manager_mod.PROBER
    assert "0x0000000000009999" in world.applied().check_detail
    assert f"0x{S1:016x}" not in world.applied().check_detail
    # And back, as after --forget-port-keys
    expect(world, S1, S2)
    with world.sessionmaker() as session:
        for row in session.scalars(sa.select(db.ExpectedSwitch)):
            row.node_guid = port_keys.guid_text(int(row.guid, 16) + 0x1000)
        session.commit()
    translated = manager_mod.Manager._translate(
        {S1: S1 + 0x1000, S2: S2 + 0x1000},
        manager_mod.PROBER,
        manager_mod.IBSWITCHES,
        {S1 + 0x1000: S1 + 0x1000, S2 + 0x1000: S2 + 0x1000},
    )
    assert translated == {S1 + 0x1000: S1 + 0x1000, S2 + 0x1000: S2 + 0x1000}


def _spine(world):
    """A switch 0x53 linked to S1 and S2 only, and expected"""
    world.sim.add(0x53, switch=True, ports=4, key=M)
    world.sim.cable(S1, 6, 0x53, 1)
    world.sim.cable(S2, 3, 0x53, 2)
    world.driver.sweep()
    world.check()
    world.check()
    expect(world, S1, S2, 0x53)


def test_a_missing_switch_with_every_link_down_is_dropped(stable):
    world = stable
    world.manager.start()
    world.manager.reconcile()
    _spine(world)
    assert world.check(), world.applied().check_detail
    # Removed: the switch ports at its other ends show their links down.
    world.sim.down |= {(S1, 6), (S2, 3), (0x53, 1), (0x53, 2)}
    assert world.check(), world.applied().check_detail
    assert expected(world) == {S1, S2}


def test_a_missing_switch_with_a_link_up_stays(stable):
    world = stable
    world.manager.start()
    world.manager.reconcile()
    _spine(world)
    # Its management agent hung, its links up: it may still forward.
    world.sim.ports[0x53].key = 0x3333333333333333
    assert not world.check()
    assert "Missing switches: 0x0000000000000053" in world.applied().check_detail
    assert expected(world) == {S1, S2, 0x53}
    # One link down isn't every link down.
    world.sim.down |= {(S1, 6), (0x53, 1)}
    assert not world.check()
    assert expected(world) == {S1, S2, 0x53}


def test_a_leaf_switch_is_never_dropped(stable):
    # T3 is reached only through S2, so nothing shows S2's link to it down.
    world = stable
    world.manager.start()
    world.manager.reconcile()
    expect(world, S1, S2)
    world.sim.down |= {(S1, 2), (S2, 1)}
    for _ in range(3):
        assert not world.check()
    assert expected(world) == {S1, S2}
    assert T3 in world.rows()


def test_ibswitches_never_drops_a_switch(stable):
    world = stable
    world.config.hardening = "all"
    world.manager.start()
    checks(world)
    world.sim.down |= {(S1, 2), (S2, 1)}
    checks(world)
    assert expected(world) == {S1 + 0x1000, S2 + 0x1000}


def test_in_ha_mode_the_leader_writes_behind_the_fence(stable):
    world = stable
    world.config.ha = True
    world.manager.term = 4
    world.manager.start()
    checks(world)
    assert expected(world) == {S1, S2}
    with world.sessionmaker() as session:
        assert session.get(db.State, manager_mod.LEADER_FENCE).value == 4
        # A later term's leader raises the fence: this one writes nothing.
        session.get(db.State, manager_mod.LEADER_FENCE).value = 5
        session.execute(sa.delete(db.ExpectedSwitch))
        session.commit()
    checks(world)
    assert expected(world) == set()


def test_confirm_fabric_records_the_source_and_node_guids(world):  # noqa: F811
    world.manager.start()
    world.manager.reconcile()
    world.manager.confirm_fabric()
    assert nodes(world) == {S1: S1 + 0x1000, S2: S2 + 0x1000}
    assert source(world) == manager_mod.PROBER
