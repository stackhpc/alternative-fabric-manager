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

"""Keep the fabric's partitions in line with Neutron

Every change bumps the generation of the recorded state, and creates a job
with the effect the change must have. One thread syncs OpenSM with the
recorded state: it renders the whole partitions file, without quarantined
objects, writes it, has OpenSM sweep it, and records what OpenSM swept and
which objects each part of it is for. A job completes only once a sweep of
its generation or later has its effect, OpenSM reported no pkey errors for
its ports, and a fabric check passed. networking-mlnx waits for a port's job
before it lets the port become ACTIVE, so a port is never reported as in its
partition before OpenSM has put it there.
"""

import contextlib
import datetime
import json
import logging
import os
import secrets
import threading
import time
import uuid

import sqlalchemy as sa

from alternative_fabric_manager import (
    allowed_sm,
    class_keys,
    db,
    effects,
    opensm_conf,
    partitions,
    port_keeper,
    sa_rate,
)
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager.drivers import (
    UNATTRIBUTED,
    PartitionsRejected,
    ReloadError,
)

LOG = logging.getLogger(__name__)

GENERATION = "generation"
APPLIES = "applies"
# Whether the per-port M_Key base was stored before the manager's current
# start: a start that stored it can't begin per-port keys
BASE_KEPT = "m_key_base_kept"
# HA: the highest term whose leader has written the secured profile's
# records, which every such write raises to its own term first (see
# specs/ha.md, The secured profile)
LEADER_FENCE = "leader_fence"
# Where the expected switches were recorded from: ibswitches, whose GUIDs
# are node GUIDs, or the prober, whose are the switches' port 0 GUIDs (see
# specs/zero-touch.md, Expected switches)
EXPECTED_SOURCE = "expected_switches_source"
IBSWITCHES = "ibswitches"
PROBER = "prober"
# The configuration hash's HMAC key, shared with HA mode's epochs (see
# ha/lease.py, HASH_KEY)
CONFIG_HASH_KEY = "config_hash_key"
APPLY_PREFIX = "# Apply "
RETRY_DELAY = 2.0
# _needs_sync()'s reason for restoring the last swept file
RESTORE = "the last swept partitions must be restored"
OPEN = (db.JobStatus.PENDING, db.JobStatus.RUNNING)


class NotFound(LookupError):
    pass


class StaleLeader(ReloadError):
    """A later term's leader has written the secured profile's records: this
    instance no longer leads, and mustn't write them"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value):
    # SQLite returns naive datetimes.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _body(content):
    """A partitions file without the line that changes on every apply"""
    return "".join(
        line
        for line in (content or "").splitlines(keepends=True)
        if not line.startswith(APPLY_PREFIX)
    )


def _increment(session, name):
    """Increment a counter in place, which locks its row until the
    transaction ends, so that concurrent instances never lose an increment,
    as a read and a write would under Galera; return its new value"""
    for _attempt in range(2):
        result = session.execute(
            sa.update(db.State)
            .where(db.State.name == name)
            .values(value=db.State.value + 1)
        )
        if result.rowcount == 1:
            return session.scalar(
                sa.select(db.State.value).where(db.State.name == name)
            )
        try:
            with session.begin_nested():
                session.add(db.State(name=name, value=1))
            return 1
        except sa.exc.IntegrityError:
            # Another instance created it first; increment it.
            continue
    raise RuntimeError(f"Can't increment {name}")


# Keys a start stores, a start before any port is keyed with them, so that
# a backup taken between holds them
LATE_KEYS = ("key_mgr_seed", "m_key_base")


def _stored_key(session, name, initial=None):
    """One of the fabric's shared keys, stored as initial, or generated, if
    it's missing"""
    secret = session.get(db.Secret, name)
    if secret is None:
        secret = db.Secret(name=name, value=initial or opensm_conf.new_key())
        session.add(secret)
    return secret.value


def _canonical_seed(value):
    """The seed as the manager stores and writes it, or None if it isn't one"""
    try:
        seed = int(value, 0)
    except (TypeError, ValueError):
        return None
    return f"0x{seed:016x}" if 0 < seed < 2**64 else None


