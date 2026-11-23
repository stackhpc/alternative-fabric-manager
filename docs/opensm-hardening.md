# OpenSM hardening

This document is for operators who decide how OpenSM protects their
InfiniBand fabric, and who turn those protections on and off. It covers:

- the groups of hardened options AFM writes into `opensm.conf`, and how to
  choose them;
- the keys AFM keeps, where they are, and how to use them without showing
  them;
- the lease of the keys on ports, and permanent keys;
- the experimental groups, each with a "Before enabling" checklist and,
  where it leaves keys on ports, a procedure to turn it off;
- what changes in HA mode.

Every procedure here gives the `afm.conf` lines, the commands, what to
check and how to undo it. Each section's "Why" part, after its how-to,
explains the reasoning.

## Overview

OpenSM's defaults suit a fabric whose hosts all trust each other. A cloud's
fabric doesn't:

- **A bare metal tenant has full control of its InfiniBand adapter.** It
  can send any subnet management packet (SMP) or subnet administration (SA)
  request, or run a subnet manager of its own.
- **A guest on a virtual function has less control,** but can still make
  SA requests.

So with the managed driver (`[opensm] driver = managed`, the default), the
manager writes a hardened set of options into `opensm.conf`, in **groups**.
Everything is hardened by default, and you opt out of what your fabric
can't take. The other drivers don't write `opensm.conf`, so no group
applies to them.

### Terms

| Term | Meaning |
| --- | --- |
| SMP | A subnet management packet: what a subnet manager reads and sets each port's settings with. `smpquery`, `ibnetdiscover` and the like send them too |
| MAD | A management datagram: any InfiniBand management packet. SMPs are one class of MAD; vendor-specific (VS), congestion control (CC) and node-to-node (N2N) packets are others |
| SA | Subnet administration: OpenSM's query service, which clients ask for path records, multicast groups, service records and the like |
| M_Key | The management key. A port protected by it answers and accepts SMPs only if they carry it |
| SM_Key | The key subnet managers authenticate each other with when one hands over to another |
| SA_Key | The key an SA client sends to be trusted |
| Class keys | The keys of the VS, CC and N2N classes, which OpenSM derives from `key_mgr_seed` |
| Lease | How long a port keeps its key's protection after a wrong-keyed packet, while no subnet manager with the right key reaches it (see The lease) |
| Group | A named set of hardened options, chosen in `[opensm] hardening` |
| Experimental | Untested on hardware: not part of `all`, and not on by default |

### The groups

| Group | Protects against | Cost | In `all` | Experimental |
| --- | --- | --- | --- | --- |
| `m_key` | A host reading or changing port settings with SMPs, such as a bare metal tenant adding its switch port to any partition | SMP diagnostic tools need `-y $KEY`. The M_Key stays on ports, so turning it off takes two steps | Yes | No |
| `sm_key` | A rogue subnet manager taking over by handover | | Yes | No |
| `sa_key` | Any client being trusted by the SA with the well-known key | | Yes | No |
| `sa_trust` | Untrusted clients using SA requests beyond the minimal set, acting for other ports, adding alias GUIDs, or exhausting the SA | ConnectX-3 SR-IOV needs an override; `saquery` dumps need `--sa_key`; applications over the limits need higher limits | Yes | No |
| `enforcement` | Packets crossing partitions at a switch port; a port both a full and a limited member of a partition; a client claiming another port's GID | None: these are OpenSM's defaults already | Yes | No |
| `babbling_ports` | A port flooding the subnet manager with traps | OpenSM disables such a port, which may be someone else's | Yes | No |
| `m_key_per_port` | One port's M_Key opening every other port | Naming it re-keys every port, and leaves keys on ports. Each port needs its own key in `-y`. Managed driver and the `m_key` group only | No | Yes |
| `allowed_sm` | A tenant's subnet manager winning the election | OpenSM ignores every other subnet manager, a standby included. Managed driver, and the `m_key` and `sm_key` groups, only | No | Yes |
| `class_keys` | A host reconfiguring switches with vendor-specific or congestion control MADs | Leaves keys on ports. In-band tools (`mlxlink`, `mlxreg`, in-band `flint`, `ibdiagnet`) need the VS key | No | Yes |
| `sa_rate` | One port flooding the SA | May limit a flooder's neighbours too; heavy start-ups may need a higher limit | No | Yes |

HA mode refuses removing `m_key` or `sm_key`, which it depends on.

## Changing the hardening

The same steps apply to every change in this document: naming or removing
a group, changing the lease, or overriding an option.

> [!WARNING]
> **Agree the hardening with whoever shares the fabric** before anything
> that changes what OpenSM enforces: changing `hardening` or
> `key_lease_period`, setting hardening options by hand in
> `[opensm_conf]`, or deploying a release that adds a group to `all`.

1. **Agree it** with whoever shares the fabric.

