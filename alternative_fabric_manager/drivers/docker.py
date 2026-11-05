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

"""Reload OpenSM in a Docker container with SIGHUP

When OpenSM gets SIGHUP and only its partitions file has changed, it runs a
pkey sweep, which updates the pkey tables of the fabric's ports, and logs
"PKEY SWEEP COMPLETE". If another of its files has changed, it runs a heavy
sweep instead, which ends with "SUBNET UP" or "HEAVY SWEEP COMPLETE".
"""

import logging
import time

import docker
import docker.errors

from alternative_fabric_manager.drivers import ReloadError

LOG = logging.getLogger(__name__)

# OpenSM logs this when it handles SIGHUP, before and after the sweep.
HUP_MARKER = "handle_hup_signal"
DONE_MARKERS = ("PKEY SWEEP COMPLETE", "HEAVY SWEEP COMPLETE", "SUBNET UP")
POLL_INTERVAL = 0.5


class DockerReloader:
    def __init__(self, container, timeout, client=None):
        self.container_name = container
        self.timeout = timeout
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def reload(self):
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} is {container.status}"
                )
            # The daemon timestamps the container's log, so compare with
            # its clock rather than ours, a second early to allow for
            # rounding. Lines from before the signal are skipped below.
            since = time.time() - 1
            seen = len(self._log(container, since))
            container.kill(signal="SIGHUP")
            self._wait_for_sweep(container, since, seen)
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e

    @staticmethod
    def _log(container, since):
        return (
            container.logs(since=since, stdout=True, stderr=True)
            .decode(errors="replace")
            .splitlines()
        )

    @staticmethod
    def _is_partitions_error(line):
        # OpenSM skips a line of the partitions file that it can't parse,
        # and still completes the sweep.
        return "PARSE ERROR" in line or ("prtn" in line and "ERR" in line)

    def _wait_for_sweep(self, container, since, seen):
        """Wait for OpenSM to handle the signal and finish its sweep"""
        deadline = time.monotonic() + self.timeout
        handled = False
        while time.monotonic() < deadline:
            lines = self._log(container, since)[seen:]
            seen += len(lines)
            for line in lines:
                if self._is_partitions_error(line):
                    raise ReloadError(
                        f"OpenSM couldn't read the partitions file: {line.strip()}"
                    )
                if HUP_MARKER in line:
                    handled = True
                elif handled and any(m in line for m in DONE_MARKERS):
                    LOG.debug("OpenSM: %s", line.strip(" *"))
                    return
            time.sleep(POLL_INTERVAL)
        raise ReloadError(
            f"OpenSM didn't finish a sweep within {self.timeout} seconds of SIGHUP"
        )
