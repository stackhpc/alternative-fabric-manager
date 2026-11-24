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

"""Per-port M_Keys: which key each port holds, and what follows from it

specs/secured-profile.md, Per-port M_Keys, The design, is the source of
truth for every rule here. This module is pure: it works on plain data,
PortState and Link, and on OpenSM's cache files as text, so that every rule
can be tested without a fabric. Only load, save and the target's and
fingerprint's load_ and save_ functions touch the database, in a session
the caller owns, and nothing here starts a process or reads a file.

GUIDs and keys are integers here. The database keeps GUIDs as 16 lower-case
hex digits, as Port.guid, and the rest of AFM passes keys as "0x%016x"
strings: parse_key, format_key, guid_text and parse_guid convert at the
edge.

A port's key opens that port, and the base opens every port, so nothing
here logs, and no exception message or repr holds a key: KeyEntries, the
one container of keys, shows only its size.
"""

import collections.abc
import dataclasses
import datetime
import hashlib
import re

import sqlalchemy as sa

from alternative_fabric_manager import db

# The state rows: the target, 1 for per-port keys on and 0 for off, and the
# fingerprint of the base the ports were keyed from, as text
TARGET = "port_keys_target"
BASE_FINGERPRINT = "port_keys_base_fingerprint"
# The target OpenSM was last started on, 1 for on, recorded before that
# start: a start whose target isn't it begins a transition
RUNNING = "port_keys_running"
# 1 once a start with the target on met the success rule (Turning it on),
# or adopted the target from ports that hold derived keys already (Restores):
# until then, every start with the target on is held to it
CONFIRMED = "port_keys_confirmed"
# A turn-on whose start failed and was set back, as "<the base's
# fingerprint>:<the configuration hash>", the hash an HMAC keyed by a secret
# in the database: while it matches the running configuration and base,
# nothing arms the turn-on again (specs/zero-touch.md). Never shown.
FAILED_TURN_ON = "port_keys_failed_turn_on"
# Why the manager's checks don't arm the turn-on, for --port-key-states:
# empty when nothing blocks it, or it doesn't apply
ARMING = "port_keys_arming"
# When the manager last restarted OpenSM to apply the target, and when the
# target last changed, as Unix times: the hourly cap, and the day after
# which it applies whatever the jobs
LAST_APPLY = "port_keys_last_apply"
# How many starts in a row fell back to the target OpenSM last ran on,
# because their verify failed
FALLBACKS = "port_keys_fallbacks"
TARGET_CHANGED = "port_keys_target_changed"

# What a port holds: the stored m_key, its key derived from the base, or
# neither as far as the manager knows
M_KEY = "m_key"
DERIVED = "derived"
UNKNOWN = "unknown"
HOLDS = (M_KEY, DERIVED, UNKNOWN)

# Labels the fingerprint, so that it can't be mistaken for, or computed as,
# a hash of the key for any other purpose
_FINGERPRINT_LABEL = b"alternative-fabric-manager m_key_base fingerprint\0"

# OpenSM writes 0x-prefixed hex; a hand-edited file may be in either case,
# and unpadded. At most 16 digits, a 64-bit value.
_HEX = r"0[xX][0-9a-fA-F]{1,16}"
_GUID2MKEY_LINE = re.compile(rf"({_HEX})\s+({_HEX})")
# An IB port number is 0 to 254 (0 being a switch's management port).
_ENDPOINT = rf"({_HEX}):([0-9]{{1,3}})"
_NEIGHBORS_LINE = re.compile(rf"{_ENDPOINT}\s+{_ENDPOINT}")
_MAX_PORT = 254


# Conversions at the edge


def parse_key(text):
    """A key as an integer, from AFM's "0x%016x" form

    The message of the ValueError it raises never holds the text, which
    may be a key.
    """
    try:
        value = int(text, 16)
    except (TypeError, ValueError):
        raise ValueError("not a 64-bit key") from None
    if not 0 <= value < 2**64:
        raise ValueError("not a 64-bit key")
    return value


def format_key(key):
    """A key in AFM's "0x%016x" form"""
    return f"0x{key:016x}"


