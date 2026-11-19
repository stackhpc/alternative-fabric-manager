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

"""Configuration, read from an INI file"""

import collections
import configparser
import dataclasses
import logging
import os
import re
import socket

# Every port is only a limited member of the default partition, so that ports
# can't reach each other there; flat networks' ports are added as full
# members. See docs/opensm-hardening.md.
DEFAULT_PARTITION = "Default=0x7fff, ipoib : ALL=limited, SELF=full ;"
DEFAULT_VOLUMES = (
    "/lib/modules:/lib/modules:ro,"
    "/etc/localtime:/etc/localtime:ro,"
    "opensm_logs:/var/log,"
    "opensm_cache:/var/cache/opensm"
)
DRIVERS = ("managed", "docker", "none")
# The hardening groups that all means, the default: harden everything, and
# opt out of a group by name, with -group. A new group joins all once its
# hardware tests pass (specs/secured-profile.md, Choosing the groups); until
# then it is in HARDENING_GROUPS only, chosen by naming it beside all.
ALL_HARDENING_GROUPS = (
    "m_key",
    "sm_key",
    "sa_key",
    "sa_trust",
    "enforcement",
    "babbling_ports",
)
# The hardening groups of opensm_conf.HARDENING: all's, then the experimental
# ones outside it, untested on hardware, chosen by naming them beside all.
# Each joins all once its hardware tests pass (specs/secured-profile.md,
# Choosing the groups). m_key_per_port leaves state on ports, their keys;
# the implemented group hasn't run on hardware yet.
EXPERIMENTAL_HARDENING_GROUPS = (
    "m_key_per_port",
    "allowed_sm",
    "class_keys",
    "sa_rate",
)
HARDENING_GROUPS = ALL_HARDENING_GROUPS + EXPERIMENTAL_HARDENING_GROUPS
# What OpenSM's cache volume must be mounted at for per-port M_Keys: the
# manager copies neighbors and guid2mkey into it before each start, which a
# read-only root filesystem without the volume would refuse.
OPENSM_CACHE_DIR = "/var/cache/opensm"
# The options that m_key_per_port sets or depends on, which [opensm_conf]
# can't override while it's named: the M_Key line is the base while the
# target is on, and OpenSM forces the others with the option anyway.
PER_PORT_OPTIONS = (
    "m_key",
    "m_key_per_port",
    "m_key_protection_level",
    "m_key_lookup",
)
# opensm.conf keys that the manager sets, which overrides can't change: the
# manager depends on OpenSM reading its partitions file, staying in the
# foreground, logging to its container's output, and enforcing partitions.
MANAGED_OPENSM_KEYS = (
    "partition_config_file",
    "daemon",
    "log_file",
    "no_partition_enforcement",
)
# allowed_sm_guids as OpenSM 5.17 reads it: 0 alone, or GUIDs separated by
# commas, spaces, tabs or newlines (osm_update_allowed_sm_guids splits with
# strtok_r on ", \t\n"). Only 0x-prefixed GUIDs are accepted, so that Python
# and OpenSM can't read one differently.
_ALLOWED_SM_GUID = re.compile(r"0[xX][0-9a-fA-F]{1,16}")
_ALLOWED_SM_SEPARATORS = re.compile(r"[,\s]+")
# The log levels whose messages the manager watches for: errors, info and
# verbose, which has the sweep banners
REQUIRED_LOG_FLAGS = 0x07
NO_HARDENING = ("none", "false", "no")
ALL_HARDENING = ("all", "true", "yes")


