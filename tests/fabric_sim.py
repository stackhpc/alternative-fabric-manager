"""A simulated InfiniBand fabric, for the prober and for OpenSM

The simulator answers a Get only as InfiniBand would: a port at protection
level 2 or more answers only the key it holds, a port at 0 or 1 answers any,
and nothing crosses a link that's down. It records every Get it was sent, by
the port its route leads to, so that the tests can assert what was never
sent: above all, that a tenant's port isn't sent a key the rules don't
allow it.

sweep() is OpenSM's sweep as hardware test 5 saw it (specs/secured-
profile.md, Results): it reaches a port only through a GUID it knows, its
own or one in neighbors, sends the port's guid2mkey entry if it has one,
and otherwise the key derived from its m_key line with m_key_per_port, or
that line without it; a port that refuses isn't re-keyed, and a switch that
refuses isn't routed through.
"""

import dataclasses
import datetime

from alternative_fabric_manager import port_keys
from alternative_fabric_manager.port_keys import DERIVED, M_KEY, PortState

M = 0x2222222222222222
BASE = 0x1111111111111111
NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
EARLIER = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)

OWN = 0xA0
S1 = 0x51
S2 = 0x52
T1, T2, T3, T4 = 0xB1, 0xB2, 0xB3, 0xB4


def derived(guid, base=BASE):
    return port_keys.port_m_key(base, guid)


@dataclasses.dataclass
class SimPort:
    guid: int
    switch: bool
    key: int
    protect: int = 2
    node_guid: int = 0
    num_ports: int = 1


