# The world fixture, imported, is redefined as each test's parameter.
# ruff: noqa: F811
"""Zero-touch operation (specs/zero-touch.md): the expected switches kept by
the manager's checks, and the per-port M_Key turn-on armed and applied by
the manager, against the simulated fabric of tests/test_port_keeper.py"""

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, port_keeper, port_keys
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric
from alternative_fabric_manager.ha import lease as lease_mod
from tests.fabric_sim import BASE, OWN, S1, S2, M
from tests.test_port_keeper import (  # noqa: F401 - the fixture
    AFTER_CHECK,
    HV,
    T3,
    T4,
    TN,
    _port_job,
    confirm,
    derived,
    expect,
    expected,
    tenants_given_m_key,
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


# The per-port M_Key turn-on, armed and applied by the manager


@pytest.fixture
def auto(world, monkeypatch):
    """The world with the manager's own arming and applying, and its
    switches expected"""
    monkeypatch.setattr(port_keeper.PortKeeper, "after_check", AFTER_CHECK)
    world.manager.start()
    expect(world, S1, S2)
    return world


def arming(world):
    with world.sessionmaker() as session:
        return port_keys.load_text(session, port_keys.ARMING)


def set_time(world, name, when):
    with world.sessionmaker() as session:
        port_keys.save_time(session, name, when)
        session.commit()


def test_a_passing_check_arms_and_applies_the_turn_on(auto):
    world = auto
    history = len(world.sim.history)
    assert world.check(), world.applied().check_detail
    state = world.state()
    assert state["target"] and not state["running"]
    # No open job: OpenSM is stopped, and the applier's next iteration
    # starts it on the target.
    assert not world.driver.is_running
    world.manager._check_opensm()
    assert world.driver.per_port() and world.state()["confirmed"]
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())
    assert world.check(), world.applied().check_detail
    assert not tenants_given_m_key(world.sim, history, {TN, HV, T3, T4})
    assert arming(world) == ""


def test_arming_waits_for_every_port_on_m_key_and_says_why_once(auto, caplog):
    world = auto
    # A Neutron port's, which the lost-port rule doesn't fail the check for
    _port_job(world)
    world.sim.ports[TN].key = derived(TN)
    for _ in range(3):
        world.check()
    assert world.state()["target"] is False
    blocked = [
        r for r in caplog.records if "Per-port M_Keys aren't armed" in r.getMessage()
    ]
    assert len(blocked) == 1 and f"0x{TN:016x}" in blocked[0].getMessage()
    assert f"0x{TN:016x}" in arming(world)
    assert f"arming blocked by: {arming(world)}" in world.keeper().states()
    # Fixed: armed at the next passing check
    world.sim.ports[TN].key = M
    world.check()
    assert world.state()["target"] is True and arming(world) == ""


def test_arming_needs_the_base_stored_by_an_earlier_start(auto):
    world = auto
    with world.sessionmaker() as session:
        session.get(db.State, manager_mod.BASE_KEPT).value = 0
        session.commit()
    world.check()
    assert world.state()["target"] is False
    assert "backing up the database" in arming(world)


def test_arming_needs_a_complete_full_verify_since_opensm_started(auto):
    world = auto
    keeper = world.keeper()

    def disarm():
        with world.sessionmaker() as session:
            port_keys.save_target(session, False)
            session.commit()

    # An open job keeps OpenSM running through the checks.
    _port_job(world)
    world.check()
    disarm()
    keeper._last_full = None
    keeper._arm_automatically()
    assert "no full verify" in arming(world)
    world.check()
    disarm()
    keeper._last_full[0].complete = False
    keeper._arm_automatically()
    assert "ran out of time" in arming(world)
    assert world.state()["target"] is False
    # A start forgets the last one.
    keeper.start(lambda prepare: None)
    assert keeper._last_full is None


def test_nothing_arms_without_the_group(auto):
    world = auto
    world.config.hardening = "all"
    world.check()
    assert world.state()["target"] is False


def _leading(world, term=3):
    world.config.ha = True
    world.manager.term = term
    world.manager._claim_fence()


def _epoch(world, hashes):
    with world.sessionmaker() as session:
        lease_mod._store_epoch(session, 1, hashes)
        session.commit()


@pytest.mark.parametrize("hashes", [None, ["other"], ["mine", "other"]])
def test_in_ha_mode_arming_needs_an_epoch_of_exactly_this_hash(auto, hashes):
    world = auto
    _leading(world)
    if hashes is not None:
        _epoch(
            world,
            [world.manager.config_hash() if h == "mine" else "0" * 64 for h in hashes],
        )
    world.check()
    assert world.state()["target"] is False
    assert "configuration epoch" in arming(world)


