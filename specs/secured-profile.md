# The secured cloud profile

Status: proposed, 2026-11-13.

UFM's Secured Cloud Profile (`cloud_profile = 2` in UFM Enterprise 6.26)
is NVIDIA's list of protections for a fabric that bare metal tenants share.
AFM's hardening (docs/opensm-hardening.md) already covers most of it. This
spec adds the rest that OpenSM 5.17 can do, as new hardening groups, and
says why the remaining items are left out.

## Where AFM stands

| Profile item | AFM today | This spec |
| --- | --- | --- |
| Default partition membership limited | `ALL=limited, SELF=full` | — |
| No duplicate pkey at index 0 | A port's GUID is in only one network's partition | — |
| Restrictive pkey posture | No full `ALL`, `ALL_CAS` or `ALL_VCAS` outside a network's partition | — |
| MKEY | `m_key`: protection level 2, no lease | — |
| SMKEY, SAKEY | `sm_key`, `sa_key` | — |
| Enhanced SA trust model with registration limits | `sa_trust` | — |
| Allowed SM GUIDs | None | `allowed_sm` group |
| VSKEY, CCKEY, N2N key | None | `class_keys` group |
| MAD limiter | `babbling_port_policy` limits traps only | `sa_rate` group |
| MFT with M_Key mandatory | None | Documented |
| Keys from a periodically randomised seed | Keys never change | Not adopted (see Not adopted) |
| SHARP job keys, service keys | None | Not adopted |
| All HCAs in the same pkey | Not applicable | Not adopted |

## Allowed SM GUIDs

### Why

A host that runs a subnet manager takes part in the SM election. The SM_Key
only protects a handover, and the M_Key stops a rogue SM setting ports. But
a tenant's SM with a higher priority, or an equal priority and a lower port
GUID, can still win the election, and send AFM's OpenSM to STANDBY. The
fabric is then left without a working SM. New ports don't come up, partition
changes aren't applied, and the fabric check fails, so no job completes.
That is a denial of service that any bare metal tenant can cause.

With `allowed_sm_guids`, OpenSM ignores every SM whose port isn't in the
list. It logs `ERR 0F09: Ignoring SM on port 0x..., is not part of the
allowed port GUIDs`, and drops an SMInfo Set from such a port with
`ERR 2F1C`.

### The group

A new hardening group, `allowed_sm`, sets one option:

```
allowed_sm_guids 0x<guid>,0x<guid>,...
```

The list holds the port GUIDs of every InfiniBand port on every host where
AFM may run OpenSM:

- **Single node:** the host's own ports. Before rendering `opensm.conf`,
  the manager reads them with `ibstat -p` in a short-lived container of
  OpenSM's image. That is how HA mode's reach probe already runs
  diagnostics before OpenSM starts. As in the fabric check's `own_guids`,
  it drops the placeholder GUIDs that unassigned virtual functions report
  (`0` and all ones). Every port is listed, because OpenSM may bind to any
  of them unless `guid` is set.
- **HA:** the union of the port GUIDs that every node row in the cluster
  records, live or not, retired nodes excluded. Each node records its own
  ports' GUIDs in its node row when it registers, with the same probe (see
  Changes to specs/ha.md). Every OpenSM a leader might hand over to, or ask
  to stop, is then in every other leader's list.

`[opensm_conf] allowed_sm_guids` overrides the generated list, as with any
hardened option. The `0` form ("do not allow any other SM") isn't used:
in HA mode, a leader's OpenSM must honour the next leader's fence ask (an
SMInfo Set, which `ERR 2F1C` would drop), and must see the next leader's SM
as a managed node's.

### Refusing a list that would lock AFM out

If the list leaves out the port that OpenSM binds to, the fabric could
have no SM. Before writing `opensm.conf`, the manager refuses to start
OpenSM, raising an alarm and reporting OpenSM as not running, when any of
these hold:

- the generated list is empty, because the probe found no ports;
- `[opensm_conf] guid` is set, and isn't in the list;
- in HA mode, this node's own row has no port GUIDs.

Whether OpenSM itself refuses to run on a port that isn't in the list is
part of the hardware test.

### Changing the list

The list changes when a node joins, is retired, or has an adapter replaced.
The leader re-renders `opensm.conf` whenever the list it would generate
changes. NVIDIA's documentation says `allowed_sm_guids` can change without
restarting OpenSM, and the binary has `osm_update_allowed_sm_guids`. Whether
a SIGHUP applies the change is part of the hardware test. Until that test
passes, the leader moves the leadership to apply it, as for a
fabric-affecting change.

