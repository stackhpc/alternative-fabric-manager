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

import datetime
import json
import logging
import os
import threading
import time

import sqlalchemy as sa

from alternative_fabric_manager import db, effects, opensm_conf, partitions
from alternative_fabric_manager.drivers import (
    UNATTRIBUTED,
    PartitionsRejected,
    ReloadError,
)

LOG = logging.getLogger(__name__)

GENERATION = "generation"
APPLIES = "applies"
APPLY_PREFIX = "# Apply "
RETRY_DELAY = 2.0
# _needs_sync()'s reason for restoring the last swept file
RESTORE = "the last swept partitions must be restored"
OPEN = (db.JobStatus.PENDING, db.JobStatus.RUNNING)


class NotFound(LookupError):
    pass


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
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # The partitions file last written and swept
        self._written = None
        # A generation whose render or apply failed in a way that can't be
        # traced to an object, so that it isn't tried again until the
        # recorded state changes
        self._failed_generation = None

    # Requests

    @staticmethod
    def _bump(session):
        """Bump the generation, first in a request's transaction"""
        state = session.get(db.State, GENERATION)
        if state is None:
            state = db.State(name=GENERATION, value=0)
            session.add(state)
        state.value += 1
        session.flush()
        return state.value

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
        session.flush()

    def put_network(self, network_id, data):
        """Create or update a network's partition"""
        partitions.check_id(network_id)
        pkey = partitions.pkey_from_network(dict(data, id=network_id))
        if pkey in self.reserved_pkeys:
            raise partitions.InvalidRequest(
                f"Partition key 0x{pkey:04x} of network {network_id} is reserved"
            )
        values = {"pkey": pkey, "name": str(data.get("name") or "")[:255]}
        with self._lock, self.sessionmaker() as session:
            generation = self._bump(session)
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

    def delete_network(self, network_id):
        partitions.check_id(network_id)
        with self._lock, self.sessionmaker() as session:
            generation = self._bump(session)
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
        }
        with self._lock, self.sessionmaker() as session:
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
            generation = self._bump(session)
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

    def delete_port(self, port_id):
        partitions.check_id(port_id)
        with self._lock, self.sessionmaker() as session:
            generation = self._bump(session)
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
        """Return the fabric's shared OpenSM keys, generating any missing"""
        with self._lock, self.sessionmaker() as session:
            keys = {}
            for name in opensm_conf.KEYS:
                secret = session.get(db.Secret, name)
                if secret is None:
                    secret = db.Secret(name=name, value=opensm_conf.new_key())
                    session.add(secret)
                keys[name] = secret.value
            session.commit()
            return keys

    def _key(self, name):
        """The key OpenSM uses, if any: an override, or a hardened one"""
        if name in self.config.opensm_conf:
            return self.config.opensm_conf[name]
        if name in self.config.hardening_groups:
            return self.keys()[name]
        return None

    def _fabric(self):
        return self.driver.fabric(m_key=self._key("m_key"), sa_key=self._key("sa_key"))

    def _write_opensm_conf(self):
        if self.config.driver != "managed":
            return
        groups = self.config.hardening_groups
        content = opensm_conf.render(
            self.config.opensm_conf,
            hardening=groups,
            keys=self.keys() if groups & {"m_key", "sm_key", "sa_key"} else None,
        )
        # The keys are secrets.
        partitions.write_atomically(self.config.opensm_conf_file, content, 0o600)

    # Syncing OpenSM

    def start(self):
        """Write the current state, start OpenSM, then keep it in sync"""
        LOG.info(
            "OpenSM hardening: %s",
            ", ".join(sorted(self.config.hardening_groups)) or "none",
        )
        self._start_opensm()
        self._thread = threading.Thread(target=self._run, name="applier", daemon=True)
        self._thread.start()
        self._wakeup.set()

    def stop(self):
        self._stop.set()
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join()
        self.driver.stop()

    def _run(self):
        while not self._stop.is_set():
            self._wakeup.wait(self.config.check_interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            try:
                self._check_opensm()
                self.reconcile()
            except Exception:
                LOG.exception("Failed to sync OpenSM")
                time.sleep(RETRY_DELAY)
                self._wakeup.set()

    def _start_opensm(self):
        """Write the current state's files and start OpenSM with them

        If the current state can't be rendered, OpenSM starts with the last
        partitions file, which passed the checks when it was written, rather
        than not at all, and the next sync deals with the state.
        """
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
                self.driver.start()
                return
            stamped = self._stamp(content)
            partitions.write_atomically(self.config.partitions_file, stamped)
            try:
                report = self.driver.start()
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
                self.driver.start()
                return
            # A start that the driver saw sweep is a sweep, with its pkey
            # errors; otherwise the next sync sweeps and records it.
            if report is not None:
                self._record(stamped, generation, attribution, report)
            return

    def _check_opensm(self):
        """Start OpenSM again if it has stopped, with the current state"""
        if self.driver.running():
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
            state = session.get(db.State, APPLIES)
            if state is None:
                state = db.State(name=APPLIES, value=0)
                session.add(state)
            state.value += 1
            count = state.value
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
            applied.generation = generation
            applied.body = content
            applied.attribution = json.dumps(attribution, sort_keys=True)
            # A sweep is judged only by a fabric check taken after it.
            applied.check_ok = False
            applied.check_detail = "Not checked since the last sweep"
            pkey_errors = report.pkey_errors if report else set()
            recorded = sorted(f"{g:016x}" for g in pkey_errors)
            if report and report.unattributed:
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
            LOG.info("Applied generation %d", generation)
            return
        # Leave the file as it is: OpenSM may have applied it, the next sync
        # tries again, and the jobs fail at their deadlines.
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
            fabric = self._fabric()
        except ReloadError as e:
            ok, detail = False, f"Can't query the fabric: {e}"
        else:
            ok, detail = self._judge(fabric)
            if fabric is not None and self._contested(fabric):
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
            applied.check_ok, applied.check_detail = ok, detail
            applied.check_time = _now()
            session.commit()
        return ok

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
            expected = {
                int(s.guid, 16) for s in session.scalars(sa.select(db.ExpectedSwitch))
            }
        others = [
            (guid, state) for guid, state in fabric.sms if guid not in fabric.own_guids
        ]
        for guid, state in others:
            LOG.warning("Another subnet manager, 0x%016x, is in state %d", guid, state)
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
        if not expected:
            return False, "No expected switches: confirm them with --confirm-switches"
        if missing:
            return False, "Missing switches: " + ", ".join(
                f"0x{guid:016x}" for guid in sorted(missing)
            )
        if masters:
            return False, "Another subnet manager is master: " + ", ".join(
                f"0x{guid:016x}" for guid in masters
            )
        return True, "The fabric is complete"

    def confirm_switches(self):
        """Record the switches OpenSM sees as the fabric's expected ones"""
        fabric = self._fabric()
        if fabric is None:
            return []
        with self._lock, self.sessionmaker() as session:
            session.execute(sa.delete(db.ExpectedSwitch))
            for guid in sorted(fabric.switches):
                session.add(db.ExpectedSwitch(guid=f"{guid:016x}"))
            session.commit()
        return sorted(fabric.switches)

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
        fresh = datetime.timedelta(seconds=2 * self.config.check_interval)
        ready = (
            applied is not None
            and self._written is not None
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
            elif ready and self._holds(job, applied, attribution, pkey_errors):
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
        self.check_fabric()
        self.evaluate_jobs()

    apply_pending = reconcile
