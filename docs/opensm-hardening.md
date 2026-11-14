# OpenSM hardening

OpenSM's defaults suit a fabric whose hosts all trust each other. A cloud's
fabric doesn't: a bare metal tenant has full control of its InfiniBand
adapter, and can send any subnet management packet (SMP) or subnet
administration (SA) request, or run a subnet manager of its own. A guest on
a virtual function has less control, but can still make SA requests.

So with the managed driver, the manager writes a hardened set of options into
`opensm.conf`, in groups. Everything is hardened by default, and you opt out
of what your fabric can't take. `[opensm] hardening` says which groups, in
one line, separated by commas:

| `hardening` | Groups |
| --- | --- |
| `all` (the default) | Every group below |
| `all, -sa_trust` or `-sa_trust` | Every group but `sa_trust` |
| `-sa_trust, -babbling_ports` | Every group but those two |
| `m_key, enforcement` | Only those two |
| `none` | None |

A group prefixed with `-` is removed from `all`. A list of removals alone
starts from `all`, since that's the default. The manager refuses a value it
can't read one way only, rather than guess:
- an unknown group, including a misspelt removal, which would otherwise leave
  the group on;
- a group both named and removed;
- removals beside a list of groups without `all` (list the groups to keep
  instead);
- `none` with anything else.

HA mode refuses removing `m_key` or `sm_key`, which it depends on.

Further hardening that matches the rest of NVIDIA's Secured Cloud Profile is
experimental: not tested enough to be on by default, and not part of `all`.
You can try it by hand on a fabric you control (see Experimental hardening).
If a later release makes one of these a group in `all`, its release notes
will say so, and `-group` removes it. A single OpenSM option can still be
overridden in `[opensm_conf]`, for example `sa_etm_max_num_mcgs = 256`, and
wins over the groups.

The keys the manager sets on ports are permanent by default: a port never
drops its protection. `[opensm] key_lease_period` opts out of that for every
such key at once (see Permanent keys).

| Group | Option | OpenSM default | Hardened | Stops |
| --- | --- | --- | --- | --- |
| `m_key` | `m_key` | `0` | Random, shared | A host reading or changing port settings with SMPs |
| `m_key` | `m_key_protection_level` | `0` | `2` | As above |
| `m_key` | `m_key_lease_period` | `0` | `[opensm] key_lease_period`, `0` by default | A port giving up its protection, and its M_Key, while no SM runs |
| `sm_key` | `sm_key` | `0x1` | Random, shared | A rogue subnet manager taking over by handover |
| `sa_key` | `sa_key` | `0x1` | Random, shared | Any client being trusted by the SA with the well-known key |
| `sa_trust` | `sa_enhanced_trust_model` | `FALSE` | `TRUE` | Untrusted clients using SA requests beyond the minimal set |
| `sa_trust` | `sa_etm_allow_untrusted_proxy_requests` | `TRUE` | `FALSE` | A client acting for another port |
| `sa_trust` | `sa_etm_allow_untrusted_guidinfo_rec` | `TRUE` | `FALSE` | A client adding alias GUIDs to its port |
| `sa_trust` | `sa_etm_allow_guidinfo_rec_by_vf` | `TRUE` | `FALSE` | A virtual function adding alias GUIDs |
| `sa_trust` | `sa_etm_max_num_mcgs` | `0` (no limit) | `128` | One port exhausting the SA's multicast groups |
| `sa_trust` | `sa_etm_max_num_srvcs` | `0` (no limit) | `32` | One port exhausting the SA's service records |
| `sa_trust` | `sa_etm_max_num_event_subs` | `0` (no limit) | `32` | One port exhausting the SA's event subscriptions |
| `enforcement` | `part_enforce` | `both` | `both` | Packets crossing partitions at a switch port |
| `enforcement` | `allow_both_pkeys` | `FALSE` | `FALSE` | A port being both a full and a limited member of a partition |
| `enforcement` | `sa_check_sgid_spoofing` | `TRUE` | `TRUE` | A client claiming another port's GID in an SA request |
| `babbling_ports` | `babbling_port_policy` | `FALSE` | `TRUE` | A port flooding the subnet manager with traps |

The `enforcement` group is OpenSM's defaults already. It is written anyway,
so that `opensm.conf` shows the options and nothing weakens them by
accident.

## Choosing what to harden

