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
experimental, untested on hardware: not part of `all`, and not on by
default. The experimental groups are `m_key_per_port`, `allowed_sm`,
`class_keys` and `sa_rate`, each chosen by naming it beside `all`, as
`hardening = all, allowed_sm` (see
Experimental hardening). Before naming one, read its "Before enabling"
checklist: what to back up, what to check when it's turned on, what shows it
works or failed, and how to turn it off. Once its hardware tests pass, each
is planned to join `all`.
The release that adds one will say so in its notes, and `-group` will
remove it. On a fabric that others share, prefer an explicit list of groups,
so that nothing switches on at an upgrade without their agreement.

A single OpenSM option can still be overridden in `[opensm_conf]`, for
example `sa_etm_max_num_mcgs = 256`, and wins over the groups.

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
| `m_key_per_port` (experimental, untested on hardware; not in `all`) | `m_key_per_port` | `FALSE` | `TRUE`, once turned on, with `m_key_lookup TRUE`, `m_key_protection_level 2` and `m_key` the base key | One port's M_Key opening every other port |
| `allowed_sm` (experimental, untested on hardware; not in `all`) | `allowed_sm_guids` | unset (every SM takes part) | This host's physical ports | A tenant's subnet manager winning the election |
| `class_keys` (experimental, untested on hardware; not in `all`) | `vs_key_enable`, `cc_key_enable`, `n2n_key_enable` | `0` (Ignore) | `2` (Enable), with protect bits `1`, leases `[opensm] key_lease_period` and `key_mgr_seed` the stored seed | A host reconfiguring switches with vendor-specific or congestion control MADs |
| `sa_rate` (experimental, untested on hardware; not in `all`) | `sa_rate_threshold` | `0` (no limit) | `1000` per 100 ms | One port flooding the SA |

The `enforcement` group is OpenSM's defaults already. It is written anyway,
so that `opensm.conf` shows the options and nothing weakens them by
accident.

## Choosing what to harden

| Fabric | `hardening` |
| --- | --- |
| Only OpenStack's hosts, guests and bare metal nodes (the default) | `all` |
| With ConnectX-3 SR-IOV | `all`, with `sa_etm_allow_untrusted_guidinfo_rec = TRUE` in `[opensm_conf]` (see the SA enhanced trust model) |
| Shared with hosts outside OpenStack that need untrusted SA dumps, such as `saquery` | `m_key, sm_key, enforcement`: an explicit list, so that no group switches on without the agreement of whoever shares the fabric. `babbling_ports` is left out because it disables ports that flood traps, which may be theirs |
| Shared with hosts whose operators can't use the M_Key yet | `enforcement`, and plan M_Key protection: without it, a bare metal tenant can reconfigure switch ports |
| Single node, with bare metal tenants, on a fabric you control or whose owner agrees, trying per-port M_Keys | `all, m_key_per_port`: experimental, untested on hardware, and it leaves keys on ports (see Per-port M_Keys) |
| Single node, on a fabric where no other host should run a subnet manager, trying allowed SMs | `all, allowed_sm`: experimental, untested on hardware (see Allowed subnet managers) |

## The keys

The manager generates `m_key`, `sm_key` and `sa_key` on first start, as
random 64-bit values, and keeps them in its database. Every OpenSM of the
fabric must use the same keys: in high availability, a standby that took
over with another M_Key couldn't configure the ports. Keeping them in the
database gives every manager the same keys, and keeps them across restarts.
Only the keys of the chosen groups are written. A key set in
`[opensm_conf]` wins over the generated one.

The manager also stores `key_mgr_seed`, if it's missing, whenever it starts
OpenSM (in HA mode, when a node takes over), whatever the groups. Only the
experimental `class_keys` group writes it. OpenSM derives the class keys (VS, CC and N2N) from it,
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

The manager stores a fifth key in the same way, `m_key_base`, the base
that per-port M_Keys derive each port's key from (see Per-port M_Keys). It
is stored, if it's missing, whatever the groups, so that backups hold it
before any port is keyed from it, and it is written to `opensm.conf`, as the
`m_key` line, only while per-port M_Keys are on. It opens every port of the
fabric once they are: `--show-keys` prints it as `m_key_base`, and it is
the most secret of the keys.

The keys are in:

- the manager's database, which the manager creates readable only by root;
- `opensm.conf`, which the manager writes readable only by root;
- OpenSM's log: at startup OpenSM prints every option it loads, the keys
  included ("Loading Cached Option:m_key = ..."), so the OpenSM container's
  log, and wherever it is shipped, holds them. With per-port M_Keys on,
  that `m_key` line is the base, and Bad M_Key traps print the key that was
  sent (see Per-port M_Keys);
- OpenSM's cache, `guid2mkey` in `/var/cache/opensm`, which holds each port's
  M_Key, and with class keys, `guid2vskey` and the like;
- `afm.conf`, if a key or the seed is set in `[opensm_conf]`. The
  `class_keys` group writes the stored seed itself, so it needn't be.

Keep them all from tenants and from log shipping. To print the keys:

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
  holds the key, and can read and set any port on the fabric for good.
  (With per-port M_Keys on, a lapsed port gives away only its own key,
  which opens only that port; see Per-port M_Keys.) So
  treat any outage with no subnet manager that lasts longer than the lease
  as a compromise of the M_Key. The manager stops OpenSM whenever it
  restarts, and in HA mode during a takeover, and while the database can't
  be reached. Make the lease longer than the longest such outage you
  expect: minutes, not seconds.

  This has happened on a test fabric. With a 60 s M_Key lease, a few
  unkeyed queries to a switch, then three minutes with no subnet manager,
  dropped the switch's protection. It answered unkeyed queries, and so
  would have given out the M_Key, for some minutes after the subnet manager
  started again (see specs/secured-profile.md, Results). So don't count on
  a running subnet manager to restore a lapsed port's protection promptly:
  the window can outlast the outage.
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

Take the key into a variable, never onto the screen:

```sh
KEY=$(docker exec alternative-fabric-manager alternative-fabric-manager \
  --show-keys | awk '$1 == "m_key" {print $2}')
smpquery -y $KEY nodeinfo <lid>
unset KEY
```

Match the name exactly, as above: `/^m_key/` matches `m_key_base` too.
With per-port M_Keys, each port has a key of its own, so one `-y` opens one
port. Take it from `--port-m-key <guid>` instead, and query that port only
(see Per-port M_Keys, Diagnostics with per-port M_Keys).

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
Each is designed in specs/secured-profile.md. Some are groups already,
experimental, untested on hardware: `m_key_per_port`, `allowed_sm`,
`class_keys` and `sa_rate`, each with the manager's checks, chosen by
naming it beside `all`, and planned to join `all` once their hardware
tests pass. HA mode allows all four, experimental there too: see In HA
mode below for what changes, and what to test.

Each group has a "Before enabling" checklist, drawn from the hardware tests
the spec plans for it. Its first enabling on a fabric is that test: follow
the checklist, and record what you saw.

