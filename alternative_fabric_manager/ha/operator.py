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

"""Operators' commands: freeze, unfreeze, force an unfreeze, status,
configuration epochs, and forgetting a node"""

import re
import socket
import time

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db
from alternative_fabric_manager.ha import lease as lease_mod


class OperatorError(Exception):
    pass


def freeze(config, lease, sessionmaker):
    """Freeze the cluster on the current leader's node, once it agrees"""
    state = lease.read()
    if state.released or not state.holder_uuid:
        raise OperatorError("No leader holds the lease")
    if state.freeze_state:
        raise OperatorError(f"The cluster's freeze is already {state.freeze_state}")
    with sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        if applied is None or applied.term != state.term or not applied.check_ok:
            raise OperatorError(
                "The leader's last fabric check in this term didn't pass"
            )
    by = f"{socket.gethostname()}"
    if not lease.request_freeze(state.term, by, config.self_stop_timeout):
        raise OperatorError("The lease moved, or a takeover isn't finished")
    deadline = time.monotonic() + config.self_stop_timeout + config.ha_period
    while time.monotonic() < deadline:
        if lease.read().freeze_state == "frozen":
            return state.holder_node
        time.sleep(config.ha_period / 2)
    # The acknowledgement may have committed since the last read.
    if lease.withdraw_freeze() == "frozen":
        return state.holder_node
    raise OperatorError("The leader didn't acknowledge the freeze")


def unfreeze(config, lease, wait):
    """End the freeze; the leader acknowledges it when it renews normally"""
    if lease.request_unfreeze() not in ("ending", ""):
        raise OperatorError("The cluster isn't frozen")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if lease.read().freeze_state == "":
            return True
        time.sleep(config.ha_period / 2)
    return False


def force_unfreeze(lease):
    """End a freeze its frozen leader never acknowledged, by confirming that
    that leader's OpenSM can't act; returns its node and term

    The confirmation names the term and the holding instance read here, and
    lease.confirm() takes it only if both still hold the ending freeze, so
    neither a later freeze nor an instance that adopted this one meanwhile
    is confirmed by mistake."""
    state = lease.read()
    if state.freeze_state != "ending":
        raise OperatorError(
            "Only while a freeze ends: the cluster isn't unfreezing; "
            "run --unfreeze first"
        )
    if not lease.confirm(state.term, state.holder_uuid):
        raise OperatorError("The freeze changed meanwhile: check --status")
    return state.holder_node, state.term


def status(state):
    """--status's lines, for a LeaseState"""
    holder = state.holder_node or "-"
    lines = [
        f"holder: {holder} (instance {state.holder_uuid or '-'})",
        f"term: {state.term}",
        f"released: {'yes' if state.released else 'no'}",
        f"freeze: {state.freeze_state or 'none'}",
    ]
    if state.freeze_state:
        lines.append(f"frozen term: {state.freeze_term}")
    if state.freeze_state == "ending":
        if state.confirmed_term == state.term:
            lines.append(
                f"unfreeze pending: forced, with {holder}'s OpenSM taken as stopped; "
                "another instance takes over at its next poll"
            )
        else:
            lines += [
                f"unfreeze pending: {holder}'s manager hasn't acknowledged it, "
                "so no instance takes over until it does, its node's stop is "
                "verified, or an operator forces the unfreeze.",
                f"Check {holder}: if its manager is paused or hung, stop its "
                "OpenSM, check that no opensm process runs there, and run "
                "--force-unfreeze.",
            ]
    return lines


# Configuration epochs (see specs/ha.md, Configuration epochs)

_HASH = re.compile(r"[0-9a-f]{64}")


def own_hash(config, lease):
    """The hash of the configuration this command runs with"""
    return config_mod.config_hash(config, lease.hash_key())


def config_epoch(config, lease):
    """--config-epoch: the epoch, this configuration's hash, and each node's
    hash and whether the epoch allows it"""
    ours = own_hash(config, lease)
    epoch = lease.epoch()
    allowed = None if epoch is None else set(epoch[1])

    def verdict(config_hash):
        if allowed is None:
            return "allowed (no epoch yet)"
        return "allowed" if config_hash in allowed else "NOT allowed"

    if epoch is None:
        lines = ["No configuration epoch yet: every hash is allowed until a takeover"]
    else:
        lines = [f"Epoch {epoch[0]} allows: {', '.join(sorted(allowed))}"]
    lines.append(f"This configuration: {ours} {verdict(ours)}")
    for name, _instance, _beat, _reach, stopped, config_hash in sorted(lease.nodes()):
        state = " (stopped)" if stopped else ""
        lines.append(
            f"Node {name}{state}: {config_hash or '(none recorded)'} "
            f"{verdict(config_hash)}"
        )
    return lines


def allow_config(config, lease, other=None):
    """--allow-config: record an epoch that allows this configuration's hash,
    and other's if given; return (epoch, hashes)"""
    hashes = [own_hash(config, lease)]
    if other:
        other = other.strip().lower()
        if not _HASH.fullmatch(other):
            raise OperatorError(
                f"{other!r} isn't a configuration hash: --config-epoch prints them"
            )
        hashes.append(other)
    hashes = sorted(set(hashes))
    settings = config_mod.ha_settings(config)
    try:
        return lease.allow_config(hashes, settings=settings), hashes
    except lease_mod.UnsafeSettings as e:
        raise OperatorError(
            f"This configuration's [ha] settings shorten a wait ({e}) while an "
            "instance with the old ones may still run OpenSM: stop every node "
            "cleanly, so that the target set is empty, first"
        ) from None


# Retiring a node (see specs/ha.md, Retiring a node)


def forget_node(config, lease, name, wait=time.sleep):
    """--forget-node: remove a retired node's row, once it's in no target-set
    entry and either marked stopped or its heartbeat stays still for T"""
    rows = {row[0]: row for row in lease.nodes()}
    if name not in rows:
        raise OperatorError(f"No node row is named {name}")
    if any(node == name for node, _instance, _term in lease.targets()):
        raise OperatorError(
            f"{name} is in the target set: it may run OpenSM. Stop its manager "
            "cleanly first"
        )
    _name, _instance, heartbeat, _reach, stopped, _hash = rows[name]
    if not stopped:
        wait(config.lease_timeout)
        again = {row[0]: row for row in lease.nodes()}.get(name)
        if again is not None and again[2] != heartbeat:
            raise OperatorError(
                f"{name}'s manager is live: its heartbeat moved. Stop it first"
            )
    if not lease.forget_node(name, heartbeat if not stopped else None):
        raise OperatorError(
            f"{name}'s row changed meanwhile: its manager may be live, or "
            "joined the target set"
        )
