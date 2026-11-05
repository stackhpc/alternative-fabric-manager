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
- reload(): make OpenSM apply the partitions file, and return once it has;
- stop(): stop OpenSM, if the driver started it.
"""


class ReloadError(Exception):
    """OpenSM didn't confirm that it applied the partitions file"""


class NoOpenSM:
    """Only write the files, for tests and for reloading by other means"""

    def start(self):
        pass

    def running(self):
        return True

    def reload(self):
        pass

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
