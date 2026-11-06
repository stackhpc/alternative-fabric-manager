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

"""The watchdog: OpenSM's container's entrypoint (see specs/ha.md)

It starts OpenSM only on a single-use start request, for the lease it has,
and kills OpenSM with SIGKILL when its lease lapses, when the manager asks
it to stop, or when it gets SIGTERM; then it exits, and so does the
container. It exits too if OpenSM exits.

The manager and the watchdog share a directory on a tmpfs:
- lease.json: {"boot_id", "instance", "term", "deadline"}, the deadline on
  CLOCK_MONOTONIC, which every container on the host shares, or null while
  frozen and held with no expiry;
- start.json: {"boot_id", "instance", "term", "deadline", "hashes"}, a
  single-use request, consumed when read, with the SHA-256 of each file
  OpenSM reads;
- stop: present when the manager asks it to stop OpenSM.
Every file is written atomically by the manager. The watchdog needs no
database: it acts on its lease alone.
"""

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time

LOG = logging.getLogger("alternative_fabric_manager.watchdog")
POLL = 0.2
LEASE = "lease.json"
START = "start.json"
STOP = "stop"


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(directory, name):
    try:
        with open(os.path.join(directory, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomically(directory, name, data):
    """Write one of the shared files, as the manager does"""
    path = os.path.join(directory, name)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def lease_valid(lease, now=None):
    """Whether a lease lets OpenSM run now"""
    if not lease or lease.get("boot_id") != boot_id():
        return False
    deadline = lease.get("deadline")
    if deadline is None:
        # Held while frozen, with no expiry
        return True
    return (now if now is not None else time.monotonic()) < deadline


def start_allowed(lease, request, now=None):
    """Whether a start request may start OpenSM: single use, for the lease
    the watchdog has, before its deadline, with the files it names"""
    if not request or not lease_valid(lease, now):
        return False, "no valid lease"
    if request.get("boot_id") != boot_id():
        return False, "a request from another boot"
    if (request.get("instance"), request.get("term")) != (
        lease.get("instance"),
        lease.get("term"),
    ):
        return False, "a request for another lease"
    deadline = request.get("deadline")
    if (
        deadline is not None
        and (now if now is not None else time.monotonic()) >= deadline
    ):
        return False, "a request past its deadline"
    for path, digest in (request.get("hashes") or {}).items():
        try:
            if sha256(path) != digest:
                return False, f"{path} isn't the file the request names"
        except OSError:
            return False, f"{path} is missing"
    return True, ""


class Watchdog:
    def __init__(self, directory, command):
        self.directory = directory
        self.command = command
        self.process = None
        self.stopping = False

    def _consume_start(self):
        path = os.path.join(self.directory, START)
        request = _read(self.directory, START)
        try:
            os.unlink(path)
        except OSError:
            return None
        return request

    def _kill(self, why):
        if self.process is not None and self.process.poll() is None:
            LOG.warning("Killing OpenSM: %s", why)
            self.process.kill()
            self.process.wait()

    def run(self, wait_for_start=30.0):
        """Wait for a start request, run OpenSM under the lease, and return
        OpenSM's exit status, or 1 if it never started"""
        give_up = time.monotonic() + wait_for_start
        while self.process is None:
            if self.stopping or os.path.exists(os.path.join(self.directory, STOP)):
                return 1
            if os.path.exists(os.path.join(self.directory, START)):
                request = self._consume_start()
                allowed, why = start_allowed(_read(self.directory, LEASE), request)
                if not allowed:
                    LOG.error("Refusing a start request: %s", why)
                    return 1
                LOG.info("Starting OpenSM for term %s", request.get("term"))
                self.process = subprocess.Popen(self.command)  # noqa: S603
                break
            if time.monotonic() > give_up:
                LOG.error("No start request arrived")
                return 1
            time.sleep(POLL)
        while True:
            if self.process.poll() is not None:
                LOG.warning("OpenSM exited with %s", self.process.returncode)
                return self.process.returncode
            if self.stopping:
                self._kill("SIGTERM")
                return 0
            if os.path.exists(os.path.join(self.directory, STOP)):
                self._kill("the manager asked")
                return 0
            if not lease_valid(_read(self.directory, LEASE)):
                self._kill("its lease lapsed")
                return 0
            time.sleep(POLL)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run OpenSM under a lease")
    parser.add_argument("--state-dir", default="/run/alternative-fabric-manager")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s watchdog: %(message)s")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    dog = Watchdog(args.state_dir, command or ["opensm"])
    signal.signal(signal.SIGTERM, lambda *_: setattr(dog, "stopping", True))
    sys.exit(dog.run())


if __name__ == "__main__":
    main()