def guid_text(guid):
    """A GUID as the database keeps it: 16 lower-case hex digits"""
    return f"{guid:016x}"


def parse_guid(text):
    """A GUID as an integer, from the database's or a file's form"""
    value = int(text, 16)
    if not 0 <= value < 2**64:
        raise ValueError(f"{text!r} isn't a 64-bit GUID")
    return value


# Keys


def port_m_key(base, guid):
    """The M_Key OpenSM 5.17.2 derives for a port with m_key_per_port, from
    the base (its m_key line) and the port's GUID

    osm_subn_calc_mkey: SHA-1 over the base and the GUID, each as 8 bytes in
    network order, with the digest folded into 8 bytes by XOR. A zero GUID
    gets 0. See specs/secured-profile.md, How OpenSM derives the keys.
    """
    if not guid:
        return 0
    # SHA-1 is OpenSM's choice, which the key must match, not AFM's.
    digest = hashlib.sha1(  # noqa: S324
        base.to_bytes(8, "big") + guid.to_bytes(8, "big")
    ).digest()
    key = bytearray(8)
    for i, b in enumerate(digest):
        key[i % 8] ^= b
    return int.from_bytes(key, "big")


def fingerprint(base):
    """A fingerprint of the base, which the state table may hold: SHA-256 of
    a fixed label and the key, so that a restored database whose base
    differs from the one the ports were keyed from is noticed, without the
    database holding the key twice"""
    return hashlib.sha256(_FINGERPRINT_LABEL + base.to_bytes(8, "big")).hexdigest()


class KeyEntries(collections.abc.Mapping):
    """Port GUID to M_Key, as guid2mkey holds them

    Its repr and str show only how many entries it has, so that a log line
    or a test failure that prints it can't show a key.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries=()):
        self._entries = dict(entries)

    def __getitem__(self, guid):
        return self._entries[guid]

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def __repr__(self):
        return f"<KeyEntries: {len(self._entries)} GUIDs>"

    __str__ = __repr__


def classify(guid, entry, m_key, base):
    """Which key a guid2mkey entry says the port holds

    "m_key" for the stored m_key, "derived" for its key derived from the
    stored base, otherwise "unknown", no entry (None) included. base may be
    None, before one is stored: nothing is then derived.
    """
    if entry is None:
        return UNKNOWN
    if entry == m_key:
        return M_KEY
    if base is not None and entry == port_m_key(base, guid):
        return DERIVED
    return UNKNOWN


def classify_entries(entries, m_key, base):
    """What each guid2mkey entry holds, by GUID: no keys, so it can be kept
    and logged"""
    return {guid: classify(guid, key, m_key, base) for guid, key in entries.items()}


# The rows


@dataclasses.dataclass(frozen=True)
class PortState:
    """A port OpenSM's cache listed, and which key it holds, never the key

    holds is "m_key", "derived" or "unknown"; lapsed means its ProtectBits
    were 0 at the last Get, so it answers any key; released is an
    operator's --release-port, until it next answers its key.
    """

    guid: int
    node_guid: int | None = None
    # As its NodeInfo said: None until it answers one
    switch: bool | None = None
    holds: str = UNKNOWN
    lapsed: bool = False
    reachable: bool = False
    released: bool = False
    last_seen: datetime.datetime | None = None

    def __post_init__(self):
        if self.holds not in HOLDS:
            raise ValueError(f"holds must be one of {', '.join(HOLDS)}")


@dataclasses.dataclass(frozen=True)
class Link:
    """One direction of a link, as a neighbors line: from the local (GUID,
    port) to the peer's"""

    guid: int
    port: int
    peer_guid: int
    peer_port: int
    last_seen: datetime.datetime | None = None

    @property
    def key(self):
        """Its row's primary key, the local side"""
        return (self.guid, self.port)


def _rows(ports):
    """The PortStates of a dict of them, by GUID, or of any iterable"""
    if isinstance(ports, collections.abc.Mapping):
        return ports.values()
    return ports


