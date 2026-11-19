"""Configuration epochs (see specs/ha.md, Configuration epochs)"""

import pytest

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db
from alternative_fabric_manager.ha import coordinator as coordinator_mod
from alternative_fabric_manager.ha import lease as lease_mod
from alternative_fabric_manager.ha import operator
from tests.test_coordinator import TIMINGS, Clock, Manager, Runner, lead


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


def make(lease, node, config_hash, **overrides):
    config = config_mod.Config(token="t", node=node, **dict(TIMINGS, **overrides))
    calls = []
    coordinator = coordinator_mod.Coordinator(
        config,
        lease,
        Runner(calls),
        Manager(calls),
        hash_config=lambda _key: config_hash,
    )
    coordinator.calls = calls
    return coordinator


A, B = "a" * 64, "b" * 64


def test_the_hash_leaves_out_rolling_and_node_options():
    base = config_mod.Config(token="t")
    key = "k"
    same = config_mod.Config(
        token="t",
        opensm_conf={"guid": "0x1", "sweep_interval": "5", "log_file": "/x"},
    )
    assert config_mod.config_hash(base, key) == config_mod.config_hash(same, key)
    other = config_mod.Config(token="t", opensm_conf={"m_key_lease_period": "60"})
    assert config_mod.config_hash(base, key) != config_mod.config_hash(other, key)
    assert config_mod.config_hash(base, key) != config_mod.config_hash(base, "j")


def test_the_hash_follows_the_hardening_groups():
    key = "k"
    one = config_mod.Config(token="t", hardening="all")
    two = config_mod.Config(token="t", hardening="all, sa_rate")
    assert config_mod.config_hash(one, key) != config_mod.config_hash(two, key)


def test_the_first_takeover_records_an_epoch_with_its_hash(lease):
    state = lease.read()
    assert state.allowed_hashes is None
    assert lease.acquire("a", "ua", state.term, state.counter, True, A) == 1
    assert lease.read().allowed_hashes == (A,)
    assert lease.epoch() == (1, (A,))


def test_a_hash_the_epoch_doesnt_allow_cant_take_the_lease(lease):
    lease.allow_config([A])
    state = lease.read()
    assert lease.acquire("b", "ub", state.term, state.counter, True, B) is None
    assert lease.acquire("a", "ua", state.term, state.counter, True, A) == 1


def test_an_epoch_allows_at_most_two_hashes(lease):
    assert lease.allow_config([A, B]) == 1
    assert lease.allow_config([A]) == 2
    with pytest.raises(ValueError):
        lease.allow_config([A, B, "c" * 64])


def test_the_hash_key_is_generated_once(lease):
    assert lease.hash_key() == lease.hash_key()


def test_a_candidate_whose_configuration_isnt_allowed_doesnt_take_over(
    lease, clock, caplog
):
    lease.allow_config([A])
    # A live node still runs the allowed configuration, so the epoch stays.
    a = make(lease, "a", A)
    a._rejoin()
    b = make(lease, "b", B)
    b._rejoin()
    b._watch()
    assert b.role == coordinator_mod.Role.CANDIDATE
    assert lease.read().released
    assert lease.epoch() == (1, (A,))
    assert "isn't one the configuration epoch allows" in caplog.text


def test_the_epoch_moves_once_every_live_node_agrees(lease, clock):
    a = make(lease, "a", A)
    lead(a)
    b = make(lease, "b", B)
    b._rejoin()
    c = make(lease, "c", B)
    c._rejoin()
    # The leader, a, still runs A: no move.
    b._watch()
    assert lease.epoch() == (1, (A,))
    # a restarts with B, as the last node of a rolling change.
    a.shutdown()
    a2 = make(lease, "a", B)
    a2._rejoin()
    b._watch()
    assert lease.epoch() == (2, (B,))
    clock.now += 100
    b._watch()
    assert b.role in (coordinator_mod.Role.TAKING_OVER, coordinator_mod.Role.LEADER)


def test_a_dead_nodes_row_stops_counting_after_t(lease, clock):
    a = make(lease, "a", A)
    lead(a)
    # a dies: its heartbeat stops, without a clean stop.
    b = make(lease, "b", B)
    b._rejoin()
    b._watch()
    assert lease.epoch() == (1, (A,))
    clock.now += TIMINGS["lease_timeout"] + 1
    b._tick()
    b._watch()
    assert lease.epoch() == (2, (B,))


