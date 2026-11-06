"""The lease's compare-and-sets (see specs/ha.md, The lease and Freeze)"""

import datetime

import pytest

from alternative_fabric_manager import db
from alternative_fabric_manager.ha import lease as lease_mod


@pytest.fixture
def lease(config):
    sessionmaker = db.make_sessionmaker(config.database_url)
    driver = lease_mod.GaleraLease(sessionmaker)
    for node, uuid in (("a", "ua"), ("b", "ub"), ("c", "uc")):
        driver.register(node, uuid, "hash", [])
    return driver


def _take(lease, node, uuid):
    state = lease.read()
    return lease.acquire(node, uuid, state.term, state.counter, state.released)


def test_a_released_lease_is_taken_once(lease):
    state = lease.read()
    assert state.released
    assert lease.acquire("a", "ua", state.term, state.counter, True) == 1
    # Another candidate, on the same stale view, loses.
    assert lease.acquire("b", "ub", state.term, state.counter, True) is None
    assert lease.read().holder_uuid == "ua"
    assert lease.targets() == [("a", "ua", 1)]


def test_only_the_holder_of_the_term_renews(lease):
    term = _take(lease, "a", "ua")
    before = lease.read().counter
    assert lease.renew("ua", term)
    assert lease.read().counter == before + 1
    assert not lease.renew("ub", term)
    assert not lease.renew("ua", term - 1)


def test_an_expired_lease_is_taken_at_the_counter_seen(lease):
    _take(lease, "a", "ua")
    seen = lease.read()
    # The holder renews after the candidate's last read: its view is stale.
    lease.renew("ua", seen.term)
    assert lease.acquire("b", "ub", seen.term, seen.counter, False) is None
    fresh = lease.read()
    term = lease.acquire("b", "ub", fresh.term, fresh.counter, False)
    assert term == seen.term + 1
    # The old holder can't renew, and stays a target until it leaves.
    assert not lease.renew("ua", seen.term)
    assert sorted(lease.targets()) == [("a", "ua", 1), ("b", "ub", 2)]


def test_release_removes_only_the_releasers_entry(lease):
    _take(lease, "a", "ua")
    term = _take(lease, "b", "ub")
    assert lease.release("b", "ub", term)
    assert lease.targets() == [("a", "ua", 1)]
    assert lease.read().released
    assert not lease.release("b", "ub", term + 1)


def test_a_verified_stop_counts_only_from_another_instance_after_the_entry(lease):
    _take(lease, "a", "ua")
    term = _take(lease, "b", "ub")
    # a's own instance can't vouch for itself.
    lease.record_verified_stop("a", "ua")
    assert not lease.remove_target(term, "a", "ua", "verified")
    # a's restarted instance verifies, at the current term, after the entry.
    lease.register("a", "ua2", "hash", [])
    lease.record_verified_stop("a", "ua2")
    assert lease.remove_target(term, "a", "ua", "verified")
    assert lease.targets() == [("b", "ub", 2)]
    # Only the leader of the current term removes entries.
    assert not lease.remove_target(term - 1, "b", "ub", "unconfirmed")


def test_taking_clears_the_mark_and_removes_covered_own_entries(lease):
    _take(lease, "a", "ua")
    # a restarts as ua2, verifies its node's stop, then takes the lease.
    lease.register("a", "ua2", "hash", [])
    lease.record_verified_stop("a", "ua2")
    state = lease.read()
    lease.release("a", "ua", state.term) or None
    term = _take(lease, "a", "ua2")
    assert lease.targets() == [("a", "ua2", term)]
    with lease.sessionmaker() as session:
        assert session.get(db.Node, "a").verified_stop_term == -1


def test_an_own_entry_added_after_the_mark_stays(lease):
    lease.record_verified_stop("a", "ua2")
    _take(lease, "a", "ua")
    lease.register("a", "ua2", "hash", [])
    term = _take(lease, "a", "ua2")
    # ua's entry came after ua2's verification, so it stays a target.
    assert sorted(lease.targets()) == [("a", "ua", 1), ("a", "ua2", term)]


def test_a_freeze_blocks_takeovers_until_it_ends(lease):
    term = _take(lease, "a", "ua")
    assert lease.request_freeze(term, "operator", 30)
    state = lease.read()
    assert lease.acquire("b", "ub", state.term, state.counter, False) is None
    assert lease.acknowledge_freeze("ua", term)
    state = lease.read()
    assert state.freeze_state == "frozen"
    assert lease.acquire("b", "ub", state.term, state.counter, False) is None
    # A frozen leader never releases.
    assert not lease.release("a", "ua", term)
    assert lease.request_unfreeze() == "ending"
    state = lease.read()
    assert lease.acquire("b", "ub", state.term, state.counter, False) is None
    # Only once the leader acknowledges is it unfrozen, and its counter moves.
    assert lease.acknowledge_unfreeze("ua", term)
    after = lease.read()
    assert after.freeze_state == "" and after.counter == state.counter + 1


def test_an_unacknowledged_freeze_request_lapses(lease):
    term = _take(lease, "a", "ua")
    assert lease.request_freeze(term, "operator", 30)
    with lease.sessionmaker() as session:
        session.get(db.Lease, 1).freeze_request_deadline = datetime.datetime(
            2000, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.commit()
    # Too late for the leader, but not for a takeover.
    assert not lease.acknowledge_freeze("ua", term)
    state = lease.read()
    assert state.freeze_request_deadline_passed
    assert lease.acquire("b", "ub", state.term, state.counter, False) == term + 1
    assert lease.read().freeze_state == ""


def test_an_ending_freeze_needs_a_confirmation_or_a_verified_stop(lease):
    term = _take(lease, "a", "ua")
    lease.request_freeze(term, "operator", 30)
    lease.acknowledge_freeze("ua", term)
    lease.request_unfreeze()
    state = lease.read()
    assert lease.acquire("b", "ub", state.term, state.counter, False) is None
    assert lease.confirm(term)
    assert lease.acquire("b", "ub", state.term, state.counter, False) == term + 1


def test_a_freeze_needs_the_holder_alone_in_the_target_set(lease):
    _take(lease, "a", "ua")
    term = _take(lease, "b", "ub")
    assert not lease.request_freeze(term, "operator", 30)


def test_adopting_a_frozen_lease_clears_an_earlier_verified_stop(lease):
    term = _take(lease, "a", "ua")
    lease.request_freeze(term, "operator", 30)
    lease.acknowledge_freeze("ua", term)
    # a's manager restarts, verifies its stop, then adopts and starts OpenSM.
    lease.register("a", "ua2", "hash", [])
    lease.record_verified_stop("a", "ua2")
    assert lease.adopt_frozen("a", "ua2") == term
    assert lease.targets() == [("a", "ua2", term)]
    # An unfreeze that the leader doesn't acknowledge: the earlier mark
    # mustn't let a candidate take over.
    lease.request_unfreeze()
    state = lease.read()
    assert lease.acquire("b", "ub", state.term, state.counter, False) is None
    assert not lease.remove_target(term, "a", "ua2", "verified")