def target_holds(target_on):
    """What the target gives every port: its derived key when on, the
    stored m_key when off"""
    return DERIVED if target_on else M_KEY


# OpenSM's cache files


def parse_guid2mkey(text):
    """Parse OpenSM's guid2mkey: a "0x<guid> 0x<key>" pair per line, OpenSM
    writing a blank line after each

    Raises ValueError, naming the line by number only, since it holds a key,
    for anything else, so that a bad read leaves the rows as they were.
    """
    entries = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _GUID2MKEY_LINE.fullmatch(line.strip())
        if not match:
            raise ValueError(f"guid2mkey line {number} isn't a GUID and a key")
        guid, key = (int(value, 16) for value in match.groups())
        if entries.get(guid, key) != key:
            raise ValueError(f"guid2mkey has two keys for GUID 0x{guid:016x}")
        entries[guid] = key
    return KeyEntries(entries)


def parse_neighbors(text):
    """Parse OpenSM's neighbors: a "0x<guid>:<port> 0x<guid>:<port>" pair per
    line, the local side then the peer, each direction of a link on a line
    of its own

    Returns Links without last_seen. Raises ValueError for anything else,
    and for a local (GUID, port) with two peers, so that a bad read leaves
    the rows as they were.
    """
    links = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _NEIGHBORS_LINE.fullmatch(line.strip())
        if not match:
            raise ValueError(f"neighbors line {number} isn't two GUID:port pairs")
        guid, port, peer_guid, peer_port = (
            int(value, 16) if i % 2 == 0 else int(value)
            for i, value in enumerate(match.groups())
        )
        if port > _MAX_PORT or peer_port > _MAX_PORT:
            raise ValueError(f"neighbors line {number} has a port beyond 254")
        link = Link(guid, port, peer_guid, peer_port)
        if links.get(link.key, link) != link:
            raise ValueError(f"neighbors has two peers for 0x{guid:016x} port {port}")
        links[link.key] = link
    return list(links.values())


def render_guid2mkey(entries):
    """guid2mkey as OpenSM writes it: sorted, 16 lower-case hex digits, with
    a blank line after each entry"""
    return "".join(
        f"0x{guid:016x} 0x{entries[guid]:016x}\n\n" for guid in sorted(entries)
    )


def paired(links):
    """The links whose reverse is recorded and points back, as a list

    A link recorded in one direction only, as a re-cabled port's old peer
    can leave, is neither followed nor rendered: the port would be reached
    through a switch port it has left, and whatever is cabled there next
    taken for it.
    """
    links = list(_rows(links))
    peers = {link.key: (link.peer_guid, link.peer_port) for link in links}
    return [
        link
        for link in links
        if peers.get((link.peer_guid, link.peer_port)) == link.key
    ]


def render_neighbors(links):
    """neighbors from every recorded link, sorted by its local side

    OpenSM finds a port only through a neighbors line, or its own topology
    (test 5: a missing line loses that port), so every link is written,
    whatever the target, except one recorded in one direction only
    (paired). It holds GUIDs and port numbers only.
    """
    return "".join(
        f"0x{link.guid:016x}:{link.port} 0x{link.peer_guid:016x}:{link.peer_port}\n"
        for link in sorted(paired(links), key=lambda link: link.key)
    )


# Folding OpenSM's cache into the rows


@dataclasses.dataclass(frozen=True)
class FoldSummary:
    """What a fold changed, as counts, for the log: no keys"""

    ports_added: int = 0
    ports_changed: int = 0
    links_added: int = 0
    links_changed: int = 0

    def __str__(self):
        return (
            f"{self.ports_added} ports added, {self.ports_changed} changed what "
            f"they hold; {self.links_added} links added, {self.links_changed} "
            "re-cabled"
        )


