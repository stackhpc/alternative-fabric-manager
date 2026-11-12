"""The coordinator's decisions, driven by hand (see specs/ha.md)

These use the real lease driver on SQLite, and fakes for OpenSM's runner and
the applier, with a clock the tests move. The failover harness in
tests/functional runs the same code against Galera and processes.
"""

import threading

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db
from alternative_fabric_manager.ha import coordinator as coordinator_mod
from alternative_fabric_manager.ha import lease as lease_mod

TIMINGS = dict(
    lease_timeout=8.0,
    self_stop_timeout=4.0,
    watchdog_timeout=4.0,
    ha_period=0.5,
    call_timeout=1.0,
    start_timeout=10.0,
    sweep_timeout=5.0,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Runner:
    def __init__(self, calls):
        self.calls = calls
        self.stopped = True
        self.leases = []

    def prepare(self, instance, term, deadline):
        self.calls.append(("prepare", term))

    def write_lease(self, instance, term, deadline):
        self.leases.append((term, deadline))

    def stop(self):
        self.calls.append("runner.stop")
        self.stopped = True

    def verify_stopped(self):
        return self.stopped

    def ask(self, node):
        self.calls.append(("ask", node))

    def reach(self):
        return self.reach_value

    reach_value = 1


class Manager:
    def __init__(self, calls):
        self.calls = calls
        self.term = 0
        self.unanswered_checks = 0
        self.failed_applies = 0
        self.missing_checks = 0
        self.missing_checks = 0
        self.on_opensm_stopped = None
        self.starts = 0

    def start(self):
        self.calls.append("manager.start")
        self.starts += 1

    def halt(self):
        self.calls.append("manager.halt")

    def iteration_age(self):
        return 0.0


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(coordinator_mod, "_now", clock)
    monkeypatch.setattr(coordinator_mod.time, "sleep", lambda _s: None)
    return clock


@pytest.fixture
def lease():
    sessionmaker = db.make_sessionmaker(":memory:")
    with sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid="0000000000000001"))
        session.commit()
    return lease_mod.GaleraLease(sessionmaker)


def make(lease, node, **overrides):
    config = config_mod.Config(token="t", node=node, **dict(TIMINGS, **overrides))
    calls = []
    coordinator = coordinator_mod.Coordinator(
        config, lease, Runner(calls), Manager(calls)
    )
    coordinator.calls = calls
    return coordinator


def lead(coordinator):
    """Rejoin, take the released lease, and finish the takeover"""
    coordinator._rejoin()
    coordinator._watch()
    coordinator._takeover.join(5)
    assert coordinator.role == coordinator_mod.Role.LEADER


def test_one_candidate_takes_a_released_lease_and_starts_opensm(lease, clock):
    a = make(lease, "a")
    lead(a)
    assert a.term == 1
    assert ("prepare", 1) in a.calls and "manager.start" in a.calls


def test_a_step_down_stops_opensm_before_waiting_for_the_applier(lease, clock):
    a = make(lease, "a")
    lead(a)
    a.calls.clear()
    a._step_down("test")
    assert a.calls.index("runner.stop") < a.calls.index("manager.halt")


def test_self_stop_comes_before_any_database_call(lease, clock, monkeypatch):
    a = make(lease, "a")
    lead(a)
    clock.now += TIMINGS["self_stop_timeout"] + 1

    def blocked(*_args):
        raise AssertionError("a database call before the self-stop")

    monkeypatch.setattr(lease, "heartbeat", blocked)
    a._tick()
    assert a.role == coordinator_mod.Role.STARTING
    assert "runner.stop" in a.calls


def _takeover(lease, clock, node, **overrides):
    """Take the lease over after it expired, without running the wait"""
    coordinator = make(lease, node, **overrides)
    coordinator._rejoin()
    state = lease.read()
    term = lease.acquire(
        node, coordinator.uuid, state.term, state.counter, state.released
    )
    coordinator._become_leader(term)
    return coordinator, term


def test_a_committed_freeze_holds_even_if_its_read_back_failed(lease, clock):
    a = make(lease, "a")
    lead(a)
    lease.request_freeze(a.term, "operator", 30)
    # The leader's acknowledgement committed, but it never saw that.
    assert lease.acknowledge_freeze(a.uuid, a.term)
    assert not a.held
    a._freeze_step()
    assert a.held
    assert a.runner.leases[-1] == (a.term, None)
    # Held: no self-stop, however long renewals fail
    clock.now += 100
    a._tick()
    assert a.role == coordinator_mod.Role.LEADER


