"""Per-port M_Keys in the single-node manager, against a simulated fabric

The fake managed driver keeps OpenSM's cache volume in memory and simulates
OpenSM as hardware test 5 saw it (tests/fabric_sim.py, Sim.sweep): the
scenarios run the manager's own start, checks and jobs over it, so that
what the fabric ends up holding, and every key any sender was sent, can be
asserted.
"""

import dataclasses
import datetime
import logging

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, opensm_conf, port_keeper, port_keys, prober
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import Fabric, ReloadError, SweepReport
from alternative_fabric_manager.port_keys import DERIVED, M_KEY, UNKNOWN
from tests import conftest
from tests.fabric_sim import BASE, OWN, S1, S2, M, Sim

TN = conftest.GUID  # a bare metal tenant's port, a Neutron port's GUID
HV = 0xC1  # a hypervisor's port: no Neutron port has its GUID
T3 = 0xB3  # another host's port, behind S2
T4 = 0xB4  # behind a link that goes down
X = 0xD1  # an adapter that answers as a switch, never confirmed
Z = 0xE1  # a port OpenSM hasn't recorded
VF = 0x12345600007821B7  # a virtual function's GUID: never a row
NETWORK = conftest.NETWORK_ID
OTHER_NETWORK = "d13bba05-eb07-45ba-ace2-765706b2d701"
VF_PORT = "f13bba05-eb07-45ba-ace2-765706b2d701"
STRANGER = 0x3333333333333333


def derived(guid):
    return port_keys.port_m_key(BASE, guid)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class Crash(BaseException):
    """The manager's process dying, and OpenSM with it"""


class FakeOpenSM:
    """The managed driver over the simulator: OpenSM's cache volume in
    memory, OpenSM's sweeps simulated, and the prober run against the
    simulator"""

    def __init__(self, sim, config):
        self.sim = sim
        self.config = config
        self.volume = {}
        self.is_running = False
        self.starts = []
        # How many times OpenSM itself ran, once prepare let it
        self.runs = 0
        self.stops = 0
        self.reads = 0
        self.writes = []
        self.probes = []
        self.calls = 0
        self.fabric_calls = []
        self.read_error = None
        self.probe_error = None
        self.report = None
        # Called with the driver once prepare has run, before OpenSM sweeps
        self.on_start = None
        # Called with the driver after OpenSM's first sweep
        self.after_sweep = None
        # OpenSM logs SUBNET UP, but keys no port, as test 5's own-port-only
        # OpenSM did
        self.stall = False
        # Raised once, after OpenSM's first sweep, as a start that never
        # logs SUBNET UP
        self.start_error = None

    # OpenSM

    def conf(self):
        options = {}
        with open(self.config.opensm_conf_file) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and not line.startswith("#"):
                    options[parts[0]] = parts[1]
        return options

    def per_port(self):
        return self.conf().get("m_key_per_port") == "TRUE"

    def sweep(self):
        """OpenSM's heavy sweep, from its cache and options"""
        options = self.conf()
        per_port = options.get("m_key_per_port") == "TRUE"
        m_line = int(options["m_key"], 16)
        neighbors = port_keys.parse_neighbors(self.volume.get("neighbors", ("",))[0])
        entries = dict(
            port_keys.parse_guid2mkey(self.volume.get("guid2mkey", ("",))[0])
        )
        if self.stall:
            return [self.sim.own]
        reached = self.sim.sweep(per_port, m_line, neighbors, entries)
        if reached is None:
            return None
        for guid in reached:
            entries[guid] = self.sim.ports[guid].key
        # Lines about a reached port that its cabling no longer shows go:
        # OpenSM rewrites what it discovered.
        links = {
            link.key: link
            for link in neighbors
            if link.guid not in reached
            or self.sim.cables.get(link.key) == (link.peer_guid, link.peer_port)
        }
        links.update((link.key, link) for link in self.sim.neighbors_of(set(reached)))
        now = _now()
        self.volume["neighbors"] = (port_keys.render_neighbors(links), now)
        self.volume["guid2mkey"] = (port_keys.render_guid2mkey(entries), now)
        return reached

    def start(self, prepare=None):
        self.starts.append(prepare)
        try:
            if prepare is not None:
                prepare("created")
            if self.on_start is not None:
                self.on_start(self)
            self.runs += 1
            if self.sweep() is None:
                raise ReloadError("OpenSM didn't start within 1 seconds")
            if self.after_sweep is not None:
                self.after_sweep(self)
            if self.start_error is not None:
                error, self.start_error = self.start_error, None
                raise error
        except Crash:
            self.is_running = False
            raise
        except Exception:
            self.stop()
            raise
        self.is_running = True
        return self.report or SweepReport()

    def running(self):
        return self.is_running

    def stop(self):
        self.stops += 1
        self.is_running = False

    def reload(self):
        self.calls += 1
        return self.report or SweepReport()

    def fabric(self, m_key=None, sa_key=None, switches=None):
        self.fabric_calls.append((m_key, sa_key, switches))
        if not self.is_running:
            raise ReloadError("OpenSM container opensm isn't running")
        if switches is None:
            key = int(m_key, 16)
            if not self.sim.accepts(self.sim.own, key):
                raise ReloadError("ibswitches failed: bad M_Key at this host's port")
            switches = {
                port.node_guid
                for port in self.sim.ports.values()
                if port.switch and self.sim.accepts(port.guid, key)
            }
        return Fabric(switches, [(self.sim.own, 3)], {self.sim.own})

    # The cache and the prober

    def read_cache(self, container=None):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        if container is None and not self.is_running:
            raise ReloadError("Docker: can't read OpenSM's cache: not running")
        return dict(self.volume)

    def write_cache(self, container, files):
        self.writes.append(dict(files))
        now = _now()
        for name, text in files.items():
            self.volume[name] = (text, now)

    def prober(self, helper=False):
        self.probes.append(helper)

        def run(gets):
            if self.probe_error is not None:
                raise self.probe_error
            if not helper and not self.is_running:
                raise ReloadError("OpenSM container opensm isn't running")
            return self.sim.run(gets)

        return run


def topology():
    """This host's port and the bare metal tenant on S1, S2 behind S1, a
    hypervisor and T4 on S1, T3 on S2: every port on the stored M_Key"""
    sim = Sim(m_key=M, base=BASE)
    sim.add(OWN, key=M)
    sim.add(S1, switch=True, ports=8, key=M)
    sim.add(S2, switch=True, ports=4, key=M)
    for guid in (TN, HV, T3, T4):
        sim.add(guid, key=M)
    sim.cable(OWN, 1, S1, 1)
    sim.cable(S1, 2, S2, 1)
    sim.cable(S1, 3, TN, 1)
    sim.cable(S1, 4, HV, 1)
    sim.cable(S2, 2, T3, 1)
    sim.cable(S1, 5, T4, 1)
    return sim


def _key_texts(sim):
    """Every key's text, in the forms a log line might use"""
    keys = {M, BASE, STRANGER} | {derived(guid) for guid in sim.ports}
    keys |= {derived(guid) for guid in (Z, X, VF)}
    texts = set()
    for key in keys:
        texts |= {f"{key:016x}", f"{key:x}", f"{key:016X}", str(key)}
    return texts


@dataclasses.dataclass
class World:
    config: object
    sessionmaker: object
    sim: Sim
    driver: FakeOpenSM
    manager: manager_mod.Manager

    def new_manager(self):
        self.manager = manager_mod.Manager(self.config, self.sessionmaker, self.driver)
        return self.manager

    def restart(self):
        """Stop the manager, and OpenSM with it, and start a new one"""
        self.manager.stop()
        self.new_manager().start()
        return self.manager

    def keeper(self):
        return self.manager.port_keeper

    def rows(self):
        with self.sessionmaker() as session:
            return port_keys.load(session)[0]

    def state(self):
        with self.sessionmaker() as session:
            return {
                "target": port_keys.load_target(session),
                "running": port_keys.load_running(session),
                "confirmed": port_keys.load_confirmed(session),
                "fingerprint": port_keys.load_fingerprint(session),
            }

    def full_verify_due(self):
        self.keeper()._last_full = None

    def check(self, due=True):
        if due:
            self.full_verify_due()
        return self.manager.check_fabric()

    def applied(self):
        with self.sessionmaker() as session:
            return session.get(db.Applied, 1)