Try them only on a fabric you control, or with the agreement of everyone
who shares it. Back up the database first (it holds the keys), and record
what you set, so that you can undo it. In HA mode, change every node's
configuration, restarting the leader last: configuration epochs keep a
node whose configuration differs from the leader's from leading, and the
epoch moves to the new configuration by itself once every live node has
it (specs/ha.md, Configuration epochs).
On a deployment whose configuration is managed, such as Kayobe's, add them
there, not by hand, or the next reconfigure removes them.

Each change takes effect when the manager restarts, which restarts OpenSM.

### Allowed subnet managers

**Experimental, untested on hardware.** A tenant can run a subnet manager
of its own. Without the SM_Key it can't take over by handover, and without
the M_Key it can't set ports, but it may still win the election and send
AFM's OpenSM to STANDBY, leaving the fabric without a working subnet
manager. With `allowed_sm_guids`, OpenSM ignores every subnet manager
whose port isn't in the list (`ERR 0F09`), and drops SMInfo Sets from them
(`ERR 2F1C`).

The `allowed_sm` group writes the list for you: this host's physical
InfiniBand ports, which the manager reads before every start of OpenSM, in a
short-lived container of OpenSM's image named like the prober's
(`opensm-prober`). It reads sysfs only, and leaves out virtual functions
(devices with a `device/physfn` link) and mlx5 sub-functions, which belong
to tenants, and ports whose link layer isn't InfiniBand. Choose it with
`hardening = all, allowed_sm`. In HA mode the list is every node's ports,
which each node's manager records in its node row (see In HA mode). The
manager refuses it:
- without the `m_key` or `sm_key` groups, or with `m_key_protection_level
  0`: an ignored subnet manager is harmless only without the keys;
- with a driver other than `managed`.

`[opensm_conf] allowed_sm_guids` replaces the generated list. It must be
`0` (no other subnet manager) or port GUIDs, each `0x` and up to 16 hex
digits, separated by commas, and the manager writes it in its own
spelling. An override puts the list in effect even without the group, with
the same rules: a list typed by hand, as earlier versions of this doc
suggested, is now checked. In HA mode it can't be `0`, and a start refuses
one that leaves out any node row's port, naming the port: check a list
typed by hand before upgrading an HA cluster.

Before every start, the manager refuses to start OpenSM, logs "Not
starting OpenSM: ...", and tries again at every `[opensm] check_interval`,
if:
- it can't read this host's ports, or finds no physical InfiniBand port;
- the list leaves out one of this host's physical ports, any of which
  OpenSM may bind to, or, with `[opensm_conf] guid` set, that port.

`ERR 7549` in OpenSM's log, a list OpenSM rejected, fails the start or the
apply: OpenSM would then ignore no subnet manager at all.

