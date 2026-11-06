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

"""What a job needs to hold in a swept partitions file before it completes

A job's effect is stated relative to its own network or port, by the
attribution of a swept file's partitions and memberships to the objects they
come from (partitions.attribute()). A delete therefore completes once a sweep
of a state at or after it has nothing from its object, whatever else holds
the same pkey or GUID by then, and a port's create can only complete once
OpenSM has swept a membership of that very port.
"""

import json


def network_present(network_id, pkey):
    """A network's create or update: its partition, with its current pkey,
    or, for a flat network (pkey None), nothing beyond the generation"""
    if pkey is None:
        return {"kind": "flat_network", "network": network_id, "guids": []}
    return {"kind": "network", "network": network_id, "pkey": pkey, "guids": []}


def network_absent(network_id, port_ids, guids):
    """A network's deletion: no partition of it, nor membership of its ports,
    which are recorded now, since the deletion removes them"""
    return {
        "kind": "no_network",
        "network": network_id,
        "ports": sorted(port_ids),
        "guids": sorted(guids),
    }


def port_present(port_id, pkey, guid):
    """A port's create or update: a full membership of it in its network's
    partition, pkey, and no other membership of it"""
    return {"kind": "port", "port": port_id, "pkey": pkey, "guids": [guid]}


def port_absent(port_id, guid=None):
    """A port's deletion: no membership of it"""
    return {"kind": "no_port", "port": port_id, "guids": [guid] if guid else []}


def dumps(effect):
    return json.dumps(effect, sort_keys=True)


def loads(text):
    return json.loads(text or "{}")


def holds(effect, attribution):
    """Check whether an effect holds in a swept file's attribution"""
    kind = effect.get("kind")
    partitions = attribution.get("partitions", {})
    members = attribution.get("members", {})
    if kind == "network":
        return partitions.get(str(effect["pkey"])) == effect["network"]
    if kind == "flat_network":
        return True
    if kind == "no_network":
        return effect["network"] not in partitions.values() and not any(
            members.get(port_id) for port_id in effect["ports"]
        )
    if kind == "port":
        return members.get(effect["port"]) == [[effect["pkey"], "full"]]
    if kind == "no_port":
        return not members.get(effect["port"])
    return False
