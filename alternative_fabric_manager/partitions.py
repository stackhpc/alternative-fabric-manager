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

"""From Neutron's networks and ports to OpenSM's partitions file"""

import os
import re
import tempfile

import jinja2
import jinja2.sandbox

# The DHCP option that carries an InfiniBand port's client ID, by name and
# by number (RFC 2132).
CLIENT_ID_OPTS = ("client-id", "61")
DEFAULT_PKEY = 0x7FFF
# A client ID is octets separated by colons, and its last 8 are the port's
# GUID, after the prefix: "20:" (mlnx_infiniband's hardware prefix), or
# "ff:00:00:00:00:00:02:00:00:02:c9:00:" (the legacy one, which Ironic's
# ramdisk also uses).
_OCTET = re.compile(r"^[0-9a-fA-F]{2}$")


class InvalidRequest(ValueError):
    """The request can't be applied, so must not be sent again"""


def pkey_from_segmentation_id(segmentation_id):
    """Return a network's partition key, which is its VLAN ID"""
    try:
        pkey = int(segmentation_id)
    except (TypeError, ValueError):
        raise InvalidRequest(
            f"provider:segmentation_id {segmentation_id!r} isn't a number"
        ) from None
    if not 1 <= pkey < DEFAULT_PKEY:
        raise InvalidRequest(
            f"provider:segmentation_id {pkey} isn't a partition key: it must be "
            "between 1 and 0x7ffe"
        )
    return pkey


def client_ids(port):
    """Return the IPv4 DHCP client IDs of a Neutron port"""
    return [
        str(opt.get("opt_value"))
        for opt in port.get("extra_dhcp_opts") or ()
        if (
            isinstance(opt, dict)
            and str(opt.get("opt_name")) in CLIENT_ID_OPTS
            and str(opt.get("ip_version", 4)) == "4"
            and opt.get("opt_value")
        )
    ]


def guid_from_port(port):
    """Return the GUID of a Neutron port, from its DHCP client ID"""
    ids = client_ids(port)
    port_id = port.get("id")
    if not ids:
        raise InvalidRequest(
            f"Port {port_id} has no DHCP client ID, which identifies its GUID"
        )
    if len(ids) > 1:
        raise InvalidRequest(f"Port {port_id} has more than one DHCP client ID")
    octets = ids[0].split(":")
    if len(octets) < 8 or not all(_OCTET.match(o) for o in octets):
        raise InvalidRequest(
            f"DHCP client ID {ids[0]!r} of port {port_id} isn't colon-separated "
            "octets ending in a GUID"
        )
    guid = int("".join(octets[-8:]), 16)
    if guid in (0, 0xFFFFFFFFFFFFFFFF):
        raise InvalidRequest(f"DHCP client ID {ids[0]!r} of port {port_id} has no GUID")
    return guid


# Renders each network's partition. Operators can replace it with their own
# template ([opensm] partitions_template), which gets the same variables:
#   default_partition  [opensm] default_partition, as is
#   networks           each with id, name, pkey (an int), pkey_hex and guids
#                      (a sorted list of "0x..." strings)
#   ipoib              rate, mtu and sl, from [opensm] ipoib_*
#   generation         the state's generation
# Whatever it renders is checked by validate() before it is written.
DEFAULT_TEMPLATE = """\
{{ default_partition }}
{% for network in networks -%}
{{ network.name }}={{ network.pkey_hex }}, ipoib, rate={{ ipoib.rate }}, \
mtu={{ ipoib.mtu }}, sl={{ ipoib.sl }} : \
{% for guid in network.guids %}{{ guid }}=full{{ ", " if not loop.last else "" }}\
{% endfor %} ;
{% endfor %}"""


class InvalidPartitions(ValueError):
    """A rendered partitions file that would break the networks' isolation"""


def load_template(path=""):
    """Return the partitions template, the operator's or the default"""
    env = jinja2.sandbox.SandboxedEnvironment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False
    )
    if not path:
        return env.from_string(DEFAULT_TEMPLATE)
    with open(path) as f:
        return env.from_string(f.read())


