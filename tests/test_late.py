"""Late requests, refused by Neutron's revision_number (see specs/ha.md,
Late requests): each sequence here is one networking-mlnx and Neutron's ML2
plugin can produce, with a request held by a paused instance arriving last
"""

import datetime
import logging

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db, partitions
from alternative_fabric_manager import manager as manager_mod
from tests import conftest
from tests.conftest import AUTH, NETWORK_ID, PORT_ID, URL

OTHER_PORT = "72c56c48-e9b8-4dcf-b3a7-0813bb3bd830"


def net(revision=None, **kwargs):
    if revision is not None:
        kwargs["revision_number"] = revision
    return conftest.network(**kwargs)


def prt(revision=None, **kwargs):
    if revision is not None:
        kwargs["revision_number"] = revision
    return conftest.port(**kwargs)


@pytest.fixture
def with_network(manager):
    manager.put_network(NETWORK_ID, net(1))
    return manager


def _generation(manager):
    with manager.sessionmaker() as session:
        state = session.get(db.State, manager_mod.GENERATION)
        return state.value if state else 0


def _port(manager, port_id=PORT_ID):
    with manager.sessionmaker() as session:
        return session.get(db.Port, port_id)


def _tombstone(manager, kind, object_id):
    with manager.sessionmaker() as session:
        return session.get(db.Tombstone, (kind, object_id))


def test_a_late_create_after_a_deletion_is_refused(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(6))
    generation = _generation(m)
    with pytest.raises(partitions.InvalidRequest, match="late"):
        m.put_port(PORT_ID, prt(5))
    assert _port(m) is None
    # Refused in the bumped transaction, which rolled back
    assert _generation(m) == generation


def test_a_rebind_after_an_unbind_is_applied(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(6))
    m.put_port(PORT_ID, prt(7))
    assert _port(m).revision == 7
    assert _tombstone(m, "port", PORT_ID) is None


def test_a_host_move_at_one_revision_is_applied(with_network):
    # A live migration sends the deletion and the create at the same
    # revision, above any earlier one.
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.put_port(PORT_ID, prt(6))
    m.delete_port(PORT_ID, prt(8))
    m.put_port(PORT_ID, prt(8))
    assert _port(m).revision == 8


def test_a_late_no_op_update_after_a_deletion_is_refused(with_network):
    # An update that changed nothing carries the same revision as the
    # deletion that follows it.
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.put_port(PORT_ID, prt(8))
    m.delete_port(PORT_ID, prt(8))
    with pytest.raises(partitions.InvalidRequest, match="late"):
        m.put_port(PORT_ID, prt(8))


def test_an_update_below_the_live_revision_is_refused(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(8))
    with pytest.raises(partitions.InvalidRequest, match="late"):
        m.put_port(PORT_ID, prt(5))
    # An equal one is a resend, and applied again.
    m.put_port(PORT_ID, prt(8))
    assert _port(m).revision == 8


def test_a_deleted_network_never_comes_back(with_network):
    m = with_network
    m.delete_network(NETWORK_ID, net(1))
    for data in (net(1), net(2), net()):
        with pytest.raises(partitions.InvalidRequest, match="deleted"):
            m.put_network(NETWORK_ID, data)


def test_a_resend_of_a_deferred_request_gets_its_job(with_network):
    # The other port holds the GUID, so this one waits.
    m = with_network
    m.put_port(OTHER_PORT, prt(3, port_id=OTHER_PORT))
    job = m.put_port(PORT_ID, prt(5))
    assert m.put_port(PORT_ID, prt(5)) == job
    with pytest.raises(partitions.InvalidRequest, match="waits"):
        m.put_port(PORT_ID, prt(4))
    assert m.get_job(job).status in (db.JobStatus.PENDING, db.JobStatus.RUNNING)


def test_a_deferred_only_port_leaves_its_revision_in_the_tombstone(with_network):
    # delete_port reads its revision before its own transaction, so it can
    # be at or below the deferred create's.
    m = with_network
    m.put_port(OTHER_PORT, prt(3, port_id=OTHER_PORT))
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(5))
    tombstone = _tombstone(m, "port", PORT_ID)
    assert (tombstone.set_revision, tombstone.deleted_revision) == (5, 5)
    with pytest.raises(partitions.InvalidRequest, match="late"):
        m.put_port(PORT_ID, prt(5))