LOG = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclasses.dataclass
class Config:
    # [api]
    host: str = "127.0.0.1"
    port: int = 8090
    # The networking-mlnx [sdn] url is http://<host>:<port><url_prefix>.
    url_prefix: str = "/ufmRestV3"
    # The networking-mlnx [sdn] domain.
    domain: str = "cloudx"
    # The networking-mlnx [sdn] token. Requests must carry
    # "Authorization: Basic <token>".
    token: str = ""

    # [database]
    # An SQLite file, for a single instance
    database: str = "/var/lib/alternative-fabric-manager/afm.sqlite"
    # Or a database URL, mysql+pymysql://user:password@host/afm for MariaDB
    # or Galera, which overrides it. The schema is then managed with
    # alternative-fabric-manager --db-upgrade.
    connection: str = ""

    @property
    def database_url(self):
        """The database to use: the connection URL, or the SQLite file"""
        return self.connection or self.database

    # [opensm]
    # managed: the manager runs OpenSM in a container of its own, in the
    #   manager's PID namespace, so OpenSM never outlives the manager.
    # docker: OpenSM runs in a container that something else starts; the
    #   manager only sends it SIGHUP.
    # none: only write the files (for tests).
    driver: str = "managed"
    # The OpenSM container: created with this name (managed), or signalled.
    container: str = "opensm"
    # managed: the OpenSM image, and the container's mounts as
    # source:destination[:mode], separated by commas. OpenSM's options go in
    # [opensm_conf].
    image: str = "ghcr.io/stackhpc/docker-opensm:latest"
    volumes: str = DEFAULT_VOLUMES
    # managed: the manager's own container, whose PID namespace OpenSM joins.
    # Empty when the manager isn't in a container, for development only:
    # OpenSM then survives the manager.
    manager_container: str = "alternative-fabric-manager"
    # The directory with OpenSM's files, as the manager sees it, and as the
    # host sees it for OpenSM's bind mount (managed). OpenSM sees it at
    # /etc/opensm.
    config_dir: str = "/etc/opensm"
    host_config_dir: str = "/etc/opensm"
    # A Jinja template for the partitions file, instead of the default.
    partitions_template: str = ""
    # Seconds to wait for OpenSM to start, and to finish a pkey sweep.
    start_timeout: float = 120.0
    sweep_timeout: float = 30.0
    # Seconds between checks that OpenSM is running (managed).
    check_interval: float = 5.0
    # Seconds between full verifies of per-port M_Keys (m_key_per_port):
    # each Gets every recorded port behind a link that's up, which is too
    # slow for every check, so the fabric check uses the last verify's
    # result, and fails if it's older than two of these.
    port_verify_interval: float = 60.0
    # Times to try applying a change in a row, before the manager waits for
    # the next check_interval.
    apply_attempts: int = 3
    # Seconds after which a job that hasn't completed fails. networking-mlnx
    # sets a port whose job failed to ERROR, so this must be longer than the
    # longest the manager can take to apply a change.
    job_timeout: float = 180.0

    # [ha] (see specs/ha.md). Times are in seconds.
    ha: bool = False
    # This instance's node, unique in the cluster
    node: str = dataclasses.field(default_factory=socket.gethostname)
    # T: how long a lease goes unrenewed before candidates take it
    lease_timeout: float = 30.0
    # T_self: how long a leader goes without renewing before it stops OpenSM
    self_stop_timeout: float = 20.0
    # T_watchdog: the watchdog's lease, and the unconfirmed wait
    watchdog_timeout: float = 45.0
    # T_reach: how long candidates wait for one that reaches every switch
    reach_timeout: float = 30.0
    # How often leases are renewed and read
    ha_period: float = 2.0
    # Every database call's timeout
    call_timeout: float = 3.0
    # Ask the old leader's SM to disable itself, at a takeover
    fence_ask: bool = True
    # How long a step-down at SIGTERM may take
    shutdown_timeout: float = 20.0
    # The manager's container's stop timeout, which the deployment sets: 30 s
    # with docker run --stop-timeout, Docker's default of 10 s otherwise, as
    # with Kayobe's controller_containers
    container_stop_timeout: float = 30.0
    # How long after leadership moves its discretionary triggers wait, so
    # that a fault every node shares can't bounce it between them
    step_down_cooldown: float = 300.0
    # The tmpfs that the manager and OpenSM's watchdog share
    ha_state_dir: str = "/run/alternative-fabric-manager"
    # The host's /proc, mounted read-only, to verify no OpenSM runs
    host_proc: str = "/host/proc"
    # OpenSM's image's start script, which the watchdog runs
    opensm_start: str = "/start.sh"
    # The rule for the default partition. All flat networks share it. Keep
    # ALL=full while ports on flat networks must reach each other; otherwise
    # give ports limited membership.
    default_partition: str = DEFAULT_PARTITION
    # The partition of flat networks, which share it, with indx0 like a VLAN
    # network's. Outside the VLAN range, so that it can't be a network's.
    # 0x7fff puts flat networks' ports in the default partition as full
    # members instead, where every port that is a limited member, such as a
    # VLAN network's, can reach them.
    flat_pkey: int = 0x7FFE
    # Allow ALL=full (or ALL_CAS, ALL_VCAS) in the default partition, which
    # lets every port reach every other there, including other tenants'. Only
    # for a fabric where that is already relied on.
    shared_default_partition: bool = False
    # IPoIB broadcast group settings for networks' partitions, as IBTA
    # values (rate 3 = 10 Gb/s, mtu 4 = 2048).
    ipoib_rate: int = 3
    ipoib_mtu: int = 4
    ipoib_sl: int = 0

    # The groups of hardened OpenSM options of docs/opensm-hardening.md to
    # write into opensm.conf (managed), separated by commas: m_key, sm_key,
    # sa_key, sa_trust, enforcement, babbling_ports; all, the default; -group
    # to remove one from all ("-sa_trust" alone starts from all); or none,
    # alone. See parse_hardening. The experimental groups, untested on
    # hardware, aren't in all, and are chosen by naming them beside it, as
    # "all, allowed_sm": m_key_per_port only permits per-port M_Keys, which
    # an operator's command begins; allowed_sm makes OpenSM ignore every SM
    # outside this host's physical ports; class_keys keys every port's VS,
    # CC and N2N classes from the stored seed, and leaves keys on ports;
    # sa_rate limits each port's SA requests (sa_rate_threshold).
    # Read docs/opensm-hardening.md, Before enabling, first.
    # [opensm_conf] overrides any single option.
    hardening: str = "all"
    # The lease, in seconds, of every key the manager sets on ports (the
    # M_Key, and later the class keys). 0, the default, makes them permanent:
    # a port never drops its protection. A positive lease lets ports keyed
    # with a lost key be keyed again, but any outage with no SM longer than
    # the lease lets a tenant read the fabric's M_Key, for good
    # (docs/opensm-hardening.md, Permanent keys). [opensm_conf] can still
    # override a single lease. Fabric-affecting: the same on every HA node.
    key_lease_period: int = 0

    # [opensm_conf]: opensm.conf options, as key = value (managed)
    opensm_conf: dict = dataclasses.field(default_factory=dict)

    @property
    def hardening_groups(self):
        """The hardening groups that [opensm] hardening names"""
        return parse_hardening(self.hardening)

    @property
    def allowed_sm_in_effect(self):
        """Whether the manager works out and checks OpenSM's
        allowed_sm_guids list: the group named, or, in single node, the
        option overridden, which gets the group's checks too

        In HA mode an override is passed to OpenSM unchecked, as 0.3.0 did
        and its docs told HA deployments to set it: refusing it would stop
        each upgraded node of such a cluster from starting."""
        if "allowed_sm" in self.hardening_groups:
            return True
        return "allowed_sm_guids" in self.opensm_conf and not self.ha

    @property
    def partitions_file(self):
        return os.path.join(self.config_dir, "partitions.conf")

    @property
    def opensm_conf_file(self):
        return os.path.join(self.config_dir, "opensm.conf")

    @classmethod
    def load(cls, path):
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path):
            raise ConfigError(f"Can't read configuration file {path}")
        config = cls()
        for section, names in OPTIONS:
            if not parser.has_section(section):
                continue
            for name in sorted(set(parser.options(section)) & REMOVED[section]):
                LOG.warning(
                    "[%s] %s was removed, and is ignored: %s",
                    section,
                    name,
                    REMOVED_WHY[name],
                )
            unknown = set(parser.options(section)) - set(names) - REMOVED[section]
            if unknown:
                raise ConfigError(
                    f"[{section}] has unknown options: {', '.join(sorted(unknown))}"
                )
            for name in names:
                if not parser.has_option(section, name):
                    continue
                kind = type(getattr(config, name))
                value = parser.get(section, name)
                try:
                    if kind is bool:
                        setattr(config, name, parser.getboolean(section, name))
                    elif kind is int:
                        setattr(config, name, int(value, 0))
                    else:
                        setattr(config, name, kind(value))
                except ValueError:
                    raise ConfigError(
                        f"[{section}] {name} must be a {kind.__name__}, not {value!r}"
                    ) from None
        if parser.has_section("opensm_conf"):
            config.opensm_conf = dict(parser.items("opensm_conf"))
        config.validate()
        return config

    def _validate_ha(self):
        problems = _ha_problems(self)
        if problems:
            raise ConfigError("[ha] " + "; ".join(problems))
        for warning in ha_warnings(self):
            LOG.warning("%s", warning)

    def validate(self):
        if not self.token:
            raise ConfigError("[api] token must be set")
        if self.ha:
            self._validate_ha()
        if self.driver not in DRIVERS:
            raise ConfigError(f"[opensm] driver must be one of {', '.join(DRIVERS)}")
        if not self.default_partition.strip().endswith(";"):
            raise ConfigError("[opensm] default_partition must end with ;")
        if self.apply_attempts < 1:
            raise ConfigError("[opensm] apply_attempts must be at least 1")
        parse_hardening(self.hardening)
        for name in (
            "start_timeout",
            "sweep_timeout",
            "check_interval",
            "job_timeout",
            "port_verify_interval",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"[opensm] {name} must be positive")
        # The M_Key's lease is a 16-bit field of PortInfo.
        if not 0 <= self.key_lease_period <= 65535:
            raise ConfigError(
                "[opensm] key_lease_period must be 0 (permanent keys) to 65535 s"
            )
        for name, low, high in (
            ("ipoib_rate", 2, 22),
            ("ipoib_mtu", 1, 5),
            ("ipoib_sl", 0, 15),
        ):
            if not low <= getattr(self, name) <= high:
                raise ConfigError(f"[opensm] {name} must be from {low} to {high}")
        if not 4095 <= self.flat_pkey <= 0x7FFF:
            raise ConfigError(
                "[opensm] flat_pkey must be from 0x0fff to 0x7fff, outside the "
                "VLAN range"
            )
        if self.opensm_conf.get("part_enforce", "both") != "both":
            raise ConfigError("[opensm_conf] part_enforce must be both")
        log_flags = self.opensm_conf.get("log_flags")
        if log_flags is not None:
            try:
                flags = int(log_flags, 0)
            except ValueError:
                raise ConfigError("[opensm_conf] log_flags isn't a number") from None
            if flags & REQUIRED_LOG_FLAGS != REQUIRED_LOG_FLAGS:
                raise ConfigError(
                    f"[opensm_conf] log_flags must include 0x{REQUIRED_LOG_FLAGS:02x}, "
                    "whose messages the manager watches for"
                )
        # With a seed of 0, OpenSM derives the class keys from the M_Key. The
        # manager also refuses a seed other than the stored one, on start.
        # Only spellings that Python and OpenSM's parser read alike: no
        # underscores, signs or octal-looking leading zeros.
        seed = self.opensm_conf.get("key_mgr_seed")
        if seed is not None:
            if not re.fullmatch(r"0[xX][0-9a-fA-F]{1,16}|[1-9][0-9]*", seed):
                raise ConfigError(
                    "[opensm_conf] key_mgr_seed must be hexadecimal (0x...) or "
                    "decimal without leading zeros"
                )
            if not 0 < int(seed, 0) < 2**64:
                raise ConfigError(
                    "[opensm_conf] key_mgr_seed must be a non-zero 64-bit value"
                )
        # The per-port M_Key base is the manager's, not an OpenSM option: as
        # an override it would be written into opensm.conf for nothing.
        if "m_key_base" in self.opensm_conf:
            raise ConfigError(
                "[opensm_conf] m_key_base isn't an OpenSM option: the manager "
                "keeps the per-port M_Key base in its database"
            )
        self._validate_per_port_keys()
        self._validate_allowed_sm()
        managed = set(self.opensm_conf) & set(MANAGED_OPENSM_KEYS)
        if managed:
            raise ConfigError(
                f"[opensm_conf] can't set {', '.join(sorted(managed))}, "
                "which the manager sets"
            )
        for key, value in self.opensm_conf.items():
            if not key.replace("_", "").isalnum() or "\n" in value:
                raise ConfigError(f"[opensm_conf] {key} isn't an OpenSM option")

    def _validate_per_port_keys(self):
        """Refuse m_key_per_port where the manager can't keep the ports' keys

        See specs/secured-profile.md, Per-port M_Keys, The design: the
        manager must write OpenSM's cache volume and run the prober in
        OpenSM's image, and the options the group sets must be its own.
        """
        groups = self.hardening_groups
        if "m_key_per_port" not in groups:
            # By hand, the option would re-key every port with no record of
            # which key each holds, which no later start could reach.
            if "m_key_per_port" in self.opensm_conf:
                raise ConfigError(
                    "[opensm_conf] can't set m_key_per_port: name the "
                    "m_key_per_port hardening group instead"
                )
            return
        # Per-port keys are a form of M_Key protection: without the m_key
        # group, nothing sets the protection they key.
        if "m_key" not in groups:
            raise ConfigError("[opensm] hardening m_key_per_port needs the m_key group")
        # HA mode refuses the group in _ha_problems, which validate checks
        # first.
        if self.driver != "managed":
            raise ConfigError(
                "[opensm] hardening m_key_per_port needs the managed driver, "
                "since the manager writes OpenSM's cache and runs its prober "
                "in OpenSM's image"
            )
        if not cache_volume_writable(self.volumes):
            raise ConfigError(
                "[opensm] hardening m_key_per_port needs a writable volume at "
                f"{OPENSM_CACHE_DIR} in [opensm] volumes, which the manager "
                "writes OpenSM's cache into before each start"
            )
        overridden = sorted(set(self.opensm_conf) & set(PER_PORT_OPTIONS))
        if overridden:
            raise ConfigError(
                f"[opensm_conf] can't set {', '.join(overridden)} with the "
                "m_key_per_port hardening group, which sets them"
            )

    def _validate_allowed_sm(self):
        """Refuse allowed_sm where an ignored SM could act unseen, or where
        the manager can't read this host's ports

        See specs/secured-profile.md, Allowed SM GUIDs, Required groups and
        Overrides. HA mode refuses it in _ha_problems, which validate checks
        first.
        """
        override = self.opensm_conf.get("allowed_sm_guids")
        if override is not None and parse_allowed_sm_guids(override) is None:
            raise ConfigError(
                "[opensm_conf] allowed_sm_guids must be 0, or port GUIDs (0x "
                "and up to 16 hex digits, not 0) separated by commas: OpenSM "
                "would otherwise disable the list"
            )
        if self.ha and "allowed_sm_guids" in self.opensm_conf:
            LOG.warning(
                "[opensm_conf] allowed_sm_guids is passed to OpenSM unchecked in "
                "HA mode: it must list every node's physical ports, or a "
                "takeover's OpenSM may be ignored or refuse to run"
            )
        if not self.allowed_sm_in_effect:
            return
        what = (
            "[opensm] hardening allowed_sm"
            if "allowed_sm" in self.hardening_groups
            else "[opensm_conf] allowed_sm_guids"
        )
        # The fabric check ignores an SM outside the list only because it has
        # neither key: without them, one could set unprotected ports, or hand
        # over, unseen.
        missing = sorted({"m_key", "sm_key"} - self.hardening_groups)
        if missing:
            raise ConfigError(f"{what} needs the {' and '.join(missing)} group(s)")
        level = str(self.opensm_conf.get("m_key_protection_level", "2")).strip()
        if level in ("0", "0x0"):
            raise ConfigError(f"{what} needs M_Key protection level 1 or more")
        if self.driver != "managed":
            raise ConfigError(
                f"{what} needs the managed driver, which reads this host's "
                "ports in OpenSM's image and writes opensm.conf"
            )


