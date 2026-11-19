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
import datetime
import io
import logging
import re
import tarfile
import time

import docker
import docker.errors
import docker.utils
import requests.exceptions

from alternative_fabric_manager import allowed_sm, opensm_conf
from alternative_fabric_manager import prober as probes
from alternative_fabric_manager.config import OPENSM_CACHE_DIR, parse_volumes
from alternative_fabric_manager.drivers import (
    UNATTRIBUTED,
    Fabric,
    LogFindings,
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

# libosmvendor's error for a directed-route SMP that failed to send, which
# names no port. PerfMgr logs "PerfMgr: ERR 5411: Failed to send clear
# counters MAD", which isn't one, so the text is matched, not the code.
_DR_SEND_ERROR = re.compile(r"ERR 5411: DR SMP Send completed with error")
# The MAD layer's "ERR 3113: MAD completed in error (%s): %s(%s), attr_mod
# 0x%x, TID 0x%lx, dest_guid 0x%016lx", for a NodeInfo or PortInfo Get. Its
# dest_guid needn't be the port's (see specs/ha.md), 0 included.
_UNREACHED = re.compile(
    r"ERR 3113: MAD completed in error \([^)]*\): SubnGet\((?:NodeInfo|PortInfo)\),"
    r".*\bdest_guid 0x([0-9a-fA-F]{1,16})"
)
# A Bad M_Key trap (256), as OpenSM logs receiving it: "Received Generic
# Notice type:%u num:%u (%s) Producer:%u (%s) from LID:%u%s TID:0x%016lx".
# The notice dump that follows holds the key that was sent.
_BAD_M_KEY_TRAP = re.compile(r"Received Generic Notice type:\d+ num:256\b")

# OpenSM 5.17's "ERR 7549: allowed_sm_guids param is not valid. Should be
# GUIDs in comma separated values format. Disabling the feature": a list that
# OpenSM rejected can never pass as protection, so it fails a start or an
# apply (specs/secured-profile.md, Overrides).
_ALLOWED_SM_REJECTED = re.compile(r"ERR 7549: allowed_sm_guids")
# "ERR 0F09: Ignoring SM on port 0x%lx, is not part of the allowed port GUIDs"
_IGNORED_SM = re.compile(r"ERR 0F09: Ignoring SM on port 0x([0-9a-fA-F]{1,16})\b")
# A port that answered a class key manager's KeyInfo with an error status, as
# the VS manager (9828) and the congestion control manager (C10A) log it:
# "Received invalid status from port GUID 0x%lx, status:0x%x, attr_id:0x%x"
_KEY_REJECTED = re.compile(
    r"ERR (9828|C10A): Received invalid status from port GUID "
    r"0x([0-9a-fA-F]{1,16}), status:0x([0-9a-fA-F]{1,8}), attr_id:0x([0-9a-fA-F]{1,8})"
)
# A key manager that failed to set keys: "failed to update port guid 0x%016lx
# vskeyinfo" (9803, 9822), "... n2nkeyinfo" (9B03, 9B22), "failed to update
# port (guid 0x%016lx) by CC keyinfo" (C124), "VS keys configuration has
# failed" (9823)
_KEY_FAILED = re.compile(
    r"ERR (9803|9822|9B03|9B22|C124): failed to update port \(?guid "
    r"0x([0-9a-fA-F]{1,16})"
)
_KEYS_FAILED = re.compile(r"ERR (9823): VS keys configuration has failed")
# OpenSM 5.17's "ERR 4C11: Number of sent SA queries %u for GUID 0x%lx Port
# 0x%lx is bigger than sa_rate_threshold: %u"
_SA_LIMITED = re.compile(
    r"ERR 4C11: Number of sent SA queries (\d+) for GUID 0x([0-9a-fA-F]{1,16}) "
    r"Port 0x([0-9a-fA-F]{1,16}) is bigger than sa_rate_threshold: (\d+)"
)
# A line of the daemon's log with timestamps: RFC 3339, nanoseconds at most
_LOG_TIME = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)"
)