def fold_check(ports, links, neighbors, guid2mkey, m_key, base, now):
    """Fold the running OpenSM's cache into the rows, at a fabric check

    ports maps GUIDs to PortStates, links maps (GUID, port) to Links;
    neighbors is parse_neighbors' list and guid2mkey parse_guid2mkey's
    entries. Returns new dicts and a FoldSummary; the arguments are left as
    they were.

    - Each neighbors line replaces the link with its local (GUID, port): a
      re-cabled port, as OpenSM saw it.
    - Every GUID neighbors lists, on either side, gets a row, and only
      those: virtual functions, which only guid2mkey lists, never do.
    - A port's holds comes from its guid2mkey entry (classify). With no
      entry, a row keeps what it holds: a port OpenSM lost still holds its
      key, and forgetting that it holds m_key would lose it for good when it
      returns. A new row without one is unknown.
    - A recorded port that neighbors doesn't list, but guid2mkey does, takes
      its holds from the entry too, which is the key OpenSM last gave it,
      and keeps its last_seen.
    - Rows and links absent from the files are kept as they were: a port
      that's down now may come back holding its key.
    - Links and ports that neighbors lists are stamped last_seen = now.
    """
    return _fold(
        ports, links, neighbors, guid2mkey, m_key, base, (now, now), start=False
    )


def fold_start(
    ports, links, neighbors, guid2mkey, m_key, base, neighbors_time, guid2mkey_time
):
    """Fold the cache found in a created, not yet started, container's
    volume into the rows, before a start

    As fold_check, except that whichever of the row and the file is newer
    wins: for each link, its last_seen against neighbors_time, and for each
    port's holds, its last_seen against guid2mkey_time, the files'
    modification times. The row wins a tie; a row with no last_seen loses.
    The files are newer after a crash within a check interval of a new port
    being keyed, and the rows after a lost volume. What's only in one of
    them is kept, or added. What neighbors wins is stamped last_seen =
    neighbors_time.
    """
    return _fold(
        ports,
        links,
        neighbors,
        guid2mkey,
        m_key,
        base,
        (neighbors_time, guid2mkey_time),
        start=True,
    )


def _fold(ports, links, neighbors, guid2mkey, m_key, base, times, start):
    """Fold the files into copies of the rows; times are neighbors' and
    guid2mkey's, which the rows' last_seen is compared with at a start"""
    neighbors_time, guid2mkey_time = times
    ports = dict(ports)
    links = dict(links)
    counts = collections.Counter()

    def file_wins(last_seen, file_time):
        if not start or last_seen is None:
            return True
        return file_time > last_seen

    listed_ends = {line.key for line in neighbors}
    for line in neighbors:
        old = links.get(line.key)
        new = dataclasses.replace(line, last_seen=neighbors_time)
        if old is None:
            counts["links_added"] += 1
        elif not file_wins(old.last_seen, neighbors_time):
            continue
        elif (old.peer_guid, old.peer_port) != (new.peer_guid, new.peer_port):
            counts["links_changed"] += 1
            # The old peer's row, if it still points back and the file
            # doesn't list its end, goes with it: that switch port has
            # nothing recorded on it now.
            stale = (old.peer_guid, old.peer_port)
            back = links.get(stale)
            if (
                back is not None
                and stale not in listed_ends
                and (back.peer_guid, back.peer_port) == line.key
            ):
                del links[stale]
        links[new.key] = new

    listed = {guid for line in neighbors for guid in (line.guid, line.peer_guid)}
    for guid in sorted(listed):
        entry = guid2mkey.get(guid)
        row = ports.get(guid)
        if row is None:
            holds = classify(guid, entry, m_key, base)
            ports[guid] = PortState(guid, holds=holds, last_seen=neighbors_time)
            counts["ports_added"] += 1
            continue
        holds = row.holds
        if entry is not None and file_wins(row.last_seen, guid2mkey_time):
            holds = classify(guid, entry, m_key, base)
        last_seen = row.last_seen
        if file_wins(row.last_seen, neighbors_time):
            last_seen = neighbors_time
        if holds != row.holds:
            counts["ports_changed"] += 1
        ports[guid] = dataclasses.replace(row, holds=holds, last_seen=last_seen)

    # A recorded port that neighbors no longer lists still has the key OpenSM
    # last gave it in guid2mkey, which is what it holds when it returns. Its
    # last_seen stays, since an entry, which OpenSM keeps for ports it has
    # lost, doesn't show that it's present.
    for guid, entry in guid2mkey.items():
        row = ports.get(guid)
        if row is None or guid in listed:
            continue
        if not file_wins(row.last_seen, guid2mkey_time):
            continue
        holds = classify(guid, entry, m_key, base)
        if holds != row.holds:
            counts["ports_changed"] += 1
            ports[guid] = dataclasses.replace(row, holds=holds)

    return ports, links, FoldSummary(**counts)