def test_a_returning_node_with_the_old_configuration_isnt_allowed(lease, clock):
    lease.allow_config([B])
    b = make(lease, "b", B)
    b._rejoin()
    a = make(lease, "a", A)
    a._rejoin()
    a._watch()
    assert lease.epoch() == (1, (B,))


def test_experimental_groups_wait_for_every_node_to_have_epochs(lease, clock, caplog):
    old = make(lease, "old", "")
    old._rejoin()
    new = make(lease, "new", A, hardening="all, sa_rate")
    new._rejoin()
    new._watch()
    clock.now += 100
    # The old release's heartbeat moves: it's live.
    lease.heartbeat("old", old.uuid, 1)
    new._watch()
    assert new.role == coordinator_mod.Role.CANDIDATE
    assert "a release without configuration epochs" in caplog.text
    # Without the groups, it may lead beside the old release.
    plain = make(lease, "plain", A)
    plain._rejoin()
    plain._watch()
    assert plain.role in (
        coordinator_mod.Role.TAKING_OVER,
        coordinator_mod.Role.LEADER,
    )


def test_no_step_down_for_a_candidate_that_reaches_nothing(lease, clock):
    a = make(lease, "a", A, step_down_cooldown=0)
    lead(a)
    lease.allow_config([B])
    a._freeze_step()
    b = make(lease, "b", B, step_down_cooldown=0)
    b.runner.reach_value = 0
    b._rejoin()
    b._tick()
    a._lead()
    assert a.role == coordinator_mod.Role.LEADER


def test_a_leader_whose_configuration_is_no_longer_allowed_hands_over(lease, clock):
    a = make(lease, "a", A, step_down_cooldown=0)
    lead(a)
    a._freeze_step()
    # Nobody else allowed: it stays.
    lease.allow_config([B])
    a._freeze_step()
    a._lead()
    assert a.role == coordinator_mod.Role.LEADER
    b = make(lease, "b", B, step_down_cooldown=0)
    b._rejoin()
    b._tick()
    a._lead()
    assert a.role == coordinator_mod.Role.STARTING


def test_a_candidate_not_allowed_doesnt_count_as_live(lease, clock):
    a = make(lease, "a", A, step_down_cooldown=0)
    lead(a)
    b = make(lease, "b", B, step_down_cooldown=0)
    b._rejoin()
    b._tick()
    a._freeze_step()
    a.manager.failed_applies = 3
    a._lead()
    assert a.role == coordinator_mod.Role.LEADER


def test_allow_config_records_this_hash_and_another(lease):
    config = config_mod.Config(token="t", ha=True)
    ours = operator.own_hash(config, lease)
    epoch, hashes = operator.allow_config(config, lease, B)
    assert epoch == 1 and hashes == sorted([ours, B])
    with pytest.raises(operator.OperatorError):
        operator.allow_config(config, lease, "nope")
    lines = operator.config_epoch(config, lease)
    assert lines[0].startswith("Epoch 1 allows")
    assert lines[1] == f"This configuration: {ours} allowed"


def test_forget_node_refuses_a_target_and_a_live_node(lease, clock):
    config = config_mod.Config(token="t", ha=True, **TIMINGS)
    a = make(lease, "a", A)
    lead(a)
    with pytest.raises(operator.OperatorError, match="target set"):
        operator.forget_node(config, lease, "a", wait=lambda _s: None)
    b = make(lease, "b", A)
    b._rejoin()

    def beat(_seconds):
        b._tick()

    with pytest.raises(operator.OperatorError, match="live"):
        operator.forget_node(config, lease, "b", wait=beat)
    operator.forget_node(config, lease, "b", wait=lambda _s: None)
    assert "b" not in {row[0] for row in lease.nodes()}


def test_a_forgotten_nodes_ports_stay_known(lease, clock):
    b = make(lease, "b", A)
    b._rejoin()
    assert lease.record_ports("b", b.uuid, {0x11, 0x12})
    assert lease.node_ports()["b"] == {0x11, 0x12}
    lease.mark_stopped("b", b.uuid)
    assert lease.forget_node("b")
    with lease.sessionmaker() as session:
        assert {row.guid for row in session.query(db.KnownSMPort)} == {
            "0000000000000011",
            "0000000000000012",
        }
