"""While a switch is missing: grants complete on a partial check when the
switch can't matter to them, and every other job waits for a whole fabric
(specs/ha.md, While a switch is missing)"""

import json

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db, partial, scan
from alternative_fabric_manager.drivers import Fabric, ReloadError, SweepReport
from tests import conftest
from tests.conftest import GUID, NETWORK_ID, PORT_ID
from tests.fabric_sim import S1, S2
from tests.test_port_keeper import HV, turned_on, world  # noqa: F401 - the fixture

LEAF_A = 0xEC0D9A0300B12560
LEAF_B = 0xEC0D9A0300B12570
OWN = 0x040973FFFFC75168
OTHER_PORT = "e13bba05-eb07-45ba-ace2-765706b2d701"
THIRD_PORT = "f13bba05-eb07-45ba-ace2-765706b2d701"
OTHER_NETWORK = "d13bba05-eb07-45ba-ace2-765706b2d701"
OTHER_CLIENT_ID = "20:12:34:56:00:00:78:21:b7"
OTHER_GUID = 0x12345600007821B7
THIRD_CLIENT_ID = "20:12:34:56:00:00:78:21:b8"
THIRD_GUID = 0x12345600007821B8
VF = 0xFA163E000062A357
VF_CLIENT_ID = "20:fa:16:3e:00:00:62:a3:57"


class PartialReloader(conftest.FakeReloader):
    """A driver whose scan places ports on leaves, the missing ones' not,
    and whose OpenSM logs a sweep start at each start and reload"""

    def __init__(self):
        super().__init__()
        self.switches = {LEAF_A, LEAF_B}
        # Each adapter port's (leaf, port)
        self.ports = {
            OWN: (LEAF_A, 1),
            GUID: (LEAF_A, 2),
            OTHER_GUID: (LEAF_A, 3),
            THIRD_GUID: (LEAF_A, 4),
        }
        self.vports = []
        self.starts = 0
        self.scan_error = None
        self.scans = 0
        # Called during each scan, as OpenSM might sweep meanwhile
        self.during_scan = None
        self.others = []

    def reload(self):
        self.starts += 1
        return super().reload()

    def fabric(self, m_key=None, sa_key=None, switches=None):
        self.fabric_calls.append((m_key, sa_key))
        if isinstance(self.fabric_result, Exception):
            raise self.fabric_result
        return Fabric(self.switches, [(OWN, 3), *self.others], {OWN})

    def scan(self, m_key=None):
        self.scans += 1
        if self.during_scan:
            self.during_scan()
        if self.scan_error:
            raise self.scan_error
        return {
            guid: ["port", f"{leaf:016x}", port]
            for guid, (leaf, port) in self.ports.items()
            if leaf in self.switches
        }

    def virtualization_dump(self):
        rows = "".join(
            f"0x{port:016x}, {index}, 0x{guid:016x}, 0x{guid:016x}, 1, {state}, "
            "1, 0, 9, 9\n"
            for guid, port, index, state in self.vports
        )
        return (
            "START_VPORTS\nPortGUID, Index, VPortGUID, VNodeGUID, VPortNum, State, "
            "IsLidRequired, VPortLidIndex, VPortLid, ActiveLid\n"
            + rows
            + "END_VPORTS\n"
        )

    def sweep_starts(self):
        return ("opensm", self.starts)


@pytest.fixture
def reloader():
    return PartialReloader()


@pytest.fixture
def fabric(manager, reloader):
    """Two leaves, confirmed, and a network with a port on each host"""
    assert manager.confirm_fabric().switches == sorted((LEAF_A, LEAF_B))
    return reloader


def _status(manager, job_id):
    return manager.get_job(job_id).status


def _settled(manager, *jobs):
    manager.reconcile()
    for job in jobs:
        assert _status(manager, job) == db.JobStatus.COMPLETED


def _lose_leaf(manager, fabric):
    """Leaf B stops answering; the check after is partial"""
    fabric.switches = {LEAF_A}
    assert not manager.check_fabric()
    with manager.sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        assert applied.check_partial


