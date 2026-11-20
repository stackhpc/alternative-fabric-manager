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

"""The prober: directed-route Gets along recorded links, with per-port keys

specs/secured-profile.md, Per-port M_Keys, The design (The prober, Before
each start, The fabric check, Lost ports and jobs) is the source of truth
for every rule here. Three layers:

- SCRIPT, a POSIX sh script run in OpenSM's image, which sends the Gets
  with smpquery and prints only whitelisted fields, and plan() and parse(),
  its wire format;
- a runner, run(gets) -> (own_guids, answers), which the Docker driver
  supplies: one batch of Gets, in parallel inside a container;
- verify(), which decides which port is sent which key, and what the
  answers mean for the rows. It is pure given a runner, so that every rule
  can be tested against a simulated fabric.

A key opens a port, so nothing here logs a key, and no exception message,
repr or result holds one. Keys reach smpquery's command line, as
ibswitches -y does today, but never the script's own: they travel in its
environment.
"""

import dataclasses
import re
import time

from alternative_fabric_manager import port_keys
from alternative_fabric_manager.drivers import ReloadError

# What a Get may ask for
NODEINFO = "nodeinfo"
PORTINFO = "portinfo"
ATTRS = (NODEINFO, PORTINFO)

# The only fields of smpquery's output that leave the container: PortInfo's
# Mkey, above all, never does.
FIELDS = (
    "Guid",
    "PortGuid",
    "NodeType",
    "NumPorts",
    "LocalPort",
    "LinkState",
    "PhysLinkState",
    "ProtectBits",
)

# Everything that goes into the plan ends up in a shell, so each part is
# checked strictly here, and again by the script.
_ID = re.compile(r"[A-Za-z0-9_]{1,32}")
# smpquery -D's path: 0, this host's port, then up to 63 exit ports
_ROUTE = re.compile(r"0(?:,[0-9]{1,3}){0,63}")
_MAX_PORT = 254
_GUID_LINE = re.compile(r"0x[0-9a-fA-F]{1,16}")
_FIELD_LINE = re.compile(r"([A-Za-z]+):([A-Za-z0-9 ()?]{1,40})")
_BEGIN = re.compile(r"BEGIN ([A-Za-z0-9_]{1,32})")
_END = re.compile(r"END ([A-Za-z0-9_]{1,32}) ([0-9]{1,3})")
# Unassigned virtual functions report placeholder GUIDs, which aren't ours.
_PLACEHOLDERS = {0, 0xFFFFFFFFFFFFFFFF}