| Fabric | `hardening` |
| --- | --- |
| Only OpenStack's hosts, guests and bare metal nodes (the default) | `all` |
| With ConnectX-3 SR-IOV | `all`, with `sa_etm_allow_untrusted_guidinfo_rec = TRUE` in `[opensm_conf]` (see the SA enhanced trust model) |
| Shared with hosts outside OpenStack that need untrusted SA dumps, such as `saquery` | `-sa_key, -sa_trust`, which keeps every other group, and groups added later |
| Shared with hosts whose operators can't use the M_Key yet | `enforcement`, and plan M_Key protection: without it, a bare metal tenant can reconfigure switch ports |

## The keys

The manager generates `m_key`, `sm_key` and `sa_key` on first start, as
random 64-bit values, and keeps them in its database. Every OpenSM of the
fabric must use the same keys: in high availability, a standby that took
over with another M_Key couldn't configure the ports. Keeping them in the
database gives every manager the same keys, and keeps them across restarts.
Only the keys of the chosen groups are written. A key set in
`[opensm_conf]` wins over the generated one.

The manager also stores `key_mgr_seed`, if it's missing, whenever it starts
OpenSM (in HA mode, when a node takes over), whatever the groups, though no
group writes it yet. OpenSM derives the class keys (VS, CC and N2N) from it,
and a port keyed with a lost seed can't be managed again, so the seed is
stored a release early, for database backups to hold it before it is ever
used. `--show-keys` prints it, and it is as secret as the other keys.

Unlike the other keys, the stored seed can't be replaced. If
`[opensm_conf] key_mgr_seed` is set when no seed is stored, as on a fabric
whose class keys were set by hand, the manager stores that value rather than
a random one, so that the ports' seed is kept. Otherwise the manager refuses
an override that differs from the stored seed. The override must be a
non-zero 64-bit value, in hexadecimal (`0x...`) or decimal without leading
zeros.

The keys are in:

- the manager's database, which the manager creates readable only by root;
- `opensm.conf`, which the manager writes readable only by root;
- OpenSM's log: at startup OpenSM prints every option it loads, the keys
  included ("Loading Cached Option:m_key = ..."), so the OpenSM container's
  log, and wherever it is shipped, holds them;
- OpenSM's cache, `guid2mkey` in `/var/cache/opensm`, which holds each port's
  M_Key.