def _port(port_id, client_id, host="compute-0", vnic_type="normal", **kwargs):
    return conftest.port(
        port_id,
        client_id=client_id,
        **{"binding:host_id": host, "binding:vnic_type": vnic_type},
        **kwargs,
    )


def _applied(manager):
    with manager.sessionmaker() as session:
        return session.get(db.Applied, 1)


COMPLETED = db.JobStatus.COMPLETED
PENDING = db.JobStatus.PENDING


def test_a_grant_completes_on_a_partial_check(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    network = manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200))
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    manager.reconcile()
    assert _status(manager, job) == COMPLETED
    # A network's creation grants nothing to any port.
    assert _status(manager, network) == COMPLETED
    applied = _applied(manager)
    assert not applied.check_ok
    assert applied.check_partial
    assert json.loads(applied.check_placed) == [f"{GUID:016x}"]


def test_a_partial_check_is_still_a_failed_check(manager, fabric):
    _lose_leaf(manager, fabric)
    manager.check_fabric()
    # It counts towards stepping down, and records no switch.
    assert manager.missing_checks == 2
    with manager.sessionmaker() as session:
        assert {s.guid for s in session.scalars(db.sa.select(db.ExpectedSwitch))} == {
            f"{LEAF_A:016x}",
            f"{LEAF_B:016x}",
        }
    assert "Missing switches" in _applied(manager).check_detail


def test_a_revocation_waits_for_the_whole_fabric_and_a_fresh_sweep(manager, fabric):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID)),
    )
    _lose_leaf(manager, fabric)
    job = manager.delete_port(PORT_ID)
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == PENDING
    # The leaf is back: a check passes, the leader sweeps again, and only a
    # check after that sweep completes the revocation.
    fabric.switches = {LEAF_A, LEAF_B}
    calls = fabric.calls
    manager.check_fabric()
    manager.evaluate_jobs()
    assert _status(manager, job) == PENDING
    assert _applied(manager).missing
    manager.reconcile()
    assert fabric.calls == calls + 1
    assert _status(manager, job) == COMPLETED
    assert not _applied(manager).missing


def test_a_port_moving_network_is_a_revocation(manager, fabric):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200)),
        manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID)),
    )
    _lose_leaf(manager, fabric)
    job = manager.put_port(
        PORT_ID, _port(PORT_ID, conftest.CLIENT_ID, network_id=OTHER_NETWORK)
    )
    manager.reconcile()
    assert _status(manager, job) == PENDING


def test_a_network_update_waits(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.put_network(NETWORK_ID, conftest.network(segmentation_id=300))
    manager.reconcile()
    assert _status(manager, job) == PENDING


def test_a_tainted_pkey_waits(manager, fabric):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h2")),
    )
    _lose_leaf(manager, fabric)
    # A port leaves pkey 100 while the leaf is missing: it may hold it stale.
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID, host="h1"))
    manager.reconcile()
    assert _status(manager, job) == PENDING
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.tainted_pkeys() == {100}
    # Another pkey isn't.
    manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200))
    other = manager.put_port(
        THIRD_PORT,
        _port(THIRD_PORT, THIRD_CLIENT_ID, host="h1", network_id=OTHER_NETWORK),
    )
    manager.reconcile()
    assert _status(manager, other) == COMPLETED


def test_a_tainted_host_waits_but_a_virtual_function_doesnt(manager, fabric):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200)),
        manager.put_port(
            OTHER_PORT,
            _port(OTHER_PORT, OTHER_CLIENT_ID, host="h1", network_id=OTHER_NETWORK),
        ),
    )
    _lose_leaf(manager, fabric)
    # A port leaves host h1, by its deletion, so h1 and pkey 200 are tainted.
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    bare_metal = manager.put_port(
        PORT_ID, _port(PORT_ID, conftest.CLIENT_ID, host="h1")
    )
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    manager.reconcile()
    vf = manager.put_port(
        THIRD_PORT, _port(THIRD_PORT, VF_CLIENT_ID, host="h1", vnic_type="direct")
    )
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, bare_metal) == PENDING
    assert _status(manager, vf) == COMPLETED