class Manager:
    def __init__(self, config, sessionmaker, driver, template=None):
        self.config = config
        self.sessionmaker = sessionmaker
        self.driver = driver
        self.template = template or partitions.load_template(config.partitions_template)
        # The pkeys that no VLAN network may take: the default partition's,
        # the flat networks', and those of the template's static partitions
        self.reserved_pkeys = partitions.pkeys(
            partitions.render(config, [], 0, self.template)
        ) | {partitions.DEFAULT_PKEY, config.flat_pkey}
        # One SQLite connection is shared by every thread, so all database
        # access is serialised.
        self._lock = threading.Lock()
        # Whether keys() may generate the keys that a start must store before
        # anything uses them (LATE_KEYS): only a start, never a command run
        # beside the manager, or a command could store one that no backup
        # holds, and the next start would take it as stored by an earlier
        # one (specs/secured-profile.md, The seed, and A new base key).
        self._stores_late_keys = False
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # The partitions file last written and swept
        self._written = None
        # In HA mode, called when OpenSM stops, instead of starting it again
        self.on_opensm_stopped = None
        # The leader's term, recorded with each sweep (see specs/ha.md)
        self.term = 0
        self._iteration_started = None
        # Consecutive fabric checks that got no answer, and applies that
        # failed to write, signal or sweep: the coordinator steps down on
        # three of either (see Stepping down)
        self.unanswered_checks = 0
        self.failed_applies = 0
        # Consecutive checks that found expected switches missing
        self.missing_checks = 0
        # How many settled fabric checks in a row have seen each switch, by
        # its GUID: kept in memory, and counted afresh for each OpenSM, so
        # that a switch is recorded as expected only once the fabric has
        # been stable for [opensm] confirm_checks (specs/zero-touch.md)
        self._seen_checks = {}
        # Whether the last fabric check saw another master, or OpenSM's own
        # not master: no sweep counts, nor is one made, until a check passes
        self._contested_fabric = False
        # A generation whose render or apply failed in a way that can't be
        # traced to an object, so that it isn't tried again until the
        # recorded state changes
        self._failed_generation = None
        # How many sweeps this manager has recorded: a job's per-port M_Key
        # evidence counts only if it was taken after the last of them
        self._sweeps = 0
        # Per-port M_Keys, which act only while the m_key_per_port group is
        # named or rows exist, in single node with the managed driver
        self.port_keeper = port_keeper.PortKeeper(self)
        # Allowed SMs, which act only while the list is in effect, in single
        # node with the managed driver
        self.allowed_sms = allowed_sm.AllowedSMs(self)
        # The allowed_sm_guids value of the start under way
        self._allowed_list = None
        # Class keys: the seed rule and the turn-off guard
        self.class_keys = class_keys.ClassKeys(self)
        # The SA request rate limit's drops, logged
        self.sa_rate = sa_rate.SARate(self)
        # This configuration's hash, worked out once it's needed
        self._config_hash = None
        # In HA mode, why the leader should step down so that a takeover's
        # start applies the per-port M_Key target, which the coordinator
        # reads and clears (specs/zero-touch.md, Applying)
        self.apply_requested = None

    # The leader fence

    @contextlib.contextmanager
    def fenced_session(self):
        """A session for the secured profile's records: in HA mode, while
        this instance leads, it first raises the leader fence to its term,
        and raises StaleLeader if a later term's leader has (see specs/ha.md,
        The secured profile). Elsewhere, as for an operator's command, a
        plain session."""
        with self._lock, self.sessionmaker() as session:
            if self.config.ha and self.term:
                self._raise_fence(session)
            yield session

    def _claim_fence(self):
        """Raise the leader fence to this term, and commit it"""
        for attempt in range(3):
            try:
                with self.fenced_session() as session:
                    session.commit()
                return
            except (sa.exc.IntegrityError, sa.exc.OperationalError):
                # Two first raises racing to insert the row, or a Galera
                # certification conflict: the next attempt sees the winner's.
                if attempt == 2:
                    raise

    def _raise_fence(self, session):
        # A new nonce every time, so that the row always changes: Galera
        # certifies a transaction against another only on rows they both
        # changed, so a deposed leader's transaction, which reads the fence
        # at its own term, conflicts with the successor's raise rather than
        # slipping past it.
        nonce = uuid.uuid4().hex
        result = session.execute(
            sa.update(db.State)
            .where(db.State.name == LEADER_FENCE, db.State.value <= self.term)
            .values(value=self.term, text=nonce)
        )
        if result.rowcount == 1:
            return
        if session.get(db.State, LEADER_FENCE) is None:
            # The first fenced write ever; a racing first write fails its
            # commit, and is retried as any failed write is.
            session.add(db.State(name=LEADER_FENCE, value=self.term, text=nonce))
            session.flush()
            return
        session.rollback()
        raise StaleLeader(
            f"A later term's leader has written the records; term {self.term} "
            "no longer leads"
        )

    # Requests

    @staticmethod
    def _bump(session):
        """Bump the generation, first in a request's transaction"""
        return _increment(session, GENERATION)

    @staticmethod
    def _release(session, kind, object_id):
        """A request for an object releases it from quarantine"""
        session.execute(
            sa.delete(db.Quarantine).where(
                db.Quarantine.kind == kind, db.Quarantine.object_id == object_id
            )
        )

    def _commit_job(
        self, session, generation, operation, object_id, effect, waiting=None
    ):
        """Create a request's job and commit; a request that must wait is
        recorded as deferred, waiting = (kind, values, reason)"""
        job = db.Job(
            operation=operation,
            object_id=object_id,
            generation=generation,
            effect=effects.dumps(effect),
            deadline=_now() + datetime.timedelta(seconds=self.config.job_timeout),
        )
        if waiting is not None:
            kind, values, reason = waiting
            job.error = f"waiting: {reason}"
            LOG.info("%s of %s %s waits: %s", operation, kind, object_id, reason)
        session.add(job)
        session.flush()
        if waiting is not None:
            session.add(
                db.Deferred(
                    kind=kind,
                    object_id=object_id,
                    job_id=job.id,
                    data=json.dumps(values),
                )
            )
        session.commit()
        self._wakeup.set()
        return job.id

    # Late requests (see specs/ha.md)

    @staticmethod
    def revision_of(data):
        """Neutron's revision_number in a request's dict, or None"""
        if not isinstance(data, dict):
            return None
        value = data.get("revision_number")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _check_late(self, session, kind, object_id, revision):
        """After the generation bump: refuse a late create or update with
        InvalidRequest, return the job of an open deferred request that this
        one resends, or None to go ahead"""
        tombstone = session.get(db.Tombstone, (kind, object_id))
        if tombstone is not None:
            if kind == "network":
                # A network is never created again under its ID.
                raise partitions.InvalidRequest(
                    f"Network {object_id} was deleted: a late request"
                )
            if revision is not None and not (
                (tombstone.set_revision is None or revision > tombstone.set_revision)
                and (
                    tombstone.deleted_revision is None
                    or revision >= tombstone.deleted_revision
                )
            ):
                raise partitions.InvalidRequest(
                    f"Port {object_id} was deleted, and revision {revision} "
                    "is from before: a late request"
                )
        if revision is None:
            return None
        model = db.Network if kind == "network" else db.Port
        current = session.get(model, object_id)
        if (
            current is not None
            and current.revision is not None
            and revision < current.revision
        ):
            raise partitions.InvalidRequest(
                f"The {kind} {object_id} is at revision {current.revision}, "
                f"past {revision}: a late request"
            )
        waiting = self._waiting(session, kind, object_id)
        if waiting is not None:
            waited = json.loads(waiting.data).get("revision")
            if waited is not None:
                if revision < waited:
                    raise partitions.InvalidRequest(
                        f"The {kind} {object_id} waits at revision {waited}, "
                        f"past {revision}: a late request"
                    )
                if revision == waited:
                    # A resend: the waiting request's job is the one
                    # networking-mlnx polls, so keep it.
                    return waiting.job_id
        return None

    def _bury(self, session, kind, object_id, revision, live):
        """Leave or raise a deleted object's tombstone, before its deferred
        request is superseded"""
        waiting = self._waiting(session, kind, object_id)
        tombstone = session.get(db.Tombstone, (kind, object_id))
        set_revisions = [
            r
            for r in (
                live.revision if live is not None else None,
                json.loads(waiting.data).get("revision") if waiting else None,
                tombstone.set_revision if tombstone is not None else None,
            )
            if r is not None
        ]
        deleted = [
            r
            for r in (
                revision,
                tombstone.deleted_revision if tombstone is not None else None,
            )
            if r is not None
        ]
        if tombstone is None:
            tombstone = db.Tombstone(kind=kind, object_id=object_id)
            session.add(tombstone)
        tombstone.set_revision = max(set_revisions, default=None)
        tombstone.deleted_revision = max(deleted, default=None)
        tombstone.deleted_at = _now()
        if (
            kind == "port"
            and live is not None
            and live.revision is not None
            and revision is not None
            and revision < live.revision
        ):
            # Unusual, so worth a warning; at the port's revision is the
            # ordinary case, which a late deletion after a host move can't
            # be told from (see Late requests). Applied either way.
            LOG.warning(
                "Deleting %s %s at revision %d, below its %d: if it was "
                "late, Neutron still has it",
                kind,
                object_id,
                revision,
                live.revision,
            )

    def forget_tombstones(self, days=7):
        """Drop tombstones older than their keeping time"""
        cutoff = _now() - datetime.timedelta(days=days)
        with self._lock, self.sessionmaker() as session:
            session.execute(
                sa.delete(db.Tombstone).where(db.Tombstone.deleted_at < cutoff)
            )
            session.commit()

    def forget_revisions(self):
        """After Neutron's database is restored: drop every tombstone and
        recorded revision, and return how many tombstones there were"""
        with self._lock, self.sessionmaker() as session:
            count = session.scalar(sa.select(sa.func.count()).select_from(db.Tombstone))
            session.execute(sa.delete(db.Tombstone))
            session.execute(sa.update(db.Network).values(revision=None))
            session.execute(sa.update(db.Port).values(revision=None))
            for row in session.scalars(sa.select(db.Deferred)):
                values = json.loads(row.data)
                values.pop("revision", None)
                row.data = json.dumps(values)
            session.commit()
            return count

    @staticmethod
    def _supersede(session, kind, object_id, reason="Superseded by a later request"):
        """Drop an object's deferred request, failing its job"""
        row = session.get(db.Deferred, (kind, object_id))
        if row is None:
            return
        job = session.get(db.Job, row.job_id)
        if job is not None and job.status in OPEN:
            job.status = db.JobStatus.FAILED
            job.error = reason
            job.finished_at = _now()
        session.delete(row)
        session.flush()

    @staticmethod
    def _waiting(session, kind, object_id):
        """An object's deferred request, if its job is still open"""
        row = session.get(db.Deferred, (kind, object_id))
        if row is None:
            return None
        job = session.get(db.Job, row.job_id)
        return row if job is not None and job.status in OPEN else None

    @staticmethod
    def _network_blocked(session, network_id, values):
        """Why a network's request must wait, if it must"""
        pkey = values["pkey"]
        if pkey is None:
            return None
        other = session.scalar(
            sa.select(db.Network).where(
                db.Network.pkey == pkey, db.Network.id != network_id
            )
        )
        if other is None:
            return None
        return f"partition key 0x{pkey:04x} is still network {other.id}'s"

    @staticmethod
    def _set_network(session, network_id, values):
        network = session.get(db.Network, network_id)
        if network is None:
            network = db.Network(id=network_id)
            session.add(network)
        network.pkey = values["pkey"]
        network.name = values["name"]
        if values.get("revision") is not None:
            network.revision = max(network.revision or 0, values["revision"])
        session.flush()

    @staticmethod
    def _port_blocked(session, port_id, values):
        """Why a port's request must wait, if it must"""
        network_id = values["network_id"]
        if session.get(db.Network, network_id) is None:
            return f"network {network_id} is waiting"
        quarantined = session.get(db.Quarantine, ("network", network_id))
        if quarantined is not None:
            # It would be left out of the file with its network. A network's
            # next request releases it, and networking-mlnx resends each
            # failed object on its own, so its ports may come first: wait,
            # since a 400 would be final.
            return f"network {network_id} is quarantined: {quarantined.reason}"
        other = session.scalar(
            sa.select(db.Port).where(
                db.Port.guid == values["guid"], db.Port.id != port_id
            )
        )
        if other is None:
            return None
        # A GUID can be in only one network's partition. networking-mlnx
        # deletes a GUID's old port before it sends a new one, but the
        # deletion can arrive later; if it never does, the job fails.
        return f"GUID 0x{values['guid']} is still port {other.id}'s"

    @staticmethod
    def _set_port(session, port_id, values):
        port = session.get(db.Port, port_id)
        if port is None:
            port = db.Port(id=port_id)
            session.add(port)
        port.network_id = values["network_id"]
        port.guid = values["guid"]
        port.host = values["host"]
        if values.get("revision") is not None:
            port.revision = max(port.revision or 0, values["revision"])
        # Set, so no longer deleted (see Late requests)
        session.execute(
            sa.delete(db.Tombstone).where(
                db.Tombstone.kind == "port", db.Tombstone.object_id == port_id
            )
        )
        session.flush()

    def put_network(self, network_id, data):
        """Create or update a network's partition"""
        partitions.check_id(network_id)
        pkey = partitions.pkey_from_network(dict(data, id=network_id))
        if pkey in self.reserved_pkeys:
            raise partitions.InvalidRequest(
                f"Partition key 0x{pkey:04x} of network {network_id} is reserved"
            )
        values = {
            "pkey": pkey,
            "name": str(data.get("name") or "")[:255],
            "revision": self.revision_of(data),
        }
        with self._lock, self.sessionmaker() as session:
            db.check_serving(session)
            generation = self._bump(session)
            resent = self._check_late(
                session, "network", network_id, values["revision"]
            )
            if resent is not None:
                return resent
            self._supersede(session, "network", network_id)
            self._release(session, "network", network_id)
            effect = effects.network_present(network_id, pkey)
            reason = self._network_blocked(session, network_id, values)
            if reason:
                return self._commit_job(
                    session,
                    generation,
                    "put_network",
                    network_id,
                    effect,
                    ("network", values, reason),
                )
            self._set_network(session, network_id, values)
            return self._commit_job(
                session, generation, "put_network", network_id, effect
            )

    def delete_network(self, network_id, data=None):
        partitions.check_id(network_id)
        with self._lock, self.sessionmaker() as session:
            db.check_serving(session)
            generation = self._bump(session)
            self._bury(
                session,
                "network",
                network_id,
                self.revision_of(data),
                session.get(db.Network, network_id),
            )
            self._supersede(
                session, "network", network_id, "Superseded by its deletion"
            )
            for row in list(session.scalars(sa.select(db.Deferred))):
                if (
                    row.kind == "port"
                    and json.loads(row.data)["network_id"] == network_id
                ):
                    self._supersede(
                        session,
                        "port",
                        row.object_id,
                        f"Network {network_id} was deleted",
                    )
            ports = list(
                session.scalars(
                    sa.select(db.Port).where(db.Port.network_id == network_id)
                )
            )
            network = session.get(db.Network, network_id)
            if network is not None:
                session.execute(
                    sa.delete(db.Port).where(db.Port.network_id == network_id)
                )
                session.delete(network)
            self._release(session, "network", network_id)
            for port in ports:
                self._release(session, "port", port.id)
            # Even when the network is gone, answer with a job rather than
            # 404, which networking-mlnx takes as done: a DELETE is sent again
            # when its first job failed, and OpenSM may still have the
            # network's partition until a sweep shows otherwise.
            return self._commit_job(
                session,
                generation,
                "delete_network",
                network_id,
                effects.network_absent(
                    network_id,
                    [port.id for port in ports],
                    [port.guid for port in ports],
                ),
            )

    def put_port(self, port_id, data):
        """Make a port's GUID a member of its network's partition"""
        partitions.check_id(port_id)
        guid = partitions.guid_from_port(data)
        network_id = data.get("network_id")
        values = {
            "network_id": network_id,
            "guid": f"{guid:016x}",
            "host": str(data.get("binding:host_id") or "")[:255],
            "revision": self.revision_of(data),
        }
        with self._lock, self.sessionmaker() as session:
            db.check_serving(session)
            # First, so that every read below sees the requests serialised
            # before this one (see Shared state, Late requests)
            generation = self._bump(session)
            resent = self._check_late(session, "port", port_id, values["revision"])
            if resent is not None:
                return resent
            network = session.get(db.Network, network_id)
            waiting_network = self._waiting(session, "network", network_id)
            if network is None and waiting_network is None:
                # The network was never sent, or its request failed. Fail
                # rather than let the port become ACTIVE outside its
                # partition.
                raise partitions.InvalidRequest(
                    f"Port {port_id} is on network {network_id}, which has no partition"
                )
            if network is not None:
                pkey = network.pkey
            else:
                pkey = json.loads(waiting_network.data)["pkey"]
            self._supersede(session, "port", port_id)
            self._release(session, "port", port_id)
            effect = effects.port_present(
                port_id, self.config.flat_pkey if pkey is None else pkey, values["guid"]
            )
            reason = self._port_blocked(session, port_id, values)
            if reason:
                return self._commit_job(
                    session,
                    generation,
                    "put_port",
                    port_id,
                    effect,
                    ("port", values, reason),
                )
            self._set_port(session, port_id, values)
            return self._commit_job(session, generation, "put_port", port_id, effect)

    def delete_port(self, port_id, data=None):
        partitions.check_id(port_id)
        with self._lock, self.sessionmaker() as session:
            db.check_serving(session)
            generation = self._bump(session)
            self._bury(
                session,
                "port",
                port_id,
                self.revision_of(data),
                session.get(db.Port, port_id),
            )
            self._supersede(session, "port", port_id, "Superseded by its deletion")
            port = session.get(db.Port, port_id)
            guid = port.guid if port is not None else None
            if port is not None:
                session.delete(port)
            self._release(session, "port", port_id)
            # As for networks: the GUID may still be in the partition until a
            # sweep shows otherwise.
            return self._commit_job(
                session,
                generation,
                "delete_port",
                port_id,
                effects.port_absent(port_id, guid),
            )

    def retry_deferred(self):
        """Apply the deferred requests that no longer have to wait, oldest
        first, until none can be; drop those whose jobs have ended, and the
        ports that waited on a dropped network"""
        with self._lock, self.sessionmaker() as session:
            progress = True
            while progress:
                progress = False
                rows = list(
                    session.scalars(sa.select(db.Deferred).order_by(db.Deferred.job_id))
                )
                for row in rows:
                    if sa.inspect(row).deleted or sa.inspect(row).detached:
                        # Dropped with its network earlier in this pass
                        continue
                    if self._retry(session, row):
                        progress = True
            session.commit()

    def _retry(self, session, row):
        """Apply or drop one deferred request; return whether it changed"""
        job = session.get(db.Job, row.job_id)
        if job is not None:
            self._time_out(job)
        if job is None or job.status not in OPEN:
            session.delete(row)
            # Its ports wait for it only if the network doesn't exist: an
            # existing network's ports aren't held up by its update.
            if row.kind == "network" and session.get(db.Network, row.object_id) is None:
                for other in list(session.scalars(sa.select(db.Deferred))):
                    if (
                        other.kind == "port"
                        and json.loads(other.data)["network_id"] == row.object_id
                    ):
                        self._supersede(
                            session,
                            "port",
                            other.object_id,
                            f"Network {row.object_id}'s request ended unapplied",
                        )
            session.flush()
            return True
        values = json.loads(row.data)
        if row.kind == "network":
            blocked = self._network_blocked(session, row.object_id, values)
        else:
            blocked = self._port_blocked(session, row.object_id, values)
        if blocked:
            return False
        LOG.info("Applying the waiting %s %s", job.operation, row.object_id)
        job.generation = self._bump(session)
        job.error = ""
        # It keeps its deadline: a request that waited too long fails, and
        # networking-mlnx sends it again, rather than outlast Nova's wait.
        # Its change releases it from quarantine, as a request does.
        self._release(session, row.kind, row.object_id)
        if row.kind == "network":
            self._set_network(session, row.object_id, values)
        else:
            self._set_port(session, row.object_id, values)
            # Its network's pkey is what it is now, not when the port waited.
            pkey = session.get(db.Network, values["network_id"]).pkey
            job.effect = effects.dumps(
                effects.port_present(
                    row.object_id,
                    self.config.flat_pkey if pkey is None else pkey,
                    values["guid"],
                )
            )
        session.delete(row)
        session.flush()
        return True

    def get_job(self, job_id):
        with self._lock, self.sessionmaker() as session:
            job = session.get(db.Job, job_id)
            if job is None:
                raise NotFound(f"Job {job_id}")
            if job.status in OPEN:
                self._evaluate(session, [job])
                session.commit()
            return job

    @staticmethod
    def _time_out(job):
        """Fail a job past its deadline; return whether it was"""
        if job.status not in OPEN or job.deadline is None:
            return False
        if _now() <= _aware(job.deadline):
            return False
        job.status = db.JobStatus.FAILED
        if job.error.startswith("waiting: "):
            # A deferred request, whose wait never ended
            job.error = "Timed out " + job.error
        else:
            job.error = job.error or "Timed out waiting for OpenSM to apply it"
        job.finished_at = _now()
        return True

    # Keys and OpenSM's options

    def keys(self):
        """Return the fabric's shared OpenSM keys, generating any missing

        A missing seed is stored as the operator's override, if there is one:
        ports may already be keyed with it, set by hand before the manager
        kept a seed (see specs/secured-profile.md, The seed). The seed and
        the per-port M_Key base are generated only by a start; elsewhere a
        missing one is left out.
        """
        initial = {
            "key_mgr_seed": _canonical_seed(self.config.opensm_conf.get("key_mgr_seed"))
        }
        for attempt in range(2):
            with self._lock, self.sessionmaker() as session:
                try:
                    keys = {}
                    for name in opensm_conf.KEYS:
                        if name in LATE_KEYS and not self._stores_late_keys:
                            secret = session.get(db.Secret, name)
                            if secret is not None:
                                keys[name] = secret.value
                            continue
                        keys[name] = _stored_key(session, name, initial.get(name))
                    session.commit()
                except sa.exc.IntegrityError:
                    # Another instance stored a key first, which surfaces at
                    # a flush or the commit; read the stored keys back.
                    session.rollback()
                    if attempt:
                        raise
                    continue
                return keys

    def _keep_seed(self):
        """Store the class keys' seed if it's missing, and refuse an override
        that isn't the stored seed

        The seed is stored whatever the groups, so that backups hold it before
        any port is keyed with it, and it can't be replaced: ports keyed with
        another seed would have keys no SM can reproduce (see
        specs/secured-profile.md, The seed). keys() stores every missing key,
        so the per-port M_Key base is stored here too, for the same reason.
        """
        seed = self.keys()["key_mgr_seed"]
        override = self.config.opensm_conf.get("key_mgr_seed")
        if override is not None and _canonical_seed(override) != seed:
            # Neither value is logged: they're secrets.
            raise config_mod.ConfigError(
                "[opensm_conf] key_mgr_seed differs from the seed in the "
                "database, which can't be replaced. Ports keyed with the stored "
                "seed need it: remove the option. Ports keyed with the option's "
                "seed need the database that holds it: restore that database"
            )

    def _base_stored(self):
        """Whether the per-port M_Key base is stored already"""
        with self._lock, self.sessionmaker() as session:
            return session.get(db.Secret, "m_key_base") is not None

    def _seed_stored(self):
        """Whether the class keys' seed is stored already"""
        with self._lock, self.sessionmaker() as session:
            return session.get(db.Secret, "key_mgr_seed") is not None

    def _record_base_kept(self, kept):
        """Record whether this start found the per-port M_Key base stored

        A start that stored the base can't begin per-port keys: the next
        start, after a backup that holds it, can (see
        specs/secured-profile.md, Per-port M_Keys). --confirm-fabric runs
        beside the running manager, so the start records it for that command
        to read. Every start overwrites it.
        """
        for attempt in range(2):
            with self.fenced_session() as session:
                try:
                    row = session.get(db.State, BASE_KEPT)
                    if row is None:
                        session.add(db.State(name=BASE_KEPT, value=int(kept)))
                    else:
                        row.value = int(kept)
                    session.commit()
                except sa.exc.IntegrityError:
                    # Another process created the row first; overwrite it.
                    session.rollback()
                    if attempt:
                        raise
                    continue
                return

    def base_kept(self):
        """Whether the running manager's start found the per-port M_Key base
        already stored, so that it may begin per-port keys; False if no start
        has recorded it"""
        with self._lock, self.sessionmaker() as session:
            row = session.get(db.State, BASE_KEPT)
            return bool(row is not None and row.value)

    def config_hash(self):
        """This configuration's hash, as HA mode's epochs use it: an HMAC
        keyed by a secret in the database, generated once, so that a hash
        that's recorded, as a failed per-port M_Key turn-on's is, doesn't let
        anyone who sees it test guesses of a key set in [opensm_conf]. The
        same in single node, so that a single-node database moved to HA mode
        keeps its record's meaning."""
        if self._config_hash is not None:
            return self._config_hash
        for attempt in range(2):
            with self._lock, self.sessionmaker() as session:
                row = session.get(db.Secret, CONFIG_HASH_KEY)
                if row is None:
                    row = db.Secret(name=CONFIG_HASH_KEY, value=secrets.token_hex(32))
                    session.add(row)
                    try:
                        session.commit()
                    except sa.exc.IntegrityError:
                        # Another process stored one first: read it back.
                        session.rollback()
                        if attempt:
                            raise
                        continue
                key = row.value
            self._config_hash = config_mod.config_hash(self.config, key)
            return self._config_hash

    def request_apply(self, why):
        """Restart OpenSM so that its start applies the per-port M_Key
        target: in single node, stop it, and the applier's next iteration
        starts it again, as after OpenSM stops; in HA mode, ask the
        coordinator to step down and release the lease, so that a
        takeover's start, possibly this node's own, applies it
        (specs/zero-touch.md, Applying)"""
        if self.config.ha:
            self.apply_requested = why
            return
        LOG.warning("Restarting OpenSM: %s", why)
        self.driver.stop()
        self._written = None
        self._wakeup.set()

    def _key(self, name):
        """The key OpenSM uses, if any: an override, or a hardened one"""
        if name in self.config.opensm_conf:
            return self.config.opensm_conf[name]
        if name in self.config.hardening_groups:
            return self.keys()[name]
        return None

    def _fabric(self, switches=None):
        """The fabric, with switches the prober found if it was given them:
        with per-port keys, ibswitches -y <m_key> can't reach a re-keyed
        port"""
        if switches is None:
            return self.driver.fabric(
                m_key=self._key("m_key"), sa_key=self._key("sa_key")
            )
        return self.driver.fabric(
            m_key=self._key("m_key"), sa_key=self._key("sa_key"), switches=switches
        )

    def _write_opensm_conf(self, port_keys=False):
        """Write opensm.conf; port_keys is whether OpenSM starts with
        per-port M_Keys on, which a start decides once it has verified the
        ports"""
        if self.config.driver != "managed":
            return
        groups = self.class_keys.groups(self.config.hardening_groups)
        if port_keys:
            # On while the group is no longer named, at a turn-off whose
            # verify failed: OpenSM then stays on the previous target.
            groups = groups | {port_keeper.GROUP}
        overrides = dict(self.config.opensm_conf)
        if "key_mgr_seed" in overrides:
            # The stored spelling, which _keep_seed checked the override
            # against, so that OpenSM can't parse it differently
            overrides["key_mgr_seed"] = self.keys()["key_mgr_seed"]
        generated = {}
        if self.allowed_sms.active():
            if self._allowed_list is None:
                raise ReloadError("No allowed_sm_guids list was worked out")
            generated[allowed_sm.OPTION] = self._allowed_list
            if allowed_sm.OPTION in overrides:
                # The manager's spelling of the override, which the start's
                # checks passed
                overrides[allowed_sm.OPTION] = self._allowed_list
        content = opensm_conf.render(
            overrides,
            hardening=groups,
            keys=(
                self.keys()
                if groups
                & {"m_key", "sm_key", "sa_key", port_keeper.GROUP, class_keys.GROUP}
                else None
            ),
            key_lease_period=self.config.key_lease_period,
            port_keys=port_keys,
            generated=generated,
        )
        # The keys are secrets.
        partitions.write_atomically(self.config.opensm_conf_file, content, 0o600)
        # Which classes OpenSM may now key, for the turn-off guard
        self.class_keys.record(groups, overrides)

    def _key_leases(self):
        """The key leases opensm.conf gets, as "name N s" or "name permanent",
        for the start log: what's written, not what's asked for"""
        if self.config.driver != "managed":
            return ""
        leases = {
            name: str(self.config.key_lease_period)
            for group in self.class_keys.groups(self.config.hardening_groups)
            for name in opensm_conf.HARDENING[group]
            if opensm_conf.is_key_lease(name)
        }
        leases.update(
            (name, value)
            for name, value in self.config.opensm_conf.items()
            if opensm_conf.is_key_lease(name)
        )
        return ", ".join(
            f"{name} {'permanent' if value.strip() in ('0', '0x0') else value + ' s'}"
            for name, value in sorted(leases.items())
        )

    # Syncing OpenSM

    def start(self):
        """Write the current state, start OpenSM, then keep it in sync"""
        # Before anything is written or started, so that a refused seed
        # leaves OpenSM as it was. Whether the base was stored is read first,
        # since _keep_seed stores every missing key, the base too.
        if self.config.ha and self.term:
            # The leader fence, raised and committed before any record is
            # read: a fenced session that only reads rolls its raise back, so
            # Galera would certify nothing against it, and a deposed
            # leader's write could land between these reads and the first
            # write (specs/ha.md, The secured profile).
            self._claim_fence()
        base_kept = self._base_stored()
        seed_kept = self._seed_stored()
        self._stores_late_keys = True
        self.port_keeper.refuse_start()
        self.class_keys.refuse_start(seed_kept)
        self._keep_seed()
        # In HA mode, behind the leader fence, which this raises first.
        self._record_base_kept(base_kept)
        self._stop.clear()
        # Counted afresh for each OpenSM this manager starts
        self.unanswered_checks = 0
        self.failed_applies = 0
        self.missing_checks = 0
        self._seen_checks = {}
        self.apply_requested = None
        # A new leader inherits a contested check: its first sweep then
        # doesn't count until a check passes (see Job completion).
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            self._contested_fabric = bool(applied and applied.contested)
        LOG.info(
            "OpenSM hardening: %s; key leases written: %s",
            ", ".join(sorted(self.config.hardening_groups)) or "none",
            self._key_leases() or "none",
        )
        overridden = sorted(
            name for name in self.config.opensm_conf if opensm_conf.is_key_lease(name)
        )
        if overridden and self.config.driver == "managed":
            # Easy to miss when key_lease_period is meant to cover every key
            LOG.warning(
                "[opensm_conf] sets %s, which wins over [opensm] key_lease_period",
                ", ".join(overridden),
            )
        try:
            self._start_opensm()
        except ReloadError as e:
            if not (self.port_keeper.active() or self.allowed_sms.active()):
                raise
            # A start with per-port M_Keys can fail for a reason the next
            # one may not have, such as a prober that couldn't run, or a
            # fabric part-way through a turn-on, and so can one with allowed
            # SMs, whose port probe may fail while the image is pulled: the
            # applier's check starts OpenSM again at every interval, as
            # after OpenSM stops.
            LOG.error("Failed to start OpenSM, retrying at every check: %s", e)
        self._thread = threading.Thread(target=self._run, name="applier", daemon=True)
        self._thread.start()
        self._wakeup.set()

    def stop(self):
        self.halt()
        self.driver.stop()

    def halt(self):
        """Stop the applier, leaving OpenSM to whoever stops it: in HA mode,
        the coordinator, which verifies that it stopped"""
        self._stop.set()
        self._wakeup.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None

    def iteration_age(self):
        """How long the applier's current iteration has run, or 0 between
        iterations: the coordinator's liveness check (see specs/ha.md)"""
        started = self._iteration_started
        return 0.0 if started is None else time.monotonic() - started

    def _run(self):
        while not self._stop.is_set():
            self._wakeup.wait(self.config.check_interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            self._iteration_started = time.monotonic()
            try:
                self._check_opensm()
                self.reconcile()
            except Exception:
                LOG.exception("Failed to sync OpenSM")
                time.sleep(RETRY_DELAY)
                self._wakeup.set()
            finally:
                self._iteration_started = None

    def _start_opensm(self):
        """Write the current state's files and start OpenSM with them

        If the current state can't be rendered, OpenSM starts with the last
        partitions file, which passed the checks when it was written, rather
        than not at all, and the next sync deals with the state.

        With allowed SMs, this host's ports are read first, and a list that
        would lock OpenSM out refuses the start with ReloadError.
        """
        self._allowed_list = None
        if self.allowed_sms.active():
            self._allowed_list = self.allowed_sms.prepare()
        self.class_keys.reset()
        self.sa_rate.reset()
        self._write_opensm_conf()
        # What OpenSM starts with is recorded as swept only if the driver saw
        # OpenSM's first sweep read it; a driver may not start OpenSM at all.
        self._written = None
        while True:
            try:
                content, generation, attribution = self._render()
            except partitions.InvalidPartitions as e:
                if not os.path.exists(self.config.partitions_file):
                    raise
                LOG.error(
                    "Starting OpenSM with the last partitions file, since the "
                    "current state can't be rendered: %s",
                    e,
                )
                self._driver_start()
                return
            stamped = self._stamp(content)
            partitions.write_atomically(self.config.partitions_file, stamped)
            try:
                report = self._driver_start()
            except PartitionsRejected as e:
                if self._quarantine_line(e, attribution, generation):
                    continue
                body = self._last_swept()
                if not body:
                    raise
                self._unattributable(str(e), generation)
                LOG.error("Starting OpenSM with the last swept partitions")
                partitions.write_atomically(
                    self.config.partitions_file, self._stamp(_body(body))
                )
                self._driver_start()
                return
            # A start that the driver saw sweep is a sweep, with its pkey
            # errors; otherwise the next sync sweeps and records it.
            # Not while another SM may have acted: a sweep then counts only
            # after a passing check, so the next sync sweeps again.
            if report is not None and not self._contested_fabric:
                self._record(stamped, generation, attribution, report)
            return

    def _driver_start(self):
        """Start OpenSM: with per-port M_Keys, through the steps before and
        after each start (specs/secured-profile.md, Per-port M_Keys)"""
        if not self.port_keeper.active():
            return self.driver.start()
        return self.port_keeper.start(self.driver.start)

    def _check_opensm(self):
        """Start OpenSM again if it has stopped, with the current state, or,
        in HA mode, tell the coordinator, which steps down"""
        if self.driver.running():
            return
        if self.on_opensm_stopped is not None:
            self._written = None
            self.on_opensm_stopped()
            return
        LOG.error("OpenSM isn't running; starting it again")
        try:
            self._start_opensm()
        except (OSError, ReloadError, partitions.InvalidPartitions) as e:
            LOG.error("Failed to start OpenSM: %s", e)

    def _state(self, session):
        """Return the generation, and the networks and ports to render"""
        state = session.get(db.State, GENERATION)
        generation = state.value if state is not None else 0
        quarantined = {
            (q.kind, q.object_id) for q in session.scalars(sa.select(db.Quarantine))
        }
        networks = [
            n
            for n in session.scalars(sa.select(db.Network))
            if ("network", n.id) not in quarantined
        ]
        kept = {n.id for n in networks}
        ports = [
            p
            for p in session.scalars(sa.select(db.Port))
            if p.network_id in kept and ("port", p.id) not in quarantined
        ]
        return generation, networks, ports

    def _render_state(self, generation, networks, ports):
        """Render and check networks and ports, and attribute the result"""
        members = {}
        for port in ports:
            members.setdefault(port.network_id, []).append(int(port.guid, 16))
        pairs = [(network, members.get(network.id, [])) for network in networks]
        content = partitions.render(self.config, pairs, generation, self.template)
        partitions.validate(
            content,
            {n.pkey: set(guids) for n, guids in pairs if n.pkey is not None},
            {guid for n, guids in pairs if n.pkey is None for guid in guids},
            flat_pkey=self.config.flat_pkey,
            shared_default=self.config.shared_default_partition,
        )
        by_pkey = {n.pkey: n.id for n in networks if n.pkey is not None}
        if any(n.pkey is None for n in networks):
            by_pkey[self.config.flat_pkey] = "flat"
        attribution = partitions.attribute(
            content, by_pkey, {int(p.guid, 16): p.id for p in ports}
        )
        return content, attribution

    def _render(self):
        """Render the recorded state, quarantining the objects that break
        the checks; return the file, its generation and its attribution

        Raise InvalidPartitions if the state can't be rendered for a reason
        that no object accounts for, such as a broken template.
        """
        while True:
            with self._lock, self.sessionmaker() as session:
                # Never from a node still catching up, which would render an
                # older state (see Galera specifics)
                db.check_serving(session)
                generation, networks, ports = self._state(session)
                try:
                    content, attribution = self._render_state(
                        generation, networks, ports
                    )
                    return content, generation, attribution
                except partitions.InvalidPartitions as e:
                    e.generation = generation
                    culprits = [
                        ("network", n.id) for n in networks if n.pkey in e.pkeys
                    ]
                    culprits += [
                        ("port", p.id) for p in ports if int(p.guid, 16) in e.guids
                    ]
                    # Each round leaves out at least one more object, so
                    # this ends.
                    if not self._quarantine(session, culprits, str(e), generation):
                        e.generation = generation
                        raise
                    session.commit()

    @staticmethod
    def _quarantine(session, culprits, reason, generation):
        """Leave objects out of the partitions file until they're sent again,
        and fail their jobs, and those of a network's ports, up to the
        generation that was rendered; return how many were quarantined

        An object sent again since that generation is left alone: what was
        rendered isn't what it is now, and its new request releases it anyway.
        """
        count = 0
        for kind, object_id in culprits:
            newer = session.scalar(
                sa.select(db.Job.id).where(
                    db.Job.object_id == object_id, db.Job.generation > generation
                )
            )
            if newer is not None:
                continue
            count += 1
            LOG.error("Quarantining %s %s: %s", kind, object_id, reason)
            session.merge(db.Quarantine(kind=kind, object_id=object_id, reason=reason))
            if kind == "network":
                ids = [object_id] + list(
                    session.scalars(
                        sa.select(db.Port.id).where(db.Port.network_id == object_id)
                    )
                )
                operations = ("put_network", "put_port")
            else:
                ids, operations = [object_id], ("put_port",)
            session.execute(
                sa.update(db.Job)
                .where(
                    db.Job.object_id.in_(ids),
                    db.Job.operation.in_(operations),
                    db.Job.status.in_(OPEN),
                    db.Job.generation <= generation,
                    # A waiting request wasn't rendered.
                    db.Job.id.not_in(sa.select(db.Deferred.job_id)),
                )
                .values(
                    status=db.JobStatus.FAILED,
                    error=f"Quarantined {kind} {object_id}: {reason}",
                    finished_at=_now(),
                )
            )
        return count

    def _stamp(self, content):
        """Add an apply counter, so that OpenSM sees a changed file on every
        apply, and runs a pkey sweep. It goes last, so that OpenSM's line
        numbers are those of the attribution."""
        with self._lock, self.sessionmaker() as session:
            count = _increment(session, APPLIES)
            session.commit()
        return content + f"{APPLY_PREFIX}{count}\n"

    @staticmethod
    def _applied(session):
        applied = session.get(db.Applied, 1)
        if applied is None:
            applied = db.Applied(id=1)
            session.add(applied)
            session.flush()
        return applied

    def _record(self, content, generation, attribution, report):
        """Record what OpenSM swept"""
        with self._lock, self.sessionmaker() as session:
            applied = self._applied(session)
            if self.term and (applied.term or 0) > self.term:
                # A deposed leader's late write: the successor's sweep stands.
                LOG.error("Not recording a sweep of term %s", self.term)
                session.rollback()
                return
            applied.generation = generation
            applied.term = self.term
            applied.body = content
            applied.attribution = json.dumps(attribution, sort_keys=True)
            # A sweep is judged only by a fabric check taken after it.
            applied.check_ok = False
            applied.check_detail = "Not checked since the last sweep"
            pkey_errors = report.pkey_errors if report else set()
            # With per-port M_Key rows, ports OpenSM couldn't reach hold
            # jobs too.
            unreached, unattributed = self.port_keeper.sweep_errors(session, report)
            recorded = sorted(f"{g:016x}" for g in pkey_errors | unreached)
            if (report and report.unattributed) or unattributed:
                recorded.append(UNATTRIBUTED)
            applied.pkey_errors = json.dumps(recorded)
            # Cleared enforcement stays recorded until an operator clears it,
            # since a later sweep needn't report it again.
            enforcement = set(json.loads(applied.enforcement_errors or "[]"))
            enforcement |= report.enforcement_errors if report else set()
            applied.enforcement_errors = json.dumps(sorted(enforcement))
            session.commit()
        for guid in sorted(pkey_errors):
            LOG.error("OpenSM couldn't set the pkey table of 0x%016x", guid)
        if report and report.unattributed:
            LOG.error(
                "OpenSM couldn't set a pkey table it names no port GUID for, "
                "so no job of this sweep completes"
            )
        self._sweeps += 1
        self._written = content

    def _last_swept(self):
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            return applied.body if applied else None

    def _needs_sync(self):
        """Why OpenSM needs the partitions file applied, if it does

        The recorded state is the source of truth. The file is rewritten
        and applied if it isn't what the manager last applied, for example
        after someone edited it, or after an apply failed; if the recorded
        state has changed, or no longer renders to it, for example after it
        changed in the database without a job; or if OpenSM couldn't set
        some ports' pkey tables.
        """
        with self._lock, self.sessionmaker() as session:
            state = session.get(db.State, GENERATION)
            generation = state.value if state is not None else 0
            applied = session.get(db.Applied, 1)
            applied_generation = applied.generation if applied else None
            blocked = self._blocked_by_pkey_errors(session, applied)
        if generation == self._failed_generation:
            # Not this generation again, but OpenSM must be on the last
            # swept file.
            return RESTORE if self._written is None else None
        if self._written is None:
            return "the last apply wasn't confirmed"
        if generation != applied_generation:
            return "the recorded state has changed"
        try:
            with open(self.config.partitions_file) as f:
                on_disk = f.read()
        except OSError:
            on_disk = None
        if on_disk != self._written:
            return "the partitions file was changed outside the manager"
        try:
            content, _generation, _attribution = self._render()
        except partitions.InvalidPartitions:
            return "the recorded state can't be rendered"
        if _body(content) != _body(self._written):
            return "the recorded state renders differently"
        if blocked:
            # Only while a job waits on them, so that a port that keeps
            # failing doesn't have the shared fabric swept forever
            return "OpenSM couldn't set the pkey tables of ports with open jobs"
        return None

    @staticmethod
    def _blocked_by_pkey_errors(session, applied):
        """Whether an open job waits on the last sweep's pkey errors"""
        errors = set(json.loads(applied.pkey_errors or "[]")) if applied else set()
        if not errors:
            return False
        waiting = set(session.scalars(sa.select(db.Deferred.job_id)))
        for job in session.scalars(sa.select(db.Job).where(db.Job.status.in_(OPEN))):
            if job.id in waiting:
                continue
            if UNATTRIBUTED in errors:
                return True
            if errors & set(effects.loads(job.effect).get("guids", [])):
                return True
        return False

    def sync(self):
        """Make OpenSM's partitions match the recorded state"""
        if not self.driver.running():
            return
        if self._contested_fabric:
            # Another master could undo a sweep made now; wait for a check
            # that passes, then sweep, then check again.
            LOG.warning("Not applying the partitions while another SM may act")
            return
        reason = self._needs_sync()
        if reason is None:
            return
        if reason == RESTORE:
            self._restore()
            return
        LOG.info("Applying the partitions: %s", reason)
        error = ""
        attempt = 0
        while attempt < self.config.apply_attempts:
            attempt += 1
            try:
                content, generation, attribution = self._render()
            except partitions.InvalidPartitions as e:
                self._unattributable(str(e), getattr(e, "generation", None) or 0)
                self._restore()
                return
            stamped = self._stamp(content)
            try:
                partitions.write_atomically(self.config.partitions_file, stamped)
                # Until OpenSM confirms it, the file isn't what was applied.
                self._written = None
                report = self.driver.reload()
            except PartitionsRejected as e:
                if not self._quarantine_line(e, attribution, generation):
                    self._unattributable(str(e), generation)
                    self._restore()
                    return
                # Render again without them; that's not a failed attempt.
                attempt -= 1
                continue
            except (OSError, ReloadError) as e:
                error = str(e)
                LOG.warning("Attempt %d to apply the partitions failed: %s", attempt, e)
                if attempt < self.config.apply_attempts:
                    time.sleep(RETRY_DELAY)
                continue
            self._record(stamped, generation, attribution, report)
            self._failed_generation = None
            self.failed_applies = 0
            LOG.info("Applied generation %d", generation)
            return
        # Leave the file as it is: OpenSM may have applied it, the next sync
        # tries again, and the jobs fail at their deadlines.
        self.failed_applies += 1
        LOG.error("Failed to apply the partitions: %s", error)

    def _quarantine_line(self, error, attribution, generation):
        """Quarantine the objects of a line OpenSM couldn't parse that changed
        since the last sweep; return whether there were any"""
        if error.line is None:
            return False
        objects = attribution.get("lines", {}).get(str(error.line), [])
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            before = json.loads(applied.attribution or "{}") if applied else {}
            changed = []
            for kind, object_id in objects:
                if (kind, object_id) == ("network", "flat"):
                    # The flat partition is no one network's.
                    continue
                if kind == "network":
                    now = {
                        k
                        for k, v in attribution["partitions"].items()
                        if v == object_id
                    }
                    then = {
                        k
                        for k, v in before.get("partitions", {}).items()
                        if v == object_id
                    }
                else:
                    now = attribution["members"].get(object_id)
                    then = before.get("members", {}).get(object_id)
                if now != then:
                    changed.append((kind, object_id))
            if not changed or not self._quarantine(
                session, changed, str(error), generation
            ):
                return False
            session.commit()
        return True

    def _unattributable(self, reason, generation):
        """A failure of a rendered generation that no object accounts for:
        fail the jobs of the generations since the last sweep up to it, and
        don't try it again; requests since then are tried when synced"""
        LOG.error("Generation %d can't be applied: %s", generation, reason)
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            applied_generation = applied.generation if applied else 0
            session.execute(
                sa.update(db.Job)
                .where(
                    db.Job.generation > applied_generation,
                    db.Job.generation <= generation,
                    db.Job.status.in_(OPEN),
                    db.Job.id.not_in(sa.select(db.Deferred.job_id)),
                )
                .values(status=db.JobStatus.FAILED, error=reason, finished_at=_now())
            )
            session.commit()
        self._failed_generation = generation

    def _restore(self):
        """Put OpenSM back on the last file it swept; the next sync retries
        if this fails"""
        body = self._last_swept()
        if not body:
            return
        LOG.warning("Restoring the last swept partitions")
        try:
            restored = self._stamp(_body(body))
            partitions.write_atomically(self.config.partitions_file, restored)
            self._written = None
            report = self.driver.reload()
        except (OSError, ReloadError) as e:
            LOG.error("Failed to restore the last partitions: %s", e)
            return
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            generation = applied.generation
            attribution = json.loads(applied.attribution or "{}")
        self._record(restored, generation, attribution, report)

    # The fabric

    def check_fabric(self):
        """Check that OpenSM sees every expected switch, and no other subnet
        manager is master, and record the result"""
        try:
            # With per-port M_Key rows, the prober finds the switches.
            outcome = self.port_keeper.check()
            fabric = self._fabric(outcome.switches if outcome else None)
            # What OpenSM logged since the last check: SMs it ignored, and
            # what its class key managers reported
            findings = self._log_findings()
            sm_problems, sm_contested = self.allowed_sms.check(findings)
            if findings is not None and self.class_keys.watching():
                self.class_keys.report(findings)
            if findings is not None and self.sa_rate.watching():
                self.sa_rate.report(findings)
        except ReloadError as e:
            # Not knowing is no evidence either way: a contested fabric stays
            # contested until a check passes.
            ok, detail = False, f"Can't query the fabric: {e}"
            self.unanswered_checks += 1
            # Nor is it a check that saw the switches.
            self._seen_checks = {}
        else:
            self._keep_expected(fabric, outcome, sm_contested)
            self.unanswered_checks = 0
            ok, detail = self._judge(fabric)
            if ok and outcome and outcome.problems:
                ok, detail = False, "; ".join(outcome.problems)
            if ok and sm_problems:
                ok, detail = False, "; ".join(sm_problems)
            # Only a passing check ends a contest; one that fails for another
            # reason, such as a missing switch, which the other SM may sit
            # behind, doesn't. An ignored SM on a known SM port contests it
            # until a query of the port clears it.
            self._contested_fabric = (
                (fabric is not None and self._contested(fabric))
                or sm_contested
                or (self._contested_fabric and not ok)
            )
            if self._contested_fabric:
                # Another master may have set pkey tables since the last
                # sweep, so it is no evidence: sweep again, once the check
                # passes, before any job completes.
                LOG.error(
                    "Another subnet manager acted; the last sweep no longer counts"
                )
                self._written = None
        if not ok:
            LOG.error("Fabric check failed: %s", detail)
        with self._lock, self.sessionmaker() as session:
            applied = self._applied(session)
            if self.term and (applied.term or 0) > self.term:
                session.rollback()
                return ok
            applied.check_ok, applied.check_detail = ok, detail
            applied.check_time = _now()
            applied.contested = self._contested_fabric
            session.commit()
        if ok:
            # At a check that passes: arm the per-port M_Key turn-on, and
            # apply the target (specs/zero-touch.md)
            try:
                self.port_keeper.after_check()
            except (ReloadError, port_keeper.PortKeysError) as e:
                LOG.error("Can't arm or apply per-port M_Keys: %s", e)
        return ok

    def _log_findings(self):
        """What OpenSM logged since the last check, read only when a group
        that judges it is in effect, or None"""
        if not (
            self.allowed_sms.active()
            or self.class_keys.watching()
            or self.sa_rate.watching()
        ):
            return None
        if not self.driver.running():
            return None
        return self.driver.log_findings()

    @staticmethod
    def _contested(fabric):
        """Whether another SM is master, or OpenSM's own isn't"""
        own = [state for guid, state in fabric.sms if guid in fabric.own_guids]
        others = [state for guid, state in fabric.sms if guid not in fabric.own_guids]
        return 3 not in own or 3 in others

    def _judge(self, fabric):
        if fabric is None:
            return True, "No fabric to check"
        with self._lock, self.sessionmaker() as session:
            # pending, which an older version wrote, is ignored: nothing is
            # pending any more (specs/zero-touch.md).
            expected = {
                int(s.guid, 16) for s in session.scalars(sa.select(db.ExpectedSwitch))
            }
            # HA: the other nodes' ports, whose SMs may hold the keys
            managed = {}
            if self.config.ha:
                for row in session.scalars(sa.select(db.Node)):
                    if row.name == self.config.node:
                        continue
                    for text in json.loads(row.port_guids or "[]"):
                        managed[int(text, 16)] = row.name
        others = [
            (guid, state) for guid, state in fabric.sms if guid not in fabric.own_guids
        ]
        for guid, state in others:
            LOG.warning("Another subnet manager, 0x%016x, is in state %d", guid, state)
        # Another managed node's SM fails the check whatever its state: it may
        # have the fabric's keys (specs/ha.md, Job completion).
        theirs = [(guid, state) for guid, state in others if guid in managed]
        if theirs:
            return False, "Another managed node's subnet manager is running: " + (
                ", ".join(
                    f"0x{guid:016x} (node {managed[guid]}, state {state})"
                    for guid, state in theirs
                )
            )
        masters = [guid for guid, state in others if state == 3]
        ours = [state for guid, state in fabric.sms if guid in fabric.own_guids]
        if 3 not in ours:
            # Jobs mustn't complete on partitions that no master applied.
            return False, (
                "OpenSM isn't master"
                if ours
                else "The SA has no record of OpenSM's own subnet manager"
            )
        missing = expected - fabric.switches
        self.missing_checks = self.missing_checks + 1 if missing else 0
        if not expected:
            return False, (
                "No expected switches yet: recording them once the fabric has "
                f"been stable for {self.config.confirm_checks} checks"
            )
        if missing:
            return False, "Missing switches: " + ", ".join(
                f"0x{guid:016x}" for guid in sorted(missing)
            )
        if masters:
            return False, "Another subnet manager is master: " + ", ".join(
                f"0x{guid:016x}" for guid in masters
            )
        return True, "The fabric is complete"

    # The expected switches (specs/zero-touch.md)

    @staticmethod
    def _master(fabric):
        """Whether OpenSM's own SM is master, as the SA says"""
        return 3 in [state for guid, state in fabric.sms if guid in fabric.own_guids]

    def _keep_expected(self, fabric, outcome, sm_contested):
        """Count the checks in a row that have seen each switch, and keep the
        expected switches from them: record the list once the fabric is
        stable, add a switch once it is, translate the list when its source
        changes, and drop a missing switch the prober shows gone

        Nothing changes, and the counts start again, while the check was
        contested or OpenSM isn't master: another SM may have changed what
        the fabric looks like. A check that couldn't query the fabric never
        gets here. In HA mode these are leader writes, behind the leader
        fence.
        """
        if fabric is None:
            return
        if self._contested(fabric) or sm_contested or not self._master(fabric):
            self._seen_checks = {}
            return
        found = set(fabric.switches)
        if outcome is not None:
            source, nodes = PROBER, dict(outcome.nodes)
        else:
            # ibswitches gives node GUIDs.
            source, nodes = IBSWITCHES, {guid: guid for guid in found}
        self._seen_checks = {guid: self._seen_checks.get(guid, 0) + 1 for guid in found}
        stable = {
            guid
            for guid, count in self._seen_checks.items()
            if count >= self.config.confirm_checks
        }
        with self.fenced_session() as session:
            rows = {
                int(row.guid, 16): (
                    None if row.node_guid is None else int(row.node_guid, 16)
                )
                for row in session.scalars(sa.select(db.ExpectedSwitch))
            }
            state = session.get(db.State, EXPECTED_SOURCE)
            recorded_source = state.text if state is not None else None
            expected = dict(rows)
            if expected and recorded_source != source:
                expected = self._translate(expected, recorded_source, source, nodes)
            # Fill in a node GUID the row lacks, as one recorded before they
            # were kept, so that a later change of source can translate it
            for guid in expected:
                if expected[guid] is None and nodes.get(guid) is not None:
                    expected[guid] = nodes[guid]
            if not expected:
                if found and stable == found:
                    expected = {guid: nodes.get(guid) for guid in found}
                    LOG.warning(
                        "Recorded the expected switches, seen by %d checks in a "
                        "row: %s",
                        self.config.confirm_checks,
                        ", ".join(f"0x{guid:016x}" for guid in sorted(found)),
                    )
            else:
                for guid in sorted(stable - set(expected)):
                    expected[guid] = nodes.get(guid)
                    LOG.warning(
                        "Switch 0x%016x, seen by %d checks in a row, is now expected",
                        guid,
                        self.config.confirm_checks,
                    )
                if source == PROBER:
                    links = port_keeper.port_keys.load(session)[1]
                    for guid in sorted(set(expected) - found):
                        if self.port_keeper.switch_gone(guid, links):
                            del expected[guid]
                            LOG.warning(
                                "Expected switch 0x%016x is missing, and the "
                                "prober shows every recorded link to it down: "
                                "it's no longer expected",
                                guid,
                            )
            if expected != rows:
                session.execute(sa.delete(db.ExpectedSwitch))
                for guid, node in sorted(expected.items()):
                    session.add(
                        db.ExpectedSwitch(
                            guid=f"{guid:016x}",
                            pending=False,
                            node_guid=None if node is None else f"{node:016x}",
                        )
                    )
            if expected and recorded_source != source:
                if state is None:
                    session.add(db.State(name=EXPECTED_SOURCE, value=0, text=source))
                else:
                    state.text = source
            session.commit()

    @staticmethod
    def _translate(expected, recorded_source, source, nodes):
        """The expected switches, recorded from another source, in this
        check's terms, by each one's node GUID: a switch the new source can't
        match stays as it is, expected, and fails the check as missing, so
        that a switch already missing isn't dropped by the change"""
        by_node = {node: guid for guid, node in nodes.items() if node is not None}
        translated = {}
        unmatched = []
        for guid, node in expected.items():
            if node is None and recorded_source != PROBER:
                # Recorded by ibswitches, or before the source was kept:
                # its GUID may be the node GUID.
                node = guid
            new = by_node.get(node) if node is not None else None
            if new is None and guid in nodes:
                new = guid
            if new is None:
                translated.setdefault(guid, expected[guid])
                unmatched.append(guid)
            else:
                translated[new] = nodes.get(new, node)
        LOG.warning(
            "The expected switches were recorded from %s, and the check now "
            "finds them with %s: translated them by their node GUIDs%s",
            recorded_source or "an older version",
            source,
            (
                "; these stay as recorded, expected: "
                + ", ".join(f"0x{guid:016x}" for guid in sorted(unmatched))
            )
            if unmatched
            else "",
        )
        return translated

    def confirm_fabric(self):
        """Record the switches OpenSM sees as the fabric's expected ones,
        and, with per-port M_Keys, the links OpenSM's cache lists; with the
        m_key_per_port group named and the target off, arm the turn-on if
        its preconditions hold (specs/secured-profile.md, Turning it on).
        Returns a port_keeper.Confirmation."""
        keeper = self.port_keeper
        # A cache that can't be read stops only the turn-on, not the
        # switches' confirmation.
        fold_problem = keeper.fold_running()
        # No switch is pending: whatever answers as one is recorded, since
        # the list is only what a complete fabric has (specs/zero-touch.md).
        confirmation = port_keeper.Confirmation()
        probed = keeper.switches()
        fabric = self._fabric(None if probed is None else set(probed))
        if fabric is None:
            return confirmation
        found = sorted(fabric.switches)
        source = IBSWITCHES if probed is None else PROBER
        with self._lock, self.sessionmaker() as session:
            session.execute(sa.delete(db.ExpectedSwitch))
            for guid in found:
                node = guid if probed is None else probed.get(guid)
                session.add(
                    db.ExpectedSwitch(
                        guid=f"{guid:016x}",
                        pending=False,
                        node_guid=None if node is None else f"{node:016x}",
                    )
                )
            state = session.get(db.State, EXPECTED_SOURCE)
            if state is None:
                session.add(db.State(name=EXPECTED_SOURCE, value=0, text=source))
            else:
                state.text = source
            session.commit()
        confirmation.switches = found
        if confirmation.switches:
            keeper.arm(confirmation, fold_problem)
        return confirmation

    def enforcement_errors(self):
        """The switch ports whose partition enforcement OpenSM cleared, as
        "<switch GUID>:<port>", or "unattributed" for one it didn't name"""
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            return json.loads(applied.enforcement_errors or "[]") if applied else []

    def clear_enforcement_errors(self):
        """Forget the switch ports whose enforcement OpenSM cleared, once an
        operator has dealt with them"""
        with self._lock, self.sessionmaker() as session:
            applied = self._applied(session)
            cleared = json.loads(applied.enforcement_errors or "[]")
            applied.enforcement_errors = "[]"
            session.commit()
        return cleared

    # Jobs

    def evaluate_jobs(self):
        """Complete the jobs whose effects the last sweep has, and fail those
        past their deadline"""
        with self._lock, self.sessionmaker() as session:
            jobs = list(
                session.scalars(sa.select(db.Job).where(db.Job.status.in_(OPEN)))
            )
            self._evaluate(session, jobs)
            session.commit()

    def _evaluate(self, session, jobs):
        """Complete what has its effect, then fail what is past its deadline,
        in that order. Cleared enforcement holds every job until an operator
        clears it; the spec's PortInfo check of it is still to come."""
        applied = session.get(db.Applied, 1)
        running = self.driver.running()
        fresh = datetime.timedelta(seconds=2 * self.config.check_interval)
        ready = (
            applied is not None
            and running
            and not self._contested_fabric
            and self._written is not None
            # Condition 1: evidence from this leader's own term, in HA mode
            and (not self.term or applied.term == self.term)
            and applied.check_ok
            and applied.check_time is not None
            and _now() - _aware(applied.check_time) <= fresh
            and not json.loads(applied.enforcement_errors or "[]")
        )
        if ready:
            attribution = json.loads(applied.attribution or "{}")
            pkey_errors = set(json.loads(applied.pkey_errors or "[]"))
            ready = UNATTRIBUTED not in pkey_errors
        waiting = set(session.scalars(sa.select(db.Deferred.job_id)))
        for job in jobs:
            if job.id in waiting:
                # Not applied yet, whatever the file holds for its object
                self._time_out(job)
            elif (
                ready
                and self._holds(job, applied, attribution, pkey_errors)
                and self.port_keeper.job_ready(session, job)
            ):
                job.status = db.JobStatus.COMPLETED
                job.finished_at = _now()
            else:
                self._time_out(job)

    @staticmethod
    def _holds(job, applied, attribution, pkey_errors):
        if (applied.generation or 0) < job.generation:
            return False
        effect = effects.loads(job.effect)
        if pkey_errors & set(effect.get("guids", [])):
            return False
        return effects.holds(effect, attribution)

    def reconcile(self):
        """Apply the requests that no longer wait, sync OpenSM, check the
        fabric, and complete the jobs it has applied"""
        self.retry_deferred()
        self.sync()
        # After the sweep: with per-port M_Keys, the check's Gets to the
        # ports of open jobs are their evidence, which counts only if it was
        # taken after the last recorded sweep.
        self.check_fabric()
        self.evaluate_jobs()
        self.forget_tombstones()

    apply_pending = reconcile