2. **Back up** as in [Back up](#back-up) below, and record what you set,
   so that you can undo it.

3. **Edit `afm.conf`.** For example, to add an experimental group:

   ```ini
   [opensm]
   hardening = all, allowed_sm
   ```

4. **Apply it.** Each change takes effect when the manager restarts,
   which restarts OpenSM.
   - On a single node, follow README.md's
     [Changing the configuration](../README.md#changing-the-configuration).
   - In HA mode, follow docs/kayobe.md's
     [Change AFM's configuration](kayobe.md#change-afms-configuration).
     It changes every node's configuration, restarting the leader last.

5. **Check** what the start wrote. The manager logs the groups and the key
   leases at every start, and the `grep -vi "key 0x"` keeps any key out of
   your terminal:

   ```sh
   docker logs --since 5m alternative-fabric-manager 2>&1 \
     | grep -E "OpenSM hardening:|SUBNET UP|ERROR" | grep -vi "key 0x"
   ```

   You should see `OpenSM hardening: <groups>; key leases written: ...`
   with the groups you meant, then OpenSM's "SUBNET UP".

6. **To undo,** put the old line back and apply it the same way. Three
   groups leave keys on ports, and must not simply be removed:
   - `m_key`: see Turning M_Key protection off;
   - `class_keys`: see Turning class keys off;
   - `m_key_per_port`: see Per-port M_Keys, Turning them off.

### Back up

AFM's database holds the fabric's keys. Back it up before every change.

**On a single node**, run as in README.md's "Running it", with the
database at the default `[database] database`,
`/var/lib/alternative-fabric-manager/afm.sqlite`, in the `afm_data`
volume, and OpenSM's files in `/etc/opensm`. A plain copy of the SQLite
file while AFM writes it can be torn, so the first command takes a
consistent copy with SQLite's online backup, inside the container:

```sh
sudo install -d -m 700 /var/backups/afm
STAMP=$(date +%Y%m%d-%H%M%S)
# The database, which holds the keys: an online backup inside the volume,
# copied out, then removed from the volume
docker exec alternative-fabric-manager python3 -c 'import sqlite3
src = sqlite3.connect("/var/lib/alternative-fabric-manager/afm.sqlite")
dst = sqlite3.connect("/var/lib/alternative-fabric-manager/afm-backup.sqlite")
src.backup(dst)
dst.close()'
DATA=$(docker volume inspect -f '{{.Mountpoint}}' afm_data)
sudo cp -a "$DATA/afm-backup.sqlite" /var/backups/afm/afm-$STAMP.sqlite
sudo rm "$DATA/afm-backup.sqlite"
# AFM's and OpenSM's configuration; opensm.conf holds the keys too
sudo cp -a /etc/alternative-fabric-manager/afm.conf /var/backups/afm/afm-$STAMP.conf
sudo cp -a /etc/opensm/opensm.conf /var/backups/afm/opensm-$STAMP.conf
# OpenSM's cache volume, for class_keys and m_key_per_port
sudo cp -a "$(docker volume inspect -f '{{.Mountpoint}}' opensm_cache)" \
  /var/backups/afm/opensm_cache-$STAMP
```

Keep the backups in that root-only directory: they are as secret as the
keys.

To check that the backed-up database holds a key without showing it,
count its row in the backup file itself. This prints `1` when the backup
holds `m_key_base` (use `key_mgr_seed` for the class key seed):

```sh
sudo python3 -c 'import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("select count(*) from secrets where name = ?", (sys.argv[2],)).fetchone()[0])' \
  /var/backups/afm/afm-$STAMP.sqlite m_key_base
```

**In HA mode**, back up the database as docs/kayobe.md does:

```sh
kayobe overcloud database backup
```

### Commands in this document

The commands run beside the running manager. On a single node:

```sh
AFM="docker exec alternative-fabric-manager alternative-fabric-manager"
```

In HA mode, as docs/kayobe.md runs them, on any node:

```sh
AFM="${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf"
```

The one exception is `--confirm-fabric`, which asks the fabric through
OpenSM's container: run it on the leader's node. GUIDs are hexadecimal,
with or without `0x`.

The `docker logs` commands in this document use the single-node
container name, `alternative-fabric-manager`. In a Kayobe HA deployment
the manager's container is `alternative_fabric_manager` (see
docs/kayobe.md): use that name there.

## The `hardening` setting

`[opensm] hardening` says which groups, in one line, separated by commas:

| `hardening` | Groups |
| --- | --- |
| `all` (the default) | Every group below |
| `all, -sa_trust` or `-sa_trust` | Every group but `sa_trust` |
| `-sa_trust, -babbling_ports` | Every group but those two |
| `m_key, enforcement` | Only those two |
| `none` | None |

"Every group" here means every group in `all`: the experimental groups
aren't in it.

**Removals.** A group prefixed with `-` is removed from `all`. A list of
removals alone starts from `all`, since that's the default.

**Values the manager refuses.** The manager refuses a value it can't read
one way only, rather than guess:
- an unknown group, including a misspelt removal, which would otherwise
  leave the group on;
- a group both named and removed;
- removals beside a list of groups without `all` (list the groups to keep
  instead);
- `none` with anything else.

**Experimental groups.** Further hardening that matches the rest of
NVIDIA's Secured Cloud Profile is experimental, untested on hardware: not
part of `all`, and not on by default. The experimental groups are
`m_key_per_port`, `allowed_sm`, `class_keys` and `sa_rate`. Choose each by
naming it beside `all`, as `hardening = all, allowed_sm` (see
Experimental hardening). Before naming one, read its "Before enabling"
checklist: what to back up, what to check when it's turned on, what shows
it works or failed, and how to turn it off.

**Upgrades.** Once its hardware tests pass, each experimental group is
planned to join `all`. The release that adds one will say so in its
notes, and `-group` will remove it. So a deployment with `all` (or only
removals) gains each group its release notes name, and one with an
explicit list of groups gains none. On a fabric that others share, prefer
an explicit list of groups, so that nothing switches on at an upgrade
without their agreement.

**Overriding one option.** A single OpenSM option can still be overridden
in `[opensm_conf]`, and wins over the groups. For example:

```ini
[opensm_conf]
sa_etm_max_num_mcgs = 256
```

OpenSM ignores an option it doesn't know, so check an override's spelling
in `opensm -c` output. Some options can't be overridden: see Options the
manager keeps.

**The lease.** The keys the manager sets on ports have a lease of an hour
by default. A port sent a wrong-keyed packet that no subnet manager then
reaches for an hour drops its protection. So a lost key is expected to
recover by waiting, though that's not yet shown on hardware.
`[opensm] key_lease_period` changes the lease of every such key at once,
and `0` makes them permanent (see Permanent keys).

## Choosing what to harden

| Fabric | `hardening` |
| --- | --- |
| Only OpenStack's hosts, guests and bare metal nodes (the default) | `all` |
| With ConnectX-3 SR-IOV | `all`, with `sa_etm_allow_untrusted_guidinfo_rec = TRUE` in `[opensm_conf]` (see the SA enhanced trust model) |
| Shared with hosts outside OpenStack that need untrusted SA dumps, such as `saquery` | `m_key, sm_key, enforcement`: an explicit list, so that no group switches on without the agreement of whoever shares the fabric. `babbling_ports` is left out because it disables ports that flood traps, which may be theirs |
| Shared with hosts whose operators can't use the M_Key yet | `enforcement`, and plan M_Key protection: without it, a bare metal tenant can reconfigure switch ports |
| Single node, with bare metal tenants, on a fabric you control or whose owner agrees, trying per-port M_Keys | `all, m_key_per_port`: experimental, untested on hardware, and it leaves keys on ports (see Per-port M_Keys) |
| Single node, on a fabric where no other host should run a subnet manager, trying allowed SMs | `all, allowed_sm`: experimental, untested on hardware (see Allowed subnet managers) |

## What each group sets

| Group | Option | OpenSM default | Hardened | Stops |
| --- | --- | --- | --- | --- |
| `m_key` | `m_key` | `0` | Random, shared | A host reading or changing port settings with SMPs |
| `m_key` | `m_key_protection_level` | `0` | `2` | As above |
| `m_key` | `m_key_lease_period` | `0` | `[opensm] key_lease_period`, `3600` by default | A port giving up its protection, and its M_Key, during an outage of every SM shorter than the lease; `0` for any outage |
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

## The keys

The manager keeps five secrets in its database:

| Key | What it is | Written to `opensm.conf` |
| --- | --- | --- |
| `m_key` | The M_Key every port holds, without per-port M_Keys | With the `m_key` group |
| `sm_key` | The SM_Key | With the `sm_key` group |
| `sa_key` | The SA_Key | With the `sa_key` group |
| `key_mgr_seed` | The seed OpenSM derives the class keys (VS, CC and N2N) from | Only with the experimental `class_keys` group |
| `m_key_base` | The base that per-port M_Keys derive each port's key from | As the `m_key` line, only while per-port M_Keys are on |

**`m_key`, `sm_key` and `sa_key`.** The manager generates them on first
start, as random 64-bit values, and keeps them in its database. Only the
keys of the chosen groups are written. A key set in `[opensm_conf]` wins
over the generated one.

**`key_mgr_seed`.** The manager stores the seed, if it's missing, whenever
it starts OpenSM (in HA mode, when a node takes over), whatever the
groups. `--show-keys` prints it, and it is as secret as the other keys.

Unlike the other keys, the stored seed can't be replaced:
- If `[opensm_conf] key_mgr_seed` is set when no seed is stored, as on a
  fabric whose class keys were set by hand, the manager stores that value
  rather than a random one, so that the ports' seed is kept.
- Otherwise the manager refuses an override that differs from the stored
  seed.
- The override must be a non-zero 64-bit value, in hexadecimal (`0x...`)
  or decimal without leading zeros.

**`m_key_base`.** The manager stores this fifth key in the same way (see
Per-port M_Keys). It is stored, if it's missing, whatever the groups, and
it is written to `opensm.conf`, as the `m_key` line, only while per-port
M_Keys are on. It opens every port of the fabric once they are:
`--show-keys` prints it as `m_key_base`, and it is the most secret of the
keys.

### Where the keys are

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

Keep them all from tenants and from log shipping.

### Using the keys

`--show-keys` prints the keys, one per line, as `<name> <value>`. Never
show its output: use it only to feed a tool, by reading one key into a
variable (see Diagnostics):

```sh
KEY=$($AFM --show-keys | awk '$1 == "m_key" {print $2}')
```

To check that a key is stored, without showing it, count its line. This
prints `1` when the seed is stored:

```sh
$AFM --show-keys | awk '$1 == "key_mgr_seed" {n++} END {print n + 0}'
```

`--show-keys` reads the live database, not a backup, and it isn't
read-only: it generates and stores `m_key`, `sm_key` or `sa_key` if one is
missing. It never generates the seed or the per-port M_Key base, which
only a start stores, so a missing one is left out of its output.

### Why the keys are kept this way

- **One set of keys for every OpenSM.** Every OpenSM of the fabric must
  use the same keys: in high availability, a standby that took over with
  another M_Key couldn't configure the ports. Keeping them in the database
  gives every manager the same keys, and keeps them across restarts.
- **The seed is stored a release early.** OpenSM derives the class keys
  from it, and a port keyed with a lost seed can't be managed again. So the
  seed is stored before it is ever used, for database backups to hold it.
- **The base is stored early too,** so that backups hold it before any
  port is keyed from it.

## M_Key protection

With `m_key_protection_level 2`, a port answers a SubnGet, and accepts a
SubnSet, only if the SMP carries its M_Key. OpenSM sets the M_Key on every
port when it configures it. The `m_key` group, in `all`, turns this on.

**Why.** The M_Key is what stops a host from reconfiguring the fabric.
Without it, any host can send an SMP that changes a port's settings,
including a switch port's pkey table, which is what switches enforce
partitions with. A bare metal tenant could then add its own switch port
to any partition.

### The lease

A port that receives an SMP without its M_Key sends the SM a Bad M_Key trap
and, if its lease period isn't 0, starts a countdown. An SM that answers with
the right M_Key stops it, and OpenSM's sweeps do too. If the countdown reaches
zero, the port drops to protection level 0, where a query returns the M_Key
itself (opensm(8)).

So a lease is a window: while no subnet manager runs for longer than the
lease, a host sending SMPs without the M_Key gets a port's protection, and
the M_Key, back.

**Short windows are expected.** The manager stops OpenSM whenever it
restarts, and in high availability every OpenSM is shut down while the
database can't be reached.

**The default lease is an hour,** far longer than a restart or a takeover:
only an outage of every subnet manager that long opens the window. In HA
mode that includes a database outage. Every OpenSM stops while Galera has
no Primary component, so a quorum loss or maintenance of the database
lasting over an hour opens it. Noticing doesn't help, since no OpenSM can
start without the database. Freeze the lease before planned database
maintenance (specs/ha.md, Freeze).

**A lapse gives a tenant the M_Key for good.** Without per-port M_Keys it
is one key for every port, and the manager can't rotate it yet.

**A lease of `0` closes the window, at the cost of recovery.** A fabric
whose M_Key is lost, for example with the manager's database, can't be
configured by a new subnet manager until every port is reset, by
power-cycling or by firmware tools.

The default favours recovery, which is expected but not yet shown on
hardware (see Permanent keys). A fabric that prefers the opposite, or whose
database may be out for an hour, sets `key_lease_period = 0`. Back up the
database either way.

## Permanent keys

Some keys are stored on the ports themselves, not only in the subnet
manager: today the M_Key, and the class keys once a group sets them. With a
lease of `0`, a port keeps its key and its protection for good. That is the
safest setting against tenants, and the hardest to recover from: a port
whose key is lost, with the database or by a mistake, stays locked until
it's power-cycled or reset by firmware tools.

`[opensm] key_lease_period` sets the lease of every key the manager puts on
ports, now and in groups added later, in one line. It is an hour by
default; `0` opts in to permanent keys.

### To change the lease

1. **Agree it** with whoever shares the fabric: the lease changes what
   OpenSM enforces.

2. **Back up** (see [Back up](#back-up)).

3. **Set it** in `afm.conf`:

   ```ini
   [opensm]
   # Seconds, up to 65535; 3600 by default. 0: permanent keys.
   key_lease_period = 3600
   ```

   In HA mode every node must have the same `key_lease_period` (see In HA
   mode, below).

4. **Apply it** as in [Changing the hardening](#changing-the-hardening).
   Changing the option takes effect when the manager restarts, which
   restarts OpenSM. In HA mode, it takes effect once the configuration
   epoch moves.

5. **Check** the start's log line, `OpenSM hardening: ...; key leases
   written: ...`. It lists each lease it writes, such as
   `m_key_lease_period 3600 s`, or `permanent` for `0`, or says it writes
   none. Also check for these warnings:
   - `[opensm_conf] sets ..., which wins over [opensm] key_lease_period`:
     see Leases set for one key, below;
   - in HA mode, `[opensm] key_lease_period is N s, no longer than a
     takeover's longest, ...` (see In HA mode, below).

6. **To undo,** set the old value and apply it again.

What a change does to ports already keyed:
- OpenSM is expected to set the new lease on a port when it next
  configures it with the right key. That hasn't been shown yet for a port
  whose lease was already set.
- Setting a lease doesn't unlock a port whose key is already lost: OpenSM
  can't reach that port to change its lease.
- The keys the subnet manager keeps to itself, the SM_Key and the SA_Key,
  aren't stored on ports, have no lease, and aren't affected.

**Upgrading from permanent keys.** A release before this default had
permanent keys by default. Upgrading a deployment that doesn't set
`key_lease_period` gives every port the hour's lease at the next start (in
HA mode, once the configuration epoch moves: specs/ha.md, Configuration
epochs). On a fabric others share, agree it, or set
`key_lease_period = 0` in the same change to keep permanent keys.

**Leases set for one key.** The option covers every key lease that a
hardening group writes. A lease set for one key in `[opensm_conf]`, such as
`m_key_lease_period = 0`, still wins over it, and the manager logs a
warning at start, since that key is then not covered. A fabric that
followed earlier versions of this doc, with `m_key_lease_period = 600` in
`[opensm_conf]`, should move it to `[opensm] key_lease_period = 600`.

**In HA mode** every node must have the same `key_lease_period`, since each
writes its own `opensm.conf`. Otherwise every takeover changes every port's
lease. It's fabric-affecting, like the hardening groups (specs/ha.md,
Configuration epochs). The manager warns when the lease is shorter than the
longest a takeover can leave the fabric without a subnet manager, since
every unclean takeover would then be a lapse.

**Short leases are for testing.** A short lease, such as `60`, is for
testing hardening on a fabric that nobody else depends on. A mistake there
may become recoverable within minutes, by the recovery procedure below.
Never run a tenant fabric with a lease that short: every restart of the
manager would give any tenant the M_Key.

### Why the lease is a trade-off

With a lease, a port that is sent a packet with the wrong key starts a
countdown. Packets with the right key, such as OpenSM's sweeps, stop it.
If the countdown runs out, the port drops its protection. For the M_Key,
that's protection level 0, where a query returns the M_Key itself. So a port
keeps its protection while a subnet manager with the right key reaches it,
and can lose it once the lease passes without one.

Choose the lease with both sides in mind.

**Exposure: one lapse gives the key away for good.**
- The countdown is started by whoever sends the wrong key, so any tenant
  can start it on its own switch port at will.
- If no subnet manager reaches that port for longer than the lease, the
  tenant reads the M_Key. That is one M_Key for every port of the fabric,
  and the manager can't rotate it yet.
- Once the subnet manager is back and the port is protected again, the
  tenant still holds the key, and can read and set any port on the fabric
  for good. (With per-port M_Keys on, a lapsed port gives away only its
  own key, which opens only that port; see Per-port M_Keys.)

So treat any outage with no subnet manager that lasts longer than the
lease as a compromise of the M_Key. The manager stops OpenSM whenever it
restarts, and in HA mode during a takeover, and while the database can't
be reached. Make the lease longer than the longest such outage you expect:
minutes, not seconds.

This has happened on a test fabric. With a 60 s M_Key lease, a few unkeyed
queries to a switch, then three minutes with no subnet manager, dropped the
switch's protection. It answered unkeyed queries, and so would have given
out the M_Key, for some minutes after the subnet manager started again
(see specs/secured-profile.md, Results). So don't count on a running subnet
manager to restore a lapsed port's protection promptly: the window can
outlast the outage.

**Recovery is expected, not yet shown.** A lease is meant to let a port
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

### Turning M_Key protection off

Ports keep their M_Key until a subnet manager with it changes their
settings, or a lease runs out. So turn M_Key protection off in two
restarts, keeping the M_Key until the ports' protection is lowered.

**Single node only.** HA mode can't follow this procedure: it requires
the `m_key` group, and refuses `m_key_protection_level 0`.

1. **Agree it** with whoever shares the fabric, and **back up** (see
   [Back up](#back-up)).

2. **Keep the M_Key, and lower the protection.** Keep `m_key` in
   `hardening`, and add to `afm.conf`:

   ```ini
   [opensm_conf]
   m_key_protection_level = 0
   ```

   The manager refuses the `allowed_sm` group with
   `m_key_protection_level 0` (see Allowed subnet managers): remove it
   first.

3. **Restart the manager** as in
   [Changing the hardening](#changing-the-hardening).

4. **Check before the second restart.** Wait for OpenSM's "SUBNET UP" in
   the manager's log, and a passing fabric check:

   ```sh
   docker logs --since 5m alternative-fabric-manager 2>&1 \
     | grep -F "OpenSM: SUBNET UP"
   $AFM --status
   ```

   Optionally, spot-check a port's protection level. Show only that
   field: `portinfo`'s output includes the M_Key itself.

   ```sh
   KEY=$($AFM --show-keys | awk '$1 == "m_key" {print $2}')
   smpquery -y $KEY portinfo <lid> | grep -i MkeyProtBits
   unset KEY
   ```

   A port OpenSM can't reach keeps its protection until its lease lapses.

5. **Then drop `m_key` from `hardening`,** for example
   `hardening = all, -m_key`, and restart the manager again.

Don't drop `m_key` at once: that leaves the ports protected by a key the
subnet manager no longer has.

### Diagnostics

Diagnostic tools that send SMPs from a host, such as `ibnetdiscover`,
`iblinkinfo`, `smpquery`, `ibportstate`, `ibhosts` and `sminfo`, need the
M_Key, with `-y <m_key>`. Tools that only read the local adapter, such as
`ibstat`, don't.

Take the key into a variable, never onto the screen:

```sh
KEY=$($AFM --show-keys | awk '$1 == "m_key" {print $2}')
smpquery -y $KEY nodeinfo <lid>
unset KEY
```

Match the name exactly, as above: `/^m_key/` matches `m_key_base` too.

With per-port M_Keys, each port has a key of its own, so one `-y` opens one
port. Take it from `--port-m-key <guid>` instead, and query that port only
(see Per-port M_Keys, Diagnostics with per-port M_Keys).

## SM_Key

The `sm_key` group, in `all`, gives the fabric's subnet managers a secret
SM_Key.

**Why.** Subnet managers authenticate each other with the SM_Key when one
hands over to another. With OpenSM's well-known default, any host running
OpenSM could negotiate a handover from the master. With a secret SM_Key,
only the fabric's own subnet managers can. A rogue subnet manager can still
try to win the election, which is why the M_Key matters most: without it,
the rogue can't configure the ports.

## The SA enhanced trust model

The `sa_trust` group, in `all`, turns on OpenSM's enhanced trust model. A
client that sends the SA_Key is trusted, and one that sends `0` is not.
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
  fabric with ConnectX-3 virtual functions, set:

  ```ini
  [opensm_conf]
  sa_etm_allow_untrusted_guidinfo_rec = TRUE
  ```

  ConnectX-4 and later can give virtual functions virtual ports instead,
  when OpenSM's `virt_enabled` is on and the firmware supports it; check
  that before relying on it.
- **Diagnostics:** `saquery` dumps need `--sa_key` or fail.
- **Applications** that register more than the limits, which is unusual,
  need higher limits.

## Partition enforcement

The `enforcement` group, in `all`, writes OpenSM's own defaults:

- `part_enforce both` makes switches check the partition of every packet
  in both directions at the port, which is what makes a partition a
  boundary.
- `allow_both_pkeys FALSE` keeps a port from being both a full and a
  limited member of the same partition, which the manager's partitions
  never need.

## Babbling ports

With `babbling_port_policy TRUE`, set by the `babbling_ports` group in
`all`, OpenSM disables a port that keeps sending it traps, so a
misbehaving or hostile host can't flood the subnet manager.

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
Each is designed in specs/secured-profile.md.

| Group | Leaves state on ports | Read first |
| --- | --- | --- |
| `allowed_sm` | No | Allowed subnet managers, and Before enabling allowed SMs |
| `class_keys` | Yes: class keys | Class keys, and Before enabling class keys |
| `sa_rate` | No | SA request rate, and Before enabling the SA request rate |
| `m_key_per_port` | Yes: each port's M_Key | Per-port M_Keys, and Before enabling per-port M_Keys |

Some are groups already, experimental, untested on hardware:
`m_key_per_port`, `allowed_sm`, `class_keys` and `sa_rate`. Each comes with
the manager's checks, is chosen by naming it beside `all`, and is planned
to join `all` once its hardware tests pass. HA mode allows all four,
experimental there too: see In HA mode below for what changes, and what to
test.

**Each group has a "Before enabling" checklist,** drawn from the hardware
tests the spec plans for it. Its first enabling on a fabric is that test:
follow the checklist, and record what you saw.

**Rules for every experimental group:**
- Try them only on a fabric you control, or with the agreement of everyone
  who shares it.
- Back up the database first (it holds the keys), and record what you
  set, so that you can undo it.
- In HA mode, change every node's configuration, restarting the leader
  last: configuration epochs keep a node whose configuration differs from
  the leader's from leading, and the epoch moves to the new configuration
  by itself once every live node has it (specs/ha.md, Configuration
  epochs). docs/kayobe.md's
  [Change AFM's configuration](kayobe.md#change-afms-configuration) does
  this.
- On a deployment whose configuration is managed, such as Kayobe's, add
  them there, not by hand, or the next reconfigure removes them.
- Each change takes effect when the manager restarts, which restarts
  OpenSM.

### Allowed subnet managers

**Experimental, untested on hardware.** With `allowed_sm_guids`, OpenSM
ignores every subnet manager whose port isn't in the list (`ERR 0F09`), and
drops SMInfo Sets from them (`ERR 2F1C`).

**What the group writes.** The `allowed_sm` group writes the list for you:
this host's physical InfiniBand ports. The manager reads them before every
start of OpenSM, in a short-lived container of OpenSM's image named like
the prober's (`opensm-prober`). It reads sysfs only, and leaves out:
- virtual functions (devices with a `device/physfn` link) and mlx5
  sub-functions, which belong to tenants;
- ports whose link layer isn't InfiniBand.

Choose it with `hardening = all, allowed_sm`. In HA mode the list is every
node's ports, which each node's manager records in its node row (see In HA
mode).

**When the manager refuses the group:**
- without the `m_key` or `sm_key` groups, or with
  `m_key_protection_level 0`: an ignored subnet manager is harmless only
  without the keys;
- with a driver other than `managed`.

**Overriding the list.** `[opensm_conf] allowed_sm_guids` replaces the
generated list. It must be `0` (no other subnet manager) or port GUIDs,
each `0x` and up to 16 hex digits, separated by commas, and the manager
writes it in its own spelling. An override puts the list in effect even
without the group, with the same rules: a list typed by hand, as earlier
versions of this doc suggested, is now checked. In HA mode it can't be
`0`, and a start refuses one that leaves out any node row's port, naming
the port: check a list typed by hand before upgrading an HA cluster.

**When the manager won't start OpenSM.** Before every start, the manager
refuses to start OpenSM, logs "Not starting OpenSM: ...", and tries again
at every `[opensm] check_interval`, if:
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

**Known SM ports.** The commands:

```sh
$AFM --known-sm-ports            # each known SM port: "this host" or "recorded"
$AFM --record-sm-port <guid>     # a port whose host may hold the keys
$AFM --drop-sm-port <guid>       # one whose adapter no longer is in such a host
```

- **Record** the ports of any host that has held the fabric's keys: one
  that ran AFM before, or where `opensm.conf` or a backup was kept.
- **Drop** a port only once its adapter is in a host that never had them,
  such as a reused card in a tenant's host: otherwise its subnet manager
  contests the fabric for good. `--drop-sm-port` refuses this host's own
  ports.

**Untested on hardware** (specs/secured-profile.md, hardware tests 1 and
2):
- whether a tenant's subnet manager can send AFM's OpenSM to STANDBY
  without the list;
- whether OpenSM logs `0F09` once or at every sweep (the manager handles
  either);
- whether OpenSM refuses to run on a port that isn't in its list;
- whether `0` lets OpenSM itself run;
- and what a SIGHUP re-reads.

#### Before enabling allowed SMs

1. **Back up** the database, `afm.conf` and `opensm.conf` (see
   [Back up](#back-up)), and note this host's physical port GUIDs
   (`ibstat -p`, leaving out virtual functions).

2. **Agree it** with whoever shares the fabric, and find out whether any
   other host runs a subnet manager, such as a standby: once the list is
   on, OpenSM ignores it. If one holds the fabric's keys, record its ports
   first, and expect the check to fail while it runs:

   ```sh
   $AFM --record-sm-port <guid>
   ```

3. **Turn it on.** Set:

   ```ini
   [opensm]
   hardening = all, allowed_sm
   ```

   and restart the manager (see
   [Changing the hardening](#changing-the-hardening)).

4. **Check that:**
   - the manager logs "OpenSM ignores every SM outside allowed_sm_guids:"
     with exactly this host's physical ports, and `opensm.conf` has the
     same `allowed_sm_guids` line:

     ```sh
     docker logs alternative-fabric-manager 2>&1 \
       | grep -F "OpenSM ignores every SM outside" | tail -1
     sudo grep '^allowed_sm_guids' /etc/opensm/opensm.conf
     ```

   - OpenSM's log has no `ERR 7549` (this prints `0`; OpenSM's log holds
     keys, so count, don't show it), and OpenSM reaches SUBNET UP as
     master:

     ```sh
     docker logs opensm 2>&1 | grep -c "ERR 7549"
     ```

   - `--known-sm-ports` lists this host's ports as "this host":

     ```sh
     $AFM --known-sm-ports
     ```

   - the fabric check passes (`$AFM --status`), and a port's job
     completes.

5. **It works if,** on a fabric nobody else uses (hardware test 1), a
   subnet manager started on another host at a higher priority leaves AFM's
   OpenSM master: OpenSM logs `ERR 0F09` naming that host's port, the
   manager logs that it isn't a known SM port, and the fabric check still
   passes. With that port recorded, the check fails with the contest detail
   while it runs, and passes again once it has stopped.

6. **It failed if** the manager logs "Not starting OpenSM: ...", OpenSM's
   log has `ERR 7549`, OpenSM enters STANDBY, or the fabric check reports
   another master; or if a subnet manager the list should ignore changes a
   port.

**To turn it off,** remove the group (and any `[opensm_conf]
allowed_sm_guids`) and restart the manager. The group leaves nothing on
ports, so that's all it takes. If a start is refused, the fabric has no
subnet manager until it's fixed: correct the list, or turn the group off.

#### Why allowed SMs

A tenant can run a subnet manager of its own. Without the SM_Key it can't
take over by handover, and without the M_Key it can't set ports, but it may
still win the election and send AFM's OpenSM to STANDBY, leaving the
fabric without a working subnet manager. The list stops OpenSM listening
to it.

### Class keys

**Experimental, untested on hardware.** OpenSM derives each port's class
keys from `key_mgr_seed`, which the manager keeps in its database.

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
- **Leases** are `[opensm] key_lease_period`, an hour by default.
  Whether 0 means "never expires" for these classes, as for the M_Key, is
  untested. A lease is what would let a port keyed by mistake be keyed
  again without a power cycle, but that recovery hasn't been shown (see
  Permanent keys).
- **Not every device supports every class.** For ports that don't, OpenSM
  logs `ERR 9828` (or the N2N manager's `ERR 9B28`, or the congestion
  control manager's `ERR C10A`) at every sweep. On the test fabric, only
  VS keys were supported. The manager doesn't fail on it, and logs it, for
  each OpenSM it starts, as the set of ports grows: "OpenSM's VS key
  manager reports N ports answering a KeyInfo with status 0xc (ERR 9828,
  attr_id 0x...)". The line names the attribute, not the class: those
  ports aren't protected by that class's key.
- **Key errors** (`9803`, `9822`, `9823`, `9B03`, `9B22`, `C124`, `C125`:
  "failed to update port ... keyinfo") are logged once per code for each
  OpenSM ("OpenSM's ... key manager failed to set class keys"). They don't
  fail the fabric check.
- **In-band tools need the VS key,** and which of them can take it is still
  being worked out. `ibdiagnet` takes `--vs_key_file`, pointed at OpenSM's
  `/var/cache/opensm/guid2vskey`, which makes that cache a secret.
- **Tested so far:** on one host, by hand before the group existed, VS
  keys derive from the seed alone. Their effect on tenants, turning them
  off, a takeover, and a lease of 0 are untested (see
  specs/secured-profile.md, Results).

#### Before enabling class keys

1. **Check that the database holds the seed, then back up** the
   database and OpenSM's cache volume (`opensm_cache`) (see
   [Back up](#back-up)). This checks the live database without showing
   the seed, and prints `1`:

   ```sh
   $AFM --show-keys | awk '$1 == "key_mgr_seed" {n++} END {print n + 0}'
   ```

   On a single node, check the backup file too, as in
   [Back up](#back-up), with `key_mgr_seed`. Restoring a database without
   that seed loses every keyed port.

2. **Agree it** with whoever shares the fabric: in-band tools (`mlxlink`,
   `mlxreg`, in-band `flint`, `ibdiagnet`, in-band firmware upgrades) need
   the VS key once it's on, and which can take it isn't settled. For the
   first run, prefer a fabric nobody else uses, and consider a shorter
   lease, such as `key_lease_period = 600`, so that a mistake may recover
   on its own sooner.

3. **Turn it on.** Set, for example:

   ```ini
   [opensm]
   hardening = all, class_keys
   key_lease_period = 600
   ```

   and restart the manager (see
   [Changing the hardening](#changing-the-hardening)).

4. **Check that:**
   - the start didn't withhold the group ("... OpenSM starts without class
     keys"): if it did, back up and restart again;

     ```sh
     docker logs --since 10m alternative-fabric-manager 2>&1 \
       | grep -F "OpenSM starts without class keys"
     ```

   - `opensm.conf` has the lines above, and OpenSM reaches SUBNET UP. Show
     the lines without the seed's, and count the seed's line, which
     prints `1`:

     ```sh
     sudo grep -E '^(vs|cc|n2n)_key_(enable|ci_protect_bits|protect_bit|lease_period) ' \
       /etc/opensm/opensm.conf
     sudo grep -c '^key_mgr_seed ' /etc/opensm/opensm.conf
     ```

   - OpenSM wrote a VS key for every port to `guid2vskey`. Count the
     lines, never show them:

     ```sh
     sudo wc -l "$(docker volume inspect -f '{{.Mountpoint}}' opensm_cache)/guid2vskey"
     ```

   - the manager's log names which ports rejected which attribute (`ERR
     9828`), and no key error;
   - the fabric check passes, and a port's job completes.

5. **It works if** a VS query that sends no SMP, such as `vendstat -N` to a
   switch, fails from a host without the VS key and answers with it, and a
   restart of the manager, which restarts OpenSM, logs no key error.

6. **It failed if** the manager logs a key error ("failed to set class
   keys"), OpenSM's KeyInfo Gets time out (`ERR 980B`), or VS queries
   answer without the key while `guid2vskey` lists the port.

**To turn it off,** follow Turning class keys off. If the seed was lost,
the keyed ports need a lease lapse, a power cycle, or in-band firmware
tools given the old seed's keys.

#### Turning class keys off

Ports keep their class keys until an SM that has them changes them. So
clear the keys from the ports before removing the group. The manager
records which classes the last `opensm.conf` it wrote enabled, and refuses
a start that would skip step 1, with "Not starting OpenSM: ports may hold
class keys ...".

1. **Disable each class, keeping the group.** Keep `class_keys` in
   `hardening`, set each class to Disable (`1`), and restart. For
   example:

   ```ini
   [opensm]
   hardening = all, class_keys

   [opensm_conf]
   vs_key_enable = 1
   cc_key_enable = 1
   n2n_key_enable = 1
   ```

   OpenSM clears the keys from the ports, reaching them with the keys it
   derives from the seed, which the group still writes.

2. **Check that OpenSM has finished:** no port answers a VS query without
   the key refused, and OpenSM's cache files `guid2vskey`, `guid2cckey`
   and `guid2_n2n_key` no longer name keys. How to tell it has finished on
   every port is what hardware test 3 is to establish; the manager can't.

3. **Only then remove the group and the `<class>_key_enable` lines,** and
   restart.

**Class keys enabled by hand without a seed,** as before 0.4.0, came from a
seed the manager doesn't have, so naming the group wouldn't help. There,
step 1 is setting each `<class>_key_enable = 1` without the group, which
the manager allows only while the last `opensm.conf` it wrote had no seed.

**What the manager refuses.** Removing the group at once, or setting a
class's enable to 0 (Ignore), leaves the ports keyed with keys the subnet
manager no longer manages: the manager refuses both while a class is
recorded enabled.

**Downgrades and restores.** A downgrade to a release without the group,
or restoring a database from before the record, has the same effect, and
nothing refuses it: do step 1 before either.

#### Why class keys

Switches and adapters accept other classes of management packets than
SMPs. Vendor-specific (VS) packets are what in-band `mlxlink`, `mlxreg` and
`flint` use. Congestion control (CC) packets set the congestion parameters
of every port. A tenant can send either to any switch unless the class is
keyed.

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

1. **Back up** `afm.conf` and `opensm.conf` (see [Back up](#back-up)). The
   group leaves nothing on ports.

2. **Agree it** with whoever shares the fabric: their clients' SA requests
   are limited too.

3. **Turn it on.** Set:

   ```ini
   [opensm]
   hardening = all, sa_rate
   ```

   and restart the manager (see
   [Changing the hardening](#changing-the-hardening)).

4. **Check that** `opensm.conf` has `sa_rate_threshold 1000`, OpenSM
   reaches SUBNET UP, and the fabric check passes:

   ```sh
   sudo grep '^sa_rate_threshold' /etc/opensm/opensm.conf
   $AFM --status
   ```

5. **It works if** (hardware test 4, on a fabric nobody else uses) a guest
   flooding the SA, such as with `saquery` in a loop, is named in a `4C11`
   line, and a guest on another virtual function of the same physical port
   is still answered; and an MPI job's start-up across the fabric logs no
   `4C11`.

6. **It failed if** `4C11` names a GUID that isn't the flooder's, such as
   its neighbours' or this host's, the fabric check fails with "saquery
   failed", or a job's start-up is limited.

**To turn it off,** remove the group (and any `sa_rate_threshold`) and
restart the manager, or raise the threshold:

```ini
[opensm_conf]
sa_rate_threshold = <a higher limit>
```

### Per-port M_Keys

With per-port M_Keys, OpenSM gives each port its own M_Key, derived from a
base key, `m_key_base` (see The keys), and the port's GUID. So a key read
from one port opens only that port, which its tenant controls anyway.

Per-port M_Keys are experimental, untested on hardware as a group, and not
in `all`. Read Before enabling per-port M_Keys first. Choose them with
`hardening = all, m_key_per_port`, or with `m_key_per_port` in an explicit
list. In HA mode, see In HA mode.

> [!WARNING]
> **Naming the group is the decision to turn them on.** Per-port M_Keys
> leave state on ports: their keys change, and the stored `m_key` alone no
> longer opens them. Once the fabric is ready, the manager arms the
> turn-on and restarts OpenSM to apply it by itself, which re-keys every
> port (see Turning them on). On a fabric that others share, agree it with
> them before naming the group, and before removing it again, which
> re-keys every port back.

**When the manager refuses the group:**
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

**Commands.** The commands below use `$AFM` (see
[Commands in this document](#commands-in-this-document)): they run beside
the running manager, as for `--show-keys`; in HA mode, on any node.
GUIDs are hexadecimal, with or without `0x`.

**Terms used below:**

| Term | Meaning |
| --- | --- |
| Target | Whether per-port M_Keys should be on. The manager sets it on when it arms the turn-on |
| Arming | Recording the target on, once the fabric is ready |
| Applying | Restarting OpenSM so that it runs with the target |
| Records | The `fabric_ports` and `fabric_links` tables: each port, which key it holds, and each link (see Running with them) |
| Prober | The manager's directed-route `smpquery` Gets, in OpenSM's image (see Running with them) |
| Full verify | A prober run over every recorded port behind a link that's up, and every switch port's link state |
| `m_key`, `derived`, `unknown` | Which key a port's record says it holds: the stored `m_key`, its derived key, or not known |
| Lapsed | A port whose protection was 0 at the last Get, so it answers any key |
| Released | A port an operator released with `--release-port` |

#### Before enabling per-port M_Keys

Hardware test 5 ran the steps by hand, before the group existed; the
implemented group hasn't been run on hardware yet (see Still to be shown).

1. **Check that the database holds `m_key_base`, then back up** the
   database and OpenSM's whole cache volume (`opensm_cache`,
   `/var/cache/opensm`), and keep both with `afm.conf` (see
   [Back up](#back-up)). This checks the live database without showing
   the base, and prints `1`:

   ```sh
   $AFM --show-keys | awk '$1 == "m_key_base" {n++} END {print n + 0}'
   ```

   On a single node, check the backup file too, as in
   [Back up](#back-up).

2. **Agree it** with whoever shares the fabric. For the first run, prefer a
   fabric nobody else uses, and consider a shorter lease than the default
   hour, such as `key_lease_period = 600`, which hardware test 5 used, so
   that a mistake recovers on its own sooner. Never use 0 there: a port
   whose key is lost then stays out of reach until it's reset (see
   Permanent keys). Then run again with the lease you mean to keep.

3. **Name the group only once you're ready for the turn-on.** Not naming
   it is the way not to arm: the manager records no ports before the
   group is named (or ports are recorded), so `--port-key-states` lists
   none until then. Once it's named, the manager arms and applies the
   turn-on by itself as soon as the ports are ready.

4. **After naming the group, watch `--port-key-states`.** Check that it
   lists every physical port you expect, each `m_key`, `reachable`, and
   none `lapsed`, and read what blocks the arming:

   ```sh
   $AFM --port-key-states
   ```

5. **When turning it on,** follow Turning them on, and check that:
   - the start logs "Per-port M_Keys are on: OpenSM gave this host's port
     and N switches their derived keys", then "Per-port M_Keys are on on
     every reachable port":

     ```sh
     docker logs alternative-fabric-manager 2>&1 | grep -F "Per-port M_Keys are on"
     ```

   - `--port-key-states` shows `target on, OpenSM last started on`, and
     `derived` for every reachable port;
   - the fabric check passes, and a port's job completes.

6. **It works if** a port answers a Get with the key `--port-m-key <guid>`
   gives, taken into a variable, and refuses the stored `m_key`, the
   switches included (test 5's gate); and a port reset on a second host
   comes back keyed with its derived key. To take each key without showing
   it, see Diagnostics with per-port M_Keys.

7. **It failed if** the log shows "The start that turns per-port M_Keys on
   failed", the fabric check fails with a detail from the table in Running
   with them, the sweeps report Bad M_Key traps that keep rising, or a
   check says "this host's port answers neither the stored M_Key nor its
   derived key".

**To turn it off,** follow Turning them off, and wait for every port to
hold `m_key` before `--forget-port-keys`. If a start fails part-way, the
manager leaves OpenSM on the target and retries hourly (see Turning them
on). If the database and the cache volume don't belong together, see
Backups and restores.

#### Turning them on

OpenSM must know every port's GUID before the start that turns per-port
M_Keys on, or it sends those ports a key of 0 and loses them. So the
manager turns them on only once its records show every port ready.

1. **Agree it** with whoever shares the fabric, and **back up the
   database** before naming the group (see Before enabling per-port
   M_Keys). The base is stored a release before any port is keyed with
   it, so routine backups hold it. A lost database with no backup since
   strands every port with a lease of 0 (see Backups and restores), which
   a non-zero `key_lease_period` makes recoverable by waiting.

2. **Name the group and restart the manager.**

   ```ini
   [opensm]
   hardening = all, m_key_per_port
   ```

   OpenSM runs as before, with the shared `m_key`. A start that stored
   `m_key_base` can't arm the turn-on (the first start of a release with
   this group stores it, whatever the groups): restart once more, after a
   backup. The manager records the fabric's ports and links from OpenSM's
   cache at every fabric check. From then on the fabric check, job
   completion and the sweeps follow the rules in Running with them,
   whichever key the ports hold.

3. **The manager arms it.** At each fabric check that passes, with the
   target off and no failed turn-on recorded, it checks the turn-on's
   conditions on the check's last full verify. That verify must be
   complete and taken since OpenSM last started. The conditions, with
   what `--port-key-states` shows while each fails:
   - the base was stored by an earlier start than the running manager's
     ("the running manager's start stored the per-port M_Key base:
     restart the manager after backing up the database, which then holds
     it");
   - the stored base is the one any ports were keyed from before ("its
     fingerprint differs"; see Backups and restores);
   - links are recorded ("no links are recorded: OpenSM's cache hasn't
     listed the fabric yet ...");
   - every recorded port behind a link that's up answers the stored
     `m_key`, with protection level 2, so none is `unknown` ("every
     recorded port behind an up link must answer the stored M_Key, with
     protection level 2, and these don't: 0x..."). A port behind a link
     that's down doesn't stop it: it keeps its `m_key` record, and OpenSM
     re-keys it through that record when it comes back;
   - in HA mode, the configuration epoch allows exactly this node's
     configuration (see In HA mode).
   - While a condition fails, it logs "Per-port M_Keys aren't armed: ...",
     once per change of reason, naming the ports that block it, and
     `--port-key-states` shows the reason.
   - Once they pass, it records the target on, logging "Per-port M_Keys
     are armed".

4. **The manager applies it.** At a fabric check that passes with no open
   job (a deferred job, which waits for another object, doesn't count),
   while the target differs from what OpenSM runs with, it restarts
   OpenSM, "Restarting OpenSM: the per-port M_Key target is on, and
   OpenSM runs with them off: a start applies it", at most once an hour.
   - On a busy cloud with no quiet check, once a day has passed since the
     target changed, it applies it whatever the jobs, and logs "A day has
     passed since the per-port M_Key target changed, so it is applied
     although N jobs are open".
   - That start verifies every recorded port, writes OpenSM's `neighbors`
     and a `guid2mkey` entry with the stored `m_key` for every port that
     holds it into OpenSM's cache volume, records a fingerprint of the
     base (a hash, not the key), and starts OpenSM with `m_key_per_port`
     and the base. OpenSM then re-keys every port it reaches. The log
     shows "Starting OpenSM with per-port M_Keys on (target on); N ports
     are changing".
   - A restart you choose, or a crash restart, applies the target the same
     way.
   - In HA mode, a leader held by a freeze doesn't apply it until the
     freeze ends.

**To check on it**, run `--port-key-states`:

```sh
$AFM --port-key-states
```

Its output:
- The first line is the target and what OpenSM last started with, such as
  `target off, OpenSM last started off`.
- Then `failed turn-on recorded: yes (a change of configuration, or
  --retry-port-key-turn-on, clears it)` or `no`.
- Then `arming blocked by: ...`, or `-` when nothing blocks it.
- Each line after them is a recorded port: its GUID, `switch`, `hca` or
  `-` (not yet known), the key it holds (`m_key`, `derived` or `unknown`),
  and its flags (`lapsed`, `reachable`, `released`, or `-`).

It never shows a key, nor a failed turn-on's hash.

**When the start succeeds.** The start succeeds when OpenSM's cache, within
`[opensm] start_timeout`, shows this host's port and every recorded switch
that answered the verify holding its derived key.
- The log then shows "Per-port M_Keys are on: OpenSM gave this host's port
  and N switches their derived keys".
- While other ports are still changing, each fabric check logs "Per-port
  M_Keys turning on: N reachable ports still hold the stored M_Key", until
  "Per-port M_Keys are on on every reachable port".
- `--port-key-states` then shows `target on, OpenSM last started on`, and
  `derived` for every reachable port.

**When the start fails.** If the start fails, whether OpenSM's cache
doesn't show the keys in time or OpenSM doesn't start at all ("OpenSM
didn't start: ..."), the manager logs "The start that turns per-port
M_Keys on failed: ...", stops OpenSM, and runs the prober to find out
whether any port left the key it held. What happens next depends on what
the prober finds:

- **No port moved, and none answers its derived key:** the target is set
  back to off, OpenSM is started without per-port M_Keys, and the failure
  is recorded, with the base's fingerprint and a hash of the
  configuration (an HMAC keyed by a secret in the database, never shown):
  "... no port left the key it held, so the target is set back to off and
  OpenSM is started without them. The failure is recorded: nothing arms
  them again until the configuration changes, or
  --retry-port-key-turn-on is run once the cause is fixed". So a fabric
  that can't be turned on isn't re-keyed in a loop, across restarts and
  takeovers. Every start logs the record, with what to check. See After a
  failed turn-on.
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

**When the verify before a start fails.** If the verify before the first
turn-on start fails or runs out of time, OpenSM starts as it last ran,
without per-port M_Keys: "The verify before this start failed, so OpenSM
starts with per-port M_Keys off, as it last ran, rather than beginning to
turn them on; the next start tries again".
- The target stays on, so the manager applies it again, at most once an
  hour, with "(N starts in a row)" in that line, a warning the first time
  and an error after.
- On a turn-on, the third start in a row that falls back is recorded as a
  failed turn-on, with the target set back to off, which OpenSM still runs
  with: "Per-port M_Keys: 3 starts in a row couldn't verify the ports to
  turn them on, so the target is set back to off and the failure is
  recorded ...". See After a failed turn-on.
- A turn-off keeps trying hourly.
- But once OpenSM has started with them, before a start has met the
  success rule (for example after the manager restarts during a turn-on),
  the fabric may be part-way, and neither target is safe. A verify that
  fails then stops the start, "The verify before this start failed while
  per-port M_Keys are turning on, and the fabric may be part-way: OpenSM
  isn't started on either target until a verify succeeds", and it's
  retried. No subnet manager runs meanwhile, as above.

**After a failed turn-on.** The manager never retries a recorded failed
turn-on by itself: each retry restarts OpenSM on a fabric others may
share (specs/zero-touch.md, A failed turn-on). A change of configuration
clears the record, since it's keyed by the configuration's hash.
Otherwise:

1. **Find the cause** in the log of the start that failed: whether OpenSM
   gave this host's port and every switch its derived key within
   `[opensm] start_timeout`, and whether any port was down or held by
   another SM. Fix it.
2. **Agree the retry** with whoever shares the fabric: it re-keys every
   port.
3. **Clear the record:**

   ```sh
   $AFM --retry-port-key-turn-on
   ```

   It prints "Cleared the failed per-port M_Key turn-on. The manager arms
   it again at its next passing check once every port is ready, then
   restarts OpenSM to turn per-port M_Keys on, which re-keys every port,
   at a passing check with no open job (at most once an hour after its
   last attempt). --port-key-states shows what blocks arming." It changes
   nothing else, and runs on any node in HA mode. With no failed turn-on
   recorded, the group not named, or the target already on, it says so,
   and changes nothing.
4. **Watch the arming** with `--port-key-states`, as in step 3 of
   Turning them on, and the start as above.

**When OpenSM's cache can't be read.** Before every start, the manager
reads OpenSM's cache from its volume.
- If the files can't be read or parsed, it writes nothing, since it would
  replace them unseen, and the start fails and is retried: "Can't read
  OpenSM's cache before starting it (...), so it isn't replaced unseen:
  the start is retried".
- Files that are absent are taken as a lost volume, and rebuilt from the
  records.

So if OpenSM left a file truncated when it stopped, move that file out of
the cache volume, with OpenSM stopped, and the next start rebuilds it.

**When a start of OpenSM fails.** While the group is named or any port is
recorded, a start of OpenSM that fails doesn't end the manager: it logs
"Failed to start OpenSM, retrying at every check: ..." and the applier
starts OpenSM again at every `[opensm] check_interval`, as it does when
OpenSM stops. Read the reason in that line rather than waiting for the
manager to exit. A start refused by the configuration (such as the `m_key`
group removed while ports hold per-port keys) still ends it.

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
  ports recorded, `--release-port` for the dead leaf's ports, which
  takes every GUID in one run, or their jobs wait. A switch whose links are up but that doesn't answer stays,
  since it may still forward. The evidence is always the check's own
  verify;
- with ports recorded, any port recorded as a switch, expected or not,
  that doesn't answer while a recorded link to it is up fails the check:
  "switches 0x... don't answer, behind links that are up: they may still
  forward with stale partitions". So a spine dropped while its links were
  down that comes back hung holds jobs again. Once it answers the check
  passes; for one that won't, `--release-port <guid>`.

Nothing is added, dropped or recorded, and the counts start again, while
the check couldn't query the fabric (a verify that ran out of time counts
as that), saw another master, or OpenSM wasn't master. The counts also
start again whenever OpenSM starts.

The prober follows every port that answers as a switch, as OpenSM's own
sweeps do, but the check counts only the expected switches it finds, so a
new switch, or a tenant's adapter answering as one, never makes a
fragment of the fabric look complete. Such an adapter is sent only the
keys OpenSM itself would send it: with per-port M_Keys on, a switch is
sent the stored `m_key` only while its record says it holds it.

##### While a switch is missing

(specs/ha.md, While a switch is missing.) Grants that the missing switch
can't matter to still complete.

**A partial check.** A check that fails only because expected switches are
missing is *partial*: OpenSM is master, no SM contests it, it queried the
fabric in full, and nothing else fails it.

**How a partial check places ports.** It places the ports from its own
scan:
- with ports recorded, from the recorded links of the ports its verify
  reached, the prober verifying every port at each check while a switch
  is missing;
- otherwise from an `ibnetdiscover` scan run in the same check, read-only,
  with `-y <m_key>` as `ibswitches` has it, through the switches the check
  found.

**Virtual functions.** Running virtual functions are placed through
OpenSM's `/var/log/opensm-virtualization.dump`: a vport listed once,
ACTIVE, at a non-zero index of a placed physical port. A new VF has no
vport until its guest runs, which Nova holds until the port's job
completes. So the manager learns each hypervisor's physical ports from the
dump at passing checks (`--status` lists them). It places a VF that the
dump doesn't list at all when:
- every physical port learnt for its host is placed; and
- every adapter port behind the missing switches, as passing scans last
  found them (`ibnetdiscover` at most once every `confirm_checks` checks,
  or the recorded links), is known: a learnt physical port, a Neutron
  port's GUID, or the port of an HA node that runs no learnt VF.

A physical port seen for two hosts is learnt for none until two passing
checks in a row agree, and a learnt port is forgotten once three passing
scans in a row miss it.

**What completes on a partial check:**
- a network's creation, or an update that keeps its pkey;
- a port's create, or an update that keeps its GUID and network, if all
  of these hold:
  - the scans either side of its sweep placed its GUID in the same place,
    with no other OpenSM sweep start logged between them;
  - its pkey isn't *tainted*;
  - unless it's a virtual function (`binding:vnic_type` `direct`): its
    host isn't tainted and no revocation is open for another port on it.

A grant placed only after its sweep is swept again, once per place.
Deletions, pkey changes, and ports moving network or changing GUID wait
for the whole fabric.

**Tainted pkeys and hosts.** A pkey or host is tainted while some port may
still hold it stale. The manager records every membership it writes,
before writing it, and forgets those the partitions no longer have only
at a passing check after a sweep of the whole fabric of the latest
partitions. Until such a reset, as after an upgrade, or after another
version or another term swept partitions the record didn't see, no port's
grant completes on a partial check.

**Once the switches are back,** the manager sweeps again before any job
completes in full.

**A partial check is still a failed check:** it raises the alarm, which,
like `--status`, names the missing switches and the open jobs, tainted
pkeys and tainted hosts they hold, and counts towards stepping down.

**After `--confirm-fabric` drops a switch,** which may be a hung one still
forwarding, the taint stays until `--reset-held`, whose message names the
pkeys it untaints. Run it once the switch is unplugged or wiped, in the
same command as the confirmation:

```sh
$AFM --confirm-fabric --reset-held
```

or on its own later, on any node:

```sh
$AFM --reset-held
```

It's refused while the latest partitions haven't been swept: the
switches stay recorded, so run `--reset-held` on its own once the sweep
has finished. On its own, it's also refused while the last check found an
expected switch missing, and with no switch dropped since the last reset
it says "No confirmation has removed a switch since the last reset:
nothing to reset.", and changes nothing. A switch the manager drops
itself, with every recorded link down, can't forward, and doesn't keep
the taint.

##### `--confirm-fabric`

`--confirm-fabric` is optional now: a shortcut that records the switches
the fabric check sees at once, replacing the list, and the way to drop a
removed switch the manager can't. It does nothing else, whatever the
hardening groups, so it's safe to run on any deployment.

```sh
$AFM --confirm-fabric
```

Every run, it:
- records the switches, with ports recorded found by the prober, and
  prints them, one per line;
- then prints those it dropped from the list, if any: "Dropped from the
  expected switches: 0x...".

It never reads OpenSM's cache, which every fabric check folds, never arms
the per-port M_Key turn-on, which the manager arms at a passing check
(see Turning them on), and never clears a failed turn-on (see After a
failed turn-on). It exits 0 once the switches are recorded. If it can't
ask the fabric at all it records nothing: "can't confirm the fabric:
...".

#### Running with them

**The records.** Two tables hold what the manager knows about the fabric,
never a key:
- `fabric_ports` has, for each port GUID that OpenSM's `neighbors` lists,
  its node GUID, whether it's a switch (as its NodeInfo said), the key it
  holds (`m_key`, its derived key, or `unknown`), whether it lapsed (its
  protection was 0 at the last Get, so it answers any key), whether it's
  reachable, whether an operator released it, and when it was last seen.
- `fabric_links` has each link, both directions, as `neighbors` does.

How they're kept:
- At every fabric check the manager reads `neighbors` and `guid2mkey` from
  the running OpenSM's cache into them.
- A `neighbors` line replaces the link recorded for its port, so a
  re-cabled port's new link replaces its old one, and the old switch
  port's link back to it is dropped.
- A link recorded in one direction only is neither followed nor written to
  OpenSM's cache: otherwise the port would be reached through a switch
  port it has left, and whatever is cabled there next taken for it.
  `--add-link` records both directions.
- Virtual functions never get a record, and a recorded port that
  disappears from OpenSM's files keeps its record, since a port that's
  down may come back holding its key.
- Before every start, the manager writes OpenSM's cache from the records.

**The prober.** The manager Gets NodeInfo and PortInfo with `smpquery` by
directed route, from this host's port along the recorded links, in
OpenSM's container, and only named fields of the answers leave it.
- A fabric check reaches every switch, and counts the expected ones it
  finds.
- A full verify, of every recorded port behind a link that's up and every
  switch port's link state, runs every `[opensm] port_verify_interval`
  seconds (60 by default; it must be positive), and the fabric check uses
  its last result.
- Once any port can hold a derived key, the prober sends the stored
  `m_key` to a tenant's port only if its record says it holds it, and with
  per-port M_Keys on, never to one recorded `derived` or `unknown`, so
  that the old key isn't handed to tenants again.
- It tries the key a port's record names first, so that few Gets carry a
  wrong key: each such Get starts the port's lease countdown, if it has a
  lease, which the right key's Get stops.

Why a prober: the manager can no longer run `ibswitches -y <m_key>`, which
uses one key for the whole fabric.

**The prober's helper container.** Before a start, when no OpenSM runs,
the prober runs in a helper container from OpenSM's image, named after
`[opensm] container` with `-prober` (`opensm-prober` by default),
privileged and on the host network. Its configuration holds the Gets and
their keys while it runs, so `docker inspect` of it shows keys: never
inspect it or paste its configuration. It is removed after each run, and a
start or stop of OpenSM removes one left behind.

##### Diagnostics with per-port M_Keys

Each port answers only its own key, so a tool that walks the fabric with
one `-y`, such as `ibnetdiscover`, `iblinkinfo` or `ibswitches`, reaches
only the ports that hold that key. Use `--port-key-states` and the fabric
check's detail to see the fabric, and query one port at a time, with its
own key, taken into a variable and never displayed:

```sh
KEY=$($AFM --port-m-key <guid>) && smpquery -y $KEY nodeinfo <lid>
unset KEY
```

`--port-m-key` prints the key the port holds now, by its record: the
stored `m_key`, or its derived key.
- It refuses a GUID with no record, or one whose key is `unknown`.
- It prints the stored `m_key` while nothing is recorded.
- For a switch, give the switch's GUID, as `--port-key-states` lists it.

The key it prints opens that port, and a switch's opens that switch: keep
it out of shells' history, logs and screens.

##### Jobs

A job that binds or unbinds a recorded port GUID needs one more piece of
evidence after its sweep. At a fabric check after that sweep, either:
- the prober's Get to the port answers with its key; or
- the switch port it's attached to shows its link down (OpenSM then sets
  its pkey table when the link comes up).

A lapsed port, which answers any key, doesn't show that. Until then the
job waits, with the error "Waiting for port 0x... to answer its M_Key, or
show its link down, after the sweep: OpenSM may not reach it.
--release-port completes its jobs as they are", and the log shows "Port
0x... hasn't answered its M_Key, nor shown its link down, since the sweep,
so its jobs are held". Only that port's jobs wait: its own tenant can
cause this, and it holds no one else's.

If the port won't be reached again, for example on a node being retired,
release it. One run takes several GUIDs, released in turn; it stops at
the first it refuses, naming those it released:

```sh
$AFM --release-port <guid> ...
```

That completes its jobs as they are. It changes nothing on the port, nor
on its switch port, and lasts until the port next answers its key. A job
on a GUID with no record, a virtual function's or a port not yet seen,
completes as it did before.

##### Sweeps

OpenSM's own log is an extra reason to hold jobs, since a lost port
doesn't stop OpenSM logging SUBNET UP:
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

##### The fabric check's failures

Besides its usual checks, the fabric check fails, with these in its detail
(after "Fabric check failed:" in the log), and no job completes meanwhile:

| Detail | What it means, and what to do |
| --- | --- |
| "switch port 0x... port N has its link up with no recorded neighbour (record what's attached with --add-link)" | Something is attached that OpenSM hasn't recorded, which may be a port it can't reach. Find out what it is, then record the link: `--add-link <switch GUID>:<port> <peer GUID>:<port>` |
| "ports that aren't Neutron ports' didn't answer their M_Keys: 0x... (no job completes until they're reached, or released with --release-port)" | A recorded port behind a link that's up, not a switch's nor a Neutron port's (a hypervisor's, or any other host's), didn't answer its key, or has lapsed, in the last full verify. Its virtual functions have no records, so their jobs would otherwise complete without evidence. Find out why it isn't reached, and once it is, the check passes again. For a port that won't be reached, `--release-port <guid>` |
| "the last full verify of the ports' M_Keys is older than two port_verify_intervals" | Full verifies are failing or too slow: the log says why ("Can't verify every port's M_Key: ..."). A full verify must fit within one `port_verify_interval`: raise it on a large fabric |
| "Missing switches: ..." | An expected switch wasn't reached. A switch the prober shows gone, every recorded link to it down, is dropped by itself; a removed or replaced leaf switch isn't, so run `--confirm-fabric` once the hardware change is done (see The expected switches, and `--confirm-fabric`). A switch that's there but doesn't answer may be hung: it holds jobs, since OpenSM can't program it |
| "No expected switches yet: recording them once the fabric has been stable for N checks" | A new deployment: the manager records the switches after `[opensm] confirm_checks` checks in a row that saw them all. `--confirm-fabric` records them at once |
| "no full verify of the ports' M_Keys yet" | At a start, until the first full verify; if it stays, as above |
| "Can't query the fabric: this host's port answers neither the stored M_Key nor its derived key" | OpenSM can't sweep from this host. Restore the database that holds its key, or wait for its lease to lapse, if it has one |

**A tenant can hold every job of the fabric.** The rule on lost ports that
aren't Neutron ports' is fabric-wide, since AFM can't tell which physical
port a virtual function is on. So a bare metal tenant can make the fabric
check fail by leaving their node refusing its key and then releasing the
node, which makes its port's GUID no longer a Neutron port's. No job then
completes, anywhere on the fabric, until that port answers its key again
or an operator runs `--release-port <guid>` for it. The check's detail
names the port. This fails closed, by design: recognise it, check the
port, and release it.

##### Keys in OpenSM's log

Bad M_Key traps print the key that was sent, and at startup OpenSM prints
the base as its `m_key` option. So OpenSM's log, the OpenSM container's
output and the `opensm_logs` volume, is as secret as `opensm.conf`: keep it
from tenants and from log shipping.

The manager never logs a key, nor the prober's command lines: it masks
keys in the lines of OpenSM's that it shows, and drops lines about Bad
M_Key traps.

The prober's keys are on `smpquery`'s command line, visible in `/proc` on
this host, as `ibswitches -y` is without per-port M_Keys: this host must
have no tenants.

##### Leases with per-port M_Keys

Per-port M_Keys use the same lease, `[opensm] key_lease_period` (see
Permanent keys).
- With a lease of 0, a port that loses its key stays out of reach until
  it's reset.
- With a lease, a lapsed port gives away only its own key, but a port
  still on the stored `m_key`, during a turn-on or after a turn-off, gives
  away the shared key, as before.

#### Backups and restores

The database holds the base, and which key each port holds. Keep backups
taken after the turn-on: those are the ones that can reach the ports.

**A database and cache volume that don't belong together.** A start that
finds the database and OpenSM's cache volume don't belong together keeps
per-port M_Keys off, leaves the volume's files as they are, and starts
OpenSM without them, so that OpenSM reaches each port through its own
cache and gives it the stored `m_key`. It logs why: "The stored per-port
M_Key base isn't the one the ports were keyed from (its fingerprint
differs)", or "OpenSM's cache has M_Keys for N ports (...) that are neither
the stored M_Key nor derived from the stored base", followed by "Keeping
per-port M_Keys off, leaving OpenSM's cache as it is, and starting OpenSM
without them". That happens, for example, with a database restored from
before the base was stored.

Every start keeps taking that path while the fingerprint differs. To begin
again:

1. Wait until `--port-key-states` shows every port holding `m_key`.
2. Remove the group, and restart the manager.
3. Run `--forget-port-keys`, which also drops a fingerprint of another
   base:

   ```sh
   $AFM --forget-port-keys
   ```

4. Begin again, if you want per-port M_Keys.

**A database restored from before the turn-on.** A database restored from
after the base was stored but before the turn-on, the usual case, has the
target off while the ports hold keys derived from its base, and no
fingerprint, since that is recorded by the turn-on start.
- With the group named, the start adopts the target on rather than giving
  the ports the exposed `m_key` back, and logs "Ports hold keys derived
  from the stored base while the per-port M_Key target is off and no
  turn-on is recorded, as a database restored from before the turn-on
  would have: adopting the target on". An adopted target isn't held to the
  turn-on's success rule.
- Without the group named, it's an ordinary turn-off.

**A database that has seen a turn-on.** With a fingerprint recorded, the
database has seen a turn-on, so ports recorded as holding derived keys
while the target is off are what a turn-off or a set-back left, such as a
node that was down. If the group is named again, they're turned off like
any others: the start logs "Per-port M_Keys are off, and ports recorded as
holding derived keys are given the stored M_Key as OpenSM reaches them;
the manager's checks arm per-port keys again once every port is ready".
Once `--port-key-states` shows them holding `m_key`, the manager arms the
turn-on again, after checking the fabric.

**When ports are lost.** Ports are lost only if the database is older than
the base and OpenSM's cache volume is gone too: then nothing holds their
keys. Recovering them needs a lease lapse (see Permanent keys), or in-band
firmware tools (MFT) given each port's key, computed from a newer backup's
base.

#### Turning them off

1. **Agree it** with whoever shares the fabric. Turning per-port M_Keys
   off gives every port the stored `m_key` again, the old key, not the
   base: a tenant who read it has it again, which turning off can't
   avoid.

2. **Remove the group, keeping `m_key`, and restart the manager.** For
   example:

   ```ini
   [opensm]
   hardening = all
   ```

   The log shows "The m_key_per_port group isn't named, so this start
   turns per-port M_Keys off: OpenSM gives every port it reaches the
   stored M_Key". The records stay: the start writes `neighbors` and an
   entry with its derived key for every port that holds one, and OpenSM,
   without the option, re-keys each port to the stored `m_key`.

3. **Wait for every port.** Each fabric check logs "Per-port M_Keys turning
   off: N reachable ports still hold their derived keys". Wait until
   `--port-key-states` shows every port, HCAs included, holding `m_key`. A
   port that's down keeps its derived key and its record until it comes
   back.

   ```sh
   $AFM --port-key-states
   ```

4. **Drop the records** with `--forget-port-keys`. It refuses while the
   group is named or any port holds its derived key, but not for a port
   whose key is `unknown`: check step 3 first. The base, and its
   fingerprint, stay.

   ```sh
   $AFM --forget-port-keys
   ```

**Keep the managed driver until then:** with another driver the records
remain but nothing keeps them ("Per-port M_Key rows exist, but the ...
driver can't keep them").

**Removed hardware.** For hardware that has been removed,
`--forget-port <guid>` drops one port and its links; it refuses a port
seen in the last day.

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

#### Why per-port M_Keys

**The shared M_Key is only as secret as the least protected port.** With
the `m_key` group, every port on the fabric holds the same M_Key. Anyone
who learns it from any one port can send SubnSet to every switch: rewrite
pkey tables, which breaks every tenant's isolation, move LIDs, or take
ports down. A bare metal tenant has root on a host with a port on the
fabric, and there are several ways for that port's key to leave it:
- the host's own adapter may let root read it;
- a port whose lease lapses gives it to anyone who asks (this has happened
  on a test fabric; see Permanent keys);
- an adapter with a non-volatile M_Key keeps it wherever the card goes.

**Two limits remain** with per-port M_Keys:
- A tenant who knows their own port's GUID and key can search for the base
  offline, at about 2^63 SHA-1s.
- A tenant who can make their adapter present another port's GUID is given
  that port's key, which only OpenSM's handling of duplicate GUIDs stands
  against.

**The base isn't the `m_key` that every port holds now.** That is exactly
the key tenants may have read, and since a GUID isn't secret, deriving from
it would hand such a tenant every port's key. So the manager keeps a
separate key, `m_key_base` (see The keys), and OpenSM's `m_key` line is the
base only while per-port M_Keys are on.

### In HA mode

**Experimental, untested on hardware in HA mode.** The four groups run in
HA mode as in single node, with these differences (specs/secured-profile.md,
HA mode). Roll every change out with docs/kayobe.md's
[Change AFM's configuration](kayobe.md#change-afms-configuration).

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
- **`m_key_per_port`:**
  - The leader keeps the records, and the expected switches, behind the
    leader fence. A takeover renders OpenSM's cache from them, never from
    its host's cache volume, which an older term left.
  - Each node's reach probe runs the prober, in its `-reach` container,
    rather than `ibswitches -y` with one key, and counts only the expected
    switches it finds.
  - The leader arms the turn-on only while the configuration epoch allows
    exactly its own configuration's hash, so that leaders with and without
    the group can't alternate, re-keying the fabric each time: roll the
    configuration to every node first.
  - To apply the target, at most once an hour, the leader steps down and
    releases the lease, "Stepping down: the per-port M_Key target is on,
    ...", with no other candidate needed; any eligible node, itself
    included, takes over, and the takeover's start applies it.
  - A failed turn-on's record is in the database, so it holds across
    takeovers.
  - The takeover's bound grows by `4 × start_timeout`, and `job_timeout`
    must exceed a takeover with a per-port start, 1.5 × `start_timeout`
    longer: with docs/kayobe.md's settings, 327 s rather than 237 s, which
    its `job_timeout = 360` covers, below Nova's `vif_plugging_timeout` of
    420 s. `job_timeout` is a cluster setting: change it on every node, as
    for an epoch, and it moves with the epoch once every live node has it.
  - A job on a recorded port completes only from the leader's checks.
- **`class_keys`, `sa_rate`.** As in single node; every leader reads the
  same seed.

#### Before enabling in HA mode

On a fabric you control, with backups of the database and a non-zero
`key_lease_period`, first run each group's own checklist on a single node.
Then, on two hosts, run these tests. `$AFM` here is the HA form (see
[Commands in this document](#commands-in-this-document)).

1. **Configuration epochs.** `--config-epoch` on each node shows one hash,
   allowed:

   ```sh
   $AFM --config-epoch
   ```

   Change one node's configuration: it's listed NOT allowed, logs the
   alarm, and never takes the lease. Restore it.

2. **With `allowed_sm`:** every node row lists its ports
   (`--known-sm-ports`), `opensm.conf`'s `allowed_sm_guids` on the leader
   holds both hosts' ports, and a takeover by the other host starts OpenSM
   as master with no `ERR 2F1C` or `0F09` for the old leader's port. Then
   stop the old leader's manager uncleanly (`docker kill`) and check that
   the new leader takes over within a takeover's bound.

3. **With `class_keys`:** after a takeover by the other host, a VS key Get
   with the derived key still answers on a port keyed by the first host
   (hardware test 3's second-host leg), and no `ERR 9828` appears.

4. **With `m_key_per_port`:** name it on every node. Check that the leader
   arms it (`--port-key-states` shows `target on`), steps down to apply it,
   and that the takeover's start turns it on. Then move the leadership
   (`docker stop` the leader): the new leader's log shows the cache
   rendered from the records, SUBNET UP, and no Bad M_Key traps from
   ports; `--port-key-states` lists no port lost. Each candidate's node
   row shows a non-zero reach. Then turn it off the same way.

5. **After each:** the fabric check passes on the leader ("The fabric is
   complete"), and a port bound to a network goes ACTIVE.

## Turning hardening on for a running fabric

OpenSM reads `opensm.conf` only when it starts, so the manager must be
restarted, which restarts OpenSM (see
[Changing the hardening](#changing-the-hardening)). OpenSM then sets the
M_Key on every port at its first sweep. Hosts that run diagnostic tools
need the M_Key from then on.

`m_key_per_port` is the exception: naming it and restarting doesn't change
any port's key at once. Per-port M_Keys begin at the start the manager
makes once its checks have armed them (see Per-port M_Keys).

## Options the manager keeps

Whatever `hardening` says:
- `[opensm_conf]` can't set `partition_config_file`, `daemon`, `log_file`
  or `no_partition_enforcement`;
- `part_enforce` must stay `both`;
- `log_flags` must keep the levels `0x07`, whose messages the manager
  watches for.

With `m_key_per_port` named, `[opensm_conf]` can't set `m_key`,
`m_key_per_port`, `m_key_protection_level` or `m_key_lookup` either, and
without it can't set `m_key_per_port` (see Per-port M_Keys).

`[opensm_conf] allowed_sm_guids` is checked like the list the `allowed_sm`
group writes (see Allowed subnet managers).

While a class key is recorded enabled, a start refuses removing
`class_keys` or a `<class>_key_enable` of 0 (see Turning class keys off).

OpenSM ignores an option it doesn't know, so check an override's spelling
in `opensm -c` output.