# The script. It takes everything from its environment, never its own
# command line, so that no key is in its argv (visible in /proc):
# - AFM_PROBES: one Get per line, "<id> <attr> <route> <port> <key>", port
#   "-" for none and the key as 0x%016x;
# - AFM_PROBE_TIMEOUT: seconds each Get may take;
# - AFM_PROBE_PARALLEL: how many Gets may run at once.
# It prints this host's port GUIDs (ibstat -p) between OWN and END OWN, then
# a block per Get: "BEGIN <id>", whitelisted "Name:value" lines, and "END <id>
# <status>". A Get that times out or is refused has a non-zero status and no
# fields: no answer, not an error. The script fails only if it can't run at
# all: a tool missing, a malformed plan, or ibstat failing.
#
# Constraints of OpenSM's image: its root filesystem is read-only, so there
# are no temp files (and no here-documents, which may need one); each Get's
# block is put together by awk, whose output to a pipe is fully buffered, and
# written at its exit in one write, far below PIPE_BUF, so parallel blocks
# never interleave (the shell's printf can't: bash line-buffers it). Only
# smpquery, and timeout, which runs it, are given a key, on their command
# lines, and smpquery only ever Gets. stderr goes nowhere, since it may echo
# what it was given.
SCRIPT = r"""exec 2>/dev/null </dev/null
set -u
for tool in smpquery timeout ibstat xargs awk grep; do
    command -v "$tool" >/dev/null || exit 3
done
[ "${AFM_PROBES+set}" = set ] || exit 2
T=${AFM_PROBE_TIMEOUT-}
P=${AFM_PROBE_PARALLEL-}
case $T in ''|*[!0-9]*) exit 2 ;; esac
case $P in ''|*[!0-9]*) exit 2 ;; esac
[ "$T" -gt 0 ] && [ "$P" -gt 0 ] || exit 2
ROUTE='0(,[0-9]{1,3}){0,63}'
RE="[A-Za-z0-9_]{1,32} (nodeinfo|portinfo) $ROUTE ([0-9]{1,3}|-) 0x[0-9a-f]{16}"
bad=$(printf '%s\n' "$AFM_PROBES" | grep -Evxc -e '' -e "$RE")
[ "$bad" = 0 ] || exit 2
dups=$(printf '%s\n' "$AFM_PROBES" |
    awk 'NF { if (seen[$1]++) d++ } END { print d + 0 }')
[ "$dups" = 0 ] || exit 2
own=$(ibstat -p) || exit 4
own=$(printf '%s\n' "$own" | grep -Ex '0x[0-9a-fA-F]{1,16}')
[ -n "$own" ] && own="$own
"
printf 'OWN\n%sEND OWN\n' "$own"
AFM_LOOKUP='$1 == id { print; exit }'
AFM_BLOCK='
status != 0 { next }
{
    i = index($0, ":")
    if (i < 2) next
    name = substr($0, 1, i - 1)
    if (name !~ /^(Guid|PortGuid|NodeType|NumPorts|LocalPort)$/ &&
        name !~ /^(LinkState|PhysLinkState|ProtectBits)$/) next
    value = substr($0, i + 1)
    sub(/^\.+/, "", value)
    if (value !~ /^[A-Za-z0-9 ()?]+$/ || length(value) > 40) next
    if (seen[name]++) next
    fields = fields name ":" value "\n"
}
END { printf "BEGIN %s\n%sEND %s %s\n", id, fields, id, status }'
export AFM_LOOKUP AFM_BLOCK
ONE='
id=$1
line=$(printf "%s\n" "$AFM_PROBES" | awk -v id="$id" "$AFM_LOOKUP")
set -f
set -- $line
set +f
port=$4
[ "$port" = - ] && port=
out=$(timeout -k 1 "$AFM_PROBE_TIMEOUT" smpquery -D -y "$5" "$2" "$3" $port)
status=$?
printf "%s\n" "$out" | awk -v id="$id" -v status="$status" "$AFM_BLOCK"
'
printf '%s\n' "$AFM_PROBES" | awk 'NF { print $1 }' |
    xargs -r -n 1 -P "$P" sh -c "$ONE" sh
"""


class ProbeError(ReloadError):
    """The prober couldn't run, or its output can't be trusted

    Its message is a short reason that never holds the prober's output.
    """


@dataclasses.dataclass(frozen=True)
class Get:
    """One SubnGet by directed route: attr is "nodeinfo" or "portinfo",
    route smpquery -D's path ("0" for this host's port), port a port number
    for PortInfo, or None, and key the M_Key to send

    Its repr hides the key.
    """

    id: str
    attr: str
    route: str
    port: int | None
    key: int = dataclasses.field(repr=False)

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError("a Get's id must be 1 to 32 letters, digits or _")
        if self.attr not in ATTRS:
            raise ValueError(f"a Get's attribute must be one of {', '.join(ATTRS)}")
        if not isinstance(self.route, str) or not _ROUTE.fullmatch(self.route):
            raise ValueError("a Get's route must be 0 followed by exit ports")
        if any(not 1 <= int(hop) <= _MAX_PORT for hop in self.route.split(",")[1:]):
            raise ValueError("a Get's route has an exit port beyond 1 to 254")
        if self.port is not None and (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= _MAX_PORT
        ):
            raise ValueError("a Get's port must be 0 to 254, or None")
        if (
            isinstance(self.key, bool)
            or not isinstance(self.key, int)
            or not 0 <= self.key < 2**64
        ):
            # The message never holds the value, which may be a key.
            raise ValueError("a Get's key must be a 64-bit integer")

    def line(self):
        """The Get as a line of the plan"""
        port = "-" if self.port is None else str(self.port)
        return f"{self.id} {self.attr} {self.route} {port} 0x{self.key:016x}"