@pytest.mark.parametrize("vnic_type", ["direct-physical", "baremetal", None])
def test_an_unknown_or_unrecorded_vnic_type_gets_the_host_rule(
    manager, fabric, vnic_type
):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200)),
        manager.put_port(
            OTHER_PORT,
            _port(OTHER_PORT, OTHER_CLIENT_ID, host="h1", network_id=OTHER_NETWORK),
        ),
    )
    _lose_leaf(manager, fabric)
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    data = _port(PORT_ID, conftest.CLIENT_ID, host="h1")
    data.pop("binding:vnic_type")
    if vnic_type is not None:
        data["binding:vnic_type"] = vnic_type
    job = manager.put_port(PORT_ID, data)
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == PENDING
    with manager.sessionmaker() as session:
        assert session.get(db.Port, PORT_ID).vnic_type == vnic_type


def test_an_open_revocation_on_the_host_holds_its_grants(manager, fabric):
    third = "a13bba05-eb07-45ba-ace2-765706b2d701"
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200)),
        manager.put_network(third, conftest.network(third, 300)),
        manager.put_port(
            OTHER_PORT,
            _port(OTHER_PORT, OTHER_CLIENT_ID, host="h1", network_id=OTHER_NETWORK),
        ),
    )
    _lose_leaf(manager, fabric)
    # The port moves network on h1: a revocation, which stays open. It stays
    # on h1, so h1 isn't tainted, only pkey 200 is.
    revocation = manager.put_port(
        OTHER_PORT,
        _port(OTHER_PORT, OTHER_CLIENT_ID, host="h1", network_id=NETWORK_ID),
    )
    manager.reconcile()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.tainted_hosts() == set()
    assert held.tainted_pkeys() == {200}
    job = manager.put_port(
        PORT_ID, _port(PORT_ID, conftest.CLIENT_ID, host="h1", network_id=third)
    )
    manager.reconcile()
    assert _status(manager, revocation) == PENDING
    assert _status(manager, job) == PENDING
    # On another host, the same grant completes.
    elsewhere = manager.put_port(
        THIRD_PORT, _port(THIRD_PORT, THIRD_CLIENT_ID, host="h2", network_id=third)
    )
    manager.reconcile()
    assert _status(manager, elsewhere) == COMPLETED


def test_a_moved_guid_waits(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    # The port is recabled while the sweep runs.
    fabric.during = lambda: fabric.ports.__setitem__(GUID, (LEAF_A, 9))
    manager.reconcile()
    assert _status(manager, job) == PENDING
    assert json.loads(_applied(manager).check_placed) == []


def test_an_extra_sweep_start_in_the_window_waits(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))

    def sweep_of_its_own():
        fabric.starts += 1

    fabric.during_scan = sweep_of_its_own
    manager.reconcile()
    assert _status(manager, job) == PENDING