def test_an_unfreeze_is_acknowledged_after_a_failed_attempt(lease, clock, monkeypatch):
    a = make(lease, "a")
    lead(a)
    lease.request_freeze(a.term, "operator", 30)
    a._freeze_step()
    assert a.held
    lease.request_unfreeze()
    real = lease.renew
    failures = [1]

    def renew(*args):
        if failures:
            failures.pop()
            raise lease_mod.NotPrimary("blip")
        return real(*args)

    monkeypatch.setattr(lease, "renew", renew)
    with pytest.raises(lease_mod.NotPrimary):
        a._freeze_step()
    a._freeze_step()
    assert lease.read().freeze_state == ""
    assert not a.frozen


def test_an_unknown_acquire_outcome_comes_back_as_a_new_instance(
    lease, clock, monkeypatch
):
    a = make(lease, "a")
    a._rejoin()
    old = a.uuid
    real = lease.acquire

    def acquire(*args):
        real(*args)
        raise lease_mod.NotPrimary("the commit's outcome is unknown")

    monkeypatch.setattr(lease, "acquire", acquire)
    rejoined = threading.Event()
    monkeypatch.setattr(a, "_rejoin", rejoined.set)
    a._watch()
    assert a.uuid != old
    assert "runner.stop" in a.calls
    assert rejoined.is_set()


def test_a_frozen_restart_starts_opensm_once(lease, clock):
    a = make(lease, "a")
    lead(a)
    lease.request_freeze(a.term, "operator", 30)
    a._freeze_step()
    starts = a.manager.starts
    release = threading.Event()
    a.manager.start = lambda: release.wait(5)
    a._opensm_gone = True
    a._restart_frozen()
    # The applier reports OpenSM gone again before it is back up.
    a._opensm_gone = True
    a._restart_frozen()
    release.set()
    assert a.calls.count(("prepare", a.term)) == 2  # the takeover's and one restart
    assert a.manager.starts == starts


def test_a_takeover_retries_database_errors(lease, clock, monkeypatch):
    a = make(lease, "a")
    lead(a)
    b, term_b = _takeover(lease, clock, "b")
    real = lease.targets
    failures = [1]

    def targets():
        if failures:
            failures.pop()
            raise lease_mod.NotPrimary("blip")
        return real()

    monkeypatch.setattr(lease, "targets", targets)
    monkeypatch.setattr(b._stop, "wait", lambda _s: False)
    clock.now += TIMINGS["watchdog_timeout"] + 1
    # The lease thread renews meanwhile.
    b.t0 = clock.now
    b._take_over(term_b, clock.now - TIMINGS["watchdog_timeout"] - 1)
    assert b.role == coordinator_mod.Role.LEADER
    assert lease.read().holder_uuid == b.uuid


def test_shutdown_marks_the_node_stopped_and_fails_health_first(lease, clock):
    a = make(lease, "a")
    lead(a)
    roles = []
    a.runner.stop = lambda: roles.append(a.role)
    a.shutdown()
    assert roles[0] == coordinator_mod.Role.STOPPING
    assert [stopped for name, *_rest, stopped in lease.nodes() if name == "a"] == [True]
    assert lease.read().released


def test_a_takeover_doesnt_start_opensm_on_a_stale_renewal(lease, clock, monkeypatch):
    a = make(lease, "a")
    lead(a)
    b, term_b = _takeover(lease, clock, "b")
    monkeypatch.setattr(b._stop, "wait", lambda _s: False)
    clock.now += TIMINGS["self_stop_timeout"] + 1
    b._take_over(term_b, clock.now - TIMINGS["watchdog_timeout"] - 1)
    assert "manager.start" not in b.calls


def test_a_candidate_whose_link_is_down_never_takes_the_lease(lease, clock):
    a = make(lease, "a")
    a._rejoin()
    a.runner.reach_value = None
    a._watch()
    clock.now += 100
    a._watch()
    assert a.role == coordinator_mod.Role.CANDIDATE
    assert lease.read().released


def test_an_unanswered_fabric_moves_leadership_only_to_a_live_candidate(lease, clock):
    a = make(lease, "a", step_down_cooldown=0)
    lead(a)
    a.manager.unanswered_checks = 3
    # Nobody else: stay, rather than leave the fabric with no SM.
    a._lead()
    assert a.role == coordinator_mod.Role.LEADER
    b = make(lease, "b", step_down_cooldown=0)
    b._rejoin()
    b._tick()
    a._lead()
    assert a.role == coordinator_mod.Role.STARTING
    assert "runner.stop" in a.calls


