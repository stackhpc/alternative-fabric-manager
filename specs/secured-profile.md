# The secured cloud profile

Status: accepted 2026-11-13; implementation in progress (see Implementation plan).

UFM's Secured Cloud Profile (`cloud_profile = 2` in UFM Enterprise 6.26)
is NVIDIA's list of protections for a fabric that bare metal tenants share.
AFM's hardening (docs/opensm-hardening.md) already covers most of it. This
spec adds the rest that OpenSM 5.17 can do, as new hardening groups, and
says why the remaining items are left out.

Each new group joins `all`, the default, once its hardware tests pass, and
can be removed with `-group` (see Choosing the groups).
Each one waits for hardware tests whose outcomes decide parts of its
design.

## Where AFM stands

| Profile item | AFM today | This spec |
| --- | --- | --- |
| Default partition membership limited | `ALL=limited, SELF=full` | — |
| No duplicate pkey at index 0 | A port's GUID is in only one network's partition | — |
| Restrictive pkey posture | No full `ALL`, `ALL_CAS` or `ALL_VCAS` outside a network's partition | — |
| MKEY | `m_key`: protection level 2, no lease | — |
| A key per port | None: one M_Key for every port | `m_key_per_port` group |
| SMKEY, SAKEY | `sm_key`, `sa_key` | — |
| Enhanced SA trust model with registration limits | `sa_trust` | — |
| Allowed SM GUIDs | None | `allowed_sm` group |
| VSKEY, CCKEY, N2N key | None | `class_keys` group |
| MAD limiter | `babbling_port_policy` limits traps only | `sa_rate` group, if the hardware test supports it |
| MFT with M_Key mandatory | None | Documented |
| Keys from a periodically randomised seed | Keys never change | Not adopted (see Not adopted) |
| SHARP job keys, service keys | None | An open question, with a proposal |
| All HCAs in the same pkey | Not applicable | Not adopted |

## Allowed SM GUIDs

### Why

A host that runs a subnet manager takes part in the SM election. A tenant's
SM without the fabric's keys can't set ports, since the M_Key protects
them. It can't complete a handover either: OpenSM ignores an SM whose
SM_Key doesn't match (`ERR 2F18`). Whether it can still send AFM's OpenSM
to STANDBY, by winning the election on priority or GUID, isn't known.
Hardware test 1 answers that, against a baseline without the list. If it
can, the fabric is left without a working SM: new ports don't come up,
partition changes aren't applied, and the fabric check fails, so no job
completes. Any bare metal tenant could cause that denial of service.

With `allowed_sm_guids`, OpenSM ignores every SM whose port isn't in the
list (`ERR 0F09: Ignoring SM on port 0x..., is not part of the allowed port
GUIDs`). It drops an SMInfo Set from such a port (`ERR 2F1C`). If the
port it is polling, or its master, leaves the list, it goes back to
discovery ("is not longer a part of the allowed SM GUIDs").

If test 1 shows that the SM_Key and M_Key already keep AFM's OpenSM master,
the group's case is only defence in depth, and the doc says so.

The list is only as strong as port GUIDs as an identity. A bare metal
tenant that could rewrite its adapter's port GUID could claim an allowed
one. Partitions already rest on that assumption.

### The group

A new hardening group, `allowed_sm`, sets one option:

```
allowed_sm_guids 0x<guid>,0x<guid>,...
```

The list holds the port GUIDs of the **physical functions** of the
InfiniBand adapters on every host where AFM may run OpenSM. Virtual
functions are left out: on a host that is also a hypervisor, a virtual
function's GUID belongs to a tenant's port. A port is a physical function
when its device has no `physfn` link in sysfs
(`/sys/class/infiniband/<dev>/device/physfn`), and isn't an mlx5
sub-function (an auxiliary device under `mlx5_core.sf`), which also has no
`physfn` link. It counts only if its link layer is InfiniBand.

- **Single node:** the host's own physical ports, read before
  `opensm.conf` is rendered. They're read in a short-lived container of
  OpenSM's image, as HA mode's reach probe already does before OpenSM
  starts. Every physical port is listed, because OpenSM may bind to any of
  them unless `guid` is set.
- **HA:** the union of the port GUIDs that the cluster's node rows record,
  live or stopped. A node's ports leave the list only when an operator
  forgets the node's row. specs/ha.md's Retiring a node says that such a
  command comes with the checks that read node rows' adapters. This spec
  adds it (see Changes to specs/ha.md). A clean stop, which marks a node
  `stopped`, leaves the list as it was.

Ports are recorded in a node row only by a probe that succeeded and found at
least one physical InfiniBand port:

- at registration, and then at every heartbeat until a probe first
  succeeds, since the probe fails while a fresh node is still pulling
  OpenSM's image;
- by an operator's command, after an adapter is replaced.

A node whose row has no port GUIDs can't be a candidate, so it never holds
the lease or runs OpenSM, and the leader renders the list from the rows
that have them.

A probe that fails, finds no image, or finds no ports never changes a row.
So the list changes only when a node joins, is forgotten, or the operator
records new adapters. It never changes because a virtual function was
assigned, or a probe failed.

### Required groups

`allowed_sm` needs the `m_key` group, at protection level 1 or more, and
the `sm_key` group, in both modes. The fabric check's rule for ignored SMs
rests on an ignored SM having neither key (see The fabric check). Without
them, an ignored SM could set pkey tables on unprotected ports while the
check, which no longer sees it, passes. `Config.validate` refuses the group
without them.

If `m_key` or `sm_key` is set in `[opensm_conf]`, rather than generated,
AFM can't know which other SMs hold it. Then every `0F09` fails the check
and contests the fabric, unless the operator records the other SMs' ports
as known SM ports.

### Overrides

`[opensm_conf] allowed_sm_guids` overrides the generated list, as with any
hardened option. Unlike other overrides, it's checked:

- **Syntax:** `Config.validate` refuses an override that isn't `0`, or a
  comma-separated list of 64-bit hexadecimal GUIDs. OpenSM would otherwise
  log `ERR 7549` and disable the feature.
- **At start:** `ERR 7549` in OpenSM's start log fails the start, so a list
  that OpenSM rejected can never pass as protection.
- **HA:** the override must contain every port GUID in the node rows,
  which are the known SM ports of nodes that haven't been forgotten, and
  `0` is refused, because each leader must accept the others' SMs (see HA).
  The override is a cluster setting, so it is part of the configuration
  hash. Only the generated list is left out of the hash.

### Refusing a list that would lock AFM out

If the list leaves out the port that OpenSM binds to, the fabric could
have no SM. The checks apply to the effective list, generated or
overridden. When one fails, the manager refuses to start OpenSM, raises an
alarm, and reports OpenSM as not running. It checks that:

- the list isn't empty, and isn't `0` in HA mode;
- the list holds this host's physical ports, as the probe reads them now,
  or holds `[opensm_conf] guid` if that is set. A probe that fails refuses
  the start, since the manager can't tell;
- in HA mode, this node's row has port GUIDs.

The same checks run before every re-render. `ERR 7549` after a SIGHUP
fails the apply, as it does a start, since OpenSM has then turned the
feature off in the running process.

Hardware test 1 also tests whether OpenSM itself refuses to run on a port
that isn't in its list.

### The fabric check

Ignoring an SM would hide it from the fabric check. ha.md relies on seeing
another SM: Why the unconfirmed wait is enough ends with the new leader's
OpenSM starting in STANDBY, or the check finding another master. So the
check sorts ignored SMs by whether they could hold the fabric's keys.

The manager keeps a set of **known SM ports**: every port GUID that any
node row has ever recorded, kept when a node is forgotten. A forgotten
node's `opensm.conf` and cache still hold the keys, and key rotation is
future work.

- **`ERR 0F09` naming a known SM port**, logged since the last check, fails
  the check and marks the fabric contested. It stays contested until a
  direct query of that port, by LID and with the SM_Key and M_Key, shows
  either:
  - SMInfo answering NOT-ACTIVE; or
  - PortInfo with the IsSM capability bit clear, meaning no SM is bound
    there.

  A query that times out never clears the contest. A paused OpenSM
  doesn't answer either, and it is ha.md's one uncovered case. In HA mode,
  a known port is outside the list only when its node was forgotten.
- **Recording a known SM port.** An operator's command adds a port to the
  known set. It is how an SM that holds overridden keys is made known (see
  Required groups), and the only source of known ports on a single node
  besides its own.
- **Dropping a known SM port.** A forgotten node's adapter may be reused
  in a host that never holds the keys, even a tenant's. An operator's
  command drops a port from the known set. It asserts that the adapter is
  no longer in a host with the keys, and is refused for a port in a node
  row. Without it, a tenant's SM on a reused adapter would contest the
  fabric for good.