def test_a_port_the_scan_before_the_sweep_didnt_place_waits(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    del fabric.ports[GUID]
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    fabric.ports[GUID] = (LEAF_A, 2)
    manager.reconcile()
    assert _status(manager, job) == PENDING


def test_a_duplicate_vport_isnt_placed(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    fabric.vports = [(VF, OWN, 7, "ACTIVE"), (VF, GUID, 3, "ACTIVE")]
    _lose_leaf(manager, fabric)
    job = manager.put_port(
        THIRD_PORT, _port(THIRD_PORT, VF_CLIENT_ID, vnic_type="direct")
    )
    manager.reconcile()
    assert _status(manager, job) == PENDING
    # Listed once, it is placed.
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    manager.check_fabric()
    job = manager.put_port(
        THIRD_PORT, _port(THIRD_PORT, VF_CLIENT_ID, vnic_type="direct")
    )
    manager.reconcile()
    assert _status(manager, job) == COMPLETED


def test_a_port_behind_the_missing_leaf_waits(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    fabric.ports[GUID] = (LEAF_B, 2)
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    manager.reconcile()
    assert _status(manager, job) == PENDING


@pytest.mark.parametrize(
    "fault",
    ["another master", "scan fails", "pkey error", "unattributed", "no scan"],
)
def test_a_check_that_isnt_partial_completes_no_grant(manager, fabric, fault):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    if fault == "another master":
        fabric.others = [(0x1234, 3)]
    elif fault == "scan fails":
        fabric.scan_error = ReloadError("ibnetdiscover failed")
    elif fault == "pkey error":
        fabric.report = SweepReport(pkey_errors={GUID})
    elif fault == "unattributed":
        fabric.report = SweepReport(unattributed=True)
    else:
        fabric.scan = None
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    manager.reconcile()
    assert _status(manager, job) == PENDING
    if fault in ("another master", "scan fails", "no scan"):
        assert not _applied(manager).check_partial


def test_the_held_map_is_extended_before_each_write(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID, host="h1"))

    def written():
        with manager.sessionmaker() as session:
            held = partial.Held.loads(session.get(db.Applied, 1).held)
        assert held.held[100] == {(f"{GUID:016x}", "full", "h1")}

    # Before OpenSM is signalled, the map has the body.
    fabric.during = written
    manager.reconcile()


def test_the_map_resets_at_a_passing_check_after_a_fresh_sweep(manager, fabric):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h2")),
    )
    _lose_leaf(manager, fabric)
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    manager.reconcile()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    # Not pruned while the fabric is incomplete
    assert held.tainted_pkeys() == {100}
    assert held.tainted_hosts() == {"h2"}
    fabric.switches = {LEAF_A, LEAF_B}
    manager.reconcile()
    manager.reconcile()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.tainted_pkeys() == set()
    assert held.tainted_hosts() == set()


def test_a_removed_switch_keeps_the_map_until_reset_held(manager, fabric, caplog):
    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h2")),
    )
    _lose_leaf(manager, fabric)
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    # The operator removes the leaf, which may be hung, still forwarding.
    manager.confirm_fabric()
    manager.reconcile()
    manager.reconcile()
    assert _applied(manager).check_ok
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.blocked
    assert held.tainted_pkeys() == {100}
    assert manager.reset_held() == ([100], ["h2"])
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert not held.blocked
    assert held.tainted_pkeys() == set()


def test_a_takeover_inherits_the_fresh_sweep(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.delete_network(NETWORK_ID)
    manager.reconcile()
    # A new manager, with the leaf back: its start's sweep isn't fresh.
    fabric.switches = {LEAF_A, LEAF_B}
    manager._missing_fabric = False
    with manager.sessionmaker() as session:
        assert session.get(db.Applied, 1).missing
    manager._contested_fabric = False
    with manager._lock, manager.sessionmaker() as session:
        manager._missing_fabric = bool(session.get(db.Applied, 1).missing)
    fabric.start = lambda: SweepReport()
    manager._start_opensm()
    manager.check_fabric()
    manager.evaluate_jobs()
    assert _status(manager, job) == PENDING
    manager.reconcile()
    assert _status(manager, job) == COMPLETED


def test_status_and_the_alarm_name_what_the_missing_switches_hold(
    manager, fabric, caplog
):
    from alternative_fabric_manager import manager as manager_mod

    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h2")),
    )
    _lose_leaf(manager, fabric)
    job = manager.delete_port(OTHER_PORT)
    manager.reconcile()
    with manager.sessionmaker() as session:
        lines = manager_mod.fabric_status(session)
    text = "\n".join(lines)
    assert "fabric check: partial" in text
    assert f"0x{LEAF_B:016x}" in text
    assert f"delete_port {OTHER_PORT}" in text
    assert "tainted pkeys: 0x0064" in text
    assert "tainted hosts: h2" in text
    assert any("tainted hosts: h2" in record.getMessage() for record in caplog.records)
    assert _status(manager, job) == PENDING


