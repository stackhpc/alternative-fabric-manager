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

"""While a switch is missing: the possibly held memberships, taint, and
which grants complete on a partial check

specs/ha.md, While a switch is missing, is the source of truth. These are
the pure rules; the manager decides when they run, and records what they
need in applied.

The possibly held map is kept as JSON in applied.held:
  {"held": {pkey: [[GUID, membership, host], ...]},
   "latest": {pkey: [[GUID, membership, host], ...]},
   "blocked": whether an expected switch was removed since the last reset,
   "blocked_after": when the block was set, the time of the last fabric
   check then, as POSIX seconds, 0 if there was none; absent in a map
   written before it was recorded}
pkeys as decimal strings, GUIDs as 16 hex digits, host the port's
binding:host_id, "" for a member that is no Neutron port's.
"""

import hashlib
import json

from alternative_fabric_manager import partitions

# The vnic_type of a virtual function, the only ports exempt from host taint
VIRTUAL_FUNCTION = "direct"


def body_entries(content, hosts):
    """A body's explicit memberships with their hosts, as {pkey: {(GUID,
    membership, host)}}: hosts maps GUIDs, as 16 hex digits, to the hosts of
    the ports that have them"""
    entries = {}
    for pkey, members in partitions.memberships(content).items():
        for guid, membership in members.items():
            text = f"{guid:016x}"
            entries.setdefault(pkey, set()).add((text, membership, hosts.get(text, "")))
    return entries


def _load_map(data):
    return {
        int(pkey): {tuple(entry) for entry in entries}
        for pkey, entries in (data or {}).items()
    }


def _dump_map(entries):
    return {
        str(pkey): sorted(list(entry) for entry in values)
        for pkey, values in sorted(entries.items())
        if values
    }


def body_hash(content):
    """A body's fingerprint, by which the map knows whether it saw the body
    applied records as swept"""
    return hashlib.sha256((content or "").encode()).hexdigest()


# How many of the latest bodies' (generation, fingerprint) the map keeps, so
# that a body written and then swept again later is still known as seen
RECENT = 16


class Held:
    """The possibly held memberships, the latest body's, whether an expected
    switch was removed since the map was last reset, and whether the map is
    complete: made so by a reset, so that it covers every membership a port
    may hold, and still so only while every body swept is one it saw"""

    def __init__(
        self,
        held=None,
        latest=None,
        blocked=False,
        known=True,
        complete=False,
        recent=None,
        blocked_after=None,
    ):
        self.held = held or {}
        self.latest = latest or {}
        self.blocked = blocked
        # The last fabric check's time when the block was set: a check
        # taken after it is one that judged the confirmed switches. The
        # check's own clock, the leader's, rather than the confirming
        # node's, so that skew between them can't pass an older check.
        self.blocked_after = blocked_after
        # False until a body has been added, as before this was recorded
        self.known = known
        self.complete = complete
        # The (generation, fingerprint) of the latest bodies added, newest
        # last
        self.recent = list(recent or [])

    @classmethod
    def loads(cls, text):
        if not text:
            return cls(known=False)
        data = json.loads(text)
        return cls(
            _load_map(data.get("held")),
            _load_map(data.get("latest")),
            bool(data.get("blocked")),
            complete=bool(data.get("complete")),
            recent=[tuple(pair) for pair in data.get("recent", [])],
            blocked_after=data.get("blocked_after"),
        )

    def dumps(self):
        return json.dumps(
            {
                "held": _dump_map(self.held),
                "latest": _dump_map(self.latest),
                "blocked": self.blocked,
                "blocked_after": self.blocked_after,
                "complete": self.complete,
                "recent": [list(pair) for pair in self.recent],
            },
            sort_keys=True,
        )

    def saw(self, generation, content):
        """Whether the map saw this body, as applied records it swept"""
        return bool(content) and (generation, body_hash(content)) in self.recent

    def latest_is(self, content):
        """Whether the latest body added is this one"""
        return bool(self.recent) and self.recent[-1][1] == body_hash(content)

    def add(self, entries, generation=None, content=None):
        """Add a body's memberships, before it's written to OpenSM's file:
        it is the latest body from then on"""
        for pkey, values in entries.items():
            self.held.setdefault(pkey, set()).update(values)
        self.latest = {pkey: set(values) for pkey, values in entries.items()}
        self.known = True
        if content is not None:
            self.recent = [*self.recent, (generation, body_hash(content))][-RECENT:]

    def reset(self, error_guids=()):
        """Reset to the latest body's memberships, which the caller has
        checked are the swept body's, plus the entries of every GUID that
        had a pkey error in the sweep, which may still hold what OpenSM
        couldn't take from them; the map is complete from then on. Return
        the pkeys this untaints."""
        before = self.tainted_pkeys()
        errors = set(error_guids)
        held = {pkey: set(values) for pkey, values in self.latest.items()}
        for pkey, values in self.held.items():
            kept = {entry for entry in values if entry[0] in errors}
            if kept:
                held.setdefault(pkey, set()).update(kept)
        self.held = held
        self.blocked = False
        self.blocked_after = None
        self.complete = True
        return before - self.tainted_pkeys()

    def tainted_pkeys(self):
        """The pkeys for which the map holds a (GUID, membership) the latest
        body doesn't: a GUID removed from it, or moved from full to limited"""
        tainted = set()
        for pkey, values in self.held.items():
            latest = {(guid, kind) for guid, kind, _host in self.latest.get(pkey, ())}
            if any((guid, kind) not in latest for guid, kind, _host in values):
                tainted.add(pkey)
        return tainted

    def tainted_hosts(self):
        """The hosts for which the map holds a (GUID, host) the latest body
        doesn't: a port that left it, by deletion, migration or rebinding"""
        latest = {
            (guid, host) for values in self.latest.values() for guid, _k, host in values
        }
        return {
            host
            for values in self.held.values()
            for guid, _kind, host in values
            if host and (guid, host) not in latest
        }