def test_a_passing_deferred_create_keeps_the_tombstone_until_set(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(6))
    m.put_port(OTHER_PORT, prt(3, port_id=OTHER_PORT))
    m.put_port(PORT_ID, prt(7))
    # It waits for the GUID: the tombstone stays meanwhile.
    assert _tombstone(m, "port", PORT_ID) is not None
    m.delete_port(OTHER_PORT, prt(4, port_id=OTHER_PORT))
    m.retry_deferred()
    assert _port(m).revision == 7
    assert _tombstone(m, "port", PORT_ID) is None


def test_requests_without_a_revision_are_not_checked(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt())
    m.put_port(PORT_ID, prt())
    assert _port(m) is not None


def test_a_deletion_below_a_live_ports_revision_warns(with_network, caplog):
    m = with_network
    m.put_port(PORT_ID, prt(8))
    with caplog.at_level(logging.WARNING):
        m.delete_port(PORT_ID, prt(7))
    assert "Neutron still has it" in caplog.text
    assert _port(m) is None


def test_an_ordinary_deletion_at_the_ports_revision_doesnt_warn(with_network, caplog):
    # networking-mlnx's ordinary deletion carries the revision of the
    # port's last request, as tempest's runs show.
    m = with_network
    m.put_port(PORT_ID, prt(8))
    with caplog.at_level(logging.WARNING):
        m.delete_port(PORT_ID, prt(8))
    assert "Neutron still has it" not in caplog.text
    assert _port(m) is None


def test_old_tombstones_are_forgotten(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(6))
    with m.sessionmaker() as session:
        session.execute(
            sa.update(db.Tombstone).values(
                deleted_at=manager_mod._now() - datetime.timedelta(days=8)
            )
        )
        session.commit()
    m.forget_tombstones()
    assert _tombstone(m, "port", PORT_ID) is None


def test_forgetting_revisions_lets_a_restored_neutron_through(with_network):
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_network(NETWORK_ID, net(9))
    assert m.forget_revisions() == 1
    m.put_network(NETWORK_ID, net(1))
    m.put_port(PORT_ID, prt(2))
    assert _port(m).revision == 2


def test_the_api_reads_a_deletions_revision_and_refuses_late_requests(client):
    response = client.post(f"{URL}/Network", json=net(1), headers=AUTH)
    assert response.status_code == 202
    response = client.post(f"{URL}/Port", json=prt(5), headers=AUTH)
    assert response.status_code == 202
    response = client.request(
        "DELETE", f"{URL}/Port/{PORT_ID}", json=prt(6), headers=AUTH
    )
    assert response.status_code == 202
    response = client.post(f"{URL}/Port", json=prt(5), headers=AUTH)
    assert response.status_code == 400
    # A deletion without a body still gets a job.
    response = client.delete(f"{URL}/Port/{PORT_ID}", headers=AUTH)
    assert response.status_code == 202


def test_a_deferred_host_move_create_and_its_resend(with_network):
    # A host move's create waits for the GUID, another port's meanwhile; its
    # resend keeps the waiting job, without a bump, a late update from before
    # the move is refused, and once applied the tombstone goes.
    m = with_network
    m.put_port(PORT_ID, prt(5))
    m.delete_port(PORT_ID, prt(8))
    m.put_port(OTHER_PORT, prt(3, port_id=OTHER_PORT))
    job = m.put_port(PORT_ID, prt(8))
    generation = _generation(m)
    assert m.put_port(PORT_ID, prt(8)) == job
    assert _generation(m) == generation
    with pytest.raises(partitions.InvalidRequest, match="late"):
        m.put_port(PORT_ID, prt(5))
    m.delete_port(OTHER_PORT, prt(4, port_id=OTHER_PORT))
    m.retry_deferred()
    assert _port(m).revision == 8
    assert _tombstone(m, "port", PORT_ID) is None


def test_a_network_deletion_at_its_revision_doesnt_warn(with_network, caplog):
    # A network never updated is deleted at its creation's revision.
    with caplog.at_level(logging.WARNING):
        with_network.delete_network(NETWORK_ID, net(1))
    assert "Neutron still has it" not in caplog.text


def test_a_deletion_with_an_odd_body_still_gets_a_job(client):
    for body in (b"not json", b"[1, 2]", b""):
        response = client.request(
            "DELETE",
            f"{URL}/Port/{PORT_ID}",
            content=body,
            headers=dict(AUTH, **{"Content-Type": "application/json"}),
        )
        assert response.status_code == 202, body