# OpenSM's log holds keys: a trap's notice dump, an SMP dump, an SA key
# mismatch ("SM_Key: 0x%lx != SM_Key: 0x%lx") and debug lines about sending
# an SMP all print one. Whatever names a key has its value masked; lines about
# Bad M_Key traps, and "Sending"/"Setting ... MKey 0x..." lines, are dropped.
_KEY_VALUE = re.compile(
    r"(?i)(\b(?:m_?key|sm_?key|sa_?key|vs_?key|cc_?key|n2n_?key|am_?key|"
    r"key_mgr_seed)\b[\s.:=]*)(?:0x)?[0-9a-f]+\b"
)
_DROPPED = re.compile(
    r"Bad M_Key|\bnum:256\b|\b(?:Sending|Setting)\b.*\bMKey 0x", re.IGNORECASE
)
# Seconds each prober Get may take, and how many run at once
PROBE_TIMEOUT = 3
PROBE_PARALLEL = 16
# The files of OpenSM's cache that the manager reads and renders
CACHE_FILES = ("neighbors", "guid2mkey")
_CACHE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")


def _mask_keys(line):
    """A line of OpenSM's that may be logged or put into an exception: its
    keys masked, or None if it mustn't be shown at all"""
    if _DROPPED.search(line):
        return None
    return _KEY_VALUE.sub(r"\1<masked>", line)


def _shown(line):
    """A line of OpenSM's as it may be shown"""
    masked = _mask_keys(line.strip())
    return "<a line about an M_Key, not shown>" if masked is None else masked


def _check_allowed_sm(line):
    if _ALLOWED_SM_REJECTED.search(line):
        raise ReloadError(
            "OpenSM rejected allowed_sm_guids (ERR 7549) and disabled the list, "
            "so it would ignore no SM"
        )


def _log_time(stamp):
    """A log line's timestamp, in nanoseconds since the epoch, or None"""
    match = _LOG_TIME.fullmatch(stamp)
    if not match:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    try:
        when = datetime.datetime.fromisoformat(match.group(1) + zone)
    except ValueError:
        return None
    return int(when.timestamp()) * 10**9 + int((match.group(2) or "").ljust(9, "0"))


def _note_finding(findings, line):
    match = _IGNORED_SM.search(line)
    if match:
        findings.ignored_sms.append(int(match.group(1), 16))
        return
    match = _KEY_REJECTED.search(line)
    if match:
        code, guid, status, attr = match.groups()
        findings.key_rejections.append(
            (code, int(guid, 16), int(status, 16), int(attr, 16))
        )
        return
    match = _KEY_FAILED.search(line)
    if match:
        findings.key_errors.append((match.group(1), int(match.group(2), 16)))
        return
    match = _KEYS_FAILED.search(line)
    if match:
        findings.key_errors.append((match.group(1), None))
        return
    match = _SA_LIMITED.search(line)
    if match:
        count, guid, port, threshold = match.groups()
        findings.sa_limited.append(
            (int(guid, 16), int(port, 16), int(count), int(threshold))
        )


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
        f"OpenSM couldn't read the partitions file: {_shown(line)}",
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
            LOG.warning("OpenSM couldn't set a pkey table: %s", _shown(line))
        return
    # What can't be traced fails closed: it holds every job of the sweep.
    if code in ENFORCEMENT_ERROR_CODES:
        ports = _SWITCH_PORT.findall(line)
        for switch, port in ports:
            report.enforcement_errors.add(f"{int(switch, 16):016x}:{port}")
        if not ports:
            report.enforcement_errors.add(UNATTRIBUTED)
        LOG.error("OpenSM cleared a switch port's enforcement: %s", _shown(line))
    elif code in PKEY_ERROR_CODES:
        guids = {int(g, 16) for g in _PORT_GUID.findall(line)}
        report.pkey_errors |= guids
        if not guids:
            report.unattributed = True
        LOG.warning("OpenSM couldn't set a pkey table: %s", _shown(line))