# Restores and adoption


def restore_problem(guid2mkey, m_key, base, recorded_fingerprint):
    """Why the volume's cache and the database don't belong together, or
    None if they do

    A restored database may predate keys the ports hold: an entry, for any
    GUID, that is neither the stored m_key nor derived from the stored base,
    or a base that isn't the one the ports were keyed from (its recorded
    fingerprint). Then the start keeps the target off, leaves the volume's
    files as they are, and starts OpenSM without the option, which re-keys
    each port it reaches through its own cache to m_key (specs/
    secured-profile.md, Restores). The reason names GUIDs, never keys.
    """
    if recorded_fingerprint is not None and (
        base is None or fingerprint(base) != recorded_fingerprint
    ):
        return (
            "the stored per-port M_Key base isn't the one the ports were keyed "
            "from (its fingerprint differs)"
        )
    strangers = sorted(
        guid
        for guid, key in guid2mkey.items()
        if classify(guid, key, m_key, base) == UNKNOWN
    )
    if strangers:
        shown = ", ".join(f"0x{guid:016x}" for guid in strangers[:5])
        more = f" and {len(strangers) - 5} more" if len(strangers) > 5 else ""
        return (
            f"OpenSM's cache has M_Keys for {len(strangers)} ports ({shown}{more}) "
            "that are neither the stored M_Key nor derived from the stored base"
        )
    return None


def should_adopt(ports, group_named, target_on, recorded_fingerprint):
    """Whether a start adopts the target on: the group is named, the target
    is off, a folded row holds its derived key, and no fingerprint is
    recorded

    A restored database that has the base but predates the turn-on has the
    target off while the ports hold the base's derived keys. Adopting the
    target, rather than re-keying the fabric back to the exposed m_key, is
    what Restores asks for. With a fingerprint, the database has seen a
    turn-on, and derived rows are a turn-off's or a set-back's leftovers,
    such as a node that was down: they're turned off as any others, and
    the manager's checks, which verify the fabric first, arm them again.
    Decided after folding the volume's files and before verifying, and only
    once restore_problem has found none.
    """
    if not group_named or target_on or recorded_fingerprint is not None:
        return False
    return any(row.holds == DERIVED for row in _rows(ports))


# The prober's keys


def nothing_derived(ports, recorded_fingerprint, volume_holds):
    """Whether nothing can hold a derived key yet: no fingerprint is
    recorded, and no row and no volume entry is derived

    volume_holds is what the volume's guid2mkey entries hold, as
    classify_entries gives them (its values, or the dict). Until then,
    trying m_key on a port can't reveal anything a derived key protects.
    """
    if recorded_fingerprint is not None:
        return False
    if any(row.holds == DERIVED for row in _rows(ports)):
        return False
    if isinstance(volume_holds, collections.abc.Mapping):
        volume_holds = volume_holds.values()
    return DERIVED not in set(volume_holds)


def is_infrastructure(port, own_guid):
    """Whether a port is one no tenant controls: this host's own, or a
    switch's port 0 (a switch's ports share its GUID). A port not yet known
    to be a switch's isn't."""
    return port.guid == own_guid or port.switch is True