Keep all four from tenants and from log shipping. To print the keys:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager --show-keys
```

## M_Key protection

The M_Key is what stops a host from reconfiguring the fabric. Without it, any
host can send an SMP that changes a port's settings, including a switch
port's pkey table, which is what switches enforce partitions with. A bare
metal tenant could then add its own switch port to any partition.

With `m_key_protection_level 2`, a port answers a SubnGet, and accepts a
SubnSet, only if the SMP carries its M_Key. OpenSM sets the M_Key on every
port when it configures it.

### The lease

A port that receives an SMP without its M_Key sends the SM a Bad M_Key trap
and, if its lease period isn't 0, starts a countdown. An SM that answers with
the right M_Key stops it, and OpenSM's sweeps do too. If the countdown reaches
zero, the port drops to protection level 0, where a query returns the M_Key
itself (opensm(8)).

So a lease is a window: while no subnet manager runs for longer than the
lease, a host sending SMPs without the M_Key gets a port's protection, and the
M_Key, back. The manager stops OpenSM whenever it restarts, and in high
availability every OpenSM is shut down while the database can't be reached,
so such windows are expected. The hardened lease is therefore `0`: a port
never drops its protection.

The cost is recovery. With a lease of `0`, a fabric whose M_Key is lost, for
example with the manager's database, can't be configured by a new subnet
manager until every port is reset, by power-cycling or by firmware tools.
Back up the database. A fabric that prefers recovery over that exposure can
give every key a lease with one option, as Permanent keys describes.

## Permanent keys

Some keys are stored on the ports themselves, not only in the subnet
manager: today the M_Key, and the class keys once a group sets them. With a
lease of `0`, the default, a port keeps its key and its protection for good.
That is the safest setting against tenants, and the hardest to recover
from: a port whose key is lost, with the database or by a mistake, stays
locked until it's power-cycled or reset by firmware tools.

`[opensm] key_lease_period` opts out of permanent keys in one line. It sets
the lease of every key the manager puts on ports, now and in groups added
later:

```ini
[opensm]
# 0 (the default): permanent keys. Otherwise seconds, up to 65535.
key_lease_period = 600
```

With a lease, a port that is sent a packet with the wrong key starts a
countdown. Packets with the right key, such as OpenSM's sweeps, stop it.
If the countdown runs out, the port drops its protection. For the M_Key,
that's protection level 0, where a query returns the M_Key itself. So a port
keeps its protection while a subnet manager with the right key reaches it,
and can lose it once the lease passes without one.

Choose the lease with both sides in mind:

- **Exposure: one lapse gives the key away for good.** The countdown is
  started by whoever sends the wrong key, so any tenant can start it on its
  own switch port at will. If no subnet manager reaches that port for
  longer than the lease, the tenant reads the M_Key. That is one M_Key for
  every port of the fabric, and the manager can't rotate it yet. Once the
  subnet manager is back and the port is protected again, the tenant still
  holds the key, and can read and set any port on the fabric for good. So
  treat any outage with no subnet manager that lasts longer than the lease
  as a compromise of the M_Key. The manager stops OpenSM whenever it
  restarts, and in HA mode during a takeover, and while the database can't
  be reached. Make the lease longer than the longest such outage you
  expect: minutes, not seconds.

  This has happened on a test fabric. With a 60 s M_Key lease, a few
  unkeyed queries to a switch, then three minutes with no subnet manager,
  dropped the switch's protection. It answered unkeyed queries, and so
  would have given out the M_Key, for some minutes after the subnet manager
  started again (see specs/secured-profile.md, Results).
- **Recovery is expected, not yet shown.** A lease is meant to let a port
  whose key was lost be keyed again. The lapse above shows the first half
  happening, but no test has yet shown a port keyed with a lost M_Key keyed
  again. The countdown starts only on a wrong-keyed packet, and a quiet
  fabric sends none. So the expected procedure is:
  1. Start a subnet manager with the keys you now have, once. Its wrong-keyed
     packets start every port's countdown.
  2. Stop every subnet manager.
  3. Wait longer than the lease.
  4. Start the subnet manager again. The ports' protection has dropped, so
     it can set its keys on them.

  For class keys there's no evidence either way yet. In the first hardware
  test, ports keyed with another seed timed out OpenSM's key queries for
  more than five minutes, despite a 60 s lease, while OpenSM ran. But the
  same timeouts also occurred with the right seed, so the cause isn't known
  (see specs/secured-profile.md, Results).

A short lease, such as `60`, is for testing hardening on a fabric that
nobody else depends on. A mistake there may become recoverable within
minutes, by the procedure above. Never run a tenant fabric with a lease
that short: every restart of the manager would give any tenant the M_Key.

The option covers every key lease that a hardening group writes. A lease
set for one key in `[opensm_conf]`, such as `m_key_lease_period = 0`, still
wins over it, and the manager logs a warning at start, since that key is
then not covered. A fabric that followed earlier versions of this doc, with
`m_key_lease_period = 600` in `[opensm_conf]`, should move it to
`[opensm] key_lease_period = 600`. The manager logs at start the leases it
writes, or that it writes none.

In HA mode every node must have the same `key_lease_period`, since each
writes its own `opensm.conf`. Otherwise every takeover changes every port's
lease. It's fabric-affecting, like the hardening groups (specs/ha.md,
Configuration epochs). The manager warns when the lease is shorter than the
longest a takeover can leave the fabric without a subnet manager, since
every unclean takeover would then be a lapse.

Changing the option takes effect when the manager restarts, which restarts
OpenSM. OpenSM is expected to set the new lease on a port when it next
configures it with the right key. That hasn't been shown yet for a port
whose lease was already set. Setting a lease doesn't unlock a port whose key
is already lost: OpenSM can't reach that port to change its lease. The keys
the subnet manager keeps to itself, the SM_Key and the SA_Key, aren't stored
on ports, have no lease, and aren't affected.

### Turning M_Key protection off

Ports keep their M_Key until a subnet manager with it changes their
settings, or a lease runs out. So to turn M_Key protection off, first keep the
M_Key and lower the protection, `m_key_protection_level = 0` in
`[opensm_conf]`, and restart the manager; then drop `m_key` from
`hardening`. Dropping it at once leaves the ports protected by a key the
subnet manager no longer has.

### Diagnostics

Diagnostic tools that send SMPs from a host, such as `ibnetdiscover`,
`iblinkinfo`, `smpquery`, `ibportstate`, `ibhosts` and `sminfo`, need the
M_Key, with `-y <m_key>`. Tools that only read the local adapter, such as
`ibstat`, don't.

## SM_Key

Subnet managers authenticate each other with the SM_Key when one hands over
to another. With OpenSM's well-known default, any host running OpenSM could
negotiate a handover from the master. With a secret SM_Key, only the
fabric's own subnet managers can. A rogue subnet manager can still try to
win the election, which is why the M_Key matters most: without it, the rogue
can't configure the ports.

## The SA enhanced trust model

A client that sends the SA_Key is trusted, and one that sends `0` is not.
With the enhanced trust model, untrusted clients get only what an IPoIB or
RDMA application needs: path records between two given ports, multicast
membership, service records, event subscriptions and class port info.
Everything else, such as dumping every node or port record, needs the
SA_Key.

- **No proxy requests:** an untrusted client can't join a multicast group,
  or register a service, for another port.
- **No alias GUIDs:** an untrusted client, and a virtual function even with
  the SA_Key, can't add an alias GUID to its port. With an alias GUID, a port
  could take another port's identity, for example the GUID that a network's
  partition lists.
- **Registration limits:** a port can register at most 128 multicast
  groups, 32 services and 32 event subscriptions, so one port can't exhaust
  the SA for everyone.

Cost:

- **ConnectX-3:** its SR-IOV gives virtual functions alias GUIDs, which the
  hypervisor's driver adds with untrusted GUIDInfoRecord requests. On a
  fabric with ConnectX-3 virtual functions, set
  `sa_etm_allow_untrusted_guidinfo_rec = TRUE`. ConnectX-4 and later can
  give virtual functions virtual ports instead, when OpenSM's
  `virt_enabled` is on and the firmware supports it; check that before
  relying on it.
- **Diagnostics:** `saquery` dumps need `--sa_key` or fail.
- **Applications** that register more than the limits, which is unusual,
  need higher limits.

## Partition enforcement

`part_enforce both` makes switches check the partition of every packet in
both directions at the port, which is what makes a partition a boundary.
`allow_both_pkeys FALSE` keeps a port from being both a full and a limited
member of the same partition, which the manager's partitions never need.

## Babbling ports

With `babbling_port_policy TRUE`, OpenSM disables a port that keeps sending
it traps, so a misbehaving or hostile host can't flood the subnet manager.

## The default partition

The shipped `[opensm] default_partition` gives every port only limited
membership of the default partition, `ALL=limited, SELF=full`. A limited
member can only reach full members, so tenants can't reach each other there,
while every port can still reach the subnet manager. It is in the partitions
file, not `opensm.conf`, so `hardening` doesn't change it.

A limited member does reach every full member, so the default partition must
have no full members besides the subnet manager and switches. That is why:

- flat networks get a partition of their own, `[opensm] flat_pkey`, 0x7ffe
  by default, rather than full membership of the default partition, which
  every VLAN network's ports could reach. With `flat_pkey = 0x7fff`, the
  older way, they are full members of the default partition, reachable from
  every tenant;
- the checks refuse `ALL`, `ALL_CAS` and `ALL_VCAS` as full members of any
  partition but a network's, and of the default partition unless
  `[opensm] shared_default_partition` is true, for a fabric that relies on
  every port reaching every other there;
- a host's own IPoIB interface on the default partition needs to be a full
  member, by its GUID with `=full` in `default_partition`, which makes it
  reachable from every tenant. Put such hosts in a static partition of their
  own instead.

A guest's IPoIB interface uses the pkey at index 0 of its port, which the
manager gives each network's partition, and the flat partition (`indx0`).
With `ALL=full` in the default partition, a guest could still add an IPoIB
interface for it, `0x7fff`, and reach other tenants there; limited membership
is what prevents that.

## Experimental hardening

These options close the rest of the gaps between AFM's hardening and
NVIDIA UFM's Secured Cloud Profile. None is enabled by default or by `all`.
Each is designed in specs/secured-profile.md, where it is planned as a
group of its own, once hardware tests show it is safe. Until then you can
try one by setting its OpenSM options in `[opensm_conf]`. The manager
writes them as they are, and checks none of what the planned groups will.

Try them only on a fabric you control, or with the agreement of everyone
who shares it. Back up the database first (it holds the keys), and record
what you set, so that you can undo it. With `[opensm] key_lease_period`
set, keys that go wrong can be recovered without a power cycle, so prefer
a short lease, such as `60`, while you experiment (see Permanent keys,
including the exposure a short lease brings).

Each change takes effect when the manager restarts, which restarts OpenSM.

### Allowed subnet managers

A tenant can run a subnet manager of its own. Without the SM_Key it can't
take over by handover, and without the M_Key it can't set ports, but it may
still win the election and send AFM's OpenSM to STANDBY, leaving the fabric
without a working subnet manager. With `allowed_sm_guids`, OpenSM ignores
every subnet manager whose port isn't in the list.

```ini
[opensm_conf]
# The port GUIDs of every physical InfiniBand port that may run OpenSM: in
# HA mode, every node's. Not virtual functions, which belong to tenants.
allowed_sm_guids = 0x...,0x...
```

- **A wrong list can leave the fabric without a subnet manager.** If the
  port OpenSM binds to isn't listed, OpenSM may refuse to run. Read the
  GUIDs with `ibstat -p` on each host, and leave out virtual functions
  (devices with a `device/physfn` link in `/sys/class/infiniband`).
- **A malformed list turns the feature off.** OpenSM logs `ERR 7549` and
  disables the feature. Check OpenSM's log after the restart.
- **In HA mode, list every node's ports.** Otherwise one leader ignores the
  next, and drops its request to stop (`ERR 2F1C`).
- OpenSM logs `ERR 0F09` for each subnet manager it ignores. AFM doesn't
  act on it yet. The planned group raises an alarm, and treats an ignored
  subnet manager on a managed node's port as a contested fabric.
- **Untested:** whether a tenant's subnet manager can send AFM's OpenSM to
  STANDBY without the list, and whether a change to the list applies
  without a restart.

### Class keys

Switches and adapters accept other classes of management packets than
SMPs. Vendor-specific (VS) packets are what in-band `mlxlink`, `mlxreg`
and `flint` use. Congestion control (CC) packets set the congestion
parameters of every port. A tenant can send either to any switch unless
the class is keyed. OpenSM derives each port's class keys from
`key_mgr_seed`. The manager already keeps a seed in its database, so that
backups hold it, but doesn't write it to `opensm.conf`.

```ini
[opensm_conf]
vs_key_enable = 2
vs_key_ci_protect_bits = 1
cc_key_enable = 2
cc_key_protect_bit = 1
n2n_key_enable = 2
n2n_key_protect_bit = 1
# The stored seed (see below). Without it, OpenSM derives the class keys
# from the M_Key. The manager refuses any other value.
key_mgr_seed = <the stored seed>
```

Copy the stored seed into `afm.conf` without displaying it:

```sh
SEED=$(docker exec alternative-fabric-manager alternative-fabric-manager \
  --show-keys | awk '/^key_mgr_seed/{print $2}')
