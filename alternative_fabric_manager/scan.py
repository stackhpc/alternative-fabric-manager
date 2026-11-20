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

"""Where a fabric check's own scan places each port

specs/ha.md, While a switch is missing, Where a port is, is the source of
truth. A partial check places the ports from its own scan, never from the
SA: in prober mode from the recorded links of the ports its verify reached
(from_verify), and where the check runs ibswitches, from an ibnetdiscover
scan in the same check (parse_ibnetdiscover). A virtual function is a vport
of its physical port, placed through OpenSM's virtualization dump
(parse_virtualization_dump, place).

A place is a JSON-able list, so that the one recorded with a sweep can be
compared with the check's after it: ["port", switch GUID, switch port] for
a port cabled to a switch, and for a vport ["vport", its physical port's
GUID, index, state] followed by the physical port's place, so that "the
same way" is the same dump entry on the same switch port.

Both formats are parsed here and nowhere else, and tests/data holds a
sample of each, from OpenSM 5.17.2 with a SwitchIB and ConnectX-5s. Neither
is evidence of what it can't be parsed as: a line or row that doesn't parse
places nothing, so a format that differs fails closed, and grants wait for
a whole fabric as they did before.
"""

import re

# The file OpenSM 5.17 rewrites when its vports change, in its container
VIRTUALIZATION_DUMP = "/var/log/opensm-virtualization.dump"
ACTIVE = "ACTIVE"
PORT = "port"
VPORT = "vport"


def guid_text(guid):
    """A GUID in the 16 hex digits Port.guid and the records use"""
    return f"{guid:016x}"


# ibnetdiscover's topology output: a block per node, a header line such as
#   Switch	37 "S-ec0d9a0300b12560"		# "SwitchIB ..." base port 0 lid 5 lmc 0
#   Ca	1 "H-040973ffffc75168"		# "MT4119 ConnectX5 ..."
# then a line per connected port. A switch's names its port, the peer's
# node, the peer's port, and for an adapter peer its port GUID:
#   [8]	"H-040973ffffc75168"[1](40973ffffc75168) 		# "..." lid 1 4xEDR
# and an adapter's names its port and port GUID, then the peer's:
#   [1](40973ffffc75168) 	"S-ec0d9a0300b12560"[8]		# lid 1 lmc 0 "..."
# GUIDs in parentheses drop their leading zeros; quoted names keep them.
# Every GUID is read as a number, so both compare alike. A port number may
# carry an extended port, as "[1][ext 1]", which is skipped.
_NODE = re.compile(
    r'^(?P<kind>Switch|Ca|Rt)\s+\d+\s+"(?P<prefix>[SHR])-(?P<guid>[0-9a-fA-F]{1,16})"'
)
_SWITCH_PORT = re.compile(
    r"^\[(?P<port>\d+)\](?:\[ext \d+\])?\s+"
    r'"(?P<prefix>[SHR])-(?P<node>[0-9a-fA-F]{1,16})"'
    r"\[(?P<peer_port>\d+)\](?:\[ext \d+\])?(?:\((?P<peer_guid>[0-9a-fA-F]{1,16})\))?"
)
_CA_PORT = re.compile(
    r"^\[(?P<port>\d+)\](?:\[ext \d+\])?\((?P<guid>[0-9a-fA-F]{1,16})\)\s+"
    r'"(?P<prefix>[SHR])-(?P<node>[0-9a-fA-F]{1,16})"\[(?P<peer_port>\d+)\]'
)


def parse_ibnetdiscover(text):
    """The adapter ports an ibnetdiscover scan reached, by port GUID, as
    {GUID: ["port", switch GUID, switch port]}

    Only a port cabled to a switch the scan found is placed, as either end
    of its link lists it. A GUID the scan lists at two places isn't placed
    at all: the scan doesn't say where it is. A switch's aggregation node,
    which ibnetdiscover lists as an adapter on the switch's own port, is
    placed like any other, and no Neutron port has its GUID.
    """
    seen = {}
    node = None
    for line in text.splitlines():
        line = line.strip()
        match = _NODE.match(line)
        if match:
            node = (match.group("kind"), int(match.group("guid"), 16))
            continue
        if node is None or not line.startswith("["):
            if not line:
                node = None
            continue
        kind, guid = node
        if kind == "Switch":
            match = _SWITCH_PORT.match(line)
            if not match or match.group("prefix") != "H":
                continue
            peer = match.group("peer_guid")
            if peer is None:
                continue
            place = (int(match.group("port")), guid)
            seen.setdefault(int(peer, 16), set()).add(place)
        elif kind == "Ca":
            match = _CA_PORT.match(line)
            if not match or match.group("prefix") != "S":
                continue
            place = (int(match.group("peer_port")), int(match.group("node"), 16))
            seen.setdefault(int(match.group("guid"), 16), set()).add(place)
    placed = {}
    for guid, places in seen.items():
        if len(places) == 1:
            port, switch = next(iter(places))
            placed[guid] = [PORT, guid_text(switch), port]
    return placed


