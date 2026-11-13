# OpenSM hardening

OpenSM's defaults suit a fabric whose hosts all trust each other. A cloud's
fabric doesn't: a bare metal tenant has full control of its InfiniBand
adapter, and can send any subnet management packet (SMP) or subnet
administration (SA) request, or run a subnet manager of its own. A guest on
a virtual function has less control, but can still make SA requests.

So with the managed driver, the manager writes a hardened set of options into
`opensm.conf`, in groups. `[opensm] hardening` names the groups, separated by
commas, `all` by default, or `none`: one option chooses what to harden, for
example `hardening = m_key, enforcement` on a fabric where only M_Key
protection is wanted. `all` may be named beside other groups, and adds them
to its own; `none` stands alone. `all` means the groups below, and keeps
meaning them: a group added later stays out of it, so that an upgrade never
turns it on by itself, and is chosen by naming it beside `all`. A single
OpenSM option can still be overridden in `[opensm_conf]`, for example
`sa_etm_max_num_mcgs = 256`, and wins over the groups.

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
| Shared with hosts outside OpenStack that need untrusted SA dumps, such as `saquery` | `m_key, sm_key, enforcement` |
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
countdown. When the countdown runs out, the port drops its protection,
and a subnet manager with the right keys can then key it again. A subnet
manager that has the right key stops the countdown, and OpenSM's sweeps do
too. So a port keeps its protection for as long as an SM manages it, and
loses it only after the lease passes with no SM managing it.

Choose the lease with both sides in mind:

- **Exposure.** While no subnet manager runs for longer than the lease, a
  host that sends packets without the key gets a port's protection, and for
  the M_Key the key itself, back. The manager stops OpenSM whenever it
  restarts, and in HA mode during a takeover and whenever the database
  can't be reached. Make the lease longer than the longest such outage you
  expect: minutes, not seconds.
- **Recovery.** A port keyed with a lost key recovers only once the lease
  has passed. Don't count on that happening while a subnet manager runs.
  In the hardware test of class keys, ports keyed with another seed stayed
  unreachable for more than five minutes, despite a 60 s lease, while
  OpenSM ran and polled them with the wrong key. They answered again after
  OpenSM had been stopped for three minutes (see specs/secured-profile.md,
  Results). Why isn't established yet. So to recover, stop every subnet
  manager for longer than the lease, then start one with the right keys.

A short lease, such as `60`, is useful for testing hardening on a fabric
that nobody else depends on. Any mistake then recovers within minutes of
stopping the manager. Don't run a tenant fabric with a lease that short:
every restart of the manager opens the window.

The option covers every key lease that a hardening group writes. A lease
set for one key in `[opensm_conf]`, such as `m_key_lease_period = 0`, still
wins over it, and the manager logs a warning at start, since that key is
then not covered. The manager logs at start whether keys are permanent or
expire, and after how long.

Changing the option takes effect when the manager restarts, which restarts
OpenSM. OpenSM sets the new lease on a port when it next configures it with
the right key. So setting a lease doesn't unlock a port whose key is
already lost: OpenSM can't reach that port to change its lease. The keys
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
