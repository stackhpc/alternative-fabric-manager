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

"""OpenSM in a Docker container, reloaded with SIGHUP

When OpenSM gets SIGHUP and only its partitions file has changed, it runs a
pkey sweep, which updates the pkey tables of the fabric's ports, and logs
"PKEY SWEEP COMPLETE". If another of its files has changed, it runs a heavy
sweep instead, which ends with "SUBNET UP" or "HEAVY SWEEP COMPLETE".
"""

import contextlib
import logging
import shlex
import time

import docker
import docker.errors

from alternative_fabric_manager import opensm_conf, partitions
from alternative_fabric_manager.drivers import ReloadError

LOG = logging.getLogger(__name__)

# OpenSM logs this when it handles SIGHUP, before and after the sweep.
HUP_MARKER = "handle_hup_signal"
DONE_MARKERS = ("PKEY SWEEP COMPLETE", "HEAVY SWEEP COMPLETE", "SUBNET UP")
# OpenSM logs one of these once it has started, as master or standby.
STARTED_MARKERS = ("SUBNET UP", "Entering STANDBY state")
POLL_INTERVAL = 0.5


def _is_partitions_error(line):
    # OpenSM skips a line of the partitions file that it can't parse, and
    # still completes the sweep.
    return "PARSE ERROR" in line or ("prtn" in line and "ERR" in line)


class DockerReloader:
    """OpenSM in a container that something else starts"""

    def __init__(self, container, timeout, client=None):
        self.container_name = container
        self.timeout = timeout
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def start(self):
        pass

    def running(self):
        # Whatever starts OpenSM restarts it too.
        return True

    def stop(self):
        pass

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
    def _log(container, since=None):
        kwargs = {"stdout": True, "stderr": True}
        if since is not None:
            kwargs["since"] = since
        return container.logs(**kwargs).decode(errors="replace").splitlines()

    def _wait_for_sweep(self, container, since, seen):
        """Wait for OpenSM to handle the signal and finish its sweep"""
        deadline = time.monotonic() + self.timeout
        handled = False
        while time.monotonic() < deadline:
            lines = self._log(container, since)[seen:]
            seen += len(lines)
            for line in lines:
                if _is_partitions_error(line):
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


class ManagedDocker(DockerReloader):
    """OpenSM in a container that the manager starts

    The container joins the manager's PID namespace, so the kernel kills
    OpenSM when the manager's container stops, however it stops: OpenSM never
    runs with partitions that no manager keeps up to date. It has no restart
    policy, since the manager decides when it starts.
    """

    def __init__(self, config, client=None):
        super().__init__(config.container, config.sweep_timeout, client)
        self.config = config

    def _run_kwargs(self):
        config = self.config
        volumes = [f"{config.host_config_dir}:{opensm_conf.CONTAINER_CONFIG_DIR}:ro"]
        volumes += [v.strip() for v in config.volumes.split(",") if v.strip()]
        kwargs = {
            "image": config.image,
            # After the image's own -F, so that this one is used
            "command": [
                "-F",
                f"{opensm_conf.CONTAINER_CONFIG_DIR}/opensm.conf",
                *shlex.split(config.opensm_args),
            ],
            "name": config.container,
            "detach": True,
            "network_mode": "host",
            "privileged": True,
            "read_only": True,
            "restart_policy": {"Name": "no"},
            "volumes": volumes,
        }
        if config.manager_container:
            kwargs["pid_mode"] = f"container:{config.manager_container}"
        else:
            LOG.warning(
                "[opensm] manager_container isn't set, so OpenSM will keep "
                "running if the manager stops"
            )
        return kwargs

    def _remove(self):
        with contextlib.suppress(docker.errors.NotFound):
            self.client.containers.get(self.container_name).remove(force=True)

    def start(self):
        """Start OpenSM, with the partitions file already written"""
        partitions.write_atomically(
            self.config.opensm_conf_file, opensm_conf.render(self.config.opensm_conf)
        )
        try:
            # A container left by an earlier manager has stopped, since it
            # was in that manager's PID namespace, but its name is taken.
            self._remove()
            container = self.client.containers.run(**self._run_kwargs())
            LOG.info("Started OpenSM container %s", self.container_name)
            self._wait_for_start(container)
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e

    def _wait_for_start(self, container):
        deadline = time.monotonic() + self.config.start_timeout
        while time.monotonic() < deadline:
            lines = self._log(container)
            for line in lines:
                if _is_partitions_error(line):
                    raise ReloadError(
                        f"OpenSM couldn't read the partitions file: {line.strip()}"
                    )
                if any(m in line for m in STARTED_MARKERS):
                    LOG.info("OpenSM: %s", line.strip(" *"))
                    return
            container.reload()
            if container.status not in ("created", "running"):
                raise ReloadError(
                    f"OpenSM exited while starting: {' / '.join(lines[-5:])}"
                )
            time.sleep(POLL_INTERVAL)
        raise ReloadError(
            f"OpenSM didn't start within {self.config.start_timeout} seconds"
        )

    def running(self):
        try:
            container = self.client.containers.get(self.container_name)
        except docker.errors.NotFound:
            return False
        except docker.errors.DockerException as e:
            # Nothing could be started either.
            LOG.warning("Can't check the OpenSM container: %s", e)
            return True
        return container.status == "running"

    def stop(self):
        try:
            self._remove()
        except docker.errors.DockerException as e:
            LOG.warning("Can't remove the OpenSM container: %s", e)
