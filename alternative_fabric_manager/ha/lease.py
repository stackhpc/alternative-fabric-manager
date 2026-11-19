# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""The lease, in Galera: the first coordination driver (see specs/ha.md)

Every call is one transaction that first checks that the database node
serving it is in a Primary component, and fails otherwise, so that only the
majority side of a partition can renew, watch or take the lease. Every
change a leader makes is a compare-and-set on its term. This module has no
timing of its own: the coordinator times renewals and expiry on its own
monotonic clock.
"""

import dataclasses
import datetime
import json
import logging
import secrets

import sqlalchemy as sa

from alternative_fabric_manager import db

LOG = logging.getLogger(__name__)


# The configuration epoch, and the key of the configuration hash's HMAC, as
# secrets rows (see specs/ha.md, Configuration epochs)
EPOCH = "config_epoch"
HASH_KEY = "config_hash_key"
# An epoch allows one hash, or two during a change.
MAX_HASHES = 2


def _epoch(session, lock=False):
    """(epoch, allowed hashes as a tuple), or None if none is stored"""
    row = session.get(db.Secret, EPOCH, with_for_update=lock)
    if row is None:
        return None
    value = json.loads(row.value)
    return value["epoch"], tuple(value["hashes"])


def _store_epoch(session, epoch, hashes):
    value = json.dumps({"epoch": epoch, "hashes": sorted(hashes)})
    row = session.get(db.Secret, EPOCH)
    if row is None:
        session.add(db.Secret(name=EPOCH, value=value))
    else:
        row.value = value


class NotPrimary(Exception):
    """The database node isn't in a Primary component, or isn't ready"""


@dataclasses.dataclass(frozen=True)
class LeaseState:
    holder_node: str
    holder_uuid: str
    term: int
    counter: int
    released: bool
    freeze_state: str
    freeze_term: int
    freeze_request_deadline_passed: bool
    confirmed_term: int
    # Seconds since leadership last moved, by the database's clock
    moved_age: float = 0.0
    # The hashes the configuration epoch allows, or None while no epoch is
    # stored, when every hash is allowed
    allowed_hashes: tuple | None = None


def _utc_now(session):
    """The database's UTC time, judged in the statement that uses it"""
    if session.get_bind().dialect.name == "sqlite":
        return datetime.datetime.now(datetime.timezone.utc)
    return session.scalar(sa.text("SELECT UTC_TIMESTAMP(6)"))


def _age(when, now):
    if when is None or now is None:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (now - when).total_seconds()


def _check_primary(session):
    """A lease call runs only on a Galera node in a Primary component, and
    ready (see The lease, Galera specifics)"""
    try:
        db.check_serving(session)
    except db.NotServing as e:
        raise NotPrimary(str(e)) from e


