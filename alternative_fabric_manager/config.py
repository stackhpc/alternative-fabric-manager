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

import configparser
import dataclasses
import os

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
    database: str = "/var/lib/alternative-fabric-manager/afm.sqlite"

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
            unknown = set(parser.options(section)) - set(names)
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

    def validate(self):
        if not self.token:
            raise ConfigError("[api] token must be set")
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


OPTIONS = (
    ("api", ("host", "port", "url_prefix", "domain", "token")),
    ("database", ("database",)),
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
)
