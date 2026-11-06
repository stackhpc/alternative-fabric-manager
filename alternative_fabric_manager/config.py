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
# The hardening groups of opensm_conf.HARDENING
HARDENING_GROUPS = (
    "m_key",
    "sm_key",
    "sa_key",
    "sa_trust",
    "enforcement",
    "babbling_ports",
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
    # sa_key, sa_trust, enforcement, babbling_ports, or all or none.
    # [opensm_conf] overrides any single option.
    hardening: str = "all"

    # [opensm_conf]: opensm.conf options, as key = value (managed)
    opensm_conf: dict = dataclasses.field(default_factory=dict)

    @property
    def hardening_groups(self):
        """The hardening groups that [opensm] hardening names"""
        words = {w.strip().lower() for w in self.hardening.split(",") if w.strip()}
        if words & set(ALL_HARDENING):
            return set(HARDENING_GROUPS)
        return words - set(NO_HARDENING)

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
        words = {w.strip().lower() for w in self.hardening.split(",") if w.strip()}
        unknown = words - set(HARDENING_GROUPS) - set(ALL_HARDENING) - set(NO_HARDENING)
        if unknown:
            raise ConfigError(
                f"[opensm] hardening has unknown groups: {', '.join(sorted(unknown))}"
            )
        if len(words) > 1 and words & (set(ALL_HARDENING) | set(NO_HARDENING)):
            raise ConfigError(
                "[opensm] hardening is either all, none, or a list of groups"
            )
        for name in ("start_timeout", "sweep_timeout", "check_interval", "job_timeout"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"[opensm] {name} must be positive")
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
        managed = set(self.opensm_conf) & set(MANAGED_OPENSM_KEYS)
        if managed:
            raise ConfigError(
                f"[opensm_conf] can't set {', '.join(sorted(managed))}, "
                "which the manager sets"
            )
        for key, value in self.opensm_conf.items():
            if not key.replace("_", "").isalnum() or "\n" in value:
                raise ConfigError(f"[opensm_conf] {key} isn't an OpenSM option")


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
    apply_bound = config.sweep_timeout + 5 + 5 * config.call_timeout + 10
    takeover = (
        apply_bound
        + config.lease_timeout
        + config.ha_period
        + config.reach_timeout
        + 5
        + config.watchdog_timeout
        + config.start_timeout
        + 5
    )
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
            "start_timeout",
            "sweep_timeout",
            "check_interval",
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
