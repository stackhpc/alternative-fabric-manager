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
from alternative_fabric_manager.drivers import PartitionsRejected, ReloadError

LOG = logging.getLogger(__name__)

GENERATION = "generation"
APPLIES = "applies"
APPLY_PREFIX = "# Apply "
RETRY_DELAY = 2.0
# Times to quarantine objects and render again, in one render
MAX_QUARANTINES = 20
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

    def _commit_job(self, session, generation, operation, object_id, effect):
        job = db.Job(
            operation=operation,
            object_id=object_id,
            generation=generation,
            effect=effects.dumps(effect),
            deadline=_now() + datetime.timedelta(seconds=self.config.job_timeout),
        )
        session.add(job)
        session.flush()
        session.commit()
        self._wakeup.set()
        return job.id

    def put_network(self, network_id, data):
        """Create or update a network's partition"""
        partitions.check_id(network_id)
        pkey = partitions.pkey_from_network(dict(data, id=network_id))
        if pkey in self.reserved_pkeys:
            raise partitions.InvalidRequest(
                f"Partition key 0x{pkey:04x} of network {network_id} is reserved"
            )
        with self._lock, self.sessionmaker() as session:
            if pkey is not None:
                other = session.scalar(
                    sa.select(db.Network).where(
                        db.Network.pkey == pkey, db.Network.id != network_id
                    )
                )
                if other is not None:
                    raise partitions.InvalidRequest(
                        f"Partition key 0x{pkey:04x} is already network {other.id}'s"
                    )
            generation = self._bump(session)
            network = session.get(db.Network, network_id)
            if network is None:
                network = db.Network(id=network_id)
                session.add(network)
            network.pkey = pkey
            network.name = str(data.get("name") or "")[:255]
            self._release(session, "network", network_id)
            return self._commit_job(
                session,
                generation,
                "put_network",
                network_id,
                effects.network_present(network_id, pkey),
            )

    def delete_network(self, network_id):
        partitions.check_id(network_id)
        with self._lock, self.sessionmaker() as session:
            generation = self._bump(session)
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
        with self._lock, self.sessionmaker() as session:
            other = session.scalar(
                sa.select(db.Port).where(
                    db.Port.guid == f"{guid:016x}", db.Port.id != port_id
                )
            )
            if other is not None:
                # A GUID can be in only one network's partition. networking-mlnx
                # deletes a GUID's old port before it sends a new one, so this is
                # a port whose deletion never arrived, or two ports with one
                # client ID.
                raise partitions.InvalidRequest(
                    f"Port {port_id} has the GUID of port {other.id}"
                )
            network = session.get(db.Network, network_id)
            if network is None:
                # The network was never sent, or its request failed. Fail
                # rather than let the port become ACTIVE outside its
                # partition.
                raise partitions.InvalidRequest(
                    f"Port {port_id} is on network {network_id}, which has no partition"
                )
            generation = self._bump(session)
            port = session.get(db.Port, port_id)
            if port is None:
                port = db.Port(id=port_id)
                session.add(port)
            port.network_id = network_id
            port.guid = f"{guid:016x}"
            port.host = str(data.get("binding:host_id") or "")[:255]
            self._release(session, "port", port_id)
            pkey = network.pkey if network.pkey is not None else self.config.flat_pkey
            return self._commit_job(
                session,
                generation,
                "put_port",
                port_id,
                effects.port_present(port_id, pkey, port.guid),
            )

    def delete_port(self, port_id):
        partitions.check_id(port_id)
        with self._lock, self.sessionmaker() as session:
            generation = self._bump(session)
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

    def get_job(self, job_id):
        with self._lock, self.sessionmaker() as session:
            job = session.get(db.Job, job_id)
            if job is None:
                raise NotFound(f"Job {job_id}")
            if self._time_out(job):
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
            self._written = None
            self.driver.start()
            return
        content = self._stamp(content)
        partitions.write_atomically(self.config.partitions_file, content)
        self._written = None
        self.driver.start()
        # OpenSM read it when it started. Its start reports no pkey errors,
        # so the jobs wait for the next sweep's, which the next sync forces.
        self._record(content, generation, attribution, None)
        self._written = None

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
        for _ in range(MAX_QUARANTINES):
            with self._lock, self.sessionmaker() as session:
                generation, networks, ports = self._state(session)
                try:
                    content, attribution = self._render_state(
                        generation, networks, ports
                    )
                    return content, generation, attribution
                except partitions.InvalidPartitions as e:
                    culprits = [
                        ("network", n.id) for n in networks if n.pkey in e.pkeys
                    ]
                    culprits += [
                        ("port", p.id) for p in ports if int(p.guid, 16) in e.guids
                    ]
                    if not culprits:
                        raise
                    self._quarantine(session, culprits, str(e))
                    session.commit()
        raise partitions.InvalidPartitions("Too many objects to quarantine")

    @staticmethod
    def _quarantine(session, culprits, reason):
        """Leave objects out of the partitions file until they're sent again,
        and fail their jobs, and those of a network's ports"""
        for kind, object_id in culprits:
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
                )
                .values(
                    status=db.JobStatus.FAILED,
                    error=f"Quarantined {kind} {object_id}: {reason}",
                    finished_at=_now(),
                )
            )

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
            pkey_errors = report.pkey_errors if report else set()
            applied.pkey_errors = json.dumps(sorted(f"{g:016x}" for g in pkey_errors))
            # Cleared enforcement stays recorded until an operator clears it,
            # since a later sweep needn't report it again.
            enforcement = set(json.loads(applied.enforcement_errors or "[]"))
            enforcement |= report.enforcement_errors if report else set()
            applied.enforcement_errors = json.dumps(sorted(enforcement))
            session.commit()
        for guid in sorted(pkey_errors):
            LOG.error("OpenSM couldn't set the pkey table of 0x%016x", guid)
        self._written = content

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
            pkey_errors = json.loads(applied.pkey_errors or "[]") if applied else []
            applied_generation = applied.generation if applied else None
        if generation == self._failed_generation:
            return None
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
        if pkey_errors:
            return "OpenSM couldn't set some ports' pkey tables"
        return None

    def sync(self):
        """Make OpenSM's partitions match the recorded state"""
        if not self.driver.running():
            return
        reason = self._needs_sync()
        if reason is None:
            return
        LOG.info("Applying the partitions: %s", reason)
        error = ""
        attempt = 0
        while attempt < self.config.apply_attempts:
            attempt += 1
            try:
                content, generation, attribution = self._render()
            except partitions.InvalidPartitions as e:
                self._unattributable(str(e))
                return
            stamped = self._stamp(content)
            try:
                partitions.write_atomically(self.config.partitions_file, stamped)
                # Until OpenSM confirms it, the file isn't what was applied.
                self._written = None
                report = self.driver.reload()
            except PartitionsRejected as e:
                if not self._quarantine_line(e, attribution):
                    self._unattributable(str(e))
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

    def _quarantine_line(self, error, attribution):
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
            if not changed:
                return False
            self._quarantine(session, changed, str(error))
            session.commit()
        return True

    def _unattributable(self, reason):
        """A failure that no object accounts for: keep OpenSM on the last
        file it swept, fail the jobs of newer generations, and don't try
        this generation again"""
        LOG.error("The partitions can't be applied: %s", reason)
        with self._lock, self.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            state = session.get(db.State, GENERATION)
            generation = state.value if state is not None else 0
            applied_generation = applied.generation if applied else 0
            session.execute(
                sa.update(db.Job)
                .where(db.Job.generation > applied_generation, db.Job.status.in_(OPEN))
                .values(status=db.JobStatus.FAILED, error=reason, finished_at=_now())
            )
            session.commit()
            body = applied.body if applied else None
        self._failed_generation = generation
        if not body:
            return
        try:
            restored = self._stamp(_body(body))
            partitions.write_atomically(self.config.partitions_file, restored)
            self._written = None
            self.driver.reload()
            self._written = restored
        except (OSError, ReloadError) as e:
            LOG.error("Failed to restore the last partitions: %s", e)

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
        if not ok:
            LOG.error("Fabric check failed: %s", detail)
        with self._lock, self.sessionmaker() as session:
            applied = self._applied(session)
            applied.check_ok, applied.check_detail = ok, detail
            applied.check_time = _now()
            session.commit()
        return ok

    def _judge(self, fabric):
        if fabric is None:
            return True, "No fabric to check"
        with self._lock, self.sessionmaker() as session:
            expected = {
                int(s.guid, 16) for s in session.scalars(sa.select(db.ExpectedSwitch))
            }
        others = [
            (guid, state) for guid, state in fabric.sms if guid != fabric.own_guid
        ]
        for guid, state in others:
            LOG.warning("Another subnet manager, 0x%016x, is in state %d", guid, state)
        masters = [guid for guid, state in others if state == 3]
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
            applied = session.get(db.Applied, 1)
            jobs = list(
                session.scalars(sa.select(db.Job).where(db.Job.status.in_(OPEN)))
            )
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
            for job in jobs:
                if self._time_out(job) or not ready:
                    continue
                if (applied.generation or 0) < job.generation:
                    continue
                effect = effects.loads(job.effect)
                if pkey_errors & set(effect.get("guids", [])):
                    continue
                if effects.holds(effect, attribution):
                    job.status = db.JobStatus.COMPLETED
                    job.finished_at = _now()
            session.commit()

    def reconcile(self):
        """Sync OpenSM, check the fabric, and complete the jobs it has
        applied"""
        self.sync()
        self.check_fabric()
        self.evaluate_jobs()

    apply_pending = reconcile
