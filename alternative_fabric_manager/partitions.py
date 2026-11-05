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


# Neutron's IDs are UUIDs; anything else could change the partitions file's
# syntax, since a network's ID is in its partition's name.
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_id(object_id):
    if not isinstance(object_id, str) or not _ID.match(object_id):
        raise InvalidRequest(f"{object_id!r} isn't a valid ID")


def pkey_from_network(network):
    """Return a network's partition key, which is its VLAN ID, or None for a
    flat network, which uses the default partition"""
    network_type = network.get("provider:network_type")
    if network_type == "flat":
        return None
    if network_type not in ("vlan", None):
        raise InvalidRequest(
            f"Network {network.get('id')} is a {network_type} network, which has "
            "no partition"
        )
    return pkey_from_segmentation_id(network.get("provider:segmentation_id"))


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
#   networks           each VLAN network, with id, name, pkey (an int),
#                      pkey_hex and guids (a sorted list of "0x..." strings)
#   flat               the flat networks' partition, with the same keys:
#                      pkey and pkey_hex are [opensm] flat_pkey, and guids
#                      those of every flat network's ports
#   ipoib              rate, mtu and sl, from [opensm] ipoib_*, and flags,
#                      those as partition flags: "ipoib, rate=3, mtu=4, sl=0"
#   generation         the state's generation
# Whatever it renders is checked by validate() before it is written.
#
# indx0 puts a network's pkey at index 0 of its ports' pkey tables. A guest's
# IPoIB interface uses the pkey at index 0, so without it the interface is in
# the default partition, not the network's. OpenSM reads a partition's
# definition and members from one line, into a fixed buffer, so a partition
# with many members is written as several definitions of the same name and
# pkey, which OpenSM merges, of at most 20 members each.
DEFAULT_TEMPLATE = """\
{{ default_partition }}
{% for network in networks + ([flat] if flat.guids else []) -%}
{% if network.pkey == 0x7fff -%}
{% set head = "Default=0x7fff" -%}
{% set flags = "" -%}
{% else -%}
{% set head = network.name ~ "=" ~ network.pkey_hex -%}
{% set flags = ", indx0, " ~ ipoib.flags -%}
{% endif -%}
{% for chunk in (network.guids | batch(20) | list) or [[]] -%}
{{ head }}{{ flags if loop.first else "" }} : \
{% for guid in chunk %}{{ guid }}=full{{ ", " if not loop.last else "" }}{% endfor %} ;
{% endfor -%}
{% endfor %}"""

# OpenSM reads the partitions file a line at a time, into a fixed buffer.
MAX_LINE = 1000


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


def _partition(name, pkey, guids):
    return {
        "name": name,
        "pkey": pkey,
        "pkey_hex": f"0x{pkey:04x}",
        "guids": [f"0x{guid:016x}" for guid in sorted(set(guids))],
    }


