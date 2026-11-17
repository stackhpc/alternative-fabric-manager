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

"""The SA request rate limit: which clients OpenSM limited

specs/secured-profile.md, SA request rate, is the source of truth. The
group is experimental, untested on hardware: hardware test 4 decides
whether it joins all or is removed. The manager only logs what OpenSM
dropped; it never fails a check on it, since a tenant flooding the SA would
otherwise stop every job.
"""

import logging

LOG = logging.getLogger(__name__)

GROUP = "sa_rate"
OPTION = "sa_rate_threshold"


class SARate:
    def __init__(self, manager):
        self.manager = manager
        # The GUIDs OpenSM limited, logged once each for the running OpenSM
        self._limited = set()

    @property
    def config(self):
        return self.manager.config

    def watching(self):
        """Whether OpenSM limits SA requests: the group named, or the option
        set to something but 0, with the managed driver in single node"""
        if self.config.ha or self.config.driver != "managed":
            return False
        if OPTION in self.config.opensm_conf:
            return self.config.opensm_conf[OPTION].strip() not in ("0", "0x0", "")
        return GROUP in self.config.hardening_groups

    def reset(self):
        """A new OpenSM: each GUID it limits is logged afresh"""
        self._limited.clear()

    def report(self, findings):
        for guid, port, count, threshold in findings.sa_limited:
            if guid in self._limited:
                continue
            self._limited.add(guid)
            LOG.warning(
                "OpenSM dropped SA requests from GUID 0x%016x (port 0x%x): %d in "
                "100 ms, over sa_rate_threshold %d (ERR 4C11). Further drops from "
                "it aren't logged until OpenSM restarts. If it's a legitimate "
                "client, such as an MPI job starting, raise the threshold",
                guid,
                port,
                count,
                threshold,
            )
