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

"""Ways to make OpenSM read a new partitions file"""


class ReloadError(Exception):
    """OpenSM didn't confirm that it applied the partitions file"""


class NoReload:
    """Only write the file, for tests and for reloading by other means"""

    def reload(self):
        pass


def make_reloader(config):
    if config.reload == "docker":
        from alternative_fabric_manager.drivers import docker

        return docker.DockerReloader(config.container, config.sweep_timeout)
    return NoReload()
