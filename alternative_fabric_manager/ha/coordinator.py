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
  neighbouring switch port is disabled (see Taking the lease).
"""

import enum
import logging
import threading
import time
import uuid

from alternative_fabric_manager import config as config_mod
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
    def __init__(self, config, lease, runner, manager, config_hash="", adapters=()):
        self.config = config
        self.lease = lease
        self.runner = runner
        self.manager = manager
        self.config_hash = config_hash
        self.adapters = list(adapters)
        self.node = config.node
        self.uuid = str(uuid.uuid4())
        self.role = Role.STARTING
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
        manager.on_opensm_stopped = self._opensm_stopped

    # Lifecycle

    def start(self):
        if getattr(self.runner, "reach", None) is not None:
            threading.Thread(
                target=self._refresh_reach, name="reach", daemon=True
            ).start()
        self._rejoin()
        self._thread = threading.Thread(target=self._run, name="lease", daemon=True)
        self._thread.start()

    def _rejoin(self):
        """Register with a new instance UUID, and make sure no OpenSM of ours
        runs, or adopt a frozen lease on this node (see Rejoining, Freeze)"""
        while not self._stop.is_set():
            try:
                self.lease.register(
                    self.node, self.uuid, self.config_hash, self.adapters
                )
                if not self.lease.agree_settings(config_mod.ha_settings(self.config)):
                    LOG.error(
                        "This instance's [ha] settings differ from the cluster's: "
                        "not a candidate"
                    )
                    time.sleep(self.config.ha_period)
                    continue
                state = self.lease.read()
                if state.freeze_state in ("frozen", "ending") and (
                    state.holder_node == self.node
                ):
                    term = self.lease.adopt_frozen(self.node, self.uuid, state.term)
                    if term is not None:
                        LOG.warning("Adopting the frozen lease of term %d", term)
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
            self.lease.heartbeat(self.node, self.uuid, self._reach())
        except Exception as e:  # noqa: BLE001
            LOG.debug("Heartbeat failed: %s", e)
        if self.role in (Role.LEADER, Role.TAKING_OVER):
            self._lead()
        elif self.role == Role.CANDIDATE:
            self._watch()

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
            self._stop.wait(max(self.config.ha_period * 4, 2.0))

    def _reach(self):
        """Expected switches this node reaches; 0 if its own port is down"""
        if self._reach_value is None:
            # Not refreshed yet, or no thread, as in tests
            return self._probe_reach()
        return self._reach_value

    def _live_others(self):
        """Other nodes whose heartbeat moved within T, not stopped, and not
        in the target set: the live candidates (see Stepping down)"""
        now = _now()
        targets = {instance for _node, instance, _term in self.lease.targets()}
        live = []
        for name, instance, heartbeat, reach, stopped in self.lease.nodes():
            seen = self._heartbeats.get(name)
            if seen is None or seen[0] != heartbeat:
                self._heartbeats[name] = (heartbeat, now)
                seen = self._heartbeats[name]
            if (
                name != self.node
                and not stopped
                and instance not in targets
                and now - seen[1] < self.config.lease_timeout
            ):
                live.append((name, reach))
        return live

    # Leading

    def _loops_alive(self):
        """The liveness condition: no loop iteration over its bound"""
        if self.manager.iteration_age() > self.apply_bound:
            LOG.error("The apply loop is past its bound")
            return False
        if (
            self._takeover_started is not None
            and _now() - self._takeover_started > self.takeover_bound
        ):
            LOG.error("The takeover is past its bound")
            return False
        return True

    @property
    def apply_bound(self):
        # One sweep, a fabric check, and five database calls, plus margin
        return self.config.sweep_timeout + 5 + 5 * self.config.call_timeout + 10

    @property
    def takeover_bound(self):
        # Each takeover step polls; the longest is OpenSM's start.
        return self.config.start_timeout + 5 * self.config.call_timeout + 10

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
            return
        if not renewed:
            LOG.error("Lost the lease of term %s", self.term)
            self._step_down("lost the lease", release=False)
            return
        self.t0 = now
        if self.opensm_started:
            self.runner.write_lease(
                self.uuid,
                self.term,
                None if self.held else self._watchdog_deadline(now),
            )
        if self._opensm_gone and self.held:
            self._restart_frozen()
        self._freeze_step()

    def _discretionary_trigger(self):
        """Why an established leader should hand over, if a live candidate
        could do better and the cooldown has passed (see Stepping down)"""
        if self.held or self.role != Role.LEADER:
            return None
        manager = self.manager
        stalled = manager.unanswered_checks >= 3 or manager.failed_applies >= 3
        missing = getattr(manager, "missing_checks", 0) >= 3
        if not (stalled or missing):
            return None
        if self._moved_age < self.config.step_down_cooldown:
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
        self.runner.prepare(self.uuid, self.term, None)

        def restart():
            try:
                self.manager.start()
            except Exception:
                LOG.exception("Restarting OpenSM while frozen failed")
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
                self.runner.write_lease(self.uuid, self.term, None)
        elif state.freeze_state == "ending":
            # In this order, and again until it commits: a deadline for the
            # watchdog, self-stop timing, a renewal, then the acknowledgement
            # (see Freeze).
            now = _now()
            if self.opensm_started:
                self.runner.write_lease(
                    self.uuid, self.term, self._watchdog_deadline(now)
                )
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
        if not self._eligible(returned - self._expired_since):
            return
        try:
            term = self.lease.acquire(
                self.node, self.uuid, state.term, state.counter, state.released
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

    # Taking over

    def _take_over(self, term, taken_at):
        """Wait for every other target to leave the set, then start OpenSM"""
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
        confirmed = None
        for node, instance, entry_term in others:
            if self.lease.remove_target(self.uuid, term, node, instance, "verified"):
                LOG.info("%s's OpenSM is verified stopped", node)
                continue
            if self.config.fence_ask and (node, instance) not in asked:
                asked.add((node, instance))
                self._ask(node)
            if _now() - taken_at < self.config.watchdog_timeout:
                continue
            if self.config.manual_takeover:
                # Only an operator's confirmation for that entry's own term
                if confirmed is None:
                    confirmed = self.lease.read().confirmed_term
                if confirmed != entry_term:
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
        self.opensm_started = True
        self.runner.write_lease(
            self.uuid, self.term, None if self.held else self._watchdog_deadline(_now())
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
        term = self.term
        # OpenSM first: the applier may take up to its bound to finish.
        self.runner.stop()
        self.manager.halt()
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