def is_grant(effect):
    return bool(effect.get("grant"))


def revocations(jobs):
    """The open revocations' (port ID or None, hosts or None), from (job,
    effect) pairs: hosts None for a job from before they were recorded"""
    found = []
    for _job, effect in jobs:
        if is_grant(effect):
            continue
        port = effect.get("port") if effect.get("kind") in ("port", "no_port") else None
        hosts = effect.get("hosts")
        found.append((port, None if hosts is None else {h for h in hosts if h}))
    return found


def judge_grant(
    effect,
    *,
    placed,
    pkey_errors,
    tainted_pkeys,
    tainted_hosts,
    open_revocations,
    vnic_type=None,
    complete=True,
):
    """Why a job waits on a partial check, or None if it completes there,
    given that conditions 1, 2, 3 and the partial reading of 5 of Job
    completion hold (specs/ha.md, A grant completes on a partial check)

    placed is the GUIDs the check placed as the last scan before the sweep
    had them; pkey_errors the sweep's; vnic_type the port row's, if live;
    complete whether the possibly held map is complete.
    """
    # Rule 1: only grants
    if not is_grant(effect):
        return "a revocation waits for the whole fabric"
    kind = effect.get("kind")
    if kind in ("network", "flat_network"):
        # A network's partition has exactly its ports as members, so a new
        # one grants nothing to any port.
        return None
    if kind != "port":
        return "only a port's or a network's grant completes on a partial check"
    if not complete:
        # It may lack memberships some port still holds, as after an
        # upgrade, so taint can't be judged.
        return (
            "the possibly held memberships aren't complete until a sweep of "
            "the whole fabric"
        )
    guid = effect["guids"][0]
    # Rule 2
    if guid in pkey_errors:
        return f"OpenSM couldn't set 0x{guid}'s pkey table"
    if guid not in placed:
        return f"0x{guid} wasn't placed in the same place either side of the sweep"
    # Rule 3
    if effect["pkey"] in tainted_pkeys:
        return f"pkey 0x{effect['pkey']:04x} is tainted: a port may hold it stale"
    # Rule 4: any port but a virtual function's, including one whose type
    # isn't recorded or isn't known
    if is_virtual_function(effect, vnic_type):
        return None
    host = effect.get("host")
    if not host:
        return "its host isn't known"
    if host in tainted_hosts:
        return f"host {host} is tainted: a port that left it may hold stale pkeys"
    for port, hosts in open_revocations:
        if port is not None and port == effect.get("port"):
            continue
        if hosts is None or host in hosts:
            return f"a revocation for another port on host {host} is open"
    return None


def is_virtual_function(effect, vnic_type=None):
    """Whether a port's grant is a virtual function's, by its request's
    vnic_type and, if it is live, its row's"""
    types = {effect.get("vnic_type")}
    if vnic_type is not None:
        types.add(vnic_type)
    return types == {VIRTUAL_FUNCTION}


def host_place(host, scanned, physical_ports):
    """Where a virtual function whose vport the dump doesn't place is
    placed: by its host's learnt physical ports, if it has any and the scan
    placed every one, as ["host", host, [[physical port GUID, place], ...]];
    None otherwise (specs/ha.md, Where a port is)"""
    if not host or not physical_ports:
        return None
    places = []
    for guid in sorted(physical_ports):
        where = scanned.get(guid)
        if where is None:
            return None
        places.append([guid, where])
    return ["host", host, places]