def test_judge_grant_rules():
    effect = {
        "kind": "port",
        "port": "p",
        "pkey": 100,
        "guids": ["0000000000000001"],
        "grant": True,
        "host": "h1",
        "hosts": ["h1"],
        "vnic_type": "normal",
    }
    common = dict(
        placed={"0000000000000001"},
        pkey_errors=set(),
        tainted_pkeys=set(),
        tainted_hosts=set(),
        open_revocations=[],
    )
    assert partial.judge_grant(effect, **common) is None
    assert partial.judge_grant(dict(effect, grant=False), **common)
    assert partial.judge_grant(effect, **dict(common, placed=set()))
    assert partial.judge_grant(effect, **dict(common, tainted_pkeys={100}))
    assert partial.judge_grant(effect, **dict(common, tainted_hosts={"h1"}))
    vf = dict(effect, vnic_type="direct")
    assert partial.judge_grant(vf, **dict(common, tainted_hosts={"h1"})) is None
    # The live port's type counts too.
    assert partial.judge_grant(
        vf, **dict(common, tainted_hosts={"h1"}), vnic_type="normal"
    )
    # A revocation of another port on its host, or one whose hosts weren't
    # recorded, holds it; its own port's doesn't.
    assert partial.judge_grant(effect, **dict(common, open_revocations=[("q", {"h1"})]))
    assert partial.judge_grant(effect, **dict(common, open_revocations=[(None, None)]))
    assert (
        partial.judge_grant(effect, **dict(common, open_revocations=[("p", {"h1"})]))
        is None
    )
    assert (
        partial.judge_grant(effect, **dict(common, open_revocations=[("q", {"h2"})]))
        is None
    )
    # A job from before effects said whether it's a grant
    old = {k: v for k, v in effect.items() if k != "grant"}
    assert partial.judge_grant(old, **common)


def test_taint_follows_the_pairs():
    held = partial.Held()
    held.add({100: {("01", "full", "h1"), ("02", "full", "h2")}})
    # A live migration keeps the (GUID, membership) pair: the pkey isn't
    # tainted, the host it left is.
    held.add({100: {("01", "full", "h3"), ("02", "full", "h2")}})
    assert held.tainted_pkeys() == set()
    assert held.tainted_hosts() == {"h1"}
    # Full to limited taints the pkey.
    held.add({100: {("01", "limited", "h3"), ("02", "full", "h2")}})
    assert held.tainted_pkeys() == {100}
    # A GUID with a pkey error keeps its entries at a reset.
    held.add({100: {("02", "full", "h2")}})
    assert held.reset({"01"}) == set()
    assert held.tainted_pkeys() == {100}
    assert held.reset() == {100}


def test_scan_place_is_what_the_manager_compares():
    # The places are JSON round-trippable, as the sweep records them.
    placed = scan.place({1: ["port", "00000000000000aa", 3]}, [])
    assert json.loads(json.dumps(placed[1])) == placed[1]


# Round 2: virtual functions placed by their hosts, the complete map, the
# prober, the re-sweep, the reset's body, and network updates

VF2 = 0xFA163E000062A358
VF2_CLIENT_ID = "20:fa:16:3e:00:00:62:a3:58"
VF_PORT_2 = "b13bba05-eb07-45ba-ace2-765706b2d701"


def _host_ports(manager):
    with manager.sessionmaker() as session:
        return {row.guid: row.host for row in session.scalars(sa.select(db.HostPort))}


def _vf(port_id, client_id, host="hv1", **kwargs):
    return _port(port_id, client_id, host=host, vnic_type="direct", **kwargs)