sudo sed -i "/^\[opensm_conf\]/a key_mgr_seed = $SEED" \
  /etc/alternative-fabric-manager/afm.conf
unset SEED
```

The leases follow `[opensm] key_lease_period` only once the planned group
exists. Until then, set `vs_key_lease_period`, `cc_key_lease_period` and
`n2n_key_lease_period` yourself, or OpenSM's default of 60 s applies.

- **A lost or changed seed can leave ports keyed for good.** Never change
  `key_mgr_seed` while ports are keyed, and keep the database backups.
- **Turn them off by disabling first.** Set each `<class>_key_enable = 1`
  and restart, so that OpenSM clears the keys from the ports. Only then
  remove the options. Removing them at once leaves the ports keyed with keys
  the subnet manager no longer manages.
- **In-band tools need the VS key,** and which of them can take it is still
  being worked out. `ibdiagnet` takes `--vs_key_file`, pointed at OpenSM's
  `/var/cache/opensm/guid2vskey`, which makes that cache a secret.
- **Not every device supports every class.** Ports that don't answer with
  `ERR 9828` at every sweep. On the test fabric, only VS keys were
  supported.
- **Tested so far:** on one host, VS keys derive from the seed alone. Their
  effect on tenants, turning them off, and a lease of 0 are untested (see
  specs/secured-profile.md, Results).

### SA request rate

`sa_rate_threshold` limits how many SA requests a port may send per 100 ms.
OpenSM drops the rest, and logs `ERR 4C11`.

```ini
[opensm_conf]
sa_rate_threshold = 1000
```

- **It may limit neighbours together.** Whether OpenSM counts per port GUID
  or per physical port is untested. If it counts per physical port, one
  guest's flood would drop the requests of every guest on that port.
- **Check it against your heaviest start-up.** An MPI job's start-up makes
  the most path record requests. If OpenSM logs `4C11` for one, raise the
  limit.

## Turning hardening on for a running fabric

OpenSM reads `opensm.conf` only when it starts, so the manager must be
restarted, which restarts OpenSM. OpenSM then sets the M_Key on every port at
its first sweep. Hosts that run diagnostic tools need the M_Key from then on.

## Options the manager keeps

Whatever `hardening` says, `[opensm_conf]` can't set `partition_config_file`,
`daemon`, `log_file` or `no_partition_enforcement`, `part_enforce` must stay
`both`, and `log_flags` must keep the levels `0x07`, whose messages the
manager watches for. OpenSM ignores an option it doesn't know, so check an
override's spelling in `opensm -c` output.