def keys_to_try(holds, target_on, infrastructure, nothing_derived, own_port=False):
    """Which keys the prober may send a port, in order: "derived" and
    "m_key"

    This is what keeps the old key from tenants (specs/secured-profile.md,
    The prober, as specs/zero-touch.md changes it):
    - this host's own port (own_port), which nothing else can answer as,
      gets both, whatever its row says;
    - a switch's port 0 (infrastructure) gets both too, except that with
      the target on and anything able to hold a derived key, it gets m_key
      only if its row says m_key. A row is a switch's because its NodeInfo
      said so, which an adapter with custom firmware could say too, and
      OpenSM itself would send such a row only its derived key, since only
      a guid2mkey entry, which a row holding m_key alone gets, names the
      stored M_Key. So the prober sends nothing OpenSM wouldn't, and an
      unknown switch row gets its derived key alone, as a tenant's does;
    - otherwise the derived key, whenever the target is on or the row says
      derived;
    - m_key only if the row says m_key, or the target is off (every port is
      about to be given m_key, so trying it exposes nothing new), or
      nothing_derived (nothing can hold a derived key yet).
    The key the row records goes first, so that a port on m_key during a
    slow transition isn't sent a wrong key at every check; an unknown row
    tries its derived key first. So with the target on, a port recorded
    derived or unknown, other than this host's own, is never sent m_key
    once anything can hold a derived key.
    """
    if holds not in HOLDS:
        raise ValueError(f"holds must be one of {', '.join(HOLDS)}")
    if infrastructure:
        allowed = {DERIVED, M_KEY}
        if not own_port and target_on and not nothing_derived and holds != M_KEY:
            allowed = {DERIVED}
    else:
        allowed = set()
        if target_on or holds == DERIVED:
            allowed.add(DERIVED)
        if holds == M_KEY or not target_on or nothing_derived:
            allowed.add(M_KEY)
    order = (M_KEY, DERIVED) if holds == M_KEY else (DERIVED, M_KEY)
    return tuple(key for key in order if key in allowed)


# What a start renders


def guid2mkey_entries(ports, target_on, m_key, base):
    """The guid2mkey entries a start writes: one only for a port whose key
    differs from what OpenSM would send without one

    With the target on, OpenSM derives every port's key, so a port still
    holding m_key needs an entry; with it off, OpenSM sends m_key, so a port
    holding its derived key does. An unknown port gets none, and so
    OpenSM's own choice. These replace OpenSM's own entries: merging would
    let a stale one beat the derivation (requirement 3).
    """
    entries = {}
    for row in _rows(ports):
        if target_on and row.holds == M_KEY:
            entries[row.guid] = m_key
        elif not target_on and row.holds == DERIVED:
            if base is None:
                raise ValueError(
                    f"port 0x{row.guid:016x} holds a derived key, but no base is stored"
                )
            entries[row.guid] = port_m_key(base, row.guid)
    return KeyEntries(entries)


# Routes for directed-route Gets


@dataclasses.dataclass(frozen=True)
class Hop:
    """A port to Get by directed route: route is smpquery -D's exit ports,
    (0,) for this host's port, and via and via_port the switch (or this
    host's port) and port it's reached through, None for this host's port"""

    guid: int
    route: tuple
    via: int | None = None
    via_port: int | None = None

    @property
    def route_text(self):
        """The route as smpquery -D takes it"""
        return ",".join(str(port) for port in self.route)


def next_hops(links, own_guid, routes, answered):
    """The next level of ports to Get, along recorded links

    routes maps every GUID already given a route (whether or not it
    answered) to it; answered holds the GUIDs of switches that answered a
    Get with the GUID recorded on their route. The first level is this
    host's port, at route (0,); the next is what its link reaches, at
    (0, <its port number>); then one exit port per switch hop. Routes go
    from this host's port and through answering switches only: never
    through an HCA, and never through a switch that doesn't answer. Called
    once per level, with what the last level found, each level's routes are
    one hop longer than the last's, so the shortest route wins; among routes
    of a length, the lowest exit ports do.
    """
    routes = dict(routes)
    if own_guid not in routes:
        return [Hop(own_guid, (0,))]
    best = {}
    for link in sorted(paired(links), key=lambda link: link.key):
        source = link.guid
        if source != own_guid and source not in answered:
            continue
        if source not in routes or link.peer_guid in routes:
            continue
        route = tuple(routes[source]) + (link.port,)
        current = best.get(link.peer_guid)
        if current is None or (len(route), route) < (
            len(current.route),
            current.route,
        ):
            best[link.peer_guid] = Hop(link.peer_guid, route, source, link.port)
    return sorted(best.values(), key=lambda hop: (len(hop.route), hop.route, hop.guid))