def plan(gets):
    """The AFM_PROBES text for these Gets"""
    seen = set()
    lines = []
    for get in gets:
        if not isinstance(get, Get):
            raise ValueError("a plan holds Gets only")
        if get.id in seen:
            raise ValueError(f"two Gets have the id {get.id}")
        seen.add(get.id)
        lines.append(get.line())
    return "\n".join(lines)


def environment(gets, timeout, parallel):
    """The script's environment for these Gets: where the keys travel"""
    for name, value in (("timeout", timeout), ("parallel", parallel)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"the prober's {name} must be a positive integer")
    return {
        "AFM_PROBES": plan(gets),
        "AFM_PROBE_TIMEOUT": str(timeout),
        "AFM_PROBE_PARALLEL": str(parallel),
    }


def parse(output, gets):
    """This host's port GUIDs and each Get's answer, from the script's
    output

    Returns (own_guids, answers): own_guids in ibstat's order without
    placeholders, and answers mapping each Get's id to its fields, or None
    if it had no answer. Raises ProbeError if the own-GUID section is
    missing, or a block is missing, duplicated, unexpected or malformed:
    every Get having a block is part of the script's contract.
    """
    ids = {get.id for get in gets}
    lines = output.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "OWN":
        raise ProbeError("the prober's output has no own-GUID section")
    own = []
    index = 1
    while True:
        if index >= len(lines):
            raise ProbeError("the prober's own-GUID section isn't ended")
        line = lines[index]
        index += 1
        if line == "END OWN":
            break
        if not _GUID_LINE.fullmatch(line):
            raise ProbeError("the prober's own-GUID section is malformed")
        guid = int(line, 16)
        if guid not in _PLACEHOLDERS and guid not in own:
            own.append(guid)

    answers = {}
    while index < len(lines):
        begin = _BEGIN.fullmatch(lines[index])
        if not begin:
            raise ProbeError("the prober's output has a line outside a block")
        get_id = begin.group(1)
        if get_id not in ids:
            raise ProbeError(f"the prober answered a Get it wasn't given ({get_id})")
        if get_id in answers:
            raise ProbeError(f"the prober answered Get {get_id} twice")
        index += 1
        fields = {}
        while True:
            if index >= len(lines):
                raise ProbeError(f"the prober's block for Get {get_id} isn't ended")
            line = lines[index]
            index += 1
            end = _END.fullmatch(line)
            if end:
                if end.group(1) != get_id:
                    raise ProbeError(f"the prober's block for Get {get_id} is broken")
                status = int(end.group(2))
                break
            field = _FIELD_LINE.fullmatch(line)
            if not field or field.group(1) not in FIELDS:
                raise ProbeError(f"the prober's block for Get {get_id} is malformed")
            if field.group(1) in fields:
                raise ProbeError(f"the prober's block for Get {get_id} repeats a field")
            fields[field.group(1)] = field.group(2)
        answers[get_id] = fields if status == 0 else None

    missing = ids - set(answers)
    if missing:
        raise ProbeError(f"the prober gave no block for {len(missing)} Gets")
    return own, answers


# The verify

# Scopes: the fabric check's, a full verify's; or a set of port GUIDs, the
# ports of open jobs
SWITCHES = "switches"
FULL = "full"