class Sim:
    def __init__(self, own=OWN, own_guids=None, m_key=M, base=BASE):
        self.m_key = m_key
        self.base = base
        self.ports = {}
        self.cables = {}
        self.down = set()
        self.own = own
        self.own_guids = list(own_guids or [own])
        # Every Get: (the GUID its route leads to, key, attr, port)
        self.sent = []
        # The GUIDs each batch's Gets went to
        self.batches = []
        self.no_portinfo = set()
        self.on_batch = None
        # Every key any sender sent: (sender, GUID, key sent, key the port
        # held then), for the security assertions of whole scenarios
        self.history = []

    def add(self, guid, switch=False, key=None, protect=2, node_guid=None, ports=1):
        self.ports[guid] = SimPort(
            guid,
            switch,
            port_keys.port_m_key(self.base, guid) if key is None else key,
            protect,
            node_guid if node_guid is not None else guid + 0x1000,
            ports,
        )

    def cable(self, a, a_port, b, b_port, up=True):
        self.cables[(a, a_port)] = (b, b_port)
        self.cables[(b, b_port)] = (a, a_port)
        if not up:
            self.down |= {(a, a_port), (b, b_port)}

    def resolve(self, route):
        """The port a route leads to, ignoring link states, and whether the
        Get can get there"""
        hops = [int(hop) for hop in route.split(",")][1:]
        current, deliverable = self.own, True
        for i, port in enumerate(hops):
            if i > 0 and not self.ports[current].switch:
                return None, False
            peer = self.cables.get((current, port))
            if peer is None:
                return None, False
            if (current, port) in self.down:
                deliverable = False
            current = peer[0]
        return current, deliverable

    def link_state(self, guid, port):
        cabled = (guid, port) in self.cables
        return "Active" if cabled and (guid, port) not in self.down else "Down"

    def answer(self, guid, deliverable, get):
        if guid is None or not deliverable:
            return None
        port = self.ports[guid]
        if port.protect >= 2 and get.key != port.key:
            return None
        if get.attr == "nodeinfo":
            return {
                "NodeType": "Switch" if port.switch else "Channel Adapter",
                "NumPorts": str(port.num_ports),
                "Guid": f"0x{port.node_guid:016x}",
                "PortGuid": f"0x{guid:016x}",
                "LocalPort": "1",
            }
        if guid in self.no_portinfo:
            return None
        if port.switch:
            state = "Active" if not get.port else self.link_state(guid, get.port)
        else:
            state = self.link_state(guid, 1)
        return {
            "LocalPort": str(get.port or 1),
            "LinkState": state,
            "PhysLinkState": "LinkUp" if state != "Down" else "Polling",
            "ProtectBits": str(port.protect),
        }

    def run(self, gets):
        if self.on_batch:
            self.on_batch(self)
        answers = {}
        batch = []
        for get in gets:
            guid, deliverable = self.resolve(get.route)
            self.sent.append((guid, get.key, get.attr, get.port))
            self.note(guid, get.key, "prober")
            batch.append(guid)
            answers[get.id] = self.answer(guid, deliverable, get)
        self.batches.append(batch)
        return list(self.own_guids), answers

    def keys_sent(self, guid):
        return [key for to, key, _attr, _port in self.sent if to == guid]

    def rows(self, holds=None, switch_known=True, changes=None):
        """Rows as a fold of OpenSM's cache would give them: every cabled
        port, holding what holds says (what it holds in the simulator by
        default)"""
        holds = holds or {}
        changes = changes or {}
        rows = {}
        for guid, port in self.ports.items():
            held = M_KEY if port.key == self.m_key else DERIVED
            fields = {
                "switch": (port.switch or None) if switch_known else None,
                "holds": holds.get(guid, held),
                "reachable": True,
                "last_seen": EARLIER,
                **changes.get(guid, {}),
            }
            rows[guid] = PortState(guid, **fields)
        return rows

    def links(self):
        return {
            (guid, port): port_keys.Link(guid, port, *peer)
            for (guid, port), peer in self.cables.items()
        }

    def note(self, guid, key, sender):
        """Record that sender sent a port a key"""
        port = self.ports.get(guid)
        held = None if port is None else port.key
        self.history.append((sender, guid, key, held))

    def accepts(self, guid, key):
        port = self.ports[guid]
        return port.protect < 2 or key == port.key

    def sweep(self, per_port, m_line, neighbors, entries):
        """OpenSM's sweep: returns the GUIDs it reached, which hold the key
        it gives them, or None if this host's port refused it, so that it
        can't sweep at all (test 5's ERR 5430)

        neighbors is a list of port_keys.Links, entries guid2mkey's.
        """
        known = {self.own}
        for link in neighbors:
            known |= {link.guid, link.peer_guid}

        def key_for(guid):
            if guid in entries:
                return entries[guid]
            if guid not in known:
                return 0 if per_port else m_line
            return port_keys.port_m_key(m_line, guid) if per_port else m_line

        def send(guid):
            key = key_for(guid)
            self.note(guid, key, "opensm")
            return self.accepts(guid, key)

        if not send(self.own):
            return None
        reached = [self.own]
        queue = [self.own]
        while queue:
            guid = queue.pop(0)
            port = self.ports[guid]
            numbers = range(1, port.num_ports + 1) if port.switch else (1,)
            if not port.switch and guid != self.own:
                continue
            for number in numbers:
                peer = self.cables.get((guid, number))
                if peer is None or (guid, number) in self.down:
                    continue
                peer_guid = peer[0]
                if peer_guid in reached:
                    continue
                if send(peer_guid):
                    reached.append(peer_guid)
                    if self.ports[peer_guid].switch:
                        queue.append(peer_guid)
        for guid in reached:
            port = self.ports[guid]
            port.key = port_keys.port_m_key(m_line, guid) if per_port else m_line
            port.protect = 2
        return reached

    def neighbors_of(self, guids):
        """The neighbors lines OpenSM writes for the links among these
        ports"""
        return [
            port_keys.Link(guid, port, *peer)
            for (guid, port), peer in sorted(self.cables.items())
            if guid in guids and peer[0] in guids and (guid, port) not in self.down
        ]


def fabric():
    """This host's port on S1, S2 behind S1, tenants T1 and T2 on S1, T3 on
    S2, and T4 on S1 behind a link that's down; every port on its derived
    key"""
    sim = Sim()
    sim.add(OWN)
    sim.add(S1, switch=True, ports=8)
    sim.add(S2, switch=True, ports=4)
    for tenant in (T1, T2, T3, T4):
        sim.add(tenant)
    sim.cable(OWN, 1, S1, 1)
    sim.cable(S1, 2, S2, 1)
    sim.cable(S1, 3, T1, 1)
    sim.cable(S1, 4, T2, 1)
    sim.cable(S2, 2, T3, 1)
    sim.cable(S1, 5, T4, 1, up=False)
    return sim
