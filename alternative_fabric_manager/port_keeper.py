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

"""Per-port M_Keys in the single-node manager: before each start, at every
check, for each job, and the operator's commands

specs/secured-profile.md, Per-port M_Keys, The design, is the source of
truth. port_keys holds the rules and prober the Gets; this module decides
when each runs and what its result means for OpenSM, the fabric check and
the jobs. The Manager owns a PortKeeper and calls it from thin hooks.

It acts only while the m_key_per_port group is named or any fabric_ports or
fabric_links row exists ("active"), never in HA mode, which refuses the
group, and only with the managed driver, which writes OpenSM's cache volume
and runs the prober. Otherwise every hook returns at once, so that OpenSM,
its files, the fabric check and the jobs are as they were without it.

The rows have one writer in the manager, the applier's thread (and the
start, which runs before or on it), but the operator's commands run in
processes of their own beside it. So every write is a short transaction
that reads the rows afresh and changes only what its operation changed:
never a copy taken before a verify of many seconds, which would undo a link
--add-link recorded, or a --release-port, meanwhile.

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
    """What --confirm-fabric did: the switches it found, those of them it
    recorded as pending (found by the prober for the first time, so not
    trusted until a later run finds them again), and, with the group named,
    whether it armed the turn-on, a line to say, and why it didn't arm it"""

    switches: list = dataclasses.field(default_factory=list)
    pending: list = dataclasses.field(default_factory=list)
    armed: bool = False
    note: str | None = None
    problems: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CheckOutcome:
    """What the per-port M_Key part of a fabric check found: the switches
    the prober reached, for _judge, or None to ask ibswitches as before;
    and why the check fails, if it does"""

    switches: set | None = None
    problems: list = dataclasses.field(default_factory=list)


