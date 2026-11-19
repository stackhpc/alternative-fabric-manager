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

"""Leadership: who runs OpenSM, and when it may (see specs/ha.md)

Every instance runs a coordinator. A candidate watches the lease on its own
monotonic clock, and takes it once it has gone unrenewed for T. The leader
renews it, only while its loops make progress, and stops its own OpenSM if
it hasn't renewed for T_self. A new leader waits for every other target to
leave the target set before it starts OpenSM: released, verified stopped,
or, unconfirmed, after Ask and T_watchdog.

The coordinator drives OpenSM through a runner:
- write_lease(instance, term, deadline): the watchdog's lease, the deadline
  on CLOCK_MONOTONIC, or None while held;
- prepare(instance, term, deadline): remove any stop request, and set the
  start request's lease and deadline, for the manager's next OpenSM start,
  which the runner, as its driver, makes under the watchdog;
- stop(): ask the watchdog to stop OpenSM, killing it if it doesn't;
- verify_stopped(): whether no OpenSM runs on the node;
- ask(adapters): SMInfo DISABLE to a node's adapters, best effort;
- reach(), optionally: how many expected switches this node's own port
  reaches, or None if the port's link is down, below Init, or its
  neighbouring switch port is disabled (see Taking the lease);
- node_ports(), optionally: the port GUIDs of this host's physical
  InfiniBand ports, as a set of ints, which the node row records (see The
  secured profile).
"""

import contextlib
import enum
import logging
import threading
import time
import uuid

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager.drivers import ReloadError
from alternative_fabric_manager.ha import lease as lease_mod

LOG = logging.getLogger(__name__)


class Role(str, enum.Enum):
    STARTING = "starting"
    CANDIDATE = "candidate"
    TAKING_OVER = "taking over"
    LEADER = "leader"
    STOPPING = "stopping"
    STOPPED = "stopped"


def _now():
    return time.monotonic()


