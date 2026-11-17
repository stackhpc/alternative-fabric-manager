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

"""Ways to run OpenSM and make it read a new partitions file

A driver has:
- start(): start OpenSM, once its partitions file is written, and return
  once it is running: a SweepReport if it saw OpenSM's first sweep, which
  read that file, or None if it can't tell;
- running(): whether OpenSM is still running;
- reload(): make OpenSM apply the partitions file, and return a SweepReport
  once it has; raise PartitionsRejected if it couldn't parse it;
- fabric(): return a Fabric, or None if there is no fabric to check; given
  switches, the switch GUIDs the prober found, it doesn't look for them;
- stop(): stop OpenSM, if the driver started it.

The managed driver's start() also takes prepare, called with the created
container before OpenSM starts in it, and it has read_cache(),
write_cache() and prober() for per-port M_Keys, and physical_ports(),
log_findings() and query_sm_port() for allowed SMs
(specs/secured-profile.md).
"""


class ReloadError(Exception):
    """OpenSM didn't confirm that it applied the partitions file"""


class PartitionsRejected(ReloadError):
    """OpenSM couldn't parse a line of the partitions file"""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


# An error that names no port it can be traced to
UNATTRIBUTED = "unattributed"


class SweepReport:
    """What OpenSM reported, while applying a partitions file, about ports
    whose pkey tables it couldn't set"""

    def __init__(
        self,
        pkey_errors=(),
        enforcement_errors=(),
        unattributed=False,
        dr_errors=False,
        unreached=(),
        bad_m_key_traps=0,
    ):
        # Port GUIDs, as ints
        self.pkey_errors = set(pkey_errors)
        # Whether some pkey error named no port GUID
        self.unattributed = unattributed
        # Switch ports whose partition enforcement OpenSM cleared, as
        # "<switch GUID>:<port>", or UNATTRIBUTED
        self.enforcement_errors = set(enforcement_errors)
        # Whether a directed-route SMP failed to send (ERR 5411), which
        # names no port
        self.dr_errors = dr_errors
        # The dest_guids of NodeInfo and PortInfo Gets that failed (ERR
        # 3113), as ints, 0 included
        self.unreached = set(unreached)
        # How many Bad M_Key traps OpenSM received: only counted, since its
        # log of one holds the key that was sent
        self.bad_m_key_traps = bad_m_key_traps


class LogFindings:
    """What OpenSM logged since the driver last looked, that a fabric check
    judges, in the order logged:
    - ignored_sms: the port GUIDs of the SMs it ignored (ERR 0F09), as ints;
    - key_rejections: ports that answered a class key manager's KeyInfo with
      an error status (ERR 9828, C10A), as (code, port GUID, status,
      attribute ID);
    - key_errors: a key manager's failures to set keys, as (code, port GUID
      or None)"""

    def __init__(self, ignored_sms=(), key_rejections=(), key_errors=()):
        self.ignored_sms = list(ignored_sms)
        self.key_rejections = list(key_rejections)
        self.key_errors = list(key_errors)


class Fabric:
    """What a fabric check sees: switch GUIDs, and the subnet managers the SA
    knows, as (port GUID, state) pairs, state 3 being master; and the GUIDs
    of the ports of OpenSM's host, one of which it is bound to"""

    def __init__(self, switches, sms, own_guids):
        self.switches = set(switches)
        self.sms = list(sms)
        self.own_guids = set(own_guids)


class NoOpenSM:
    """Only write the files, for tests and for reloading by other means"""

    def start(self):
        return None

    def running(self):
        return True

    def reload(self):
        return SweepReport()

    def fabric(self, m_key=None, sa_key=None, switches=None):
        return None

    def stop(self):
        pass


def make_driver(config):
    if config.driver == "managed":
        from alternative_fabric_manager.drivers import docker

        return docker.ManagedDocker(config)
    if config.driver == "docker":
        from alternative_fabric_manager.drivers import docker

        return docker.DockerReloader(config.container, config.sweep_timeout)
    return NoOpenSM()