def parse_allowed_sm_guids(text):
    """The GUIDs of an allowed_sm_guids value, as a sorted tuple of ints, ()
    for 0 (no other SM), or None if OpenSM wouldn't read it as a list"""
    words = [w for w in _ALLOWED_SM_SEPARATORS.split(text.strip()) if w]
    if words == ["0"]:
        return ()
    if not words or not all(_ALLOWED_SM_GUID.fullmatch(w) for w in words):
        return None
    guids = {int(w, 16) for w in words}
    # OpenSM stops at a GUID of 0 that isn't alone, and disables the list.
    if 0 in guids:
        return None
    return tuple(sorted(guids))


def format_allowed_sm_guids(guids):
    """An allowed_sm_guids value in the manager's spelling: 0 for none"""
    return ",".join(f"0x{guid:016x}" for guid in sorted(guids)) or "0"


def parse_volumes(volumes):
    """The mounts of an [opensm] volumes value, each source:destination
    [:mode], separated by commas

    A mode is itself a comma-separated list (ro,z or z,ro), so an item
    without a colon continues the mode of the mount before it, unless it
    is a path, which is an anonymous volume of its own.
    """
    mounts = []
    for item in volumes.split(","):
        item = item.strip()
        if not item:
            continue
        if (
            ":" not in item
            and not item.startswith("/")
            and mounts
            and mounts[-1].count(":") >= 2
        ):
            mounts[-1] += "," + item
        else:
            mounts.append(item)
    return mounts


