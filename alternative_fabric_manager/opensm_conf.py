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

"""OpenSM's options file: the manager's options, the hardened options and
the operator's overrides

docs/opensm-hardening.md explains each hardened option.
"""

import secrets as secrets_mod

# Where OpenSM's container sees the configuration directory
CONTAINER_CONFIG_DIR = "/etc/opensm"

# Keys that every OpenSM of the fabric must share. The manager generates them
# once and keeps them in its database.
#
# No group writes key_mgr_seed yet. OpenSM derives the class keys (VS, CC,
# N2N) from it, and with leases of 0 a lost seed leaves ports keyed with keys
# no SM can reproduce. So the manager stores it, if it's missing, whenever it
# starts OpenSM (in HA mode, at a takeover), a release before any group can
# key a port with it, and every backup of the database holds it by then (see
# specs/secured-profile.md, The seed).
#
# Per-port M_Keys derive from m_key_base, not from m_key, which every port
# holds today and tenants may have read: deriving from m_key would hand such a
# tenant every port's key. It is stored, if it's missing, whatever the groups,
# as the seed is, so that every backup holds it a release before any port is
# keyed from it (see specs/secured-profile.md, Per-port M_Keys). It's written
# into opensm.conf only as the m_key line, and only while per-port keys are
# on (render's port_keys): OpenSM derives each port's key from that line.
KEYS = ("m_key", "sm_key", "sa_key", "key_mgr_seed", "m_key_base")

# The hardened options, in groups that [opensm] hardening names. A group's
# key, if it has one, is generated once and kept in the database.
HARDENING = {
    # Only an SM with the M_Key can read or change a port's settings, with
    # SubnGet or SubnSet. With no lease, a port never clears its protection,
    # however long no SM runs: otherwise a host that sends wrong M_Keys while
    # every SM is down, as the manager's fencing can make them, would get the
    # port's protection, and its M_Key, back.
    "m_key": {
        "m_key": None,
        "m_key_protection_level": "2",
        "m_key_lease_period": "0",
    },
    # Only the fabric's own SMs can hand over to each other.
    "sm_key": {"sm_key": None},
    # Clients with the SA_Key are trusted by the SA.
    "sa_key": {"sa_key": None},
    # Untrusted clients get only a minimal set of SA requests, can't act for
    # other ports or add alias GUIDs, and can register only so much.
    "sa_trust": {
        "sa_enhanced_trust_model": "TRUE",
        "sa_etm_allow_untrusted_proxy_requests": "FALSE",
        "sa_etm_allow_untrusted_guidinfo_rec": "FALSE",
        "sa_etm_allow_guidinfo_rec_by_vf": "FALSE",
        "sa_etm_max_num_mcgs": "128",
        "sa_etm_max_num_srvcs": "32",
        "sa_etm_max_num_event_subs": "32",
    },
    # Switches drop packets of partitions their ports aren't in, both ways,
    # and clients can't claim other ports' GIDs. OpenSM's defaults, written so
    # that they show.
    "enforcement": {
        "part_enforce": "both",
        "allow_both_pkeys": "FALSE",
        "sa_check_sgid_spoofing": "TRUE",
    },
    # Ports that flood the SM with traps are disabled.
    "babbling_ports": {"babbling_port_policy": "TRUE"},
    # Each port gets its own M_Key, derived from the base and its GUID, so
    # that a key read from one port opens only that port. OpenSM forces
    # lookup and level 2 with the option; they're written so that they show.
    # Written only while per-port keys are on (render's port_keys), since
    # naming the group only permits them (specs/secured-profile.md, Per-port
    # M_Keys, The design).
    "m_key_per_port": {
        "m_key_per_port": "TRUE",
        "m_key_lookup": "TRUE",
        "m_key_protection_level": "2",
    },
}


def new_key():
    """Return a random, non-zero 64-bit key, as OpenSM writes them"""
    key = 0
    while not key:
        key = secrets_mod.randbits(64)
    return f"0x{key:016x}"


def is_key_lease(name):
    """Whether an option is the lease of a key OpenSM sets on ports: the
    M_Key's, and the class keys' (vs_, cc_ and n2n_key_lease_period)"""
    return name.endswith("key_lease_period")


def effective_groups(groups, port_keys=False):
    """The groups whose options are written: m_key_per_port only permits
    per-port keys, so it writes nothing unless they're on"""
    groups = set(groups)
    if not port_keys:
        groups.discard("m_key_per_port")
    return groups


def hardened_options(groups, keys, key_lease_period=0, port_keys=False):
    """Return the options of the hardening groups, with the fabric's keys,
    and key_lease_period as every key lease

    m_key_per_port's options are written only when port_keys is true, the
    target being on, and then the m_key line is the per-port M_Key base,
    which OpenSM derives each port's key from. Otherwise the group writes
    nothing, and the base is never written.
    """
    groups = effective_groups(groups, port_keys)
    options = {}
    for group in sorted(groups):
        for name, value in HARDENING[group].items():
            if value is None:
                value = keys[name]
            elif is_key_lease(name):
                value = str(key_lease_period)
            options[name] = value
    if "m_key_per_port" in groups:
        options["m_key"] = keys["m_key_base"]
    return options


def render(overrides, hardening=(), keys=None, key_lease_period=0, port_keys=False):
    """Return opensm.conf

    OpenSM uses its defaults for options that aren't set. hardening names
    the hardening groups, keys are the fabric's shared keys, and
    key_lease_period is the lease of every key they set on ports, 0 for
    permanent keys; overrides win over hardening, but not over the options
    the manager depends on. port_keys says whether per-port M_Keys are on,
    their target: while it's false, m_key_per_port renders as if it weren't
    named.
    """
    hardening = effective_groups(hardening, port_keys)
    lines = [
        "# Generated by alternative-fabric-manager; changes are overwritten.",
        "# OpenSM uses its defaults for options that aren't set here.",
        "",
        "# Set by the manager",
        f"partition_config_file {CONTAINER_CONFIG_DIR}/partitions.conf",
        "daemon FALSE",
    ]
    if "log_flags" not in overrides:
        # The levels whose messages the manager watches for
        lines.append("log_flags 0x07")
    hardened = hardened_options(hardening, keys or {}, key_lease_period, port_keys)
    hardened = {k: v for k, v in hardened.items() if k not in overrides}
    if hardened:
        lines += [
            "",
            f"# Hardening ({', '.join(sorted(hardening))}), see "
            "docs/opensm-hardening.md",
        ]
        lines += [f"{key} {value}" for key, value in sorted(hardened.items())]
    if overrides:
        lines += ["", "# From [opensm_conf]"]
        lines += [f"{key} {value}" for key, value in sorted(overrides.items())]
    return "\n".join(lines) + "\n"