def from_verify(results, ports, links):
    """In prober mode, the adapter ports a check's verifies reached, by port
    GUID, placed where the recorded links cable them to a switch the verify
    found: results are prober.VerifyResults, ports and links the rows
    (port_keys.load), links only those recorded both ways"""
    from alternative_fabric_manager import port_keys

    reached = set()
    found = set()
    for result in results:
        reached |= result.answered
        found |= result.switches
    switch_ends = {}
    for link in port_keys.paired(links):
        row = ports.get(link.guid)
        if row is None or row.switch is not True or link.guid not in found:
            continue
        peer = ports.get(link.peer_guid)
        if peer is not None and peer.switch is True:
            continue
        switch_ends.setdefault(link.peer_guid, set()).add((link.guid, link.port))
    placed = {}
    for guid, ends in switch_ends.items():
        if guid in reached and len(ends) == 1:
            switch, port = next(iter(ends))
            placed[guid] = [PORT, guid_text(switch), port]
    return placed


# OpenSM's virtualization dump has sections, each a CSV header line, rows
# with values padded with spaces, and an END_ line:
#   START_VPORTS
#   PortGUID, Index, VPortGUID, VNodeGUID, VPortNum, State, IsLidRequired, ...
#   0x040973ffffc75168,     7, 0xfa163e000062a357, 0xfa163e000062a357,   1, ACTIVE, ...
#   END_VPORTS
# Index 0 is the physical port itself, its VPortGUID its PortGUID. Columns
# are found by the header's names, so that added ones don't matter.
_VPORT_COLUMNS = ("PortGUID", "Index", "VPortGUID", "State")


def _int(text, base=0):
    try:
        return int(text, base)
    except (TypeError, ValueError):
        return None


def parse_virtualization_dump(text):
    """The rows of OpenSM's virtualization dump's VPORTS section, in order,
    as (vport GUID, physical port GUID, index, state) tuples, any of whose
    parts may be None where a row doesn't say; [] without the section, or
    if its header lacks a column this needs"""
    entries = []
    columns = None
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line == "START_VPORTS":
            inside, columns = True, None
            continue
        if line == "END_VPORTS":
            inside = False
            continue
        if not inside or not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if columns is None:
            columns = {name: i for i, name in enumerate(cells)}
            if not all(name in columns for name in _VPORT_COLUMNS):
                return []
            continue

        def cell(name, cells=cells, columns=columns):
            i = columns[name]
            return cells[i] if i < len(cells) else None

        state = cell("State")
        entries.append(
            (
                _int(cell("VPortGUID"), 16),
                _int(cell("PortGUID"), 16),
                _int(cell("Index"), 10),
                state.upper() if state else None,
            )
        )
    return entries


def place(physical, entries):
    """Every GUID a scan places, by GUID: physical is the scan's adapter
    ports' places, entries the dump read with it

    A vport is placed if the dump lists its GUID exactly once, ACTIVE, at a
    non-zero index of a placed physical port. A physical port is placed by
    the scan alone, unless the dump lists its GUID as another port's vport,
    when nothing says which it is.
    """
    by_guid = {}
    for entry in entries:
        if entry[0] is not None:
            by_guid.setdefault(entry[0], []).append(entry)
    placed = {}
    for guid, where in physical.items():
        listed = by_guid.get(guid, [])
        if all(entry[1] == guid for entry in listed):
            placed[guid] = list(where)
    for guid, listed in by_guid.items():
        if guid in physical or len(listed) != 1:
            continue
        _guid, port, index, state = listed[0]
        if port is None or not index or state != ACTIVE or port == guid:
            continue
        where = placed.get(port)
        if where is None or where[0] != PORT:
            continue
        placed[guid] = [VPORT, guid_text(port), index, state, *where]
    return placed
