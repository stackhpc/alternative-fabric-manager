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

"""Class keys (VS, CC, N2N) in the single-node manager: whether a start may
render the group, the guard against dropping keys from ports unmanaged,
and what OpenSM's key managers report

specs/secured-profile.md, Class keys, is the source of truth. The group is
experimental, untested on hardware: HA mode refuses it until hardware test
3's second-host legs pass.

The seed opens every port's class keys: nothing here logs it, or any key.
"""

import collections
import contextlib
import logging

import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db, opensm_conf

LOG = logging.getLogger(__name__)

GROUP = "class_keys"
# The classes, as OpenSM's options name them, and each one's bit in the
# record of what the last opensm.conf enabled
CLASSES = {"vs": 1, "cc": 2, "n2n": 4}
# Which classes the last opensm.conf the manager wrote enabled (2), as bits
RECORD = "class_keys_enabled"
# Which of OpenSM's key managers a code belongs to, by its range, as the
# binary's own lines show (9803 "vskeyinfo", 9B03 "n2nkeyinfo", C124 "by CC
# keyinfo")
MANAGERS = {"98": "VS", "9B": "N2N", "C1": "congestion control"}
# How many GUIDs a log line names before "and N more"
SHOWN = 5


def _guids(guids):
    guids = sorted(guids)
    shown = ", ".join(f"0x{guid:016x}" for guid in guids[:SHOWN])
    if len(guids) > SHOWN:
        shown += f" and {len(guids) - SHOWN} more"
    return shown


def manager_of(code):
    return MANAGERS.get(code[:2], "an unknown")


def enables(groups, overrides):
    """Each class's <class>_key_enable as opensm.conf would have it: an
    override's value, 2 from the group, or None when it's left out, which
    OpenSM takes as 0 (Ignore). A value that isn't a number counts as 2,
    so that the guard errs towards keys being on."""
    found = {}
    for name in CLASSES:
        option = f"{name}_key_enable"
        value = overrides.get(option)
        if value is None:
            found[name] = (
                int(opensm_conf.HARDENING[GROUP][option]) if GROUP in groups else None
            )
            continue
        try:
            found[name] = int(str(value).strip(), 0)
        except ValueError:
            found[name] = 2
    return found


def guard_problem(recorded, groups, overrides):
    """Why a start may not drop the keys from ports unmanaged, or None

    recorded is the set of classes the last opensm.conf enabled. See
    specs/secured-profile.md, Turning the keys off, and downgrades.
    """
    if not recorded:
        return None
    names = ", ".join(sorted(recorded))
    if GROUP not in groups:
        return (
            f"ports may hold class keys ({names}) that the last opensm.conf "
            "enabled, and without the class_keys group OpenSM can't reach them "
            "to clear them: name the group, set each <class>_key_enable = 1 in "
            "[opensm_conf] and restart, check that OpenSM has cleared the keys, "
            "and only then remove the group (docs/opensm-hardening.md, Turning "
            "class keys off)"
        )
    ignored = sorted(
        name
        for name, value in enables(groups, overrides).items()
        if name in recorded and not value
    )
    if ignored:
        return (
            f"ports may hold {', '.join(ignored)} keys that the last opensm.conf "
            "enabled, and enable 0 would leave them unmanaged: set "
            "<class>_key_enable = 1 in [opensm_conf] and restart first, to clear "
            "them (docs/opensm-hardening.md, Turning class keys off)"
        )
    return None


class ClassKeys:
    def __init__(self, manager):
        self.manager = manager
        # The group is named, but this manager's start stored the seed, so
        # no OpenSM it starts gets the group (The seed)
        self.withheld = False
        # What OpenSM's key managers reported, logged once each for the
        # running OpenSM
        self._rejections = collections.defaultdict(set)
        self._errors = set()

    @property
    def config(self):
        return self.manager.config

    @contextlib.contextmanager
    def _session(self):
        with self.manager._lock, self.manager.sessionmaker() as session:
            yield session

    def _acts(self):
        """Whether the manager writes opensm.conf and owns the record: single
        node, managed driver. In HA mode, which refuses the group, a write
        outside the term's compare-and-set would be wrong."""
        return not self.config.ha and self.config.driver == "managed"

    @property
    def named(self):
        return GROUP in self.config.hardening_groups

    def recorded(self):
        """The classes the last opensm.conf the manager wrote enabled"""
        if not self._acts():
            return set()
        with self._session() as session:
            row = session.get(db.State, RECORD)
            bits = row.value if row is not None else 0
        return {name for name, bit in CLASSES.items() if bits & bit}

    def refuse_start(self, seed_kept):
        """Before a start: refuse one that would leave ports keyed with keys
        no SM manages, and withhold the group from a start that stored the
        seed. Raises ConfigError."""
        self.withheld = False
        if not self._acts():
            return
        problem = guard_problem(
            self.recorded(), self.config.hardening_groups, self.config.opensm_conf
        )
        if problem is not None:
            raise config_mod.ConfigError(f"Not starting OpenSM: {problem}")
        if self.named and not seed_kept:
            # No backup can hold a seed this start stored: ports keyed with
            # it, at a lease of 0, would be lost with the database.
            self.withheld = True
            LOG.error(
                "The class_keys group is named, but this start stored the class "
                "key seed, key_mgr_seed, which no backup holds yet: OpenSM starts "
                "without class keys. Back up the database, then restart the "
                "manager to turn them on"
            )

    def groups(self, groups):
        """The groups opensm.conf gets: without class_keys while withheld"""
        if self.withheld:
            return set(groups) - {GROUP}
        return groups

    def record(self, groups, overrides):
        """Record which classes the opensm.conf just written enables"""
        if not self._acts():
            return
        bits = sum(
            CLASSES[name]
            for name, value in enables(groups, overrides).items()
            if value == 2
        )
        for attempt in range(2):
            with self._session() as session:
                row = session.get(db.State, RECORD)
                if row is None:
                    if not bits:
                        return
                    session.add(db.State(name=RECORD, value=bits))
                else:
                    row.value = bits
                try:
                    session.commit()
                except sa.exc.IntegrityError:
                    # Another process created the row first; overwrite it.
                    session.rollback()
                    if attempt:
                        raise
                    continue
                return

    def watching(self):
        """Whether OpenSM may be keying ports, so that its key managers'
        reports matter"""
        if not self._acts():
            return False
        return (self.named and not self.withheld) or bool(self.recorded())

    def reset(self):
        """A new OpenSM: its reports are logged afresh"""
        self._rejections.clear()
        self._errors.clear()

    def report(self, findings):
        """Log what OpenSM's key managers reported since the last check:
        rejections as they grow, errors once per code"""
        grown = {}
        for code, guid, status, attr in findings.key_rejections:
            ports = self._rejections[(code, status, attr)]
            if guid not in ports:
                ports.add(guid)
                grown[(code, status, attr)] = ports
        for (code, status, attr), ports in sorted(grown.items()):
            LOG.warning(
                "OpenSM's %s key manager reports %d ports answering a KeyInfo "
                "with status 0x%x (ERR %s, attr_id 0x%x): they don't support that "
                "class's key, so it doesn't protect them: %s",
                manager_of(code),
                len(ports),
                status,
                code,
                attr,
                _guids(ports),
            )
        for code, guid in findings.key_errors:
            if code in self._errors:
                continue
            self._errors.add(code)
            LOG.error(
                "OpenSM's %s key manager failed to set class keys (ERR %s%s): "
                "check the ports' keys before relying on them",
                manager_of(code),
                code,
                f", port 0x{guid:016x}" if guid is not None else "",
            )
