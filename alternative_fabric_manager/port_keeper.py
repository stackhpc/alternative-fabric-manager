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

"""Per-port M_Keys: before each start, at every check, for each job, and
the operator's commands

specs/secured-profile.md, Per-port M_Keys, The design, is the source of
truth. port_keys holds the rules and prober the Gets; this module decides
when each runs and what its result means for OpenSM, the fabric check and
the jobs. The Manager owns a PortKeeper and calls it from thin hooks.

It acts only while the m_key_per_port group is named or any fabric_ports or
fabric_links row exists ("active"), and only with the managed driver, which
writes OpenSM's cache volume and runs the prober. Otherwise every hook
returns at once, so that OpenSM, its files, the fabric check and the jobs
are as they were without it.

In HA mode the leader keeps the rows, behind the leader fence (Manager.
fenced_session); a takeover never reads its host's cache volume, which an
older term left, and renders it from the rows; every instance's reach probe
runs the prober (reach_switches); and only the leader, whose checks gather
it, has a job's evidence (specs/secured-profile.md, HA mode).

The rows have one writer in the manager, the applier's thread (and the
start, which runs before or on it), but the operator's commands run in
processes of their own beside it. So every write is a short transaction
that reads the rows afresh and changes only what its operation changed:
never a copy taken before a verify of many seconds, which would undo a link
--add-link recorded, or a --release-port, meanwhile.

specs/zero-touch.md makes the turn-on automatic: at a check that passes,
the manager arms it once the ports are ready, and restarts OpenSM to apply
the target (after_check). Every row that answers as a switch is followed.

A key opens a port, and the base every port: nothing here logs a key, nor
any of OpenSM's cache files, and no exception message holds one.
"""

import contextlib
import dataclasses
import datetime
import logging
import time

import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, effects, port_keys, prober
from alternative_fabric_manager.drivers import PartitionsRejected, ReloadError

LOG = logging.getLogger(__name__)

GROUP = "m_key_per_port"
# Seconds between reads of the running OpenSM's cache while a start that
# turns per-port keys on waits for it to show them
CONFIRM_POLL = 1.0
# A port seen more recently than this is present, and can't be forgotten
FORGET_AFTER = datetime.timedelta(days=1)
# At most how many times a verify is repeated as it learns which ports are
# switches: one per level of switches beyond the first
DISCOVERY_ROUNDS = 8
# How many GUIDs a log line or a check's detail names before "and N more"
SHOWN = 5
# At most how often the manager restarts OpenSM to apply the target, and
# after how long since the target changed it applies it whatever the jobs
# (specs/zero-touch.md, Applying), in seconds
APPLY_EVERY = 3600
APPLY_ANYWAY = 86400
# What the arming's blocking reason says about a failed turn-on
FAILED_REASON = (
    "a turn-on failed under this configuration and base, and is recorded: "
    "change the configuration, or run --retry-port-key-turn-on once its cause "
    "is fixed, to arm it again"
)
# What --retry-port-key-turn-on says when it clears a failed turn-on
RETRIED = (
    "Cleared the failed per-port M_Key turn-on. The manager arms it again at "
    "its next passing check once every port is ready, then restarts OpenSM to "
    "turn per-port M_Keys on, which re-keys every port, at a passing check "
    "with no open job (at most once an hour after its last attempt). "
    "--port-key-states shows what blocks arming."
)
# How many starts in a row of a turn-on may fall back to the target OpenSM
# last ran on before it's recorded as failed
FALLBACKS_TO_FAIL = 3
# The state table's text column holds at most this many characters
_TEXT = 255

# What a job's port showed after its sweep (Lost ports and jobs)
ANSWERED = "answered its key"
LINK_DOWN = "link down"


class PortKeysError(Exception):
    """An operator's command that can't be done: its message names GUIDs,
    never a key"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _guids(guids):
    """GUIDs for a message, at most SHOWN of them"""
    guids = sorted(guids)
    shown = ", ".join(f"0x{guid:016x}" for guid in guids[:SHOWN])
    if len(guids) > SHOWN:
        shown += f" and {len(guids) - SHOWN} more"
    return shown


def _on(on):
    return "on" if on else "off"


def parse_guid(text):
    """A GUID an operator typed, with or without 0x"""
    try:
        value = int(text.strip(), 16)
    except (AttributeError, ValueError):
        raise PortKeysError(f"{text!r} isn't a GUID") from None
    if not 0 < value < 2**64:
        raise PortKeysError(f"{text!r} isn't a 64-bit GUID")
    return value


def parse_endpoint(text):
    """A <GUID>:<port> an operator typed"""
    guid, sep, port = (text or "").rpartition(":")
    if not sep or not port.isdigit() or not 0 <= int(port) <= 254:
        raise PortKeysError(f"{text!r} isn't <GUID>:<port>, the port 0 to 254")
    return parse_guid(guid), int(port)


def merge_verify(fresh, result):
    """The rows a verify changed, applied to the rows as they are now

    fresh is the rows as just read back, which a command may have changed
    since the verify read them. Only the ports the verify probed change,
    and only in what a Get shows: which key a port holds (from an answer
    with ProtectBits 2), whether it lapsed, whether it's reachable, its
    node and whether it's a switch, and when it was seen. released is the
    operator's, cleared only once the port answers its key (result.keyed).
    A row deleted meanwhile stays deleted.
    """
    changed = {}
    for guid in result.answered:
        row, new = fresh.get(guid), result.ports.get(guid)
        if row is None or new is None:
            continue
        keyed = guid in result.keyed
        merged = dataclasses.replace(
            row,
            holds=new.holds if keyed else row.holds,
            lapsed=new.lapsed,
            reachable=new.reachable,
            switch=new.switch,
            node_guid=new.node_guid,
            last_seen=new.last_seen,
            released=row.released and not keyed,
        )
        if merged != row:
            changed[guid] = merged
    for guid in result.unanswered | result.unconfirmed:
        row = fresh.get(guid)
        if row is not None and guid not in result.answered and row.reachable:
            changed[guid] = dataclasses.replace(row, reachable=False)
    return changed


def expected_guids(session):
    """The expected switches' GUIDs as recorded, with the node GUIDs kept
    beside them, so that a switch found by either source counts"""
    guids = set()
    for row in session.scalars(sa.select(db.ExpectedSwitch)):
        guids.add(int(row.guid, 16))
        node_guid = getattr(row, "node_guid", None)
        if node_guid:
            guids.add(int(node_guid, 16))
    return guids


def _diff(old, new):
    """What new changed or added, by key"""
    return {key: value for key, value in new.items() if old.get(key) != value}


@dataclasses.dataclass
class _Plan:
    """What a start's prepare decided, for the success rule after it"""

    target_on: bool
    effective_on: bool
    # Whether the start is held to the success rule (Turning it on)
    confirm: bool = False
    own_guid: int | None = None
    # Recorded switch ports that answered the verify before the start
    switches: frozenset = frozenset()
    # Ports that answered their key in that verify
    keyed: frozenset = frozenset()
    # The rows OpenSM's cache was rendered from
    before: dict = dataclasses.field(default_factory=dict)
    links: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Evidence:
    """What a job's port showed at a check, after which sweep"""

    kind: str
    # The manager's count of recorded sweeps when it was taken, and the
    # applied generation then
    sweep: int
    generation: int