**Ignored subnet managers.** At every fabric check, the manager reads the
`ERR 0F09` lines OpenSM logged since the last one:
- **On a known SM port**, one whose host may hold the fabric's keys, the
  check fails and the fabric is contested, so that no job completes, until
  a direct query of the port (`sminfo -G` and `smpquery -G portinfo`, with
  the port's M_Key) shows its subnet manager NOT-ACTIVE or no subnet
  manager bound there (IsSM clear). The detail reads "OpenSM ignores
  subnet managers on 0x..., which may hold the fabric's keys (ERR 0F09)".
  This host's own physical ports are known SM ports, recorded at every
  start, and so are ports you record. With `m_key` or `sm_key` set in
  `[opensm_conf]`, every ignored subnet manager is judged so, since AFM
  can't know who holds those keys.
- **On any other port,** the manager logs an error once for each OpenSM it
  starts ("... which isn't a known SM port: it can't hold the fabric's
  keys, so OpenSM stays master, but find out whose it is"), and the check
  isn't affected.

The commands, beside the running manager:

```sh
AFM="docker exec alternative-fabric-manager alternative-fabric-manager"
$AFM --known-sm-ports            # each known SM port: "this host" or "recorded"
$AFM --record-sm-port <guid>     # a port whose host may hold the keys
$AFM --drop-sm-port <guid>       # one whose adapter no longer is in such a host
```

Record the ports of any host that has held the fabric's keys: one that ran
AFM before, or where `opensm.conf` or a backup was kept. Drop a port only
once its adapter is in a host that never had them, such as a reused card
in a tenant's host: otherwise its subnet manager contests the fabric for
good. `--drop-sm-port` refuses this host's own ports.

The group leaves nothing on ports: removing it and restarting the manager
turns it off.

- **Untested on hardware** (specs/secured-profile.md, hardware tests 1 and
  2): whether a tenant's subnet manager can send AFM's OpenSM to STANDBY
  without the list; whether OpenSM logs `0F09` once or at every sweep (the
  manager handles either); whether OpenSM refuses to run on a port that
  isn't in its list; whether `0` lets OpenSM itself run; and what a SIGHUP
  re-reads.

#### Before enabling allowed SMs

- **Back up** the database, `afm.conf` and `opensm.conf`, and note this
  host's physical port GUIDs (`ibstat -p`, leaving out virtual functions).
- **Agree it** with whoever shares the fabric, and find out whether any
  other host runs a subnet manager, such as a standby: once the list is on,
  OpenSM ignores it. If one holds the fabric's keys, record its ports with
  `--record-sm-port` first, and expect the check to fail while it runs.
- **When turning it on,** restart the manager with `hardening = all,
  allowed_sm`, and check that:
  - the manager logs "OpenSM ignores every SM outside allowed_sm_guids:"
    with exactly this host's physical ports, and `opensm.conf` has the same
    `allowed_sm_guids` line;
  - OpenSM's log has no `ERR 7549`, and OpenSM reaches SUBNET UP as master;
  - `--known-sm-ports` lists this host's ports as "this host";
  - the fabric check passes, and a port's job completes.
- **It works if,** on a fabric nobody else uses (hardware test 1), a
  subnet manager started on another host at a higher priority leaves AFM's
  OpenSM master: OpenSM logs `ERR 0F09` naming that host's port, the
  manager logs that it isn't a known SM port, and the fabric check still
  passes. With that port recorded, the check fails with the contest detail
  while it runs, and passes again once it has stopped.
- **It failed if** the manager logs "Not starting OpenSM: ...", OpenSM's
  log has `ERR 7549`, OpenSM enters STANDBY, or the fabric check reports
  another master; or if a subnet manager the list should ignore changes a
  port.
- **To turn it off,** remove the group (and any `[opensm_conf]
  allowed_sm_guids`) and restart the manager. If a start is refused, the
  fabric has no subnet manager until it's fixed: correct the list, or turn
  the group off.

### Class keys

**Experimental, untested on hardware.** Switches and adapters accept other
classes of management packets than SMPs. Vendor-specific (VS) packets are
what in-band `mlxlink`, `mlxreg` and `flint` use. Congestion control (CC)
packets set the congestion parameters of every port. A tenant can send
either to any switch unless the class is keyed. OpenSM derives each port's
class keys from `key_mgr_seed`, which the manager keeps in its database.

The `class_keys` group, chosen with `hardening = all, class_keys` in
single-node mode, writes:

```
vs_key_enable 2
vs_key_ci_protect_bits 1
vs_key_lease_period <[opensm] key_lease_period>
cc_key_enable 2
cc_key_protect_bit 1
cc_key_lease_period <[opensm] key_lease_period>
n2n_key_enable 2
n2n_key_protect_bit 1
n2n_key_lease_period <[opensm] key_lease_period>
key_mgr_seed <the stored seed>
```

- **In HA mode it's experimental too**: seed-only derivation has been
  shown on one host, not across a takeover by another. Every leader reads
  the same seed from the database.
- **Not on the start that stores the seed.** A start of the manager that
  finds no seed stored, as on a fresh install or after restoring a backup
  from before the seed, stores one and starts OpenSM without class keys,
  logging "The class_keys group is named, but this start stored the class
  key seed ... OpenSM starts without class keys". Back up the database,
  which then holds the seed, and restart the manager.
- **A lost or changed seed can leave ports keyed for good.** The manager
  refuses an `[opensm_conf] key_mgr_seed` other than the stored one. Keep
  the database backups: they hold the seed.
- **Leases** are `[opensm] key_lease_period`, 0 (permanent) by default.
  Whether 0 means "never expires" for these classes, as for the M_Key, is
  untested. A lease is what would let a port keyed by mistake be keyed
  again without a power cycle, but that recovery hasn't been shown (see
  Permanent keys).
- **Not every device supports every class.** For ports that don't, OpenSM
  logs `ERR 9828` (or the N2N manager's `ERR 9B28`, or the congestion
  control manager's `ERR C10A`) at every
  sweep. On the test fabric, only VS keys were supported. The manager
  doesn't fail on it, and logs it, for each OpenSM it starts, as the set of
  ports grows: "OpenSM's VS key manager reports N ports answering a KeyInfo
  with status 0xc (ERR 9828, attr_id 0x...)". The line names the attribute,
  not the class: those ports aren't protected by that class's key.
- **Key errors** (`9803`, `9822`, `9823`, `9B03`, `9B22`, `C124`, `C125`: "failed to
  update port ... keyinfo") are logged once per code for each OpenSM
  ("OpenSM's ... key manager failed to set class keys"). They don't fail
  the fabric check.

#### Turning class keys off

Ports keep their class keys until an SM that has them changes them. The
manager records which classes the last `opensm.conf` it wrote enabled, and
refuses a start that would skip step 1, with "Not starting OpenSM: ports
may hold class keys ...":

1. Keep the group, set each `<class>_key_enable = 1` (Disable) in
   `[opensm_conf]`, and restart. OpenSM clears the keys from the ports,
   reaching them with the keys it derives from the seed, which the group
   still writes.
2. Check that OpenSM has finished: no port answers a VS query without the
   key refused, and OpenSM's cache files `guid2vskey`, `guid2cckey` and
   `guid2_n2n_key` no longer name keys. How to tell it has finished on
   every port is what hardware test 3 is to establish; the manager can't.
3. Only then remove the group and the `<class>_key_enable` lines.

Class keys enabled by hand without a seed, as before 0.4.0, came from a
seed the manager doesn't have, so naming the group wouldn't help: there,
step 1 is setting each `<class>_key_enable = 1` without the group, which
the manager allows only while the last `opensm.conf` it wrote had no seed.

Removing the group at once, or setting a class's enable to 0 (Ignore),
leaves the ports keyed with keys the subnet manager no longer manages: the
manager refuses both while a class is recorded enabled. A downgrade to a
release without the group, or restoring a database from before the record,
has the same effect, and nothing refuses it: do step 1 before either.
- **In-band tools need the VS key,** and which of them can take it is still
  being worked out. `ibdiagnet` takes `--vs_key_file`, pointed at OpenSM's
  `/var/cache/opensm/guid2vskey`, which makes that cache a secret.
- **Not every device supports every class.** For ports that don't, OpenSM
  logs `ERR 9828` at every sweep. On the test fabric, only VS keys were
  supported.
- **Tested so far:** on one host, by hand before the group existed, VS
  keys derive from the seed alone. Their effect on tenants, turning them
  off, a takeover, and a lease of 0 are untested (see
  specs/secured-profile.md, Results).

#### Before enabling class keys

- **Back up** the database, which holds the seed, and OpenSM's cache volume
  (`opensm_cache`), and check that the backup holds the seed without
  showing it: `--show-keys | awk '$1 == "key_mgr_seed" {n++} END {print n +
  0}'` prints 1. Restoring a database without that seed loses every keyed
  port.
- **Agree it** with whoever shares the fabric: in-band tools (`mlxlink`,
  `mlxreg`, in-band `flint`, `ibdiagnet`, in-band firmware upgrades) need
  the VS key once it's on, and which can take it isn't settled. For the
  first run, prefer a fabric nobody else uses, and consider a lease, such
  as `key_lease_period = 600`, so that a mistake may recover on its own.
- **When turning it on,** restart the manager with `hardening = all,
  class_keys`, and check that:
  - the start didn't withhold the group ("... OpenSM starts without class
    keys"): if it did, back up and restart again;
  - `opensm.conf` has the lines above, and OpenSM reaches SUBNET UP;
  - OpenSM wrote a VS key for every port to `guid2vskey` (count the lines,
    never show them);
  - the manager's log names which ports rejected which attribute (`ERR
    9828`), and no key error;
  - the fabric check passes, and a port's job completes.
- **It works if** a VS query that sends no SMP, such as `vendstat -N` to a
  switch, fails from a host without the VS key and answers with it, and a
  restart of the manager, which restarts OpenSM, logs no key error.
- **It failed if** the manager logs a key error ("failed to set class
  keys"), OpenSM's KeyInfo Gets time out (`ERR 980B`), or VS queries answer
  without the key while `guid2vskey` lists the port.
- **To turn it off,** follow Turning class keys off. If the seed was lost,
  the keyed ports need a lease lapse, a power cycle, or in-band firmware
  tools given the old seed's keys.

### SA request rate

**Experimental, untested on hardware.** `sa_rate_threshold` limits how many
SA requests a port may send per 100 ms. OpenSM drops the rest, and logs
`ERR 4C11`. The `sa_rate` group, chosen with `hardening = all, sa_rate`,
sets it to 1000. `[opensm_conf] sa_rate_threshold` overrides the value,
and sets the limit even without the group.

The manager logs each GUID OpenSM limited, once for each OpenSM it starts:
"OpenSM dropped SA requests from GUID 0x... (port 0x...): N in 100 ms,
over sa_rate_threshold 1000 (ERR 4C11)". It never fails the fabric check
on it: a tenant flooding the SA would otherwise stop every job.

- **It may limit neighbours together.** Whether OpenSM counts per port GUID
  or per physical port is untested. If it counts per physical port, one
  guest's flood would drop the requests of every guest on that port.
- **Check it against your heaviest start-up.** An MPI job's start-up makes
  the most path record requests. If OpenSM logs `4C11` for one, raise the
  limit.
- **The manager's own queries** send the SA_Key; whether trusted requesters
  are limited is untested. If they are, the fabric check fails with
  "saquery failed".

#### Before enabling the SA request rate

- **Back up** `afm.conf` and `opensm.conf`. The group leaves nothing on
  ports.
- **Agree it** with whoever shares the fabric: their clients' SA requests
  are limited too.
- **When turning it on,** restart the manager with `hardening = all,
  sa_rate`, and check that `opensm.conf` has `sa_rate_threshold 1000`,
  OpenSM reaches SUBNET UP, and the fabric check passes.
- **It works if** (hardware test 4, on a fabric nobody else uses) a guest
  flooding the SA, such as with `saquery` in a loop, is named in a `4C11`
  line, and a guest on another virtual function of the same physical port
  is still answered; and an MPI job's start-up across the fabric logs no
  `4C11`.
- **It failed if** `4C11` names a GUID that isn't the flooder's, such as
  its neighbours' or this host's, the fabric check fails with "saquery
  failed", or a job's start-up is limited.
- **To turn it off,** remove the group (and any `sa_rate_threshold`) and
  restart the manager, or raise the threshold.

### Per-port M_Keys

With the `m_key` group, every port on the fabric holds the same M_Key.
Anyone who learns it from any one port can send SubnSet to every switch:
rewrite pkey tables, which breaks every tenant's isolation, move LIDs, or
take ports down. A bare metal tenant has root on a host with a port on the
fabric, and there are several ways for that port's key to leave it: the
host's own adapter may let root read it, a port whose lease lapses gives
it to anyone who asks (this has happened on a test fabric; see Permanent
keys), and an adapter with a non-volatile M_Key keeps it wherever the card
goes. So the shared M_Key is only as secret as the least protected port.

With per-port M_Keys, OpenSM gives each port its own key, derived from a
base key and the port's GUID, so a key read from one port opens only that
port, which its tenant controls anyway. Two limits remain. A tenant who
knows their own port's GUID and key can search for the base offline, at
about 2^63 SHA-1s. And a tenant who can make their adapter present another
port's GUID is given that port's key, which only OpenSM's handling of
duplicate GUIDs stands against.

The base isn't the `m_key` that every port holds now. That is exactly the
key tenants may have read, and since a GUID isn't secret, deriving from it
would hand such a tenant every port's key. So the manager keeps a separate
key, `m_key_base` (see The keys), and OpenSM's `m_key` line is the base
only while per-port M_Keys are on.

Per-port M_Keys are experimental, untested on hardware as a group, and
not in `all`. Read Before enabling per-port M_Keys first. Choose them with
`hardening = all, m_key_per_port`, or with `m_key_per_port` in an explicit
list. In HA mode, see In HA mode. The manager refuses the group:
- with a driver other than `managed`: the manager must write OpenSM's
  cache and run its prober in OpenSM's image;
- without the `m_key` group;
- without a writable volume at `/var/cache/opensm` in `[opensm] volumes`,
  which the default `opensm_cache:/var/cache/opensm` is. A mode with `ro`
  among its options, such as `ro,z`, makes it read-only;
- with `m_key`, `m_key_per_port`, `m_key_protection_level` or
  `m_key_lookup` in `[opensm_conf]`, which the group sets. Without the
  group, `[opensm_conf]` can't set `m_key_per_port` either: by hand it
  would re-key every port with no record of which key each holds.

While any port holds its per-port key, a start also refuses `m_key_lookup`
other than `TRUE` in `[opensm_conf]`, and a `hardening` without the `m_key`
group: turning per-port M_Keys off depends on both.

Per-port M_Keys leave state on ports: their keys change, and the stored
`m_key` alone no longer opens them. **Naming the group is the decision to
turn them on:** once the fabric is ready, the manager arms the turn-on
and restarts OpenSM to apply it by itself, which re-keys every port (see
Turning them on). On a fabric that others share, agree it with them
before naming the group, and before removing it again, which re-keys
every port back.

In the commands below, `docker exec alternative-fabric-manager
alternative-fabric-manager` runs the command beside the running manager,
as for `--show-keys`; in HA mode, on any node, except that
`--confirm-fabric` arms the turn-on only on the leader's node, where
OpenSM's cache is. GUIDs are hexadecimal, with or without `0x`.

#### The expected switches, and `--confirm-fabric`

The manager keeps the switches the fabric check must find by itself
(specs/zero-touch.md). Its fabric check counts how many checks in a row
have seen each switch:
- on a new deployment, once every switch it finds has been seen by
  `[opensm] confirm_checks` checks in a row (12 by default, a minute at
  the default `check_interval`), it records them all, logging "Recorded
  the expected switches, seen by N checks in a row: 0x...". Until then
  the check fails with "No expected switches yet: recording them once the
  fabric has been stable for N checks", and jobs wait;
- a new switch, seen by that many checks in a row, is added: "Switch 0x...
  , seen by N checks in a row, is now expected";
- with ports recorded, the switches are known by the prober's port 0
  GUIDs, where `ibswitches` gave node GUIDs. When the source changes, as
  when the group is first named, each switch is translated by its node
  GUID, which the manager keeps beside it; one it can't match stays
  expected, and fails the check as missing;
- a missing switch fails the check. With ports recorded, it's dropped only
  once the prober shows every recorded link to it down, from the switch
  ports and this host's port at its other ends: "Expected switch 0x... is
  missing, and the prober shows every recorded link to it down: it's no
  longer expected". A leaf switch's adapters are reached only through it,
  so it is never dropped this way: a removed or replaced leaf, or any
  switch without ports recorded, needs `--confirm-fabric`, and, with
  ports recorded, `--release-port` for the dead leaf's ports, or their
  jobs wait. A switch whose links are up but that doesn't answer stays,
  since it may still forward.

Nothing is added, dropped or recorded while the check couldn't query the
fabric, saw another master, or OpenSM wasn't master.

The prober follows every port that answers as a switch, as OpenSM's own
sweeps do, but the check counts only the expected switches it finds, so a
new switch, or a tenant's adapter answering as one, never makes a
fragment of the fabric look complete. Such an adapter is sent only the
keys OpenSM itself would send it: with per-port M_Keys on, a switch is
sent the stored `m_key` only while its record says it holds it.

`--confirm-fabric` is optional now: a shortcut that records the switches
the fabric check sees at once, replacing the list, and the way to drop a
removed switch the manager can't. Every run, it:
- while the group is named or ports are recorded, folds the links and
  ports that the running OpenSM's cache lists into the records first;
- records the switches, with ports recorded found by the prober, and
  prints them, one per line;
- with the group named and the target off, clears a recorded failed
  turn-on (see Turning them on) and arms the turn-on at once if:
  - it could read the running OpenSM's cache;
  - the base was stored by an earlier start than the running manager's
    ("the running manager's start stored the per-port M_Key base: restart
    the manager after backing up the database, which then holds it");
  - the stored base is the one any ports were keyed from before ("its
    fingerprint differs"; see Backups and restores);
  - links are recorded ("no links are recorded: OpenSM's cache hasn't
    listed the fabric yet ...");
  - the prober, run beside the running OpenSM, finds every recorded port
    behind a link that's up answering the stored `m_key`, with protection
    level 2, so none is `unknown` ("every recorded port behind an up link
    must answer the stored M_Key, with protection level 2, and these
    don't: 0x..."). A port behind a link that's down doesn't stop it: it
    keeps its `m_key` record, and OpenSM re-keys it through that record
    when it comes back.

If any fails, the switches stay recorded and the turn-on isn't armed: it
prints "the switches are confirmed, but per-port M_Keys aren't armed:",
then each failure, and exits non-zero. If it can't ask the fabric at all
it records nothing: "can't confirm the fabric: ...". When it arms, it
prints "Per-port M_Keys are armed: the running manager restarts OpenSM to
turn them on at its next passing check with no open job, or its next
restart does"; when they're on already, "Per-port M_Keys are already on,
or their turn-on is recorded: nothing more to do". On a deployment that
names the group, then, running it with the target off turns per-port
M_Keys on as soon as the fabric is ready, as the manager would.

#### Turning them on

OpenSM must know every port's GUID before the start that turns per-port
M_Keys on, or it sends those ports a key of 0 and loses them. So the
manager turns them on only once its records show every port ready:

1. **Agree it** with whoever shares the fabric, and **back up the
   database** before naming the group (see Before enabling per-port
   M_Keys). The base is stored a release before any port is keyed with
   it, so routine backups hold it; a lost database with no backup since
   strands every port with a lease of 0 (see Backups and restores), which
   a non-zero `key_lease_period` makes recoverable by waiting.
2. **Name the group and restart the manager.** OpenSM runs as before,
   with the shared `m_key`. A start that stored `m_key_base` can't arm the
   turn-on (the first start of a release with this group stores it,
   whatever the groups): restart once more, after a backup. The manager
   records the fabric's ports and links from OpenSM's cache at every
   fabric check, and from then on the fabric check, job completion and
   the sweeps follow the rules in Running with them, whichever key the
   ports hold.
3. **The manager arms it.** At each fabric check that passes, with the
   target off and no failed turn-on recorded, it checks the turn-on's
   conditions (those `--confirm-fabric` checks, above) on the check's last
   full verify, which must be complete and taken since OpenSM last
   started. While one fails, it logs "Per-port M_Keys aren't armed: ...",
   once per change of reason, naming the ports that block it, and
   `--port-key-states` shows the reason. Once they pass, it records the
   target on, logging "Per-port M_Keys are armed".
4. **The manager applies it.** At a fabric check that passes with no open
   job, while the target differs from what OpenSM runs with, it restarts
   OpenSM, "Restarting OpenSM: the per-port M_Key target is on, and
   OpenSM runs with them off: a start applies it", at most once an hour.
   On a busy cloud with no quiet check, once a day has passed since the
   target changed, it applies it whatever the jobs, and logs "A day has
   passed since the per-port M_Key target changed, so it is applied
   although N jobs are open". That start verifies every recorded port,
   writes OpenSM's `neighbors` and a `guid2mkey` entry with the stored
   `m_key` for every port that holds it into OpenSM's cache volume,
   records a fingerprint of the base (a hash, not the key), and starts
   OpenSM with `m_key_per_port` and the base. OpenSM then re-keys every
   port it reaches. The log shows "Starting OpenSM with per-port M_Keys on
   (target on); N ports are changing". A restart you choose, or a crash
   restart, applies the target the same way.

To check on it, `--port-key-states`:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --port-key-states
```

Its first line is the target and what OpenSM last started with, such as
`target off, OpenSM last started off`; then `failed turn-on recorded: yes`
or `no`, and `arming blocked by: ...`, or `-` when nothing blocks it. Each
line after them is a recorded port: its GUID, `switch`, `hca` or `-` (not
yet known), the key it holds (`m_key`, `derived` or `unknown`), and its
flags (`lapsed`, `reachable`, `released`, or `-`). It never shows a key,
nor a failed turn-on's hash.

The start succeeds when OpenSM's cache, within `[opensm] start_timeout`,
shows this host's port and every recorded switch that answered the verify
holding its derived key. The log then shows "Per-port M_Keys are on:
OpenSM gave this host's port and N switches their derived keys". While
other ports are still changing, each fabric check logs "Per-port M_Keys
turning on: N reachable ports still hold the stored M_Key", until "Per-port
M_Keys are on on every reachable port". `--port-key-states` then shows
`target on, OpenSM last started on`, and `derived` for every reachable
port.

If the start fails, whether OpenSM's cache doesn't show the keys in time
or OpenSM doesn't start at all ("OpenSM didn't start: ..."), the manager
logs "The start that turns per-port M_Keys on failed: ...", stops OpenSM,
and runs the prober to find out whether any port left the key it held:
- **No port moved, and none answers its derived key:** the target is set
  back to off, OpenSM is started without per-port M_Keys, and the failure
  is recorded, with the base's fingerprint and a hash of the
  configuration (an HMAC keyed by a secret in the database, never shown):
  "... no port left the key it held, so the target is set back to off and
  OpenSM is started without them. The failure is recorded: nothing arms
  them again until the configuration changes, or --confirm-fabric is run
  once the cause is fixed". So a fabric that can't be turned on isn't
  re-keyed in a loop, across restarts and takeovers. Every start logs the
  record, with what to check. Fix the cause the message names; then a
  change of configuration lets the manager arm it again, or
  `--confirm-fabric` clears the record and arms it.
- **Some ports moved, or some port answers its derived key** (as one
  keyed by an earlier, interrupted turn-on would): the fabric is
  part-way, so the manager starts OpenSM on the target, from the verified
  records, and leaves it running: "... after OpenSM moved N ports (...) to
  their derived keys: the fabric is part-way, so OpenSM is started on the
  target, from the verified rows, and left running, and the turn-on is
  retried at most once an hour until it succeeds". Each retry is a restart
  held to the success rule, under the same hourly cap as applying. Jobs
  meanwhile complete only on the per-port evidence they need anyway. Fix
  the cause if you can. If you can't, removing the group turns per-port
  M_Keys off (see Turning them off), which gives the moved ports the
  stored `m_key` back.
- **The prober can't tell:** "... the prober can't tell which keys the
  ports hold" or "can't tell whether any port moved": the manager doesn't
  start OpenSM on either target until it can, and retries at every check.
  No subnet manager runs meanwhile: the ports keep their keys, but nothing
  new is configured and no job completes. Fix what stops the prober, which
  the message names. Don't start OpenSM by hand: a start on the wrong
  target loses the ports.

If the verify before the first turn-on start fails or runs out of time,
OpenSM starts as it last ran, without per-port M_Keys: "The verify before
this start failed, so OpenSM starts with per-port M_Keys off, as it last
ran, rather than beginning to turn them on; the next start tries again".
The target stays on, so the manager applies it again, at most once an
hour. But once OpenSM has started with them, before a start has met the
success rule (for example after the manager restarts during a turn-on),
the fabric may be part-way, and neither target is safe: a verify that
fails then stops the start, "The verify before this start failed while
per-port M_Keys are turning on, and the fabric may be part-way: OpenSM
isn't started on either target until a verify succeeds", and it's
retried. No subnet manager runs meanwhile, as above.

Before every start, the manager reads OpenSM's cache from its volume. If
the files can't be read or parsed, it writes nothing, since it would
replace them unseen, and the start fails and is retried: "Can't read
OpenSM's cache before starting it (...), so it isn't replaced unseen: the
start is retried". Files that are absent are taken as a lost volume, and
rebuilt from the records. So if OpenSM left a file truncated when it
stopped, move that file out of the cache volume, with OpenSM stopped, and
the next start rebuilds it.

While the group is named or any port is recorded, a start of OpenSM that
fails doesn't end the manager: it logs "Failed to start OpenSM, retrying
at every check: ..." and the applier starts OpenSM again at every
`[opensm] check_interval`, as it does when OpenSM stops. Read the reason
in that line rather than waiting for the manager to exit. A start refused
by the configuration (such as the `m_key` group removed while ports hold
per-port keys) still ends it.

#### Running with them

**The records.** Two tables hold what the manager knows about the fabric,
never a key. `fabric_ports` has, for each port GUID that OpenSM's
`neighbors` lists, its node GUID, whether it's a switch (as its NodeInfo
said), the key it holds (`m_key`, its derived key, or `unknown`), whether
it lapsed (its protection was 0 at the last Get, so it answers any key),
whether it's reachable, whether an operator released it, and when it was
last seen. `fabric_links` has each link, both directions, as `neighbors`
does. At every fabric check the manager reads `neighbors` and `guid2mkey`
from the running OpenSM's cache into them. A `neighbors` line replaces the
link recorded for its port, so a re-cabled port's new link replaces its
old one, and the old switch port's link back to it is dropped. A link
recorded in one direction only is neither followed nor written to
OpenSM's cache: otherwise the port would be reached through a switch port
it has left, and whatever is cabled there next taken for it. `--add-link`
records both directions. Virtual functions never get a
record, and a recorded port that disappears from OpenSM's files keeps its
record, since a port that's down may come back holding its key. Before
every start, the manager writes OpenSM's cache from the records.

**The prober.** The manager can no longer run `ibswitches -y <m_key>`,
which uses one key for the whole fabric. Instead it Gets NodeInfo and
PortInfo with `smpquery` by directed route, from this host's port along the
recorded links, in OpenSM's container, and only named fields of the
answers leave it. A fabric check reaches every switch, and counts the
expected ones it finds. A full
verify, of every recorded port behind a link that's up and every switch
port's link state, runs every `[opensm] port_verify_interval` seconds (60
by default; it must be positive), and the fabric check uses its last
result. Once any port can hold a derived key, the prober sends the stored
`m_key` to a tenant's port only if its record says it holds it, and with
per-port M_Keys on, never to one recorded `derived` or `unknown`, so that
the old key isn't handed to tenants again. It tries the key a port's
record names first, so that few Gets carry a wrong key: each such Get
starts the port's lease countdown, if it has a lease, which the right
key's Get stops.

Before a start, when no OpenSM runs, the prober runs in a helper container
from OpenSM's image, named after `[opensm] container` with `-prober`
(`opensm-prober` by default), privileged and on the host network. Its
configuration holds the Gets and their keys while it runs, so `docker
inspect` of it shows keys: never inspect it or paste its configuration. It
is removed after each run, and a start or stop of OpenSM removes one left
behind.

**Diagnostics with per-port M_Keys.** Each port answers only its own key,
so a tool that walks the fabric with one `-y`, such as `ibnetdiscover`,
`iblinkinfo` or `ibswitches`, reaches only the ports that hold that key.
Use `--port-key-states` and the fabric check's detail to see the fabric,
and query one port at a time, with its own key, taken into a variable and
never displayed:

```sh
KEY=$(docker exec alternative-fabric-manager alternative-fabric-manager \
  --port-m-key <guid>) && smpquery -y $KEY nodeinfo <lid>
unset KEY
```

`--port-m-key` prints the key the port holds now, by its record: the
stored `m_key`, or its derived key. It refuses a GUID with no record, or
one whose key is `unknown`, and prints the stored `m_key` while nothing is
recorded. For a switch, give the switch's GUID, as `--port-key-states`
lists it. The key it prints opens that port, and a switch's opens that
switch: keep it out of shells' history, logs and screens.

**Jobs.** A job that binds or unbinds a recorded port GUID needs one more
piece of evidence after its sweep: at a fabric check after that sweep, the
prober's Get to the port answers with its key, or the switch port it's
attached to shows its link down (OpenSM then sets its pkey table when the
link comes up). A lapsed port, which answers any key, doesn't show that.
Until then the job waits, with the error "Waiting for port 0x... to answer
its M_Key, or show its link down, after the sweep: OpenSM may not reach
it. --release-port completes its jobs as they are", and the log shows
"Port 0x... hasn't answered its M_Key, nor shown its link down, since the
sweep, so its jobs are held". Only that port's jobs wait: its own tenant
can cause this, and it holds no one else's. If the port won't be reached
again, for example on a node being retired, release it:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --release-port <guid>
```

That completes its jobs as they are. It changes nothing on the port, nor
on its switch port, and lasts until the port next answers its key. A job
on a GUID with no record, a virtual function's or a port not yet seen,
completes as it did before.

**Sweeps.** OpenSM's own log is an extra reason to hold jobs, since a lost
port doesn't stop OpenSM logging SUBNET UP:
- `ERR 5411` "DR SMP Send completed with error" names no port, so it holds
  every job of the sweep ("OpenSM failed to send a directed-route SMP
  during the sweep, which names no port, so no job of the sweep
  completes").
- `ERR 3113` for a NodeInfo or PortInfo Get names a `dest_guid`. If that is
  a Neutron port's GUID, that port's jobs wait ("OpenSM couldn't reach
  Neutron ports ... during the sweep; their jobs wait"); otherwise every
  job of the sweep does ("... which aren't Neutron ports', so no job of the
  sweep completes").
- Bad M_Key traps are only counted ("OpenSM received N Bad M_Key traps
  during the sweep"): a trap's sender writes the data that names it, so
  traps hold no job.

**The fabric check's failures.** Besides its usual checks, the fabric
check fails, with these in its detail (after "Fabric check failed:" in the
log), and no job completes meanwhile:

| Detail | What it means, and what to do |
| --- | --- |
| "switch port 0x... port N has its link up with no recorded neighbour (record what's attached with --add-link)" | Something is attached that OpenSM hasn't recorded, which may be a port it can't reach. Find out what it is, then record the link: `--add-link <switch GUID>:<port> <peer GUID>:<port>` |
| "ports that aren't Neutron ports' didn't answer their M_Keys: 0x... (no job completes until they're reached, or released with --release-port)" | A recorded port behind a link that's up, not a switch's nor a Neutron port's (a hypervisor's, or any other host's), didn't answer its key, or has lapsed, in the last full verify. Its virtual functions have no records, so their jobs would otherwise complete without evidence. Find out why it isn't reached, and once it is, the check passes again. For a port that won't be reached, `--release-port <guid>` |
| "the last full verify of the ports' M_Keys is older than two port_verify_intervals" | Full verifies are failing or too slow: the log says why ("Can't verify every port's M_Key: ..."). A full verify must fit within one `port_verify_interval`: raise it on a large fabric |
| "Missing switches: ..." | An expected switch wasn't reached. A switch the prober shows gone, every recorded link to it down, is dropped by itself; a removed or replaced leaf switch isn't, so run `--confirm-fabric` once the hardware change is done (on a deployment that names `m_key_per_port` with the target off, that also arms the turn-on; see The expected switches, and `--confirm-fabric`). A switch that's there but doesn't answer may be hung: it holds jobs, since OpenSM can't program it |
| "No expected switches yet: recording them once the fabric has been stable for N checks" | A new deployment: the manager records the switches after `[opensm] confirm_checks` checks in a row that saw them all. `--confirm-fabric` records them at once |
| "no full verify of the ports' M_Keys yet" | At a start, until the first full verify; if it stays, as above |
| "Can't query the fabric: this host's port answers neither the stored M_Key nor its derived key" | OpenSM can't sweep from this host. Restore the database that holds its key, or wait for its lease to lapse, if it has one |

**A tenant can hold every job of the fabric.** The rule on lost ports that
aren't Neutron ports' is fabric-wide, since AFM can't tell which physical
port a virtual function is on. So a bare metal tenant who leaves their node
refusing its key and then releases the node, which makes its port's GUID
no longer a Neutron port's, makes the fabric check fail: no job completes,
anywhere on the
fabric, until that port answers its key again or an operator runs
`--release-port <guid>` for it. The check's detail names the port. This
fails closed, by design: recognise it, check the port, and release it.

**OpenSM's log holds keys.** Bad M_Key traps print the key that was sent,
and at startup OpenSM prints the base as its `m_key` option. So OpenSM's
log, the OpenSM container's output and the `opensm_logs` volume, is as
secret as `opensm.conf`: keep it from tenants and from log shipping. The
manager never logs a key, nor the prober's command lines: it masks keys in
the lines of OpenSM's that it shows, and drops lines about Bad M_Key traps.
The prober's keys are on `smpquery`'s command line, visible in `/proc` on
this host, as `ibswitches -y` is without per-port M_Keys: this host must
have no tenants.

**Leases.** Per-port M_Keys use the same lease, `[opensm]
key_lease_period` (see Permanent keys). With a lease of 0, a port that
loses its key stays out of reach until it's reset. With a lease, a lapsed
port gives away only its own key, but a port still on the stored `m_key`,
during a turn-on or after a turn-off, gives away the shared key, as
before.

#### Backups and restores

The database holds the base, and which key each port holds. Keep backups
taken after the turn-on: those are the ones that can reach the ports.

A start that finds the database and OpenSM's cache volume don't belong
together keeps per-port M_Keys off, leaves the volume's files as they are,
and starts OpenSM without them, so that OpenSM reaches each port through
its own cache and gives it the stored `m_key`. It logs why: "The stored
per-port M_Key base isn't the one the ports were keyed from (its
fingerprint differs)", or "OpenSM's cache has M_Keys for N ports (...)
that are neither the stored M_Key nor derived from the stored base",
followed by "Keeping per-port M_Keys off, leaving OpenSM's cache as it is,
and starting OpenSM without them". That happens, for example, with a
database restored from before the base was stored. Every start keeps
taking that path while the fingerprint differs, so once
`--port-key-states` shows every port holding `m_key`, remove the group,
restart, run `--forget-port-keys`, which also drops a fingerprint of
another base, and begin again if you want per-port M_Keys.

A database restored from after the base was stored but before the
turn-on, the usual case, has the target off while the ports hold keys
derived from its base, and no fingerprint, since that is recorded by the
turn-on start. With the group named, the start adopts the target on rather
than giving the ports the exposed `m_key` back, and logs "Ports hold keys
derived from the stored base while the per-port M_Key target is off and no
turn-on is recorded, as a database restored from before the turn-on would
have: adopting the target on". An adopted target isn't held to the
turn-on's success rule. Without the group named, it's an ordinary
turn-off.

With a fingerprint recorded, the database has seen a turn-on, so ports
recorded as holding derived keys while the target is off are what a
turn-off or a set-back left, such as a node that was down. If the group
is named again, they're turned off like any others: the start logs
"Per-port M_Keys are off, and ports recorded as holding derived keys are
given the stored M_Key as OpenSM reaches them; --confirm-fabric arms
per-port keys again". Once `--port-key-states` shows them holding `m_key`,
the manager arms the turn-on again, after checking the fabric, as does
`--confirm-fabric`.

Ports are lost only if the database is older than the base and OpenSM's
cache volume is gone too: then nothing holds their keys. Recovering them
needs a lease lapse (see Permanent keys), or in-band firmware tools (MFT)
given each port's key, computed from a newer backup's base.

#### Turning them off

1. **Agree it** with whoever shares the fabric. Turning per-port M_Keys
   off gives every port the stored `m_key` again, the old key, not the
   base: a tenant who read it has it again, which turning off can't
   avoid.
2. **Remove the group, keeping `m_key`, and restart the manager.** The log
   shows "The m_key_per_port group isn't named, so this start turns
   per-port M_Keys off: OpenSM gives every port it reaches the stored
   M_Key". The records stay: the start writes `neighbors` and an entry
   with its derived key for every port that holds one, and OpenSM, without
   the option, re-keys each port to the stored `m_key`.
3. **Wait for every port.** Each fabric check logs "Per-port M_Keys turning
   off: N reachable ports still hold their derived keys". Wait until
   `--port-key-states` shows every port, HCAs included, holding `m_key`. A
   port that's down keeps its derived key and its record until it comes
   back.
4. **Drop the records** with `--forget-port-keys`. It refuses while the
   group is named or any port holds its derived key, but not for a port
   whose key is `unknown`: check step 3 first. The base, and its
   fingerprint, stay.

Keep the managed driver until then: with another driver the records
remain but nothing keeps them ("Per-port M_Key rows exist, but the ...
driver can't keep them"). For hardware that has been removed, `--forget-port
<guid>` drops one port and its links; it refuses a port seen in the last
day.

**Downgrades.** A release without the group can't write OpenSM's cache
from the records, so ports holding derived keys would be lost. Before
downgrading, turn per-port M_Keys off and wait for `--port-key-states` to
show every port holding `m_key`, HCAs included. Restoring an older
database is a downgrade too (see Backups and restores).

#### Still to be shown

The group joins `all` only after a run of the implemented group on one
host, with a lease of 0 and the fabric owner's agreement, including its
turn-off. Still to run:
- a turn-on with a base that differs from the `m_key` the ports hold, and
  a turn-on whose start fails and is set back;
- a lease of 0, and a port reset seen from a second host;
- a takeover on a second host (see In HA mode), and a re-cabled port.

Not yet decided: a node that's down at the turn-on keeps its `m_key`
record, and is sent the stored `m_key` when it comes back, perhaps with a
new tenant, and an adapter that has reset its key. That matters only if a
host can read the key of an SMP sent to it. That is settled before the
group joins `all`.

#### Before enabling per-port M_Keys

Hardware test 5 ran the steps by hand, before the group existed; the
implemented group hasn't been run on hardware yet (see Still to be shown).

- **Back up** the database, which will hold `m_key_base`, and OpenSM's
  whole cache volume (`opensm_cache`, `/var/cache/opensm`), and keep both
  with `afm.conf`. Check that the backup holds the base without showing
  it: `--show-keys | awk '$1 == "m_key_base" {n++} END {print n + 0}'`
  prints 1.
- **Agree it** with whoever shares the fabric. For the first run, prefer a
  fabric nobody else uses, and consider a lease, such as `key_lease_period
  = 600`, which hardware test 5 used, so that a mistake recovers on its own:
  with a lease of 0, a port whose key is lost stays out of reach until it's
  reset (see Permanent keys). Then run again with the lease you mean to
  keep.
- **Before the turn-on,** check that `--port-key-states` lists every
  physical port you expect, each `m_key`, `reachable`, and none `lapsed`.
  The manager arms and applies the turn-on by itself once they are, so
  name the group only once you're ready for it.
- **When turning it on,** follow Turning them on, and check that:
  - the start logs "Per-port M_Keys are on: OpenSM gave this host's port
    and N switches their derived keys", then "Per-port M_Keys are on on
    every reachable port";
  - `--port-key-states` shows `target on, OpenSM last started on`, and
    `derived` for every reachable port;
  - the fabric check passes, and a port's job completes.
- **It works if** a port answers a Get with the key `--port-m-key <guid>`
  gives, taken into a variable, and refuses the stored `m_key`, the
  switches included (test 5's gate); and a port reset on a second host
  comes back keyed with its derived key.
- **It failed if** the log shows "The start that turns per-port M_Keys on
  failed", the fabric check fails with a detail from the table in Running
  with them, the sweeps report Bad M_Key traps that keep rising, or a check
  says "this host's port answers neither the stored M_Key nor its derived
  key".
- **To turn it off,** follow Turning them off, and wait for every port to
  hold `m_key` before `--forget-port-keys`. If a start fails part-way, the
  manager leaves OpenSM on the target and retries hourly (see Turning them
  on). If the database and
  the cache volume don't belong together, see Backups and restores.


### In HA mode

**Experimental, untested on hardware in HA mode.** The four groups run in
HA mode as in single node, with these differences (specs/secured-profile.md,
HA mode):

- **Configuration epochs.** Every node's configuration has a hash, which
  `--config-epoch` lists with the epoch. Only a node whose hash the epoch
  allows may lead. The epoch moves by itself once every live node has the
  same hash, so change every node and restart the leader last.
  `--allow-config [HASH]` moves it by hand. A node naming an experimental
  group isn't a candidate while a node of a release without configuration
  epochs is live: upgrade every node first, then name the groups.
- **Node ports.** Each node's manager records its physical InfiniBand
  ports in its node row, in a short-lived container named like OpenSM's
  with `-reach` (`opensm-reach`). They're known SM ports from then on.
  `--forget-node NODE` removes a retired node's row, so that its ports
  leave the `allowed_sm_guids` list at the next takeover; it's refused
  while the node may run OpenSM or its manager is live. The fabric check
  fails on any other node's SM, by these ports.
- **`allowed_sm`.** The list is every node row's ports. A node without
  them isn't a candidate. A new node's ports join the running list at the
  next takeover.
- **`m_key_per_port`.** The leader keeps the records, and the expected
  switches, behind the leader fence. A takeover renders OpenSM's cache
  from them, never from its host's cache volume, which an older term
  left. Each node's reach probe runs the prober, in its `-reach`
  container, rather than `ibswitches -y` with one key, and counts only
  the expected switches it finds. The leader arms the turn-on only while
  the configuration epoch allows exactly its own configuration's hash, so
  that leaders with and without the group can't alternate, re-keying the
  fabric each time: roll the configuration to every node first. To apply
  the target, at most once an hour, the leader steps down and releases
  the lease, "Stepping down: the per-port M_Key target is on, ...", with
  no other candidate needed; any eligible node, itself included, takes
  over, and the takeover's start applies it. A failed turn-on's record is
  in the database, so it holds across takeovers. The
  takeover's bound grows by `4 × start_timeout`, and `job_timeout` must
  exceed a takeover with a per-port start, 1.5 × `start_timeout` longer:
  with docs/kayobe.md's settings, 327 s rather than 237 s, which its
  `job_timeout = 360` covers, below Nova's `vif_plugging_timeout` of 420 s.
  `job_timeout` is a cluster setting: change it on every node, as for an
  epoch, and it moves with the epoch once every live node has it.
  A job on a recorded port completes only from the leader's checks.
- **`class_keys`, `sa_rate`.** As in single node; every leader reads the
  same seed.

#### Before enabling in HA mode

On a fabric you control, with backups of the database and a non-zero
`key_lease_period`, first run each group's own checklist on a single node.
Then, on two hosts:
- `--config-epoch` on each node shows one hash, allowed. Change one node's
  configuration: it's listed NOT allowed, logs the alarm, and never takes
  the lease; restore it.
- With `allowed_sm`: every node row lists its ports
  (`--known-sm-ports`), `opensm.conf`'s `allowed_sm_guids` on the leader
  holds both hosts' ports, and a takeover by the other host starts OpenSM
  as master with no `ERR 2F1C` or `0F09` for the old leader's port.
  Then stop the old leader's manager uncleanly (`docker kill`) and check
  that the new leader takes over within a takeover's bound.
- With `class_keys`: after a takeover by the other host, a VS key Get
  with the derived key still answers on a port keyed by the first host
  (hardware test 3's second-host leg), and no `ERR 9828` appears.
- With `m_key_per_port`: name it on every node; check that the leader
  arms it (`--port-key-states` shows `target on`), steps down to apply it,
  and that the takeover's start turns it on; then move the leadership (`docker stop` the leader): the new leader's log shows the
  cache rendered from the records, SUBNET UP, and no Bad M_Key traps from
  ports; `--port-key-states` lists no port lost. Each candidate's node
  row shows a non-zero reach. Then turn it off the same way.
- After each: the fabric check passes on the leader ("The fabric is
  complete"), and a port bound to a network goes ACTIVE.

## Turning hardening on for a running fabric

OpenSM reads `opensm.conf` only when it starts, so the manager must be
restarted, which restarts OpenSM. OpenSM then sets the M_Key on every port at
its first sweep. Hosts that run diagnostic tools need the M_Key from then on.

`m_key_per_port` is the exception: naming it and restarting doesn't change
any port's key at once. Per-port M_Keys begin at the start the manager
makes once its checks have armed them, or after `--confirm-fabric` has
(see Per-port M_Keys).

## Options the manager keeps

Whatever `hardening` says, `[opensm_conf]` can't set `partition_config_file`,
`daemon`, `log_file` or `no_partition_enforcement`, `part_enforce` must stay
`both`, and `log_flags` must keep the levels `0x07`, whose messages the
manager watches for. With `m_key_per_port` named, `[opensm_conf]` can't set
`m_key`, `m_key_per_port`, `m_key_protection_level` or `m_key_lookup`
either, and without it can't set `m_key_per_port` (see Per-port M_Keys).
`[opensm_conf] allowed_sm_guids` is checked like the list the `allowed_sm`
group writes (see Allowed subnet managers). While a class key is recorded
enabled, a start refuses removing `class_keys` or a `<class>_key_enable`
of 0 (see Turning class keys off).
OpenSM ignores an option it doesn't know, so check an override's spelling
in `opensm -c` output.