The list is derived from node rows, not configured, so it is left out of
the configuration hash, like other node-specific options.

### The fabric check

An SM that OpenSM ignores may no longer appear in the SA's SMInfo records,
which the fabric check reads. So the check also matches `ERR 0F09` in
OpenSM's log since the last check. As with an unknown SM today, that raises
an alarm and doesn't fail the check, because OpenSM is ignoring the other
SM. An unknown SM that is master, or AFM's OpenSM going to STANDBY, still
fails the check.

## Class keys

### Why

Besides SMPs, which the M_Key protects, switches and adapters accept
management datagrams of other classes:

- **vendor-specific (VS):** NVIDIA's in-band configuration and diagnostics,
  which `mlxlink`, `mlxreg` and in-band `flint` use;
- **congestion control (CC):** the congestion control parameters of every
  port;
- **node-to-node (N2N, class C):** NVIDIA's node-to-node class, which UFM's
  profile also keys.

Unless a class is keyed, a bare metal tenant can send these to any switch.
With VS MADs, it could reconfigure or degrade switch ports. With CC MADs, it
could throttle other tenants' traffic.

### The group

A new hardening group, `class_keys`, sets:

```
vs_key_enable 2
vs_key_ci_protect_bits 1
vs_key_lease_period 0
cc_key_enable 2
cc_key_protect_bit 1
cc_key_lease_period 0
n2n_key_enable 2
n2n_key_protect_bit 1
n2n_key_lease_period 0
key_mgr_seed <generated>
```

OpenSM derives each port's VS, CC and N2N keys from `key_mgr_seed`, or from
the M_Key when the seed is 0. AFM generates a seed of its own, random,
64-bit and non-zero, and keeps it in the database with the other keys. A
leaked M_Key then doesn't give away the class keys. The `secrets` table
holds keys by name, so `key_mgr_seed` needs no schema change. The manager
generates it only when `class_keys` is chosen.

The leases are 0 for the same reason as the M_Key's (see
docs/opensm-hardening.md, The lease). AFM stops OpenSM during restarts and
takeovers, and a lease that ran out while no SM ran would let a host clear
a port's protection. The hardware test checks that a lease of 0 means "never
expires" for these classes, as it does for the M_Key. If it doesn't, the
group uses the longest lease OpenSM accepts, and the doc says so.

### HA

Every leader must derive the same keys for every port, or a new leader
couldn't configure ports that its predecessor keyed. That holds only if
OpenSM derives a port's keys from the seed and the port alone, not from
state that's cached on one node. OpenSM caches M_Keys in `guid2mkey`, and
the binary has `key_mgr_get_key_db`. The two-OpenSM hardware test checks
this. Until it passes, HA mode refuses `class_keys`.

### Turning the keys off

As with the M_Key, ports keep their keys until an SM that has them changes
them. To turn a class off, set `<class>_key_enable 1` (Disable) in
`[opensm_conf]` and restart. OpenSM then clears the keys from the ports.
Only then drop `class_keys`. Dropping it at once sets the class's enable to
0 (Ignore), which leaves ports protected by keys that the SM no longer
manages.

### Cost

In-band tools that send VS MADs, such as `mlxlink`, `mlxreg`, in-band
`flint`, `ibdiagnet`, and in-band switch firmware upgrades, need the VS key.
Which of them can take it, and how, is an open question. The doc's
Diagnostics section will list them once it's answered. AFM itself sends
only SMPs and SA requests, so its fabric check, reach probe and fence ask
are unaffected.

## SA request rate

UFM's MAD limiter is a UFM feature, and its documentation doesn't say which
OpenSM options it uses. The nearest OpenSM option is `sa_rate_threshold`:
the most SA requests that one port may send per 100 ms. OpenSM drops the
rest, logging `ERR 4C11: Number of sent SA queries ... for GUID ... Port
... is bigger than sa_rate_threshold`. Each virtual function has its own
port GUID, so one tenant can't use up another tenant's allowance.

A new hardening group, `sa_rate`, sets `sa_rate_threshold 1000`, that is
10,000 requests a second from one port. That is well above what IPoIB and
connection setup need, but it stops one port flooding the SA. Before the
group goes into `all`, the value is checked against an MPI job's start-up
on the test fabric, which makes the most path record requests.

## Not adopted

