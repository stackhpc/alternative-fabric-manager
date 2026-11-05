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
  once it is running;
- running(): whether OpenSM is still running;
- reload(): make OpenSM apply the partitions file, and return a SweepReport
  once it has; raise PartitionsRejected if it couldn't parse it;
- fabric(): return a Fabric, or None if there is no fabric to check;
- stop(): stop OpenSM, if the driver started it.
"""


class ReloadError(Exception):
    """OpenSM didn't confirm that it applied the partitions file"""


class PartitionsRejected(ReloadError):
    """OpenSM couldn't parse a line of the partitions file"""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


class SweepReport:
    """What OpenSM reported, while applying a partitions file, about ports
    whose pkey tables it couldn't set"""

    def __init__(self, pkey_errors=(), enforcement_errors=()):
        # GUIDs, as ints
        self.pkey_errors = set(pkey_errors)
        # Switch ports whose partition enforcement OpenSM cleared, as
        # "<switch GUID>:<port>"
        self.enforcement_errors = set(enforcement_errors)


class Fabric:
    """What a fabric check sees: switch GUIDs, and the subnet managers the SA
    knows, as (port GUID, state) pairs, state 3 being master; and the GUID
    of OpenSM's own port"""

    def __init__(self, switches, sms, own_guid):
        self.switches = set(switches)
        self.sms = list(sms)
        self.own_guid = own_guid


class NoOpenSM:
    """Only write the files, for tests and for reloading by other means"""

    def start(self):
        pass

    def running(self):
        return True

    def reload(self):
        return SweepReport()

    def fabric(self, m_key=None, sa_key=None):
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