def cache_volume_writable(volumes):
    """Whether [opensm] volumes mounts something writable at OpenSM's cache

    Each mount is source:destination[:mode]; a mode with ro among its
    comma-separated options makes it read-only, so the manager couldn't
    copy the cache into it.
    """
    for volume in parse_volumes(volumes):
        fields = volume.split(":")
        if len(fields) < 2 or not fields[0]:
            continue
        destination = fields[1].rstrip("/") or "/"
        mode = fields[2] if len(fields) > 2 else ""
        options = {option.strip() for option in mode.split(",")}
        if destination == OPENSM_CACHE_DIR and "ro" not in options:
            return True
    return False


def ha_settings(config):
    """The settings every instance of a cluster must share, as stored"""
    import json

    return json.dumps(
        {
            name: getattr(config, name)
            for name in (
                "lease_timeout",
                "self_stop_timeout",
                "watchdog_timeout",
                "reach_timeout",
                "ha_period",
                "call_timeout",
                "fence_ask",
                "job_timeout",
                "step_down_cooldown",
            )
        },
        sort_keys=True,
    )


def parse_hardening(text):
    """Return the hardening groups that an [opensm] hardening value names

    It is a list separated by commas of group names, all (or true, yes),
    and -group to remove a group; or none (or false, no) alone. all is the
    default, so a list of removals alone, such as "-sa_trust", starts from
    all. Raises ConfigError for a list that is unclear or contradicts
    itself, so that what's hardened is never a guess.
    """
    words = [w.strip().lower() for w in text.split(",") if w.strip()]
    if not words:
        # A blank line, or a template that rendered nothing, mustn't turn
        # hardening off: none says so explicitly.
        raise ConfigError(
            "[opensm] hardening is empty: say all, a list of groups, or none"
        )
    removed = {w[1:].strip() for w in words if w.startswith("-")}
    named = {w for w in words if not w.startswith("-")}
    known = set(HARDENING_GROUPS)
    unknown = (named - known - set(ALL_HARDENING) - set(NO_HARDENING)) | (
        removed - known
    )
    if unknown:
        raise ConfigError(
            f"[opensm] hardening has unknown groups: {', '.join(sorted(unknown))}"
        )
    if named & set(NO_HARDENING) and len(words) > 1:
        raise ConfigError(
            "[opensm] hardening is either none alone, or a list of groups, "
            "which may include all and -group"
        )
    if named & removed:
        raise ConfigError(
            "[opensm] hardening both names and removes "
            + ", ".join(sorted(named & removed))
        )
    # Removing from a list of groups that doesn't include all is either a
    # no-op or a mistake: say which groups to keep instead.
    starts_from_all = bool(named & set(ALL_HARDENING)) or not named
    if removed and not starts_from_all:
        raise ConfigError(
            "[opensm] hardening can remove groups (-group) only from all; "
            "list the groups to keep instead"
        )
    groups = named & known
    if starts_from_all and not named & set(NO_HARDENING):
        groups |= set(ALL_HARDENING_GROUPS)
    return groups - removed