class PortKeeper:
    def __init__(self, manager):
        self.manager = manager
        self._driver_alarmed = False
        self._plan = None
        # A start whose success rule failed and whose prober then couldn't
        # tell which keys the ports hold: the next start runs that prober
        # again before OpenSM starts on either target
        self._unsettled = None
        self._reset_running()

    def _reset_running(self):
        """Forget what was learned from the OpenSM that ran last"""
        # The last full verify since OpenSM started: (result, monotonic
        # time it began)
        self._last_full = None
        # The confirmed switches the last full verify followed
        self._full_trusted = None
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
        if self.config.ha or not (self.group_named or rows):
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
        if self.config.ha:
            return False
        if self.group_named and self.config.driver == "managed":
            return True
        with self._session() as session:
            rows = self._has_rows(session)
        return self._usable(rows)

    def _rows_in(self, session):
        """Whether rows exist and the hooks that take a session act"""
        if self.config.ha or self.config.driver != "managed":
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

    def _trusted(self, session):
        """The switches an operator confirmed, which alone may be sent both
        keys, and routed through: not the pending ones"""
        return {
            int(row.guid, 16)
            for row in session.scalars(
                sa.select(db.ExpectedSwitch).where(db.ExpectedSwitch.pending.is_(False))
            )
        }

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
        follow_unconfirmed=False,
    ):
        """A verify within bound seconds, repeated while it learns that a
        port is a switch: routes go only through recorded switches, so rows
        that OpenSM's cache has just listed, whose NodeInfo no Get has seen
        yet, are reached one level of switches at a time

        Only the switches an operator confirmed are routed through, unless
        follow_unconfirmed, which --confirm-fabric alone passes.
        """
        with self._session() as session:
            trusted = self._trusted(session)
        m_key, base = self._m_keys()
        run = self.driver.prober(helper=helper)
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
                trusted_switches=trusted,
                follow_unconfirmed=follow_unconfirmed,
            )
            # Only a switch that the next round would route through helps.
            learned = {
                guid
                for guid in result.answered
                if guid in ports
                and ports[guid].switch is not True
                and result.ports[guid].switch is True
                and (follow_unconfirmed or guid in trusted)
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
        if self.config.ha or self.config.driver != "managed":
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
            message = (
                f"Per-port M_Keys: a start that turns them on failed ({problem}), "
                f"after OpenSM moved {len(moved)} ports ({_guids(moved)}) to "
                "their derived keys: the fabric is part-way, so OpenSM is "
                "started on the target again, from the verified rows, until it "
                "succeeds"
            )
            LOG.error("%s", message)
            raise ReloadError(message)
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
        # files that can't be read or parsed fail the start.
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
                    "them; --confirm-fabric arms per-port keys again"
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
            LOG.error(
                "The verify before this start %s, so OpenSM starts with per-port "
                "M_Keys %s, as it last ran, rather than beginning to turn them %s; "
                "the next start tries again",
                "failed" if result is None else "ran out of time",
                _on(running),
                _on(target),
            )

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
        self._plan = _Plan(
            target_on=target,
            effective_on=effective,
            confirm=effective and not confirmed,
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
        with self._session() as session:
            port_keys.save_target(session, False)
            port_keys.save_running(session, False)
            port_keys.save_confirmed(session, False)
            # The fingerprint stays: OpenSM ran with the option, and may have
            # keyed a port the prober can't see.
            session.commit()
        LOG.error(
            "Per-port M_Keys: %sno port left the key it held, so the target is "
            "set back to off and OpenSM is started without them; run "
            "--confirm-fabric again once the cause is fixed",
            f"a start that turns them on failed ({problem}); " if problem else "",
        )
        return set()

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
            recorded = port_keys.load_fingerprint(session)
            applied = session.get(db.Applied, 1)
            generation = (applied.generation or 0) if applied is not None else 0
            jobs = self._job_guids(session, ports)
            trusted = frozenset(self._trusted(session))
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
        # A full verify is due after an interval, and when the confirmed
        # switches have changed, since it followed only those.
        full = (
            self._last_full is None
            or began - self._last_full[1] >= interval
            or self._full_trusted != trusted
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
                self._full_trusted = trusted
                LOG.info("Verified every port's M_Key: %s", result)
        if not full:
            # The switches, and the rows no Get has yet shown to be one or
            # not, so that a switch confirmed since the last full verify is
            # found and followed
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
        outcome = CheckOutcome(switches=set(result.switches))
        outcome.problems = self._problems(ports, neutron, results)
        return outcome

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

    def _problems(self, ports, neutron, results):
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

    def uses_prober(self):
        """Whether --confirm-fabric finds the switches with the prober,
        which follows any row that answers as a switch, rather than with
        ibswitches"""
        if not self.active():
            return False
        with self._session() as session:
            return self._has_rows(session)

    def confirmed_switches(self):
        """The switches an operator confirmed, pending ones aside"""
        with self._session() as session:
            return self._trusted(session)

    def recorded_switches(self):
        """The switches the last --confirm-fabric recorded, pending ones
        included: one of these found again isn't pending any more"""
        with self._session() as session:
            return {
                int(row.guid, 16)
                for row in session.scalars(sa.select(db.ExpectedSwitch))
            }

    def switches(self):
        """The switches the prober reaches, for --confirm-fabric, or None
        to ask ibswitches as before: beside the running OpenSM, writing no
        rows

        It follows every row that answers as a switch, confirmed or not,
        so that new switches can be confirmed, and a full verify, so that
        rows not yet known to be switches are found; which keys each port
        is sent still follows the confirmed switches.
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
            follow_unconfirmed=True,
        )
        if result.own_port_lost:
            raise ReloadError("this host's port answers neither M_Key")
        if not result.complete:
            # Part of the list would replace the whole of it.
            raise ReloadError("the prober's discovery of the switches ran out of time")
        return set(result.switches)

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
        released. A GUID with no row needs none (Lost ports and jobs)."""
        if self.config.ha or self.config.driver != "managed":
            return True
        names = effects.loads(job.effect).get("guids", [])
        if not names:
            return True
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

    def _refuse_ha(self):
        if self.config.ha:
            raise PortKeysError("per-port M_Keys aren't available in HA mode")

    def fold_running(self):
        """--confirm-fabric: fold the links and ports the running OpenSM's
        cache lists into the rows, as a check does; returns why it couldn't,
        or None"""
        if not self.active():
            return None
        m_key, base = self._m_keys()
        try:
            files = self.driver.read_cache()
            now = _now()
            neighbors = port_keys.parse_neighbors(files.get("neighbors", ("",))[0])
            entries = port_keys.parse_guid2mkey(files.get("guid2mkey", ("",))[0])
        except (ReloadError, ValueError) as e:
            return f"can't read the running OpenSM's cache ({e})"
        summary = self._fold(port_keys.fold_check, neighbors, entries, m_key, base, now)
        LOG.info("Folded OpenSM's cache: %s", summary)
        return None

    def arm(self, confirmation, fold_problem=None):
        """--confirm-fabric, once the switches are confirmed: with the group
        named and the target off, record the target on if every
        precondition of the turn-on holds, or say which failed (Turning it
        on). The turn-on itself waits for the manager's next start."""
        if self.config.ha or not self.group_named:
            return
        if self.config.driver != "managed":
            return
        with self._session() as session:
            if port_keys.load_target(session):
                confirmation.note = (
                    "Per-port M_Keys are already on, or their turn-on is "
                    "recorded: nothing more to do"
                )
                return
            ports, links = port_keys.load(session)
            recorded = port_keys.load_fingerprint(session)
            confirmed = self._trusted(session)
        problems = confirmation.problems
        if fold_problem is not None:
            problems.append(fold_problem)
        if not self.manager.base_kept():
            problems.append(
                "the running manager's start stored the per-port M_Key base: "
                "restart the manager after backing up the database, which then "
                "holds it, and confirm the fabric again"
            )
        m_key, base = self._m_keys()
        if recorded is not None and recorded != port_keys.fingerprint(base):
            problems.append(
                "the stored per-port M_Key base isn't the one the ports were "
                "keyed from (its fingerprint differs)"
            )
        if confirmation.pending:
            problems.append(
                f"switches {_guids(confirmation.pending)} are pending: check the "
                "list above, and run --confirm-fabric again"
            )
        if not links:
            problems.append(
                "no links are recorded: OpenSM's cache hasn't listed the fabric "
                "yet, so OpenSM couldn't find the ports"
            )
        elif not confirmed:
            problems.append(
                "no switches are confirmed, so the prober can't reach the ports "
                "beyond this host's"
            )
        else:
            problems += self._turn_on_verify(ports, links, recorded, confirmed)
        if problems:
            return
        with self._session() as session:
            port_keys.save_target(session, True)
            port_keys.save_confirmed(session, False)
            session.commit()
        confirmation.armed = True
        confirmation.note = (
            "Per-port M_Keys are armed: the next restart of the manager turns them on"
        )

    def _turn_on_verify(self, ports, links, recorded, confirmed):
        """Why the prober, beside the running OpenSM, doesn't find every
        recorded port behind an up link holding the stored M_Key, if it
        doesn't"""
        try:
            result = self._verify(
                ports,
                links,
                target_on=False,
                nothing=port_keys.nothing_derived(ports, recorded, ()),
                scope=prober.FULL,
                helper=False,
                bound=self.config.port_verify_interval,
            )
        except ReloadError as e:
            return [f"can't verify the ports' M_Keys: {e}"]
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
            line = (
                "every recorded port behind an up link must answer the stored "
                "M_Key, with protection level 2"
            )
            unconfirmed = sorted(
                guid
                for guid, row in result.ports.items()
                if row.switch is True and guid not in confirmed
            )
            if unconfirmed:
                line += (
                    f"; switches {_guids(unconfirmed)} aren't confirmed, so "
                    "nothing beyond them was reached"
                )
            problems.append(
                line + ", and these don't: " + ", ".join(f"0x{g:016x}" for g in failing)
            )
        return problems

    def states(self):
        """--port-key-states: each recorded port, and the target; no key"""
        self._refuse_ha()
        with self._session() as session:
            ports, _links = port_keys.load(session)
            target = port_keys.load_target(session)
            running = port_keys.load_running(session)
        lines = [f"target {_on(target)}, OpenSM last started {_on(running)}"]
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

    def port_m_key(self, guid):
        """--port-m-key: the port's current key, for -y, under AGENTS.md's
        rule on keys"""
        self._refuse_ha()
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
        self._refuse_ha()
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
        self._refuse_ha()
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
        self._refuse_ha()
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
            session.commit()
        return len(ports), len(links)

    def forget_port(self, guid):
        """--forget-port: drop one port and its links, for removed hardware"""
        self._refuse_ha()
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