- **`ERR 0F09` naming any other port** raises an alarm and doesn't fail the
  check. Such an SM has no keys: they're generated by AFM, and held only by
  the database, the managers' hosts and the backups, which the hardening doc
  says to keep from tenants. Without the M_Key it can't set ports, so it
  can't undo a sweep. Without the SM_Key it can't hand over. The list stops
  it taking the election, so AFM's OpenSM stays master, and the check still
  sees that from AFM's own SMInfo record.
- An SM in the SA's SMInfo records is judged as today.

This rule rests on two outcomes of hardware test 1:

- **Whether `0F09` repeats at every sweep, or is logged once.** If it's
  logged once, a known port stays contested until the direct Get clears it,
  as above. For other ports, an alarm raised once is enough.
- **Whether an ignored master still sets ports.** It shouldn't, without the
  M_Key. If test 1 shows that an SM with a wrong M_Key can change a port's
  state anyway, then every `0F09` fails the check, and `allowed_sm` gives
  only the alarm.

### HA

In HA mode, once the running list is current (see Changing the list),
every leader's list contains every other node's ports, so:

- the fence ask, an SMInfo Set from the new leader's host, isn't dropped
  with `ERR 2F1C`;
- a resumed old leader's OpenSM sees the new one, and the new one sees it,
  as the election and the fabric check expect.

`allowed_sm` in HA mode depends on parts of specs/ha.md that aren't
implemented yet. HA mode refuses the group until all of these are in:

- node rows record adapters (cli.py builds the Coordinator without them
  today);
- configuration epochs are enforced, so leaders with different lists can't
  alternate during a rolling upgrade;
- the fabric check treats a managed node's SM as ha.md says;
- the fence ask is implemented, and hardware test 1 shows it is honoured
  with both hosts listed.

A rolling upgrade to a version that records port GUIDs leaves older rows
without them. The leader renders the list from the rows that have port
GUIDs, as in The group. It refuses to start OpenSM with the group only
while a row without port GUIDs is in the target set. That row belongs to
an old-version leader, which doesn't follow the candidate rule and whose
OpenSM the new list would ignore.

### Changing the list

UFM's documentation lists `allowed_sm_guids` among the options that can
change at run time, and the binary has `osm_update_allowed_sm_guids`.
Hardware test 2 checks whether a SIGHUP applies a change, both adding and
removing a GUID, and what else a SIGHUP re-reads from a changed
`opensm.conf`.

In both cases, the leader records the effective list its OpenSM runs with
in `applied`, and raises an alarm while a live node with port GUIDs is
outside it.

- **If a SIGHUP applies it:** the leader re-renders `opensm.conf` when the
  list changes, and sends a SIGHUP. Single node, the manager does the same
  when it starts.
- **If not:** the operator applies a change by moving the leadership, as
  for an upgrade, once the new node's row has its ports. There's no
  automatic step-down for it.

A candidate inside the running list is preferred when the lease is free.
Whether a candidate outside it may take over depends on hardware test 1's
asymmetric-list run:

- **If the newcomer sees the old OpenSM** (the old one still answers its
  SMInfo Get, since `2F1C` covers only Sets): it may take over, by any of
  ha.md's ways out of the target set. The new leader's list holds the old
  leader's ports, so a resumed old OpenSM is seen, and the new OpenSM goes
  to STANDBY or its check fails. Only the fence ask is lost (`2F1C`), and
  the Ask never counts on its own.
- **If it doesn't:** it may take over only once the old leader released the
  lease or verified its own stop, never by an unconfirmed takeover, which
  could leave two masters holding the keys with checks that pass.

Either way, no rule that bars every outsider: that would leave no eligible
node when a leader dies before the operator moves the leadership.

In the second case, a leader that dies uncleanly while every live
candidate is outside its running list leaves the fabric with no SM until
its stop is verified. That fails closed, and the alarm for a live node
outside the running list warns of it beforehand. The hardening doc will
tell operators to move the leadership promptly after a node joins.

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
vs_key_lease_period <key_lease_period>
cc_key_enable 2
cc_key_protect_bit 1
cc_key_lease_period <key_lease_period>
n2n_key_enable 2
n2n_key_protect_bit 1
n2n_key_lease_period <key_lease_period>
key_mgr_seed <stored>
```

The leases are `[opensm] key_lease_period` (see Leases). Hardware that
doesn't support a class answers its KeyInfo with status `0xc`, which OpenSM
logs as `ERR 9828` at every sweep: on the test fabric, CC and N2N keys
weren't supported (see Results). The group doesn't treat `9828` as a
failure, or it would fail on such hardware. It doesn't silently overstate
itself either: the manager logs, once per start, which classes the ports
rejected, so the operator knows that only VS keys protect those ports.

### The seed

OpenSM derives each port's class keys from `key_mgr_seed`, or uses the
M_Key when the seed is 0. AFM uses a seed of its own, random, 64-bit and
non-zero. That's not because a separate seed is much harder to steal: it's
kept wherever the M_Key is, including OpenSM's startup log. It's because an
operator given the VS key for a diagnostic tool would otherwise be given the
M_Key.

With leases of 0, a seed that is lost leaves every port keyed with keys no
SM can reproduce. So the seed must be in every backup taken before any port
is keyed:

- `key_mgr_seed` joins `KEYS`. The manager generates it at startup, whatever
  the hardening groups, in the release **before** the one that can enable
  `class_keys`. The backup that the deploy-host skill takes before an
  upgrade then holds it.
- A start that generates the seed never renders `class_keys`. It refuses
  the group with an alarm, and a later start may use it. That covers a
  fresh install, a skipped release and a restore of an older backup: the
  seed is always in the database, and so in the next backup, before a
  port is keyed. After such a restore, a new seed is a refusal and an
  alarm, not ports keyed with a seed that a backup lacks.
- The seed enters the configuration hash only when `class_keys` is chosen,
  so adding it to `KEYS` doesn't change every node's hash.
- `--show-keys` prints it. AGENTS.md's rule never to print OpenSM's keys
  covers it.
- An `[opensm_conf] key_mgr_seed` override must be a non-zero 64-bit
  value, in hexadecimal or decimal without leading zeros: spellings that
  Python and OpenSM read alike. When no seed is stored yet, a valid
  override is stored as the seed rather than a random one, since ports may
  already be keyed with it, set by hand. Otherwise an override that differs
  from the stored seed is refused, as ha.md refuses keys other than the
  stored ones. An equal override is written as the stored value, never in
  the operator's spelling.
- `keys()` stores the seed too, as `--show-keys` and rendering call it, so
  the class_keys step must record whether a start stored it, for the rule
  above.

OpenSM also caches class keys per port (`guid2vskey`, `guid2cckey`,
`guid2_n2n_key`, beside `guid2mkey`) in `/var/cache/opensm`. If those
caches, rather than the seed, decided a port's key, a database backup
wouldn't be enough, even on a single node. Hardware test 3's first run
found that, on the same host, they don't for VS keys: an OpenSM restarted
with the class key caches moved aside derived the same keys from the seed
(see Results). That rules out the class key caches, not a derivation that
also uses the SM's own port GUID or other local state, which is what an HA
takeover by another host would expose. The second-host run decides HA.
CC and N2N keys are still unverified, because the test fabric doesn't
support them. Until the rest of test 3 passes, the group is refused in
single-node and HA modes.

A port keyed with another seed wasn't shown to recover while OpenSM ran.
With a changed seed, OpenSM wrote its own derived keys to the cache, and its
KeyInfo Gets to the switch timed out for more than five minutes despite a
60 s lease. The switch answered unkeyed queries again after OpenSM had been
stopped for three minutes. The cause isn't established, since the same
timeouts also appeared with the right seed (see Results, Not explained
yet). Either way, the seed rules above are the safe path: don't count on a
running OpenSM to recover from a changed or lost seed.

### Leases

The leases follow `[opensm] key_lease_period`, like the M_Key's: one option
sets every key lease the manager writes, 0 (permanent) by default, so that
an operator opts out of permanent keys in one line (see
docs/opensm-hardening.md, Permanent keys). The default is 0 for the same
reason as the M_Key's (see docs/opensm-hardening.md, The lease): AFM stops
OpenSM during restarts and takeovers, and a lease that ran out while no SM
ran would let a host clear a port's protection. Hardware test 3 checks that
a lease of 0 means "never expires" for these classes, as it does for the
M_Key. If it doesn't, the default for these classes is the longest lease
OpenSM accepts, and the doc says so.

The cost of a lease of 0 is recovery. In the first run, ports keyed with a
lost seed answered again after OpenSM had been stopped for longer than the
60 s lease. Whether the lease or something else did it isn't established
(see Results). With a lease of 0 the ports are expected to stay keyed until
a power cycle clears them, which test 3 also checks. The default stays 0,
the same choice made for the M_Key. An operator who prefers recovery sets
`key_lease_period` and accepts the window. That is a policy choice the
option makes, not something a test decides.

### Turning the keys off, and downgrades

Ports keep their keys until an SM that has them changes them. To turn a
class off:

1. Set `<class>_key_enable 1` (Disable) in `[opensm_conf]`, and restart.
   OpenSM then clears the keys from the ports. This needs the seed the
   ports were keyed with: OpenSM reaches a port only with the key it
   derives.
2. Check that it has finished, as hardware test 3 establishes.
3. Only then drop `class_keys`.

Dropping the group at once sets enable to 0 (Ignore), which leaves ports
protected by keys that the SM no longer manages. A downgrade to a release
without the group has the same effect. So the release notes, and the
hardening doc's downgrade section, require step 1 before any downgrade.

### Cost

In-band tools that send VS MADs, such as `mlxlink`, `mlxreg`, in-band
`flint`, `ibdiagnet`, and in-band switch firmware upgrades, need the VS key.
`mlxlink` also sends SMPs, so it needs the M_Key as well, whatever the VS
keys (see Results).

An unkeyed `vendstat -N` (General Info) answered with
`vs_key_ci_protect_bits 1`. Whether that is because General Info is
unprotected, or because the VS keys weren't enforced at all, the first run
can't tell.

`mlxlink` has no key option. `ibdiagnet` takes `--vs_key_file`, pointed at
OpenSM's `guid2vskey`, but in the test it couldn't open the adapter from
OpenSM's container while OpenSM ran. Using it makes OpenSM's cache,
`/var/cache/opensm`, a secret that operators handle, so the hardening doc's
list of where keys live gains it when `class_keys` lands. Which tools can
take the key, and how, is still an open question. The doc's Diagnostics
section will list them once it's answered.

AFM itself sends only SMPs and SA requests, so its fabric check, reach
probe and fence ask are unaffected.

## SA request rate

UFM's MAD limiter (`[MADLimiter] mad_limiter_enabled` in UFM) is a UFM
feature, and its documentation doesn't say which OpenSM options it uses.
The nearest OpenSM option is `sa_rate_threshold`: the most SA requests per
100 ms. OpenSM drops the rest, logging `ERR 4C11: Number of sent SA queries
... for GUID ... Port ... is bigger than sa_rate_threshold`.

What OpenSM counts against is unknown. If it counts per port GUID, each
virtual function gets its own allowance. If it counts per physical port,
virtual functions sharing a port share one allowance, and one guest could
get its neighbours' and its hypervisor's requests dropped. The group would
then add a denial of service between tenants. Nor is it known whether
trusted requesters, which send the SA_Key, are limited, which would include
the manager's own `saquery`.

A new hardening group, `sa_rate`, sets `sa_rate_threshold`, only if
hardware test 4 shows that:

- the count is per port GUID;
- a flooding port is limited, and another port is unaffected;
- the chosen value doesn't limit an MPI job's start-up.

Otherwise the group isn't added, and the MAD limiter stays a gap. The
value, proposed as 1000 per 100 ms, is set from the test's results. That
includes the SA's total capacity, since a limit near it protects little
against several ports flooding at once.

## Per-port M_Keys

### Why

Every port on the fabric has the same M_Key today. Anyone who learns it
from any one port can send SubnSet to every switch: rewrite pkey tables,
which breaks every tenant's isolation, move LIDs, or take ports down. A
bare metal tenant has root on a host with a port on the fabric, and there
are several ways for that port's key to leave it:

- **The host's own adapter holds it.** mlx5's driver queries its own port
  with the adapter's M_Key check bypassed (MAD_IFC's "ignore M_Key"), so
  root, who can load a module of their own, can likely read the port's
  PortInfo, M_Key included. This hasn't been tried here.
- **A lease lapse returns it.** Hardware test 3 saw this happen: unkeyed
  SMPs while no SM ran dropped two ports' protection, and their PortInfo
  then gave the M_Key to anyone who asked (see Results).
- **Adapters with a non-volatile M_Key** (`IB_PORT_CAP_HAS_NV_MKEY`) keep it
  across reboots, so it goes wherever the card goes.

So the shared M_Key is only as secret as the least protected port, and a
tenant controls their own. With a key per port, a port's key opens only
that port, which its tenant controls anyway. Two limits: a tenant who knows
their own port's GUID and key can search for `m_key` offline, at about
2^63 SHA-1s, and a tenant who can make their adapter present another
port's GUID is handed that port's key, which only OpenSM's duplicate-GUID
handling stands against (the same assumption as Allowed SM GUIDs).

### How OpenSM derives the keys

With `m_key_per_port TRUE`, OpenSM 5.17.2 gives each port the key that
`osm_subn_calc_mkey` computes, read from its disassembly: SHA-1 over the
`m_key` and then the port GUID (`osm_physp_get_port_guid`), each as 8
bytes in network order, with the 20-byte digest folded into 8 bytes by
XOR (digest byte `i` into key byte `i mod 8`), read in network order:

```python
def port_m_key(m_key, port_guid):
    digest = hashlib.sha1(
        m_key.to_bytes(8, "big") + port_guid.to_bytes(8, "big")
    ).digest()
    key = bytearray(8)
    for i, b in enumerate(digest):
        key[i % 8] ^= b
    return int.from_bytes(key, "big")
