# The secured cloud profile

Status: proposed, 2026-11-13.

UFM's Secured Cloud Profile (`cloud_profile = 2` in UFM Enterprise 6.26)
is NVIDIA's list of protections for a fabric that bare metal tenants share.
AFM's hardening (docs/opensm-hardening.md) already covers most of it. This
spec adds the rest that OpenSM 5.17 can do, as new hardening groups, and
says why the remaining items are left out.

The new groups are opt-in and stay out of `all` (see Choosing the groups).
Each one waits for hardware tests whose outcomes decide parts of its
design.

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
vs_key_lease_period 0
cc_key_enable 2
cc_key_protect_bit 1
cc_key_lease_period 0
n2n_key_enable 2
n2n_key_protect_bit 1
n2n_key_lease_period 0
key_mgr_seed <stored>
```

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
- An `[opensm_conf] key_mgr_seed` override that is `0`, or differs from the
  stored seed, is refused, as ha.md refuses keys other than the stored ones.

OpenSM also caches class keys per port (`guid2vskey`, `guid2cckey`,
`guid2_n2n_key`, beside `guid2mkey`) in `/var/cache/opensm`. If those
caches, rather than the seed, decide a port's key, a database backup isn't
enough, even on a single node. Hardware test 3 checks that an OpenSM with
an empty cache and only the seed manages ports that another OpenSM keyed.
Until it passes, the group is refused in single-node and HA modes.

### Leases

The leases are 0 for the same reason as the M_Key's (see
docs/opensm-hardening.md, The lease). AFM stops OpenSM during restarts and
takeovers, and a lease that ran out while no SM ran would let a host clear
a port's protection. Hardware test 3 checks that a lease of 0 means "never
expires" for these classes, as it does for the M_Key. If it doesn't, the
group uses the longest lease OpenSM accepts, and the doc says so.

### Turning the keys off, and downgrades

Ports keep their keys until an SM that has them changes them. To turn a
class off:

1. Set `<class>_key_enable 1` (Disable) in `[opensm_conf]`, and restart.
   OpenSM then clears the keys from the ports.
2. Check that it has finished, as hardware test 3 establishes.
3. Only then drop `class_keys`.

Dropping the group at once sets enable to 0 (Ignore), which leaves ports
protected by keys that the SM no longer manages. A downgrade to a release
without the group has the same effect. So the release notes, and the
hardening doc's downgrade section, require step 1 before any downgrade.

### Cost

In-band tools that send VS MADs, such as `mlxlink`, `mlxreg`, in-band
`flint`, `ibdiagnet`, and in-band switch firmware upgrades, need the VS key.
Which of them can take it, and how, is an open question. The doc's
Diagnostics section will list them once it's answered. AFM itself sends
only SMPs and SA requests, so its fabric check, reach probe and fence ask
are unaffected.

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

## Not adopted

- **Rotating keys.** UFM re-randomises its key seed periodically. AFM's
  keys never change, and the HA spec already makes key rotation future
  work. Rotating the M_Key safely needs every port to be re-keyed while
  both keys are known, and a takeover in the middle of a rotation to know
  which key each port has.
- **`m_key_per_port`.** This gives every port its own M_Key, so a leaked
  port key exposes only that port. It isn't adopted until it's known how
  OpenSM derives the keys, for the same reason as the class keys' cache.
  AFM's fence ask, fabric check and diagnostics would also need each target
  port's key.
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

`all` keeps meaning the six groups it has today. The new groups are named
beside it, for example `hardening = all, allowed_sm`. `Config.validate`
accepts `all` together with named groups, but still refuses `none` with
anything else. So an upgrade never turns on a new group by itself, and
each one is a deliberate choice that whoever shares the fabric has agreed
to, as AGENTS.md requires.

- `allowed_sm` and `sa_rate` leave no state on ports. Whether either
  should later join `all`, with an upgrade that asks for an explicit
  acknowledgement, is decided after deployments have used them.
- `class_keys` stays out of `all` for good, because it leaves keys on every
  port that only its seed can remove.

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

## Implementation plan

- [ ] `key_mgr_seed` in `KEYS`, generated at startup, in `--show-keys`;
      refuse a zero or changed override. Ship this a release before
      `class_keys`.
- [ ] `all` with named extras in `Config.validate`.
- [ ] Hardware tests 1 and 2. Then `allowed_sm` for single node, with the
      physical-port probe, override checks, `ERR 7549` as a failed start,
      the lock-out checks, the `0F09` rules, and the commands to record
      and drop known SM ports.
- [ ] specs/ha.md changes. Then node rows' port GUIDs, known SM ports, and
      `allowed_sm` in HA mode, once its dependencies are in.
- [ ] Hardware test 3. Then `class_keys`, with its downgrade procedure.
- [ ] Hardware test 4. Then `sa_rate`, or a note that the gap stays.
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
