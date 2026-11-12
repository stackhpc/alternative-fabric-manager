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
"PKEY SWEEP COMPLETE". Otherwise, for example when the file hasn't changed,
it runs a heavy sweep, which reads the partitions file after it logs "HEAVY
SWEEP COMPLETE", and ends with "SUBNET UP".
"""

import contextlib
import logging
import re
import time

import docker
import docker.errors
import docker.utils

from alternative_fabric_manager import opensm_conf
from alternative_fabric_manager.drivers import (
    UNATTRIBUTED,
    Fabric,
    PartitionsRejected,
    ReloadError,
    SweepReport,
)

LOG = logging.getLogger(__name__)

# OpenSM logs this when it handles SIGHUP, before and after the sweep, and
# then starts a pkey sweep, or a heavy one if another of its files changed.
HUP_MARKER = "handle_hup_signal"
SWEEP_START_MARKERS = ("INITIATING PKEY SWEEP", "INITIATING HEAVY SWEEP")
# A heavy sweep logs "HEAVY SWEEP COMPLETE" before it reads the partitions
# file, so only "SUBNET UP" marks its end.
DONE_MARKERS = ("PKEY SWEEP COMPLETE", "SUBNET UP")
# OpenSM logs this once it has started as master. A managed OpenSM that
# enters STANDBY has found another subnet manager, so its start failed.
STARTED_MARKERS = ("SUBNET UP",)
STANDBY_MARKER = "Entering STANDBY state"
# The pkey manager's and virtualisation manager's errors setting pkey tables,
# in OpenSM 5.17, by code (see specs/ha.md, OpenSM errors)
PKEY_ERROR_CODES = (
    "0502",
    "0503",
    "0511",
    "0514",
    "0505",
    "0506",
    "0508",
    "0509",
    "0512",
    "0F12",
    "1503",
    "5703",
    "6B06",
)
# A switch port with too few pkey entries: OpenSM clears its enforcement bit
ENFORCEMENT_ERROR_CODES = ("0507", "0513")
_ERR = re.compile(r"ERR ([0-9A-F]{4}):")
# The port GUIDs some pkey errors name. Most name a port only by its node's
# description, and 0509 by its node GUID and port number, which isn't the
# port's GUID; those can't be traced to a port.
# A virtual port's GUID is the one its Neutron port has, so both count.
_PORT_GUID = re.compile(r"(?:of port|VPort|guid \()\s*0x([0-9a-fA-F]{1,16})")
_SWITCH_PORT = re.compile(r"switch 0x([0-9a-fA-F]{1,16}) port (\d+)")
_PARSE_LINE = re.compile(r"PARSE ERROR: line (\d+)")
POLL_INTERVAL = 0.5
# Seconds between attempts to pull OpenSM's image that failed
PULL_RETRY = 60.0


def _is_partitions_error(line):
    # OpenSM skips a line of the partitions file that it can't parse, and
    # still completes the sweep. The pkey errors are recorded, not raised.
    match = _ERR.search(line)
    if match and match.group(1) in PKEY_ERROR_CODES + ENFORCEMENT_ERROR_CODES:
        return False
    return "PARSE ERROR" in line or ("prtn" in line and "ERR" in line)


def _rejected(line):
    match = _PARSE_LINE.search(line)
    return PartitionsRejected(
        f"OpenSM couldn't read the partitions file: {line.strip()}",
        line=int(match.group(1)) if match else None,
    )


# The MAD layer's errors, which a PKeyTable set or get can fail with: they
# name no port GUID, so they hold every job of the sweep
MAD_ERROR_CODES = ("3113", "3119", "3120")
# OpenSM names the attribute P_KeyTable, or V_PKeyTable for a virtual port.
_PKEY_TABLE_ATTR = re.compile(r"P_?KeyTable|attribute 0x16\b")


def _note_pkey_error(report, line):
    """Record a pkey or enforcement error that OpenSM logged"""
    match = _ERR.search(line)
    if not match:
        return
    code = match.group(1)
    if code in MAD_ERROR_CODES:
        if _PKEY_TABLE_ATTR.search(line):
            report.unattributed = True
            LOG.warning("OpenSM couldn't set a pkey table: %s", line.strip())
        return
    # What can't be traced fails closed: it holds every job of the sweep.
    if code in ENFORCEMENT_ERROR_CODES:
        ports = _SWITCH_PORT.findall(line)
        for switch, port in ports:
            report.enforcement_errors.add(f"{int(switch, 16):016x}:{port}")
        if not ports:
            report.enforcement_errors.add(UNATTRIBUTED)
        LOG.error("OpenSM cleared a switch port's enforcement: %s", line.strip())
    elif code in PKEY_ERROR_CODES:
        guids = {int(g, 16) for g in _PORT_GUID.findall(line)}
        report.pkey_errors |= guids
        if not guids:
            report.unattributed = True
        LOG.warning("OpenSM couldn't set a pkey table: %s", line.strip())


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
        # Something else starts OpenSM, so there is no sweep to report.
        return None

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
            return self._wait_for_sweep(container, since, seen)
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e

    def fabric(self, m_key=None, sa_key=None):
        """Ask the fabric, from OpenSM's container, for its switches and the
        subnet managers its SA knows"""
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} isn't running"
                )
            m_key_args = ["-y", m_key] if m_key else []
            switches = self._exec(container, ["ibswitches", *m_key_args])
            sm_records = self._exec(
                container, ["saquery", *(["--smkey", sa_key] if sa_key else []), "SMIR"]
            )
            ports = self._exec(container, ["ibstat", "-p"])
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e
        switch_guids = {
            int(g, 16)
            for g in re.findall(r"^Switch\s*:\s*0x([0-9a-fA-F]+)", switches, re.M)
        }
        sms = [
            (int(guid, 16), int(state))
            for guid, state in re.findall(
                r"GUID\.+0x([0-9a-fA-F]+).*?SMState\.+(\d+)", sm_records, re.S
            )
        ]
        # OpenSM binds to one of the host's ports, not necessarily the
        # first: an SM on any of them is this one.
        own = {int(g, 16) for g in re.findall(r"0x([0-9a-fA-F]+)", ports)}
        # Unassigned virtual functions report placeholder GUIDs.
        own -= {0, 0xFFFFFFFFFFFFFFFF}
        return Fabric(switch_guids, sms, own)

    @staticmethod
    def _exec(container, command):
        code, output = container.exec_run(command)
        output = output.decode(errors="replace")
        if code != 0:
            raise ReloadError(f"{command[0]} failed: {output.strip()[:200]}")
        return output

    @staticmethod
    def _log(container, since=None):
        kwargs = {"stdout": True, "stderr": True}
        if since is not None:
            kwargs["since"] = since
        return container.logs(**kwargs).decode(errors="replace").splitlines()

    def _wait_for_sweep(self, container, since, seen):
        """Wait for OpenSM to handle the signal, then for a sweep that starts
        after that to finish: one already under way may not have read the
        file. Returns what it reported about pkey tables it couldn't set."""
        deadline = time.monotonic() + self.timeout
        handled = started = False
        report = SweepReport()
        while time.monotonic() < deadline:
            lines = self._log(container, since)[seen:]
            seen += len(lines)
            for line in lines:
                if "PARSE ERROR" in line:
                    raise _rejected(line)
                if _is_partitions_error(line):
                    raise ReloadError(
                        f"OpenSM couldn't read the partitions file: {line.strip()}"
                    )
                if started:
                    _note_pkey_error(report, line)
                if HUP_MARKER in line:
                    handled = True
                elif handled and any(m in line for m in SWEEP_START_MARKERS):
                    started = True
                elif started and any(m in line for m in DONE_MARKERS):
                    LOG.debug("OpenSM: %s", line.strip(" *"))
                    return report
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
            "command": ["-F", f"{opensm_conf.CONTAINER_CONFIG_DIR}/opensm.conf"],
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

    def has_image(self):
        """Whether this host has OpenSM's image"""
        try:
            self.client.images.get(self.config.image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def ensure_image(self):
        """Pull OpenSM's image if this host doesn't have it

        A start, or a takeover in HA mode, then doesn't wait on a registry,
        nor fail because one is down (see specs/ha.md, Timeouts). An image
        already here is never pulled again: with a tag that moves, that would
        change which OpenSM runs from one start to the next. Raises if the
        pull fails: DockerException, or requests' errors if the daemon goes
        away."""
        if self.has_image():
            return
        LOG.info("Pulling OpenSM's image %s", self.config.image)
        repository, tag = docker.utils.parse_repository_tag(self.config.image)
        # The low-level pull, since the high-level one discards the stream,
        # and with it the registry's reason for a pull that fails part way
        for chunk in self.client.api.pull(
            repository, tag=tag or "latest", stream=True, decode=True
        ):
            if "error" in chunk:
                raise docker.errors.APIError(chunk["error"])
        if not self.has_image():
            raise docker.errors.ImageNotFound(
                f"{self.config.image} isn't here after pulling it"
            )
        LOG.info("Pulled OpenSM's image %s", self.config.image)

    def keep_image(self, stop, interval=PULL_RETRY):
        """ensure_image() every interval until stop is set: for a thread of
        its own, since a pull may take minutes. It keeps checking after a
        pull succeeds, so that a node whose image is removed, as by a prune,
        gets it back before it's needed."""
        while not stop.is_set():
            try:
                self.ensure_image()
            except Exception as e:  # noqa: BLE001 - as a daemon restart raises
                LOG.error(
                    "Can't pull OpenSM's image %s, retrying in %d s: %s",
                    self.config.image,
                    interval,
                    e,
                )
            stop.wait(interval)

    def _remove(self):
        with contextlib.suppress(docker.errors.NotFound):
            self.client.containers.get(self.container_name).remove(force=True)

    def start(self):
        """Start OpenSM, with its files already written"""
        try:
            # A container left by an earlier manager has stopped, since it
            # was in that manager's PID namespace, but its name is taken.
            self._remove()
            container = self.client.containers.run(**self._run_kwargs())
            LOG.info("Started OpenSM container %s", self.container_name)
            return self._wait_for_start(container)
        except (docker.errors.DockerException, ReloadError) as e:
            # An OpenSM that didn't start properly, for example one that
            # rejected part of its partitions file, mustn't keep running.
            self.stop()
            if isinstance(e, ReloadError):
                raise
            raise ReloadError(f"Docker: {e}") from e

    def _wait_for_start(self, container):
        """Wait for OpenSM's first sweep as master, which reads the
        partitions file, and report its pkey errors"""
        deadline = time.monotonic() + self.config.start_timeout
        while time.monotonic() < deadline:
            lines = self._log(container)
            report = SweepReport()
            for line in lines:
                _note_pkey_error(report, line)
                if "PARSE ERROR" in line:
                    raise _rejected(line)
                if _is_partitions_error(line):
                    raise ReloadError(
                        f"OpenSM couldn't read the partitions file: {line.strip()}"
                    )
                if STANDBY_MARKER in line:
                    raise ReloadError(
                        "OpenSM entered STANDBY: another subnet manager is master"
                    )
                if any(m in line for m in STARTED_MARKERS):
                    LOG.info("OpenSM: %s", line.strip(" *"))
                    return report
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
