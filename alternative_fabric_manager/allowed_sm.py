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

"""Allowed SMs in the single-node manager: the list OpenSM gets, the checks
that refuse a list that would lock OpenSM out, what an SM that OpenSM
ignores means for the fabric check, and the operator's commands

specs/secured-profile.md, Allowed SM GUIDs, is the source of truth. The
group is experimental, untested on hardware: hardware tests 1 and 2 decide
parts of its rules, and HA mode refuses it until its dependencies are in.

It acts only while the list is in effect (the allowed_sm group named, or
[opensm_conf] allowed_sm_guids set), in single node with the managed
driver, which Config.validate requires. Otherwise every hook returns at
once.

The scripts here run in OpenSM's image. The port probe reads sysfs only.
The query sends one SMInfo and one PortInfo Get to a port, with its M_Key
on the tools' command lines, as the prober's smpquery does, never on the
script's own; only the SMInfo state and the capability mask leave the
container, never a key.
"""

import contextlib
import dataclasses
import datetime
import logging
import re

import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, port_keys
from alternative_fabric_manager.drivers import ReloadError

LOG = logging.getLogger(__name__)

GROUP = "allowed_sm"
OPTION = "allowed_sm_guids"
# Seconds the probe, and each Get of the query, may take
PROBE_TIMEOUT = 30
QUERY_TIMEOUT = 5
# SMInfo's state NOT-ACTIVE, and PortInfo CapabilityMask's IsSM bit
NOT_ACTIVE = 0
IS_SM = 0x2
# How many GUIDs a log line or a check's detail names before "and N more"
SHOWN = 5

# The port probe. It prints "PORT <device> <port> <GID 0>" for each port of
# a physical function with an InfiniBand link layer, then "END". A device
# with a physfn link is a virtual function, and one whose device is an
# mlx5_core.sf auxiliary device is a sub-function: neither is this host's
# own. GID 0 is the subnet prefix and the port GUID, whatever the port's
# state, as ibstat reads it. AFM_SYSFS is for tests only.
PORTS_SCRIPT = r"""exec 2>/dev/null </dev/null
set -u
for tool in readlink cat; do
    command -v "$tool" >/dev/null || exit 3
done
root=${AFM_SYSFS:-/sys/class/infiniband}
cd "$root" || exit 4
for dev in *; do
    [ -d "$dev/ports" ] || continue
    [ -e "$dev/device/physfn" ] && continue
    path=$(readlink -f "$dev/device") || exit 5
    case ${path##*/} in mlx5_core.sf.*) continue ;; esac
    for port in "$dev"/ports/*; do
        [ -d "$port" ] || continue
        layer=$(cat "$port/link_layer") || exit 5
        [ "$layer" = InfiniBand ] || continue
        gid=$(cat "$port/gids/0") || exit 5
        printf 'PORT %s %s %s\n' "$dev" "${port##*/}" "$gid"
    done
done
echo END
"""

# The direct query of a known SM port, by its GUID, which sminfo and
# smpquery turn into a LID through the SA (-G). It takes everything from its
# environment: AFM_GUID and AFM_KEY, each 0x and 16 hex digits, and
# AFM_QUERY_TIMEOUT. It prints "STATE <n>" if the port's SM answered SMInfo,
# and "CAPMASK <0x...>" if the port answered PortInfo, and exits 0 even if
# neither answered: no answer clears nothing. stderr goes nowhere, since it
# may echo the key.
QUERY_SCRIPT = r"""exec 2>/dev/null </dev/null
set -u
for tool in sminfo smpquery timeout awk grep; do
    command -v "$tool" >/dev/null || exit 3
done
printf '%s\n' "${AFM_GUID-}" | grep -Eqx '0x[0-9a-f]{16}' || exit 2
printf '%s\n' "${AFM_KEY-}" | grep -Eqx '0x[0-9a-f]{16}' || exit 2
T=${AFM_QUERY_TIMEOUT-}
case $T in ''|*[!0-9]*) exit 2 ;; esac
[ "$T" -gt 0 ] || exit 2
if out=$(timeout -k 1 "$T" sminfo -G -y "$AFM_KEY" "$AFM_GUID"); then
    printf '%s\n' "$out" | awk '
        match($0, / state [0-9]+( |$)/) {
            v = substr($0, RSTART + 7, RLENGTH - 7)
            sub(/ $/, "", v)
            print "STATE " v
            exit
        }'
fi
if out=$(timeout -k 1 "$T" smpquery -G -y "$AFM_KEY" portinfo "$AFM_GUID"); then
    printf '%s\n' "$out" | awk -F: '
        $1 == "CapMask" {
            v = $2
            sub(/^\.+/, "", v)
            if (v ~ /^0x[0-9a-fA-F]{1,8}$/) print "CAPMASK " v
            exit
        }'
fi
exit 0
"""

