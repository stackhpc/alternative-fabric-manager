"""Requests that conflict with another writer run again, validation
included (see specs/ha.md, Galera specifics)"""

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db
from alternative_fabric_manager import manager as manager_mod
from tests import conftest
from tests.conftest import NETWORK_ID

OTHER = "d13bba05-eb07-45ba-ace2-765706b2d701"


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(manager_mod.random, "uniform", lambda _a, _b: 0)


def _error(code, message):
    return sa.exc.OperationalError("COMMIT", {}, Exception(code, message))


def _jobs(manager):
    with manager.sessionmaker() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(db.Job))


def test_a_pkey_race_at_the_constraint_waits_rather_than_failing(manager, monkeypatch):
    # Another instance's network took the pkey, committed through another
    # Galera node after this request's validation looked: the constraint
    # catches it, and the retry's validation makes the request wait.
    manager.put_network(OTHER, conftest.network(OTHER, segmentation_id=100))
    blocked = manager._network_blocked
    calls = []

    def blind_first(session, network_id, values):
        calls.append(network_id)
        return None if len(calls) == 1 else blocked(session, network_id, values)

    monkeypatch.setattr(manager, "_network_blocked", blind_first)
    job_id = manager.put_network(NETWORK_ID, conftest.network(segmentation_id=100))
    assert len(calls) == 2
    assert "waiting" in manager.get_job(job_id).error
    with manager.sessionmaker() as session:
        assert session.get(db.Network, NETWORK_ID) is None
        assert session.get(db.Deferred, ("network", NETWORK_ID)) is not None


def test_a_deadlock_runs_the_request_again_once(manager, monkeypatch):
    commit_job = manager._commit_job
    calls = []

    def conflicting(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _error(db.DEADLOCK, "Deadlock found")
        return commit_job(*args, **kwargs)

    monkeypatch.setattr(manager, "_commit_job", conflicting)
    job_id = manager.put_network(NETWORK_ID, conftest.network())
    assert len(calls) == 2
    assert _jobs(manager) == 1
    assert manager.get_job(job_id).operation == "put_network"


def test_a_lost_connection_isnt_retried(manager, monkeypatch):
    # Its commit may have happened: running it again could apply it twice.
    calls = []

    def lost(*_args, **_kwargs):
        calls.append(1)
        raise _error(2013, "Lost connection to MySQL server during query")

    monkeypatch.setattr(manager, "_commit_job", lost)
    with pytest.raises(sa.exc.OperationalError):
        manager.put_network(NETWORK_ID, conftest.network())
    assert len(calls) == 1


def test_a_conflict_outlasting_the_retries_is_raised(manager, monkeypatch):
    calls = []

    def always(*_args, **_kwargs):
        calls.append(1)
        raise _error(db.DEADLOCK, "Deadlock found")

    monkeypatch.setattr(manager, "_commit_job", always)
    with pytest.raises(sa.exc.OperationalError):
        manager.delete_network(NETWORK_ID)
    assert len(calls) == manager_mod.REQUEST_ATTEMPTS
    assert _jobs(manager) == 0