def test_in_ha_mode_the_leader_steps_down_to_apply(auto):
    world = auto
    _leading(world)
    _epoch(world, [world.manager.config_hash()])
    world.check()
    assert world.state()["target"] is True
    # Not stopped by the manager: the coordinator steps down.
    assert world.driver.is_running
    assert "target is on" in world.manager.apply_requested


def test_an_open_job_holds_the_apply_until_a_day_has_passed(auto, caplog):
    world = auto
    # A job not yet swept stays open through the checks.
    _port_job(world)
    world.check()
    assert world.state()["target"] is True
    assert world.driver.is_running
    world.check()
    assert world.driver.is_running
    set_time(world, port_keys.TARGET_CHANGED, time_ago(86401))
    world.check()
    assert not world.driver.is_running
    assert any("A day has passed" in r.getMessage() for r in caplog.records)


def time_ago(seconds):
    import time

    return time.time() - seconds


def test_the_apply_is_tried_at_most_once_an_hour(auto):
    world = auto
    world.check()
    assert not world.driver.is_running
    # Its start's verify fails, so OpenSM starts on the previous target.
    world.driver.probe_error = manager_mod.ReloadError("the prober failed")
    world.manager._check_opensm()
    world.driver.probe_error = None
    assert world.driver.is_running and not world.driver.per_port()
    assert world.state()["target"] and not world.state()["running"]
    world.check()
    assert world.driver.is_running
    set_time(world, port_keys.LAST_APPLY, time_ago(3601))
    world.check()
    assert not world.driver.is_running
    world.manager._check_opensm()
    assert world.driver.per_port() and world.state()["confirmed"]


def test_a_part_way_turn_on_is_retried_under_the_cap(auto, monkeypatch):
    world = auto
    asked = []
    monkeypatch.setattr(world.manager, "request_apply", asked.append)
    with world.sessionmaker() as session:
        port_keys.save_target(session, True)
        port_keys.save_running(session, True)
        port_keys.save_confirmed(session, False)
        session.commit()
    world.keeper()._apply_if_due()
    assert len(asked) == 1 and "success rule" in asked[0]
    world.keeper()._apply_if_due()
    assert len(asked) == 1


def _failed_turn_on(world):
    """A turn-on whose start moved nothing, set back and recorded"""
    world.check()
    assert world.state()["target"] is True
    world.driver.stall = True
    world.manager._check_opensm()
    world.driver.stall = False
    state = world.state()
    assert not state["target"] and world.driver.is_running


def test_a_failed_turn_on_is_recorded_and_never_rearmed(auto, caplog):
    world = auto
    _failed_turn_on(world)
    with world.sessionmaker() as session:
        record = port_keys.load_text(session, port_keys.FAILED_TURN_ON)
    config_hash = world.manager.config_hash()
    assert record == f"{port_keys.fingerprint(BASE)}:{config_hash}"
    for _ in range(3):
        world.check()
    assert world.state()["target"] is False
    assert arming(world) == port_keeper.FAILED_REASON
    lines = world.keeper().states()
    assert "failed turn-on recorded: yes" in lines
    # The hash is never shown, nor logged.
    shown = "\n".join(lines) + "\n".join(r.getMessage() for r in caplog.records)
    assert config_hash not in shown
    # Logged at every start, with what to check
    world.restart()
    assert any(
        "turn-on failed under this configuration" in r.getMessage()
        for r in caplog.records
    )
    assert world.state()["target"] is False


def test_a_configuration_change_clears_a_failed_turn_on(auto):
    world = auto
    _failed_turn_on(world)
    world.config.key_lease_period = 60
    world.restart()
    world.check()
    assert world.state()["target"] is True
    assert "failed turn-on recorded: no" in world.keeper().states()


def test_confirm_fabric_clears_a_failed_turn_on_and_arms(auto):
    world = auto
    _failed_turn_on(world)
    world.manager.reconcile()
    confirmation = confirm(world)
    assert confirmation.cleared and confirmation.armed
    assert "failed turn-on recorded: no" in world.keeper().states()


def test_a_failure_record_holds_no_key():
    record = port_keys.failure_record(BASE, "a" * 64)
    assert f"{BASE:x}" not in record
    with pytest.raises(ValueError):
        port_keys.failure_record(BASE, "")
