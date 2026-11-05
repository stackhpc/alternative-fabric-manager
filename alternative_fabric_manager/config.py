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

DEFAULT_PARTITION = "Default=0x7fff, ipoib, rate=2, mtu=4, sl=0 : ALL=full, SELF=full ;"


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
    partitions_file: str = "/etc/opensm/partitions.conf"
    # How OpenSM is told to read the file: "docker" sends SIGHUP to a
    # container and waits for the pkey sweep in its log, "none" only writes
    # the file (for tests).
    reload: str = "docker"
    container: str = "opensm"
    # Seconds to wait for OpenSM to finish the pkey sweep.
    sweep_timeout: float = 30.0
    # Times to try applying a change before its jobs fail.
    apply_attempts: int = 3
    # The rule for the default partition, written as is. All flat networks
    # share it. Keep ALL=full while ports on flat networks must reach each
    # other; otherwise give ports limited membership.
    default_partition: str = DEFAULT_PARTITION
    # IPoIB broadcast group settings for networks' partitions, as IBTA
    # values (rate 3 = 10 Gb/s, mtu 4 = 2048).
    ipoib_rate: int = 3
    ipoib_mtu: int = 4
    ipoib_sl: int = 0

    @classmethod
    def load(cls, path):
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path):
            raise ConfigError(f"Can't read configuration file {path}")
        config = cls()
        for section, names in (
            ("api", ("host", "port", "url_prefix", "domain", "token")),
            ("database", ("database",)),
            (
                "opensm",
                (
                    "partitions_file",
                    "reload",
                    "container",
                    "sweep_timeout",
                    "apply_attempts",
                    "default_partition",
                    "ipoib_rate",
                    "ipoib_mtu",
                    "ipoib_sl",
                ),
            ),
        ):
            if not parser.has_section(section):
                continue
            for name in names:
                if not parser.has_option(section, name):
                    continue
                kind = type(getattr(config, name))
                value = parser.get(section, name)
                try:
                    setattr(config, name, kind(value))
                except ValueError:
                    raise ConfigError(
                        f"[{section}] {name} must be a {kind.__name__}, not {value!r}"
                    ) from None
        config.validate()
        return config

    def validate(self):
        if not self.token:
            raise ConfigError("[api] token must be set")
        if self.reload not in ("docker", "none"):
            raise ConfigError("[opensm] reload must be docker or none")
        if not self.default_partition.strip().endswith(";"):
            raise ConfigError("[opensm] default_partition must end with ;")
        if self.apply_attempts < 1:
            raise ConfigError("[opensm] apply_attempts must be at least 1")