- **Rotating keys.** UFM re-randomises its key seed periodically. AFM's
  keys never change, and the HA spec already makes key rotation future
  work. Rotating the M_Key safely needs every port to be re-keyed while
  both keys are known, and a takeover in the middle of a rotation to know
  which key each port has.
- **`m_key_per_port`.** This gives every port its own M_Key, so a leaked
  port key exposes only that port. It isn't adopted until it's known how
  OpenSM derives the keys, for the reason given under Class keys, HA. AFM's
  fence ask, fabric check and diagnostics would also need each target
  port's key.
- **SHARP job keys and service keys.** These protect UFM's SHARP
  Aggregation Manager, which AFM doesn't run. OpenSM leaves SHARP enabled on
  switches that support it (`sharp_enabled 2`). Whether a tenant could use
  SHARP without an Aggregation Manager that AFM controls is an open
  question.
- **All HCAs in the same pkey.** UFM's single-pkey bare metal profile
  doesn't fit a fabric where each network is its own partition.

## MFT and the M_Key

UFM's profile requires firmware tools to use the M_Key. For AFM this is
operational, not a setting. The hardening doc will say that in-band MFT
tools (`flint -d lid-...`, `mlxlink`, `mlxconfig` over InfiniBand) need the
M_Key, and the VS key once `class_keys` is on. It will also say that such
tools must be run only from the managers' hosts, never from a tenant's.

## Defaults and upgrades

`allowed_sm`, `class_keys` and `sa_rate` join `all` once their hardware
tests pass. Until then they can be named, but `all` leaves them out. When
they join `all`, a deployment that relies on the default gets them at its
next upgrade. The release notes mark this as fabric-affecting:

- single node: the restart after the upgrade applies them;
- HA: the hash changes, so the operator records a new epoch, as for any
  fabric-affecting change.

Whoever shares the fabric must agree to them first, as with any change to
hardening.

## Changes to specs/ha.md

These go into specs/ha.md before the HA code changes:

- **Shared state, `nodes`:** each node's adapters' port GUIDs, beside their
  node GUIDs, recorded at registration and whenever the probe finds them
  changed.
- **Configuration:** `opensm.conf` includes `allowed_sm_guids`, generated
  from the node rows. The list is excluded from the configuration hash, and
  a change to it moves the leadership until a SIGHUP is shown to apply it.
- **Job completion, fabric checks:** `ERR 0F09` raises the unknown-SM alarm.
- **Required keys:** `class_keys` is refused until the two-OpenSM test
  shows that keys derived from the seed agree across leaders.
- **Retiring a node** removes its ports from the list, and the leader then
  re-renders.

## Hardware tests

On a fabric that nobody else shares, since these tests run a second SM,
send SMInfo Sets and flood the SA, with two hosts:

1. `allowed_sm_guids` with only host A: an OpenSM on host B at a higher
   priority doesn't send A's OpenSM to STANDBY, A logs `0F09`, and B's
   SMInfo Set is dropped with `2F1C`. Also check whether B appears in A's
   SMInfo records, and whether OpenSM runs on a port that isn't in its
   own list.
2. Adding host B's GUID and sending SIGHUP: does it take effect without a
   restart?
3. `class_keys`: VS, CC and N2N keys are set on switches and adapters, and an
   unkeyed `mlxlink` from a host fails. After a takeover by the other
   host's OpenSM, with the same seed, ports are still configured. With
   leases of 0, the keys survive an hour with no SM while a host sends
   unkeyed MADs.
4. `sa_rate`: an MPI job's start-up across the test fabric logs no `4C11`.

## Implementation plan

- [ ] Hardware tests 1 and 2; then the `allowed_sm` group for single node,
      with the lock-out checks and the `0F09` alarm.
- [ ] specs/ha.md changes; the node rows' port GUIDs; `allowed_sm` in HA
      mode.
- [ ] Hardware test 3; then `class_keys`, with `key_mgr_seed` in the
      database.
- [ ] Hardware test 4; then `sa_rate`.
- [ ] docs/opensm-hardening.md: the new groups, MFT and the VS key.
- [ ] Add the groups to `all`, with release notes.

## Open questions

- Which in-band tools can take the VS key, and how?
- Could a tenant use SHARP without an Aggregation Manager that AFM
  controls?
- Is UFM's MAD limiter more than an SA rate limit, for example a limit on
  SMPs or VS MADs from hosts? If so, does OpenSM 5.17 have an equivalent?