def takeover_bound(config):
    """The longest an HA takeover takes, from the old leader's last apply to
    the new leader's OpenSM starting: as long as the fabric may go without a
    subnet manager in an unclean takeover"""
    apply_bound = config.sweep_timeout + 5 + 5 * config.call_timeout + 10
    return (
        apply_bound
        + config.lease_timeout
        + config.ha_period
        + config.reach_timeout
        + 5
        + config.watchdog_timeout
        + config.start_timeout
        + 5
    )


def ha_warnings(config):
    """HA settings that are allowed but weaken the fabric's protection"""
    warnings = []
    takeover = takeover_bound(config)
    if 0 < config.key_lease_period <= takeover:
        # Not refused, so that a test fabric can use a short lease
        warnings.append(
            f"[opensm] key_lease_period is {config.key_lease_period} s, no longer "
            f"than a takeover's longest, {takeover:.0f} s: any tenant can read "
            "the M_Key during an unclean takeover (see docs/opensm-hardening.md, "
            "Permanent keys)"
        )
    return warnings


def _ha_problems(config):
    """The timing inequalities of specs/ha.md that a configuration breaks"""
    problems = []
    if not config.connection:
        problems.append("[database] connection must name a server database")
    if (
        config.self_stop_timeout + config.ha_period + config.call_timeout
        >= config.lease_timeout
    ):
        problems.append(
            "self_stop_timeout + ha_period + call_timeout must be less than "
            "lease_timeout"
        )
    takeover = takeover_bound(config)
    if config.job_timeout <= takeover:
        problems.append(
            f"[opensm] job_timeout must exceed {takeover:.0f} s, a takeover's "
            "longest, so that no job times out during one; HA deployments use "
            "300 s, with Nova's vif_plugging_timeout at 420 s"
        )
    if config.shutdown_timeout >= config.container_stop_timeout:
        problems.append(
            "shutdown_timeout must be less than container_stop_timeout, the "
            "manager's container's stop timeout"
        )
    if config.watchdog_timeout <= config.ha_period + config.call_timeout:
        problems.append("watchdog_timeout must exceed ha_period + call_timeout")
    if config.driver != "managed":
        problems.append("[opensm] driver must be managed: the manager starts OpenSM")
    if not config.manager_container:
        problems.append(
            "[opensm] manager_container must name the manager's container, "
            "whose PID namespace OpenSM joins, so that it dies with the manager"
        )
    # Without them, a tenant's SM could set ports or win a handover, and Ask
    # has no key (see Fencing, Required keys).
    missing = {"m_key", "sm_key"} - config.hardening_groups
    if missing:
        problems.append(
            "[opensm] hardening must include " + " and ".join(sorted(missing))
        )
    if str(config.opensm_conf.get("m_key_protection_level", "1")) == "0":
        problems.append("M_Key protection level must be 1 or more")
    # HA needs configuration epochs, so that leaders with and without the
    # group can't alternate, and a reach probe that works on re-keyed ports
    # (specs/secured-profile.md, Per-port M_Keys, requirement 5).
    if "m_key_per_port" in config.hardening_groups:
        problems.append(
            "[opensm] hardening can't include m_key_per_port: per-port M_Keys "
            "aren't supported in HA mode yet"
        )
    # Untested anywhere: experimental groups are single node until their
    # hardware tests run (specs/secured-profile.md, SA request rate).
    if "sa_rate" in config.hardening_groups:
        problems.append(
            "[opensm] hardening can't include sa_rate: the SA request rate "
            "limit isn't supported in HA mode yet"
        )
    # A takeover by another host hasn't been shown to derive the same class
    # keys (specs/secured-profile.md, The seed).
    if "class_keys" in config.hardening_groups:
        problems.append(
            "[opensm] hardening can't include class_keys: class keys aren't "
            "supported in HA mode yet"
        )
    # HA needs node rows with port GUIDs, configuration epochs, the managed
    # node SM rule and the fence ask first (specs/secured-profile.md,
    # Allowed SM GUIDs, HA). A list typed by hand is passed on unchecked, as
    # in 0.3.0 (see Config.allowed_sm_in_effect).
    if "allowed_sm" in config.hardening_groups:
        problems.append(
            "[opensm] hardening can't include allowed_sm: allowed SMs aren't "
            "supported in HA mode yet"
        )
    return problems