class GaleraLease:
    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker

    def _session(self):
        session = self.sessionmaker()
        _check_primary(session)
        return session

    @staticmethod
    def _row(session, lock=False):
        query = sa.select(db.Lease).where(db.Lease.id == 1)
        if lock:
            query = query.with_for_update()
        lease = session.scalar(query)
        if lease is None:
            lease = db.Lease(id=1)
            session.add(lease)
            session.flush()
        return lease

    def read(self):
        """Read the lease"""
        with self._session() as session:
            lease = self._row(session)
            deadline = lease.freeze_request_deadline
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            now = _utc_now(session)
            if now is not None and now.tzinfo is None:
                now = now.replace(tzinfo=datetime.timezone.utc)
            state = LeaseState(
                holder_node=lease.holder_node,
                holder_uuid=lease.holder_uuid,
                term=lease.term,
                counter=lease.counter,
                released=lease.released,
                freeze_state=lease.freeze_state,
                freeze_term=lease.freeze_term,
                freeze_request_deadline_passed=deadline is not None and now > deadline,
                confirmed_term=lease.confirmed_term,
                moved_age=_age(lease.moved_at, now),
                allowed_hashes=epoch[1] if (epoch := _epoch(session)) else None,
            )
            session.commit()
            return state

    def renew(self, instance_uuid, term):
        """Bump the counter; return False if the lease is no longer ours"""
        with self._session() as session:
            result = session.execute(
                sa.update(db.Lease)
                .where(
                    db.Lease.id == 1,
                    db.Lease.holder_uuid == instance_uuid,
                    db.Lease.term == term,
                    db.Lease.released.is_(False),
                )
                .values(counter=db.Lease.counter + 1)
            )
            session.commit()
            return result.rowcount == 1

    @staticmethod
    def _takeover_allowed(lease, deadline_passed):
        """Whether the freeze lets anyone take the lease over"""
        if lease.freeze_state == "":
            return True
        if lease.freeze_state == "requested":
            # An unacknowledged request past its deadline counts as withdrawn.
            return deadline_passed
        if lease.freeze_state == "ending":
            # Only with an operator's confirmation for the frozen term; a
            # verified stop is checked by the caller.
            return lease.confirmed_term == lease.freeze_term
        return False

    def acquire(
        self, node, instance_uuid, seen_term, seen_counter, released, config_hash=None
    ):
        """Take the lease, if it is still as seen: unchanged since it
        expired, or released. Adds this instance's target-set entry, clears
        its node's verified-stop mark, and removes its node's older entries
        that the mark covers, all in one transaction. Returns the new term,
        or None if someone else moved first.

        With config_hash, only if the configuration epoch allows it; with no
        epoch stored, the takeover stores one that allows only it (see
        specs/ha.md, Configuration epochs)."""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if (lease.term, lease.counter, lease.released) != (
                seen_term,
                seen_counter,
                released,
            ):
                session.rollback()
                return None
            if config_hash is not None:
                epoch = _epoch(session, lock=True)
                if epoch is None:
                    _store_epoch(session, 1, [config_hash])
                    LOG.warning(
                        "Recorded configuration epoch 1, allowing this node's "
                        "configuration, %s",
                        config_hash[:12],
                    )
                elif config_hash not in epoch[1]:
                    session.rollback()
                    return None
            deadline = lease.freeze_request_deadline
            now = _utc_now(session)
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            if now is not None and now.tzinfo is None:
                now = now.replace(tzinfo=datetime.timezone.utc)
            passed = deadline is not None and now > deadline
            own = session.get(db.Node, node)
            if not self._takeover_allowed(lease, passed):
                # While a freeze is ending, the frozen node's OpenSM must be
                # verified stopped, by another instance than the holder.
                frozen = session.get(db.Node, lease.holder_node)
                stopped = (
                    lease.freeze_state == "ending"
                    and frozen is not None
                    and frozen.verified_stop_uuid not in ("", lease.holder_uuid)
                    and frozen.verified_stop_term >= lease.freeze_term
                )
                if not stopped:
                    session.rollback()
                    return None
                # The frozen holder's entry now leaves as verified.
                session.execute(
                    sa.delete(db.TargetEntry).where(
                        db.TargetEntry.node == lease.holder_node,
                        db.TargetEntry.instance_uuid == lease.holder_uuid,
                    )
                )
            ending = lease.freeze_state == "ending"
            old_node, old_uuid = lease.holder_node, lease.holder_uuid
            lease.holder_node = node
            lease.holder_uuid = instance_uuid
            lease.term += 1
            lease.counter += 1
            lease.released = False
            lease.moved_at = now
            if ending:
                # Allowed by a verified stop or a confirmation of the frozen
                # holder: its held entry leaves in this takeover.
                session.execute(
                    sa.delete(db.TargetEntry).where(
                        db.TargetEntry.node == old_node,
                        db.TargetEntry.instance_uuid == old_uuid,
                    )
                )
            lease.freeze_state = ""
            lease.freeze_request_deadline = None
            # A confirmation is used up by the takeover that follows it.
            lease.confirmed_term = 0
            term = lease.term
            stop_term = own.verified_stop_term if own is not None else -1
            stop_uuid = own.verified_stop_uuid if own is not None else ""
            for entry in session.scalars(
                sa.select(db.TargetEntry).where(db.TargetEntry.node == node)
            ):
                if (
                    entry.instance_uuid != instance_uuid
                    and stop_uuid not in ("", entry.instance_uuid)
                    and stop_term >= entry.term
                    and not entry.held
                ):
                    session.delete(entry)
            session.add(
                db.TargetEntry(node=node, instance_uuid=instance_uuid, term=term)
            )
            if own is not None:
                own.verified_stop_term = -1
                own.verified_stop_uuid = ""
            session.commit()
            return term

    def release(self, node, instance_uuid, term):
        """Give the lease up, having verified that our OpenSM stopped"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.holder_uuid != instance_uuid or lease.term != term:
                session.rollback()
                return False
            if lease.freeze_state == "requested":
                # Not frozen yet: withdraw the request, and release.
                lease.freeze_state = ""
                lease.freeze_request_deadline = None
            elif lease.freeze_state:
                # A frozen leader never releases (see Freeze).
                session.rollback()
                return False
            session.execute(
                sa.delete(db.TargetEntry).where(
                    db.TargetEntry.node == node,
                    db.TargetEntry.instance_uuid == instance_uuid,
                )
            )
            lease.released = True
            session.commit()
            return True

    def targets(self):
        """The target set's entries, as (node, instance UUID, term)"""
        with self._session() as session:
            entries = [
                (e.node, e.instance_uuid, e.term)
                for e in session.scalars(sa.select(db.TargetEntry))
            ]
            session.commit()
            return entries

    def remove_target(self, leader_uuid, term, node, instance_uuid, how):
        """Remove a target entry, as the leader of term, recording how it
        left: "verified", if its node verified its stop after the entry was
        added, or "unconfirmed" after the wait. A held entry, a frozen
        leader's, leaves unconfirmed only with an operator's confirmation
        for its term."""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.term != term or lease.holder_uuid != leader_uuid:
                session.rollback()
                return False
            entry = session.get(db.TargetEntry, (node, instance_uuid))
            if entry is None:
                session.commit()
                return True
            if how == "verified":
                other = session.get(db.Node, node)
                if (
                    other is None
                    or other.verified_stop_uuid in ("", instance_uuid)
                    or other.verified_stop_term < entry.term
                ):
                    session.rollback()
                    return False
            elif entry.held and lease.confirmed_term != entry.term:
                session.rollback()
                return False
            session.delete(entry)
            session.commit()
            return True

    def register(self, node, instance_uuid, config_hash, adapters):
        """Register an instance on its node row, at start"""
        with self._session() as session:
            row = session.get(db.Node, node)
            if row is None:
                row = db.Node(name=node)
                session.add(row)
            row.instance_uuid = instance_uuid
            row.config_hash = config_hash
            row.adapters = json.dumps(sorted(adapters))
            row.stopped = False
            session.commit()

    def heartbeat(self, node, instance_uuid, reach):
        with self._session() as session:
            result = session.execute(
                sa.update(db.Node)
                .where(db.Node.name == node, db.Node.instance_uuid == instance_uuid)
                .values(heartbeat=db.Node.heartbeat + 1, reach=reach)
            )
            session.commit()
            return result.rowcount == 1

    def restore(self, node, instance_uuid, config_hash, adapters):
        """Put back this instance's node row if none exists, as after someone
        deleted it by hand, and return whether it did. A row that exists is
        left alone, whichever UUID it holds (see One instance per name)."""
        with self._session() as session:
            if session.get(db.Node, node) is not None:
                session.rollback()
                return False
            session.add(
                db.Node(
                    name=node,
                    instance_uuid=instance_uuid,
                    config_hash=config_hash,
                    adapters=json.dumps(sorted(adapters)),
                )
            )
            session.commit()
            return True

    def record_verified_stop(self, node, instance_uuid):
        """Record that this instance verified its node runs no OpenSM, at the
        lease term read in the same transaction"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.holder_uuid == instance_uuid and not lease.released:
                # A holder can't vouch for its own stop.
                session.rollback()
                return None
            row = session.get(db.Node, node)
            if row is None:
                row = db.Node(name=node, instance_uuid=instance_uuid)
                session.add(row)
            row.verified_stop_uuid = instance_uuid
            row.verified_stop_term = lease.term
            session.commit()
            return lease.term

    def mark_stopped(self, node, instance_uuid):
        with self._session() as session:
            session.execute(
                sa.update(db.Node)
                .where(db.Node.name == node, db.Node.instance_uuid == instance_uuid)
                .values(stopped=True)
            )
            session.commit()

    def expected_switches(self):
        """How many switches a complete fabric has"""
        with self._session() as session:
            # A pending switch isn't expected yet.
            count = session.scalar(
                sa.select(sa.func.count())
                .select_from(db.ExpectedSwitch)
                .where(db.ExpectedSwitch.pending.is_(False))
            )
            session.commit()
            return count or 0

    def nodes(self):
        """Each node row, as (name, instance UUID, heartbeat, reach,
        stopped, configuration hash)"""
        with self._session() as session:
            rows = [
                (
                    n.name,
                    n.instance_uuid,
                    n.heartbeat,
                    n.reach,
                    n.stopped,
                    n.config_hash,
                )
                for n in session.scalars(sa.select(db.Node))
            ]
            session.commit()
            return rows

    # Node port GUIDs (see specs/ha.md, The secured profile)

    def record_ports(self, node, instance_uuid, guids):
        """Record a successful probe's physical port GUIDs, as integers, in
        this instance's node row, and among the known SM ports, in one
        transaction; False if the row isn't this instance's"""
        texts = sorted(f"{guid:016x}" for guid in guids)
        if not texts:
            return False
        with self._session() as session:
            row = session.get(db.Node, node)
            if row is None or row.instance_uuid != instance_uuid:
                session.rollback()
                return False
            row.port_guids = json.dumps(texts)
            now = _utc_now(session)
            for text in texts:
                if session.get(db.KnownSMPort, text) is None:
                    session.add(db.KnownSMPort(guid=text, own=False, recorded=now))
            session.commit()
            return True

    def node_ports(self):
        """Each node row's port GUIDs, as {name: frozenset of integers}"""
        with self._session() as session:
            rows = {
                n.name: frozenset(int(g, 16) for g in json.loads(n.port_guids or "[]"))
                for n in session.scalars(sa.select(db.Node))
            }
            session.commit()
            return rows

    def forget_node(self, name, heartbeat=None):
        """Remove a retired node's row, if it's in no target-set entry and,
        with heartbeat, its heartbeat is still that, or, without, it's marked
        stopped. Its port GUIDs stay among the known SM ports."""
        with self._session() as session:
            self._row(session, lock=True)
            row = session.get(db.Node, name, with_for_update=True)
            entries = session.scalar(
                sa.select(sa.func.count())
                .select_from(db.TargetEntry)
                .where(db.TargetEntry.node == name)
            )
            if (
                row is None
                or entries
                or (heartbeat is None and not row.stopped)
                or (heartbeat is not None and row.heartbeat != heartbeat)
            ):
                session.rollback()
                return False
            session.delete(row)
            session.commit()
            return True

    # Configuration epochs (see specs/ha.md, Configuration epochs)

    def hash_key(self):
        """The configuration hash's HMAC key, generated once"""
        with self._session() as session:
            row = session.get(db.Secret, HASH_KEY)
            if row is None:
                row = db.Secret(name=HASH_KEY, value=secrets.token_hex(32))
                session.add(row)
                try:
                    session.commit()
                except sa.exc.IntegrityError:
                    # Another instance stored one first.
                    session.rollback()
                    row = session.get(db.Secret, HASH_KEY)
            value = row.value
            session.commit()
            return value

    def epoch(self):
        """(epoch, allowed hashes), or None while none is stored"""
        with self._session() as session:
            epoch = _epoch(session)
            session.commit()
            return epoch

    def allow_config(self, hashes, seen=False, settings=None):
        """Record a new epoch that allows exactly these hashes; return its
        number, or None if seen is given, as the epoch read before, (epoch,
        hashes) or None, and the epoch has moved since. With settings, the
        recorder's [ha] settings, as JSON, are stored with it: they're in
        its hash, so the nodes the epoch allows agree with them."""
        hashes = sorted(set(hashes))
        if not 1 <= len(hashes) <= MAX_HASHES:
            raise ValueError(f"an epoch allows 1 to {MAX_HASHES} hashes")
        with self._session() as session:
            epoch = _epoch(session, lock=True)
            if seen is not False and epoch != seen:
                session.rollback()
                return None
            number = (epoch[0] if epoch else 0) + 1
            _store_epoch(session, number, hashes)
            if settings is not None:
                row = session.get(db.Secret, "ha_settings", with_for_update=True)
                ours = json.loads(settings)
                if row is None:
                    session.add(db.Secret(name="ha_settings", value=settings))
                else:
                    row.value = json.dumps(
                        {**json.loads(row.value), **ours}, sort_keys=True
                    )
            session.commit()
            return number

    # The freeze (see specs/ha.md, Freeze)

    def request_freeze(self, term, by, request_seconds):
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.term != term or lease.released or lease.freeze_state:
                session.rollback()
                return False
            entries = list(session.scalars(sa.select(db.TargetEntry)))
            if [e.instance_uuid for e in entries] != [lease.holder_uuid]:
                session.rollback()
                return False
            now = _utc_now(session)
            lease.freeze_state = "requested"
            lease.freeze_term = term
            # No earlier confirmation may end this freeze.
            lease.confirmed_term = 0
            lease.freeze_by = by
            lease.freeze_at = now
            lease.freeze_request_deadline = now + datetime.timedelta(
                seconds=request_seconds
            )
            session.commit()
            return True

    def acknowledge_freeze(self, instance_uuid, term):
        """As the leader: freeze, if the request is still before its deadline,
        judged by the database's clock in this transaction"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            now = _utc_now(session)
            deadline = lease.freeze_request_deadline
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=datetime.timezone.utc)
            if (
                lease.freeze_state != "requested"
                or lease.holder_uuid != instance_uuid
                or lease.term != term
                or deadline is None
                or now > deadline
            ):
                session.rollback()
                return False
            lease.freeze_state = "frozen"
            lease.freeze_request_deadline = None
            session.execute(
                sa.update(db.TargetEntry)
                .where(db.TargetEntry.instance_uuid == instance_uuid)
                .values(held=True)
            )
            session.commit()
            return True

    def withdraw_freeze(self):
        """Withdraw a pending request; return the freeze's state after"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.freeze_state == "requested":
                lease.freeze_state = ""
                lease.freeze_request_deadline = None
            state = lease.freeze_state
            session.commit()
            return state

    def request_unfreeze(self):
        with self._session() as session:
            lease = self._row(session, lock=True)
            if lease.freeze_state == "frozen":
                lease.freeze_state = "ending"
            session.commit()
            return lease.freeze_state

    def acknowledge_unfreeze(self, instance_uuid, term):
        """As the leader, once it renews normally again: end the freeze"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if (
                lease.freeze_state != "ending"
                or lease.holder_uuid != instance_uuid
                or lease.term != term
            ):
                session.rollback()
                return False
            lease.freeze_state = ""
            lease.counter += 1
            session.execute(
                sa.update(db.TargetEntry)
                .where(db.TargetEntry.instance_uuid == instance_uuid)
                .values(held=False)
            )
            session.commit()
            return True

    def confirm(self, term, holder_uuid):
        """An operator confirms that the frozen holder of term, the instance
        holder_uuid, can't act, so that a takeover may end a freeze it never
        acknowledged"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if (
                lease.freeze_state != "ending"
                or term != lease.term
                or holder_uuid != lease.holder_uuid
            ):
                # Only while a freeze ends: never for one still in force, nor
                # for an instance that adopted the frozen lease since the
                # operator read it, which keeps the term
                session.rollback()
                return False
            lease.confirmed_term = term
            session.commit()
            return True

    def agree_settings(self, settings):
        """Store the cluster's HA settings, or check them against those
        stored: every instance must agree, or the waits don't hold"""
        name = "ha_settings"
        with self._session() as session:
            row = session.get(db.Secret, name, with_for_update=True)
            if row is None:
                session.add(db.Secret(name=name, value=settings))
                session.commit()
                return True
            # Only the settings both versions know are compared: a setting a
            # newer version removed mustn't keep its instances from leading.
            # A setting it added is stored by the first instance that has
            # it, and checked against from then on; stored settings it lacks
            # stay, for older instances during a rolling upgrade.
            stored, ours = json.loads(row.value), json.loads(settings)
            if any(stored[key] != ours[key] for key in stored.keys() & ours.keys()):
                session.commit()
                return False
            added = sorted(ours.keys() - stored.keys())
            if added:
                LOG.warning("Storing new [ha] settings: %s", ", ".join(added))
                row.value = json.dumps({**stored, **ours}, sort_keys=True)
            ignored = sorted(stored.keys() - ours.keys())
            if ignored:
                LOG.info("Ignoring stored [ha] settings: %s", ", ".join(ignored))
            session.commit()
            return True

    def adopt_frozen(self, node, instance_uuid, frozen_term):
        """A restarted manager on the frozen node takes the frozen lease
        over: it replaces the old instance's entry with its own, and clears
        its node's verified-stop mark, as a takeover does"""
        with self._session() as session:
            lease = self._row(session, lock=True)
            if (
                lease.freeze_state not in ("frozen", "ending")
                or lease.holder_node != node
                or lease.term != frozen_term
            ):
                session.rollback()
                return None
            session.execute(
                sa.delete(db.TargetEntry).where(
                    db.TargetEntry.node == node,
                    db.TargetEntry.instance_uuid == lease.holder_uuid,
                )
            )
            lease.holder_uuid = instance_uuid
            # A confirmation was about the previous instance, and a candidate's
            # view from before this must not let it take over.
            lease.confirmed_term = 0
            lease.counter += 1
            session.add(
                db.TargetEntry(
                    node=node, instance_uuid=instance_uuid, term=lease.term, held=True
                )
            )
            own = session.get(db.Node, node)
            if own is not None:
                own.verified_stop_term = -1
                own.verified_stop_uuid = ""
            session.commit()
            return lease.term