def _learn_hv1(manager, fabric):
    """A VM on hv1 runs, so the dump lists its VF on OWN, and a passing
    check learns OWN for hv1; then the VM goes"""
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    _settled(manager, manager.put_port(THIRD_PORT, _vf(THIRD_PORT, VF_CLIENT_ID)))
    manager.check_fabric()
    assert _host_ports(manager) == {f"{OWN:016x}": "hv1"}
    fabric.vports = []
    _settled(manager, manager.delete_port(THIRD_PORT))
    manager.reconcile()


def test_a_vf_without_a_vport_is_placed_by_its_hosts_physical_ports(manager, fabric):
    _learn_hv1(manager, fabric)
    _lose_leaf(manager, fabric)
    # Nova keeps the guest paused until the job completes: no vport yet.
    job = manager.put_port(VF_PORT_2, _vf(VF_PORT_2, VF2_CLIENT_ID))
    manager.reconcile()
    assert _status(manager, job) == COMPLETED
    placed = json.loads(_applied(manager).placements)["guids"]
    assert placed[f"{VF2:016x}"][:2] == ["host", "hv1"]


def test_a_vf_on_a_host_with_no_learnt_port_waits(manager, fabric):
    _learn_hv1(manager, fabric)
    _lose_leaf(manager, fabric)
    job = manager.put_port(VF_PORT_2, _vf(VF_PORT_2, VF2_CLIENT_ID, host="hv2"))
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == PENDING


def test_a_vf_whose_hosts_port_is_behind_the_missing_leaf_waits(manager, fabric):
    _learn_hv1(manager, fabric)
    fabric.ports[OWN] = (LEAF_B, 1)
    _lose_leaf(manager, fabric)
    job = manager.put_port(VF_PORT_2, _vf(VF_PORT_2, VF2_CLIENT_ID))
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == PENDING


def _settle(manager, times=3):
    for _ in range(times):
        manager.reconcile()


def test_host_ports_are_learnt_only_at_a_passing_check(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    manager.put_port(THIRD_PORT, _vf(THIRD_PORT, VF_CLIENT_ID))
    _settle(manager)
    assert _host_ports(manager) == {}
    fabric.switches = {LEAF_A, LEAF_B}
    _settle(manager)
    assert _host_ports(manager) == {f"{OWN:016x}": "hv1"}
    # A port learnt for another host leaves this one's.
    manager.put_port(THIRD_PORT, _vf(THIRD_PORT, VF_CLIENT_ID, host="hv2"))
    _settle(manager)
    assert _host_ports(manager) == {f"{OWN:016x}": "hv2"}
    # Only a direct port's vport teaches anything.
    manager.put_port(
        THIRD_PORT, _port(THIRD_PORT, VF_CLIENT_ID, host="hv3", vnic_type="normal")
    )
    _settle(manager)
    assert _host_ports(manager) == {f"{OWN:016x}": "hv2"}
    from alternative_fabric_manager import manager as manager_mod

    with manager.sessionmaker() as session:
        lines = manager_mod.fabric_status(session)
    assert f"host hv2: physical ports 0x{OWN:016x}" in lines


def test_learning_takes_only_a_settled_unambiguous_vport(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    # With its job open, as while it migrates, nothing is learnt.
    job = manager.put_port(THIRD_PORT, _vf(THIRD_PORT, VF_CLIENT_ID))
    manager.check_fabric()
    assert _host_ports(manager) == {}
    _settled(manager, job)
    # Listed twice, nothing is learnt.
    fabric.vports = [(VF, OWN, 7, "ACTIVE"), (VF, GUID, 3, "ACTIVE")]
    manager.check_fabric()
    assert _host_ports(manager) == {}
    # On a physical port that is a recorded port's GUID, as a bare metal
    # port's, nothing is learnt.
    _settled(manager, manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID)))
    fabric.vports = [(VF, GUID, 3, "ACTIVE")]
    manager.check_fabric()
    assert _host_ports(manager) == {}
    fabric.vports = [(VF, OWN, 7, "ACTIVE")]
    manager.check_fabric()
    assert _host_ports(manager) == {f"{OWN:016x}": "hv1"}