```

A zero GUID gets 0. With the option, OpenSM also forces a non-zero
`m_key`, `m_key_lookup TRUE` and protection level 2.

The key OpenSM sends to a port (`req_determine_mkey`) is, in order:
- the port's entry in `guid2mkey`, if OpenSM knows the port's GUID, from
  its topology or from `neighbors` (looked up by the local side's port
  GUID and port number);
- otherwise, if it knows the GUID, the derived key ("Target port mkey
  unknown, using default");
- otherwise, with the option on, 0 ("Target port guid unknown, using
  default"); `m_key` only with it off.

So a cached entry beats the derivation, and with the option on OpenSM
never tries the shared `m_key` on a port whose entry is missing.

### A cold cache can't find the ports

At protection level 2 a port answers no SubnGet without its key, NodeInfo
included (test 3's probe saw this on every physical port). An OpenSM that
starts without the fabric's `neighbors` knows no GUID beyond its own port,
and sends 0, which a per-port keyed port refuses: every HA takeover on
another host, and any lost cache. With a lease of 0 those ports stay out
of reach; with a lease they come back only by lapsing, which is the
exposure above. Test 5 found that `neighbors` alone is enough (see Results).

AFM's own SMPs have the same problem. The fabric check and the HA reach
probe run `ibswitches -y <m_key>`, with one key for the whole fabric.
`ibdiagnet` in OpenSM's image takes per-port keys (`--discovery_only
--m_key_files <dir>`, reading `guid2mkey`, `guid2lid` and `neighbors`),
but class keys' Cost found it couldn't open the adapter from OpenSM's
container while OpenSM ran. The fallback is a prober that walks recorded
links with `smpquery -D <route> -y <derived key> nodeinfo`, Gets only.

### Turning it on and off is a key rotation

Turning the option on re-keys every port from `m_key` to its derived key,
and turning it off reverses that. Each works only where OpenSM's cache
says which key each port holds: on a warm cache, `guid2mkey` maps every
GUID to `m_key` before turn-on (if OpenSM writes the cache with the
option off; test 5's backup A checks this) and to the derived key after. A cold or
stale cache, a takeover, a manager-written cache with the wrong key, or a
crash during the re-keying sweep leaves ports that no OpenSM can reach.
This is Not adopted's objection to rotating keys, and the design has to
answer it rather than avoid it.

### Requirements

The review of this section's first version set these requirements, and
The design below meets them:

1. **A per-port key state, never a key.** `fabric_ports` records, per port
   GUID, whether the port holds `m_key`, its derived key, or is changing,
   set by comparing OpenSM's cache with both candidates, and the manager
   renders OpenSM's cache from that state on every start, with the group
   on or off, while rows exist. A transition is recorded under the term
   compare-and-set before the start that begins it, and ports left
   changing are resolved by a Get with each key, or by an operator.
2. **Records follow the fabric, not AFM's sweeps.** OpenSM keys new ports
   in its own sweeps, so the record is taken at every fabric check
   interval, from `guid2mkey` and `neighbors` (not `guid2lid`, so virtual
   functions don't accumulate). With the group on, NodeInfo or PortInfo
   timeouts in a sweep are unattributed errors that hold the sweep's jobs,
   as is a switch port with its link up and no discovered neighbour: jobs
   wait only on evidence of a linked port OpenSM can't reach, not for a
   GUID without a record (virtual functions never have one, and a bare
   metal port is often bound before its node powers on). A present GUID
   can't be forgotten.
3. **Merging into a cache** follows the state, not "keep what's unknown":
   a stale entry beats the derivation, so it's as harmful as a missing one.
   Re-cabled links replace their rows.
4. **The fabric check, `--confirm-switches` and the reach probe** run a
   discovery that test 5 has shown works beside a running OpenSM, from a
   private directory that is removed afterwards, with stated exit-status
   rules, within the fabric check's timeout in ha.md. It returns switch
   node GUIDs, as `_judge` compares sets. Every instance re-renders its
   files from the database on a period, so a candidate's probe uses
   current keys; with no rows, the probe uses `m_key` as today.
5. **HA:** refused until configuration epochs are enforced (as
   `allowed_sm` is), so that leaders with and without the group can't
   alternate. The fence ask and `allowed_sm`'s `0F09` direct query need
   the target port's derived key and a route, so node rows need port
   GUIDs. A helper that writes the cache volume counts in the takeover's
   start time, and the watchdog's START hashes cover the rendered files.
   HA joins `all` only after a run on a second host.
6. **By hand and rollback:** `Config.validate` refuses `m_key_per_port`
   in `[opensm_conf]` unless the group is named, and with the group,
   overrides of `m_key_protection_level` and `m_key_lookup`. Keys derive
   from the effective `m_key`. A downgrade to a release without the group
   needs the group turned off first, verified on every port, HCAs
   included; restoring an older database is a downgrade too.
7. **Every user of the M_Key changes:** README.md, docs/kayobe.md,
   AGENTS.md, the deploy-host skill and `--show-keys`'s help name the
   per-port key for `-y`. `--port-m-key <guid>` prints a key, under
   AGENTS.md's rule on keys.

### The design

This first version is for single node with the managed driver. HA mode
refuses the group (requirement 5), and so does any other driver, since
the manager must write OpenSM's cache volume and run the prober in
OpenSM's image.

**The group.** `m_key_per_port` writes `m_key_per_port TRUE`,
`m_key_lookup TRUE` and `m_key_protection_level 2`, written so that they
show though OpenSM forces them. It needs the `m_key` group, and is in
`HARDENING_GROUPS` only, so it's chosen as `all, m_key_per_port`.
`Config.validate` refuses it without `m_key`, in HA mode, with another
driver, without a volume at `/var/cache/opensm` in `[opensm] volumes`
(the copy would hit the read-only root), and with `m_key`,
`m_key_per_port`, `m_key_protection_level` or `m_key_lookup` in
`[opensm_conf]`; and refuses `m_key_per_port` in `[opensm_conf]` without
the group. While any row holds a derived key, the start also refuses
`m_key_lookup FALSE` and removing the `m_key` group, which a turn-off
depends on. Naming the group permits per-port keys; an operator's
command begins them (Turning it on).

**A new base key.** Every derived key is a function of the base key and
a GUID, and a GUID isn't secret. The base can't be the `m_key` every port
holds today, which is exactly the key the group exists because tenants
may have read: test 3 saw it handed out by a lapsed port. Deriving from
it would hand that tenant every port's key. So the manager keeps a
separate base, `m_key_base`, in `KEYS`: generated, if it's missing,
whatever the groups, as the seed is, so that backups hold it a release
before any port is keyed with it; never printed, and its own line in
`--show-keys`. As with the seed, a start that stored the base can't
begin per-port keys: the next start, after a backup, can. OpenSM's
`m_key` line is the base while the target is on. Ports still holding the
old `m_key` are reached through their `guid2mkey` entries, which beat the
derivation. Turning the group off re-keys every port to the stored
`m_key`, the old key, not the base: a tenant who read it has it again,
which turning off can't avoid, and the docs say so. A later turn-on uses
the same base, so ports that kept their derived keys still match. The
`state` table keeps a fingerprint of the base the ports were keyed from
(SHA-256 of a fixed label and the key, not the key), and a start whose
base doesn't match it refuses the target on (see Restores).

**What's recorded.** Two tables, written in single node by the manager
and, once HA allows the group, by the leader under its term's
compare-and-set:
- `fabric_ports`: port GUID, its node GUID (the switch's port 0 for a
  switch), whether it's a switch (as its NodeInfo said; not known until
  it answers one), `holds` (`m_key`, `derived` or `unknown`), `lapsed`
  (its ProtectBits were 0 at the last Get), `reachable`, `released` (an
  operator's `--release-port`, cleared when the port next answers its
  key), and `last_seen`.
- `fabric_links`: (GUID, port) to (GUID, port), both directions as in
  `neighbors`, with `last_seen`.

The `state` table holds the target, per-port keys on or off; the target
OpenSM was last started on, written before that start, so that a start
whose target differs from it is one that begins a transition; and whether
a turn-on start has met its success rule (Turning it on), which every
start with the target on is held to until one does. A port
whose `holds` isn't what the target gives it (`derived` when on, `m_key`
when off) is changing, which needs no state of its own: requirement 1's
"changing" is that disagreement.

**Reading OpenSM's cache.** At every fabric check interval, while the
group is named or any row exists, the manager reads `neighbors` and
`guid2mkey` from the running container (`get_archive`, never logged).
Each `neighbors` row replaces the row with its local (GUID, port), which
handles a re-cabled port as OpenSM saw it; the old peer's row, if it
still points back, goes with it, and a link recorded in one direction
only is neither followed nor rendered. Otherwise the port would be
reached through its old switch port, whose link is down, and whatever
is cabled there next would be taken for it. Each GUID it lists gets a row;
its `holds` comes from its `guid2mkey` entry, compared with the old
`m_key` and its derived key, or is `unknown` if its entry is neither. A
port with no entry keeps what its row holds, since a port OpenSM lost
still holds its key, and a new row without one is `unknown`. Only GUIDs that `neighbors` lists are recorded, so
virtual functions never get rows, and a recorded port that's absent from
OpenSM's files keeps its row: a port that's down now may come back
holding its key. A read that fails is logged, and leaves the rows as
they were; job completion doesn't depend on it (Lost ports and jobs).

**The prober.** A script in OpenSM's image runs `smpquery -D` Gets by
directed route from this host's port along `fabric_links`, in parallel
up to a bound, each with a timeout, and prints what answered. Switches
are reached first, and each switch's PortInfo for its ports gives their
link state, so a port behind a link that's down isn't sent anything. It
Gets PortInfo, which shows ProtectBits, and NodeInfo, whose GUID must
match the row's, or the answer doesn't count. A port with ProtectBits 0
answers any key, so it's recorded `lapsed`, with `holds` unchanged; only
an answer with ProtectBits 2 shows which key a port holds. The script
prints named fields only (GUIDs, node type, port count, link state,
ProtectBits), so PortInfo's M_Key field never leaves the container, and
it takes its routes and keys from its environment, not its own command
line. This host's port is the one that answers the empty route. Which
keys it may try is what keeps the old key from tenants:
- the port's derived key, whenever the target is on or the row says
  `derived`;
- `m_key` only if the row says `m_key`, or the target is off (every port
  is about to be given `m_key`, so trying it exposes nothing new), or
  nothing can hold a derived key yet: no fingerprint is recorded, and no
  row or volume entry is `derived`;
- both keys on this host's port and on switch port 0, which no tenant
  controls. A row is a switch's because its NodeInfo said so, which an
  adapter that answers as a switch could say too, as it could give a
  confirmed switch's node GUID. So only a row whose own GUID, which the
  route's answer must match, is one an operator confirmed
  (`--confirm-switches`) counts here, and only such a switch is routed
  through or counted by the fabric check: a Get that passes through a
  node shows it the key. Any other row gets a tenant port's rule. While
  rows exist, `--confirm-switches` records the GUIDs the prober found
  on its routes, following every row that answers as a switch, for the
  operator to check. Those are the switches' port 0 GUIDs, where
  `ibswitches` gave node GUIDs: on a switch whose two differ, the check
  reports the switch missing until it's confirmed again with rows
  present. Until some switch is confirmed, a verify reaches this host's
  port and its peer only, and `--begin-port-keys` refuses.

It tries the key the row records first, then the other one the rule
allows, so a port on `m_key` during a slow transition isn't sent a wrong
key at every check. A port whose row is `unknown` tries its derived key
first. With the target on, a
port recorded `derived` or `unknown` that doesn't answer its derived key
is marked unreachable and never sent `m_key`. Routes go only through
rows already known to be switches, so a verify is repeated, within its
bound, while it learns that more rows are: rows new from OpenSM's cache
are reached one level of switches at a time. A wrong-key Get counts as
a violation and starts a lease countdown, which the right key's Get
stops (test 5); with a lease of 0 there is no countdown. Keys are on
`smpquery`'s command line, visible in `/proc` on the manager's host, as
`ibswitches -y` is today; that host has no tenants. A helper container
has a fixed name, and a start removes one left behind, since its
configuration holds the plan. The prober runs in
the running OpenSM's container (`docker exec`) for checks, and in a
helper container from its image, as the reach probe does, before a
start.

**Before each start.** While the group is named or any row exists, the
manager:
1. reads `neighbors` and `guid2mkey` from the created, not yet started,
   container's volume (`get_archive` works on it) and folds them into
   the rows as a check does, except that for each (GUID, port) whichever
   of the row and the file is newer wins (`last_seen` against the file's
   time): the file is newer after a crash within a check interval of a
   new port being keyed, and the rows after a lost volume. Files that
   are absent are a lost volume; files that can't be read or parsed fail
   the start, which is retried, since step 3 would replace them unseen;
2. verifies every recorded port behind a link that's up with the
   prober, setting `holds`, `lapsed` and `reachable`. It probes
   this host's port and the switches first, and keeps their results
   whatever happens to the rest. Verification has a bound of half of
   `start_timeout`; if it fails or runs out, the start goes on from the
   rows as they are, which came from OpenSM's own cache,
   except that a start that would begin a transition starts OpenSM on
   the previous target instead, with an alarm, and a start with the
   target on whose turn-on hasn't succeeded yet doesn't start OpenSM at
   all, and is retried: the fabric may be part-way;
3. writes `neighbors` from `fabric_links` and `guid2mkey` from `holds`,
   root-only, by copying them into that container's `/var/cache/opensm`
   (`put_archive`; Docker allows this into a volume of a read-only
   container, checked on this host), and then starts it. So
   `ManagedDocker.start` becomes create, copy, start, rather than
   `containers.run`.

`guid2mkey` gets an entry only for a port whose key differs from what
OpenSM would send without one: with the target on, the old `m_key` for a
port holding it; with it off, the derived key for a port holding that. A
port with no entry, an `unknown` one included, gets OpenSM's own choice.
These files replace OpenSM's own, which step 1 folded in and step 2
verified: merging entries would let a stale one beat the derivation
(requirement 3). With no rows and the group not named, nothing is read
or written, as today.

If this host's own port answers neither key, OpenSM can't sweep (test
5's `ERR 5430`), and no render fixes that: the start fails with an alarm
naming the cause, as a lost M_Key does today.

**Turning it on.** OpenSM must know every port's GUID before the start
that turns per-port keys on, or it sends 0 and loses them (test 5's empty
leg). And it must be a start someone chose, not a crash restart. So
naming the group doesn't begin it: `--begin-port-keys` does. It's
refused unless the group is named, the base was stored by an earlier
start, links are recorded, and the prober, run beside the running
OpenSM, finds every recorded port behind an up link holding `m_key`
(and no row `unknown` behind an up link; one behind a down link is
re-keyed through OpenSM's own choice when it returns). Then it records the target on and tells the
operator to restart the manager. That start verifies, renders an `m_key`
entry for every port, and starts OpenSM with the option and the base,
which re-keys each port it reaches; the fingerprint is recorded before
OpenSM is started. A
port that's down at the time keeps its `m_key` row, and is re-keyed
through it when it returns. The transition is complete when every
recorded port that's reachable holds its derived key; until then the
check logs how many still hold `m_key`.

That start succeeds only when OpenSM logs SUBNET UP and, within
`start_timeout`, a cache read shows this host's port and every recorded
switch that answered the pre-start verify holding its derived key: test 5 saw SUBNET UP from an OpenSM that
reached only its own port, and the prober reaching a port with the
row's key doesn't show that OpenSM did. Otherwise, as when OpenSM doesn't
start at all, the manager stops
OpenSM and runs the prober: if no port has left its recorded key, and
none answers its derived key (one that an earlier, interrupted turn-on
keyed; a port that's down with a derived row is a turn-off's leftover,
which a set-back's own start handles), it
sets the target back to off, starts OpenSM without the option, and
raises an alarm; the fingerprint stays recorded, since OpenSM ran with
the option and may have keyed a port the prober can't see. A port that
answered its key before the start and answers nothing now counts as the
prober failing, not as nothing having moved. If some port has moved, it
starts OpenSM on the target
from the verified rows, and keeps retrying that, with the alarm, since
the fabric is then part-way. While the group is named or rows exist, a
start that fails doesn't end the manager: the applier retries it at
every check interval, as it does when OpenSM stops. If the prober itself fails, it retries it,
with the alarm, and doesn't start OpenSM on either target blind: the
ports keep their keys meanwhile, and a start on the wrong target is the
outage test 5 had. The
first run of the implemented group tests this path (Still to run).

**Turning it off.** Removing the group from the configuration sets the
target to off at the next start: an operator's change, and the safe
direction. The rows stay, so the start renders `neighbors` and a derived
entry for every port holding one, and OpenSM, without the option,
re-keys them to `m_key`, as test 5 did. If the manager stops before it
reads the result, the next start's prober tries both keys (the target
is off), so it finds them. Once every recorded port holds `m_key`, the
rows change nothing, and `--forget-port-keys` drops them.

**The fabric check.** While rows exist, `ibswitches -y <m_key>` can't
reach a re-keyed port, so the fabric check and `--confirm-switches` run
the prober on the recorded switches only, under the check's timeout.
A full verify, of every recorded port behind an up link and every
switch port's link state, runs on a slower period
(`[opensm] port_verify_interval`, 60 s by default, a positive number
`Config.validate` checks, and a full verify must fit within one), beside
OpenSM, on the applier's thread, so that the rows have one writer; the
check uses its last result, and fails if that result is older than two
periods. It
returns the switch node GUIDs that answered with the GUID recorded on
their route, as `ibswitches` does, so `_judge` is unchanged. A route
through a switch that doesn't answer isn't followed. A switch port whose
link is up with no recorded neighbour, in the last full verify, fails
the check, with the switch
and port named (requirement 2): something is attached that OpenSM
hasn't recorded, which may be a port it can't reach. `--add-link` records
a link by hand for such a port, once the operator knows what's there.
The SA query for SMInfo records is unchanged. A non-zero exit, or output
it can't parse, is a failed query (`ReloadError`), as today.

**Lost ports and jobs.** A lost port shows up in OpenSM's own heavy
sweeps, which the manager doesn't watch: a pkey sweep, which most of the
manager's are, does no discovery, and test 5's lost port still let
OpenSM log SUBNET UP. So job completion doesn't wait for log lines. With
rows present, a job that binds or unbinds a recorded port GUID needs one
more piece of evidence, after its sweep and before it completes: a
prober Get to that port that answers with its key (a lapsed port, which
answers any key, hasn't shown that), or its switch port's
PortInfo showing the link down (then OpenSM sets its pkey table when the
link comes up, as for any port that's down today). These Gets go only to
the GUIDs of open jobs, at each check, so their cost follows the jobs,
not the fabric. A job whose port doesn't answer is held, with an alarm
naming the port; its own tenant can cause that, and it holds no one
else's job. `--release-port <guid>` lets an operator complete such jobs,
for example for a node being retired. It completes them as they are:
it changes nothing on the port, and leaves the switch port's
enforcement table as it was. A job on a GUID with no row (a
virtual function, or a port not yet seen) completes as today, which is
safe only while every port that carries such ports is reached: a
recorded HCA port behind an up link that isn't a Neutron port's GUID (a
hypervisor's, or any other host's) and doesn't answer its key in the
last full verify fails the fabric check, with the port named, so no job
completes until it's reached again or an operator runs `--release-port`
for it. AFM can't tie a virtual function to its host's physical port
(it has `Port.host`, not the GUID), so this is fabric-wide. A tenant
who leaves their node refusing its key and then releases it makes its
GUID non-Neutron, so the check fails fabric-wide until the port is
reached or released: that fails closed, the alarm names the port, and
the hardening doc says so, so that operators recognise it.

The log lines are an extra trigger to fail closed. With rows present, a
watched sweep that logs `ERR 5411` "DR SMP Send completed with error",
which names no port (the route is only in a separate debug dump), holds
every job of the sweep. `ERR 3113` for a NodeInfo or PortInfo Get names
its `dest_guid`, and holds the jobs of that GUID if it's a Neutron port's;
otherwise (a hypervisor's or switch's port, or 0, which ha.md notes
needn't be the port's) it holds every job of the sweep. The match is on the
text, since a PerfMgr message also uses `5411`. A Bad M_Key trap names
its issuer's LID from the notice's own data, which the sender writes, so
traps hold nothing and attribute nothing: they're logged as a count,
with the key masked, since OpenSM's line includes it.

**Restores.** A database restored from before per-port keys has no rows,
or rows holding `m_key`, and maybe no base. If the volume's `guid2mkey`
has entries that match neither the old `m_key` nor the stored base's
derived keys, or the base doesn't match the recorded fingerprint, the
start keeps the target off, leaves the volume's files as they are, and
starts OpenSM without the option, with an alarm. That's test 5's working
turn-off: OpenSM reaches each port through its own cache and re-keys it
to `m_key`. Only if the volume is gone too are ports lost, and then the
hardening doc's recovery applies (a lease lapse, or in-band MFT with each
port's key from a newer backup). So backups taken after the turn-on are
the ones to keep.

A restored database that has the base but predates the turn-on (the
usual case, since the base is stored a release early) has the target
off, while the volume and the ports hold the base's derived keys. With
the group named and no fingerprint recorded, a start whose folded rows
show ports on the stored base's derived keys adopts the target on
(with a fingerprint, the database has seen a turn-on, and the rows are
a turn-off's or a set-back's leftovers, which `--begin-port-keys` must
check), decided after step 1 and
before step 2, so that the verify runs with the target on, records the
fingerprint, and raises an alarm saying so, rather than re-keying the
fabric back to the exposed `m_key`. An adopted target isn't held to the
turn-on's success rule, whose set-back would do exactly that, and is
recorded as the target OpenSM last ran on, so that a verify that fails
doesn't start OpenSM without the option either. Without the group named it's an
ordinary turn-off.

**Commands.** `--begin-port-keys` as above. `--port-key-states` lists each
recorded port's GUID, `holds`, `lapsed` and `reachable`, never a key.
`--port-m-key <guid>` prints that port's current key, for feeding `-y`
under AGENTS.md's rule, never for display. `--add-link` records a link.
`--release-port <guid>` completes a lost port's held jobs, and exempts a
non-Neutron port from the fabric check's rule on lost ports, until the
port answers its key again.
`--forget-port-keys` drops every row, refused while the group is named or
any row holds a derived key; it also drops a recorded fingerprint that
isn't the stored base's, which would otherwise keep every later start
on Restores' path; `--forget-port <guid>` drops one port and
its links, for removed hardware, refused for a port seen in the last day.

**Downgrades.** A release without the group can't render the cache, so
the notes say to turn the group off and wait for `--port-key-states` to
show every port holding `m_key` first, HCAs included.

**Logs.** `opensm.log` now holds keys: Bad M_Key traps print the key
that was sent. The hardening doc says the log volume is as secret as
`opensm.conf`. The manager never logs a line of OpenSM's that holds a
key, nor the prober's command line.

**Not yet decided.** A node that's down at the turn-on keeps `holds =
m_key`, and is sent `m_key` when it returns, perhaps with a new tenant
and an adapter that has reset its key. That matters only if a host can
read the key of an SMP sent to it, which Why assumes it might. Ageing
such rows to derived-only for adapters without a non-volatile M_Key is
the likely answer, once the implemented run shows what a reset port
holds. It's settled before the group joins `all`.

### Choosing the group

`m_key_per_port` leaves state on ports: their keys change, and `m_key`
alone no longer opens them. Test 5 runs before the group exists, with
hand-made files and a lease, so it decides the facts and the design, not
the group's place in `all`. The group stays experimental until a run of
the implemented group on one host, with a lease of 0 (its default, with
the fabric owner's agreement), its turn-off, and a port reset seen from a
second host. Then it joins `all` for single node under the rules in
Choosing the groups, including the mechanical guard for a group that
leaves state on ports.

## Not adopted

- **Rotating keys.** UFM re-randomises its key seed periodically. AFM's
  keys never change, and the HA spec already makes key rotation future
  work. Rotating the M_Key safely needs every port to be re-keyed while
  both keys are known, and a takeover in the middle of a rotation to know
  which key each port has.
- **The performance management class.** Neither UFM's profile nor OpenSM
  keys it, so a tenant can read, and reset, switch counters.
- **All HCAs in the same pkey.** UFM's single-pkey bare metal profile
  doesn't fit a fabric where each network is its own partition.

## MFT and the M_Key

UFM's profile requires firmware tools to use the M_Key. For AFM this is
operational, not a setting. The hardening doc will say that in-band MFT
tools (`flint -d lid-...`, `mlxlink`, `mlxconfig` over InfiniBand) need the
M_Key, and the VS key once `class_keys` is on. It will also say that such
tools must be run only from the managers' hosts, never from a tenant's.
In-band MFT is one of the hardening doc's recovery routes for a lost M_Key.
With `class_keys`, a lost seed blocks it too, which is another reason the
seed must be backed up before any port is keyed.

## Choosing the groups

Everything is hardened by default, and an operator opts out of what their
fabric can't take, by name: `hardening = -class_keys` hardens everything
but the class keys. (An earlier version of this spec kept new groups out of
`all` for good. The user chose hardening by default, with an easy opt-out,
instead.)

- **While a group is untested,** it is in `HARDENING_GROUPS` but not
  `ALL_HARDENING_GROUPS`. It can be chosen only by naming it beside `all`,
  for example `hardening = all, allowed_sm`. Each new group waits for its
  hardware tests (see Hardware tests), so until then it is experimental.
- **Before a group exists,** its options can be tried by hand in
  `[opensm_conf]`, without the checks the group will add.
  docs/opensm-hardening.md, Experimental hardening, documents them as
  additional hardening that isn't on by default, with their risks.
- **Once its tests pass,** it joins `all`. The release that adds it is
  fabric-affecting: its notes name the group, what it changes on the fabric,
  and how to opt out with `-group`. In HA mode the change needs an epoch,
  like any fabric-affecting change. The opt-out can only be written with the
  upgrade itself, since the older release refuses `-newgroup` as unknown, and
  a rollback must remove it again. On a fabric that others share, whoever
  shares it agrees before the upgrade, or the deployment uses an explicit
  list of groups, which no upgrade widens. AGENTS.md's rule on agreeing the
  hardening, and the deploy-host skill, cover such an upgrade, as well as
  a change to `[opensm] hardening`.
- **A group that leaves state on ports** (`class_keys`) needs a mechanical
  guard, not only release notes: the first start in which `all` gains it is
  refused until the operator names it or removes it explicitly. That's
  designed when the first such group is implemented.
- **`class_keys` leaves keys on every port,** which only its seed can
  remove. Before it joins `all`, the seed must have been in backups for a
  release (see The seed), and the hardening doc must give the procedure
  for turning it off.

`[opensm] hardening` grammar: groups, `all`, and `-group`, separated by
commas, or `none` alone. A list of removals alone starts from `all`.
`Config.validate` refuses a list it can't read one way only: unknown
groups (a misspelt removal would leave the group on), a group both named
and removed, removals beside groups without `all`, and `none` with
anything else. HA mode refuses removing `m_key` or `sm_key`.

## Changes to specs/ha.md

These go into specs/ha.md before the HA code changes:

- **Shared state, `nodes`:** each node's physical functions' port GUIDs,
  beside the adapters' node GUIDs. They're recorded only by a successful
  probe: at registration, then at every heartbeat until one succeeds, or by
  an operator's command. Also add the known SM
  ports, which keep a forgotten node's GUIDs.
- **Retiring a node:** commands to record and drop known SM ports,
  for a reused adapter, and a command to forget a node's row, which ha.md
  defers to this kind of check. Forgetting a row removes the node's ports
  from the list, keeps them among the known SM ports, and is refused for a
  node that is live or in the target set. The leader then re-renders.
- **Configuration:** `opensm.conf` includes `allowed_sm_guids`, generated
  from the node rows. An override is part of the configuration hash; the
  generated list isn't. `applied` records the running list.
  `key_lease_period` is in the hash, as fabric-affecting (already made in
  ha.md). A lease no longer than a takeover's longest is warned about, not
  refused, so that a test fabric can use a short one.
- **Job completion, fabric checks:** `ERR 0F09` naming a known SM port
  fails the check and contests the fabric, until a direct query shows
  NOT-ACTIVE or IsSM clear. Naming another port, it raises the unknown-SM
  alarm. The managed-node SM rule matches SMInfo records' port GUIDs
  against the node rows' port GUIDs, not their adapters' node GUIDs.
- **Candidates:** a node with no port GUIDs can't be a candidate. A
  candidate inside `applied`'s running list is preferred. One outside it
  may take over as Changing the list says, which hardware test 1 decides.
  An alarm is raised while a live node is outside it.
- **Required keys:** HA mode refuses `allowed_sm` until its dependencies
  (see HA) are in, and `class_keys` until hardware test 3 passes.
- **Per-port M_Keys:** `fabric_ports` (with each port's key state) and
  `fabric_links`, written only by the leader under its term's
  compare-and-set; `m_key_base` among the shared keys; in Job
  completion and the OpenSM errors table, a job on a recorded port
  needing a keyed Get or a down link after its sweep, and `ERR 5411`'s
  DR SMP message and `ERR 3113`'s NodeInfo and PortInfo timeouts; the cache rendered before every start and on a period
  for the reach probe; `m_key_per_port` in the configuration hash; the
  fence ask with the target port's derived key. See Per-port M_Keys, The
  design.

## Hardware tests

These run on a fabric that nobody else shares, since they run a second SM,
send SMInfo Sets and flood the SA. They use two hosts, A and B. Each test
says which design choice its outcome decides.

1. **Allowed SMs.** A runs AFM's OpenSM. B runs OpenSM at a higher
   priority, in these runs:
   - without a list, B with a wrong SM_Key, then with an SM_Key of 0;
   - with A's list holding only A, B with a wrong SM_Key, then with an
     SM_Key of 0, then with the fabric's keys;
   - with both listed, B with the fabric's keys;
   - asymmetric lists: A is master with a list holding only A; B, with the
     fabric's keys and a list holding both, starts second. Record B's
     state, B's SMInfo records of A, and whether A answers B's SMInfo Get;
   - with A's list holding only A, B with the fabric's keys, and B's
     OpenSM then stopped with SIGSTOP. Record what the direct SMInfo Get
     returns, whether PortInfo's IsSM bit stays set, and whether A still
     logs `0F09`.

   Each run records:
   - A's state;
   - B's state, as A's SMInfo records show it and as a direct SMInfo Get
     shows it;
   - how often A logs `0F09` (once, or every sweep);
   - whether B, believing it is master, changes any port.

   Two further runs record whether A's OpenSM starts when its own port
   isn't in its list, and whether `allowed_sm_guids 0` behaves like a list
   holding only A. The fence ask is then sent as AFM will send it: by a
   tool on B, by directed route, before B's OpenSM runs, from B's second
   port too. The test records whether A honours it with both hosts listed,
   and what `2F1C`'s "sender guid" is.

   *Decides:* the group's case (Why), the fabric check's rule for ignored
   SMs, whether a candidate outside the running list may take over
   unconfirmed, and whether `allowed_sm` is allowed in HA mode.
2. **Changing the list.** With A running, add B's GUID, then remove it,
   each followed by a SIGHUP. Record whether each takes effect, what `0F09`
   and the "not longer a part" message show, and which other options a
   SIGHUP re-reads.

   *Decides:* SIGHUP or a leadership move (Changing the list).
3. **Class keys.**
   - A keys the fabric with `class_keys`.
   - A keyed VS query from A works, and an unkeyed `mlxlink` from B fails.
   - A stops. B starts with the same seed and an empty `/var/cache/opensm`.
     B logs none of the key managers' errors (`9803`, `9822`, `9823`,
     `9B03`, `9B22`, `C1xx`). Keyed queries still work and unkeyed ones
     still fail. B can set a port's VS key.
   - Stale cache: B keys ports added during its term, then stops, and A
     leads again with its old cache. A manages every port, the new ones
     included, as every HA takeover starts from a stale cache.
   - With leases of 0, the keys survive an hour with no SM while B sends
     unkeyed MADs.
   - B starts with a different seed: record what it logs, and whether it
     can recover the ports.
   - The turning-off procedure: with enable 1, the keys are cleared, and
     the test finds how to tell that it has finished.

   *Decides:* whether `class_keys` is allowed in either mode, the lease
   value, and the downgrade procedure.
4. **SA rate.** Two guests on virtual functions of one physical port, and
   one guest elsewhere. One guest floods the SA. Record:
   - whether `4C11` names the guest's GUID and limits only it;
   - whether a requester with the SA_Key is limited;
   - the SA's total request rate.

   Then run an MPI job's start-up across the fabric, and check it logs no
   `4C11`.

   *Decides:* whether `sa_rate` is added, and its value.
5. **Per-port M_Keys.** One host, on a fabric nobody else uses, with its
   owner's agreement, and `m_key_lease_period 600`: longer than any leg,
   so that a lapse can't pass for a result, and short enough that every
   port comes back on its own if the cache is lost. No run uses a lease
   of 0. Each leg prints counts and matches only, never a key. OpenSM is
   stopped and started through the manager, which would otherwise restart
   it while its cache is being changed.

   **Checking state** means a PortInfo Get with the key the port should
   hold, to every physical port, reading its protection bits (2), its
   lease, and its M_Key compared with the expected key: never another
   wrong-key probe. A state check that fails counts as a wrong-key SMP:
   it is timed like one, and not retried in a loop. Every wrong-key or
   zero-key SMP starts a port's lease
   countdown, and OpenSM's light sweeps may not query adapters, so each
   leg that sends one is timed from its first such SMP and restored within
   5 minutes. A port found lapsed (protection bits 0) is waited for until
   OpenSM protects it again, and the time recorded, before the next leg.

   - **Backup A:** the database and OpenSM's whole cache, checked against
     the live files. Every physical port's GUID has a `guid2mkey` entry
     equal to `m_key`; if not, stop.
   - **The lease:** with the option off, set the lease to 600, restart, and
     check state: every physical port's lease reads 600.
   - **Trap control:** one deliberate wrong-key Get to one adapter behind
     the switch, to confirm OpenSM logs the bad M_Key trap at this
     `log_flags`, so that its absence later means something. Then check
     that port's state, and again after 11 minutes. If it is still
     protected, keyed Gets or sweeps stop a countdown, and the test goes on
     as written. If it lapsed, every later step that sends a wrong key
     (the gate included) is followed by a wait until OpenSM has protected
     every port again, checked by state, before the next leg's
     observations.
   - **Turn-on from a manager-written cache:** OpenSM stopped, `guid2mkey`
     replaced by derived keys while ports still hold `m_key`, then started
     with the option. Expected to fail (ports refuse the derived key).
     Record it, and the SM's own port's entry and key afterwards (OpenSM
     may re-key its own port, and whether local SMPs are key-checked isn't
     known; if it did, that one entry is kept as derived in the next leg).
     Then backup A's cache goes back, the option off, and state is checked.
   - **Turn-on with a warm cache:** start with the option. *Gate:* every
     entry in `guid2mkey` equals `port_m_key`, and state checks with the
     derived keys, lease 600 included; then a NodeInfo Get with `m_key`
     fails, sent before a last derived-key Get answers, on every physical
     port but the SM's own. If the gate fails, turn the option off with the
     cache in place, check state with `m_key`, and stop.
   - **Backup B:** the cache after turn-on, which is the way back from
     here.
   - **Discovery:** `ibdiagnet --discovery_only --m_key_files` with
     rendered files, by `docker exec` in the running OpenSM container
     (output in a private directory on its log volume, removed after) and
     from a separate container (output in a private host directory). Record
     whether it opens the adapter beside OpenSM, which switches it finds,
     how long it takes, and its exit status, and that OpenSM logs no
     bad-M_Key trap or port change during it. The `smpquery -D` prober is
     tried for every switch the same way.
   - **Cold starts:** OpenSM stopped and started with its cache replaced
     by each of: nothing; `neighbors` only; `guid2mkey` only; rendered
     `guid2mkey` and `neighbors`; the rendered files without the GUID of
     an adapter behind the switch (not the switch, nor this host's link,
     which would repeat the empty case); the rendered files with that
     adapter's `neighbors` row naming the wrong GUID. Each records, within
     two sweeps, which ports OpenSM manages and what it logs; then backup
     B goes back, OpenSM restarts, and state is checked.
   - **Turn-off with the cache in place:** state checks with `m_key` on
     every physical port, and `guid2mkey` holds `m_key` for every GUID.
   - **Revert:** the original `afm.conf`; state checks with `m_key`,
     protection bits 2 and the original lease on every physical port; and
     a cache matching backup A's GUIDs and keys.

   A port reset can't be observed from one host (resetting this host's
   adapter resets the SM's own port), so whether adapters keep their key
   waits for a second host, as does HA.

   *Decides:* whether OpenSM writes `guid2mkey` with the option off,
   whether `neighbors` alone lets a cold OpenSM reach the ports, what the
   manager must render, and whether `ibdiagnet` or the prober can be the
   fabric check. Not whether the group joins `all` (see Choosing the
   group).

### Results

**Test 3, first run, 2026-11-13.** On the DevStack host's fabric, which its
owner confirmed nobody else used during the test: one switch (LID 5) with
its embedded CA (LID 8), this host's single physical port, and five other
adapters. Hosts and switch run OpenSM
5.17.2.MLNX20240610. With no second host, a restart of the same OpenSM
stood in for a takeover. The leases were 60 s, not 0. Everything was
reverted afterwards, and `afm.conf`, `opensm.conf` and the cache's file
set matched the backup.

- **Keys set:** with `vs_key_enable 2` and a seed, OpenSM wrote a VS key
  for every port to `guid2vskey`.
- **CC and N2N keys aren't supported here:** the switch and adapters
  answer KeyInfo with status `0xc` (method or attribute not supported),
  logged as `ERR 9828` at every sweep. The CC and N2N caches stayed empty.
  `class_keys` must not treat `9828` as a failure, or it fails on such
  hardware.
- **Whether VS keys block unkeyed access wasn't shown.** The run used
  unkeyed `mlxlink` as its probe, but `mlxlink` reads the device ID with an
  SMP first, which the M_Key (protection level 2) refuses without the key.
  Its failures, before and during the keyed runs, were the M_Key's doing.
  Its successes after the quiet three minutes were the M_Key's protection
  lapsing (see the next bullet). So nothing in this run shows VS key
  protection working, or `enable 1` clearing it. The next run needs a VS
  probe that sends no SMP, or `mlxlink` given the M_Key, and must verify
  Turning the keys off, step 1. An unkeyed `vendstat -N` (General Info),
  which sends VS MADs only, answered throughout, with and without VS keys.
- **The M_Key lease lapsed, as Permanent keys warns.** The fabric ran with
  `m_key_lease_period 60`. Unkeyed `mlxlink` probes, which send unkeyed
  SMPs, started the switch's and its CA's countdowns. Three minutes with
  no SM then dropped their protection, and unkeyed SMPs to them answered,
  which at that level returns the M_Key, for some minutes after OpenSM
  started again. They were protected again within half an hour. The fabric
  had no other users, but this is the exposure
  docs/opensm-hardening.md, Permanent keys, describes, happening in
  practice. When a running OpenSM puts back a port's lapsed protection
  isn't known: the next run should record it.
- **The seed alone decides VS keys, on one host:** after moving the class
  key caches aside, OpenSM derived identical keys (the same hash over the
  8 ports: the switch, its CA and six adapters, this host's included),
  and logged no key errors other than `9828`. `guid2mkey` and the rest of
  the cache stayed.
- **A changed seed didn't recover while OpenSM ran:** with a new seed,
  OpenSM wrote new keys to its cache. KeyInfo Gets to the switch and its CA
  then timed out every 10 s (`ERR 980B`, `ERR 5410`, class `0xa`, attribute
  `0xf`), for more than five minutes, despite the 60 s lease. No VS probe
  that avoids the M_Key was tried during those minutes, so whether the
  ports kept their VS keys isn't known.
- **Not explained yet:** the same timeouts also appeared on a restart with
  the original seed. That restart followed unkeyed `mlxlink` probes of
  the switch. They continued with `vs_key_enable 1`, and stopped only with
  enable back at its default, when OpenSM sends no KeyInfo at all. The
  rest of test 3 retests this with a probe-free run.

After the run, OpenSM, with no class key options, logged no errors, and
every physical port refused unkeyed SMPs and answered keyed ones: M_Key
protection was back. Whether any port still holds a VS key from the test
seeds isn't known, since no probe that shows it worked. If one does, it
holds it with a 60 s lease, which is expected to run out after a
wrong-keyed VS MAD and a minute with none, if class key leases behave like
the M_Key's. That isn't shown.

Still to run for test 3:
- a lease of 0, which must verify that `enable 1` clears the keys;
- unkeyed queries during a wrong-seed run, and a run with no unkeyed probes;
- whether a power cycle clears class keys;
- the stale-cache leg;
- a keyed query by a tool that works while OpenSM runs;
- a second host;
- CC and N2N keys on hardware that supports them.

**Test 5, 2026-11-16.** The same fabric and host as test 3, with its owner's
agreement and nobody else using it; one switch (LID 5), its embedded CA,
this host's port (LID 1) and five other adapters: 8 physical ports. The
lease was 600 s throughout. Everything was reverted: every port holds
`m_key` at protection level 2 with its original 60 s lease, and
`afm.conf`, `opensm.conf` and the cache files are byte-identical to
backup A.

- **The derivation is right.** After a warm turn-on, every `guid2mkey`
  entry equalled `port_m_key`, the switch's included, and every port,
  this host's too, answered its derived key and refused `m_key`. OpenSM
  writes `guid2mkey` with the option off: backup A held `m_key` for every
  GUID.
- **A keyed Get stops a countdown.** A port sent one wrong-key Get, and
  then a keyed one, was still protected 11 minutes later. OpenSM logs the
  Bad M_Key trap (256) at this `log_flags`, with the key that was sent.
- **Turn-on from a cache of derived keys fails at the SM's own port.**
  With ports still on `m_key`, OpenSM's first SMP, to its own port, was
  refused at send (`ERR 5430`, "Invalid argument"), then `ERR 3309`/`3320`
  "SM port ... is unknown": it never swept, and no other port saw a wrong
  key. Local SMPs are key-checked, refused before they leave the host.
- **`neighbors` alone is enough.** A cold OpenSM with only `neighbors`
  managed every port, logged no error, caused no M_Key violation, and
  wrote every port's derived key to `guid2mkey`. With `guid2mkey` alone,
  or nothing, it reached only its own port: the switch refused key 0 (89
  Bad M_Key traps in two sweeps) and the SA held no nodes.
- **A missing or wrong `neighbors` row loses that port.** Without one
  adapter's GUID, or with its row naming another GUID (its `guid2mkey`
  entry intact), OpenSM managed the other nine nodes, and the adapter
  refused it until its row came back. OpenSM still logged SUBNET UP, so
  a fabric check that counts switches passes.
- **`ibdiagnet` can't be the fabric check here.** It failed to open the
  adapter ("Ibis failed to get CAs in the system", or "couldn't CA: mlx5_0
  and port: 1" when named), by `docker exec` beside OpenSM and from a
  separate container, in 0.3 s, before sending anything. `smpquery -D -y
  <derived key> nodeinfo 0,1` reached the switch in 0.13 s.
- **AFM in HA mode can't start OpenSM once the ports are re-keyed.** The
  first restart through the manager after turn-on never took the lease:
  the reach probe's `ibswitches -y <m_key>` failed at this host's own
  port, so the node's reach was 0 and it wasn't eligible. The fabric had
  no SM from 10:13:51 to about 10:23, when OpenSM was started by hand as
  the managed driver runs it, with the manager stopped; existing links
  stayed up, and no port lapsed. The rest of the test ran that way. Until
  then, the fabric check and the reach probe failed every 5 s, refused at
  send on this host's port, which logged a Bad M_Key trap each time.
- **Turn-off with the cache in place works.** OpenSM re-keyed every port
  to `m_key`, with no error, and wrote `m_key` back to `guid2mkey`.

What it means for the design (Per-port M_Keys, The design):
- The manager renders `neighbors` from `fabric_links`, and OpenSM
  derives the keys. `neighbors` holds GUIDs and port numbers only, so it
  isn't a secret. `guid2mkey` needs an entry only for a port whose key
  isn't the one OpenSM would choose: one still on `m_key` at turn-on, or
  on its derived key at turn-off (requirement 1).
- The fabric check, `--confirm-switches` and the reach probe use the
  `smpquery -D` prober along recorded links (requirement 4); `ibdiagnet`
  is dropped.
- The reach probe must work before the group is turned on anywhere in
  HA, since a node that can't count switches can't lead; the HA refusal
  in requirement 5 stands until it does.
- Requirement 2's unattributed timeouts are needed: a lost port doesn't
  stop SUBNET UP.

Still to run for test 5 (with the implemented group): a turn-on with a
new base key (`m_key` differing from the key `guid2mkey` names), and a
turn-on whose start fails and is set back; a lease of 0, a port reset seen from a second host, a takeover on a second host,
and a re-cabled port.

## Implementation plan

- [x] `key_mgr_seed` in `KEYS`, generated at startup, in `--show-keys`;
      refuse a zero or changed override. Ship this a release before
      `class_keys`.
- [x] `all` with named extras in `Config.validate`.
- [x] `-group` removals from `all`.
- [ ] Each group joins `ALL_HARDENING_GROUPS` once its hardware tests
      pass, with release notes.
- [ ] Hardware tests 1 and 2. Then `allowed_sm` for single node, with the
      physical-port probe, override checks, `ERR 7549` as a failed start,
      the lock-out checks, the `0F09` rules, and the commands to record
      and drop known SM ports.
- [ ] specs/ha.md changes. Then node rows' port GUIDs, known SM ports, and
      `allowed_sm` in HA mode, once its dependencies are in.
- [ ] Hardware test 3 (first run done, see Results). Then `class_keys`,
      with its downgrade procedure.
- [ ] Hardware test 4. Then `sa_rate`, or a note that the gap stays.
- [x] Hardware test 5's first run (see Results).
- [x] `m_key_base` in `KEYS`, generated whatever the groups, a release
      before `m_key_per_port`.
- [ ] `m_key_per_port` for single node, as Per-port M_Keys, The design
      says, with requirement 7's changes to README.md, docs/kayobe.md,
      AGENTS.md, the deploy-host skill and `--show-keys`'s help;
      experimental until its single-node run passes.
- [ ] docs/opensm-hardening.md: the new groups, MFT and the VS key, the
      downgrade procedure.

## Open questions

- Which in-band tools can take the VS key, and how?
- SHARP: AFM runs no Aggregation Manager, and OpenSM leaves SHARP enabled
  on switches that support it (`sharp_enabled 2`). The proposal is a
  `sharp` group that sets `sharp_enabled 1` (Disable on all supporting
  switches), once whoever shares the fabric confirms that nobody uses
  SHARP. Should it be added?
- Is UFM's MAD limiter more than an SA rate limit, for example a limit on
  SMPs or VS MADs from hosts? If so, does OpenSM 5.17 have an equivalent?
