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

An effect also records what a partial fabric check judges the job by (see
specs/ha.md, While a switch is missing): whether it is a grant, which only
adds memberships or changes none, and the hosts its ports are bound to, and
for a port its binding:vnic_type. An effect without them, as a job created
before they were recorded has, is a revocation whose hosts aren't known, so
it waits for a whole fabric, and holds every host's grants meanwhile.
"""

import json


def network_present(network_id, pkey, grant=False, hosts=()):
    """A network's create or update: its partition, with its current pkey,
    or, for a flat network (pkey None), nothing beyond the generation.
    grant is whether it creates the network; hosts are those of its ports,
    whose memberships an update may revoke."""
    if pkey is None:
        effect = {"kind": "flat_network", "network": network_id, "guids": []}
    else:
        effect = {"kind": "network", "network": network_id, "pkey": pkey, "guids": []}
    effect.update(grant=bool(grant), hosts=sorted(set(hosts)))
    return effect


def network_absent(network_id, port_ids, guids, hosts=()):
    """A network's deletion: no partition of it, nor membership of its ports,
    which are recorded now, since the deletion removes them"""
    return {
        "kind": "no_network",
        "network": network_id,
        "ports": sorted(port_ids),
        "guids": sorted(guids),
        "grant": False,
        "hosts": sorted(set(hosts)),
    }


def port_present(
    port_id, pkey, guid, grant=False, host=None, old_host=None, vnic_type=None
):
    """A port's create or update: a full membership of it in its network's
    partition, pkey, and no other membership of it. grant is whether it only
    adds memberships or changes none; host is the port's host, old_host the
    one it was on before an update, both of which hosts lists."""
    return {
        "kind": "port",
        "port": port_id,
        "pkey": pkey,
        "guids": [guid],
        "grant": bool(grant),
        "host": host,
        "hosts": sorted({h for h in (host, old_host) if h is not None}),
        "vnic_type": vnic_type,
    }


def port_absent(port_id, guid=None, hosts=()):
    """A port's deletion: no membership of it"""
    return {
        "kind": "no_port",
        "port": port_id,
        "guids": [guid] if guid else [],
        "grant": False,
        "hosts": sorted({host for host in hosts if host is not None}),
    }


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