def test_a_node_rows_port_is_learnt(manager, fabric):
    # On an all-in-one or hyperconverged host, OpenSM's node is also the
    # hypervisor, so its physical port carries instances' VFs.
    with manager.sessionmaker.begin() as session:
        session.add(db.Node(name="hv1", port_guids=f'["{OWN:016x}"]'))
    _learn_hv1(manager, fabric)
    assert _host_ports(manager) == {f"{OWN:016x}": "hv1"}


def test_a_learnt_port_the_passing_scan_doesnt_find_is_dropped(manager, fabric):
    _learn_hv1(manager, fabric)
    del fabric.ports[OWN]
    manager._checks_since_scan = manager.config.confirm_checks
    manager.check_fabric()
    assert _host_ports(manager) == {}


def test_an_unattributed_adapter_behind_the_missing_leaf_holds_vf_grants(
    manager, fabric
):
    _learn_hv1(manager, fabric)
    # Another adapter port behind leaf B, which nobody is known to own: it
    # may be hv1's other port, which a VF could land on.
    fabric.ports[0xC0FFEE] = (LEAF_B, 7)
    manager._checks_since_scan = manager.config.confirm_checks
    assert manager.check_fabric()
    _lose_leaf(manager, fabric)
    job = manager.put_port(VF_PORT_2, _vf(VF_PORT_2, VF2_CLIENT_ID))
    _settle(manager)
    assert _status(manager, job) == PENDING
    # Once it is known to be another host's, the grant completes.
    with manager.sessionmaker() as session:
        session.add(db.HostPort(guid=f"{0xC0FFEE:016x}", host="hv7"))
        session.commit()
    _settle(manager)
    assert _status(manager, job) == COMPLETED


def test_a_vf_the_dump_lists_but_doesnt_place_waits(manager, fabric):
    _learn_hv1(manager, fabric)
    # Attached to a running guest, or migrated live: listed, not ACTIVE
    fabric.vports = [(VF2, OWN, 8, "DOWN")]
    _lose_leaf(manager, fabric)
    job = manager.put_port(VF_PORT_2, _vf(VF_PORT_2, VF2_CLIENT_ID))
    _settle(manager)
    assert _status(manager, job) == PENDING


def test_a_term_between_leaves_the_map_incomplete(manager, fabric):
    manager.term = 3
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    manager.reconcile()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.complete
    # Term 4 led, and may have written a body it never recorded; term 5
    # writes next.
    manager.term = 5
    manager._written = None
    with manager.sessionmaker() as session:
        session.get(db.Applied, 1).term = 5
        session.commit()
    manager.sync()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert not held.complete
    # The next term after its own keeps it.
    manager.check_fabric()
    manager.term = 6
    manager._written = None
    with manager.sessionmaker() as session:
        session.get(db.Applied, 1).term = 6
        session.commit()
    manager.sync()
    manager.check_fabric()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.complete


@pytest.mark.parametrize("how", ["upgraded", "swept by another version"])
def test_a_map_that_may_lack_memberships_completes_no_port_grant(manager, fabric, how):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    fabric.ports[GUID] = (LEAF_B, 2)
    _settled(manager, manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID)))
    _lose_leaf(manager, fabric)
    # Deleted while leaf B is missing: GUID may still hold pkey 100.
    manager.delete_port(PORT_ID)
    if how == "upgraded":
        # As 0.6.0 left it: it swept the deletion, and never wrote the map.
        manager.reconcile()
        with manager.sessionmaker() as session:
            session.get(db.Applied, 1).held = None
            session.commit()
    else:
        # Another version's leader sweeps the deletion, unseen by the map.
        content, generation, attribution = manager._render()
        with manager.sessionmaker() as session:
            applied = session.get(db.Applied, 1)
            applied.body = content
            applied.generation = generation
            applied.attribution = json.dumps(attribution)
            session.commit()
        manager._written = None
    job = manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h9"))
    network = manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200))
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == PENDING
    # A network's creation grants nothing, so still completes.
    assert _status(manager, network) == COMPLETED
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert not held.complete


