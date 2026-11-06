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

"""OpenSM in a container under the watchdog, for HA mode (see specs/ha.md)

The managed driver's container, with the watchdog as its entrypoint: the
manager copies watchdog.py, which needs only Python's standard library, to
the tmpfs the two containers share, and OpenSM's image runs it with its own
python3. It is both the manager's driver and the coordinator's runner.

The deployment must:
- mount the same tmpfs directory, [ha] ha_state_dir, into the manager's
  container at the same path, such as the host's /run/alternative-fabric-manager;
- mount the host's /proc read-only at [ha] host_proc, /host/proc, so that the
  manager can verify that no OpenSM runs on the host, in any container;
- create the manager's container with a stop timeout above
  [ha] shutdown_timeout, 30 s.
"""

import contextlib
import hashlib
import logging
import os
import re
import shutil
import time

import docker.errors

from alternative_fabric_manager import opensm_conf
from alternative_fabric_manager.drivers import docker as docker_driver
from alternative_fabric_manager.ha import watchdog as wd

LOG = logging.getLogger(__name__)
WATCHDOG = "watchdog.py"
# How long reach() keeps an answer: it runs a container
REACH_CACHE = 10.0
_PORT_UP = re.compile(r"State:\s*(Initializing|Armed|Active)\b")
_LINK_UP = re.compile(r"Physical state:\s*LinkUp\b")
_INFINIBAND = re.compile(r"Link layer:\s*InfiniBand\b")
# How long the probe may take
REACH_TIMEOUT = 20
_SWITCH = re.compile(r"^Switch\s*:\s*0x[0-9a-fA-F]+", re.M)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HAManagedDocker(docker_driver.ManagedDocker):
    def __init__(self, config, client=None, m_key=None):
        super().__init__(config, client)
        self.state_dir = config.ha_state_dir
        # Called for the fabric's M_Key, for reach()'s SMPs
        self.m_key = m_key or (lambda: None)
        self._start = None
        self._reach = (None, -REACH_CACHE)

    def _run_kwargs(self):
        kwargs = super()._run_kwargs()
        conf = kwargs["command"][-1]
        kwargs["entrypoint"] = [
            "python3",
            os.path.join(self.state_dir, WATCHDOG),
            "--state-dir",
            self.state_dir,
            "--",
        ]
        kwargs["command"] = ["/usr/sbin/opensm", "-F", conf]
        kwargs["volumes"] = [*kwargs["volumes"], f"{self.state_dir}:{self.state_dir}"]
        return kwargs

    # The coordinator's runner

    def prepare(self, instance, term, deadline):
        os.makedirs(self.state_dir, exist_ok=True)
        shutil.copyfile(wd.__file__, os.path.join(self.state_dir, WATCHDOG))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.state_dir, wd.STOP))
        self._start = (instance, term, deadline)

    def write_lease(self, instance, term, deadline):
        wd.write_atomically(
            self.state_dir,
            wd.LEASE,
            {
                "boot_id": wd.boot_id(),
                "instance": instance,
                "term": term,
                "deadline": deadline,
                "held": deadline is None,
            },
        )

    def _container_paths(self):
        """OpenSM's files as the manager sees them, and as OpenSM does"""
        inside = opensm_conf.CONTAINER_CONFIG_DIR
        return {
            os.path.join(inside, "partitions.conf"): self.config.partitions_file,
            os.path.join(inside, "opensm.conf"): self.config.opensm_conf_file,
        }

    def start(self):
        instance, term, deadline = self._start
        wd.write_atomically(
            self.state_dir,
            wd.START,
            {
                "boot_id": wd.boot_id(),
                "instance": instance,
                "term": term,
                "deadline": deadline,
                "hashes": {
                    inside: _sha256(outside)
                    for inside, outside in self._container_paths().items()
                },
            },
        )
        return super().start()

    def stop(self):
        """Ask the watchdog to stop OpenSM; kill its container, paused or not,
        if it doesn't within a few seconds"""
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, wd.STOP), "w") as f:
            f.write("stop")
        end = time.monotonic() + 5
        while time.monotonic() < end and self._opensm_pids():
            time.sleep(0.2)
        try:
            container = self.client.containers.get(self.container_name)
            if container.status in ("running", "paused"):
                container.kill()
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as e:
            LOG.warning("Can't remove the OpenSM container: %s", e)
        # Rejoining: no stale lease or start request survives a stop.
        for name in (wd.LEASE, wd.START):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(self.state_dir, name))

    def _opensm_pids(self):
        """OpenSM processes anywhere on the host, through its /proc"""
        proc = self.config.host_proc
        pids = []
        try:
            entries = os.listdir(proc)
        except OSError:
            LOG.error("Can't read the host's processes at %s", proc)
            return None
        for pid in entries:
            if not pid.isdigit():
                continue
            try:
                with open(os.path.join(proc, pid, "comm")) as f:
                    if f.read().strip() == "opensm":
                        pids.append(int(pid))
            except OSError:
                continue
        return pids

    def verify_stopped(self):
        # Unreadable counts as not verified.
        return self._opensm_pids() == []

    def ask(self, node):
        # SMInfo DISABLE by directed route is still to come (see specs/ha.md,
        # Fencing); self-stop and the watchdog don't depend on it.
        LOG.info("Asking %s's SM to stop isn't implemented yet", node)

    def reach(self):
        """Switches this node's port reaches, or None if its port is down"""
        value, when = self._reach
        if time.monotonic() - when < REACH_CACHE:
            return value
        value = self._probe_reach()
        self._reach = (value, time.monotonic())
        return value

    def _probe_reach(self):
        key = self.m_key()
        command = [
            "sh",
            "-c",
            f"timeout {REACH_TIMEOUT} ibstat; echo ===; timeout {REACH_TIMEOUT} "
            "ibswitches " + (f"-y {key}" if key else ""),
        ]
        try:
            output = self.client.containers.run(
                image=self.config.image,
                entrypoint=command[:2],
                command=command[2:],
                network_mode="host",
                privileged=True,
                remove=True,
            )
        except docker.errors.DockerException as e:
            LOG.warning("Can't probe this node's reach: %s", e)
            return None
        status, _, switches = output.decode(errors="replace").partition("===")
        # An InfiniBand port with its link up, in Init or above
        if not any(
            _PORT_UP.search(port) and _LINK_UP.search(port) and _INFINIBAND.search(port)
            for port in status.split("Port ")[1:]
        ):
            return None
        return len(_SWITCH.findall(switches))