class Coordinator:
    def __init__(
        self,
        config,
        lease,
        runner,
        manager,
        config_hash="",
        adapters=(),
        hash_config=None,
    ):
        self.config = config
        self.lease = lease
        self.runner = runner
        self.manager = manager
        # This instance's configuration hash, worked out at registration by
        # hash_config(key), with the HMAC key from the database, if given
        # (see specs/ha.md, Configuration epochs). "" is always allowed, as
        # in tests.
        self.config_hash = config_hash
        self._hash_config = hash_config
        # The hashes the epoch allows, as last read; None while none is
        # stored
        self._allowed_hashes = None
        # The epoch last alarmed about, to alarm once per epoch
        self._epoch_alarmed = None
        # Whether the alarm for a live node of a release before epochs was
        # logged, until it's gone
        self._old_alarmed = False
        # The instance UUID whose node row has this host's ports recorded
        self._ports_recorded = None
        # Whether its [ha] settings match the cluster's, and whether a block
        # (see _blocked) held at the last watch
        self._settings_ok = True
        self._settings_alarmed = False
        self._blocked_now = False
        # Whether the reach thread runs, which alone probes reach
        self._reach_thread = False
        # Whether the takeover under way may run per-port M_Keys' steps,
        # which lengthen its bound
        self._port_keys_start = False
        # Whether the alarm for a node row without ports was logged
        self._ports_alarmed = False
        self.adapters = list(adapters)
        self.node = config.node
        self.uuid = str(uuid.uuid4())
        self.role = Role.STARTING
        # Whether another instance has registered on this node's row
        self._displaced = False
        self.term = None
        # When the last renewal that succeeded was sent
        self.t0 = None
        # Whether OpenSM may be running under this instance's lease
        self.opensm_started = False
        # Held by a freeze: no self-stop, a watchdog lease with no deadline
        self.held = False
        self.frozen = False
        # Expiry watching: the lease as last seen, and since when
        self._seen = None
        self._seen_since = None
        self._released_lease_taken = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._takeover = None
        self._takeover_started = None
        self._renewals_stopped = False
        # What the liveness thread may renew the watchdog's lease with: the
        # instance, term and t0 of the lease thread's last write with a
        # deadline, or None once it wrote a held lease or began stepping
        # down. The lock keeps a renewal from landing after that.
        self._grant = None
        self._grant_lock = threading.Lock()
        # Whether the runner may write a start request: from the takeover's
        # prepare until a step-down revokes the grant
        self._may_start = False
        self._opensm_gone = False
        self._restarting = False
        # Other nodes' heartbeats as last seen, and since when, to tell live
        # candidates from dead rows
        self._heartbeats = {}
        self._expired_since = None
        # This node's reach, refreshed on a thread of its own, since a scan
        # of a large fabric mustn't hold up renewals
        self._reach_value = None
        self._moved_age = 0.0
        # When a database call last succeeded on a Primary component, for
        # /healthz
        self._database_seen = None
        manager.on_opensm_stopped = self._opensm_stopped

    # Lifecycle

    def healthy(self):
        """Whether HAProxy should send this instance requests, and why not:
        not while it stops, nor while it can't reach a Primary component,
        as seen by its heartbeat every period (see Galera specifics). A
        lease thread held up, as by a step-down waiting for the applier, or
        a leader whose stop can't be verified, also skips heartbeats, and so
        leaves rotation while the others serve."""
        if self.role in (Role.STOPPING, Role.STOPPED):
            return False, "Stopping"
        seen = self._database_seen
        window = 2 * self.config.ha_period + self.config.call_timeout
        if seen is None or _now() - seen > window:
            return False, "Can't reach a Primary component of the database"
        return True, ""

    def start(self):
        if getattr(self.runner, "reach", None) is not None:
            self._reach_thread = True
            threading.Thread(
                target=self._refresh_reach, name="reach", daemon=True
            ).start()
        self._rejoin()
        self._thread = threading.Thread(target=self._run, name="lease", daemon=True)
        self._thread.start()
        threading.Thread(target=self._liveness, name="liveness", daemon=True).start()

    def _rejoin(self):
        """Register with a new instance UUID, and make sure no OpenSM of ours
        runs, or adopt a frozen lease on this node (see Rejoining, Freeze)"""
        while not self._stop.is_set():
            try:
                if self._hash_config is not None and not self.config_hash:
                    # Once: the configuration this process runs with, not
                    # one edited on disk since
                    self.config_hash = self._hash_config(self.lease.hash_key())
                self.lease.register(
                    self.node, self.uuid, self.config_hash, self.adapters
                )
                self._database_seen = _now()
                # Settings that differ keep it from being a candidate, not
                # from registering: its heartbeat must count, so that the
                # epoch, with the settings, can move once every node has
                # them (see _blocked).
                self._settings_ok = self.lease.agree_settings(
                    config_mod.ha_settings(self.config)
                )
                state = self.lease.read()
                if state.freeze_state in ("frozen", "ending") and (
                    state.holder_node == self.node
                ):
                    term = self.lease.adopt_frozen(self.node, self.uuid, state.term)
                    if term is not None:
                        LOG.warning("Adopting the frozen lease of term %d", term)
                        # Whatever its hash, since a freeze keeps OpenSM
                        # where it is; it hands over once the freeze ends.
                        self._allowed_hashes = state.allowed_hashes
                        if not self._config_allowed():
                            self._alarm_config("leading while frozen")
                        self._become_leader(term, held=True)
                        self._start_opensm()
                        return
                self.runner.stop()
                if not self.runner.verify_stopped():
                    LOG.error("An OpenSM still runs on this node; not a candidate")
                    time.sleep(self.config.ha_period)
                    continue
                if self.lease.record_verified_stop(self.node, self.uuid) is None:
                    time.sleep(self.config.ha_period)
                    continue
                self.role = Role.CANDIDATE
                return
            except lease_mod.NotPrimary as e:
                LOG.warning("Waiting for a Primary component: %s", e)
            except Exception as e:  # noqa: BLE001 - the database may be down
                LOG.warning("Can't register yet: %s", e)
            time.sleep(self.config.ha_period)

    def shutdown(self):
        """At SIGTERM: step down within shutdown_timeout, then stop"""
        deadline = _now() + self.config.shutdown_timeout
        previous = self.role
        # /healthz fails from now, so that HAProxy stops sending requests.
        self.role = Role.STOPPING
        self._stop.set()
        with self._lock:
            instance = self.uuid
            if previous in (Role.LEADER, Role.TAKING_OVER) and not self.frozen:
                self._step_down("shutting down", deadline=deadline)
            else:
                self._revoke_grant()
                self.runner.stop()
                self.manager.halt()
            try:
                self.lease.mark_stopped(self.node, instance)
            except Exception as e:  # noqa: BLE001
                LOG.warning("Can't mark this node stopped: %s", e)
            self.role = Role.STOPPED
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(self.config.ha_period * 2 + self.config.call_timeout)

    # The lease thread

    def _run(self):
        while not self._stop.is_set():
            started = _now()
            try:
                with self._lock:
                    self._tick()
            except Exception:
                LOG.exception("Lease loop failed")
            self._stop.wait(max(0.0, self.config.ha_period - (_now() - started)))

    def _tick(self):
        # Before any database call, which may block for its timeout
        if self.role in (Role.LEADER, Role.TAKING_OVER) and self._self_stop_due():
            return
        try:
            reach = (
                0
                if self.role == Role.CANDIDATE and self._blocked_now
                else self._reach()
            )
            beat = self.lease.heartbeat(self.node, self.uuid, reach)
            self._database_seen = _now()
            if beat:
                self._displaced = False
            elif self.role in (Role.CANDIDATE, Role.TAKING_OVER, Role.LEADER):
                # Not while starting, when _rejoin hasn't yet registered a
                # new UUID, nor once stopped, which mustn't look live again
                self._restore_row()
        except Exception as e:  # noqa: BLE001
            LOG.debug("Heartbeat failed: %s", e)
        if self.role in (Role.LEADER, Role.TAKING_OVER):
            self._lead()
        elif self.role == Role.CANDIDATE:
            self._watch()

    def _restore_row(self):
        """A heartbeat matched no row: put this instance's row back if it is
        missing, so that it is never live unseen by the others, but never
        take the name back from another instance (see One instance per
        name)"""
        if self.lease.restore(self.node, self.uuid, self.config_hash, self.adapters):
            LOG.warning("This node's row was missing; restored it")
            self._displaced = False
        elif not self._displaced:
            # Once, rather than at every tick
            LOG.error("Another instance has registered on node %s's row", self.node)
            self._displaced = True

    def _probe_reach(self):
        reach = getattr(self.runner, "reach", None)
        if reach is None:
            return 1
        try:
            return reach() or 0
        except Exception as e:  # noqa: BLE001
            LOG.warning("Can't tell what this node reaches: %s", e)
            return 0

    def _refresh_reach(self):
        while not self._stop.is_set():
            self._reach_value = self._probe_reach()
            self._record_ports()
            self._stop.wait(max(self.config.ha_period * 4, 2.0))

    def _record_ports(self):
        """Record this host's physical ports in the node row, once per
        registered instance: at registration, then at every refresh until a
        probe finds some (see The secured profile). A probe that fails, or
        finds none, changes nothing."""
        probe = getattr(self.runner, "node_ports", None)
        uuid_ = self.uuid
        if probe is None or self._ports_recorded == uuid_:
            return
        if self.role == Role.STARTING:
            # Not registered yet under this UUID
            return
        try:
            guids = probe()
        except Exception as e:  # noqa: BLE001 - such as an image being pulled
            LOG.info("Can't read this host's physical ports yet: %s", e)
            return
        if not guids:
            return
        try:
            if self.lease.record_ports(self.node, uuid_, guids):
                self._ports_recorded = uuid_
                LOG.info(
                    "Recorded this host's physical ports: %s",
                    ", ".join(f"0x{guid:016x}" for guid in sorted(guids)),
                )
        except Exception as e:  # noqa: BLE001 - try again at the next refresh
            LOG.warning("Can't record this host's physical ports: %s", e)

    def _reach(self):
        """Expected switches this node reaches; 0 if its own port is down"""
        if self._reach_value is None:
            if self._reach_thread:
                # Not refreshed yet: probing here too would race the reach
                # thread's probe for the same helper container.
                return 0
            # No thread, as in tests
            return self._probe_reach()
        return self._reach_value

    def _live_rows(self):
        """Node rows not marked stopped whose heartbeat moved within T, this
        node's and the leader's included, as (name, instance, reach,
        configuration hash)"""
        now = _now()
        live = []
        for (
            name,
            instance,
            heartbeat,
            reach,
            stopped,
            config_hash,
        ) in self.lease.nodes():
            seen = self._heartbeats.get(name)
            if seen is None or seen[0] != heartbeat:
                self._heartbeats[name] = (heartbeat, now)
                seen = self._heartbeats[name]
            if not stopped and now - seen[1] < self.config.lease_timeout:
                live.append((name, instance, reach, config_hash))
        return live

    def _live_others(self):
        """Other nodes whose heartbeat moved within T, not stopped, not in
        the target set, and whose configuration the epoch allows: the live
        candidates (see Stepping down)"""
        targets = {instance for _node, instance, _term in self.lease.targets()}
        return [
            (name, reach)
            for name, instance, reach, config_hash in self._live_rows()
            if name != self.node
            and instance not in targets
            and self._allows(config_hash)
        ]

    # Configuration epochs

    def _allows(self, config_hash):
        """Whether the epoch, as last read, allows a hash"""
        allowed = self._allowed_hashes
        return allowed is None or config_hash in allowed

    def _config_allowed(self):
        """Whether the epoch, as last read, allows this instance's
        configuration; one with no hash, as in tests, always is"""
        return not self.config_hash or self._allows(self.config_hash)

    def _move_epoch(self):
        """Record an epoch with only this configuration's hash once every
        live node row has it, the leader's included: a rolling change then
        needs no command (see Configuration epochs). Returns whether it
        did."""
        if not self.config_hash:
            return False
        rows = self._live_rows()
        if not rows or any(h != self.config_hash for *_rest, h in rows):
            return False
        seen = self.lease.epoch()
        if seen is not None and self.config_hash in seen[1]:
            return False
        number = self.lease.allow_config(
            [self.config_hash],
            seen=seen,
            settings=config_mod.ha_settings(self.config),
        )
        if number is None:
            return False
        LOG.warning(
            "Every live node runs configuration %s: recorded configuration "
            "epoch %d, allowing only it",
            self.config_hash[:12],
            number,
        )
        self._allowed_hashes = (self.config_hash,)
        return True

    def _old_versions_live(self):
        """Whether a live node row has no configuration hash: a release
        before epochs, which never checks one"""
        return any(not h for *_rest, h in self._live_rows())

    def _alarm_config(self, why):
        """Log, once per epoch, that this instance's configuration isn't
        allowed"""
        key = self._allowed_hashes
        if key == self._epoch_alarmed:
            return
        self._epoch_alarmed = key
        LOG.error(
            "This node's configuration (%s) isn't one the configuration epoch "
            "allows (%s): %s. See --config-epoch and --allow-config",
            self.config_hash[:12],
            ", ".join(h[:12] for h in key or ()),
            why,
        )

    # Leading

    def _loops_alive(self, log=True):
        """The liveness condition: no loop iteration over its bound"""
        if self.manager.iteration_age() > self.apply_bound:
            if log:
                LOG.error("The apply loop is past its bound")
            return False
        takeover_started = self._takeover_started
        if (
            takeover_started is not None
            and _now() - takeover_started > self.takeover_bound
        ):
            if log:
                LOG.error("The takeover is past its bound")
            return False
        return True

    # The watchdog's lease

    def _write_watchdog_lease(self, deadline):
        """Write the watchdog's lease, held if no deadline, and record what
        the liveness thread may renew it with"""
        with self._grant_lock:
            self.runner.write_lease(self.uuid, self.term, deadline)
            self._grant = None if deadline is None else (self.uuid, self.term, self.t0)

    def _revoke_grant(self):
        with self._grant_lock:
            self._grant = None
            # A step-down revokes it before it asks the watchdog to stop, so
            # no start request is written after that stop request.
            self._may_start = False

    def _allow_start(self):
        with self._grant_lock:
            self._may_start = True

    @contextlib.contextmanager
    def start_gate(self):
        """For the runner, as it writes a start request: the request's
        deadline, worked out now, held for the write; raises ReloadError if
        this instance may no longer start OpenSM. With per-port M_Keys the
        request is written after the verify before the start, which may
        take longer than T_self, and again after a turn-on that failed
        (specs/secured-profile.md, HA mode)."""
        with self._grant_lock:
            if not self._may_start:
                raise ReloadError("This instance no longer leads: not starting OpenSM")
            if self.held:
                yield None
                return
            if self.t0 is None or _now() - self.t0 >= self.config.self_stop_timeout:
                raise ReloadError("The last renewal is too old to start OpenSM")
            yield self.t0 + self.config.self_stop_timeout

    def _liveness(self):
        """Renew the watchdog's lease every period while the loops make
        progress, with no database call in the way, so that a Galera stall
        shorter than T_self, during which the lease thread's own calls
        block, doesn't let it lapse. It stops once no renewal has succeeded
        for T_self, and the deadline stays bounded by t0 + T_self +
        T_watchdog (see The watchdog)."""
        while not self._stop.wait(self.config.ha_period):
            try:
                self._renew_watchdog()
            except Exception:  # noqa: BLE001 - try again next period
                LOG.exception("Renewing the watchdog's lease failed")

    def _renew_watchdog(self):
        with self._grant_lock:
            if self._grant is None or self.held:
                return
            instance, term, t0 = self._grant
            now = _now()
            if t0 is None or now - t0 >= self.config.self_stop_timeout:
                return
            if not self._loops_alive(log=False):
                return
            self.runner.write_lease(
                instance,
                term,
                min(
                    now + self.config.watchdog_timeout,
                    t0 + self.config.self_stop_timeout + self.config.watchdog_timeout,
                ),
            )

    @property
    def apply_bound(self):
        # One sweep, a fabric check, and five database calls, plus margin
        return self.config.sweep_timeout + 5 + 5 * self.config.call_timeout + 10

    @property
    def takeover_bound(self):
        # Each takeover step polls; the longest is OpenSM's start. With
        # per-port M_Keys, the longest start is a turn-on whose success rule
        # fails: it verifies, starts, waits, stops, verifies again and starts
        # once more (specs/secured-profile.md, HA mode).
        return (
            self.config.start_timeout * (5 if self._port_keys_start else 1)
            + 5 * self.config.call_timeout
            + 10
        )

    def _port_keys_active(self):
        """Whether this takeover's start may run per-port M_Keys' steps"""
        if "m_key_per_port" in self.config.hardening_groups:
            return True
        keeper = getattr(self.manager, "port_keeper", None)
        try:
            return bool(keeper is not None and keeper.active())
        except Exception:  # noqa: BLE001 - assume the longer bound
            return True

    def _watchdog_deadline(self, now):
        return min(
            now + self.config.watchdog_timeout,
            self.t0 + self.config.self_stop_timeout + self.config.watchdog_timeout,
        )

    def _self_stop_due(self):
        """Self-stop if no renewal has succeeded for T_self; return whether
        it did"""
        now = _now()
        if (
            not self.held
            and self.t0 is not None
            and now - self.t0 > self.config.self_stop_timeout
        ):
            LOG.error(
                "No renewal for %.1f s: stopping OpenSM (self-stop)", now - self.t0
            )
            self._step_down("self-stop", release=False)
            return True
        return False

    def _lead(self):
        now = _now()
        if self._self_stop_due():
            return
        if not self._loops_alive():
            if not self.held:
                self._step_down("a loop overran its bound")
            return
        if self._opensm_gone and not self.held:
            self._opensm_gone = False
            self._step_down("OpenSM stopped")
            return
        why = self._discretionary_trigger()
        if why:
            self._step_down(why)
            return
        if self._renewals_stopped:
            return
        try:
            renewed = self.lease.renew(self.uuid, self.term)
        except Exception as e:  # noqa: BLE001 - not Primary, or unreachable
            LOG.warning("Renewal failed: %s", e)
            renewed = None
        if renewed is False:
            LOG.error("Lost the lease of term %s", self.term)
            self._step_down("lost the lease", release=False)
            return
        if renewed:
            self.t0 = now
        # The watchdog's lease follows the liveness condition, not each
        # renewal: a Galera stall shorter than T_self, such as while the
        # survivors evict a lost node, mustn't kill OpenSM. Its deadline
        # stays bounded by t0 + T_self + T_watchdog, and self-stop ends it
        # at T_self (see The watchdog).
        if self.opensm_started and self.t0 is not None:
            # A fresh reading, since now was read before this tick's database
            # calls and would shorten the liveness thread's deadline; and the
            # T_self check against the same reading, as the liveness thread
            # makes it, so that a manager paused past T_self writes nothing
            # (see The watchdog).
            fresh = _now()
            if self.held:
                self._write_watchdog_lease(None)
            elif fresh - self.t0 < self.config.self_stop_timeout:
                self._write_watchdog_lease(self._watchdog_deadline(fresh))
        if renewed is None:
            return
        if self._opensm_gone and self.held:
            self._restart_frozen()
        self._freeze_step()

    def _discretionary_trigger(self):
        """Why an established leader should hand over, if a live candidate
        could do better and the cooldown has passed (see Stepping down)"""
        if self.held or self.role != Role.LEADER:
            return None
        if not self._config_allowed():
            # A configuration no longer allowed: hand over to a live
            # candidate whose configuration is, after the cooldown (see
            # Configuration epochs)
            if self._moved_age >= self.config.step_down_cooldown:
                try:
                    others = self._live_others()
                except Exception:  # noqa: BLE001
                    others = []
                # Only for a candidate that can take over: one whose port
                # reaches nothing never would, and the fabric would have no SM.
                if any(reach > 0 for _name, reach in others):
                    return "its configuration is no longer allowed"
            self._alarm_config("leading until a candidate whose is allowed is live")
        manager = self.manager
        stalled = manager.unanswered_checks >= 3 or manager.failed_applies >= 3
        missing = getattr(manager, "missing_checks", 0) >= 3
        if not (stalled or missing):
            return None
        # Unanswered checks mean no working SM: they don't wait for the
        # cooldown, which is for faults every node may share.
        unanswered = manager.unanswered_checks >= 3
        if not unanswered and self._moved_age < self.config.step_down_cooldown:
            return None
        try:
            others = self._live_others()
        except Exception:  # noqa: BLE001
            return None
        if stalled and others:
            return "OpenSM doesn't answer, or can't be applied to"
        if missing and any(reach > self._reach() for _name, reach in others):
            return "expected switches are missing, and another node reaches more"
        return None

    def _restart_frozen(self):
        """Start OpenSM again as the frozen node, only once a read from a
        Primary component shows this instance still holds the frozen lease:
        a confirmed takeover may have happened while this manager was
        paused, and the held watchdog lease can't know it"""
        try:
            state = self.lease.read()
        except Exception as e:  # noqa: BLE001
            LOG.warning("Can't confirm the freeze to restart OpenSM: %s", e)
            return
        if (
            state.holder_uuid != self.uuid
            or state.term != self.term
            or state.freeze_state not in ("frozen", "ending")
            or self._restarting
        ):
            return
        self._opensm_gone = False
        self._restarting = True
        LOG.error("OpenSM stopped while frozen: starting it again here")
        # The applier stops while OpenSM starts, so that it neither reports
        # OpenSM gone again nor races the start; start() restarts it.
        self.manager.halt()
        # The held lease again first: a failed start removes the watchdog's
        # files.
        self._write_watchdog_lease(None)
        self.runner.prepare(self.uuid, self.term, None)
        self._allow_start()

        def restart():
            try:
                self.manager.start()
            except Exception:
                LOG.exception("Restarting OpenSM while frozen failed; retrying")
                # Only the frozen node may start it, so keep trying.
                self._opensm_gone = True
            finally:
                self._restarting = False

        threading.Thread(target=restart, name="restart", daemon=True).start()

    def _freeze_step(self):
        try:
            state = self.lease.read()
        except Exception as e:  # noqa: BLE001
            LOG.warning("Can't read the lease: %s", e)
            return
        self.frozen = state.freeze_state in ("frozen", "ending")
        self._moved_age = state.moved_age
        self._allowed_hashes = state.allowed_hashes
        ours = state.holder_uuid == self.uuid and state.term == self.term
        if not ours:
            return
        if state.freeze_state == "requested" and not self.held:
            # Hold only once a read shows the freeze committed, below.
            self.lease.acknowledge_freeze(self.uuid, self.term)
            state = self.lease.read()
        if state.freeze_state == "frozen" and not self.held:
            # Whoever's acknowledgement committed it, and whether or not ours
            # read back at the time, a committed freeze of our lease holds.
            LOG.warning("Frozen: holding OpenSM on this node")
            self.held = True
            self.frozen = True
            if self.opensm_started:
                self._write_watchdog_lease(None)
        elif state.freeze_state == "ending":
            # In this order, and again until it commits: a deadline for the
            # watchdog, self-stop timing, a renewal, then the acknowledgement
            # (see Freeze).
            now = _now()
            if self.opensm_started:
                self._write_watchdog_lease(self._watchdog_deadline(now))
            self.held = False
            if self.lease.renew(self.uuid, self.term):
                self.t0 = now
                if self.lease.acknowledge_unfreeze(self.uuid, self.term):
                    LOG.warning("Unfrozen")
                    self.frozen = False

    def _become_leader(self, term, held=False):
        self.term = term
        self.manager.term = term
        self.t0 = _now()
        self.held = held
        self.frozen = held
        self.role = Role.LEADER if held else Role.TAKING_OVER
        self._renewals_stopped = False
        self._opensm_gone = False

    # Watching

    def _watch(self):
        try:
            state = self.lease.read()
        except Exception as e:  # noqa: BLE001
            # A failed read resets the timer (see The lease, Expiry).
            LOG.debug("Can't read the lease: %s", e)
            self._seen = None
            self._seen_since = None
            return
        returned = _now()
        self.frozen = state.freeze_state in ("frozen", "ending")
        self._allowed_hashes = state.allowed_hashes
        if not self._config_allowed() or not self._settings_ok:
            # At every watch, not only once the lease expires, so that the
            # heartbeats it judges liveness by are watched for T first
            try:
                if self._move_epoch():
                    self._settings_ok = False
            except Exception as e:  # noqa: BLE001
                LOG.debug("Can't move the configuration epoch: %s", e)
        try:
            self._blocked_now = self._blocked()
        except Exception as e:  # noqa: BLE001
            LOG.debug("Can't tell whether this node may be a candidate: %s", e)
            self._blocked_now = True
        key = (state.term, state.counter, state.released)
        if key != self._seen:
            self._seen = key
            self._seen_since = returned
        expired = state.released or (
            self._seen_since is not None
            and returned - self._seen_since >= self.config.lease_timeout
        )
        if not expired:
            self._expired_since = None
            return
        if self._expired_since is None:
            self._expired_since = returned
        if self._blocked_now:
            return
        if not self._eligible(returned - self._expired_since):
            return
        try:
            term = self.lease.acquire(
                self.node,
                self.uuid,
                state.term,
                state.counter,
                state.released,
                config_hash=self.config_hash or None,
            )
        except Exception as e:  # noqa: BLE001
            # The outcome is unknown, so it counts as taken: this instance may
            # be in the target set. Make sure no OpenSM of ours runs, and come
            # back as a new instance, whose verified stop covers the old one.
            LOG.warning("Taking the lease had an unknown outcome: %s", e)
            self._abandon()
            return
        if term is None:
            return
        LOG.warning(
            "Took the lease, term %d, after it %s",
            term,
            "was released" if state.released else "expired",
        )
        self._become_leader(term)
        self._takeover_started = _now()
        self._takeover = threading.Thread(
            target=self._take_over,
            args=(term, _now()),
            name="takeover",
            daemon=True,
        )
        self._takeover.start()

    def _eligible(self, waited):
        """Whether this node may take the lease: its own port is up, and it
        reaches every expected switch, or, once no candidate has for
        T_reach, as many as any live candidate (see Taking the lease)"""
        reach = self._reach()
        if reach <= 0:
            return False
        try:
            expected = self.lease.expected_switches()
            others = self._live_others()
        except Exception:  # noqa: BLE001
            return False
        if expected and reach >= expected:
            return True
        if waited < self.config.reach_timeout:
            return False
        return reach >= max((r for _name, r in others), default=0)

    def _blocked(self):
        """Whether this node may not be a candidate whatever it reaches: its
        configuration or [ha] settings aren't the cluster's, a release
        without epochs is live while it names experimental groups, or,
        with allowed_sm, its row has no port GUIDs. Each is logged once.
        While blocked, it heartbeats a reach of 0, so that no leader steps
        down for it (see Configuration epochs, The secured profile)."""
        if not self._config_allowed():
            self._alarm_config("not a candidate")
            return True
        if not self._settings_ok:
            self._settings_ok = self.lease.agree_settings(
                config_mod.ha_settings(self.config)
            )
            if not self._settings_ok:
                if not self._settings_alarmed:
                    LOG.error(
                        "This instance's [ha] settings differ from the cluster's: "
                        "not a candidate until every live node has them, and "
                        "the configuration epoch moves"
                    )
                    self._settings_alarmed = True
                return True
            self._settings_alarmed = False
        if self._experimental() and self._old_versions_live():
            # A release before epochs could lead next without the groups,
            # and undo them (see Configuration epochs).
            if not self._old_alarmed:
                LOG.error(
                    "This node's configuration names experimental hardening "
                    "groups (%s), but a live node runs a release without "
                    "configuration epochs, which could lead without them: not "
                    "a candidate until every node is upgraded",
                    ", ".join(sorted(self._experimental())),
                )
                self._old_alarmed = True
            return True
        self._old_alarmed = False
        if self.config.allowed_sm_in_effect:
            if not self.lease.node_ports().get(self.node):
                # The leader's allowed_sm_guids would leave this node out.
                if not self._ports_alarmed:
                    LOG.error(
                        "allowed_sm_guids is in effect, but this node's row has "
                        "no port GUIDs yet: not a candidate until the port "
                        "probe records them"
                    )
                    self._ports_alarmed = True
                return True
            self._ports_alarmed = False
        return False

    def _experimental(self):
        """The experimental hardening groups this configuration names"""
        return set(self.config.hardening_groups) & set(
            config_mod.EXPERIMENTAL_HARDENING_GROUPS
        )

    # Taking over

    def _take_over(self, term, taken_at):
        """Wait for every other target to leave the set, then start OpenSM"""
        # Read once, on this thread: the liveness thread mustn't wait on the
        # database for it.
        self._port_keys_start = self._port_keys_active()
        try:
            asked = set()
            while not self._stop.is_set():
                with self._lock:
                    if self.term != term or self.role != Role.TAKING_OVER:
                        return
                try:
                    if not self._take_over_step(term, taken_at, asked):
                        break
                except Exception as e:  # noqa: BLE001
                    # A database blip: self-stop bounds safety, so retry
                    # rather than give the lease up and start again.
                    LOG.warning("Takeover step failed, retrying: %s", e)
                self._takeover_started = _now()
                self._stop.wait(self.config.ha_period)
            with self._lock:
                if self.term != term or self.role != Role.TAKING_OVER:
                    return
                if self.t0 is None or _now() - self.t0 > self.config.self_stop_timeout:
                    LOG.error("The last renewal is too old to start OpenSM")
                    return
                self._prepare_start()
            # Outside the lock: the lease thread keeps renewing meanwhile.
            self.manager.start()
            with self._lock:
                if self.term == term and self.role == Role.TAKING_OVER:
                    self.role = Role.LEADER
                    LOG.warning("Leading, term %d", term)
        except Exception:
            LOG.exception("Takeover failed")
            with self._lock:
                if self.term == term:
                    self._step_down("the takeover failed")
        finally:
            self._takeover_started = None

    def _take_over_step(self, term, taken_at, asked):
        """One pass over the other targets; return whether any remain"""
        others = [entry for entry in self.lease.targets() if entry[1] != self.uuid]
        if not others:
            return False
        for node, instance, _entry_term in others:
            if self.lease.remove_target(self.uuid, term, node, instance, "verified"):
                LOG.info("%s's OpenSM is verified stopped", node)
                continue
            if self.config.fence_ask and (node, instance) not in asked:
                asked.add((node, instance))
                self._ask(node)
            if _now() - taken_at < self.config.watchdog_timeout:
                continue
            if self.lease.remove_target(self.uuid, term, node, instance, "unconfirmed"):
                LOG.warning("%s leaves the target set unconfirmed", node)
        return True

    def _ask(self, node):
        try:
            self.runner.ask(node)
        except Exception as e:  # noqa: BLE001 - best effort
            LOG.info("Asking %s's SM to stop failed: %s", node, e)

    def _prepare_start(self):
        """The start request's lease and deadline, and the watchdog's lease,
        before the manager starts OpenSM"""
        deadline = None if self.held else self.t0 + self.config.self_stop_timeout
        self.runner.prepare(self.uuid, self.term, deadline)
        self._allow_start()
        self.opensm_started = True
        self._write_watchdog_lease(
            None if self.held else self._watchdog_deadline(_now())
        )

    def _start_opensm(self):
        """Start OpenSM as the frozen node, which holds"""
        self._prepare_start()
        self.manager.start()
        self.role = Role.LEADER
        LOG.warning("Leading, term %d", self.term)

    # Stepping down

    def _opensm_stopped(self):
        """From the applier's thread: leave it to the lease thread, since a
        step-down waits for the applier to finish"""
        self._opensm_gone = True

    def _abandon(self):
        """Come back as a new instance, having made sure no OpenSM of ours
        runs, without releasing anything"""
        self._revoke_grant()
        self.runner.stop()
        if not self.runner.verify_stopped():
            LOG.error("Can't verify that OpenSM stopped: not a candidate")
            self._renewals_stopped = True
            return
        self.uuid = str(uuid.uuid4())
        self._seen = None
        self._seen_since = None
        self.role = Role.STARTING
        threading.Thread(target=self._rejoin, daemon=True).start()

    def _step_down(self, why, release=True, deadline=None):
        """Stop OpenSM, verify it stopped, and release the lease if asked,
        and if the deadline, at SIGTERM, allows"""
        LOG.warning("Stepping down: %s", why)
        self._revoke_grant()
        term = self.term
        # OpenSM first: the applier may take up to its bound to finish.
        self.runner.stop()
        self.manager.halt()
        # No longer a leader: its writes to the secured profile's records
        # mustn't raise the leader fence, nor its jobs be judged by its term.
        self.manager.term = 0
        self.opensm_started = False
        self._opensm_gone = False
        verified = self.runner.verify_stopped()
        if not verified:
            LOG.error("Can't verify that OpenSM stopped: no more renewals")
            self._renewals_stopped = True
            return
        if release and term is not None and (deadline is None or _now() < deadline):
            try:
                self.lease.release(self.node, self.uuid, term)
            except Exception as e:  # noqa: BLE001
                LOG.warning("Can't release the lease: %s", e)
        # A new instance UUID, as after a restart (see Rejoining)
        self.uuid = str(uuid.uuid4())
        self.term = None
        self.t0 = None
        self.held = False
        self._seen = None
        self._seen_since = None
        if not self._stop.is_set():
            self.role = Role.STARTING
        if not self._stop.is_set():
            threading.Thread(target=self._rejoin, daemon=True).start()