def test_a_deposed_leaders_sweep_doesnt_replace_the_successors(lease):
    from alternative_fabric_manager import manager as manager_mod
    from alternative_fabric_manager.drivers import NoOpenSM

    config = config_mod.Config(token="t", driver="none", database=":memory:")
    sessionmaker = lease.sessionmaker
    old = manager_mod.Manager(config, sessionmaker, NoOpenSM())
    new = manager_mod.Manager(config, sessionmaker, NoOpenSM())
    new.term, old.term = 2, 1
    new._record("new", 5, {}, None)
    old._record("old", 4, {}, None)
    with sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        assert (applied.body, applied.term) == ("new", 2)


def test_the_cooldown_keeps_a_new_leader_through_a_shared_fault(lease, clock):
    a = make(lease, "a", step_down_cooldown=300)
    lead(a)
    b = make(lease, "b", step_down_cooldown=300)
    b._rejoin()
    b._tick()
    a.manager.failed_applies = 3
    a._lead()
    assert a.role == coordinator_mod.Role.LEADER


def test_a_missing_switch_moves_leadership_to_a_node_that_reaches_more(lease, clock):
    a = make(lease, "a", step_down_cooldown=0)
    lead(a)
    a._reach_value = 0
    a.manager.missing_checks = 3
    b = make(lease, "b", step_down_cooldown=0)
    b._rejoin()
    b._tick()
    a._lead()
    assert a.role == coordinator_mod.Role.STARTING


def test_a_new_leadership_counts_failures_afresh(tmp_path):
    from alternative_fabric_manager import manager as manager_mod
    from alternative_fabric_manager.drivers import NoOpenSM

    config = config_mod.Config(
        token="t", driver="none", database=":memory:", config_dir=str(tmp_path)
    )
    manager = manager_mod.Manager(config, db.make_sessionmaker(":memory:"), NoOpenSM())
    manager.unanswered_checks = manager.failed_applies = manager.missing_checks = 3
    manager.start()
    manager.halt()
    assert (manager.unanswered_checks, manager.failed_applies) == (0, 0)
    assert manager.missing_checks == 0


def test_a_failed_frozen_restart_is_retried_with_the_held_lease(lease, clock):
    a = make(lease, "a")
    lead(a)
    lease.request_freeze(a.term, "operator", 30)
    a._freeze_step()

    def fail():
        raise RuntimeError("OpenSM didn't start")

    a.manager.start = fail
    a._opensm_gone = True
    a._restart_frozen()
    for _ in range(100):
        if not a._restarting:
            break
        threading.Event().wait(0.01)
    # Retried on the next tick, with the held lease written again
    assert a._opensm_gone
    assert a.runner.leases[-1] == (a.term, None)


def test_unanswered_checks_dont_wait_for_the_cooldown(lease, clock):
    a = make(lease, "a", step_down_cooldown=300)
    lead(a)
    b = make(lease, "b", step_down_cooldown=300)
    b._rejoin()
    b._tick()
    a.manager.unanswered_checks = 3
    a._lead()
    assert a.role == coordinator_mod.Role.STARTING


def test_a_failed_renewal_still_renews_the_watchdogs_lease(lease, clock, monkeypatch):
    # A Galera stall shorter than T_self, as while the survivors evict a
    # lost node, mustn't let the watchdog's lease lapse; its deadline stays
    # bounded by the last renewal that succeeded (see The watchdog).
    a = make(lease, "a")
    lead(a)
    t0 = a.t0
    a.runner.leases.clear()

    def renew(*_args):
        raise lease_mod.NotPrimary("stalled")

    monkeypatch.setattr(lease, "renew", renew)
    clock.now += TIMINGS["self_stop_timeout"] / 2
    a._tick()
    assert a.role == coordinator_mod.Role.LEADER
    assert a.t0 == t0
    (term, deadline), *_ = a.runner.leases
    assert term == a.term
    assert clock.now < deadline
    assert deadline <= t0 + TIMINGS["self_stop_timeout"] + TIMINGS["watchdog_timeout"]


def _stalled_leader(lease, clock):
    """A leader whose lease thread then makes no progress, as while its
    database calls block"""
    a = make(lease, "a")
    lead(a)
    a.runner.leases.clear()
    return a, a.t0


def _bound(t0):
    return t0 + TIMINGS["self_stop_timeout"] + TIMINGS["watchdog_timeout"]


def test_the_liveness_thread_renews_the_watchdog_through_a_stall(lease, clock):
    a, t0 = _stalled_leader(lease, clock)
    clock.now = t0 + TIMINGS["self_stop_timeout"] - 0.1
    a._renew_watchdog()
    (term, deadline), *_ = a.runner.leases
    assert term == a.term
    assert clock.now < deadline <= _bound(t0)