def all_routes(links, own_guid, answered):
    """Every recorded GUID's Hop, level by level as next_hops gives them,
    following only the switches in answered: for Gets to a known port, such
    as an open job's, along what the last verify reached"""
    hops = {}
    while True:
        level = next_hops(
            links, own_guid, {guid: hop.route for guid, hop in hops.items()}, answered
        )
        if not level:
            return hops
        hops.update((hop.guid, hop) for hop in level)


# Judgements over the rows


def changing(ports, target_on):
    """The rows whose holds isn't what the target gives them, by GUID: a
    port changing needs no state of its own (requirement 1)"""
    target = target_holds(target_on)
    return sorted(
        (row for row in _rows(ports) if row.holds != target),
        key=lambda row: row.guid,
    )


def transition_complete(ports, target_on):
    """Whether every recorded port that's reachable holds what the target
    gives it (Turning it on: the transition is then complete)"""
    target = target_holds(target_on)
    return all(row.holds == target for row in _rows(ports) if row.reachable)


def forget_problem(ports, group_named):
    """Why --forget-port-keys can't drop the rows, or None if it can

    The rows are what OpenSM's cache is rendered from: while the group is
    named, or any port holds its derived key, dropping them would lose the
    ports that hold derived keys (Turning it off).
    """
    if group_named:
        return "the m_key_per_port hardening group is named"
    derived = sum(1 for row in _rows(ports) if row.holds == DERIVED)
    if derived:
        return f"{derived} ports hold their derived keys"
    return None


# The database


def _utc(when):
    """SQLite gives back naive datetimes, which are UTC"""
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=datetime.timezone.utc)
    return when


def load(session):
    """The rows, as (ports by GUID, links by (GUID, port))"""
    ports = {}
    for row in session.scalars(sa.select(db.FabricPort)):
        guid = parse_guid(row.guid)
        ports[guid] = PortState(
            guid=guid,
            node_guid=None if row.node_guid is None else parse_guid(row.node_guid),
            switch=row.switch,
            holds=row.holds,
            lapsed=bool(row.lapsed),
            reachable=bool(row.reachable),
            released=bool(row.released),
            last_seen=_utc(row.last_seen),
        )
    links = {}
    for row in session.scalars(sa.select(db.FabricLink)):
        link = Link(
            guid=parse_guid(row.guid),
            port=row.port,
            peer_guid=parse_guid(row.peer_guid),
            peer_port=row.peer_port,
            last_seen=_utc(row.last_seen),
        )
        links[link.key] = link
    return ports, links


def save(session, ports, links):
    """Make the rows those given, deleting any others, in the session's
    transaction: the caller commits, under the term's compare-and-set where
    that applies"""
    existing = {row.guid: row for row in session.scalars(sa.select(db.FabricPort))}
    wanted = {guid_text(port.guid): port for port in _rows(ports)}
    for guid, row in existing.items():
        if guid not in wanted:
            session.delete(row)
    for guid, port in wanted.items():
        row = existing.get(guid)
        if row is None:
            row = db.FabricPort(guid=guid)
            session.add(row)
        row.node_guid = None if port.node_guid is None else guid_text(port.node_guid)
        row.switch = port.switch
        row.holds = port.holds
        row.lapsed = port.lapsed
        row.reachable = port.reachable
        row.released = port.released
        row.last_seen = port.last_seen

    existing = {
        (row.guid, row.port): row for row in session.scalars(sa.select(db.FabricLink))
    }
    wanted = {(guid_text(link.guid), link.port): link for link in _rows(links)}
    for key, row in existing.items():
        if key not in wanted:
            session.delete(row)
    for key, link in wanted.items():
        row = existing.get(key)
        if row is None:
            row = db.FabricLink(guid=key[0], port=key[1])
            session.add(row)
        row.peer_guid = guid_text(link.peer_guid)
        row.peer_port = link.peer_port
        row.last_seen = link.last_seen