# Options a release removed, which a configuration may still set: ignored,
# with a warning, for a release or so
REMOVED_WHY = {
    "manual_takeover": "every takeover is automatic; see specs/ha.md, Fencing",
}
REMOVED = collections.defaultdict(set, {"ha": {"manual_takeover"}})

OPTIONS = (
    ("api", ("host", "port", "url_prefix", "domain", "token")),
    ("database", ("database", "connection")),
    (
        "opensm",
        (
            "driver",
            "container",
            "image",
            "volumes",
            "manager_container",
            "config_dir",
            "host_config_dir",
            "partitions_template",
            "hardening",
            "key_lease_period",
            "start_timeout",
            "sweep_timeout",
            "check_interval",
            "port_verify_interval",
            "apply_attempts",
            "job_timeout",
            "default_partition",
            "flat_pkey",
            "shared_default_partition",
            "ipoib_rate",
            "ipoib_mtu",
            "ipoib_sl",
        ),
    ),
    (
        "ha",
        (
            "ha",
            "node",
            "lease_timeout",
            "self_stop_timeout",
            "watchdog_timeout",
            "reach_timeout",
            "ha_period",
            "call_timeout",
            "fence_ask",
            "shutdown_timeout",
            "container_stop_timeout",
            "step_down_cooldown",
            "ha_state_dir",
            "host_proc",
            "opensm_start",
        ),
    ),
)