def test_the_liveness_thread_stops_at_t_self(lease, clock):
    a, t0 = _stalled_leader(lease, clock)
    clock.now = t0 + TIMINGS["self_stop_timeout"]
    a._renew_watchdog()
    assert not a.runner.leases


def test_the_liveness_thread_never_renews_after_a_step_down(lease, clock):
    a, _t0 = _stalled_leader(lease, clock)
    a._step_down("test")
    a.runner.leases.clear()
    a._renew_watchdog()
    assert not a.runner.leases


def test_the_liveness_thread_leaves_a_held_lease_alone(lease, clock):
    a, _t0 = _stalled_leader(lease, clock)
    lease.request_freeze(a.term, "operator", 30)
    a._freeze_step()
    assert a.held
    a.runner.leases.clear()
    a._renew_watchdog()
    assert not a.runner.leases


def test_the_liveness_thread_stops_when_a_loop_overruns(lease, clock, monkeypatch):
    a, t0 = _stalled_leader(lease, clock)
    monkeypatch.setattr(a.manager, "iteration_age", lambda: a.apply_bound + 1)
    clock.now = t0 + 1
    a._renew_watchdog()
    assert not a.runner.leases


def test_health_follows_the_database(lease, clock, monkeypatch):
    a = make(lease, "a")
    assert not a.healthy()[0]
    a._rejoin()
    assert a.healthy() == (True, "")

    real = lease.heartbeat
    cut_off = [True]

    def heartbeat(*args):
        if cut_off[0]:
            raise lease_mod.NotPrimary("cut off")
        return real(*args)

    monkeypatch.setattr(lease, "heartbeat", heartbeat)
    clock.now += 2 * TIMINGS["ha_period"] + TIMINGS["call_timeout"] + 0.1
    a._tick()
    ok, why = a.healthy()
    assert not ok and "Primary" in why
    cut_off[0] = False
    a._tick()
    assert a.healthy()[0]
    a.role = coordinator_mod.Role.STOPPING
    assert a.healthy() == (False, "Stopping")


def test_a_manager_paused_past_t_self_in_a_renewal_writes_no_lease(
    lease, clock, monkeypatch
):
    a = make(lease, "a")
    lead(a)
    a.runner.leases.clear()
    real = lease.renew

    def slow(*args):
        # Paused inside the call, past T_self from the tick's start
        clock.now += TIMINGS["self_stop_timeout"] + 1
        return real(*args)

    monkeypatch.setattr(lease, "renew", slow)
    a._lead()
    assert not a.runner.leases


def test_a_deleted_node_row_is_put_back_at_the_next_heartbeat(lease, clock):
    a = make(lease, "a")
    lead(a)
    b = make(lease, "b")
    b._rejoin()
    with lease.sessionmaker() as session:
        session.execute(sa.delete(db.Node).where(db.Node.name == "a"))
        session.commit()
    a._tick()
    # Without its row, a would be live but unseen by every other instance.
    rows = {name: instance for name, instance, *_rest in b.lease.nodes()}
    assert rows["a"] == a.uuid
    assert lease.heartbeat("a", a.uuid, 0)


def test_a_heartbeat_never_takes_the_name_back_from_another_instance(
    lease, clock, caplog
):
    a = make(lease, "a")
    a._rejoin()
    lease.register("a", "newer", "hash", [])
    a._tick()
    a._tick()
    rows = {name: instance for name, instance, *_rest in lease.nodes()}
    assert rows["a"] == "newer"
    # Reported, once rather than at every tick
    assert caplog.text.count("Another instance has registered") == 1


def test_a_new_uuid_not_yet_registered_isnt_reported_as_displaced(lease, clock, caplog):
    a = make(lease, "a")
    lead(a)
    old = a.uuid
    # As in the window after a step-down, before _rejoin registers the new
    # UUID: the row still holds this process's own old one.
    a.uuid = "new"
    a.role = coordinator_mod.Role.STARTING
    a._tick()
    rows = {name: instance for name, instance, *_rest in lease.nodes()}
    assert rows["a"] == old
    assert "Another instance" not in caplog.text


def test_a_candidate_puts_back_its_deleted_row(lease, clock):
    a = make(lease, "a")
    a._rejoin()
    assert a.role == coordinator_mod.Role.CANDIDATE
    with lease.sessionmaker() as session:
        session.execute(sa.delete(db.Node).where(db.Node.name == "a"))
        session.commit()
    a._tick()
    rows = {name: instance for name, instance, *_rest in lease.nodes()}
    assert rows["a"] == a.uuid