def upsert(session, ports=(), links=()):
    """Write these rows, adding any that are missing, and leave every other
    row as it is, in the session's transaction

    Unlike save, nothing is deleted: a command run beside the manager, such
    as --add-link, may have added a row since the caller read them, and a
    write of one operation's changes mustn't undo another's.
    """
    for port in _rows(ports):
        guid = guid_text(port.guid)
        row = session.get(db.FabricPort, guid)
        if row is None:
            row = db.FabricPort(guid=guid)
            session.add(row)
        row.node_guid = None if port.node_guid is None else guid_text(port.node_guid)
        row.switch = port.switch
        row.holds = port.holds
        row.lapsed = port.lapsed
        row.reachable = port.reachable
        row.released = port.released
        row.last_seen = port.last_seen
    for link in _rows(links):
        key = (guid_text(link.guid), link.port)
        row = session.get(db.FabricLink, key)
        if row is None:
            row = db.FabricLink(guid=key[0], port=key[1])
            session.add(row)
        row.peer_guid = guid_text(link.peer_guid)
        row.peer_port = link.peer_port
        row.last_seen = link.last_seen


def _load_flag(session, name):
    row = session.get(db.State, name)
    return bool(row is not None and row.value)


def _save_flag(session, name, on):
    row = session.get(db.State, name)
    if row is None:
        session.add(db.State(name=name, value=int(bool(on))))
    else:
        row.value = int(bool(on))


def load_target(session):
    """Whether the recorded target is per-port keys on; off with no row"""
    return _load_flag(session, TARGET)


def save_target(session, on):
    """Record the target, in the session's transaction"""
    _save_flag(session, TARGET, on)


def load_running(session):
    """Whether OpenSM was last started with per-port keys on; off with no
    row"""
    return _load_flag(session, RUNNING)


def save_running(session, on):
    """Record the target OpenSM is about to be started on, in the session's
    transaction"""
    _save_flag(session, RUNNING, on)


def load_confirmed(session):
    """Whether a start with the target on has met the success rule"""
    return _load_flag(session, CONFIRMED)


def save_confirmed(session, on):
    """Record whether a start with the target on has met the success rule,
    in the session's transaction"""
    _save_flag(session, CONFIRMED, on)


def load_text(session, name):
    """A state row's text, or None with no row"""
    row = session.get(db.State, name)
    return None if row is None else row.text


def save_text(session, name, text):
    """Record a state row's text, None deleting the row, in the session's
    transaction"""
    row = session.get(db.State, name)
    if text is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(db.State(name=name, value=0, text=text))
    else:
        row.text = text


def load_time(session, name):
    """A Unix time a state row records, or None with no row"""
    row = session.get(db.State, name)
    return None if row is None else row.value


def save_time(session, name, when):
    """Record a Unix time, as whole seconds, in the session's transaction"""
    row = session.get(db.State, name)
    if row is None:
        session.add(db.State(name=name, value=int(when)))
    else:
        row.value = int(when)


def failure_record(base, config_hash):
    """What a failed turn-on records: the base's fingerprint and the
    configuration hash, never the base or the configuration"""
    if not re.fullmatch(r"[0-9a-f]{64}", config_hash or ""):
        raise ValueError("not a configuration hash")
    return f"{fingerprint(base)}:{config_hash}"


def load_fingerprint(session):
    """The recorded fingerprint of the base the ports were keyed from, or
    None"""
    row = session.get(db.State, BASE_FINGERPRINT)
    return None if row is None else row.text


def save_fingerprint(session, value):
    """Record the base's fingerprint (fingerprint(base), never the base), in
    the session's transaction"""
    # A key passed by mistake would be stored in the clear, so only what
    # fingerprint() returns is taken.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("not a fingerprint: pass fingerprint(base)")
    row = session.get(db.State, BASE_FINGERPRINT)
    if row is None:
        session.add(db.State(name=BASE_FINGERPRINT, value=0, text=value))
    else:
        row.text = value
