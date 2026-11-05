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
import threading
import time

import sqlalchemy as sa

from alternative_fabric_manager import db, partitions
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
        # One SQLite connection is shared by every thread, so all database
        # access is serialised.
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread = None

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
        pkey = partitions.pkey_from_network(dict(data, id=network_id))
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
        guid = partitions.guid_from_port(data)
        network_id = data.get("network_id")
        with self._lock, self.sessionmaker() as session:
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
        self._write_current()
        self.driver.start()
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
        except (OSError, ReloadError, partitions.InvalidPartitions) as e:
            LOG.error("Failed to start OpenSM: %s", e)

    def _render(self, session):
        """Return the partitions file for the current state, checked"""
        state = session.get(db.State, GENERATION)
        if state is None:
            state = db.State(name=GENERATION, value=0)
            session.add(state)
        state.value += 1
        members = {}
        for port in session.scalars(sa.select(db.Port)):
            members.setdefault(port.network_id, []).append(int(port.guid, 16))
        networks = [
            (network, members.get(network.id, []))
            for network in session.scalars(sa.select(db.Network))
        ]
        content = partitions.render(self.config, networks, state.value, self.template)
        partitions.validate(
            content,
            {n.pkey: set(guids) for n, guids in networks if n.pkey is not None},
            {guid for n, guids in networks if n.pkey is None for guid in guids},
        )
        return content

    def _write_current(self):
        """Write the current state's partitions file, before OpenSM starts"""
        with self._lock, self.sessionmaker() as session:
            content = self._render(session)
            session.commit()
        partitions.write_atomically(self.config.partitions_file, content)

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
                return [], None, None
            try:
                content, error = self._render(session), None
            except partitions.InvalidPartitions as e:
                content, error = None, str(e)
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

    def apply_pending(self):
        """Apply the desired state, if any job is waiting for it"""
        job_ids, content, error = self._snapshot()
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
                partitions.write_atomically(self.config.partitions_file, content)
                self.driver.reload()
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