def render(config, networks, generation, template):
    """Return the partitions file for networks, a list of
    (network, [port GUID]) pairs, flat networks' pkey being None"""
    flat = {
        guid for network, guids in networks if network.pkey is None for guid in guids
    }
    networks = [(n, guids) for n, guids in networks if n.pkey is not None]
    context = {
        "default_partition": config.default_partition.strip(),
        "ipoib": {
            "rate": config.ipoib_rate,
            "mtu": config.ipoib_mtu,
            "sl": config.ipoib_sl,
            "flags": (
                f"ipoib, rate={config.ipoib_rate}, mtu={config.ipoib_mtu}, "
                f"sl={config.ipoib_sl}"
            ),
        },
        "generation": generation,
        "flat": _partition("flat", config.flat_pkey, flat),
        "networks": [
            dict(_partition(f"net-{network.id}", network.pkey, guids), id=network.id)
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
# The keywords that take in tenants' ports
_TENANT_KEYWORDS = {"ALL", "ALL_CAS", "ALL_VCAS"}
_MEMBERSHIPS = {"full", "limited", "both"}
_GUID = re.compile(r"^(0x[0-9a-fA-F]{1,16}|[1-9][0-9]{0,19})$")
_PKEY = re.compile(r"^(0x[0-9a-fA-F]{1,4}|[0-9]{1,5})$")


def _parse(content):
    """Return the partition statements of a partitions file, as
    (pkey, {member: membership}) pairs, following OpenSM's syntax"""
    for number, line in enumerate(content.splitlines(), 1):
        if len(line) > MAX_LINE:
            raise InvalidPartitions(
                f"Line {number} is longer than {MAX_LINE} characters; split the "
                "partition into several definitions with the same name and pkey"
            )
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
        if not _PKEY.match(pkey):
            raise InvalidPartitions(f"Partition {name!r} has pkey {pkey!r}")
        pkey = int(pkey, 0) & DEFAULT_PKEY
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
                    if not _GUID.match(member):
                        raise InvalidPartitions(
                            f"Partition {name!r} has member {member!r}"
                        )
                    member = int(member, 0)
                members[member] = membership
        statements.append((pkey, members))
    return statements


def pkeys(content):
    """Return the pkeys that a partitions file defines"""
    return {pkey for pkey, _members in _parse(content)}


def validate(
    content,
    expected,
    flat=frozenset(),
    flat_pkey=DEFAULT_PKEY - 1,
    shared_default=False,
):
    """Check a rendered partitions file against the networks' membership

    expected maps each VLAN network's pkey to its ports' GUIDs, and flat holds
    the GUIDs of ports on flat networks, whose partition is flat_pkey. Every
    network's partition, and the flat networks', must have exactly its GUIDs
    as full members, and no keyword. A GUID may be in only one of them, and no
    other partition may have one of them as a member. No partition other than
    a network's may have ALL, ALL_CAS or ALL_VCAS as full members, except the
    default partition with shared_default. The default partition must be
    defined.
    """
    found = {}
    for pkey, members in _parse(content):
        found.setdefault(pkey, {}).update(members)
    if DEFAULT_PKEY not in found:
        raise InvalidPartitions("The default partition (0x7fff) isn't defined")
    expected = {pkey: set(guids) for pkey, guids in expected.items()}
    if flat_pkey != DEFAULT_PKEY and (flat or flat_pkey in found):
        if flat_pkey in expected:
            raise InvalidPartitions(f"Flat partition 0x{flat_pkey:04x} is a network's")
        expected[flat_pkey] = set(flat)
    managed = {}
    for pkey, guids in expected.items():
        for guid in guids:
            if guid in managed:
                raise InvalidPartitions(
                    f"Port 0x{guid:016x} is in partitions 0x{managed[guid]:04x} and "
                    f"0x{pkey:04x}"
                )
            managed[guid] = pkey
    if flat_pkey == DEFAULT_PKEY:
        for guid in flat:
            if guid in managed:
                raise InvalidPartitions(
                    f"Port 0x{guid:016x} is on a flat network and in partition "
                    f"0x{managed[guid]:04x}"
                )
    for pkey, members in found.items():
        full = {k for k in _TENANT_KEYWORDS if members.get(k) in ("full", "both")}
        if full and not (pkey == DEFAULT_PKEY and shared_default):
            raise InvalidPartitions(
                f"Partition 0x{pkey:04x} has {', '.join(sorted(full))} as full "
                "members, which would share it with every tenant"
            )
    for pkey, guids in expected.items():
        members = found.get(pkey)
        if members is None:
            raise InvalidPartitions(f"Network partition 0x{pkey:04x} is missing")
        if members != {guid: "full" for guid in guids}:
            raise InvalidPartitions(
                f"Partition 0x{pkey:04x} must have exactly its ports as full members"
            )
    default = found[DEFAULT_PKEY]
    if flat_pkey == DEFAULT_PKEY:
        everyone = any(default.get(k) in ("full", "both") for k in ("ALL", "ALL_CAS"))
        for guid in flat:
            if not everyone and default.get(guid) not in ("full", "both"):
                raise InvalidPartitions(
                    f"Port 0x{guid:016x} of a flat network must be a full member of "
                    "the default partition"
                )
    for pkey, members in found.items():
        if pkey in expected:
            continue
        others = set(managed) & set(members)
        if others:
            raise InvalidPartitions(
                f"Partition 0x{pkey:04x} isn't a network's, but has a network's "
                f"port 0x{min(others):016x} as a member"
            )


def write_atomically(path, content, mode=0o644):
    """Replace a file, so that OpenSM never reads half of it

    The file is created with mode, so that it is never readable by others
    before it should be.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".afm.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