def _note_fabric_error(report, line):
    """Record what OpenSM logged about ports it couldn't reach, and Bad
    M_Key traps, which are counted and never logged

    Only collected: what they hold is the manager's decision.
    """
    if _BAD_M_KEY_TRAP.search(line):
        report.bad_m_key_traps += 1
    elif _DR_SEND_ERROR.search(line):
        report.dr_errors = True
    else:
        match = _UNREACHED.search(line)
        if match:
            report.unreached.add(int(match.group(1), 16))


def _note(report, line):
    _note_pkey_error(report, line)
    _note_fabric_error(report, line)


def _untar(data, name):
    """A cache file's text and modification time, from get_archive's tar"""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            member = tar.next()
            if member is None or member.name != name or not member.isfile():
                raise ReloadError(f"OpenSM's cache file {name} isn't a regular file")
            text = tar.extractfile(member).read().decode("ascii")
            mtime = datetime.datetime.fromtimestamp(member.mtime, datetime.timezone.utc)
    except (tarfile.TarError, UnicodeDecodeError, ValueError, OverflowError):
        # The file holds keys: the reason never quotes it.
        raise ReloadError(f"OpenSM's cache file {name} can't be read") from None
    return text, mtime


def _tar(files):
    """A tar of OpenSM's cache files, each root's and mode 0600, since
    guid2mkey holds keys"""
    data = io.BytesIO()
    now = time.time()
    with tarfile.open(fileobj=data, mode="w") as tar:
        for name, text in files.items():
            if not _CACHE_NAME.fullmatch(name):
                raise ValueError(f"{name!r} isn't a file name in OpenSM's cache")
            body = text.encode("ascii")
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = now
            tar.addfile(info, io.BytesIO(body))
    return data.getvalue()


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

    def fabric(self, m_key=None, sa_key=None, switches=None):
        """Ask the fabric, from OpenSM's container, for its switches and the
        subnet managers its SA knows

        switches, a set of switch node GUIDs, is given instead when the
        prober has found them: with per-port keys, ibswitches -y <m_key>
        can't reach a re-keyed port.
        """
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} isn't running"
                )
            if switches is None:
                m_key_args = ["-y", m_key] if m_key else []
                listed = self._exec(container, ["ibswitches", *m_key_args])
            sm_records = self._exec(
                container, ["saquery", *(["--smkey", sa_key] if sa_key else []), "SMIR"]
            )
            ports = self._exec(container, ["ibstat", "-p"])
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e
        if switches is None:
            switch_guids = {
                int(g, 16)
                for g in re.findall(r"^Switch\s*:\s*0x([0-9a-fA-F]+)", listed, re.M)
            }
        else:
            switch_guids = set(switches)
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
            # saquery's SMInfo records hold SM_Keys.
            shown = " / ".join(_shown(line) for line in output.strip().splitlines())
            raise ReloadError(f"{command[0]} failed: {shown[:200]}")
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
                        f"OpenSM couldn't read the partitions file: {_shown(line)}"
                    )
                if handled:
                    # OpenSM re-reads its options at SIGHUP.
                    _check_allowed_sm(line)
                if started:
                    _note(report, line)
                if HUP_MARKER in line:
                    handled = True
                elif handled and any(m in line for m in SWEEP_START_MARKERS):
                    started = True
                elif started and any(m in line for m in DONE_MARKERS):
                    LOG.debug("OpenSM: %s", _shown(line.strip(" *")))
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

    # Whether start() pulls a missing image. Not in HA mode, where a start
    # mustn't wait on a registry while it holds the lease.
    pulls_on_start = True

    def __init__(self, config, client=None):
        super().__init__(config.container, config.sweep_timeout, client)
        self.config = config
        # How far log_findings() has read: (container ID, the last line's
        # timestamp in nanoseconds)
        self._log_mark = None

    def _run_kwargs(self):
        config = self.config
        volumes = [f"{config.host_config_dir}:{opensm_conf.CONTAINER_CONFIG_DIR}:ro"]
        volumes += parse_volumes(config.volumes)
        kwargs = {
            "image": config.image,
            # After the image's own -F, so that this one is used
            "command": ["-F", f"{opensm_conf.CONTAINER_CONFIG_DIR}/opensm.conf"],
            "name": config.container,
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
        # docker-py makes a 404 ImageNotFound only by its message's wording,
        # which another API, such as Podman's, may not match
        except docker.errors.NotFound:
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
        # With its anonymous volumes: one for each volume of the image that
        # [opensm] volumes doesn't name, which a start retried at every
        # check would otherwise leave behind each time.
        with contextlib.suppress(docker.errors.NotFound):
            self.client.containers.get(self.container_name).remove(force=True, v=True)

    def scratch_mounts(self):
        """A tmpfs over each volume OpenSM's image declares (/var/log and
        /var/cache/opensm), for a helper container of that image

        Without a mount there, Docker gives each such path an anonymous
        volume, which removing the container leaves behind unless asked:
        a helper run every few seconds, as the reach probe is, filled a
        host's Docker disk with them. A tmpfs leaves nothing, however the
        container ends. Raises DockerException if the image can't be read,
        rather than run the helper with nothing over its volumes."""
        image = self.client.images.get(self.config.image)
        config = (getattr(image, "attrs", None) or {}).get("Config") or {}
        return {path.rstrip("/") or "/": "" for path in config.get("Volumes") or {}}

    @property
    def prober_name(self):
        """The helper prober's container: a fixed name, so that one left
        behind, whose configuration holds the plan and its keys, is found
        and removed"""
        return f"{self.container_name}-prober"

    def _remove_prober(self):
        with contextlib.suppress(docker.errors.NotFound):
            self.client.containers.get(self.prober_name).remove(force=True, v=True)

    def start(self, prepare=None):
        """Start OpenSM, with its files already written

        The container is created; then prepare(container), if given, may
        read and write OpenSM's cache in its volume before OpenSM starts
        (read_cache, write_cache, and the helper prober); then it's started.
        Creating a container never pulls its image, as running one did, so
        a single-node start pulls a missing one first: nothing else there
        would bring back an image that was pruned while the manager ran.
        """
        try:
            if self.pulls_on_start:
                self.ensure_image()
            # A container left by an earlier manager has stopped, since it
            # was in that manager's PID namespace, but its name is taken.
            self._remove()
            self._remove_prober()
            container = self.client.containers.create(**self._run_kwargs())
            if prepare is not None:
                prepare(container)
            container.start()
            LOG.info("Started OpenSM container %s", self.container_name)
            return self._wait_for_start(container)
        except (docker.errors.DockerException, ReloadError) as e:
            # An OpenSM that didn't start properly, for example one that
            # rejected part of its partitions file, mustn't keep running,
            # and one whose cache wasn't prepared mustn't start.
            self.stop()
            if isinstance(e, ReloadError):
                raise
            raise ReloadError(f"Docker: {e}") from e
        except Exception:
            self.stop()
            raise

    def _wait_for_start(self, container):
        """Wait for OpenSM's first sweep as master, which reads the
        partitions file, and report its pkey errors"""
        deadline = time.monotonic() + self.config.start_timeout
        while time.monotonic() < deadline:
            lines = self._log(container)
            report = SweepReport()
            for line in lines:
                _note(report, line)
                _check_allowed_sm(line)
                if "PARSE ERROR" in line:
                    raise _rejected(line)
                if _is_partitions_error(line):
                    raise ReloadError(
                        f"OpenSM couldn't read the partitions file: {_shown(line)}"
                    )
                if STANDBY_MARKER in line:
                    raise ReloadError(
                        "OpenSM entered STANDBY: another subnet manager is master"
                    )
                if any(m in line for m in STARTED_MARKERS):
                    LOG.info("OpenSM: %s", _shown(line.strip(" *")))
                    return report
            container.reload()
            if container.status not in ("created", "running"):
                shown = [_mask_keys(line.strip()) for line in lines]
                last = [line for line in shown if line is not None][-5:]
                raise ReloadError(f"OpenSM exited while starting: {' / '.join(last)}")
            time.sleep(POLL_INTERVAL)
        raise ReloadError(
            f"OpenSM didn't start within {self.config.start_timeout} seconds"
        )

    # OpenSM's cache and the prober (specs/secured-profile.md, Per-port
    # M_Keys). guid2mkey and the prober's environment hold keys, so none of
    # what they carry is logged or put into an exception.

    def read_cache(self, container=None):
        """neighbors and guid2mkey from OpenSM's cache, as {name: (text,
        modification time)}, the time an aware UTC datetime

        A file that doesn't exist is absent. get_archive works on a created
        container, before OpenSM starts, and on a running one; container is
        OpenSM's by default.
        """
        try:
            if container is None:
                container = self.client.containers.get(self.container_name)
            files = {}
            for name in CACHE_FILES:
                try:
                    stream, _stat = container.get_archive(f"{OPENSM_CACHE_DIR}/{name}")
                    data = b"".join(stream)
                except docker.errors.NotFound:
                    continue
                files[name] = _untar(data, name)
            return files
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: can't read OpenSM's cache: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ReloadError(
                f"Docker: can't read OpenSM's cache: {type(e).__name__}"
            ) from e

    def write_cache(self, container, files):
        """Replace files of OpenSM's cache in a created container's volume
        with these, {name: text}, each root's and mode 0600"""
        data = _tar(files)
        try:
            written = container.put_archive(OPENSM_CACHE_DIR, data)
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: can't write OpenSM's cache: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ReloadError(
                f"Docker: can't write OpenSM's cache: {type(e).__name__}"
            ) from e
        if not written:
            raise ReloadError("Docker didn't write OpenSM's cache")

    def prober(self, helper=False, timeout=PROBE_TIMEOUT, parallel=PROBE_PARALLEL):
        """A runner for prober.verify: run(gets) -> (own_guids, answers),
        one batch of Gets in parallel, which raises ReloadError (ProbeError
        for what the script did) if it fails

        By default the script runs in the running OpenSM's container; with
        helper, in a container of its own from OpenSM's image, as before a
        start, when no OpenSM runs. The Gets and their keys travel in the
        script's environment, never its command line.
        """

        def run(gets):
            environment = probes.environment(gets, timeout, parallel)
            if helper:
                # Each Get may take the timeout, a batch of them at a time
                batches = -(-len(gets) // parallel)
                wait = batches * (timeout + 2) + 30
                code, output = self._probe_in_helper(environment, wait)
            else:
                code, output = self._probe_in_opensm(environment)
            if code != 0:
                raise probes.ProbeError(f"the prober exited with status {code}")
            try:
                text = output.decode("ascii")
            except (AttributeError, UnicodeDecodeError):
                raise probes.ProbeError("the prober's output isn't text") from None
            return probes.parse(text, gets)

        return run

    def _probe_in_opensm(self, environment):
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} isn't running"
                )
            return container.exec_run(
                ["sh", "-c", probes.SCRIPT],
                environment=environment,
                stdout=True,
                stderr=False,
            )
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: can't run the prober: {e}") from e

    def _probe_in_helper(self, environment, wait):
        return self._run_helper(probes.SCRIPT, environment, wait)

    def _run_helper(self, script, environment, wait):
        """Run script by sh in a helper container of OpenSM's image, as
        the prober's: (status, stdout)"""
        # Created, not run: containers.run() would pull a missing image, and
        # a start mustn't wait on a registry.
        try:
            present = self.has_image()
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: {e}") from e
        if not present:
            raise ReloadError(f"OpenSM's image {self.config.image} isn't on this host")
        container = None
        try:
            self._remove_prober()
            container = self.client.containers.create(
                name=self.prober_name,
                image=self.config.image,
                entrypoint=["sh", "-c"],
                command=[script],
                environment=environment,
                network_mode="host",
                privileged=True,
                tmpfs=self.scratch_mounts(),
            )
            container.start()
            code = container.wait(timeout=wait)["StatusCode"]
            return code, container.logs(stdout=True, stderr=False)
        except (
            docker.errors.DockerException,
            requests.exceptions.RequestException,
        ) as e:
            raise ReloadError(
                f"Docker: can't run the prober: {type(e).__name__}"
            ) from e
        finally:
            if container is not None:
                try:
                    container.remove(force=True, v=True)
                except (
                    docker.errors.DockerException,
                    requests.exceptions.RequestException,
                ) as e:
                    LOG.warning("Can't remove the prober's container: %s", e)

    # Allowed SMs (specs/secured-profile.md, Allowed SM GUIDs)

    def physical_ports(self):
        """The port GUIDs of this host's physical InfiniBand ports, as a set
        of ints, read from sysfs in a helper container of OpenSM's image,
        before OpenSM starts; raises ReloadError if it can't tell

        It runs before start(), which pulls a missing image on a single
        node, so it pulls one first too: otherwise a start retried after a
        failed pull would never get that far.
        """
        if self.pulls_on_start:
            try:
                self.ensure_image()
            except (
                docker.errors.DockerException,
                requests.exceptions.RequestException,
            ) as e:
                raise allowed_sm.ProbeError(
                    f"can't pull OpenSM's image: {type(e).__name__}"
                ) from e
        code, output = self._run_helper(
            allowed_sm.PORTS_SCRIPT, {}, allowed_sm.PROBE_TIMEOUT
        )
        if code != 0:
            raise allowed_sm.ProbeError(f"the port probe exited with status {code}")
        try:
            text = output.decode("ascii")
        except (AttributeError, UnicodeDecodeError):
            raise allowed_sm.ProbeError("the port probe's output isn't text") from None
        return allowed_sm.parse_ports(text)

    def log_findings(self):
        """What the running OpenSM logged since the last call, as a
        LogFindings: the first call for a container reads its whole log

        Lines are taken by the daemon's timestamps, so that each is judged
        once. Raises ReloadError if the log can't be read.
        """
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} isn't running"
                )
            mark = self._log_mark
            last = mark[1] if mark is not None and mark[0] == container.id else 0
            kwargs = {"stdout": True, "stderr": True, "timestamps": True}
            if last:
                kwargs["since"] = last // 10**9
            text = container.logs(**kwargs).decode(errors="replace")
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: can't read OpenSM's log: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ReloadError(
                f"Docker: can't read OpenSM's log: {type(e).__name__}"
            ) from e
        findings = LogFindings()
        newest = last
        for line in text.splitlines():
            stamp, _, message = line.partition(" ")
            when = _log_time(stamp)
            if when is not None:
                if when <= last:
                    continue
                newest = max(newest, when)
            else:
                # Judged rather than skipped: a line judged twice repeats an
                # alarm or a contest, one skipped could hide an SM.
                message = line
            _note_finding(findings, message)
        self._log_mark = (container.id, newest)
        return findings

    def query_sm_port(self, guid, m_key):
        """Query a known SM port directly, from OpenSM's container: SMInfo's
        state, and PortInfo's capability mask, as an allowed_sm.PortQuery.
        The key travels in the script's environment, never its command
        line, and only those two values leave the container."""
        environment = {
            "AFM_GUID": f"0x{guid:016x}",
            "AFM_KEY": m_key,
            "AFM_QUERY_TIMEOUT": str(allowed_sm.QUERY_TIMEOUT),
        }
        try:
            container = self.client.containers.get(self.container_name)
            if container.status != "running":
                raise ReloadError(
                    f"OpenSM container {self.container_name} isn't running"
                )
            code, output = container.exec_run(
                ["sh", "-c", allowed_sm.QUERY_SCRIPT],
                environment=environment,
                stdout=True,
                stderr=False,
            )
        except docker.errors.DockerException as e:
            raise ReloadError(f"Docker: can't query the port: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ReloadError(
                f"Docker: can't query the port: {type(e).__name__}"
            ) from e
        if code != 0:
            raise ReloadError(f"the port query exited with status {code}")
        try:
            text = output.decode("ascii")
        except (AttributeError, UnicodeDecodeError):
            raise ReloadError("the port query's output isn't text") from None
        return allowed_sm.parse_query(text)

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
        try:
            self._remove_prober()
        except docker.errors.DockerException as e:
            LOG.warning("Can't remove the prober's container: %s", e)