@dataclasses.dataclass
class VerifyResult:
    """What a verify found: new rows, and sets of GUIDs, never a key

    - ports: every row, those probed replaced by new PortStates;
    - own_guid: which of this host's ports answered, or None;
    - own_port_lost: this host's port answered neither key (or the host has
      no port), so OpenSM can't sweep: the caller fails the start;
    - switches: the GUIDs of switch rows (their port 0's) that answered as
      a switch with the GUID recorded on their route, of which the fabric
      check counts the expected ones;
    - answered: port GUIDs that answered with their recorded GUID;
    - keyed: those of them whose answer showed which key they hold
      (ProtectBits 2 or more);
    - unanswered: port GUIDs sent every allowed key without an answer;
    - unconfirmed: port GUIDs whose route answered, but not with their
      GUID, or not with both NodeInfo and PortInfo: the answer doesn't
      count, and no further key was sent;
    - link_down: port GUIDs not sent anything because their link is down;
    - link_states: (switch port GUID, port) to whether its link is up, as
      the switch's PortInfo said;
    - own_link_up: whether this host's port's link is up, as its own
      PortInfo said, or None if it didn't;
    - unrecorded: (switch port GUID, port) whose link is up with no
      recorded link, from a full verify only;
    - complete: False if the deadline ended the verify early, with
      everything learned so far kept.
    """

    ports: dict
    own_guid: int | None = None
    own_port_lost: bool = False
    switches: set = dataclasses.field(default_factory=set)
    answered: set = dataclasses.field(default_factory=set)
    keyed: set = dataclasses.field(default_factory=set)
    unanswered: set = dataclasses.field(default_factory=set)
    unconfirmed: set = dataclasses.field(default_factory=set)
    link_down: set = dataclasses.field(default_factory=set)
    link_states: dict = dataclasses.field(default_factory=dict)
    own_link_up: bool | None = None
    unrecorded: list = dataclasses.field(default_factory=list)
    # Rows the recorded links reach only through switches that didn't
    # answer, never sent a key
    unrouted: set = dataclasses.field(default_factory=set)
    complete: bool = True
    gets: int = 0
    batches: int = 0

    def counts(self):
        """Counts for a log line"""
        return {
            "switches": len(self.switches),
            "answered": len(self.answered),
            "keyed": len(self.keyed),
            "unanswered": len(self.unanswered),
            "unconfirmed": len(self.unconfirmed),
            "link_down": len(self.link_down),
            "unrecorded": len(self.unrecorded),
            "gets": self.gets,
            "batches": self.batches,
        }

    def __str__(self):
        counts = self.counts()
        text = ", ".join(f"{counts[name]} {name}" for name in counts)
        if self.own_port_lost:
            text = "this host's port answered neither key; " + text
        if not self.complete:
            text += " (incomplete: out of time)"
        return text


class _OutOfTime(Exception):
    pass


def verify(
    ports,
    links,
    *,
    m_key,
    base,
    target_on,
    nothing_derived,
    run,
    scope,
    now,
    deadline=None,
    clock=time.monotonic,
):
    """Probe recorded ports along recorded links with the keys the rules
    allow, and say what each answer means for the rows

    ports maps GUIDs to PortStates, links (GUID, port) to Links, as
    port_keys keeps them; m_key and base are integers, base None if none is
    stored; nothing_derived is port_keys.nothing_derived's answer; run is
    a runner, run(gets) -> (own_guids, answers), which raises ProbeError
    (a ReloadError) if it fails; scope is SWITCHES, FULL or a set of port
    GUIDs; now stamps last_seen; deadline, on clock's scale, is checked
    between batches. The arguments are left as they were.

    Every row that answers as a switch is routed through and counted in
    result.switches, as OpenSM's own sweeps go through whatever answers as
    one (specs/zero-touch.md, Switch trust is dropped). Which of them the
    fabric check counts is the caller's: the expected switches found, so
    that a new switch, or an adapter answering as one, never makes a
    fragment look complete. And keys_to_try sends a switch row only what
    OpenSM would send it, so following it hands a spoofer no key OpenSM
    wouldn't.
    """
    return _Verify(
        ports,
        links,
        m_key=m_key,
        base=base,
        target_on=target_on,
        nothing_derived=nothing_derived,
        run=run,
        scope=scope,
        now=now,
        deadline=deadline,
        clock=clock,
    ).go()