def test_a_grant_placed_only_after_its_sweep_is_swept_again(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    del fabric.ports[GUID]
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))
    fabric.ports[GUID] = (LEAF_A, 2)
    starts = fabric.starts
    for _ in range(5):
        manager.reconcile()
    assert _status(manager, job) == COMPLETED
    # Its own sweep, and one more once it was placed: bounded
    assert fabric.starts - starts == 2


def test_a_grant_whose_place_keeps_still_is_swept_again_once(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.put_port(PORT_ID, _port(PORT_ID, conftest.CLIENT_ID))

    def sweep_of_its_own():
        fabric.starts += 1

    # OpenSM sweeps by itself during every scan: never placed.
    fabric.during_scan = sweep_of_its_own
    starts = fabric.calls
    for _ in range(5):
        manager.reconcile()
    assert _status(manager, job) == PENDING
    assert fabric.calls - starts <= 2


def test_a_reset_never_uses_a_body_opensm_didnt_sweep(manager, fabric, monkeypatch):
    from alternative_fabric_manager import manager as manager_mod
    from alternative_fabric_manager import port_keeper

    _settled(
        manager,
        manager.put_network(NETWORK_ID, conftest.network()),
        manager.put_port(OTHER_PORT, _port(OTHER_PORT, OTHER_CLIENT_ID, host="h2")),
    )
    manager.delete_port(OTHER_PORT)
    written = manager_mod.partitions.write_atomically

    def fails(path, content, *args):
        if path == manager.config.partitions_file:
            raise OSError("disk full")
        return written(path, content, *args)

    monkeypatch.setattr(manager_mod.partitions, "write_atomically", fails)
    manager.sync()
    # The map has the deletion's body, which OpenSM never read.
    assert manager.check_fabric()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.tainted_pkeys() == {100}
    with pytest.raises(port_keeper.PortKeysError):
        manager.reset_held()
    monkeypatch.setattr(manager_mod.partitions, "write_atomically", written)
    manager.reconcile()
    manager.reconcile()
    with manager.sessionmaker() as session:
        held = partial.Held.loads(session.get(db.Applied, 1).held)
    assert held.tainted_pkeys() == set()


def test_a_network_update_that_keeps_its_pkey_is_a_grant(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    _lose_leaf(manager, fabric)
    job = manager.put_network(NETWORK_ID, conftest.network(name="renamed"))
    manager.reconcile()
    assert _status(manager, job) == COMPLETED


def test_ibnetdiscover_places_nothing_on_a_switch_the_check_didnt_find(manager, fabric):
    _settled(manager, manager.put_network(NETWORK_ID, conftest.network()))
    stranger = 0xEC0D9A0300B12590
    fabric.ports[GUID] = (stranger, 2)
    fabric.switches = {LEAF_A}
    original = fabric.scan

    def listed_anyway(m_key=None):
        found = original(m_key)
        found[GUID] = ["port", f"{stranger:016x}", 2]
        return found

    fabric.scan = listed_anyway
    manager.check_fabric()
    assert f"{GUID:016x}" not in manager._last_scan["placed"]


# Prober mode


def test_prober_mode_has_partial_checks_and_completes_grants(world):  # noqa: F811
    turned_on(world)
    manager = world.manager
    # S2, and T3 behind it, are cut off: T3 was never sent a key.
    world.sim.down |= {(S1, 2), (S2, 1)}
    assert not world.check()
    applied = world.applied()
    assert applied.check_partial, applied.check_detail
    manager.put_network(NETWORK_ID, conftest.network())
    job = manager.put_port(PORT_ID, conftest.port())
    world.full_verify_due()
    manager.reconcile()
    assert _status(manager, job) == COMPLETED
    # A port that refuses its key still fails the check.
    world.sim.ports[HV].key = 0x3333333333333333
    assert not world.check()
    assert not world.applied().check_partial
