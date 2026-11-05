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

Every change records the desired state and a pending job. One thread applies
the desired state: it writes the whole partitions file, has OpenSM read it,
and only then completes the jobs that the file covers. networking-mlnx waits
for a port's job before it lets the port become ACTIVE, so a port is never
reported as in its partition before OpenSM has put it there.
"""

import datetime
import logging
import os
import threading
import time

import sqlalchemy as sa

from alternative_fabric_manager import db, opensm_conf, partitions
from alternative_fabric_manager.drivers import ReloadError

LOG = logging.getLogger(__name__)

GENERATION = "generation"
RETRY_DELAY = 2.0


class NotFound(LookupError):
    pass


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
        # The partitions file last written and applied, and its generation
        self._written = None
        self._written_generation = None

    # Requests

    def _record(self, session, operation, object_id):
        job = db.Job(operation=operation, object_id=object_id)
        session.add(job)
        session.flush()
        return job.id

    def _commit_job(self, session, operation, object_id):
        job_id = self._record(session, operation, object_id)
        session.commit()
        self._wakeup.set()
        return job_id

    def put_network(self, network_id, data):
        """Create or update a network's partition"""
        partitions.check_id(network_id)
        pkey = partitions.pkey_from_network(dict(data, id=network_id))
        if pkey in self.reserved_pkeys:
            raise partitions.InvalidRequest(
                f"Partition key 0x{pkey:04x} of network {network_id} is reserved"
            )
        with self._lock, self.sessionmaker() as session:
            other = None
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
            network = session.get(db.Network, network_id)
            if network is None:
                network = db.Network(id=network_id)
                session.add(network)
            network.pkey = pkey
            network.name = str(data.get("name") or "")[:255]
            return self._commit_job(session, "put_network", network_id)

    def delete_network(self, network_id):
        partitions.check_id(network_id)
        with self._lock, self.sessionmaker() as session:
            network = session.get(db.Network, network_id)
            if network is not None:
                session.execute(
                    sa.delete(db.Port).where(db.Port.network_id == network_id)
                )
                session.delete(network)
            # Even when the network is gone, answer with a job rather than
            # 404, which networking-mlnx takes as done: a DELETE is sent again
            # when its first job failed, and OpenSM may still have the
            # network's partition until an apply succeeds.
            return self._commit_job(session, "delete_network", network_id)

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
            if session.get(db.Network, network_id) is None:
                # The network was never sent, or its request failed. Fail
                # rather than let the port become ACTIVE outside its
                # partition.
                raise partitions.InvalidRequest(
                    f"Port {port_id} is on network {network_id}, which has no partition"
                )
            port = session.get(db.Port, port_id)
            if port is None:
                port = db.Port(id=port_id)
                session.add(port)
            port.network_id = network_id
            port.guid = f"{guid:016x}"
            port.host = str(data.get("binding:host_id") or "")[:255]
            return self._commit_job(session, "put_port", port_id)

    def delete_port(self, port_id):
        partitions.check_id(port_id)
        with self._lock, self.sessionmaker() as session:
            port = session.get(db.Port, port_id)
            if port is not None:
                session.delete(port)
            # As for networks: the GUID may still be in the partition until an
            # apply succeeds.
            return self._commit_job(session, "delete_port", port_id)

    def get_job(self, job_id):
        with self._lock, self.sessionmaker() as session:
            job = session.get(db.Job, job_id)
            if job is None:
                raise NotFound(f"Job {job_id}")
            return job

    # Applying the desired state

    def start(self):
        """Write the current state, start OpenSM, then apply changes"""
        LOG.info(
            "OpenSM hardening: %s",
            ", ".join(sorted(self.config.hardening_groups)) or "none",
        )
        self._write_current()
        self.driver.start()
        self._started()
        self._thread = threading.Thread(target=self._run, name="applier", daemon=True)
        self._thread.start()
        # Apply what was pending when the service last stopped.
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
                self.apply_pending()
                self.reconcile()
            except Exception:
                LOG.exception("Failed to apply the partitions")
                time.sleep(RETRY_DELAY)
                self._wakeup.set()

    def _check_opensm(self):
        """Start OpenSM again if it has stopped, with the current state"""
        if self.driver.running():
            return
        LOG.error("OpenSM isn't running; starting it again")
        try:
            self._write_current()
            self.driver.start()
            self._started()
        except (OSError, ReloadError, partitions.InvalidPartitions) as e:
            LOG.error("Failed to start OpenSM: %s", e)

    def _render(self, session, generation=None):
        """Return the partitions file for the current state, checked, and its
        generation: a new one, unless generation is given"""
        if generation is None:
            state = session.get(db.State, GENERATION)
            if state is None:
                state = db.State(name=GENERATION, value=0)
                session.add(state)
            state.value += 1
            generation = state.value
        members = {}
        for port in session.scalars(sa.select(db.Port)):
            members.setdefault(port.network_id, []).append(int(port.guid, 16))
        networks = [
            (network, members.get(network.id, []))
            for network in session.scalars(sa.select(db.Network))
        ]
        content = partitions.render(self.config, networks, generation, self.template)
        partitions.validate(
            content,
            {n.pkey: set(guids) for n, guids in networks if n.pkey is not None},
            {guid for n, guids in networks if n.pkey is None for guid in guids},
            flat_pkey=self.config.flat_pkey,
            shared_default=self.config.shared_default_partition,
        )
        return content, generation

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

    def _write_current(self):
        """Write the current state's files, before OpenSM starts

        If the current state can't be rendered, OpenSM starts with the last
        partitions file, which passed the checks when it was written, rather
        than not at all.
        """
        self._write_opensm_conf()
        try:
            with self._lock, self.sessionmaker() as session:
                content, generation = self._render(session)
                session.commit()
        except partitions.InvalidPartitions as e:
            if not os.path.exists(self.config.partitions_file):
                raise
            LOG.error(
                "Starting OpenSM with the last partitions file, since the "
                "current state can't be rendered: %s",
                e,
            )
            return
        partitions.write_atomically(self.config.partitions_file, content)
        # OpenSM reads it when it starts.
        self._pending_start = (content, generation)

    def _snapshot(self):
        """Return the pending jobs, and the file that covers them or why
        there can't be one"""
        with self._lock, self.sessionmaker() as session:
            job_ids = list(
                session.scalars(
                    sa.select(db.Job.id).where(
                        db.Job.status.in_((db.JobStatus.PENDING, db.JobStatus.RUNNING))
                    )
                )
            )
            if not job_ids:
                return [], (None, None), None
            try:
                content, error = self._render(session), None
            except partitions.InvalidPartitions as e:
                content, error = (None, None), str(e)
            session.execute(
                sa.update(db.Job)
                .where(db.Job.id.in_(job_ids))
                .values(status=db.JobStatus.RUNNING)
            )
            session.commit()
            return job_ids, content, error

    def _finish(self, job_ids, status, error=""):
        with self._lock, self.sessionmaker() as session:
            session.execute(
                sa.update(db.Job)
                .where(db.Job.id.in_(job_ids))
                .values(
                    status=status,
                    error=error,
                    finished_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
            session.commit()

    def _started(self):
        """Record the file that OpenSM started with"""
        pending, self._pending_start = getattr(self, "_pending_start", None), None
        if pending is not None:
            self._written, self._written_generation = pending

    def _apply(self, content, generation, attempt=1):
        """Write a partitions file and have OpenSM apply it"""
        if attempt > 1:
            # So that OpenSM sees a changed file, and runs a pkey sweep
            content += f"# Attempt {attempt}\n"
        partitions.write_atomically(self.config.partitions_file, content)
        # Until OpenSM confirms it, the file isn't what was applied, so a
        # failed apply is retried by reconcile().
        self._written = None
        self.driver.reload()
        self._written, self._written_generation = content, generation

    def reconcile(self):
        """Make the partitions file, and OpenSM, match the recorded state

        The recorded state is the source of truth. The file is rewritten
        and applied again if it isn't what the manager last applied, for
        example after someone edited it, or after an apply failed, or if
        the recorded state no longer renders to it, for example after it
        changed in the database without a job.
        """
        if not self.driver.running():
            return
        try:
            with open(self.config.partitions_file) as f:
                on_disk = f.read()
        except OSError:
            on_disk = None
        if self._written is not None and on_disk == self._written:
            with self._lock, self.sessionmaker() as session:
                try:
                    desired, _generation = self._render(
                        session, generation=self._written_generation
                    )
                except partitions.InvalidPartitions:
                    desired = None
            # The attempt line, if any, isn't part of the state.
            written = self._written.split("# Attempt ")[0]
            if desired == written:
                return
            reason = "the recorded state has changed"
        elif self._written is None:
            reason = "the last apply wasn't confirmed"
        else:
            reason = "the partitions file was changed outside the manager"
        LOG.warning("Applying the partitions again: %s", reason)
        try:
            with self._lock, self.sessionmaker() as session:
                content, generation = self._render(session)
                session.commit()
            self._apply(content, generation)
        except (OSError, ReloadError, partitions.InvalidPartitions) as e:
            LOG.error("Failed to apply the partitions again: %s", e)

    def apply_pending(self):
        """Apply the desired state, if any job is waiting for it"""
        job_ids, (content, generation), error = self._snapshot()
        if not job_ids:
            return
        if error:
            # Retrying wouldn't render anything else, and the file must not
            # be written.
            LOG.error("Refusing to write the partitions file: %s", error)
            self._finish(job_ids, db.JobStatus.FAILED, error)
            return
        LOG.info("Applying partitions for %d jobs", len(job_ids))
        for attempt in range(1, self.config.apply_attempts + 1):
            try:
                self._apply(content, generation, attempt)
            except (OSError, ReloadError) as e:
                error = str(e)
                LOG.warning(
                    "Attempt %d to apply the partitions failed: %s",
                    attempt,
                    error,
                )
                if attempt < self.config.apply_attempts:
                    time.sleep(RETRY_DELAY)
                continue
            self._finish(job_ids, db.JobStatus.COMPLETED)
            LOG.info("Applied partitions for %d jobs", len(job_ids))
            return
        # Leave the file as it is: OpenSM may have applied it, and the next
        # change rewrites it whole anyway.
        self._finish(job_ids, db.JobStatus.FAILED, error)