@pytest.fixture
def world(config, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(manager_mod, "RETRY_DELAY", 0)
    monkeypatch.setattr(port_keeper, "CONFIRM_POLL", 0.01)
    # The applier's loop isn't run: the tests drive reconcile themselves.
    monkeypatch.setattr(manager_mod.Manager, "_run", lambda self: None)
    config.driver = "managed"
    config.hardening = "all, m_key_per_port"
    config.start_timeout = 1.0
    sessionmaker = db.make_sessionmaker(config.database_url)
    with sessionmaker() as session:
        session.add(db.Secret(name="m_key", value=port_keys.format_key(M)))
        session.add(db.Secret(name="m_key_base", value=port_keys.format_key(BASE)))
        session.commit()
    sim = topology()
    driver = FakeOpenSM(sim, config)
    manager = manager_mod.Manager(config, sessionmaker, driver)
    world = World(config, sessionmaker, sim, driver, manager)
    yield world
    # No log line, nor any exception logged, holds a key.
    texts = _key_texts(sim)
    for record in caplog.records:
        shown = record.getMessage() + (record.exc_text or "")
        if record.exc_info:
            shown += str(record.exc_info[1])
        assert not any(text in shown for text in texts), record.getMessage()


def assert_no_key(error, sim):
    shown = str(error)
    assert not any(text in shown for text in _key_texts(sim)), shown


def confirm(world):
    """--confirm-fabric, beside the running manager"""
    return world.manager.confirm_fabric()


def arm(world):
    """Record the target on, as a --confirm-fabric before what the test
    changes would have"""
    with world.sessionmaker() as session:
        port_keys.save_target(session, True)
        session.commit()


def promote(world):
    """Confirm the pending switches, as a second --confirm-fabric would,
    without arming the turn-on"""
    with world.sessionmaker() as session:
        session.execute(sa.update(db.ExpectedSwitch).values(pending=False))
        session.commit()


def expected(world, pending=False):
    with world.sessionmaker() as session:
        return {
            int(row.guid, 16)
            for row in session.scalars(
                sa.select(db.ExpectedSwitch).where(
                    db.ExpectedSwitch.pending.is_(pending)
                )
            )
        }


def running_with_rows(world):
    """The group named, OpenSM started, its cache folded at a check, and the
    switches confirmed: the first confirmation, which leaves every switch
    pending and so doesn't arm the turn-on, then the switches promoted"""
    world.manager.start()
    world.manager.reconcile()
    first = confirm(world)
    assert first.switches == [S1, S2] and first.pending == [S1, S2]
    assert not first.armed and world.state()["target"] is False
    promote(world)
    world.manager.reconcile()
    return world


def turned_on(world):
    running_with_rows(world)
    assert confirm(world).armed
    world.restart()
    world.manager.reconcile()
    return world


def tenants_given_m_key(sim, since, tenants):
    """Sends of m_key, after since, to tenant ports that didn't hold it"""
    return [
        (sender, guid)
        for sender, guid, key, held in sim.history[since:]
        if guid in tenants and key == M and held != M
    ]


# Inert


def test_inert_without_the_group_or_rows(world):
    world.config.hardening = "all"
    world.manager.start()
    world.manager.put_network(NETWORK, conftest.network())
    job = world.manager.put_port(conftest.PORT_ID, conftest.port())
    world.manager.reconcile()
    confirmation = confirm(world)
    assert confirmation.pending == [] and confirmation.note is None
    assert not confirmation.problems
    world.manager.reconcile()
    assert world.manager.get_job(job).status == db.JobStatus.COMPLETED
    # Nothing read, written or probed; OpenSM started as before
    assert world.driver.reads == 0 and world.driver.writes == []
    assert world.driver.probes == [] and world.sim.sent == []
    assert world.driver.starts == [None]
    assert all(call[2] is None for call in world.driver.fabric_calls)
    assert world.rows() == {}
    # opensm.conf is byte-identical to the render before per-port keys
    keys = world.manager.keys()
    expected = opensm_conf.render(
        {}, hardening=world.config.hardening_groups, keys=keys
    )
    with open(world.config.opensm_conf_file) as f:
        assert f.read() == expected
    with world.sessionmaker() as session:
        assert session.get(db.State, port_keys.TARGET) is None
        assert session.get(db.State, port_keys.RUNNING) is None


def test_inert_in_ha_mode(world):
    world.config.ha = True
    keeper = world.keeper()
    assert not keeper.active()
    assert keeper.check() is None
    with pytest.raises(port_keeper.PortKeysError, match="HA"):
        keeper.states()


def test_rows_with_another_driver_alarm_and_stay_inert(world, caplog):
    running_with_rows(world)
    world.config.driver = "docker"
    world.config.hardening = "all"
    keeper = world.keeper()
    reads = world.driver.reads
    assert not keeper.active() and keeper.check() is None
    assert not keeper.active()
    assert world.driver.reads == reads
    alarms = [r for r in caplog.records if "driver can't keep them" in r.getMessage()]
    assert len(alarms) == 1


def test_naming_the_group_alone_changes_nothing_on_ports(world):
    running_with_rows(world)
    assert {port.key for port in world.sim.ports.values()} == {M}
    options = world.driver.conf()
    assert "m_key_per_port" not in options and options["m_key"] == f"0x{M:016x}"
    rows = world.rows()
    assert set(rows) == {OWN, S1, S2, TN, HV, T3, T4}
    assert {row.holds for row in rows.values()} == {M_KEY}
    assert world.state()["target"] is False
    assert world.applied().check_ok


# Start-time refusals


@pytest.mark.parametrize(
    "change, match",
    [
        ({"hardening": "all", "opensm_conf": {"m_key_lookup": "FALSE"}}, "lookup"),
        ({"hardening": "all, -m_key"}, "m_key group"),
    ],
)
def test_a_start_is_refused_while_a_row_holds_a_derived_key(world, change, match):
    turned_on(world)
    world.manager.stop()
    for name, value in change.items():
        setattr(world.config, name, value)
    starts = len(world.driver.starts)
    with pytest.raises(config_mod.ConfigError, match=match):
        world.new_manager().start()
    assert len(world.driver.starts) == starts


# Turning it on


def test_confirm_fabric_without_the_group_only_confirms(world):
    # Rows exist, as during a turn-off: the prober confirms, nothing arms.
    running_with_rows(world)
    world.config.hardening = "all"
    confirmation = confirm(world)
    assert confirmation.switches == [S1, S2]
    assert not confirmation.armed and not confirmation.problems
    assert confirmation.note is None
    assert world.driver.fabric_calls[-1][2] == {S1, S2}
    assert world.state()["target"] is False


def _refused(world, match):
    """A --confirm-fabric that confirms the switches but doesn't arm"""
    confirmation = confirm(world)
    assert confirmation.switches == [S1, S2]
    assert not confirmation.armed
    assert any(match in problem for problem in confirmation.problems), (
        confirmation.problems
    )
    with world.sessionmaker() as session:
        expected = {
            int(row.guid, 16) for row in session.scalars(sa.select(db.ExpectedSwitch))
        }
    assert expected == {S1, S2}
    assert world.state()["target"] is False
    return confirmation


def test_confirm_fabric_doesnt_arm_if_this_start_stored_the_base(world):
    running_with_rows(world)
    with world.sessionmaker() as session:
        session.get(db.State, manager_mod.BASE_KEPT).value = 0
        session.commit()
    _refused(world, "backing up the database")


def test_confirm_fabric_doesnt_arm_without_links(world):
    world.manager.start()
    # OpenSM's cache lists nothing yet: no rows, so ibswitches confirms.
    world.driver.volume = {}
    confirmation = confirm(world)
    assert confirmation.switches == [S1 + 0x1000, S2 + 0x1000]
    assert any("no links" in problem for problem in confirmation.problems)
    assert world.state()["target"] is False


def test_confirm_fabric_lists_the_ports_that_dont_hold_m_key(world):
    running_with_rows(world)
    world.sim.ports[HV].key = derived(HV)
    world.sim.ports[T3].protect = 0
    confirmation = _refused(world, "must answer the stored M_Key")
    message = "\n".join(confirmation.problems)
    assert f"0x{HV:016x}" in message and f"0x{T3:016x}" in message
    assert f"0x{TN:016x}" not in message
    assert_no_key(message, world.sim)
    # The verify writes no rows.
    assert world.rows()[HV].holds == M_KEY


def test_confirm_fabric_doesnt_arm_for_an_unknown_row_behind_an_up_link(world):
    running_with_rows(world)
    world.keeper().add_link((S1, 6), (Z, 1))
    world.sim.add(Z, key=STRANGER)
    world.sim.cable(S1, 6, Z, 1)
    _refused(world, f"0x{Z:016x}")


def test_confirm_fabric_arms_with_an_unknown_row_behind_a_down_link(world):
    running_with_rows(world)
    world.keeper().add_link((S1, 6), (Z, 1))
    world.sim.add(Z, key=STRANGER)
    world.sim.cable(S1, 6, Z, 1, up=False)
    confirmation = confirm(world)
    assert confirmation.armed and "next restart" in confirmation.note


def test_confirm_fabric_doesnt_arm_when_the_verify_runs_out_of_time(world, monkeypatch):
    running_with_rows(world)
    keeper = world.keeper()
    real = keeper._verify

    def verify(*args, follow_unconfirmed=False, **kwargs):
        if not follow_unconfirmed:
            kwargs["bound"] = 1e-9
        return real(*args, follow_unconfirmed=follow_unconfirmed, **kwargs)

    monkeypatch.setattr(keeper, "_verify", verify)
    _refused(world, "ran out of time")


def test_confirm_fabric_fails_when_this_hosts_port_is_lost(world):
    running_with_rows(world)
    world.sim.ports[OWN].key = STRANGER
    with pytest.raises(ReloadError, match="this host's port"):
        confirm(world)
    assert world.state()["target"] is False


def test_confirm_fabric_with_the_target_on_only_confirms(world):
    turned_on(world)
    before = world.state()
    confirmation = confirm(world)
    assert confirmation.switches == [S1, S2] and not confirmation.problems
    assert "already on" in confirmation.note
    assert world.state() == before


def test_confirm_fabric_reports_a_cache_it_cant_read_but_confirms(world):
    running_with_rows(world)
    world.driver.read_error = ReloadError("Docker: can't read OpenSM's cache")
    _refused(world, "can't read the running OpenSM's cache")


def test_a_full_turn_on(world):
    running_with_rows(world)
    seen = {}

    def on_start(driver):
        if driver.per_port():
            seen.setdefault("fingerprint", world.state()["fingerprint"])
            seen.setdefault("history", len(world.sim.history))

    world.driver.on_start = on_start
    assert confirm(world).armed
    assert world.state()["target"] is True
    world.restart()
    # The fingerprint was recorded before OpenSM was started with the option.
    assert seen["fingerprint"] == port_keys.fingerprint(BASE)
    options = world.driver.conf()
    assert options["m_key_per_port"] == "TRUE"
    assert options["m_key"] == f"0x{BASE:016x}"
    # The start rendered an m_key entry for every port that held it, and
    # every reachable port now holds its derived key.
    entries = port_keys.parse_guid2mkey(world.driver.writes[-1]["guid2mkey"])
    assert set(entries) == {OWN, S1, S2, TN, HV, T3, T4}
    for guid in (OWN, S1, S2, TN, HV, T3, T4):
        assert world.sim.ports[guid].key == derived(guid)
    state = world.state()
    assert state["running"] and state["confirmed"]
    world.manager.reconcile()
    assert world.applied().check_ok, world.applied().check_detail
    assert {row.holds for row in world.rows().values()} == {DERIVED}
    # No tenant port that held its derived key was sent m_key after the
    # fingerprint was recorded.
    assert not tenants_given_m_key(world.sim, seen["history"], {TN, HV, T3, T4})
    # The fabric check counts the prober's switches.
    assert world.driver.fabric_calls[-1][2] == {S1, S2}


def test_a_port_down_at_turn_on_is_rekeyed_through_its_entry(world):
    running_with_rows(world)
    world.sim.down |= {(S1, 5), (T4, 1)}
    assert confirm(world).armed
    world.restart()
    world.manager.reconcile()
    assert world.sim.ports[T4].key == M
    assert world.rows()[T4].holds == M_KEY
    assert world.state()["confirmed"]
    assert world.applied().check_ok, world.applied().check_detail
    # It comes back, and OpenSM's next sweep reaches it through its entry.
    world.sim.down -= {(S1, 5), (T4, 1)}
    world.driver.sweep()
    assert world.sim.ports[T4].key == derived(T4)
    world.check()
    assert world.rows()[T4].holds == DERIVED


def test_a_turn_on_that_moves_nothing_is_set_back(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed
    world.driver.stall = True
    world.restart()
    state = world.state()
    assert not state["target"] and not state["running"] and not state["confirmed"]
    # The fingerprint stays: OpenSM ran with the option.
    assert state["fingerprint"] == port_keys.fingerprint(BASE)
    assert world.driver.is_running and not world.driver.per_port()
    assert {port.key for port in world.sim.ports.values()} == {M}
    assert any("set back to off" in r.getMessage() for r in caplog.records)


def test_a_part_way_turn_on_is_retried_on_the_target(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed

    def cut(driver):
        if driver.per_port():
            world.sim.down |= {(S1, 2), (S2, 1)}

    def mend(driver):
        world.sim.down -= {(S1, 2), (S2, 1)}

    world.driver.on_start = cut
    world.driver.after_sweep = mend
    world.manager.stop()
    world.new_manager()
    # The start fails, with OpenSM stopped and the target kept on.
    world.manager.start()
    assert not world.driver.is_running
    assert any("part-way" in r.getMessage() for r in caplog.records)
    state = world.state()
    assert state["target"] and state["running"] and not state["confirmed"]
    assert world.rows()[TN].holds == DERIVED and world.rows()[S2].holds == M_KEY
    # The applier's check starts it again, on the target, until it succeeds.
    world.driver.on_start = world.driver.after_sweep = None
    world.manager.reconcile()
    world.manager._check_opensm()
    assert world.driver.is_running and world.driver.per_port()
    assert world.state()["confirmed"]
    assert world.sim.ports[S2].key == derived(S2)


def test_a_prober_failure_after_a_failed_turn_on_starts_nothing(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed
    world.driver.stall = True

    def break_prober(driver):
        driver.probe_error = prober.ProbeError("the prober exited with status 3")

    world.driver.after_sweep = break_prober
    world.manager.stop()
    world.new_manager().start()
    assert not world.driver.is_running
    starts = len(world.driver.starts)
    # Retried without starting OpenSM on either target while it fails
    world.manager._check_opensm()
    assert len(world.driver.starts) == starts and not world.driver.is_running
    assert any("can't tell" in r.getMessage() for r in caplog.records)
    # Once it can tell, and nothing moved, the target is set back.
    world.driver.probe_error = None
    world.driver.after_sweep = None
    world.driver.stall = False
    world.manager._check_opensm()
    assert world.driver.is_running and not world.driver.per_port()
    assert world.state()["target"] is False


def test_a_crash_mid_transition_resumes_on_the_target(world):
    running_with_rows(world)
    assert confirm(world).armed

    def crash(driver):
        raise Crash()

    world.driver.after_sweep = crash
    world.manager.stop()
    with pytest.raises(Crash):
        world.new_manager().start()
    world.driver.after_sweep = None
    world.new_manager().start()
    assert world.state()["confirmed"]
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())
    # The fold took the volume's newer entries: no row is left on m_key.
    world.manager.reconcile()
    assert {row.holds for row in world.rows().values()} == {DERIVED}


def test_a_verify_failure_at_a_transition_start_uses_the_previous_target(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed
    world.driver.probe_error = prober.ProbeError("the prober exited with status 4")
    world.restart()
    assert world.driver.is_running and not world.driver.per_port()
    state = world.state()
    assert state["target"] and not state["running"]
    assert {port.key for port in world.sim.ports.values()} == {M}
    assert any("as it last ran" in r.getMessage() for r in caplog.records)
    world.driver.probe_error = None
    world.restart()
    assert world.driver.per_port() and world.state()["confirmed"]


def test_this_hosts_port_lost_fails_the_start(world, caplog):
    running_with_rows(world)
    world.sim.ports[OWN].key = STRANGER
    world.manager.stop()
    world.new_manager()
    with pytest.raises(ReloadError, match="neither the stored M_Key") as raised:
        world.manager._start_opensm()
    assert_no_key(raised.value, world.sim)
    assert not world.driver.is_running
    # A start of the manager retries it at every check rather than exiting.
    world.manager.start()
    assert not world.driver.is_running
    assert any("retrying at every check" in r.getMessage() for r in caplog.records)


# Turning it off


def test_a_turn_off_and_forgetting_the_rows(world):
    turned_on(world)
    with pytest.raises(port_keeper.PortKeysError, match="is named"):
        world.keeper().forget_port_keys()
    world.config.hardening = "all"
    history = len(world.sim.history)
    world.restart()
    state = world.state()
    assert not state["target"] and not state["running"]
    options = world.driver.conf()
    assert "m_key_per_port" not in options and options["m_key"] == f"0x{M:016x}"
    # OpenSM reached each port through its derived entry, never m_key.
    opensm = [
        (guid, key)
        for sender, guid, key, _ in world.sim.history[history:]
        if sender == "opensm"
    ]
    assert all(key == derived(guid) for guid, key in opensm)
    assert {port.key for port in world.sim.ports.values()} == {M}
    with pytest.raises(port_keeper.PortKeysError, match="derived"):
        world.keeper().forget_port_keys()
    world.manager.reconcile()
    assert {row.holds for row in world.rows().values()} == {M_KEY}
    assert world.keeper().forget_port_keys() == (7, 12)
    assert world.rows() == {}
    # The fingerprint and base stay; and with no rows and no group, the
    # manager is inert again.
    assert world.state()["fingerprint"] == port_keys.fingerprint(BASE)
    world.restart()
    reads = world.driver.reads
    world.manager.reconcile()
    assert world.driver.reads == reads and world.driver.starts[-1] is None


# Restores


def test_a_restore_with_foreign_entries_leaves_the_volume_alone(world, caplog):
    running_with_rows(world)
    world.manager.stop()
    world.sim.ports[HV].key = STRANGER
    entries = {guid: M for guid in world.sim.ports}
    entries[HV] = STRANGER
    world.driver.volume["guid2mkey"] = (port_keys.render_guid2mkey(entries), _now())
    volume = dict(world.driver.volume)
    with world.sessionmaker() as session:
        port_keys.save_target(session, True)
        session.commit()
    writes = len(world.driver.writes)
    seen = []
    world.driver.on_start = lambda driver: seen.append(dict(driver.volume))
    world.new_manager().start()
    assert len(world.driver.writes) == writes
    # OpenSM started on the volume's files as they were.
    assert seen == [volume]
    assert world.state()["target"] is False
    assert not world.driver.per_port()
    assert any("neither the stored M_Key" in r.getMessage() for r in caplog.records)
    # OpenSM reached HV through its own entry, and gave it m_key.
    assert world.sim.ports[HV].key == M


def test_a_fingerprint_mismatch_keeps_the_target_off(world, caplog):
    running_with_rows(world)
    world.manager.stop()
    with world.sessionmaker() as session:
        port_keys.save_fingerprint(session, port_keys.fingerprint(BASE + 1))
        port_keys.save_target(session, True)
        session.commit()
    writes = len(world.driver.writes)
    world.new_manager().start()
    assert len(world.driver.writes) == writes
    assert world.state()["target"] is False and not world.driver.per_port()
    assert any("fingerprint differs" in r.getMessage() for r in caplog.records)
    _refused(world, "fingerprint differs")
    # The way out: with the group no longer named and every port on the
    # stored M_Key, forgetting the rows drops the other base's fingerprint.
    world.manager.stop()
    world.config.hardening = "all"
    world.keeper().forget_port_keys()
    assert world.state()["fingerprint"] is None


def test_a_restored_database_adopts_the_target_on(world, caplog):
    # The ports and the volume hold derived keys; the database has the base
    # but no rows, and the target off.
    for port in world.sim.ports.values():
        port.key = derived(port.guid)
    neighbors = world.sim.neighbors_of(set(world.sim.ports))
    entries = {guid: derived(guid) for guid in world.sim.ports}
    world.driver.volume = {
        "neighbors": (port_keys.render_neighbors(neighbors), _now()),
        "guid2mkey": (port_keys.render_guid2mkey(entries), _now()),
    }
    world.manager.start()
    state = world.state()
    assert state["target"] and state["running"] and state["confirmed"]
    assert state["fingerprint"] == port_keys.fingerprint(BASE)
    assert world.driver.per_port()
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())
    assert any("adopting the target on" in r.getMessage() for r in caplog.records)
    # Not held to the turn-on's success rule: it read no running cache.
    assert world.driver.reads == 1
    # m_key was never sent to a tenant.
    assert not tenants_given_m_key(world.sim, 0, {TN, HV, T3, T4})


def test_without_the_group_a_restore_is_an_ordinary_turn_off(world):
    for port in world.sim.ports.values():
        port.key = derived(port.guid)
    neighbors = world.sim.neighbors_of(set(world.sim.ports))
    entries = {guid: derived(guid) for guid in world.sim.ports}
    world.driver.volume = {
        "neighbors": (port_keys.render_neighbors(neighbors), _now()),
        "guid2mkey": (port_keys.render_guid2mkey(entries), _now()),
    }
    world.config.hardening = "all"
    world.manager.start()
    assert world.state()["target"] is False
    assert {port.key for port in world.sim.ports.values()} == {M}


@pytest.mark.parametrize(
    "spoil",
    [
        lambda driver: setattr(
            driver, "read_error", ReloadError("Docker: can't read OpenSM's cache")
        ),
        lambda driver: driver.volume.update(guid2mkey=("not a cache\n", _now())),
    ],
)
def test_a_start_whose_cache_cant_be_read_fails_and_writes_nothing(
    world, caplog, spoil
):
    turned_on(world)
    world.manager.stop()
    spoil(world.driver)
    writes = len(world.driver.writes)
    world.new_manager()
    with pytest.raises(ReloadError, match="isn't replaced unseen"):
        world.manager._start_opensm()
    assert len(world.driver.writes) == writes and not world.driver.is_running
    # Retried at every check, until it can be read
    world.manager.start()
    assert not world.driver.is_running
    world.driver.read_error = None
    world.driver.volume.pop("guid2mkey", None)
    world.manager._check_opensm()
    assert world.driver.is_running and world.driver.per_port()


def test_a_lost_volume_is_rebuilt_from_the_rows(world):
    turned_on(world)
    world.manager.stop()
    world.driver.volume = {}
    world.new_manager().start()
    assert world.driver.is_running and world.driver.per_port()
    written = world.driver.writes[-1]
    assert port_keys.parse_neighbors(written["neighbors"])
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())


def test_a_checks_cache_read_failure_leaves_the_rows(world, caplog):
    turned_on(world)
    before = world.rows()
    world.driver.read_error = ReloadError("Docker: can't read OpenSM's cache")
    assert world.check(), world.applied().check_detail
    assert any("Can't read OpenSM's cache" in r.getMessage() for r in caplog.records)
    assert {g: r.holds for g, r in world.rows().items()} == {
        g: r.holds for g, r in before.items()
    }


# The fabric check


def test_a_switch_port_up_with_no_recorded_neighbour_fails_the_check(world):
    turned_on(world)
    world.sim.add(Z, key=derived(Z))
    world.sim.cable(S1, 6, Z, 1)
    assert not world.check()
    detail = world.applied().check_detail
    assert f"switch port 0x{S1:016x} port 6" in detail and "--add-link" in detail
    world.keeper().add_link((S1, 6), (Z, 1))
    assert world.rows()[Z].holds == UNKNOWN
    assert world.check(), world.applied().check_detail
    assert world.rows()[Z].holds == DERIVED


def test_a_lost_non_neutron_port_fails_the_check_until_released(world):
    turned_on(world)
    world.sim.ports[HV].key = STRANGER
    assert not world.check()
    assert f"0x{HV:016x}" in world.applied().check_detail
    world.keeper().release_port(HV)
    assert world.check(), world.applied().check_detail
    assert world.rows()[HV].released
    # It answers its key again, which clears the release.
    world.sim.ports[HV].key = derived(HV)
    assert world.check()
    assert not world.rows()[HV].released


def test_a_lost_neutron_port_doesnt_fail_the_check(world):
    turned_on(world)
    world.manager.put_network(NETWORK, conftest.network())
    world.manager.put_port(conftest.PORT_ID, conftest.port())
    world.sim.ports[TN].key = STRANGER
    assert world.check(), world.applied().check_detail


def test_this_hosts_port_lost_fails_the_check(world):
    turned_on(world)
    world.sim.ports[OWN].key = STRANGER
    assert not world.check()
    detail = world.applied().check_detail
    assert "Can't query the fabric" in detail and "neither" in detail


def test_no_full_verify_fails_the_check(world, monkeypatch):
    turned_on(world)
    keeper = world.keeper()
    real = keeper._verify

    def verify(*args, scope, **kwargs):
        if scope == prober.FULL:
            raise prober.ProbeError("the prober exited with status 3")
        return real(*args, scope=scope, **kwargs)

    monkeypatch.setattr(keeper, "_verify", verify)
    assert not world.check()
    assert "no full verify" in world.applied().check_detail
    # One that's older than two intervals fails it too.
    monkeypatch.setattr(keeper, "_verify", real)
    world.check()
    result, began = keeper._last_full
    keeper._last_full = (
        result,
        began - 3 * world.config.port_verify_interval,
    )
    monkeypatch.setattr(keeper, "_verify", verify)
    assert not world.manager.check_fabric()
    assert "older than two" in world.applied().check_detail


def test_a_prober_failure_is_a_failed_query(world):
    turned_on(world)
    world.driver.probe_error = prober.ProbeError("the prober exited with status 3")
    unanswered = world.manager.unanswered_checks
    assert not world.check()
    assert world.manager.unanswered_checks == unanswered + 1
    assert "prober" in world.applied().check_detail


def test_a_second_confirmation_promotes_the_pending_switches(world):
    world.manager.start()
    world.manager.reconcile()
    first = confirm(world)
    assert first.pending == [S1, S2] and not first.armed
    # Pending switches aren't expected, so the check can't pass yet.
    assert expected(world) == set() and expected(world, pending=True) == {S1, S2}
    second = confirm(world)
    assert second.switches == [S1, S2] and second.pending == []
    assert expected(world) == {S1, S2} and expected(world, pending=True) == set()
    assert second.armed


def test_confirm_fabric_uses_the_prober_while_rows_exist(world):
    turned_on(world)
    assert confirm(world).switches == [S1, S2]
    assert world.driver.fabric_calls[-1][2] == {S1, S2}


def test_an_unconfirmed_switch_isnt_given_both_keys(world):
    running_with_rows(world)
    # An adapter that answers NodeInfo as a switch, after the switches were
    # confirmed
    world.sim.add(X, switch=True, key=M)
    world.sim.cable(S1, 7, X, 1)
    world.driver.sweep()
    world.check()
    assert world.rows()[X].switch is True
    # Armed by a confirmation before X appeared
    arm(world)
    world.restart()
    history = len(world.sim.history)
    for _ in range(3):
        world.check()
    assert world.sim.ports[X].key == derived(X)
    assert not tenants_given_m_key(world.sim, history, {X, TN, HV, T3})


# Jobs


def _port_job(world):
    world.manager.put_network(NETWORK, conftest.network())
    return world.manager.put_port(conftest.PORT_ID, conftest.port())


def _status(world, job):
    return world.manager.get_job(job).status


def test_a_job_completes_when_its_port_answers(world):
    turned_on(world)
    job = _port_job(world)
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_a_job_is_held_while_its_port_doesnt_answer(world, caplog):
    turned_on(world)
    world.sim.ports[TN].key = STRANGER
    network = world.manager.put_network(
        OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200)
    )
    job = _port_job(world)
    for _ in range(3):
        world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.PENDING
    assert f"0x{TN:016x}" in world.manager.get_job(job).error
    # It holds no one else's job.
    assert _status(world, network) == db.JobStatus.COMPLETED
    alarms = [r for r in caplog.records if "its jobs are held" in r.getMessage()]
    assert len(alarms) == 1
    # It times out as any held job does, with the reason.
    with world.sessionmaker() as session:
        session.get(db.Job, job).deadline = _now() - datetime.timedelta(seconds=1)
        session.commit()
    finished = world.manager.get_job(job)
    assert finished.status == db.JobStatus.FAILED
    assert f"0x{TN:016x}" in finished.error


def test_a_held_job_completes_once_released(world):
    turned_on(world)
    world.sim.ports[TN].key = STRANGER
    job = _port_job(world)
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.PENDING
    world.keeper().release_port(TN)
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_a_job_completes_when_its_link_is_down(world):
    turned_on(world)
    world.sim.down |= {(S1, 3), (TN, 1)}
    job = _port_job(world)
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_a_job_on_a_guid_without_a_row_completes_as_before(world):
    turned_on(world)
    world.sim.ports[TN].key = STRANGER
    world.manager.put_network(NETWORK, conftest.network())
    job = world.manager.put_port(
        VF_PORT, conftest.port(VF_PORT, client_id="20:12:34:56:00:00:78:21:b7")
    )
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_evidence_from_before_the_sweep_doesnt_count(world):
    turned_on(world)
    job = _port_job(world)
    world.manager.sync()
    world.manager.check_fabric()
    evidence = world.keeper()._evidence[TN]
    # A sweep since the evidence was taken
    world.manager._sweeps += 1
    world.manager.evaluate_jobs()
    assert _status(world, job) == db.JobStatus.PENDING
    world.manager._sweeps -= 1
    # Evidence taken at an applied generation before the job's
    world.keeper()._evidence[TN] = dataclasses.replace(
        evidence, generation=evidence.generation - 1
    )
    world.manager.evaluate_jobs()
    assert _status(world, job) == db.JobStatus.PENDING
    world.keeper()._evidence[TN] = evidence
    world.manager.evaluate_jobs()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_the_jobs_gets_go_to_their_ports_only(world):
    turned_on(world)
    world.check()
    job = _port_job(world)
    world.manager.sync()
    sent = len(world.sim.sent)
    world.manager.check_fabric()
    probed = {guid for guid, *_ in world.sim.sent[sent:]}
    assert TN in probed and HV not in probed and T3 not in probed
    world.manager.evaluate_jobs()
    assert _status(world, job) == db.JobStatus.COMPLETED


# OpenSM's log


@pytest.mark.parametrize(
    "report, held",
    [
        (SweepReport(dr_errors=True), {"network", "port"}),
        (SweepReport(unreached={TN}), {"port"}),
        (SweepReport(unreached={HV}), {"network", "port"}),
        (SweepReport(unreached={0}), {"network", "port"}),
        (SweepReport(bad_m_key_traps=3), set()),
    ],
)
def test_the_log_holds_jobs_with_rows(world, report, held):
    turned_on(world)
    world.manager.put_network(OTHER_NETWORK, conftest.network(OTHER_NETWORK, 200))
    jobs = {
        "network": world.manager.put_network(
            "a13bba05-eb07-45ba-ace2-765706b2d701",
            conftest.network("a13bba05-eb07-45ba-ace2-765706b2d701", 300),
        ),
        "port": _port_job(world),
    }
    world.driver.report = report
    world.manager.reconcile()
    pending = {
        name
        for name, job in jobs.items()
        if _status(world, job) == db.JobStatus.PENDING
    }
    assert pending == held


@pytest.mark.parametrize(
    "report",
    [SweepReport(dr_errors=True), SweepReport(unreached={0, HV})],
)
def test_the_log_changes_nothing_without_rows(world, report):
    world.config.hardening = "all"
    world.manager.start()
    confirm(world)
    world.driver.report = report
    job = _port_job(world)
    world.manager.reconcile()
    assert _status(world, job) == db.JobStatus.COMPLETED


def test_bad_m_key_traps_are_only_counted(world, caplog):
    turned_on(world)
    world.driver.report = SweepReport(bad_m_key_traps=4)
    _port_job(world)
    world.manager.reconcile()
    assert any("4 Bad M_Key traps" in r.getMessage() for r in caplog.records)


# Progress and concurrency


def test_progress_is_logged_when_the_count_changes(world, caplog):
    keeper = world.keeper()
    rows = {
        guid: port_keys.PortState(guid, holds=M_KEY, reachable=True)
        for guid in (OWN, S1, TN)
    }
    keeper._progress(rows, True)
    keeper._progress(rows, True)
    rows[OWN] = dataclasses.replace(rows[OWN], holds=DERIVED)
    keeper._progress(rows, True)
    rows = {guid: dataclasses.replace(row, holds=DERIVED) for guid, row in rows.items()}
    keeper._progress(rows, True)
    keeper._progress(rows, True)
    lines = [
        r.getMessage() for r in caplog.records if "Per-port M_Keys" in r.getMessage()
    ]
    assert lines == [
        "Per-port M_Keys turning on: 3 reachable ports still hold the stored M_Key",
        "Per-port M_Keys turning on: 2 reachable ports still hold the stored M_Key",
        "Per-port M_Keys are on on every reachable port",
    ]


def test_a_command_beside_a_verify_isnt_lost(world):
    turned_on(world)
    world.sim.down |= {(S1, 5), (T4, 1)}
    keeper = world.keeper()
    done = []

    def beside(sim):
        if not done:
            done.append(True)
            keeper.add_link((S1, 6), (Z, 1))
            keeper.release_port(T4)

    world.sim.on_batch = beside
    world.check()
    world.sim.on_batch = None
    rows = world.rows()
    assert Z in rows and rows[T4].released
    with world.sessionmaker() as session:
        links = port_keys.load(session)[1]
    assert (S1, 6) in links and (Z, 1) in links


def test_merge_verify_keeps_what_a_command_changed():
    old = {
        1: port_keys.PortState(1, holds=M_KEY, reachable=True),
        2: port_keys.PortState(2, holds=M_KEY, reachable=True),
    }
    fresh = dict(old)
    fresh[1] = dataclasses.replace(old[1], released=True)
    fresh[3] = port_keys.PortState(3)
    result = prober.VerifyResult(
        ports={
            1: dataclasses.replace(old[1], holds=DERIVED, released=False),
            2: dataclasses.replace(old[2], lapsed=True),
        },
        answered={1, 2},
        keyed={1},
    )
    changed = port_keeper.merge_verify(fresh, result)
    assert changed[1].holds == DERIVED and not changed[1].released
    # A lapsed port's answer shows nothing about its key, nor its release.
    assert changed[2].holds == M_KEY and changed[2].lapsed
    assert 3 not in changed
    fresh[2] = dataclasses.replace(fresh[2], released=True)
    assert port_keeper.merge_verify(fresh, result)[2].released
    # A row forgotten meanwhile isn't brought back.
    del fresh[1]
    assert 1 not in port_keeper.merge_verify(fresh, result)


# The operator's commands


def test_port_m_key(world):
    keeper = world.keeper()
    # With no rows, the stored m_key
    assert keeper.port_m_key(TN) == f"0x{M:016x}"
    turned_on(world)
    assert keeper.port_m_key(TN) == f"0x{derived(TN):016x}"
    keeper.add_link((S1, 6), (Z, 1))
    with pytest.raises(port_keeper.PortKeysError, match="isn't known") as raised:
        keeper.port_m_key(Z)
    assert_no_key(raised.value, world.sim)
    with pytest.raises(port_keeper.PortKeysError, match="no port"):
        keeper.port_m_key(VF)


def test_port_key_states_show_no_key(world):
    turned_on(world)
    lines = world.keeper().states()
    assert lines[0] == "target on, OpenSM last started on"
    assert f"0x{TN:016x} hca derived reachable" in lines
    assert f"0x{S1:016x} switch derived reachable" in lines
    shown = "\n".join(lines)
    assert not any(text in shown for text in _key_texts(world.sim))


def test_forget_port(world):
    running_with_rows(world)
    keeper = world.keeper()
    with pytest.raises(port_keeper.PortKeysError, match="present GUID"):
        keeper.forget_port(T4)
    with world.sessionmaker() as session:
        session.get(db.FabricPort, port_keys.guid_text(T4)).last_seen = (
            _now() - datetime.timedelta(days=2)
        )
        session.commit()
    assert keeper.forget_port(T4) == 2
    assert T4 not in world.rows()
    with world.sessionmaker() as session:
        links = port_keys.load(session)[1]
    assert (S1, 5) not in links and (T4, 1) not in links
    with pytest.raises(port_keeper.PortKeysError, match="no port"):
        keeper.forget_port(T4)


def test_add_link_replaces_a_recabled_ports_link(world):
    running_with_rows(world)
    keeper = world.keeper()
    assert keeper.add_link((S1, 5), (Z, 1)) == 1
    with world.sessionmaker() as session:
        links = port_keys.load(session)[1]
    assert links[(S1, 5)].peer_guid == Z
    with pytest.raises(port_keeper.PortKeysError):
        keeper.add_link((S1, 5), (S1, 5))


def test_release_port_needs_a_row(world):
    with pytest.raises(port_keeper.PortKeysError, match="no port"):
        world.keeper().release_port(TN)


@pytest.mark.parametrize(
    "text, value",
    [("0x00000000000000b1", 0xB1), ("b1", 0xB1), ("0XB1", 0xB1)],
)
def test_guids_with_or_without_0x(text, value):
    assert port_keeper.parse_guid(text) == value
    assert port_keeper.parse_endpoint(f"{text}:3") == (value, 3)


@pytest.mark.parametrize("text", ["", "x", "0", "1" * 17, "0xb1:", "0xb1:255"])
def test_bad_guids_are_refused(text):
    with pytest.raises(port_keeper.PortKeysError):
        if ":" in text:
            port_keeper.parse_endpoint(text)
        else:
            port_keeper.parse_guid(text)


def test_no_state_is_written_by_a_check_while_opensm_is_stopped(world):
    running_with_rows(world)
    world.driver.stop()
    reads = world.driver.reads
    assert world.keeper().check() is None
    assert world.driver.reads == reads


def test_keeper_bounds_the_start_verify(world, monkeypatch):
    running_with_rows(world)
    bounds = []
    keeper = world.keeper()
    real = keeper._verify

    def verify(*args, bound, **kwargs):
        bounds.append((kwargs["scope"], kwargs["helper"], bound))
        return real(*args, bound=bound, **kwargs)

    world.manager.stop()
    world.new_manager()
    monkeypatch.setattr(world.manager.port_keeper, "_verify", verify)
    world.manager.start()
    assert bounds == [(prober.FULL, True, world.config.start_timeout / 2)]


# Trust is by a switch row's own GUID


def test_a_tenant_claiming_a_confirmed_switchs_guid_is_never_sent_m_key(world):
    running_with_rows(world)
    # An adapter that answers NodeInfo as a switch, with S1's GUID, a
    # confirmed switch's, as its node GUID
    world.sim.add(X, switch=True, key=M, node_guid=S1)
    world.sim.cable(S1, 7, X, 1)
    world.driver.sweep()
    world.check()
    assert world.rows()[X].switch is True
    # Armed by a confirmation before X appeared
    arm(world)
    world.restart()
    assert world.sim.ports[X].key == derived(X)
    # Then it refuses its derived key.
    world.sim.ports[X].key = STRANGER
    history = len(world.sim.history)
    # Successive checks, each a verify with discovery rounds
    for _ in range(3):
        world.check()
        world.check(due=False)
    assert not [
        guid
        for _, guid, key, _ in world.sim.history[history:]
        if guid == X and key == M
    ]
    assert world.driver.fabric_calls[-1][2] == {S1, S2}


def test_a_first_confirmation_on_a_fresh_fabric_doesnt_arm(world):
    # OpenSM is running; no check has folded its cache yet.
    world.manager.start()
    confirmation = confirm(world)
    # The links the running OpenSM's cache lists are recorded,
    with world.sessionmaker() as session:
        assert (S1, 2) in port_keys.load(session)[1]
    # the switches are pending, and that's why it isn't armed.
    assert confirmation.switches == [S1, S2] and confirmation.pending == [S1, S2]
    assert expected(world, pending=True) == {S1, S2} and expected(world) == set()
    assert any("pending" in problem for problem in confirmation.problems)
    assert world.state()["target"] is False
    world.manager.reconcile()
    assert world.applied().check_detail.startswith("No expected switches")
    # Run again, once the list is checked: promoted, and armed
    confirmation = confirm(world)
    assert confirmation.pending == [] and confirmation.armed
    assert expected(world) == {S1, S2}
    world.manager.reconcile()
    assert world.applied().check_ok, world.applied().check_detail


def test_confirm_fabric_follows_unconfirmed_switches(world):
    running_with_rows(world)
    # A new switch, behind S2, that no one has confirmed yet
    world.sim.add(0x53, switch=True, ports=2, key=M)
    world.sim.cable(S2, 3, 0x53, 1)
    world.driver.sweep()
    world.check()
    assert 0x53 not in world.driver.fabric_calls[-1][2]
    confirmation = confirm(world)
    assert confirmation.switches == [S1, S2, 0x53] and confirmation.pending == [0x53]
    # Pending: not routed through or counted, and not expected
    world.check()
    assert world.driver.fabric_calls[-1][2] == {S1, S2}
    assert world.applied().check_ok, world.applied().check_detail
    # Found again by a later run: confirmed
    assert confirm(world).pending == []
    world.check()
    assert world.driver.fabric_calls[-1][2] == {S1, S2, 0x53}


# A re-cabled port


def test_a_recabled_port_leaves_no_stale_half_link(world):
    turned_on(world)
    # TN moves from S1 port 3 to S1 port 6; S1 port 3 is left empty.
    del world.sim.cables[(S1, 3)], world.sim.cables[(TN, 1)]
    world.sim.cable(S1, 6, TN, 1)
    world.driver.sweep()
    job = _port_job(world)
    world.manager.reconcile()
    with world.sessionmaker() as session:
        links = port_keys.load(session)[1]
    assert (S1, 3) not in links
    assert links[(TN, 1)].peer_port == 6 and links[(S1, 6)].peer_guid == TN
    # Reached through S1 port 6, not reported as behind a down link
    result = world.keeper()._last_full[0]
    assert TN in result.keyed and TN not in result.link_down
    assert world.keeper()._evidence[TN].kind == port_keeper.ANSWERED
    assert _status(world, job) == db.JobStatus.COMPLETED
    world.restart()
    lines = world.driver.writes[-1]["neighbors"]
    assert f"0x{S1:016x}:3 " not in lines


# A start that never logs SUBNET UP, and the part-way fabric


def test_a_turn_on_start_that_fails_with_nothing_moved_is_set_back(world):
    running_with_rows(world)
    assert confirm(world).armed
    # OpenSM keys nothing, and never logs SUBNET UP.
    world.driver.stall = True
    world.driver.start_error = ReloadError("OpenSM didn't start within 1 seconds")
    world.manager.stop()
    world.new_manager().start()
    # Settled as a failed success rule: nothing moved, so the target is set
    # back, and OpenSM runs without the option.
    assert world.state()["target"] is False
    assert world.driver.is_running and not world.driver.per_port()
    assert {port.key for port in world.sim.ports.values()} == {M}


def test_a_turn_on_start_that_fails_after_keying_is_part_way(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed
    world.driver.start_error = ReloadError("OpenSM didn't start within 1 seconds")
    world.manager.stop()
    world.new_manager().start()
    assert not world.driver.is_running
    assert any("part-way" in r.getMessage() for r in caplog.records)
    assert world.state()["target"] is True
    world.manager._check_opensm()
    assert world.driver.is_running and world.state()["confirmed"]


def test_a_restart_while_unsettled_starts_nothing_until_a_verify_succeeds(world):
    running_with_rows(world)
    assert confirm(world).armed
    world.driver.stall = True

    def break_prober(driver):
        driver.probe_error = prober.ProbeError("the prober exited with status 3")

    world.driver.after_sweep = break_prober
    world.manager.stop()
    world.new_manager().start()
    assert not world.driver.is_running
    # The manager restarts: what it held in memory is gone, and the prober
    # still fails.
    world.driver.after_sweep = None
    starts, runs = len(world.driver.starts), world.driver.runs
    world.new_manager().start()
    # prepare refused it: OpenSM wasn't started on either target
    assert not world.driver.is_running
    assert len(world.driver.starts) == starts + 1
    assert world.driver.runs == runs
    assert {port.key for port in world.sim.ports.values()} == {M}
    # Once the prober works, the start goes ahead, and settles.
    world.driver.probe_error = None
    world.manager._check_opensm()
    assert world.driver.is_running
    assert world.state()["target"] is False and not world.driver.per_port()


def test_a_set_back_needs_no_row_holding_a_derived_key(world, caplog):
    running_with_rows(world)
    assert confirm(world).armed

    def crash(driver):
        raise Crash()

    world.driver.after_sweep = crash
    world.manager.stop()
    with pytest.raises(Crash):
        world.new_manager().start()
    # The next start's OpenSM keys nothing more, so no port moves during it,
    # but the rows the volume gave hold derived keys: part-way, not set back.
    world.driver.after_sweep = None
    world.driver.stall = True
    world.new_manager().start()
    assert not world.driver.is_running
    assert world.state()["target"] is True
    assert any("part-way" in r.getMessage() for r in caplog.records)


# Adoption only without a recorded turn-on


def test_derived_leftovers_of_a_turn_off_arent_adopted(world, caplog):
    turned_on(world)
    # A node that's powered off through the turn-off keeps its derived key.
    world.sim.down |= {(S1, 5), (T4, 1)}
    world.config.hardening = "all"
    world.restart()
    world.manager.reconcile()
    assert world.rows()[T4].holds == DERIVED
    # The group is named again: the target stays off.
    world.config.hardening = "all, m_key_per_port"
    world.restart()
    assert world.state()["target"] is False and not world.driver.per_port()
    assert any("--confirm-fabric arms" in r.getMessage() for r in caplog.records)
    entries = port_keys.parse_guid2mkey(world.driver.writes[-1]["guid2mkey"])
    assert entries[T4] == derived(T4)
    # It comes back, and OpenSM gives it m_key through its entry.
    world.sim.down -= {(S1, 5), (T4, 1)}
    world.driver.sweep()
    assert world.sim.ports[T4].key == M


# Writes of the rows touch only what changed


def test_a_merge_doesnt_write_released(world):
    turned_on(world)
    world.keeper().release_port(HV)
    world.sim.ports[HV].key = STRANGER
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE fabric_ports"):
            statements.append(statement)

    engine = world.sessionmaker.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        world.check()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements
    # An unchanged column isn't written, so a --release-port committed
    # beside a merge isn't undone by it, under REPEATABLE READ too.
    assert not any("released" in statement for statement in statements)
    assert world.rows()[HV].released


def test_a_down_ports_derived_row_doesnt_stop_a_set_back(world):
    turned_on(world)
    # A node that's down through a turn-off keeps its derived key and row.
    world.sim.down |= {(S1, 5), (T4, 1)}
    world.config.hardening = "all"
    world.restart()
    world.manager.reconcile()
    world.config.hardening = "all, m_key_per_port"
    world.restart()
    world.manager.reconcile()
    assert world.rows()[T4].holds == DERIVED
    # It's behind a down link, so the confirmation arms the turn-on.
    assert confirm(world).armed
    # The turn-on start fails with nothing moved: set back, not retried.
    world.driver.stall = True
    world.restart()
    assert world.state()["target"] is False
    assert world.driver.is_running and not world.driver.per_port()
    entries = port_keys.parse_guid2mkey(world.driver.writes[-1]["guid2mkey"])
    assert entries[T4] == derived(T4)


def test_an_adoption_whose_verify_fails_stays_on(world):
    for port in world.sim.ports.values():
        port.key = derived(port.guid)
    neighbors = world.sim.neighbors_of(set(world.sim.ports))
    entries = {guid: derived(guid) for guid in world.sim.ports}
    world.driver.volume = {
        "neighbors": (port_keys.render_neighbors(neighbors), _now()),
        "guid2mkey": (port_keys.render_guid2mkey(entries), _now()),
    }
    world.driver.probe_error = prober.ProbeError("the prober exited with status 3")
    world.manager.start()
    assert world.driver.is_running and world.driver.per_port()
    assert world.driver.conf()["m_key"] == f"0x{BASE:016x}"
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())
    state = world.state()
    assert state["target"] and state["running"] and state["confirmed"]
    assert state["fingerprint"] == port_keys.fingerprint(BASE)
    assert not tenants_given_m_key(world.sim, 0, {TN, HV, T3, T4})


def test_an_unreadable_cache_alarm_says_how_to_recover(world):
    turned_on(world)
    world.manager.stop()
    world.driver.volume.update(guid2mkey=("truncat", _now()))
    world.new_manager()
    with pytest.raises(ReloadError, match="move it out of the cache volume"):
        world.manager._start_opensm()


def _cli_confirm(world, monkeypatch, capsys):
    from alternative_fabric_manager import cli, drivers

    monkeypatch.setattr(config_mod.Config, "load", lambda _path: world.config)
    monkeypatch.setattr(db, "make_sessionmaker", lambda *a, **k: world.sessionmaker)
    monkeypatch.setattr(drivers, "make_driver", lambda _config: world.driver)
    cli.main(["--config-file", "afm.conf", "--confirm-fabric"])
    return capsys.readouterr().out.splitlines()


def test_confirm_fabric_marks_switches_not_confirmed_before(world, monkeypatch, capsys):
    running_with_rows(world)
    world.sim.add(0x53, switch=True, ports=2, key=M)
    world.sim.cable(S2, 3, 0x53, 1)
    world.driver.sweep()
    world.check()
    with pytest.raises(SystemExit) as raised:
        _cli_confirm(world, monkeypatch, capsys)
    lines = capsys.readouterr().out.splitlines()
    # The switches are printed, and recorded, before it exits non-zero.
    assert lines[:3] == [
        f"0x{S1:016x}",
        f"0x{S2:016x}",
        "0x0000000000000053 (new: not trusted until you confirm the fabric "
        "again; check that it's a switch)",
    ]
    assert lines[3].startswith("1 switches are new, and pending")
    message = str(raised.value.code)
    assert message.startswith("alternative-fabric-manager: ")
    assert "pending" in message and "0x0000000000000053" in message
    assert world.state()["target"] is False
    # Nothing new: the switches, one per line, and that it's armed
    assert _cli_confirm(world, monkeypatch, capsys) == [
        *(f"0x{guid:016x}" for guid in (S1, S2, 0x53)),
        "Per-port M_Keys are armed: the next restart of the manager turns them on",
    ]
    assert world.state()["target"] is True


def test_confirm_fabric_with_ibswitches_prints_guids_only(world, monkeypatch, capsys):
    world.config.hardening = "all"
    world.manager.start()
    assert _cli_confirm(world, monkeypatch, capsys) == [
        f"0x{guid + 0x1000:016x}" for guid in (S1, S2)
    ]
    # Nothing read beyond the switches, and no row written
    assert world.driver.reads == 0 and world.driver.probes == []
    assert world.rows() == {}


@pytest.mark.parametrize("change", [{"ha": True}, {"driver": "docker"}])
def test_confirm_fabric_in_ha_or_with_another_driver_confirms_as_before(
    world, monkeypatch, capsys, change
):
    running_with_rows(world)
    for name, value in change.items():
        setattr(world.config, name, value)
    reads, probes = world.driver.reads, len(world.driver.probes)
    assert _cli_confirm(world, monkeypatch, capsys) == [
        f"0x{guid + 0x1000:016x}" for guid in (S1, S2)
    ]
    assert world.driver.reads == reads and len(world.driver.probes) == probes
    assert world.state()["target"] is False


def test_a_turn_on_driven_by_confirm_fabric_alone(world, monkeypatch, capsys):
    world.manager.start()
    world.manager.reconcile()
    with pytest.raises(SystemExit, match="pending"):
        _cli_confirm(world, monkeypatch, capsys)
    capsys.readouterr()
    assert _cli_confirm(world, monkeypatch, capsys)[-1].startswith(
        "Per-port M_Keys are armed"
    )
    history = len(world.sim.history)
    world.restart()
    assert world.driver.per_port() and world.state()["confirmed"]
    assert all(port.key == derived(port.guid) for port in world.sim.ports.values())
    world.manager.reconcile()
    assert world.applied().check_ok, world.applied().check_detail
    assert not tenants_given_m_key(world.sim, history, {TN, HV, T3, T4})


# Pending switches


def test_an_adapter_answering_as_a_switch_stays_pending_and_untrusted(
    world, monkeypatch, capsys
):
    turned_on(world)
    # A tenant adapter answers NodeInfo as a switch, with a confirmed
    # switch's node GUID, behind which another port appears.
    # Unprotected as it comes, so OpenSM reaches it, and keys it.
    world.sim.add(X, switch=True, key=0, protect=0, node_guid=S1, ports=2)
    world.sim.cable(S1, 7, X, 1)
    world.sim.add(Z, key=0, protect=0)
    world.sim.cable(X, 2, Z, 1)
    world.driver.sweep()
    assert world.sim.ports[X].key == derived(X)
    world.check()
    # The operator confirms the fabric, as after adding a real switch: with
    # the target on, it only confirms.
    lines = _cli_confirm(world, monkeypatch, capsys)
    assert f"0x{X:016x} (new: not trusted" in lines[2]
    assert expected(world) == {S1, S2} and expected(world, pending=True) == {X}
    history = len(world.sim.history)
    for _ in range(3):
        world.check()
        world.check(due=False)
        assert world.driver.fabric_calls[-1][2] == {S1, S2}
    sent = [(guid, key) for _, guid, key, _ in world.sim.history[history:]]
    assert (X, M) not in sent
    assert not [guid for guid, _ in sent if guid == Z]  # never routed through
    # A second confirmation is the operator's decision to trust it.
    confirm(world)
    assert X in expected(world)
    world.check()
    assert world.driver.fabric_calls[-1][2] == {S1, S2, X}


def test_a_new_real_switch_is_pending_then_confirmed(world):
    turned_on(world)
    # As they come, unprotected: OpenSM reaches them, and keys them.
    world.sim.add(0x53, switch=True, ports=2, key=0, protect=0)
    world.sim.cable(S2, 3, 0x53, 1)
    world.sim.add(Z, key=0, protect=0)
    world.sim.cable(0x53, 2, Z, 1)
    world.driver.sweep()
    assert world.sim.ports[Z].key == derived(Z)
    world.check()
    assert confirm(world).pending == [0x53]
    # The fabric check still passes on the confirmed switches; the port
    # behind the pending one isn't reached, so the lost-port rule holds the
    # fabric, as for any port not reached.
    assert not world.check()
    assert f"0x{Z:016x}" in world.applied().check_detail
    assert "Missing" not in world.applied().check_detail
    assert confirm(world).pending == []
    assert world.check(), world.applied().check_detail


def test_a_pending_switch_not_found_again_is_dropped(world):
    running_with_rows(world)
    world.sim.add(0x53, switch=True, ports=2, key=M)
    world.sim.cable(S2, 3, 0x53, 1)
    world.driver.sweep()
    world.check()
    assert confirm(world).pending == [0x53]
    world.sim.down |= {(S2, 3), (0x53, 1)}
    confirmation = confirm(world)
    assert confirmation.switches == [S1, S2] and confirmation.pending == []
    assert expected(world) == {S1, S2} and expected(world, pending=True) == set()


def test_ibswitches_confirms_nothing_as_pending(world):
    world.config.hardening = "all"
    world.manager.start()
    confirmation = confirm(world)
    assert confirmation.pending == []
    assert expected(world) == {S1 + 0x1000, S2 + 0x1000}


def test_a_discovery_cut_short_confirms_nothing(world, monkeypatch, capsys):
    running_with_rows(world)
    keeper = world.keeper()
    real = keeper._verify

    def verify(*args, follow_unconfirmed=False, **kwargs):
        if follow_unconfirmed:
            kwargs["bound"] = 1e-9
        return real(*args, follow_unconfirmed=follow_unconfirmed, **kwargs)

    monkeypatch.setattr(keeper, "_verify", verify)
    with pytest.raises(ReloadError, match="ran out of time"):
        confirm(world)
    assert expected(world) == {S1, S2}
    monkeypatch.setattr(
        manager_mod.Manager,
        "confirm_fabric",
        lambda self: (_ for _ in ()).throw(ReloadError("the prober ran out of time")),
    )
    with pytest.raises(SystemExit) as raised:
        _cli_confirm(world, monkeypatch, capsys)
    assert str(raised.value.code) == (
        "alternative-fabric-manager: can't confirm the fabric: the prober ran out "
        "of time (no switch was confirmed)"
    )