def _link_up(fields):
    """Whether a PortInfo answer shows the link up: True, False, or None if
    it doesn't say"""
    if not fields or "LinkState" not in fields:
        return None
    return fields["LinkState"].strip().lower() != "down"


def _int(text, base=10):
    try:
        return int(text, base)
    except (TypeError, ValueError):
        return None


class _Verify:
    def __init__(
        self,
        ports,
        links,
        *,
        m_key,
        base,
        target_on,
        nothing_derived,
        run,
        scope,
        now,
        deadline,
        clock,
    ):
        self.rows = dict(ports)
        # A link recorded in one direction only isn't followed.
        self.links = port_keys.paired(links)
        self.link_keys = {link.key for link in self.links}
        self.m_key = m_key
        self.base = base
        self.target_on = target_on
        self.nothing_derived = nothing_derived
        self.run = run
        self.now = now
        self.deadline = deadline
        self.clock = clock
        if scope in (SWITCHES, FULL):
            self.scope = scope
            self.wanted = None
        else:
            self.scope = None
            self.wanted = set(scope)
        self.result = VerifyResult(ports=self.rows)
        # The key each port answered with, to ask switches about their
        # ports: kept here only, never in the result
        self._good_key = {}
        self._node_info = {}
        self._ids = 0
        # Whether this host's port's link is up, from its own PortInfo
        self._own_link = None
        # Every route given so far, by GUID
        self._routes = {}

    # Batches

    def _batch(self, gets):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise _OutOfTime
        self.result.batches += 1
        self.result.gets += len(gets)
        return self.run(gets)

    def _get(self, attr, route, port, key):
        self._ids += 1
        return Get(f"g{self._ids}", attr, route, port, key)

    def _key(self, name, guid):
        if name == port_keys.M_KEY:
            return self.m_key
        if self.base is None:
            return None
        return port_keys.port_m_key(self.base, guid)

    def _row(self, guid):
        return self.rows.get(guid) or port_keys.PortState(guid)

    def go(self):
        try:
            if self._own_port():
                self._walk()
                self._unrouted()
        except _OutOfTime:
            self.result.complete = False
        if self.scope == FULL:
            self.result.unrecorded = sorted(
                key
                for key, up in self.result.link_states.items()
                if up and key not in self.link_keys
            )
        return self.result

    # Recording answers

    def _record(self, guid, name, node, port):
        """Record a port's answer to a key: node and port are its NodeInfo
        and PortInfo fields"""
        protect = _int(port.get("ProtectBits"))
        changes = {"reachable": True, "last_seen": self.now}
        if "NodeType" in node:
            changes["switch"] = node["NodeType"].strip() == "Switch"
        node_guid = _int(node.get("Guid"), 16)
        if node_guid is not None:
            changes["node_guid"] = node_guid
        if protect is not None and protect >= 2:
            # Only a port that checks Gets shows which key it holds.
            changes.update(holds=name, lapsed=False, released=False)
            self.result.keyed.add(guid)
        elif protect == 0:
            # It answers any key, so the key says nothing about what it
            # holds.
            changes["lapsed"] = True
        # Rows come from OpenSM's cache, so a port without one, such as
        # this host's port before OpenSM recorded it, isn't given one here.
        if guid in self.rows:
            self.rows[guid] = dataclasses.replace(self.rows[guid], **changes)
        self.result.answered.add(guid)
        self._good_key[guid] = self._key(name, guid)
        self._node_info[guid] = node

    def _mark(self, guid, kind):
        """A port that was sent keys and didn't count as answering"""
        getattr(self.result, kind).add(guid)
        row = self.rows.get(guid)
        if row is not None:
            self.rows[guid] = dataclasses.replace(row, reachable=False)

    # This host's port

    def _own_port(self):
        """Find which of this host's ports answers, and with which key:
        False if none does, so nothing beyond it can be reached"""
        own_guids, _ = self._batch([])
        candidates = [g for g in own_guids if g in self.rows]
        candidates += [g for g in own_guids if g not in self.rows]
        attempts = []
        tried = set()
        for guid in candidates:
            # This host's port is infrastructure: both keys, the recorded
            # one first. m_key is the same for every candidate, so it's
            # sent once.
            for name in port_keys.keys_to_try(
                self._row(guid).holds,
                self.target_on,
                True,
                self.nothing_derived,
                own_port=True,
            ):
                key = self._key(name, guid)
                if key is None or key in tried:
                    continue
                tried.add(key)
                attempts.append((guid, name, key))
        for guid, name, key in attempts:
            node_get = self._get(NODEINFO, "0", None, key)
            port_get = self._get(PORTINFO, "0", None, key)
            _, answers = self._batch([node_get, port_get])
            node, port = answers[node_get.id], answers[port_get.id]
            if node is None and port is None:
                continue
            answered = _int((node or {}).get("PortGuid"), 16)
            # The derived key is the candidate's own; m_key may be held by
            # whichever of them smpquery's port is.
            if name == port_keys.DERIVED:
                matches = answered == guid
            else:
                matches = answered in candidates
            if not matches or port is None:
                continue
            self.result.own_guid = answered
            self._record(answered, name, node, port)
            self._own_link = _link_up(port)
            self.result.own_link_up = self._own_link
            return True
        self.result.own_port_lost = True
        for guid in candidates:
            if guid in self.rows:
                self._mark(guid, "unanswered")
        return False

    # Beyond it

    def _followed(self, guid):
        """Whether a row is a switch that routes may go through, and that
        result.switches counts: any row whose NodeInfo said it's one"""
        return self._row(guid).switch is True

    def _in_scope(self, guid, needed):
        if self.scope == FULL:
            return True
        if self.scope == SWITCHES:
            return self._row(guid).switch is True
        return guid in self.wanted or guid in needed

    def _needed_switches(self):
        """For a set of GUIDs, the recorded switches on their routes, so
        that only those are probed to reach them"""
        if self.wanted is None:
            return set()
        switches = {g for g in self.rows if self._followed(g)}
        hops = port_keys.all_routes(self.links, self.result.own_guid, switches)
        needed = set()
        for guid in self.wanted:
            hop = hops.get(guid)
            while hop is not None and hop.via not in (None, self.result.own_guid):
                needed.add(hop.via)
                hop = hops.get(hop.via)
        return needed

    def _walk(self):
        own = self.result.own_guid
        needed = self._needed_switches()
        routes = self._routes
        routes[own] = (0,)
        answered = set()
        others = []
        # Switches first, level by level, so that a deadline that runs out
        # keeps their results whatever happens to the rest.
        level = port_keys.next_hops(self.links, own, routes, answered)
        while level:
            for hop in level:
                routes[hop.guid] = hop.route
            switches = []
            for hop in level:
                if not self._in_scope(hop.guid, needed):
                    continue
                if self._followed(hop.guid):
                    switches.append(hop)
                else:
                    others.append(hop)
            for hop in self._linked(switches):
                node = self._node_info.get(hop.guid)
                # Only a switch that says it's one again is routed through,
                # and counted, by the GUID its route's answer matched.
                if (
                    hop.guid in self.result.answered
                    and node is not None
                    and node.get("NodeType", "").strip() == "Switch"
                ):
                    answered.add(hop.guid)
                    self.result.switches.add(hop.guid)
            if self.scope == FULL:
                self._switch_ports([hop for hop in switches if hop.guid in answered])
            level = port_keys.next_hops(self.links, own, routes, answered)
        self._linked(others)

    def _unrouted(self):
        """The rows the recorded links reach only through switches that
        didn't answer: never sent a key, so not ports that refused one, as
        those behind a missing switch are (specs/ha.md, While a switch is
        missing)"""
        switches = {guid for guid, row in self.rows.items() if row.switch is True}
        possible = port_keys.all_routes(
            self.links, self.result.own_guid, switches | {self.result.own_guid}
        )
        self.result.unrouted = {
            guid for guid in possible if guid in self.rows and guid not in self._routes
        }

    def _linked(self, hops):
        """Probe the hops whose link is up, and return them"""
        if not hops:
            return []
        missing = {
            (hop.via, hop.via_port)
            for hop in hops
            if hop.via != self.result.own_guid
            and (hop.via, hop.via_port) not in self.result.link_states
        }
        if missing:
            self._port_states(sorted(missing))
        up = []
        for hop in hops:
            if hop.via == self.result.own_guid:
                state = self._own_link
            else:
                state = self.result.link_states.get((hop.via, hop.via_port))
            if state is False:
                self.result.link_down.add(hop.guid)
            elif state is True:
                up.append(hop)
            # A link whose state isn't known isn't followed, and the port
            # keeps its row: not down, which would complete its jobs, and
            # not lost.
        self._probe(up)
        return up

    def _switch_ports(self, hops):
        """Every port's link state of these switches, which answered"""
        wanted = []
        for hop in hops:
            count = _int(self._node_info[hop.guid].get("NumPorts"))
            if count is None or not 0 < count <= 254:
                continue
            wanted += [
                (hop.guid, port)
                for port in range(1, count + 1)
                if (hop.guid, port) not in self.result.link_states
            ]
        if wanted:
            self._port_states(wanted)

    def _port_states(self, switch_ports):
        """Ask switches, with their own keys, for the PortInfo of their
        ports, and record whether each link is up"""
        gets = {}
        for switch, port in switch_ports:
            key = self._good_key.get(switch)
            if key is None or switch not in self._routes:
                continue
            route = ",".join(str(hop) for hop in self._routes[switch])
            gets[(switch, port)] = self._get(PORTINFO, route, port, key)
        if not gets:
            return
        _, answers = self._batch(list(gets.values()))
        for switch_port, get in gets.items():
            up = _link_up(answers[get.id])
            if up is not None:
                self.result.link_states[switch_port] = up

    def _probe(self, hops):
        """Send each hop its allowed keys in order, the next only to those
        that didn't answer the last"""
        pending = []
        for hop in hops:
            row = self._row(hop.guid)
            own = row.guid == self.result.own_guid
            names = [
                name
                for name in port_keys.keys_to_try(
                    row.holds,
                    self.target_on,
                    port_keys.is_infrastructure(row, self.result.own_guid),
                    self.nothing_derived,
                    own_port=own,
                )
                if self._key(name, hop.guid) is not None
            ]
            if names:
                pending.append((hop, names))
            else:
                self._mark(hop.guid, "unanswered")
        while pending:
            gets = {}
            for hop, names in pending:
                key = self._key(names[0], hop.guid)
                # A switch's port 0 holds its PortInfo, M_Key included; an
                # HCA's port answers for itself.
                port = 0 if self._row(hop.guid).switch is True else None
                gets[hop.guid] = (
                    self._get(NODEINFO, hop.route_text, None, key),
                    self._get(PORTINFO, hop.route_text, port, key),
                )
            _, answers = self._batch([get for pair in gets.values() for get in pair])
            retry = []
            for hop, names in pending:
                node_get, port_get = gets[hop.guid]
                node, port = answers[node_get.id], answers[port_get.id]
                if node is None and port is None:
                    if len(names) > 1:
                        retry.append((hop, names[1:]))
                    else:
                        self._mark(hop.guid, "unanswered")
                    continue
                # Something answered this key at the route: no other key is
                # sent there, whether or not the answer counts.
                if (
                    node is None
                    or port is None
                    or _int(node.get("PortGuid"), 16) != hop.guid
                ):
                    self._mark(hop.guid, "unconfirmed")
                    continue
                self._record(hop.guid, names[0], node, port)
            pending = retry