def render(config, networks, generation, template):
    """Return the partitions file for networks, a list of
    (network, [port GUID]) pairs"""
    context = {
        "default_partition": config.default_partition.strip(),
        "ipoib": {
            "rate": config.ipoib_rate,
            "mtu": config.ipoib_mtu,
            "sl": config.ipoib_sl,
        },
        "generation": generation,
        "networks": [
            {
                "id": network.id,
                "name": f"net-{network.id}",
                "pkey": network.pkey,
                "pkey_hex": f"0x{network.pkey:04x}",
                "guids": [f"0x{guid:016x}" for guid in sorted(set(guids))],
            }
            for network, guids in sorted(networks, key=lambda n: n[0].pkey)
        ],
    }
    try:
        body = template.render(context)
    except jinja2.TemplateError as e:
        raise InvalidPartitions(f"The partitions template failed: {e}") from e
    # The header is the manager's, not the template's: the generation makes
    # the file change on every apply, so that OpenSM always runs a pkey sweep
    # when it gets SIGHUP.
    header = (
        "# Generated by alternative-fabric-manager; changes are overwritten.\n"
        f"# Generation {generation}\n"
    )
    content = header + body
    if not content.endswith("\n"):
        content += "\n"
    return content


_KEYWORDS = {"ALL", "ALL_CAS", "ALL_VCAS", "ALL_SWITCHES", "ALL_ROUTERS", "SELF"}
_MEMBERSHIPS = {"full", "limited", "both"}


def _parse(content):
    """Return the partition statements of a partitions file, as
    (pkey, {member: membership}) pairs, following OpenSM's syntax"""
    text = "\n".join(line.split("#", 1)[0] for line in content.splitlines())
    statements = []
    for statement in text.split(";"):
        if not statement.strip():
            continue
        if ":" not in statement:
            raise InvalidPartitions(f"No ':' in partition {statement.strip()!r}")
        definition, properties = statement.split(":", 1)
        name, *flags = (f.strip() for f in definition.split(","))
        if "=" not in name:
            raise InvalidPartitions(
                f"Partition {name!r} has no pkey; give every partition one"
            )
        name, pkey = (part.strip() for part in name.split("=", 1))
        try:
            pkey = int(pkey, 0) & DEFAULT_PKEY
        except ValueError:
            raise InvalidPartitions(f"Partition {name!r} has pkey {pkey!r}") from None
        default = "limited"
        for flag in flags:
            if flag.startswith("defmember="):
                default = flag.split("=", 1)[1].strip()
        members = {}
        for line in properties.splitlines():
            line = line.strip()
            if not line or line.startswith("mgid="):
                # A multicast group, whose flags are separated by commas too
                continue
            for spec in line.split(","):
                spec = spec.strip()
                if not spec:
                    continue
                member, _, membership = spec.partition("=")
                membership = membership.strip() or default
                if membership not in _MEMBERSHIPS:
                    # OpenSM takes an unrecognised membership as limited.
                    membership = "limited"
                member = member.strip()
                if member not in _KEYWORDS:
                    try:
                        member = int(member, 0)
                    except ValueError:
                        raise InvalidPartitions(
                            f"Partition {name!r} has member {member!r}"
                        ) from None
                members[member] = membership
        statements.append((pkey, members))
    return statements


def validate(content, expected):
    """Check a rendered partitions file against the networks' membership

    expected maps each network's pkey to its ports' GUIDs. Every network's
    pkey must have exactly those GUIDs as full members, no keyword and no
    other member, and no other partition may have one of the networks' ports
    as a member. The default partition must be defined.
    """
    found = {}
    for pkey, members in _parse(content):
        found.setdefault(pkey, {}).update(members)
    if DEFAULT_PKEY not in found:
        raise InvalidPartitions("The default partition (0x7fff) isn't defined")
    managed = set().union(*expected.values()) if expected else set()
    for pkey, guids in expected.items():
        members = found.get(pkey)
        if members is None:
            raise InvalidPartitions(f"Network partition 0x{pkey:04x} is missing")
        wanted = {guid: "full" for guid in guids}
        if members != wanted:
            raise InvalidPartitions(
                f"Partition 0x{pkey:04x} must have exactly its ports as full members"
            )
    for pkey, members in found.items():
        if pkey in expected or pkey == DEFAULT_PKEY:
            continue
        if managed & set(members):
            raise InvalidPartitions(
                f"Partition 0x{pkey:04x} isn't a network's, but has a "
                "network's port as a member"
            )


def write_atomically(path, content):
    """Replace a file, so that OpenSM never reads half of it"""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".partitions.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
