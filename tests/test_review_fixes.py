"""Regression tests for the review of the secured profile in HA mode: the
start request's deadline and stop request, the leader fence claimed before a
start reads, blocked candidates' reach, and [ha] settings moving with the
epoch"""

import json
import os

import pytest

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import NoOpenSM, ReloadError
from alternative_fabric_manager.ha import coordinator as coordinator_mod
from alternative_fabric_manager.ha import watchdog as wd
from tests.test_coordinator import TIMINGS
from tests.test_epochs import clock, lease, make  # noqa: F401 - fixtures
from tests.test_ha_docker import driver  # noqa: F401 - fixture

A, B = "a" * 64, "b" * 64


def _request(config):
    with open(os.path.join(config.ha_state_dir, wd.START)) as f:
        return json.load(f)


def test_the_start_requests_deadline_is_worked_out_when_its_written(
    driver,  # noqa: F811
    config,
):
    import contextlib

    @contextlib.contextmanager
    def gate():
        yield 1234.5

    driver.start_gate = gate
    # prepare's deadline, long past by the time a verify has run
    driver.prepare("u", 3, 1.0)
    driver.start()
    assert _request(config)["deadline"] == 1234.5


def test_a_refused_gate_starts_nothing(driver, config):  # noqa: F811
    import contextlib

    @contextlib.contextmanager
    def gate():
        raise ReloadError("This instance no longer leads")
        yield  # pragma: no cover

    driver.start_gate = gate
    driver.prepare("u", 3, 1e12)
    with pytest.raises(ReloadError, match="no longer leads"):
        driver.start()
    assert not os.path.exists(os.path.join(config.ha_state_dir, wd.START))


def test_a_start_after_a_stop_in_the_same_takeover_can_start(
    driver,  # noqa: F811
    config,
):
    # A turn-on that fails stops OpenSM and starts it again, with no new
    # runner.prepare in between.
    driver.prepare("u", 3, 1e12)
    driver.start()
    driver.stop()
    assert os.path.exists(os.path.join(config.ha_state_dir, wd.STOP))
    driver.start()
    assert not os.path.exists(os.path.join(config.ha_state_dir, wd.STOP))
    watchdog_lease = {
        "instance": "u",
        "term": 3,
        "deadline": 1e12,
        "held": False,
        "boot_id": wd.boot_id(),
    }
    request = _request(config)
    assert (request["instance"], request["term"]) == ("u", 3)
    assert wd.lease_valid(watchdog_lease)


def test_the_coordinators_gate(lease, clock):  # noqa: F811
    a = make(lease, "a", "")
    with pytest.raises(ReloadError, match="no longer leads"), a.start_gate():
        pass
    a._become_leader(1)
    a._prepare_start()
    with a.start_gate() as deadline:
        assert deadline == a.t0 + TIMINGS["self_stop_timeout"]
    clock.now += TIMINGS["self_stop_timeout"]
    with pytest.raises(ReloadError, match="too old"), a.start_gate():
        pass
    clock.now -= TIMINGS["self_stop_timeout"]
    a._revoke_grant()
    with pytest.raises(ReloadError, match="no longer leads"), a.start_gate():
        pass


def test_a_takeovers_start_claims_the_fence_before_reading(config, monkeypatch):
    config.ha = True
    sessionmaker = db.make_sessionmaker(config.database_url)
    manager = manager_mod.Manager(config, sessionmaker, NoOpenSM())
    manager.term = 7
    seen = []

    def refuse_start():
        with sessionmaker() as session:
            row = session.get(db.State, manager_mod.LEADER_FENCE)
            seen.append(row.value if row else None)

    monkeypatch.setattr(manager.port_keeper, "refuse_start", refuse_start)
    manager.start()
    manager.stop()
    assert seen == [7]


def test_a_blocked_candidate_heartbeats_no_reach(lease, clock):  # noqa: F811
    lease.allow_config([A])
    a = make(lease, "a", A)
    a._rejoin()
    b = make(lease, "b", B)
    b._rejoin()
    b._watch()
    b._tick()
    reach = {row[0]: row[3] for row in lease.nodes()}
    assert reach["b"] == 0
    a._tick()
    assert {row[0]: row[3] for row in lease.nodes()}["a"] == 1


def test_ha_settings_move_with_the_epoch(lease, clock):  # noqa: F811
    old = make(lease, "a", A, job_timeout=300)
    old._rejoin()
    assert old._settings_ok
    # Every node is restarted with a longer job_timeout: their settings
    # differ from the stored ones, but they register, heartbeat, and move
    # the epoch, with the settings, once every live node agrees.
    old.shutdown()
    a = make(lease, "a", B, job_timeout=360)
    a._rejoin()
    assert not a._settings_ok
    b = make(lease, "b", B, job_timeout=360)
    b._rejoin()
    b._watch()
    assert lease.epoch()[1] == (B,)
    with lease.sessionmaker() as session:
        stored = json.loads(session.get(db.Secret, "ha_settings").value)
    assert stored["job_timeout"] == 360
    assert not b._blocked_now
    clock.now += 100
    b._watch()
    assert b.role in (coordinator_mod.Role.TAKING_OVER, coordinator_mod.Role.LEADER)


def test_the_hash_covers_the_ha_settings():
    one = config_mod.Config(token="t", job_timeout=300)
    two = config_mod.Config(token="t", job_timeout=360)
    assert config_mod.config_hash(one, "k") != config_mod.config_hash(two, "k")


def _settings(**changes):
    return config_mod.ha_settings(config_mod.Config(token="t", **changes))


def test_an_epoch_never_shortens_a_wait_while_a_target_may_run(lease):  # noqa: F811
    from alternative_fabric_manager.ha import lease as lease_mod

    assert lease.agree_settings(_settings(lease_timeout=30.0))
    state = lease.read()
    lease.acquire("a", "ua", state.term, state.counter, True)
    # A shorter lease_timeout while a's entry is in the target set: a
    # successor could leave a's OpenSM unconfirmed too soon.
    with pytest.raises(lease_mod.UnsafeSettings, match="lease_timeout"):
        lease.allow_config([A], settings=_settings(lease_timeout=20.0))
    assert lease.epoch() is None
    # The safe direction moves.
    assert lease.allow_config([A], settings=_settings(lease_timeout=40.0)) == 1
    # Once every target has left, any direction does.
    assert lease.release("a", "ua", 1)
    assert lease.allow_config([B], settings=_settings(lease_timeout=20.0)) == 2


def test_unsafe_moves_are_named():
    stored = json.loads(_settings())
    shorter = json.loads(
        _settings(watchdog_timeout=10.0, self_stop_timeout=25.0, job_timeout=600)
    )
    assert config_mod.unsafe_setting_moves(stored, shorter) == [
        "self_stop_timeout",
        "watchdog_timeout",
    ]