@dataclasses.dataclass
class Confirmation:
    """What --confirm-fabric did: the switches it recorded as expected, and
    those it dropped from them"""

    switches: list = dataclasses.field(default_factory=list)
    removed: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CheckOutcome:
    """What the per-port M_Key part of a fabric check found: the switches
    the prober reached, for _judge, or None to ask ibswitches as before,
    with each one's node GUID, by which the expected switches are
    translated when their source changes; and why the check fails, if it
    does"""

    switches: set | None = None
    nodes: dict = dataclasses.field(default_factory=dict)
    problems: list = dataclasses.field(default_factory=list)
    # The check's own verify, whose link states are the evidence a missing
    # switch is dropped on, never an older one's; and whether every verify
    # of the check finished in time: one that didn't is a failed query for
    # the expected switches (specs/zero-touch.md)
    verify: object = None
    complete: bool = True
    # Every verify of the check, from which a partial check places the ports
    # they reached (specs/ha.md, Where a port is)
    results: list = dataclasses.field(default_factory=list)
    # Whether the check ran a full verify, which reached every port it could
    full: bool = False


class PortKeeper:
    def __init__(self, manager):
        self.manager = manager
        self._driver_alarmed = False
        self._plan = None
        # A start whose success rule failed and whose prober then couldn't
        # tell which keys the ports hold: the next start runs that prober
        # again before OpenSM starts on either target
        self._unsettled = None
        # The arming's blocking reason last logged, to log it once per
        # change; "" once nothing blocks it
        self._arming_reason = None
        # The next start is one after a part-way turn-on failed: OpenSM is
        # started on the target, and left running, without the success
        # rule, which the hourly retries hold it to
        self._skip_rule = False
        self._reset_running()

    def _reset_running(self):
        """Forget what was learned from the OpenSM that ran last"""
        # The last full verify since OpenSM started: (result, monotonic
        # time it began)
        self._last_full = None
        self._evidence = {}
        self._held = set()
        self._changing = None

    @property
    def config(self):
        return self.manager.config

    @property
    def driver(self):
        return self.manager.driver

    @property
    def group_named(self):
        return GROUP in self.config.hardening_groups

    def _session(self):
        # In HA mode, the leader's sessions are behind the leader fence.
        return self.manager.fenced_session()

    # Whether to act at all

    @staticmethod
    def _has_rows(session):
        return (
            session.scalar(sa.select(db.FabricPort.guid).limit(1)) is not None
            or session.scalar(sa.select(db.FabricLink.guid).limit(1)) is not None
        )

    def _usable(self, rows):
        """Whether to act, given whether rows exist"""
        if not (self.group_named or rows):
            return False
        if self.config.driver != "managed":
            # Config.validate refuses the group with another driver, but
            # rows outlive a configuration change.
            if not self._driver_alarmed:
                LOG.error(
                    "Per-port M_Key rows exist, but the %s driver can't keep "
                    "them: OpenSM's cache isn't rendered from them, and the "
                    "prober doesn't run",
                    self.config.driver,
                )
                self._driver_alarmed = True
            return False
        return True

    def active(self):
        """Whether the group is named or any row exists, in single node with
        the managed driver"""
        if self.group_named and self.config.driver == "managed":
            return True
        with self._session() as session:
            rows = self._has_rows(session)
        return self._usable(rows)

    def _rows_in(self, session):
        """Whether rows exist and the hooks that take a session act"""
        if self.config.driver != "managed":
            return False
        return self._has_rows(session)

    # Keys

    def _m_keys(self):
        """The M_Key OpenSM uses with per-port keys off, and the base, as
        integers"""
        keys = self.manager.keys()
        if "m_key_base" not in keys:
            # Only a start stores it (Manager.keys)
            raise PortKeysError(
                "The per-port M_Key base isn't stored yet: start the manager "
                "once, and back up its database, first"
            )
        m_key = self.manager._key("m_key") or keys["m_key"]
        return port_keys.parse_key(m_key), port_keys.parse_key(keys["m_key_base"])

    # Writing the rows: short transactions on fresh rows

    def _merge(self, result):
        with self._session() as session:
            ports, _links = port_keys.load(session)
            port_keys.upsert(session, merge_verify(ports, result).values())
            session.commit()

    def _fold(self, fold, *args):
        with self._session() as session:
            ports, links = port_keys.load(session)
            new_ports, new_links, summary = fold(ports, links, *args)
            port_keys.upsert(
                session, _diff(ports, new_ports).values(), _diff(links, new_links)
            )
            # A re-cabled port's old peer, which the fold dropped
            for guid, port in set(links) - set(new_links):
                row = session.get(db.FabricLink, (port_keys.guid_text(guid), port))
                if row is not None:
                    session.delete(row)
            session.commit()
        return summary

    def _verify(
        self,
        ports,
        links,
        *,
        target_on,
        nothing,
        scope,
        helper,
        bound,
        helper_name=None,
    ):
        """A verify within bound seconds, repeated while it learns that a
        port is a switch: routes go only through recorded switches, so rows
        that OpenSM's cache has just listed, whose NodeInfo no Get has seen
        yet, are reached one level of switches at a time

        Every row that answers as a switch is routed through
        (specs/zero-touch.md). helper_name names the helper container, the
        prober's by default.
        """
        m_key, base = self._m_keys()
        named = {"name": helper_name} if helper_name else {}
        run = self.driver.prober(helper=helper, **named)
        deadline = time.monotonic() + bound
        for _round in range(DISCOVERY_ROUNDS):
            result = prober.verify(
                ports,
                links,
                m_key=m_key,
                base=base,
                target_on=target_on,
                nothing_derived=nothing,
                run=run,
                scope=scope,
                now=_now(),
                deadline=deadline,
            )
            # Only a switch that the next round would route through helps.
            learned = {
                guid
                for guid in result.answered
                if guid in ports
                and ports[guid].switch is not True
                and result.ports[guid].switch is True
            }
            if not learned or not result.complete:
                break
            ports = result.ports
        return result

    def _read_cache(self, container=None, quiet=False, strict=False):
        """(neighbors, guid2mkey entries, their times) from OpenSM's cache,
        a file that doesn't exist being empty; or None if it can't be read,
        which is logged without the files' content, at debug if quiet

        strict, before a start, raises ReloadError instead: a start writes
        the volume's files, which mustn't be replaced unseen.
        """
        try:
            files = self.driver.read_cache(container)
            now = _now()
            neighbors_text, neighbors_time = files.get("neighbors", ("", now))
            guid2mkey_text, guid2mkey_time = files.get("guid2mkey", ("", now))
            return (
                port_keys.parse_neighbors(neighbors_text),
                port_keys.parse_guid2mkey(guid2mkey_text),
                neighbors_time,
                guid2mkey_time,
            )
        except (ReloadError, ValueError) as e:
            if strict:
                message = (
                    f"Can't read OpenSM's cache before starting it ({e}), so it "
                    "isn't replaced unseen: the start is retried. If OpenSM left "
                    "a file truncated, move it out of the cache volume while "
                    "OpenSM is stopped, and the next start rebuilds it from the "
                    "records, as for a lost volume"
                )
                LOG.error("%s", message)
                raise ReloadError(message) from None
            LOG.log(
                logging.DEBUG if quiet else logging.ERROR,
                "Can't read OpenSM's cache, so the per-port M_Key rows stay as "
                "they were: %s",
                e,
            )
            return None

    # Before a start

    def refuse_start(self):
        """Refuse a start that would strand ports on derived keys: while any
        row holds one, turning per-port keys off depends on m_key_lookup and
        on the m_key group (The group)"""
        if self.config.driver != "managed":
            return
        with self._session() as session:
            derived = session.scalar(
                sa.select(sa.func.count())
                .select_from(db.FabricPort)
                .where(db.FabricPort.holds == port_keys.DERIVED)
            )
        if not derived:
            return
        lookup = self.config.opensm_conf.get("m_key_lookup")
        if lookup is not None and lookup.strip().upper() != "TRUE":
            raise config_mod.ConfigError(
                f"[opensm_conf] m_key_lookup is {lookup.strip()}, but {derived} "
                "ports hold per-port M_Keys, which OpenSM reaches only through "
                "its cache with m_key_lookup TRUE: remove the option"
            )
        if "m_key" not in self.config.hardening_groups:
            raise config_mod.ConfigError(
                f"[opensm] hardening doesn't name the m_key group, but {derived} "
                "ports hold per-port M_Keys, which only a start with it can turn "
                "back to the stored M_Key: name it"
            )

    def start(self, start):
        """Start OpenSM with start(prepare=...), the driver's start, and hold
        a start that turns per-port keys on to the success rule

        Returns start's SweepReport. Raises ReloadError with OpenSM stopped
        if the start can't go ahead, which the manager retries.
        """
        self._reset_running()
        self._log_failed_turn_on()
        if self._unsettled is not None:
            # Not on either target until the prober can tell which keys
            # the ports hold: then on the target if some port moved, or
            # with the target set back to off if none did
            self._settle(self._unsettled)
        self._plan = None
        try:
            report = start(prepare=self.prepare)
        except PartitionsRejected:
            # The manager quarantines what OpenSM couldn't parse, and starts
            # again.
            raise
        except ReloadError as e:
            plan = self._plan
            if plan is None or not plan.confirm:
                raise
            # OpenSM didn't start, but may have keyed ports first: as when
            # the success rule fails
            problem = f"OpenSM didn't start: {e}"
        else:
            plan = self._plan
            if plan is None or not plan.confirm:
                return report
            problem = self._await_keys(plan)
        if problem is None:
            with self._session() as session:
                port_keys.save_confirmed(session, True)
                session.commit()
            LOG.warning(
                "Per-port M_Keys are on: OpenSM gave this host's port and %d "
                "switches their derived keys",
                len(plan.switches),
            )
            return report
        LOG.error(
            "The start that turns per-port M_Keys on failed: %s. Stopping OpenSM "
            "to find which keys the ports hold",
            problem,
        )
        self.driver.stop()
        moved = self._settle(plan, problem)
        if moved:
            LOG.error(
                "Per-port M_Keys: a start that turns them on failed (%s), after "
                "OpenSM moved %d ports (%s) to their derived keys: the fabric is "
                "part-way, so OpenSM is started on the target, from the verified "
                "rows, and left running, and the turn-on is retried at most once "
                "an hour until it succeeds",
                problem,
                len(moved),
                _guids(moved),
            )
            with self._session() as session:
                port_keys.save_time(session, port_keys.LAST_APPLY, time.time())
                session.commit()
            self._skip_rule = True
        self._plan = None
        return start(prepare=self.prepare)

    def prepare(self, container):
        """Read, verify and render OpenSM's cache in a created container,
        before OpenSM starts in it (Before each start)"""
        m_key, base = self._m_keys()
        named = self.group_named
        with self._session() as session:
            target = port_keys.load_target(session)
            running = port_keys.load_running(session)
            confirmed = port_keys.load_confirmed(session)
            recorded = port_keys.load_fingerprint(session)
            if target and not named:
                # Turning it off: the operator's change, and the safe
                # direction
                port_keys.save_target(session, False)
                port_keys.save_confirmed(session, False)
                session.commit()
                target = False
                LOG.warning(
                    "The m_key_per_port group isn't named, so this start turns "
                    "per-port M_Keys off: OpenSM gives every port it reaches the "
                    "stored M_Key"
                )

        # Files that are absent are a lost volume, which the rows rebuild;
        # files that can't be read or parsed fail the start. In HA mode the
        # volume is never read: its files are this host's from an older
        # term, if any, and folding them could put back a link or a key
        # state the rows have since replaced (specs/secured-profile.md, HA
        # mode). The rows rebuild it, as a lost volume.
        if self.config.ha:
            now = _now()
            volume = ([], {}, now, now)
        else:
            volume = self._read_cache(container, strict=True)
        entries = volume[1]
        problem = port_keys.restore_problem(entries, m_key, base, recorded)
        if problem is not None:
            self._restore(problem)
            return

        neighbors, found, neighbors_time, guid2mkey_time = volume
        summary = self._fold(
            port_keys.fold_start,
            neighbors,
            found,
            m_key,
            base,
            neighbors_time,
            guid2mkey_time,
        )
        LOG.info("Folded OpenSM's cache into the per-port M_Key rows: %s", summary)
        with self._session() as session:
            ports, links = port_keys.load(session)
            leftovers = (
                named
                and not target
                and recorded is not None
                and any(row.holds == port_keys.DERIVED for row in ports.values())
            )
            if leftovers:
                LOG.warning(
                    "Per-port M_Keys are off, and ports recorded as holding "
                    "derived keys are given the stored M_Key as OpenSM reaches "
                    "them; the manager's checks arm per-port keys again once "
                    "every port is ready"
                )
            if port_keys.should_adopt(ports, named, target, recorded):
                port_keys.save_target(session, True)
                # Not held to the success rule, whose set-back would give the
                # ports, which hold derived keys already, the exposed m_key
                port_keys.save_confirmed(session, True)
                # Nor is it a transition: the ports hold derived keys
                # already, so on is the direction in which nothing changes,
                # and a verify that fails goes on from the rows on it.
                port_keys.save_running(session, True)
                session.commit()
                target = True
                confirmed = True
                running = True
                LOG.error(
                    "Ports hold keys derived from the stored base while the "
                    "per-port M_Key target is off and no turn-on is recorded, "
                    "as a database restored from before the turn-on would have: "
                    "adopting the target on, rather than giving them back the "
                    "stored M_Key"
                )

        volume_holds = port_keys.classify_entries(entries, m_key, base)
        nothing = port_keys.nothing_derived(ports, recorded, volume_holds)
        result = None
        # With nothing recorded, there is nothing to verify, and no port can
        # be lost: as at a start before per-port keys.
        verified = not ports
        try:
            if ports:
                result = self._verify(
                    ports,
                    links,
                    target_on=target,
                    nothing=nothing,
                    scope=prober.FULL,
                    helper=True,
                    bound=self.config.start_timeout / 2,
                )
        except ReloadError as e:
            LOG.error("Can't verify the ports' M_Keys before starting OpenSM: %s", e)
        if result is not None:
            # This host's port's and the switches' results are kept, whatever
            # happened to the rest.
            self._merge(result)
            LOG.info("Verified the ports' M_Keys before starting OpenSM: %s", result)
            if result.own_port_lost:
                message = (
                    "This host's port answers neither the stored M_Key nor its "
                    "derived key, so OpenSM can't sweep: restore the database "
                    "that holds its key, or wait for its lease to lapse"
                )
                LOG.error("%s", message)
                raise ReloadError(message)
            verified = result.complete
        effective = target
        if not verified and target == running and target and not confirmed:
            # A turn-on that hasn't succeeded yet: the fabric may be
            # part-way, and neither target is safe to start blind.
            message = (
                "The verify before this start "
                + ("failed" if result is None else "ran out of time")
                + " while per-port M_Keys are turning on, and the fabric may be "
                "part-way: OpenSM isn't started on either target until a verify "
                "succeeds"
            )
            LOG.error("%s", message)
            raise ReloadError(message)
        if not verified and target != running:
            effective = running
            target = self._fell_back(target, running, result is None)
        else:
            # A start on its target: the fallbacks in a row end.
            self._count_fallbacks(0)

        with self._session() as session:
            if effective and recorded is None:
                # Before any port can be given a key derived from it
                port_keys.save_fingerprint(session, port_keys.fingerprint(base))
            port_keys.save_running(session, effective)
            ports, links = port_keys.load(session)
            session.commit()
        self.manager._write_opensm_conf(port_keys=effective)
        self.driver.write_cache(
            container,
            {
                "neighbors": port_keys.render_neighbors(links),
                "guid2mkey": port_keys.render_guid2mkey(
                    port_keys.guid2mkey_entries(ports, effective, m_key, base)
                ),
            },
        )
        answered = result.answered if result is not None else set()
        skip, self._skip_rule = self._skip_rule, False
        self._plan = _Plan(
            target_on=target,
            effective_on=effective,
            confirm=effective and not confirmed and not skip,
            own_guid=result.own_guid if result is not None else None,
            switches=frozenset(
                guid
                for guid in answered
                if ports.get(guid, None) is not None and ports[guid].switch is True
            ),
            keyed=frozenset(result.keyed if result is not None else ()),
            before=ports,
            links=links,
        )
        LOG.info(
            "Starting OpenSM with per-port M_Keys %s (target %s); %d ports are "
            "changing",
            _on(effective),
            _on(target),
            len(port_keys.changing(ports, effective)),
        )

    def _count_fallbacks(self, count):
        """Record how many starts in a row fell back to the target OpenSM
        last ran on"""
        with self._session() as session:
            if port_keys.load_time(session, port_keys.FALLBACKS) != count:
                port_keys.save_time(session, port_keys.FALLBACKS, count)
            session.commit()

    def _fell_back(self, target, running, failed):
        """A start whose verify failed, which starts OpenSM on the target it
        last ran on: logged at a rising level, and on the third in a row of a
        turn-on, recorded as a failed turn-on with the target set back to
        off, which OpenSM still runs with, so that it stops restarting
        (specs/zero-touch.md, Applying). A turn-off keeps trying: it's the
        safe direction. Returns the target."""
        record = self._failure_record() if target else None
        with self._session() as session:
            count = (port_keys.load_time(session, port_keys.FALLBACKS) or 0) + 1
            port_keys.save_time(session, port_keys.FALLBACKS, count)
            session.commit()
        LOG.log(
            logging.WARNING if count == 1 else logging.ERROR,
            "The verify before this start %s, so OpenSM starts with per-port "
            "M_Keys %s, as it last ran, rather than beginning to turn them %s "
            "(%d starts in a row); the next start tries again",
            "failed" if failed else "ran out of time",
            _on(running),
            _on(target),
            count,
        )
        if not target or count < FALLBACKS_TO_FAIL:
            return target
        with self._session() as session:
            port_keys.save_target(session, False)
            port_keys.save_confirmed(session, False)
            port_keys.save_time(session, port_keys.TARGET_CHANGED, time.time())
            port_keys.save_text(session, port_keys.FAILED_TURN_ON, record)
            port_keys.save_time(session, port_keys.FALLBACKS, 0)
            session.commit()
        LOG.error(
            "Per-port M_Keys: %d starts in a row couldn't verify the ports to "
            "turn them on, so the target is set back to off and the failure is "
            "recorded: nothing arms them again until the configuration changes, "
            "or --retry-port-key-turn-on is run once the cause, which the lines "
            "above name, is fixed",
            count,
        )
        return False

    def _restore(self, problem):
        """The volume's cache and the database don't belong together: keep
        the target off and the volume's files as they are, and start OpenSM
        without per-port keys, which re-keys each port it reaches through
        its own cache to the stored M_Key (Restores)"""
        with self._session() as session:
            port_keys.save_target(session, False)
            port_keys.save_confirmed(session, False)
            port_keys.save_running(session, False)
            session.commit()
        LOG.error(
            "%s. Keeping per-port M_Keys off, leaving OpenSM's cache as it is, "
            "and starting OpenSM without them: it gives each port it reaches "
            "through its cache the stored M_Key",
            problem[:1].upper() + problem[1:],
        )
        self.manager._write_opensm_conf(port_keys=False)
        self._plan = _Plan(target_on=False, effective_on=False)

    # The success rule (Turning it on)

    def _await_keys(self, plan):
        """None once the running OpenSM's cache shows this host's port and
        every recorded switch that answered the verify before the start
        holding its derived key, within start_timeout; otherwise why not"""
        if plan.own_guid is None:
            return "the verify before the start didn't find this host's port"
        wanted = {plan.own_guid} | set(plan.switches)
        m_key, base = self._m_keys()
        deadline = time.monotonic() + self.config.start_timeout
        missing = wanted
        while True:
            if not self.driver.running():
                return "OpenSM stopped"
            # OpenSM may not have written it yet: polled quietly
            volume = self._read_cache(quiet=True)
            if volume is not None:
                entries = volume[1]
                missing = {
                    guid
                    for guid in wanted
                    if port_keys.classify(guid, entries.get(guid), m_key, base)
                    != port_keys.DERIVED
                }
                if not missing:
                    return None
            if time.monotonic() >= deadline:
                return (
                    f"within {self.config.start_timeout:g} s, OpenSM's cache "
                    f"didn't show {_guids(missing)} holding derived keys"
                )
            time.sleep(CONFIRM_POLL)

    def _settle(self, plan, problem=None):
        """After a turn-on start failed, with OpenSM stopped: find whether
        any port left the key it held before it, and return those that did.
        If none did, the target is set back to off. If the prober can't
        tell, raise ReloadError, for the manager to retry without starting
        OpenSM on either target."""
        self._unsettled = plan
        with self._session() as session:
            recorded = port_keys.load_fingerprint(session)
        try:
            result = self._verify(
                plan.before,
                plan.links,
                target_on=True,
                nothing=port_keys.nothing_derived(plan.before, recorded, ()),
                scope=prober.FULL,
                helper=True,
                bound=self.config.start_timeout / 2,
            )
        except ReloadError as e:
            message = (
                "Per-port M_Keys: a start that turns them on failed, and the "
                f"prober can't tell which keys the ports hold ({e}): OpenSM "
                "isn't started on either target until it can"
            )
            LOG.error("%s", message)
            raise ReloadError(message) from None
        self._merge(result)
        moved = {
            guid
            for guid in result.keyed
            if guid in plan.before
            and result.ports[guid].holds != plan.before[guid].holds
        }
        unknown = set(plan.keyed) - result.keyed
        if not moved and not unknown and not result.own_port_lost and result.complete:
            # A port that answered its derived key now, as after a crash
            # during an earlier turn-on start, makes the fabric part-way too.
            # A derived row that didn't answer, such as a node that was down
            # through a turn-off, doesn't: with the target off its derived
            # entry is rendered, and OpenSM reaches it when it returns.
            moved = {
                guid
                for guid in result.keyed
                if result.ports[guid].holds == port_keys.DERIVED
            }
        if moved:
            self._unsettled = None
            return moved
        if result.own_port_lost or not result.complete or unknown:
            reason = (
                "this host's port answered neither key"
                if result.own_port_lost
                else "the verify ran out of time"
                if not result.complete
                else f"{_guids(unknown)} no longer answer"
            )
            message = (
                "Per-port M_Keys: a start that turns them on failed, and the "
                f"prober can't tell whether any port moved ({reason}): OpenSM "
                "isn't started on either target until it can"
            )
            LOG.error("%s", message)
            raise ReloadError(message)
        self._unsettled = None
        record = self._failure_record()
        with self._session() as session:
            port_keys.save_target(session, False)
            port_keys.save_running(session, False)
            port_keys.save_confirmed(session, False)
            port_keys.save_time(session, port_keys.TARGET_CHANGED, time.time())
            # So that a fabric that can't be turned on isn't re-keyed in a
            # loop, across restarts and takeovers alike
            port_keys.save_text(session, port_keys.FAILED_TURN_ON, record)
            # The fingerprint stays: OpenSM ran with the option, and may have
            # keyed a port the prober can't see.
            session.commit()
        LOG.error(
            "Per-port M_Keys: %sno port left the key it held, so the target is "
            "set back to off and OpenSM is started without them. The failure is "
            "recorded: nothing arms them again until the configuration changes, "
            "or --retry-port-key-turn-on is run once the cause is fixed",
            f"a start that turns them on failed ({problem}); " if problem else "",
        )
        return set()

    # A failed turn-on (specs/zero-touch.md)

    def _failure_record(self):
        """What a failed turn-on records under the running configuration
        and base"""
        _m_key, base = self._m_keys()
        return port_keys.failure_record(base, self.manager.config_hash())

    def _failed_turn_on(self, session, record):
        """Whether a failed turn-on is recorded under this configuration and
        base (record, _failure_record's, worked out before the session,
        which holds the manager's lock that keys() takes); one recorded under
        another is cleared, in the session's transaction, since a change of
        configuration is what may have fixed its cause"""
        recorded = port_keys.load_text(session, port_keys.FAILED_TURN_ON)
        if recorded is None:
            return False
        if recorded == record:
            return True
        port_keys.save_text(session, port_keys.FAILED_TURN_ON, None)
        LOG.warning(
            "A failed per-port M_Key turn-on was recorded under another "
            "configuration: cleared, so that the turn-on may be armed again"
        )
        return False

    def _log_failed_turn_on(self):
        """At every start, say that a failed turn-on is recorded, and what
        to check"""
        if not self.group_named or self.config.driver != "managed":
            return
        try:
            record = self._failure_record()
        except PortKeysError:
            # No base stored yet: nothing can have failed.
            return
        with self._session() as session:
            failed = self._failed_turn_on(session, record)
            session.commit()
        if failed:
            LOG.error(
                "A per-port M_Key turn-on failed under this configuration and "
                "is recorded, so nothing arms it again. Check the log of the "
                "start that failed: whether OpenSM gave this host's port and "
                "every switch its derived key within [opensm] start_timeout, "
                "and that no port was down or held by another SM. Then change "
                "the configuration, or run --retry-port-key-turn-on, to arm it "
                "again"
            )

    # At every check

    def check(self):
        """Fold the running OpenSM's cache, verify, and judge: what the
        fabric check needs from per-port keys, or None to check as before

        On the applier's thread, after sync(), so that every Get here is
        taken after the last recorded sweep. Raises ReloadError if the
        prober fails, which the check counts as a failed query.
        """
        if not self.active() or not self.driver.running():
            return None
        m_key, base = self._m_keys()
        volume = self._read_cache()
        running_holds = {}
        if volume is not None:
            neighbors, entries = volume[0], volume[1]
            summary = self._fold(
                port_keys.fold_check, neighbors, entries, m_key, base, _now()
            )
            if summary != port_keys.FoldSummary():
                LOG.info("Folded OpenSM's cache: %s", summary)
            running_holds = port_keys.classify_entries(entries, m_key, base)

        with self._session() as session:
            ports, links = port_keys.load(session)
            if not ports:
                # The group is named but OpenSM's cache has listed nothing
                # yet: ibswitches as before.
                return None
            target = port_keys.load_running(session)
            expected = expected_guids(session)
            # The expected switches as recorded, each with its node GUID,
            # by which this check's own verify says whether any is missing
            expected_rows = [
                (
                    int(row.guid, 16),
                    None if row.node_guid is None else int(row.node_guid, 16),
                )
                for row in session.scalars(sa.select(db.ExpectedSwitch))
            ]
            recorded = port_keys.load_fingerprint(session)
            applied = session.get(db.Applied, 1)
            generation = (applied.generation or 0) if applied is not None else 0
            jobs = self._job_guids(session, ports)
            neutron = {
                guid.lower() for guid in session.scalars(sa.select(db.Port.guid))
            }
        sweep = self.manager._sweeps
        nothing = port_keys.nothing_derived(ports, recorded, running_holds)
        interval = self.config.port_verify_interval

        def verify(scope, ports, links):
            return self._verify(
                ports,
                links,
                target_on=target,
                nothing=nothing,
                scope=scope,
                helper=False,
                bound=interval,
            )

        began = time.monotonic()
        results = []
        # A full verify is due after an interval, and at every check while
        # expected switches are missing, so that the scan a partial check
        # places ports from reaches every adapter port, not only the
        # switches and the open jobs' ports (specs/ha.md, Where a port is).
        full = (
            self._last_full is None
            or began - self._last_full[1] >= interval
            or getattr(self.manager, "_missing_fabric", False)
        )
        if full:
            try:
                result = verify(prober.FULL, ports, links)
            except ReloadError as e:
                # The check uses the last full verify until it's older than
                # two intervals, and finds the switches without one.
                LOG.error("Can't verify every port's M_Key: %s", e)
                full = False
            else:
                self._last_full = (result, began)
                LOG.info("Verified every port's M_Key: %s", result)
        if not full:
            # The switches, and the rows no Get has yet shown to be one or
            # not, so that a switch new since the last full verify is found
            # and followed
            result = verify(
                {guid for guid, row in ports.items() if row.switch is not False},
                ports,
                links,
            )
        self._merge(result)
        if result.own_port_lost:
            # As ibswitches -y failing at this host's port: a failed query
            raise ReloadError(
                "this host's port answers neither the stored M_Key nor its derived key"
            )
        results.append(result)
        # A full verify Gets every port behind an up link, the jobs' too.
        if jobs and not full:
            with self._session() as session:
                ports, links = port_keys.load(session)
            scoped = verify(set(jobs), ports, links)
            self._merge(scoped)
            results.append(scoped)

        evidence = {}
        for each in results:
            for guid in jobs:
                if guid in each.keyed:
                    evidence[guid] = _Evidence(ANSWERED, sweep, generation)
                elif guid in each.link_down and guid not in evidence:
                    evidence[guid] = _Evidence(LINK_DOWN, sweep, generation)
        self._evidence = evidence

        with self._session() as session:
            ports, _links = port_keys.load(session)
        self._progress(ports, target)
        outcome = CheckOutcome(
            switches=set(result.switches),
            nodes={guid: result.ports[guid].node_guid for guid in result.switches},
            verify=result,
            complete=all(each.complete for each in results),
            results=results,
            full=full,
        )
        outcome.problems = self._problems(
            ports,
            neutron,
            results,
            expected,
            self._any_missing(expected_rows, result, outcome.nodes),
        )
        if not full and not outcome.complete:
            # A full verify's own rule says so below; the switches' or the
            # jobs' verify running out of time means ports it didn't reach
            # weren't judged.
            outcome.problems.append(
                "this check's verify of the switches or of the jobs' ports ran "
                "out of time (a fabric this size may need a longer [opensm] "
                "port_verify_interval)"
            )
        outcome.problems += self._hung_switches(ports, links, result)
        return outcome

    @staticmethod
    def _hung_switches(ports, links, result):
        """A switch row, expected or not, that didn't answer this check's
        verify although a recorded link to it is up: it may forward with
        stale pkey tables, so the check fails until it answers or is
        released (specs/zero-touch.md, Expected switches)"""
        hung = []
        for guid, row in ports.items():
            if row.switch is not True or row.released or guid in result.answered:
                continue
            for link in port_keys.paired(links):
                if link.guid != guid:
                    continue
                peer = (link.peer_guid, link.peer_port)
                if result.link_states.get(peer) is True or (
                    link.peer_guid == result.own_guid and result.own_link_up is True
                ):
                    hung.append(guid)
                    break
        if not hung:
            return []
        return [
            f"switches {_guids(hung)} don't answer, behind links that are up: "
            "they may still forward with stale partitions (no job completes "
            "until they answer, or are released with --release-port)"
        ]

    # After a check that passes (specs/zero-touch.md)

    def after_check(self):
        """At a fabric check that passes: arm the per-port M_Key turn-on if
        it's ready, and restart OpenSM to apply the target if it's due"""
        if self.config.driver != "managed" or not self.active():
            return
        if not self.driver.running():
            return
        self._arm_automatically()
        self._apply_if_due()

    def _note_arming(self, reason):
        """Log the arming's blocking reason once per change, and keep it for
        --port-key-states; "" when nothing blocks it"""
        if reason == self._arming_reason:
            return
        self._arming_reason = reason
        if reason:
            LOG.warning("Per-port M_Keys aren't armed: %s", reason)
        with self._session() as session:
            port_keys.save_text(session, port_keys.ARMING, reason[:_TEXT])
            session.commit()

    def _arm_automatically(self):
        """Arm the turn-on while the group is named, the target is off, and
        no failed turn-on is recorded, once the arming's checks pass on the
        last full verify since OpenSM started (Arming)"""
        if not self.group_named:
            self._note_arming("")
            return
        record = self._failure_record()
        with self._session() as session:
            armed = port_keys.load_target(session)
            failed = not armed and self._failed_turn_on(session, record)
            ports, links = port_keys.load(session)
            recorded = port_keys.load_fingerprint(session)
            session.commit()
        if armed:
            self._note_arming("")
            return
        if failed:
            self._note_arming(FAILED_REASON)
            return
        problems = self._epoch_problems()
        problems += self._readiness_problems(links, recorded)
        if links:
            full = self._last_full
            if full is None:
                problems.append("no full verify of the ports' M_Keys yet")
            else:
                problems += self._verify_problems(ports, full[0], shown=SHOWN)
        if problems:
            self._note_arming("; ".join(problems))
            return
        with self._session() as session:
            port_keys.save_target(session, True)
            port_keys.save_confirmed(session, False)
            port_keys.save_time(session, port_keys.TARGET_CHANGED, time.time())
            session.commit()
        self._note_arming("")
        LOG.warning(
            "Per-port M_Keys are armed: every recorded port behind an up link "
            "holds the stored M_Key, so OpenSM is restarted to turn them on "
            "once a check passes with no open job"
        )

    def _apply_if_due(self):
        """Restart OpenSM so that its start applies the target, when it
        differs from what OpenSM runs with, or is on and its success rule
        isn't met yet: at most once an hour, and with no open job until a
        day has passed since the target changed (Applying)"""
        with self._session() as session:
            target = port_keys.load_target(session) and self.group_named
            running = port_keys.load_running(session)
            confirmed = port_keys.load_confirmed(session)
            last = port_keys.load_time(session, port_keys.LAST_APPLY)
            changed = port_keys.load_time(session, port_keys.TARGET_CHANGED)
            # A deferred job waits for another object and isn't applied
            # until it stops waiting, so a restart can't affect it.
            jobs = session.scalar(
                sa.select(sa.func.count())
                .select_from(db.Job)
                .where(
                    db.Job.status.in_((db.JobStatus.PENDING, db.JobStatus.RUNNING)),
                    db.Job.id.not_in(sa.select(db.Deferred.job_id)),
                )
            )
            now = time.time()
            if target == running and not (target and not confirmed):
                return
            if changed is None:
                # Recorded by a release before it was kept: from now
                port_keys.save_time(session, port_keys.TARGET_CHANGED, now)
                changed = now
            if last is not None and 0 <= now - last < APPLY_EVERY:
                session.commit()
                return
            overdue = now - changed >= APPLY_ANYWAY
            session.commit()
            if jobs and not overdue:
                return
        if target != running:
            why = (
                f"the per-port M_Key target is {_on(target)}, and OpenSM runs "
                f"with them {_on(running)}: a start applies it"
            )
        else:
            why = (
                "per-port M_Keys haven't met their turn-on's success rule yet: "
                "a start retries it"
            )
        if not self.manager.may_apply():
            # Held by a freeze, which keeps OpenSM where it is: applied once
            # it ends, and the hour counted from then
            return
        if jobs:
            LOG.warning(
                "A day has passed since the per-port M_Key target changed, so it "
                "is applied although %d jobs are open",
                jobs,
            )
        # Before the restart, which in single node stops OpenSM at once
        with self._session() as session:
            port_keys.save_time(session, port_keys.LAST_APPLY, now)
            session.commit()
        self.manager.request_apply(why)

    @staticmethod
    def switch_gone(guid, links, result):
        """Whether this check's verify (result) shows every
        recorded link to a switch down, from the switch ports and this
        host's port at their other ends: the same evidence of a link down
        that a job's port gives (Lost ports and jobs)

        An adapter at the other end, other than this host's, is reached only
        through the switch, so it never shows the link down: a leaf switch
        stays expected, and its removal needs --confirm-fabric
        (specs/zero-touch.md, What stays manual). A switch with no recorded
        link can't be shown gone either. links are the recorded links, read
        in the caller's session. Never an older verify: a link that came
        back up since would be missed.
        """
        if result is None:
            return False
        ends = [link for link in port_keys.paired(links) if link.guid == guid]
        if not ends:
            return False
        for link in ends:
            peer = (link.peer_guid, link.peer_port)
            if result.link_states.get(peer) is False:
                continue
            if link.peer_guid == result.own_guid and result.own_link_up is False:
                continue
            return False
        return True

    def _job_guids(self, session, ports):
        """The recorded port GUIDs that open jobs name, deferred ones aside"""
        waiting = set(session.scalars(sa.select(db.Deferred.job_id)))
        guids = set()
        for job in session.scalars(
            sa.select(db.Job).where(
                db.Job.status.in_((db.JobStatus.PENDING, db.JobStatus.RUNNING))
            )
        ):
            if job.id in waiting:
                continue
            for text in effects.loads(job.effect).get("guids", []):
                with contextlib.suppress(ValueError):
                    guid = int(text, 16)
                    if guid in ports:
                        guids.add(guid)
        return guids

    @staticmethod
    def _excused(guid, result, expected, missing=True):
        """Whether a port the verify never sent a key to is behind only
        expected switches that are missing, or switches whose links are
        down, on its route: only while some expected switch is missing, so
        that this can make a check partial at most, never passing
        (specs/ha.md, A partial check)"""
        if not missing:
            return False
        blocking = result.unrouted.get(guid)
        return bool(blocking) and all(
            switch in expected or switch in result.link_down for switch in blocking
        )

    @staticmethod
    def _any_missing(expected_rows, result, nodes):
        """Whether this check's own verify misses some expected switch,
        each matched as the check matches it: by the GUID it was recorded
        by, or its node GUID, against the switches the verify found and
        their node GUIDs, so that a list recorded from ibswitches, before
        it's translated, counts the same. Never an older full verify's: one
        taken while a switch was missing would excuse ports after it's back
        (specs/ha.md, A partial check)."""
        found = set(result.switches) | {
            node for node in nodes.values() if node is not None
        }
        return any(
            guid not in found and (node is None or node not in found)
            for guid, node in expected_rows
        )

    def _problems(self, ports, neutron, results, expected=frozenset(), missing=False):
        """Why the fabric check fails, as far as per-port keys go"""
        problems = []
        if any(result.own_port_lost for result in results):
            problems.append(
                "this host's port answers neither the stored M_Key nor its derived key"
            )
        full = self._last_full
        if full is None:
            problems.append("no full verify of the ports' M_Keys yet")
            return problems
        result, began = full
        if time.monotonic() - began > 2 * self.config.port_verify_interval:
            problems.append(
                "the last full verify of the ports' M_Keys is older than two "
                "port_verify_intervals"
            )
        for switch, port in result.unrecorded:
            problems.append(
                f"switch port 0x{switch:016x} port {port} has its link up with no "
                "recorded neighbour (record what's attached with --add-link)"
            )
        if not result.complete:
            # Some switches' ports were never looked at, so the rules above
            # can't have covered them; and a port it never reached isn't
            # one that didn't answer.
            problems.append(
                "the last full verify of the ports' M_Keys ran out of time before "
                "reaching every port (a fabric this size may need a longer "
                "[opensm] port_verify_interval)"
            )
            return problems
        lost = sorted(
            row.guid
            for row in ports.values()
            if row.switch is not True
            and row.guid not in result.link_down
            # Behind an expected switch that's missing, or one whose link
            # is down, it was never sent a key: the missing switch fails the
            # check instead. Behind any other switch that didn't answer, it
            # still counts.
            and not self._excused(row.guid, result, expected, missing)
            and port_keys.guid_text(row.guid) not in neutron
            and not row.released
            and row.guid not in result.keyed
        )
        if lost:
            problems.append(
                f"ports that aren't Neutron ports' didn't answer their M_Keys: "
                f"{_guids(lost)} (no job completes until they're reached, or "
                "released with --release-port)"
            )
        return problems

    def _progress(self, ports, target):
        """Log how many ports still hold the other key when that changes,
        and once when none does"""
        count = sum(1 for row in port_keys.changing(ports, target) if row.reachable)
        if count == self._changing:
            return
        if count:
            LOG.warning(
                "Per-port M_Keys turning %s: %d reachable ports still hold %s",
                _on(target),
                count,
                "the stored M_Key" if target else "their derived keys",
            )
        elif self._changing:
            LOG.warning("Per-port M_Keys are %s on every reachable port", _on(target))
        self._changing = count

    def switches(self):
        """The switches the prober reaches, for --confirm-fabric, with their
        node GUIDs, or None to ask ibswitches as before: beside the running
        OpenSM, writing no rows

        A full verify, so that rows not yet known to be switches are found.
        """
        if not self.active():
            return None
        with self._session() as session:
            ports, links = port_keys.load(session)
            target = port_keys.load_running(session)
            recorded = port_keys.load_fingerprint(session)
        if not ports:
            return None
        result = self._verify(
            ports,
            links,
            target_on=target,
            nothing=port_keys.nothing_derived(ports, recorded, ()),
            scope=prober.FULL,
            helper=False,
            bound=self.config.port_verify_interval,
        )
        if result.own_port_lost:
            raise ReloadError("this host's port answers neither M_Key")
        if not result.complete:
            # Part of the list would replace the whole of it.
            raise ReloadError("the prober's discovery of the switches ran out of time")
        return {guid: result.ports[guid].node_guid for guid in result.switches}

    def reach_switches(self, bound, helper_name=None):
        """HA mode's reach probe: the expected switches this node's port
        reaches by the prober along the recorded links, with each port's
        key, within bound seconds; or None to ask ibswitches, as without
        rows

        It follows every switch, but counts only the expected ones, so that
        a new switch, or an adapter answering as one, never makes a
        candidate reach more (specs/zero-touch.md). Until any is recorded,
        as on a new deployment, it counts every switch, since otherwise no
        candidate would reach any, and none would be eligible. It writes no
        rows: a candidate isn't the leader. Rows a check interval behind
        the leader's are sent both keys where a verify would send them
        (specs/secured-profile.md, HA mode).
        """
        if self.config.driver != "managed":
            return None
        with self.manager._lock, self.manager.sessionmaker() as session:
            ports, links = port_keys.load(session)
            target = port_keys.load_running(session)
            recorded = port_keys.load_fingerprint(session)
            expected = expected_guids(session)
        if not ports:
            return None
        result = self._verify(
            ports,
            links,
            target_on=target,
            nothing=port_keys.nothing_derived(ports, recorded, ()),
            scope={guid for guid, row in ports.items() if row.switch is not False},
            helper=True,
            bound=bound,
            helper_name=helper_name,
        )
        if result.own_port_lost:
            return set()
        found = set(result.switches)
        if not expected:
            return found
        return {
            guid
            for guid in found
            if guid in expected or result.ports[guid].node_guid in expected
        }

    def expected_switches(self):
        """The expected switches' GUIDs, node GUIDs included, for HA
        mode's reach probe with ibswitches, which counts only these"""
        with self.manager._lock, self.manager.sessionmaker() as session:
            return expected_guids(session)

    # Sweeps and jobs, in the manager's session

    def sweep_errors(self, session, report):
        """What OpenSM's log during a sweep holds, beyond pkey errors: the
        port GUIDs whose jobs it holds, and whether it holds every job of the
        sweep (The log lines are an extra trigger to fail closed)

        With no rows, nothing."""
        if report is None or not self._rows_in(session):
            return set(), False
        unattributed = bool(report.dr_errors)
        if report.dr_errors:
            LOG.error(
                "OpenSM failed to send a directed-route SMP during the sweep, "
                "which names no port, so no job of the sweep completes"
            )
        neutron = {int(guid, 16) for guid in session.scalars(sa.select(db.Port.guid))}
        guids = {guid for guid in report.unreached if guid and guid in neutron}
        others = set(report.unreached) - guids
        if guids:
            LOG.error(
                "OpenSM couldn't reach Neutron ports %s during the sweep; their "
                "jobs wait",
                _guids(guids),
            )
        if others:
            unattributed = True
            LOG.error(
                "OpenSM couldn't reach %s during the sweep, which aren't Neutron "
                "ports', so no job of the sweep completes",
                _guids(others),
            )
        if report.bad_m_key_traps:
            LOG.warning(
                "OpenSM received %d Bad M_Key traps during the sweep",
                report.bad_m_key_traps,
            )
        return guids, unattributed

    def job_ready(self, session, job):
        """Whether a job, whose sweep has its effect, has its ports'
        evidence too: each recorded port it names answered its key, or
        showed its link down, at a check after the last sweep, or is
        released. A GUID with no row needs none (Lost ports and jobs).

        In HA mode only the leader has the evidence, from its checks:
        another instance never completes a job on a recorded port, and
        returns its stored status, which the leader writes."""
        if self.config.driver != "managed":
            return True
        names = effects.loads(job.effect).get("guids", [])
        if not names:
            return True
        if self.config.ha and not self.manager.term:
            return not session.scalar(
                sa.select(sa.func.count())
                .select_from(db.FabricPort)
                .where(
                    db.FabricPort.guid.in_(names),
                    db.FabricPort.released.is_(False),
                )
            )
        rows = {
            row.guid: row
            for row in session.scalars(
                sa.select(db.FabricPort).where(db.FabricPort.guid.in_(names))
            )
        }
        for text, row in sorted(rows.items()):
            guid = int(text, 16)
            if row.released:
                continue
            evidence = self._evidence.get(guid)
            if (
                evidence is not None
                and evidence.sweep == self.manager._sweeps
                and job.generation <= evidence.generation
            ):
                if guid in self._held:
                    self._held.discard(guid)
                    LOG.warning(
                        "Port 0x%016x %s; its jobs may complete", guid, evidence.kind
                    )
                continue
            job.error = (
                f"Waiting for port 0x{guid:016x} to answer its M_Key, or show "
                "its link down, after the sweep: OpenSM may not reach it. "
                "--release-port completes its jobs as they are"
            )
            if guid not in self._held:
                self._held.add(guid)
                LOG.error(
                    "Port 0x%016x hasn't answered its M_Key, nor shown its link "
                    "down, since the sweep, so its jobs are held",
                    guid,
                )
            return False
        return True

    # The operator's commands, beside the running manager

    def _epoch_problems(self):
        """In HA mode, why the turn-on can't be armed under the epoch: it
        must allow exactly this node's configuration's hash, since an epoch
        that also allows a configuration without the group would let leaders
        with and without it alternate, re-keying the fabric each time"""
        if not self.config.ha:
            return []
        from alternative_fabric_manager.ha import lease as lease_mod

        config_hash = self.manager.config_hash()
        with self._session() as session:
            epoch = lease_mod.read_epoch(session)
            session.commit()
        if epoch is not None and tuple(epoch[1]) == (config_hash,):
            return []
        return [
            "the configuration epoch doesn't allow exactly this node's "
            "configuration (see --config-epoch)"
        ]

    def _readiness_problems(self, links, recorded):
        """Why the turn-on can't be armed, beyond what the ports hold: the
        base stored by an earlier start, the ports keyed from it if any were,
        and links recorded (Turning it on)"""
        problems = []
        if not self.manager.base_kept():
            problems.append(
                "the running manager's start stored the per-port M_Key base: "
                "restart the manager after backing up the database, which then "
                "holds it"
            )
        _m_key, base = self._m_keys()
        if recorded is not None and recorded != port_keys.fingerprint(base):
            problems.append(
                "the stored per-port M_Key base isn't the one the ports were "
                "keyed from (its fingerprint differs)"
            )
        if not links:
            problems.append(
                "no links are recorded: OpenSM's cache hasn't listed the fabric "
                "yet, so OpenSM couldn't find the ports"
            )
        return problems

    @staticmethod
    def _verify_problems(ports, result, shown=None):
        """Why a verify doesn't show every recorded port behind an up link
        holding the stored M_Key, if it doesn't: it must be complete, and
        reach this host's port. shown limits how many GUIDs are named."""
        if result.own_port_lost:
            return ["this host's port answers neither M_Key"]
        problems = []
        if not result.complete:
            problems.append("the verify of the ports' M_Keys ran out of time")
        failing = sorted(
            guid
            for guid in ports
            if guid not in result.link_down
            and not (
                guid in result.keyed and result.ports[guid].holds == port_keys.M_KEY
            )
        )
        if failing:
            names = (
                _guids(failing)
                if shown is not None
                else ", ".join(f"0x{g:016x}" for g in failing)
            )
            problems.append(
                "every recorded port behind an up link must answer the stored "
                f"M_Key, with protection level 2, and these don't: {names}"
            )
        return problems

    def states(self):
        """--port-key-states: each recorded port, and the target; no key"""
        with self._session() as session:
            ports, _links = port_keys.load(session)
            target = port_keys.load_target(session)
            running = port_keys.load_running(session)
            failed = port_keys.load_text(session, port_keys.FAILED_TURN_ON)
            arming = port_keys.load_text(session, port_keys.ARMING)
        lines = [
            f"target {_on(target)}, OpenSM last started {_on(running)}",
            # Whether one is recorded, never its hash
            "failed turn-on recorded: "
            + (
                "yes (a change of configuration, or --retry-port-key-turn-on, "
                "clears it)"
                if failed is not None
                else "no"
            ),
            f"arming blocked by: {arming or '-'}",
        ]
        for guid in sorted(ports):
            row = ports[guid]
            kind = {True: "switch", False: "hca", None: "-"}[row.switch]
            flags = [
                name
                for name, on in (
                    ("lapsed", row.lapsed),
                    ("reachable", row.reachable),
                    ("released", row.released),
                )
                if on
            ]
            lines.append(f"0x{guid:016x} {kind} {row.holds} {','.join(flags) or '-'}")
        return lines

    def retry_turn_on(self):
        """--retry-port-key-turn-on: clear a recorded failed turn-on, so that
        the manager's checks arm it again; returns what to say. It writes
        only that record, so it runs from any node in HA mode, and the
        arming that follows is the leader's (specs/zero-touch.md, A failed
        turn-on)."""
        if not self.group_named:
            return f"The {GROUP} hardening group isn't named: nothing to retry."
        with self._session() as session:
            if port_keys.load_target(session):
                return (
                    "Per-port M_Keys are already on, or their turn-on is "
                    "armed: nothing to retry."
                )
            if port_keys.load_text(session, port_keys.FAILED_TURN_ON) is None:
                return "No failed per-port M_Key turn-on is recorded: nothing to retry."
            port_keys.save_text(session, port_keys.FAILED_TURN_ON, None)
            session.commit()
        LOG.warning("An operator cleared the failed per-port M_Key turn-on")
        return RETRIED

    def port_m_key(self, guid):
        """--port-m-key: the port's current key, for -y, under AGENTS.md's
        rule on keys"""
        with self._session() as session:
            ports, _links = port_keys.load(session)
        m_key, base = self._m_keys()
        if not ports:
            return port_keys.format_key(m_key)
        row = ports.get(guid)
        if row is None:
            raise PortKeysError(f"no port 0x{guid:016x} is recorded")
        if row.holds == port_keys.DERIVED:
            return port_keys.format_key(port_keys.port_m_key(base, guid))
        if row.holds == port_keys.M_KEY:
            return port_keys.format_key(m_key)
        raise PortKeysError(f"which key port 0x{guid:016x} holds isn't known")

    def add_link(self, local, peer):
        """--add-link: a link, both directions, and rows for its ports"""
        (guid, port), (peer_guid, peer_port) = local, peer
        if (guid, port) == (peer_guid, peer_port):
            raise PortKeysError("a link joins two different ports")
        with self._session() as session:
            ports, _links = port_keys.load(session)
            new = [
                port_keys.PortState(g)
                for g in sorted({guid, peer_guid})
                if g not in ports
            ]
            port_keys.upsert(
                session,
                new,
                [
                    port_keys.Link(guid, port, peer_guid, peer_port),
                    port_keys.Link(peer_guid, peer_port, guid, port),
                ],
            )
            session.commit()
        return len(new)

    def release_port(self, guid):
        """--release-port: complete a lost port's jobs as they are, and exempt
        it from the lost-port rule, until it answers its key again"""
        with self._session() as session:
            row = session.get(db.FabricPort, port_keys.guid_text(guid))
            if row is None:
                raise PortKeysError(f"no port 0x{guid:016x} is recorded")
            row.released = True
            session.commit()

    def forget_port_keys(self):
        """--forget-port-keys: drop every row and link, once they change
        nothing

        The base stays, and so does its fingerprint, so that a later
        turn-on still never sends a tenant's port the stored M_Key. A
        fingerprint of another base is dropped: the base the ports were
        keyed from is gone, as after restoring an older database, no
        recorded port holds a key derived from it any more, and while it
        stayed no start could turn per-port keys on again.
        """
        _m_key, base = self._m_keys()
        with self._session() as session:
            ports, links = port_keys.load(session)
            problem = port_keys.forget_problem(ports, self.group_named)
            if problem is not None:
                raise PortKeysError(f"can't forget the per-port M_Key rows: {problem}")
            session.execute(sa.delete(db.FabricLink))
            session.execute(sa.delete(db.FabricPort))
            recorded = port_keys.load_fingerprint(session)
            if recorded is not None and recorded != port_keys.fingerprint(base):
                session.execute(
                    sa.delete(db.State).where(
                        db.State.name == port_keys.BASE_FINGERPRINT
                    )
                )
            for save in (
                port_keys.save_target,
                port_keys.save_running,
                port_keys.save_confirmed,
            ):
                save(session, False)
            port_keys.save_text(session, port_keys.ARMING, None)
            session.commit()
        return len(ports), len(links)

    def forget_port(self, guid):
        """--forget-port: drop one port and its links, for removed hardware"""
        text = port_keys.guid_text(guid)
        with self._session() as session:
            row = session.get(db.FabricPort, text)
            links = list(
                session.scalars(
                    sa.select(db.FabricLink).where(
                        sa.or_(
                            db.FabricLink.guid == text, db.FabricLink.peer_guid == text
                        )
                    )
                )
            )
            if row is None and not links:
                raise PortKeysError(f"no port 0x{guid:016x} is recorded")
            seen = row.last_seen if row is not None else None
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=datetime.timezone.utc)
            if seen is not None and _now() - seen < FORGET_AFTER:
                raise PortKeysError(
                    f"port 0x{guid:016x} was seen in the last day: a present GUID "
                    "can't be forgotten"
                )
            for link in links:
                session.delete(link)
            if row is not None:
                session.delete(row)
            session.commit()
        return len(links)