_PORT_LINE = re.compile(
    r"PORT ([A-Za-z0-9_.-]{1,64}) ([0-9]{1,3}) "
    r"((?:[0-9a-fA-F]{4}:){7}[0-9a-fA-F]{4})"
)


class AllowedSMError(Exception):
    """An operator's command that can't be done"""


class ProbeError(ReloadError):
    """The port probe couldn't tell which physical ports this host has"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _guids(guids):
    """GUIDs for a message, at most SHOWN of them"""
    guids = sorted(guids)
    shown = ", ".join(f"0x{guid:016x}" for guid in guids[:SHOWN])
    if len(guids) > SHOWN:
        shown += f" and {len(guids) - SHOWN} more"
    return shown


def parse_ports(output):
    """The port GUIDs of PORTS_SCRIPT's output, as a set of ints

    Raises ProbeError for output that isn't the script's, or that isn't
    complete: a probe that can't be read is no evidence of which ports
    this host has.
    """
    lines = output.splitlines()
    if not lines or lines[-1] != "END":
        raise ProbeError("the port probe's output is incomplete")
    guids = set()
    for line in lines[:-1]:
        match = _PORT_LINE.fullmatch(line)
        if not match:
            raise ProbeError("the port probe's output isn't what it prints")
        guid = int("".join(match.group(3).split(":")[4:]), 16)
        if guid in (0, 0xFFFFFFFFFFFFFFFF):
            raise ProbeError(
                f"port {match.group(2)} of {match.group(1)} has no port GUID"
            )
        guids.add(guid)
    return guids


@dataclasses.dataclass(frozen=True)
class PortQuery:
    """What the direct query of a known SM port showed: SMInfo's state, and
    PortInfo's capability mask, each None if it didn't answer"""

    state: int | None = None
    capmask: int | None = None

    @property
    def clear(self):
        """Whether it shows that no SM acts there: NOT-ACTIVE, or no SM
        bound to the port"""
        return self.state == NOT_ACTIVE or (
            self.capmask is not None and not self.capmask & IS_SM
        )

    def __str__(self):
        parts = []
        if self.state is not None:
            parts.append(f"SMInfo state {self.state}")
        if self.capmask is not None:
            parts.append(f"IsSM {'set' if self.capmask & IS_SM else 'clear'}")
        return ", ".join(parts) or "no answer"


def parse_query(output):
    """QUERY_SCRIPT's output as a PortQuery; anything else in it is ignored,
    and a line seen twice keeps its first value"""
    state = capmask = None
    for line in output.splitlines():
        match = re.fullmatch(r"STATE ([0-9]{1,2})", line)
        if match and state is None:
            state = int(match.group(1))
            continue
        match = re.fullmatch(r"CAPMASK (0x[0-9a-fA-F]{1,8})", line)
        if match and capmask is None:
            capmask = int(match.group(1), 16)
    return PortQuery(state, capmask)


def lockout_problem(listed, own, bound=None):
    """Why OpenSM, given this list, might not run on this host, or None

    listed is the effective list, () for 0; own is this host's physical
    ports as the probe read them now; bound is [opensm_conf] guid, the port
    OpenSM binds to, if set. See specs/secured-profile.md, Refusing a list
    that would lock AFM out.
    """
    if not own:
        return (
            "the port probe found no physical InfiniBand port on this host, so "
            "it can't tell which ports OpenSM may run on"
        )
    if not listed:
        # 0: no other SM. OpenSM's own port is expected to be allowed, which
        # hardware test 1 checks.
        return None
    if bound is not None:
        try:
            port = int(str(bound).strip(), 0)
        except ValueError:
            return f"[opensm_conf] guid {bound!r} isn't a GUID"
        if port not in listed:
            return (
                f"the list leaves out 0x{port:016x}, the port [opensm_conf] guid "
                "binds OpenSM to"
            )
        return None
    missing = set(own) - set(listed)
    if missing:
        return (
            f"the list leaves out this host's physical ports {_guids(missing)}, "
            "any of which OpenSM may bind to"
        )
    return None


class AllowedSMs:
    def __init__(self, manager):
        self.manager = manager
        # Known SM ports that OpenSM ignores, until a direct query shows no
        # SM acts there: the fabric is contested meanwhile
        self._contested = set()
        # Other ports OpenSM ignores, alarmed once for the running OpenSM
        self._alarmed = set()

    @property
    def config(self):
        return self.manager.config

    @property
    def driver(self):
        return self.manager.driver

    @contextlib.contextmanager
    def _session(self):
        with self.manager._lock, self.manager.sessionmaker() as session:
            yield session

    def active(self):
        """Whether OpenSM gets a list: in single node, with the managed
        driver, which Config.validate requires"""
        return (
            self.config.allowed_sm_in_effect
            and not self.config.ha
            and self.config.driver == "managed"
        )

    @property
    def contested(self):
        """The known SM ports OpenSM ignores, until a query clears them"""
        return frozenset(self._contested)

    def _keys_overridden(self):
        """Whether a key is the operator's, held by SMs AFM can't know"""
        return any(name in self.config.opensm_conf for name in ("m_key", "sm_key"))

    # Before a start

    def prepare(self):
        """The list for this start, in the manager's spelling, once this
        host's ports are read and recorded as known SM ports

        Raises ReloadError, with an alarm logged, if the probe fails or the
        list would lock OpenSM out: the start is refused, and retried at the
        next check.
        """
        try:
            own = self.driver.physical_ports()
        except ReloadError as e:
            message = (
                f"Not starting OpenSM: can't read this host's physical "
                f"InfiniBand ports, so it can't tell whether {OPTION} lets "
                f"OpenSM run here ({e}); retrying at every check"
            )
            LOG.error("%s", message)
            raise ProbeError(message) from None
        if own:
            self._record_own(own)
        override = self.config.opensm_conf.get(OPTION)
        if override is not None:
            # Config.validate refused one it can't read.
            listed = config_mod.parse_allowed_sm_guids(override)
        else:
            listed = tuple(sorted(own))
        problem = lockout_problem(listed, own, self.config.opensm_conf.get("guid"))
        if problem is not None:
            message = (
                f"Not starting OpenSM: {OPTION} would lock it out: {problem}. "
                "Retrying at every check"
            )
            LOG.error("%s", message)
            raise ReloadError(message)
        value = config_mod.format_allowed_sm_guids(listed)
        LOG.info(
            "OpenSM ignores every SM outside %s: %s",
            OPTION,
            "every other SM" if not listed else _guids(listed),
        )
        # A new OpenSM: each ignored SM is alarmed again. A contest stays
        # until a query clears it.
        self._alarmed.clear()
        return value

    def _record_own(self, own):
        """Record this host's ports as known SM ports, and as its own; a port
        no longer found stays known, but isn't this host's"""
        texts = {port_keys.guid_text(guid) for guid in own}
        with self._session() as session:
            rows = {row.guid: row for row in session.scalars(sa.select(db.KnownSMPort))}
            for text in texts - set(rows):
                session.add(db.KnownSMPort(guid=text, own=True, recorded=_now()))
            for text, row in rows.items():
                row.own = text in texts
            session.commit()

    # The fabric check

    def check(self, findings):
        """Judge the SMs OpenSM logged ignoring since the last check, given
        the driver's LogFindings: the check's problems, and whether the
        fabric is contested

        The manager reads the log, and a log that can't be read fails the
        check as a failed query: an SM ignored meanwhile would be missed.
        """
        if not self.active() or findings is None:
            return [], False
        with self._session() as session:
            known = {
                int(row.guid, 16) for row in session.scalars(sa.select(db.KnownSMPort))
            }
        overridden = self._keys_overridden()
        if not overridden:
            # --drop-sm-port, beside the running manager, asserts that the
            # port's adapter is no longer in a host with the keys.
            for guid in self._contested - known:
                LOG.warning(
                    "Port 0x%016x is no longer a known SM port, so it no longer "
                    "contests the fabric",
                    guid,
                )
            self._contested &= known
        if findings.ignored_sms:
            for guid in findings.ignored_sms:
                if guid in known or overridden:
                    if guid not in self._contested:
                        LOG.error(
                            "OpenSM ignores a subnet manager on port 0x%016x (ERR "
                            "0F09), %s: the fabric is contested until a query of "
                            "the port shows its SM NOT-ACTIVE, or no SM bound there",
                            guid,
                            "a known SM port, whose host may hold the fabric's keys"
                            if guid in known
                            else "and the fabric's keys are set in [opensm_conf], "
                            "so any SM may hold them",
                        )
                    self._contested.add(guid)
                elif guid not in self._alarmed:
                    LOG.error(
                        "OpenSM ignores a subnet manager on port 0x%016x (ERR 0F09), "
                        "which isn't a known SM port: it can't hold the fabric's "
                        "keys, so OpenSM stays master, but find out whose it is",
                        guid,
                    )
                    self._alarmed.add(guid)
        for guid in sorted(self._contested):
            result = self._query(guid)
            if result is not None and result.clear:
                LOG.warning(
                    "No subnet manager acts on port 0x%016x any more (%s): it "
                    "no longer contests the fabric",
                    guid,
                    result,
                )
                self._contested.discard(guid)
        if not self._contested:
            return [], False
        return [
            f"OpenSM ignores subnet managers on {_guids(self._contested)}, which "
            "may hold the fabric's keys (ERR 0F09): contested until a query shows "
            "each NOT-ACTIVE, or no SM bound there"
        ], True

    def _query(self, guid):
        """The direct query of a contested port, or None if it couldn't be
        sent: neither clears the contest"""
        key = self._port_key(guid)
        if key is None:
            LOG.warning(
                "Can't query port 0x%016x: which M_Key it holds isn't known", guid
            )
            return None
        try:
            return self.driver.query_sm_port(guid, key)
        except ReloadError as e:
            LOG.warning("Can't query port 0x%016x: %s", guid, e)
            return None

    def _port_key(self, guid):
        """The M_Key the port holds, as 0x%016x: with per-port M_Key rows,
        its row's; otherwise the M_Key OpenSM uses. None if it isn't known,
        so that a tenant's port is never sent the stored M_Key by guess."""
        keeper = self.manager.port_keeper
        if keeper.active():
            with self._session() as session:
                row = session.get(db.FabricPort, port_keys.guid_text(guid))
            if row is not None:
                m_key, base = keeper._m_keys()
                if row.holds == port_keys.DERIVED:
                    return port_keys.format_key(port_keys.port_m_key(base, guid))
                if row.holds == port_keys.M_KEY:
                    return port_keys.format_key(m_key)
                return None
        m_key = self.manager._key("m_key")
        if not m_key:
            return None
        return port_keys.format_key(port_keys.parse_key(m_key))

    # The operator's commands

    def _refuse_ha(self):
        if self.config.ha:
            raise AllowedSMError("allowed SMs aren't available in HA mode")

    def known_ports(self):
        """--known-sm-ports: each known SM port, and whether it's this
        host's, as the last start's probe found"""
        self._refuse_ha()
        with self._session() as session:
            rows = sorted(
                session.scalars(sa.select(db.KnownSMPort)), key=lambda r: r.guid
            )
            return [
                f"0x{row.guid} {'this host' if row.own else 'recorded'}" for row in rows
            ]

    def record(self, guid):
        """--record-sm-port: a port whose SM may hold the fabric's keys, so
        that OpenSM ignoring it contests the fabric; False if it was
        known already"""
        self._refuse_ha()
        text = port_keys.guid_text(guid)
        with self._session() as session:
            if session.get(db.KnownSMPort, text) is not None:
                return False
            session.add(db.KnownSMPort(guid=text, own=False, recorded=_now()))
            session.commit()
        return True

    def drop(self, guid):
        """--drop-sm-port: forget a known SM port, whose adapter is no longer
        in a host with the fabric's keys"""
        self._refuse_ha()
        text = port_keys.guid_text(guid)
        with self._session() as session:
            row = session.get(db.KnownSMPort, text)
            if row is None:
                raise AllowedSMError(f"0x{text} isn't a known SM port")
            if row.own:
                raise AllowedSMError(
                    f"0x{text} is this host's port, as the last start found it: "
                    "OpenSM may run there"
                )
            session.delete(row)
            session.commit()
