# High availability

Status: accepted for implementation, 2026-11-05.

## Overview

**What this covers.** This is the design for running AFM highly
available. A fabric manager instance runs on every OpenSM node, behind
HAProxy, with the shared state in the deployment's Galera cluster. Exactly
one instance, the **leader**, runs OpenSM at a time. The others serve the
API and stand by to take over. Leadership is a lease in Galera: Galera's
quorum is the only consensus, and there is no Raft.

**Who it's for.** Developers changing anything under
`alternative_fabric_manager/ha/`, and reviewers checking such changes. It
is the signed-off design and the source of truth for HA behaviour: change
this spec first, then the code, then the tests. Operators deploying HA mode
want [docs/kayobe.md](../docs/kayobe.md) instead.

**Why one OpenSM.** Running one OpenSM, rather than OpenSM's own master and
standbys on every node, has two benefits:

- no standby can take over with partitions that nobody applied;
- a job's evidence is one OpenSM's sweep.

The cost is that a takeover starts an OpenSM rather than promoting a running
one. Until it has swept, there is no subnet administrator (SA), so new
connections, IPoIB joins and ports coming up wait, and its multicast groups
are rebuilt.

### The invariants

Three principles carry the safety argument:

- **A job completes only on evidence that the fabric has its effect.** The
  current leader's OpenSM swept a body in which the job's effect holds, in
  the leader's current term, while the leader saw:
  - the whole fabric, or, for a grant while expected switches are missing,
    every port it grants to, on pkeys no port may hold stale (see While a
    switch is missing); and
  - no other subnet manager that could master or set ports.
- **A new leader starts OpenSM only once every OpenSM that may have run
  since the last verified stop is stopped.** Each such OpenSM counts as
  stopped once one of these holds:
  - it was released cleanly;
  - its own node verified it stopped;
  - or, unconfirmed: it was asked to stop, if `fence_ask` is on, and
    `T_watchdog` has passed since the lease expired, by when its self-stop
    or watchdog has killed it if either can run.

  A paused node is the known exception, until confirmed fencing (see
  Fencing).
- **When in doubt, wait:**
  - only the majority side of a Galera partition can elect a leader;
  - a leader that can't renew stops its OpenSM;
  - no job completes while the fabric check sees another master.

### Map of this spec

| Section | What it says |
| --- | --- |
| Glossary | The spec's terms, in short |
| Architecture | The nodes, HAProxy, Galera, and what runs where |
| Shared state | The tables in Galera, how a request is recorded, and each job's effect |
| The lease | Who leads: the term, renewal, self-stop, expiry, the target set, taking and releasing the lease |
| Requests and applying | How the leader applies the recorded state; late requests, quarantine and OpenSM's errors |
| Job completion | When a job completes or fails, the fabric checks, and the grants that complete while a switch is missing |
| Fencing | How a new leader knows every other OpenSM has stopped; rejoining, retiring a node, and the required keys |
| The watchdog | OpenSM's container's entrypoint, the freeze, and when a leader steps down |
| Timeouts | The timing inequalities, and the defaults that meet them |
| Configuration | The checks on rendered files, configuration epochs, and the secured profile in HA mode |
| Galera specifics | Writes, retries, causal reads, UTC and registration |
| Upgrades | Rolling upgrades, and moving from single node |
| Failure scenarios | What each failure does, and its outcome |
| Alternatives considered | Designs not chosen, and why |
| Implementation plan | What is built and what isn't |
| Open questions | What still needs answering, mostly on hardware |
| Future improvements | Podman, confirmed fencing, and other coordination drivers |

## Glossary

These are short reminders. The section named in each row holds the exact
rule.

| Term | Meaning | See |
| --- | --- | --- |
| **Leader** | The one instance that holds the lease, renders the partitions file and runs OpenSM | The lease |
| **Candidate** | An instance that doesn't lead, and may take the lease | The lease |
| **Live** | A candidate whose heartbeat is current and that isn't in the target set. A live row is one not marked stopped whose heartbeat moved within `T` | Stepping down, Configuration epochs |
| **Lease** | The row in Galera that names the leader. The leader renews it by incrementing its renewal counter | The lease |
| **Term** | A counter in the lease that only goes up. Every leader write to Galera is a compare-and-set on it | The lease |
| **`t0`** | When the leader sent its last renewal that succeeded (was acknowledged) | The lease, Why the unconfirmed wait is enough |
| **Primary component** | The majority side of a Galera partition: `wsrep_cluster_status = 'Primary'` and `wsrep_ready = ON`. A lease read or write counts only on a node in one | The lease |
| **Self-stop** | A leader with no successful renewal for `T_self` stops its own OpenSM through the watchdog, and verifies that none runs | The lease, Stepping down |
| **Expiry** | Seen by a candidate once successful reads spanning `T` all show the same renewal counter, or once the lease was released | The lease |
| **Target set** | An entry (node, instance UUID, term added) for every instance that may have run OpenSM since the last stop that was released or verified. A new leader starts OpenSM only once no entry but its own is left | The lease, Fencing |
| **Verified stop** | A node's manager stopped OpenSM through its watchdog, verified through the host's process table that no OpenSM runs, and recorded that in its node row | Fencing, Rejoining |
| **Unconfirmed** (takeover, target) | A target leaves the target set unconfirmed once the new leader has asked it to stop, if `fence_ask` is on, and `T_watchdog` has passed since the lease expired or since the successor took a released lease. An **unconfirmed takeover** is one whose targets leave that way: no step confirms them, so it relies on the old leader fencing itself, and on the Ask | Fencing, Unconfirmed takeovers, with the ladder |
| **Ask** | Fencing's step 1: SubnSet(SMInfo) with the DISABLE modifier, sent to the target's SM. Behind `[ha] fence_ask`. Never counts on its own. Not sent yet (see Fencing, Known limitations) | Fencing |
| **Held** | While frozen, the leader *holds*: it stops timing self-stop and gives its watchdog a **held lease**. The frozen leader's target entry is marked **held**, and leaves only as verified, or with an operator's confirmation | Freeze |
| **Freeze** | An operator's request, acknowledged by the leader, that keeps OpenSM on the current leader's node until an unfreeze | Freeze |
| **Watchdog** | The entrypoint of OpenSM's container. It starts OpenSM only on a start request, and kills it when its own lease, renewed by the manager's liveness thread, lapses | The watchdog, The watchdog's lease |
| **Step down** | A leader stops OpenSM through the watchdog, verifies through the host's process table that no OpenSM runs, and releases the lease | Stepping down |
| **Discretionary trigger** | A step-down trigger that the cooldown can delay: a configuration no longer allowed, missing switches, failing applies | Stepping down |
| **Generation** | A counter bumped by every request. A job's generation is the first state that includes it | Shared state |
| **Effect** | What a job needs to see in a swept body, stated relative to its own object | Effects |
| **Body** | The rendered contents of the partitions file | Requests and applying |
| **Sweep** | Any pass by OpenSM over the fabric that applies the partitions file. The ones that count are the one after the leader's SIGHUP, and OpenSM's first after it starts, a takeover's included. OpenSM also sweeps by itself, unwatched | Requests and applying, Which sweeps count |
| **`applied`** | The table in which the leader records what it swept, and its last fabric check | Shared state, Job completion |
| **Fabric check** | The check, after every sweep and every `check_interval`, that OpenSM saw every expected switch and no other subnet manager that matters | Job completion, The fabric checks |
| **Contested** check | A check that saw another master, or the leader's OpenSM not master | Job completion |
| **Expected switches** | The switch GUIDs a complete fabric has | Job completion |
| **Partial check** | A check that fails only because expected switches are missing | A partial check |
| **Placed** | A port a scan reached through switches it found. A vport is placed by the dump read with the scan, and a virtual function with no vport by its host's learnt physical ports | Where a port is |
| **Grant**, **revocation** | A grant only adds memberships, or changes none. A revocation removes or moves one | A grant completes on a partial check |
| **Tainted** pkey | Some port may still hold a membership in it that the latest body removed, or moved from full to limited | Possibly held memberships |
| **Tainted** host | A port left it (by deletion, migration or rebinding), and the map still holds that port's entry | Possibly held memberships |
| **Quarantine** | Objects left out of rendering because they broke a render or OpenSM's parse | Quarantine |
| **Deferred** | A request whose pkey or GUID another object still holds, or a port on a network that waits or is quarantined | Shared state |
| **Tombstone** | What a deletion leaves, for 7 days, so that a late request can be refused | Late requests |
| **Configuration hash**, **epoch** | An HMAC of the settings that decide the partitions and the fabric's protection. The epoch is a number, and the one or two hashes whose instances may be candidates | Configuration epochs |
| **Leader fence** | The highest term whose leader has written the secured profile's records | The secured profile |
| **Cluster settings** | The `[ha]` settings stored in Galera, which every instance must share | Timeouts |

The timing symbols. All but `B` and `B_takeover` are cluster settings:

| Symbol | Option | Meaning |
| --- | --- | --- |
| `T` | `[ha] lease_timeout` | How long a lease goes unrenewed before candidates take it |
| `T_self` | `[ha] self_stop_timeout` | How long a leader goes without renewing before it stops OpenSM; shorter than `T` |
| `T_watchdog` | `[ha] watchdog_timeout` | The watchdog's lease, and the unconfirmed wait |
| `T_reach` | `[ha] reach_timeout` | How long candidates wait for one that reaches every switch |
| The period | `[ha] ha_period` | How often leases are renewed and read |
| The call timeout | `[ha] call_timeout` | Every database call's timeout |
| `B`, `B_takeover` | Derived from other settings, not an option (see The watchdog's lease) | The apply loop's and the takeover loop's bounds |

## Architecture

```
                 Neutron server (mlnx_sdn_assist)
                               |
                 HAProxy: all three backends active
           ____________________|____________________
          |                    |                    |
 +--------v-------+   +--------v-------+   +--------v-------+
 | node 1         |   | node 2         |   | node 3         |
 |   manager      |   |   manager      |   |   manager      |
 |  (leader)      |   |                |   |                |
 |     | file +   |   |                |   |                |
 |     | SIGHUP   |   |                |   |                |
 |   watchdog     |   |                |   |                |
 |   OpenSM       |   |  no OpenSM     |   |  no OpenSM     |
 +--------^-------+   +--------^-------+   +--------^-------+
          |____________________|____________________|
                               |
       Galera: networks, ports, jobs, generations, the lease, nodes
```

- **Every instance serves the API.** HAProxy sends each request to any of
  the three instances, which are all active backends, round robin with no
  session stickiness. Each instance records requests in Galera and answers
  polls from it.
- **Only the leader touches OpenSM.** It renders the partitions file and
  runs OpenSM.
- **No other managed subnet manager may run on the fabric**, nor one
  embedded in a switch. The leader checks after every sweep that no other
  SM could master or set ports (see Job completion).
- **Control traffic stays off the fabric.** The cluster's own control
  traffic, Galera and the management network, must not run over the
  InfiniBand fabric that OpenSM manages. Otherwise, losing the subnet
  manager would cut the managers off from Galera too, and every leader
  would stop itself. The deployment role checks this.

## Shared state

| Table | Holds |
| --- | --- |
| `networks` | Network ID, type, pkey (unique, NULL for flat networks), name, the highest revision it was set at (see Late requests) |
| `ports` | Port ID, network (foreign key), GUID (unique), host, the highest revision it was set at |
| `tombstones` | Deleted objects' kind, ID, the highest revision they were set at and were deleted at, and the deletion time, for 7 days (see Late requests) |
| `state` | The `generation` counter; the schema version; the configuration epoch, the configuration hashes it allows, and the cluster settings (see Configuration); the leader fence, the highest term whose leader has written the secured profile's records (see The secured profile) |
| `jobs` | Operation, object, the GUIDs and pkeys it involves, generation, effect, deadline, status, error, timestamps (UTC) |
| `lease` | One row: the leader's node and instance UUID, its term, its renewal counter, whether it was released, the **target set** (see The lease), when leadership last moved, and the **freeze**: whether it is frozen, the frozen leader's node, instance UUID and term, who froze it, when, and when it expires (see Freeze) |
| `nodes` | Per node: name, instance UUID, heartbeat counter, configuration hash, the number of expected switches it reaches, its adapters' node GUIDs, its physical functions' port GUIDs (see The secured profile), and whether its OpenSM is verified stopped (see Fencing, Rejoining) |
| `applied` | The leader's last swept generation, its body, which object each partition and membership in it comes from, the GUIDs whose pkey tables OpenSM couldn't set, the switch ports whose enforcement OpenSM cleared, the leader's term, the last fabric check's result and time, whether it was partial and the open grants' GUIDs it placed, and the memberships some port may hold, whether that map is complete, and the generation of its latest body (see While a switch is missing) |
| `known_sm_ports` | Port GUIDs whose SMs may hold the fabric's keys: every port GUID a node row has recorded, kept when the node is forgotten, and those an operator recorded |
| `fabric_ports`, `fabric_links` | Per-port M_Keys' records: each port's key state, never a key, and the links OpenSM's cache listed (see specs/secured-profile.md, Per-port M_Keys) |
| `quarantine` | Objects left out of rendering, and why |
| `expected_switches` | The switch GUIDs a complete fabric has (see Job completion) |
| `deferred` | A request whose pkey or GUID another object still holds, or a port on a network that waits or is quarantined, its values, revision and job |
| `host_ports` | The physical ports each hypervisor's virtual functions were seen on, learnt from OpenSM's virtualization dump (see While a switch is missing) |

### Requests that wait

networking-mlnx orders requests per object only. So a network reusing a
deleted network's VLAN, or a port taking over a deleted port's GUID, can
arrive before the deletion.

Such a request is not refused, which networking-mlnx would take as final.
Instead:

- its job is created at once;
- the request is applied when the deletion arrives, or its job fails at its
  deadline;
- a later request for the same object supersedes it.

A port on a quarantined network waits in the same way, until the network's
next request releases it.

**The cost.** A genuine clash, such as one VLAN on two physical networks,
therefore fails at the deadline rather than at once, and networking-mlnx
retries it until an operator resolves it.

### A request's transaction

Each request runs in one transaction, in this order:

1. **Bump the generation**, with
   `UPDATE state SET generation = generation + 1`, which locks the row.
2. **Read and validate**, only then, with locking reads, so that its checks
   see every change committed before it. It validates against the
   configuration stored in Galera, such as the pkeys that the template's
   static partitions reserve and `flat_pkey`, not the instance's own, so
   that every instance accepts the same requests.
3. **Record the change and create its job**, with that generation, its
   effect, and its deadline: its creation time plus the job timeout, so
   that every instance agrees on when it times out.

Every write goes to one Galera node (see Galera specifics). So requests are
serialised on the generation row, and a job's generation is exactly the
first state that includes it.

**A DELETE of a network or port that isn't recorded** still bumps the
generation and creates a job. Its identity is taken from the request's
body, which networking-mlnx sends on DELETE as on create.

**A job records the GUIDs and pkeys it involves when the request arrives**,
since a network's deletion removes its ports from the tables before the job
is checked.

### Effects

A job's **effect** is stated relative to its own object, as the leader's
render attributes each partition and each explicit membership to the
network or port it comes from. Keyword members, such as `ALL=limited`, are
never counted.

| Request | Effect, in the swept body |
| --- | --- |
| Create or update a VLAN network | A partition attributed to the network, with its current pkey |
| Create or update a flat network | Nothing beyond the swept generation: flat networks share the flat partition |
| Delete a network, VLAN or flat | No partition, and no membership, attributed to the network or its ports |
| Create or update a port | A full membership of its GUID attributed to the port, in its network's partition or the flat partition, and no other membership attributed to it |
| Delete a port | No membership attributed to the port |

Every effect also needs the swept generation to be at least the job's.

**Consequences of the relative rule:**

- A delete completes once a sweep of a state at or after it has nothing
  from its object, whatever else holds the same pkey or GUID by then.
- A port created at G and deleted at G+1 never has a membership attributed
  to it in a swept body. So its create times out and fails, while its
  delete completes.

## The lease

The lease row names the leader and its **term**, a counter that only goes
up.

**Every leader write is a compare-and-set on its term.** That covers every
write a leader makes to Galera, such as a record of what it swept or which
targets left the set. So a deposed leader's late writes are refused.

**The term fences only Galera.** A running OpenSM sweeps without asking
anyone. So OpenSM itself is fenced by the target set, self-stop and the
waits in Fencing.

### Renewal

- **What it does.** The leader increments the lease's renewal counter every
  few seconds, matching its instance UUID and term.
- **Where it runs.** On a thread of its own, which runs through applies and
  takeovers alike.
- **Only while the loops make progress.** It renews only under the same
  progress condition as the watchdog's liveness thread (see The watchdog).
  If a loop hangs, the Galera lease lapses with the watchdog's, so a
  candidate takes over, rather than the fabric being left without a subnet
  manager under a lease that is still renewed. The renewal thread also asks
  the watchdog to stop OpenSM.
- **A renewal that matches no row** means it lost the lease: it stops
  OpenSM at once (see Stepping down).

### Only a Primary component counts

Every lease read and write runs in a transaction that first checks, on the
node serving it:

- `wsrep_cluster_status = 'Primary'`, and
- `wsrep_ready = ON`.

It counts as failed otherwise. So only instances that reach the majority
side of a Galera partition can renew, watch or take the lease: the majority
elects a leader, and the minority can't.

### Self-stop

The leader times its renewals on its own monotonic clock, from when each
was sent. If none has succeeded for `T_self`, shorter than `T`, it:

1. stops OpenSM itself through the watchdog, which needs no database;
2. verifies locally that no OpenSM runs (see Stepping down).

**Why this is self-fencing.** A leader in a minority partition, or cut off
from Galera, so stops its OpenSM before any candidate on the majority side
can see the lease expire, since that takes successful reads spanning `T`
after its last renewal.

### Expiry

Every other instance watches the renewal counter on its own monotonic
clock. The lease has expired once:

- successful reads spanning `T` all show the same counter, or
- it was released.

**A failed read resets the timer.** So a database outage, which stops
renewals and reads alike, never makes a lease look expired when the
database returns. Nothing compares the database's clock with any other.

**Safety doesn't rest on these reads, though.** A read from a lagging node
could show a stale counter, and so a false expiry. But the takeover's
compare-and-set includes the counter it saw, so it matches no row, or
Galera's certification aborts it. Lease reads wait causally, as every
session's statements do (see Galera specifics), so false expiries are rare.

### The target set

The target set holds an entry for every instance that may have run OpenSM
since the last stop that was released or verified. Each entry has:

- its node;
- its instance UUID;
- the term in which it was added.

A takeover never removes an entry, except with a term compare-and-set that
records one of these:

- that the instance released the lease;
- that its node verified its stop after the entry was added;
- that it left unconfirmed (see Fencing).

So a takeover whose winner dies midway leaves its targets, and itself, for
the next.

### Taking the lease

**1. Check its own link.** A candidate first checks that its own fabric
port:

- has its physical link up;
- has its logical state Init or above. Active is not required, since only
  a subnet manager makes a port Active, which would deadlock a cold start;
- has its neighbouring switch port not disabled.

**2. Count and publish its reach.** It counts the expected switches it can
reach with directed-route SMPs, and publishes the count in its node row
every few seconds.

**3. Wait for one that reaches every switch.** Only a candidate that
reaches every expected switch may take the lease, or, once no candidate has
for `T_reach`, the one that reaches most.

**4. Take it, in one compare-and-set.** The compare-and-set includes the
counter it saw unchanged, and in the same statement:

- sets itself as leader, with the term incremented;
- adds an entry for itself to the target set;
- clears its node's verified-stop mark;
- removes older entries for its own node, with other instance UUIDs, but
  only where its node's verified-stop term, read in that compare-and-set,
  is at least the entry's term. So an entry added after the verification,
  such as by a takeover whose outcome was unknown, stays.

All of this is in the same compare-and-set, so that no earlier mark can let
it leave the set later. Only the winner proceeds.

**5. Wait for the other targets.** It waits for every other entry to leave
the target set (see Fencing). Only once none is left but its own does it
start OpenSM.

**6. Check the renewal before starting OpenSM.** Before it writes
the start request, it checks that its last renewal succeeded within
`T_self`. The start request carries the deadline `t0` + `T_self`, past
which the watchdog refuses it. So a winner that stalls after its wait can't
start OpenSM once it may have lost the lease.

### Releasing the lease

A leader that stops cleanly steps down. It:

1. stops OpenSM;
2. verifies through the host's process table that no OpenSM runs;
3. removes its own entry from the target set and marks the lease released,
   with a term compare-and-set.

A successor can take it at once, and starts OpenSM at once if no other
entry is left.

**A release removes only the releaser's entry.** If others remain, for
example because the releaser was itself waiting on an unconfirmed target,
the successor treats the release as the expiry. So it asks them again and
waits `T_watchdog` from when it took the lease.

#### On SIGTERM

This happens as from `docker stop` or a host shutdown.

- **It fails `/healthz` at once.** The manager fails `/healthz` at once,
  so HAProxy stops sending it requests. It steps down without waiting
  for that, and exits. It serves requests until it exits.
- **The step-down has a bound**, `[ha] shutdown_timeout`, 20 s by default.
- **The container's stop timeout is above it**, 30 s, set when the
  container is created, as `--stop-timeout` or StopTimeout. dockerd stops
  containers at a host shutdown with the timeout set when they were
  created, 10 s by default, not with `docker stop -t`.
- **If the database can't be reached within the bound**, the manager still
  stops OpenSM and verifies it, but exits without releasing the lease. A
  successor then waits for the lease to expire and the unconfirmed wait.
  That is safe, since its OpenSM is verified stopped.
- **If the manager is killed anyway**, OpenSM dies with it. OpenSM's
  container joins the manager's PID namespace, whose init,
  `docker run --init`, exits with the manager and takes the namespace with
  it.

#### A candidate that stops

A candidate that stops cleanly marks its node row as stopped, so that it
isn't counted as a live candidate.

#### After a self-stop

Once the leader reaches a Primary component again, it releases the lease
the same way, if it still holds it, and removes itself from the target set,
since it verified its OpenSM stopped. If a successor has taken the lease
meanwhile, it is a candidate like any other.

### A takeover whose outcome is unknown

A takeover whose own commit outcome is unknown counts as taken. The
instance adds itself to the target set before it can know, so a successor
waits for it as for any target.

### Who may be a candidate

- A candidate's configuration hash must be one the current configuration
  epoch allows.
- Its OpenSM must be verified stopped.
- While `allowed_sm` is in effect it also needs port GUIDs in its node row
  (see The secured profile).

### The coordination driver interface

The lease's exclusion sits behind a small coordination driver interface:
acquire it, renew it, release it, and watch it for expiry.

**What every driver must guarantee:**

- no other instance can acquire the lease, or see it expire, before
  `t0` + `T`, where:
  - `t0` is when the holder sent its last renewal that was acknowledged;
  - expiry is timed from when the first read showing that renewal
    returned, not from when it was sent;
- only a quorum of the driver's store can grant or renew it.

**Self-stop is timed on the holder's monotonic clock from `t0`**, whatever
the driver.

**What stays in Galera whatever the driver:** the term, the target set,
and the outcome-unknown rule. A takeover:

1. first acquires the lease from the driver;
2. then, in one Galera compare-and-set conditioned on the term it read
   before acquiring, bumps the term and adds its target-set entry, before
   it does anything else. So the term never repeats, and Galera refuses a
   lower term's writes.

An acquirer that hasn't done so by `t0` + `T_self` of its own acquisition
abandons the attempt, since its lease may have lapsed.

Galera is the first driver, since every Kolla deployment has it. Others are
a future improvement (see Coordination drivers). Jobs, objects and the
applied state stay in Galera too.

## Requests and applying

1. **Any instance records the request.** HAProxy sends networking-mlnx's
   request to any instance. The instance records it whenever Galera is up,
   leader or not, and answers with the job's path.
   - **A job's status is always answered, never with 503**, since
     networking-mlnx answers anything but a timeout on a job poll by
     sending its request again.
   - **The exception is a Galera outage**, when every `/healthz` fails and
     HAProxy itself answers 503. networking-mlnx then sends its requests
     again, which, with its default unlimited `retry_count`, only delays
     them.
   - **While the cluster is frozen**, new requests are answered with 503
     too, but job polls still get their status (see Freeze).
2. **The leader notices.** It polls the generation, every second or so, and
   is woken at once when it took the request itself.
3. **The leader applies.** When it is behind, it:
   1. reads the generation and the whole state in one consistent-read
      transaction;
   2. renders the partitions file without the quarantined objects;
   3. checks it (see Configuration);
   4. writes it atomically and fsyncs it;
   5. sends OpenSM SIGHUP;
   6. waits for the sweep that starts after the signal to end (see OpenSM
      errors).
4. **The leader checks and records.** After the sweep, it checks the fabric
   (see Job completion). Then it records, in `applied`, with a
   compare-and-set on its term:
   - the generation;
   - the body;
   - its attribution;
   - the GUIDs with pkey errors.

**Applying is level-triggered.** The leader always renders the newest
state, and never replays changes. The file's header carries the generation
and an attempt counter, so that a retry still changes the file and OpenSM
runs a pkey sweep.

**The leader reconciles every `check_interval`.** If its file isn't what it
last applied, or the state rendered at that generation differs, it applies
again.

### Late requests

**The problem.** With more than one instance, a request can run after a
later one for the same object:

1. An instance that is paused, as a container or a host can be, holds the
   requests the kernel had already accepted for it, for as long as it is
   paused. HAProxy, which keeps routing to it for about 10 s, bounds
   nothing once a request is in its socket.
2. networking-mlnx times out, after 10 s by default, and sends the same
   request again through HAProxy to another instance, and then the
   object's next requests.
3. The paused instance runs the first one when it resumes, whatever
   happened since.

A port's create or update that runs after the port's deletion would put a
deleted port's GUID back in a tenant's partition. networking-mlnx has no
full sync that would remove it.

One instance can't do this: networking-mlnx sends an object's requests one
at a time, and a resent one queues behind the first.

**The check.** So every request is checked against what the database holds
for its object. It uses the `revision_number` that networking-mlnx sends in
each object's dict, deletions included, as Neutron has it. The check runs
in the request's own transaction, after the generation bump, which
serialises requests, so that a late request queued behind a deletion sees
its tombstone. A refusal rolls the transaction back.

#### What networking-mlnx sends

Neutron's revision for an object only ever rises, in each transaction that
changes it.

| Request | Revision it carries |
| --- | --- |
| A port's create, on binding | The revision before the binding's bump |
| A port's update | The revision after its own |
| An update that changed nothing | The same revision as the last |
| A host move, as in a live migration | The port's deletion and its create, in one transaction, at the same revision, above any earlier one |
| A deletion of the port, rather than an unbind | A revision read before the deletion's transaction, which may be below one already sent |
| A resent request | The same revision as the first |

A network is never created again under the same ID. A port is created
again under its ID when it is bound again or moved.

#### Live objects and deferred requests

Live objects, and deferred requests, record the highest revision they were
set at.

- **A create or update below it is refused with a 400**: no sender is
  waiting for it, and it can never succeed.
- **An equal one is a resend:**
  - if the object is live, it is applied again;
  - if its request is deferred and its job still open, it is answered with
    that request's job, without superseding it, so that the job
    networking-mlnx polls still ends as it should.
- **A deferred request records its revision** when it is applied later.

#### Tombstones

A deletion leaves a tombstone, whether or not the object was live or
deferred. The tombstone records:

- `s`: the highest of the live object's revision, its deferred request's,
  and an earlier tombstone's `s`;
- `d`: the highest deletion revision seen.

**A port's create or update after the tombstone** passes only if its
revision is:

- above `s`, and
- at least `d`.

It removes the tombstone once the port is set, then or when a deferred
request is applied later. While it waits, the tombstone stays, so that if
its job fails, a late request from before the deletion is still refused.

For example:

- a host move's create, at the deletion's revision and above any earlier
  one, passes;
- so does a create after an unbind;
- a late update that changed nothing, at `s`, is refused.

**Any create or update of a tombstoned network is refused**, whatever its
revision, or without one: a network that was never updated is created and
deleted at the same revision.

#### Deletions

**A deletion is always applied, unchecked.** A deletion of the port carries
a revision read before its own transaction, possibly below one already
sent, so a late deletion can't be told from a current one. It only ever
removes a GUID from a partition, so it can't break isolation.

**Known limitation: a late deletion after a host move.** It removes a live
port, which Neutron shows ACTIVE, from its partition until its next update
that changes it, or its next binding, which may never come. A resend at the
same revision doesn't restore it. A late deletion at the port's revision
can't even be warned of: an ordinary deletion carries the revision of the
port's last request too, as tempest's runs show.

**A deletion below a live port's revision**, which is unusual, logs a
warning naming it.

#### Requests without a revision

A request with no `revision_number` is neither checked nor recorded, except
against a network's tombstone. A deletion without one leaves a tombstone
with no `d`. Objects from before revisions were recorded have none until
their next request.

#### Keeping and clearing tombstones

- **Tombstones are kept for 7 days** and then removed by the leader's
  reconcile. The single-node manager records and removes them alike.
- **After Neutron's database is restored from a backup**, its revisions go
  back and deleted IDs return. So `--forget-revisions` clears the
  tombstones and the revisions recorded on objects and deferred requests.

### Quarantine

State is cumulative, so an object that makes a render fail would otherwise
fail every later change, deletes included. So a failure is attributed to an
object, and only that object is left out:

- **A check of the rendered file** names the network or port whose
  partition or membership broke it.
- **An OpenSM parse error** names a line, which holds a partition's
  definition and up to 20 of its memberships. The objects on that line that
  changed since the last sweep are blamed. If none did, it is a failure
  that can't be attributed.

**An attributed failure.** The leader quarantines the object, fails its
jobs, and applies the rest again. An object is released from quarantine by
its next change or its deletion.

**A failure that can't be attributed**, for example a template error. The
leader keeps the last body it swept, fails the jobs of that generation, and
raises an alarm. Validating requests against the stored configuration makes
that a template problem, not a request one.

### OpenSM errors

OpenSM skips lines of a partitions file it can't parse and still finishes
the sweep. It logs, rather than fails on, a port whose pkey table it
couldn't set. The leader treats these as failures when they appear between
the signal and the end of the sweep:

- a partitions file parse error, `PARSE ERROR` from `parse_part_conf`,
  quarantines the line's object;
- the pkey manager's and virtualisation manager's errors setting pkey
  tables, by their codes in OpenSM 5.17, from its binary's messages:

| Code | Meaning | Treated as |
| --- | --- | --- |
| `ERR 0502` | No memory for a pending pkey entry | A pkey error on that port |
| `ERR 0505`, `ERR 0508` | A pkey couldn't be set in a block and index of a port's table | A pkey error on that port |
| `ERR 0506`, `ERR 0509` | A port's pkey table block couldn't be updated | A pkey error on that port |
| `ERR 0512` | A pkey couldn't be set because the port's table is full | A pkey error on that port |
| `ERR 0503` | A pkey's block and index couldn't be found for a port, so it is dropped | A pkey error on that port |
| `ERR 0511` | A port's PortInfo, which carries a switch port's enforcement bits, couldn't be set | A pkey error on that port |
| `ERR 0514` | The pkey manager couldn't allocate backup blocks | A pkey error |
| `ERR 0507`, `ERR 0513` | A switch port has too few pkey entries, so OpenSM **clears its enforcement bit**: the switch stops enforcing partitions on that port | Recorded against the switch port in `applied` until a PortInfo query shows its enforcement on again; a pkey error on every port behind it meanwhile, and an alarm, since a host behind it can send traffic with any pkey. Disabling the switch port is part of confirmed fencing, a future improvement |
| `ERR 0F12` | A PKeyTable request couldn't be started | A pkey error on that port |
| `ERR 1503`, `ERR 5703` | A virtual port's pkey table couldn't be read or set | A pkey error on that port |
| `ERR 6B06` | A virtual port's partition item couldn't be allocated | A pkey error on that port |
| `ERR 3113`, `ERR 3119`, `ERR 3120` | The MAD layer: a MAD completed in error, a Set failed, or a Get timed out; only when the attribute is PKeyTable (`0x16`) | An unattributed pkey error. A pkey sweep still completes after them, so they are the commonest way a port's pkey table goes unset, such as an adapter that doesn't answer |

**How each code names the port:**

| Codes | Name the port by |
| --- | --- |
| 0502, 0503, 0505, 0506, 0508, 0512 | Its node's description only |
| 0509, 0511 | Its node GUID and port number |
| 0514 | None |
| 0F12 | Nothing |
| 1503, 5703, 6B06 | Its port GUID |
| 3113, 3119, 3120 | A destination GUID, which needn't be the port's |

**An error that can't be traced to a port GUID fails closed.** It is
recorded as unattributed, and no job of that sweep completes. The leader
sweeps again while a job waits on it. So does a 0507 or 0513 that names no
switch port.

**A pkey error on a port's GUID** is recorded in `applied`, so jobs whose
effects involve that GUID don't complete, and the leader retries at the
next reconcile. It doesn't count towards stepping down: one bad adapter
mustn't move the leadership.

**Other codes.** Codes outside this list, such as the SA's `ERR 45xx` for
requests between ports that share no pkey, are about clients, not about
applying the file.

**Matching.** The list is matched by code, `ERR 0507:` for example. A newer
OpenSM's codes need checking before it is supported.

## Job completion

networking-mlnx holds a port DOWN until its job completes.

### Completed

A job at generation G is **Completed** when, in `applied`:

1. the leader's term is the current term;
2. the swept generation is at least G;
3. the job's effect holds in the swept body, and no GUID it involves had a
   pkey error in that sweep;
4. the leader's last fabric check, recorded in `applied`:
   - passed,
   - was taken after that sweep, and
   - is no older than `2 × check_interval`;
5. the sweep is still valid:
   - OpenSM is still running on the file it swept, and
   - the sweep started after a passing check that followed the last check
     to see another master or the leader's OpenSM not master, or to find
     expected switches missing (see While a switch is missing).

   `applied` records that with the sweep, so that every instance, not only
   the leader, can judge a job.

**After the leader stops.** A leader clears the last check before it stops
OpenSM. After a crash, other instances can still judge a job on the last
sweep for up to `2 × check_interval`. That is safe, since its effect was
swept and checked.

### Which sweeps count

- **Recording a sweep clears the last check**, so that a sweep is judged
  only by a check taken after it.
- **OpenSM's first sweep after it starts counts as a sweep**, a takeover's
  included, unless the last recorded check, whatever its term, was
  contested. Then the leader waits for a passing check and sweeps again with
  SIGHUP.
- **A contested check invalidates the sweep.** A check that sees another
  master, or the leader's OpenSM not master, invalidates the sweep, since
  the other SM may have set pkey tables meanwhile, and could undo a sweep
  made while it acts. So the leader:
  - makes no sweep while the last check was contested;
  - sweeps again only once a check passes;
  - and jobs complete only on that sweep and a check after it.

**Still to verify.** Whether OpenSM's pkey sweep resets pkey tables that it
believes are already right, or a heavy sweep is needed, is part of the
two-OpenSM test.

### The fabric checks

The fabric checks run after every sweep and every `check_interval`:

- **Complete topology:** OpenSM discovered every switch in
  `expected_switches`. That list is configured at deployment, or learnt
  from sweeps and confirmed by an operator. So a leader on a fragment of a
  split fabric, or one whose own link is down, doesn't count its fragment
  as the fabric.
- **No other subnet manager that matters:** the leader looks at the SMs its
  own SA knows, by their SMInfo records, and OpenSM's log since the last
  check. It sorts them by port GUID and by where they sit:

  | SM | Fails the check | Also |
  | --- | --- | --- |
  | Another managed node's SM: one whose port GUID is in another node row's port GUIDs | Whatever its state | The leader asks it to stop, as in Fencing, step 1, and raises an alarm, since it may have the fabric's keys |
  | An unknown SM, such as one a bare metal tenant runs | Only if it is master, or the leader's OpenSM went to STANDBY | Otherwise it only raises an alarm |

  **Why an unknown SM is only an alarm.** Without the SM_Key and M_Key,
  which HA mode requires (see Fencing, Required keys), it can't win a
  handover or set ports. How OpenSM 5.17 treats such an SM is part of the
  two-OpenSM hardware test.

A failed check raises an alarm, and no job completes until a check passes
again, except for the grants that While a switch is missing allows.

### Failed and Pending

A job is **Failed** once it isn't Completed and either:

- its object is quarantined, or
- the database's UTC time is past its stored deadline.

Otherwise it is **Pending**.

**How status is stored.** Status is computed when a job is read, in the
order above: Completed, Failed, Pending. It is written to its row the
first time it is terminal, with
`UPDATE ... WHERE status IN ('Pending', 'Running')`. A reader whose update
matches no row returns the stored status.

**Duplicate jobs are harmless.** A request whose commit outcome is unknown,
and which networking-mlnx sends again, creates a second job with the same
effect. That is harmless.

### While a switch is missing

**The problem.** A dead or unreachable switch fails the check, and holds
every job, until it comes back or an operator removes it
(`--confirm-fabric`, and with per-port M_Keys, specs/zero-touch.md). Most
of those jobs don't depend on the switch, and the ones users notice are
grants: a new instance's port stays DOWN while the switch is missing.

**What this section does.** It lets a grant complete when the switch can't
matter to it. Every other job still waits for a whole fabric. This applies
in single-node mode too, which keeps `applied` and runs the same check.

#### What a missing switch can hide

OpenSM enforces a port's partitions at two places, with `part_enforce
both`, which AFM sets:

- its own pkey table;
- the switch port it is cabled to.

Links between switches don't enforce. So a missing switch matters only to
the ports reached through it:

- the adapters cabled to a missing leaf;
- those on the far side of a missing spine, whose leaves are then missing
  too.

A missing switch may be dead, or alive with its agent hung and forwarding
with the tables it last had (specs/zero-touch.md, What stays manual). Its
ports keep what OpenSM last set:

- **A revocation from such a port hasn't happened.** So the port can still
  use the pkey it lost. Once that pkey is granted to another port, as when
  it is reused for another tenant's network, the stale port and the new
  member, full or limited, could talk through the hung leaf's stale table
  and the spines.
  - OpenSM applies a rendered grant at its sweep whether or not the job
    completes, so that leak exists today too. What completion adds is
    Neutron and Nova treating the port as up: a completed grant of such a
    pkey would let a new instance use the exposure, where a waiting grant
    fails with its timeout, and Nova's boot with it.
  - A revocation's wait bounds nothing, since a timed-out delete leaves the
    stale holder as it was, but completing it would tell Neutron the port
    is gone.
- **A grant to such a port hasn't happened either**, so completing it would
  say that a port works when it doesn't.

So only grants complete here, and only on pkeys no port can hold stale.

#### Scope

With per-port M_Key rows, a switch that doesn't answer behind a link that's
up fails the check as a hung switch (specs/zero-touch.md, Expected
switches), and that check is never partial. So there this section covers
only switches whose links are down, and the hung-leaf case matters where
the check runs `ibswitches`.

#### A partial check

A **partial check** is one that fails only because expected switches are
missing. That is, all of these hold:

- OpenSM is master;
- the check is contested by no SM;
- it queried the fabric in full;
- it finds no enforcement error;
- it finds no unanswered key;
- it finds no failing apply;
- it finds nothing else that fails a check.

**Unanswered keys in prober mode.** While this check's own verify finds
some expected switch missing (never an older full verify's, which may
predate the switch's return):

- a port never sent a key because each switch that didn't answer on its
  route (the shortest the recorded links give, which the prober follows) is
  an expected switch that is missing, or one whose link is down, **is no
  unanswered key**;
- one behind any other switch that didn't answer **is** one.

With no expected switch missing, every such port counts as unanswered, as
before. So this can make a check partial at most, never passing.

**What isn't partial.** A check that couldn't query the fabric, or any part
of what follows, isn't partial: it fails as today.

**A partial check is still a failed check everywhere else:**

- for the alarm;
- for Stepping down (a live candidate that reaches more switches takes
  over);
- for zero-touch's recording and dropping of switches.

#### Where a port is

**Placements come from the check's own scan.** A partial check places the
ports from its own scan, the one that found the switches missing, taken
after the sweep it judges. It never places them from the SA: the SA's
NodeRecords are OpenSM's database, which a sweep the manager didn't watch
may have changed, not evidence of what that sweep reached.

**What the scan lists.** Each adapter port it reached, and the switch it is
cabled to:

- in prober mode, from `fabric_links`;
- where the check runs `ibswitches`, which lists no adapters, from an
  `ibnetdiscover` scan in the same check.

**Placed.** A port is **placed** in a scan if the scan reached it through
switches it found.

**Virtual functions are vports.** A virtual function is a vport of its
physical port: OpenSM 5.17 runs ConnectX virtual functions that way (IB
virtualization). Its virtualization dump,
`/var/log/opensm-virtualization.dump`, is rewritten when its vports change,
and maps each vport GUID to its physical port, index and state.

A vport is placed if the dump read with the scan lists its GUID:

- exactly once,
- ACTIVE,
- on a placed physical port.

ACTIVE is OpenSM's view, not proof that the guest's link works.

**A grant needs its GUID placed at both checks around its sweep.** It needs
its GUID placed, the same way, by both:

- the last scan before its sweep, in the current term, and
- the check after the sweep,

with no OpenSM sweep start logged from the first scan to the end of the
second, other than the watched sweep's own. For a vport, "the same way"
means the same dump entry in both reads.

*Why:* OpenSM then knew the port, in the same place, throughout the watched
sweep, so the sweep set it or logged an error. A port that appeared or
moved meanwhile, such as a vport created after the sweep began, waits for
the next one.

##### Learning hypervisors' physical ports

**Why it's needed.** A new virtual function's grant can't wait for its
vport. A VF becomes a vport only once its guest runs, and Nova keeps a new
guest paused until the port's job completes. So the manager **learns each
hypervisor's physical ports**.

**What is learnt.** At each passing check, never a partial one, the manager
records (H, that physical port) in `host_ports` for every vport the dump
lists that meets all of these:

- listed exactly once;
- ACTIVE;
- at an index above 0;
- its GUID is a recorded port's, of `binding:vnic_type` `direct`, bound to
  host H and with no open job;
- on a physical port that isn't a recorded port's GUID, as a bare metal
  node's is.

A node row's port can be learnt: a control-plane host that also runs
instances, as in a hyperconverged or all-in-one deployment, is a hypervisor
too.

**Unlearning.** A physical port seen for another host than the one it is
learnt for, as a stale dump after a migration may show it:

- is unlearnt, logged, and kept as learnt for no host;
- is learnt again only once two passing checks in a row, each with a dump
  it could read, of the same OpenSM, see it for the same host.

**Pruning.** A learnt physical port is dropped once three passing checks'
scans in a row, of the same OpenSM, haven't found it. Three, since a scan
made while its link flapped mustn't drop it, and three such scans span at
least three `confirm_checks` intervals where the check runs `ibswitches`.

**The counts.** Both counts are the leader's, in memory, and start again at
each start of OpenSM, which only delays relearning and pruning.

**Writes.** Learning and pruning are leader writes, behind the leader
fence.

##### A virtual function with no vport yet

A virtual function's GUID that **neither dump read lists at all** is placed
if all of these hold:

- H has at least one learnt physical port;
- the same learnt set is placed the same way by both scans, in the same
  sweep-start window;
- every adapter port that the last passing scan found cabled to a switch
  now missing is attributed. Attributed means one of:
  - a physical port learnt for a host;
  - a recorded port's GUID;
  - a node row's port while none of that node's ports is learnt (or kept
    as learnt for no host). Once one is, the node is a hypervisor whose
    other ports a VF may land on, so they count only once learnt
    themselves.

  One that isn't attributed may be another physical port of H, which the
  VF could land on, behind a hung leaf, so it holds every grant placed this
  way.

**A GUID a dump read lists but doesn't place** (listed twice, not ACTIVE,
or on a physical port that isn't placed), as when a VF is attached to a
running guest or migrated live, waits as any vport does. A host with no
learnt physical port waits.

**The adapter-to-switch links kept.** The manager keeps the
adapter-to-switch links that passing scans found:

- in prober mode, `fabric_links`;
- where the check runs `ibswitches`, an `ibnetdiscover` scan at a passing
  check at most once every `confirm_checks` checks, which adds to the links
  kept rather than replacing them. So an adapter whose link was down at one
  scan keeps its last link until a scan finds it elsewhere.

**They never shrink.** An adapter removed from the fabric stays behind its
old switch, and, while that switch is missing, holds the grants placed
through hosts' physical ports unless it is attributed. That fails closed,
and is a known cost.

**Not covered.** An adapter cabled, or moved between hosts, since that scan
is a hardware change at the fabric, which this doesn't cover.

**Why it is safe otherwise.** A VF placed this way is on a physical port
the scan placed, behind a switch the check found. So OpenSM programs its
vport when it appears, as with a whole fabric, and pkey taint still guards
against a leak.

##### Recording placements

- **In memory.** The leader keeps its last scan's placements in memory,
  since a request's grant usually didn't exist when that scan ran.
- **With each sweep.** It records, with each sweep in `applied`, those of
  the GUIDs the sweep's grants involve. So the records, and the writes,
  follow the jobs, not the fabric.
- **After a takeover**, the last scan is the new leader's, never the old
  one's.
- **Sweeping again for a moved grant.** An open grant whose GUID, or for a
  virtual function its host's physical ports, the last scan places where
  the last sweep's records don't, is swept again, once for each place it is
  seen in, so that it isn't left to time out.

#### Possibly held memberships

`applied` keeps a map from pkey to the (GUID, membership type, host)
entries that some port may hold, the host being the port's
`binding:host_id`. Its purpose: a pkey some port may hold stale is never
granted on a partial check.

**Extended before each write.** The manager adds a body's memberships to
the map, in a write that is a compare-and-set on its term, before it writes
the body to OpenSM's file. So a body OpenSM may have read is in the map
even if the manager crashes before recording its sweep.

**Tainted.**

- A **pkey** is tainted while the map holds a (GUID, membership type) for
  it that the latest body doesn't: a GUID removed from it, or moved from
  full to limited. A live migration keeps the pair, so it doesn't taint the
  pkey.
- A **host** is tainted while the map holds a (GUID, host) for it that the
  latest body doesn't: a port that left it, by deletion, migration or
  rebinding.
- Taint isn't pruned while the fabric is incomplete, since telling that a
  stale holder has been cleared needs evidence the partial check doesn't
  have.

**Reset.** The map is reset only at a passing check, and only when all of
these hold:

- the check follows a sweep that counts in full (the fresh sweep below, or
  any later one) with no unattributed error;
- `--confirm-fabric` removed no expected switch since the map was last
  reset;
- the swept body is the latest body written (otherwise the reset waits for
  that body's sweep).

It is reset to the memberships of the swept body, plus the entries of every
GUID that had a pkey error in that sweep, so that one failing adapter
doesn't keep every pkey tainted.

**After `--confirm-fabric` removes a switch**, which may be a hung one
still forwarding, its ports' stale pkeys stay tainted. Only
`--confirm-fabric --reset-held` resets the map:

- the command's message names the reset, along with the pkeys it
  untaints;
- it refuses while the confirmation found problems.

Zero-touch's drop of a switch whose recorded links are all down doesn't
block the reset: such a switch can't forward.

**Complete.** The map is **complete** only once a reset has made it so.
Until then, it may lack memberships some port still holds, so no port's
grant completes on a partial check. That is the case:

- as after an upgrade;
- once another version may have swept a body the map didn't see
  (`applied`'s generation or body changed without it);
- once another version may have written one and crashed before recording
  its sweep (before a leader's first write of the map, the map's term isn't
  its own or the one just before it: every takeover raises the term by one,
  so another term led between).

A network's creation, which grants nothing, still may complete.

Taint only affects grants on a partial check: with the whole fabric seen,
jobs complete as before.

#### A grant completes on a partial check

A grant completes on a partial check when:

- conditions 1, 2 and 3 of Job completion hold;
- condition 5 holds, with this reading: OpenSM is still running on the
  file it swept, and the sweep started after a passing check that followed
  the last contested one. Missing switches don't invalidate it here, a
  contested check still does, and a partial check never ends a contest;
- the partial check is recorded after the sweep, no older than
  `2 × check_interval`;

and:

1. **The job is a grant:** creating a network, creating a port, or updating
   a port in a way that only adds memberships or changes none, such as a
   migration's new host. An update that removes or moves any membership,
   such as a network's pkey change, a port moving network, or a port whose
   GUID changes, is a revocation and waits.
2. **Its ports are placed.** Every GUID it grants a membership to, or keeps
   one for, is placed as above, with no pkey error in the sweep, and the
   sweep had no unattributed error.
3. **Its pkeys aren't tainted.** No pkey it grants a membership in is
   tainted.
4. **Its host is clean**, for any port but a virtual function's
   (`binding:vnic_type` `direct`): its host isn't tainted, and no revocation
   job is open for another port bound to it.
   - *Why:* a bare metal node with ports on two leaves, one of them
     missing, may still hold the last tenant's pkeys on the port behind it,
     and the new tenant's own port being placed says nothing about that
     one.
   - *Which ports:* a port with no recorded `vnic_type`, as one recorded
     before this, or with a type this doesn't know, such as
     `direct-physical`, a whole function passed to an instance, gets the
     same rule.
   - *Why virtual functions are exempt:* a stale virtual function on a
     hypervisor is another instance's, which the new instance doesn't get,
     and taint that isn't pruned while the switch is missing would soon
     hold every busy hypervisor's grants.

| Request | On a partial check |
| --- | --- |
| Create a VLAN network | Completes: a network's partition has exactly its ports as members, so a new one grants nothing to any port. Its ports' joins check the taint |
| Create a port, bind one, or update it without removing a membership | Completes if its GUID is placed at the checks either side of the sweep, with its membership swept, and neither its pkey nor its host is tainted |
| A port on a flat network | As above, with the flat partition's pkey, which is tainted while any port that left it may hold it |
| Delete a network or a port; update a network's pkey; move a port | Waits for a whole fabric, as today |

#### A fresh sweep after a partial period

After any partial check, or any check that found expected switches missing,
the leader sweeps again with SIGHUP once a check passes, as after a
contested one. Jobs complete in full only on that sweep and a passing check
after it.

*Why:* a sweep made while a switch was missing doesn't count once it is
back, since OpenSM set that switch's ports later, in a sweep the manager
didn't watch.

#### Reporting

`--status` and the alarm name the missing switches, and the open jobs,
tainted pkeys and tainted hosts they hold. `--status` also lists the
physical ports learnt for each host.

**A future improvement** would close the leak itself, not only bound it:
leave grants in tainted pkeys out of the rendered body while a switch is
missing.

## Fencing

Before a new leader starts OpenSM, every other node in the target set must
be unable to act on the fabric.

**No confirmed fence yet.** The first release has no fence that confirms
this without the target's cooperation. The ladder that would, killing
OpenSM's container over SSH, disabling the target's switch ports and
powering it off, is a future improvement (see Confirmed fencing).

### When a target leaves the set

Instead, a target leaves the set, with a term compare-and-set, once one of
these holds:

- **It stopped cleanly:** it released the lease, having verified that its
  OpenSM stopped (see Stepping down).
- **It verified its own stop:** after a self-stop or a restart, its manager:
  - stopped OpenSM through its watchdog;
  - verified through the host's process table that no OpenSM runs;
  - and recorded that in its node row, with an instance UUID other than the
    entry's and the lease term read in the same transaction, which must be
    at least the entry's term (see Rejoining).
- **Or, unconfirmed:** both of these hold:
  - the new leader has asked it to stop, step 1 below, if `fence_ask` is
    on;
  - `T_watchdog` has passed since the lease expired, or since the successor
    took a released lease. By then the target's self-stop or watchdog has
    killed its OpenSM, if either can run at all.

### Step 1: Ask

Send the target's SM a SubnSet(SMInfo) with:

- the DISABLE modifier;
- the SM_Key;
- and the fabric's M_Key, since the target's port is M_Key protected.

A live OpenSM honours it by going to NOT-ACTIVE.

- **It never counts on its own.** It can't be confirmed, since a frozen SM
  neither honours nor answers it.
- **`[ha] fence_ask`**, a cluster setting, is on by default.
- **It is best effort.** The new leader sends it by directed route, from
  its reachability scan, to every port of the target's adapters, by their
  node GUIDs in the target's node row. A send that times out, or that has
  no route, as for a crashed or powered-off target, counts as asked.

> **Not yet implemented.** The first implementation doesn't send the Ask
> yet (see the limitations below, and the implementation plan).

**No manual-takeover option.** There is no option to make every takeover
wait for an operator instead of the unconfirmed wait. It would trade the
paused-node limitation for a fabric without a subnet manager until someone
acts. Confirmed fencing, a future improvement, removes the limitation
without that cost.

### Why the unconfirmed wait is enough

Let `t0` be when the leader sent its last renewal that succeeded. No
candidate sees the lease expire before `t0` + `T`, and the unconfirmed start
comes `T_watchdog` after that. Meanwhile:

| The old leader | Why its OpenSM has stopped |
| --- | --- |
| **A crashed manager** | Its OpenSM died with it, through the PID namespace |
| **A manager cut off from Galera, or in a minority partition** | It stops its OpenSM by `t0` + `T_self` + the time to stop it, before `t0` + `T` (see The lease, Self-stop) |
| **A paused or hung manager** | Its liveness thread stops renewing the watchdog's lease by `t0` + `T_self` + its period + a call timeout, which is less than `t0` + `T`. So the watchdog kills OpenSM by `t0` + `T` + `T_watchdog`, before the new leader starts |
| **A leader whose self-stop and watchdog both failed** | Step 1 reaches its OpenSM in band, and a live OpenSM honours DISABLE. Failing that, the new leader's OpenSM starts in STANDBY, or the fabric check finds another master, so the start fails, no job completes, and an alarm is raised |

**What the paused-manager case needs:**

- every node's watchdog uses the same `T_watchdog`, so it is a cluster
  setting that each instance passes to its watchdog;
- `T_self` + the liveness period + the call timeout < `T`.

### Known limitations

**Not covered: a paused node, or a paused OpenSM container.** Nothing on it
runs, its watchdog included. So its OpenSM can resume as master before its
watchdog kills it, and sweep a stale partitions file until it notices the
new master. For that time it can undo a completed job, such as put back a
deleted port. The new leader's fabric checks find the other master, so no
job completes meanwhile, and an alarm is raised. This is the first
release's known limitation; confirmed fencing removes it.

**The Ask isn't sent yet.** The first implementation has a second
limitation: step 1, Ask, is not yet sent, so a leader whose self-stop and
watchdog both failed is not asked to stop. Its OpenSM is then found by the
new leader's fabric checks, as another master, so no job completes, and an
alarm is raised, until it stops.

### Rejoining

A node that left the target set unconfirmed rejoins as a candidate once its
OpenSM is known stopped and unable to start by itself:

1. **It registers again.** Its manager comes back and registers, with a new
   instance UUID, and sees that it doesn't hold the lease. A manager that
   finds it lost the lease without restarting does the same, with a new
   UUID.
2. **It verifies its stop.** It:
   - tells its watchdog to stop OpenSM, if it still runs, for example after
     a network partition rather than a crash;
   - removes the watchdog's lease and any start request from the tmpfs,
     since a crashed manager leaves them behind with no watchdog to act on
     them;
   - verifies through the host's process table that no OpenSM runs;
   - and records that in its node row.

   The watchdog only starts OpenSM on a single-use start request for a term
   the node holds, so OpenSM can't come back by itself.
3. **It becomes a candidate.** If its configuration's hash is allowed, it
   becomes a candidate again.

### Retiring a node

**Stop it cleanly first.** Stop a node's manager cleanly before taking the
node out of service, as with `docker stop`. It:

- steps down if it leads, releasing the lease;
- marks its node row stopped, which no instance counts as a live
  candidate.

Its row stays, and nothing acts on it. A frozen leader's node needs an
unfreeze first (see Freeze).

**A node that dies uncleanly** instead stops counting as live once its
heartbeat has been unchanged for `T`. Its target entry, if it has one,
leaves at the next takeover, as in Fencing.

**A retired node's row still counts.** Its port GUIDs are in the
`allowed_sm_guids` list, and an SM on them is another managed node's (see
Job completion).

**`--forget-node <name>` removes the row.** It is refused:

- for a node in the target set;
- for one whose heartbeat moved within `T`, unless its row is marked
  stopped. The command reads the heartbeat, waits `T`, and reads it again.

**The node's port GUIDs stay among the known SM ports**, since its files
still hold the keys. `--drop-sm-port` drops one once its adapter is no
longer in a host with the keys.

The running leader's list keeps the node until the next takeover renders a
new one.

### Seeing the host's processes

The manager runs in its own PID namespace, under `docker run --init` as PID
1, and OpenSM's container joins it. So the manager's own `/proc` shows only
the two containers.

It verifies that no OpenSM runs on the host, in any container or none,
through the host's `/proc`. That is mounted read-only into its container at
`/host/proc`. It looks for any process whose executable is `opensm`.

### Required keys

**The managed driver.** HA mode needs the managed driver, which starts
OpenSM itself, and refuses the others: with no OpenSM to check, every check
would pass.

**M_Key and SM_Key.** It also refuses to start unless:

- M_Key protection is on, at protection level 1 or more, and
- OpenSM has a generated SM_Key.

These are the `m_key` and `sm_key` hardening groups. Without them, a
tenant's SM could set ports or win a handover, and step 1 has no key to
authenticate with.

**Protect the backups.** The keys are then in Galera and its backups, as
well as in OpenSM's files and log, so the backups need the same
protection.

**The other groups are experimental.** The secured profile's other groups,
`allowed_sm`, `class_keys`, `sa_rate` and `m_key_per_port`, are allowed in
HA mode as experimental: untested on hardware there, and outside `all` (see
The secured profile).

## The watchdog

**What it is.** OpenSM's container keeps joining the manager's PID
namespace, so that OpenSM dies with the manager whenever the manager exits.
Its entrypoint is a small watchdog, which:

- starts OpenSM only on a start request;
- kills it with SIGKILL when its lease expires, when the manager asks it to
  stop, or when OpenSM exits. Then the watchdog exits too, so the container
  stops.

The container has no restart policy. The watchdog runs in its own
container's cgroup, so pausing the manager's container doesn't pause it.

### The watchdog's lease

The lease is renewed by a liveness thread of the manager every period,
2 s, only while the manager's loops make progress: no iteration of a loop
has run longer than that loop's bound.

**Each bound** is the sum of the timeouts of the calls one iteration makes,
plus a margin:

- **The apply loop:** one apply attempt per iteration in HA mode, with
  retries in later iterations. One iteration is a sweep, 30 s, a fabric
  check, 5 s, and up to five database transactions of 3 s, so `B` is 60 s.
- **The takeover loop**, which runs a takeover and OpenSM's start
  separately from the apply loop: Ask, 5 s, the unconfirmed wait,
  `T_watchdog`, and `start_timeout`, 60 s in HA mode, so `B_takeover` is
  120 s. A wait with no bound, for an unacknowledged unfreeze, polls in
  iterations of a few seconds each.
- **The lease renewal loop:** a call timeout, 3 s.

Also:

- **The HA apply loop must be built to its bound:** at most five
  transactions and one sweep per iteration. So a re-render after a
  quarantine, or a rejected line, waits for the next iteration.
- **A call that fails or times out** still finishes its iteration.

**`T_watchdog`** must exceed the period plus a call timeout, so that a
healthy manager's watchdog lease never lapses. It is 45 s.

**The deadline.** Each renewal sets the deadline to the earlier of:

- now + `T_watchdog`, and
- `t0` + `T_self` + `T_watchdog`,

from the same clock reading it checked `T_self` against, so that a stall
between the check and the write can't extend OpenSM's life.

**When renewals stop:**

- A paused, hung or crashed manager stops renewing, and OpenSM stops within
  `T_watchdog`.
- The liveness thread also stops renewing once no lease renewal has
  succeeded for `T_self`. So if the leader's own self-stop fails, the
  watchdog kills OpenSM anyway, within `T_self` + `T_watchdog`.

### Its files

- **The lease and the start request are on a tmpfs** the two containers
  share, so they don't survive a reboot. They are tagged with the host's
  boot ID, the instance UUID and the term.
- **The deadline is in the host's monotonic clock**, which every container
  on the host shares.
- **A start request is consumed when used**, and a stop removes both files,
  so a later `docker start` of the container can't start OpenSM.

### Its rules

- **On SIGTERM,** as at a host shutdown, the watchdog kills OpenSM with
  SIGKILL and exits, rather than leave OpenSM to be reparented. The manager
  then verifies through the process table that no OpenSM runs.
- **OpenSM must start as master.** An OpenSM that enters STANDBY has found
  another SM, so the start failed. The leader stops it and treats it as the
  fabric check finding another SM.
- **Before starting OpenSM,** the watchdog checks the hashes of
  `partitions.conf` and `opensm.conf` against those the start request
  names. With no partitions file, OpenSM would make every port a full
  member of the default partition, and an old `opensm.conf` could have
  other keys.

**What it can't cover.** The watchdog can't cover a pause of its own
container, or of the node: a frozen OpenSM can resume before the watchdog
does. That is the first release's known limitation (see Fencing).

**No automatic restart in HA mode.** The single-node manager starts OpenSM
again whenever it stops. In HA mode it doesn't by itself: an OpenSM that
stops makes its leader step down, and a new takeover, possibly by the same
node, starts it. The exception is the shared failure in Stepping down, when
the leader starts it again itself.

### Freeze

An operator can freeze the cluster for maintenance, such as of Galera
itself. OpenSM stays on the current leader's node, and isn't stopped or
moved, until the cluster is unfrozen.

#### Agreement is Galera's, and the leader's

**The request.** `alternative-fabric-manager --freeze` writes a freeze
request to the lease row, with a compare-and-set on the current term, on a
Primary component. It is refused unless a leader:

- holds the lease;
- has no other entry in the target set;
- has OpenSM running;
- and its last fabric check, in the current term, passed.

The request carries a deadline, the database's `UTC_TIMESTAMP()` +
`T_self`.

**The acknowledgement.** The cluster is frozen only once the leader
acknowledges the request, in the same row, with a compare-and-set that
requires the request to be before its deadline, judged by
`UTC_TIMESTAMP()` in that statement. The leader does so only while it is
renewing normally.

**An unacknowledged request.**

- If no acknowledgement arrives, the command withdraws the request.
- Whether or not anyone withdraws it, a request past its deadline
  unacknowledged counts as withdrawn, in the acknowledgement's
  compare-and-set and in every takeover's alike. So a command that dies
  can't leave a pending request blocking takeovers.

**Takeovers.** Every takeover's compare-and-set requires:

- while a request is pending, that it is withdrawn or past its deadline;
- once the cluster is frozen, that it is not frozen.

So no other instance can take over, whatever happens to Galera or the
network meanwhile.

*Why Galera:* a majority vote among the managers over another channel
wouldn't do: a candidate outside that majority could still reach Galera's.

**Draining.** `--drain` first waits, up to the job timeout, for open jobs to
end.

> **Not yet implemented.** `--drain` is described here as designed, but it
> isn't implemented yet, and nor is `--for`, without which a frozen
> leader's held watchdog lease has no deadline: the implementation plan
> leaves "Freeze: `--drain`, and `--for`" unticked, and the CLI has neither
> option.

#### Only while Galera is up

The cluster can only be frozen while Galera is up, and while the leader can
renew:

- a leader that can't reach a Primary component may be on the minority side
  of a partition, so it can't decide to keep its OpenSM by itself;
- a leader that self-stops never acknowledges.

#### The leader holds

**When it holds.** The leader holds only once it has read back, on a
Primary component, that its acknowledgement committed.

- **A leader that self-stopped meanwhile** and then finds its
  acknowledgement committed holds as the frozen node, and starts OpenSM
  again, rather than release the lease, since a frozen leader never
  releases.
- **An acknowledgement whose outcome is unknown** doesn't count until then.
  The leader keeps timing self-stop meanwhile, since the request may have
  passed its deadline.

**What holding means.** The frozen leader then:

- stops timing self-stop;
- ignores every step-down trigger but SIGTERM;
- never releases the lease;
- gives its watchdog a held lease, whose deadline is the freeze's expiry,
  if it has one, on the host's monotonic clock, plus `T_self`.

So OpenSM survives a Galera outage or a hung manager.

**What it keeps doing.** The leader keeps renewing, so that its `t0` is
fresh at the unfreeze, and applying and checking, while Galera is up. It
raises an alarm while frozen.

#### Requests wait

- Every instance reads the freeze within a poll interval, and then answers
  new requests with 503, which networking-mlnx retries, unlike 400.
- Job polls are still answered.
- Requests recorded before the freeze are applied as usual if Galera is up.
- Ports created during a freeze stay DOWN, so Nova's wait fails an instance
  whose port waits longer than it.

#### Restarts on the frozen node

OpenSM's container dies with the manager, through the PID namespace. While
frozen, only the frozen node's manager may start OpenSM again, after a
restart or after OpenSM exits.

**Adopting the frozen lease.** A restarted manager has a new instance UUID.
With a compare-and-set that matches the frozen node and term, it:

- adopts the frozen lease;
- replaces the instance UUID;
- replaces the old instance's target-set entry with its own;
- and clears its node's verified-stop mark, as a takeover does.

That way, a stop it verified before adopting can't later let a candidate
take over, or its entry leave the set, while its OpenSM runs.

**Only then does it start OpenSM**, with a start request for the frozen
term that carries the held deadline rather than `t0` + `T_self`.

**Rejoining's removal of the watchdog's files** doesn't apply to the frozen
node while it is frozen.

**Never from the held lease alone.** It never restarts OpenSM from the
watchdog's held lease alone, even with Galera down: a confirmed takeover
may have happened while its manager was paused, which the held lease can't
know. It restarts OpenSM only once a read from a Primary component shows
that it, or the instance it adopted from, still holds the frozen lease.

**What a freeze trades.** If the frozen node itself fails, the fabric has no
subnet manager until an operator unfreezes the cluster.

#### `--status` and `--force-unfreeze`

- **`--status`** shows the lease's holder, its instance and term, whether it
  was released, and the freeze, with its frozen term.
- **While a freeze ends unacknowledged**, it reports the unfreeze as pending
  and names the frozen leader's node as the one to check. Once confirmed,
  it says so.
- **`--force-unfreeze` takes no term.** It confirms the freeze ending when
  it reads the lease row, naming its term and holding instance. `confirm()`
  accepts it only if both still hold the ending freeze. So it confirms
  neither a later freeze nor an instance that adopted this one meanwhile,
  which keeps the term.

#### Unfreezing is two-phase

Unfreezing needs Galera.

1. **The freeze is marked as ending** by `--unfreeze`, or by the freeze's
   expiry, read by any instance that reaches a Primary component.
2. **The frozen leader acknowledges it**, in this order, which is required:
   1. it gives its watchdog a lease with a deadline again;
   2. it resumes timing self-stop;
   3. it renews;
   4. and only then records the acknowledgement in the lease row.
3. **Only then is the cluster unfrozen.** Every candidate restarts its
   expiry timer from that acknowledgement, treating it as a renewal.

**Until the leader acknowledges, no candidate takes over on expiry.** A
leader that never acknowledges, because it is paused, hung, or cut off,
leaves the lease only through:

- a verified stop of its node, or
- an operator's confirmation, with `--force-unfreeze`, that its OpenSM
  can't act.

Its watchdog's held deadline, if the freeze had an expiry, stops OpenSM
meanwhile.

**A takeover while the freeze is ending** is a compare-and-set that
requires either:

- an operator's confirmation recorded in the lease row while the freeze is
  ending, or
- the frozen node's verified stop, by an instance other than the holder, at
  a term at least the frozen term.

It clears the freeze in the same statement.

**The frozen leader's target entry is marked held**, since the unconfirmed
wait doesn't stop a held watchdog. It leaves only as verified, or with that
confirmation.

#### SIGTERM while frozen

SIGTERM while frozen stops OpenSM, since OpenSM's container is in the
manager's PID namespace, which ends with it, but doesn't release the lease.
The node's manager starts it again when it comes back. Shutting the frozen
node down for good needs an unfreeze first.

### Stepping down

A leader steps down by:

1. stopping OpenSM through the watchdog;
2. verifying through the host's process table that no OpenSM runs;
3. releasing the lease.

#### Triggers

It steps down when:

| Trigger | Condition |
| --- | --- |
| Its renewal matches no row | |
| No renewal has succeeded on a Primary component for `T_self` | The self-stop: it can't release the lease then, so it releases it once it can, as in The lease |
| It gets SIGTERM | Within `[ha] shutdown_timeout` |
| OpenSM exits | |
| Its configuration is no longer allowed | And a live candidate with an allowed configuration exists; otherwise it keeps leading and raises an alarm (see Configuration epochs) |
| A loop overruns its bound | It stops OpenSM, verifies, and releases the lease if it can |
| Its fabric checks fail to answer, or OpenSM fails to answer them, for three `check_interval`s in a row, as a paused or wedged OpenSM under a healthy manager would | And a live candidate exists |
| Its fabric checks find expected switches missing | And a live candidate reports reaching more of them |
| It can't write, signal or sweep, as opposed to a rejected body, for three `check_interval`s in a row | And a live candidate exists |
| The per-port M_Key target differs from what its OpenSM runs with | At a passing check with no open job, at most once an hour. It releases the lease so that a takeover's start, possibly its own, applies the target (specs/zero-touch.md). Unlike the other triggers, it needs no other candidate |

#### Shared failures never move the leader

Failures that every node would have the same way never make a leader step
down:

- quarantine;
- pkey errors;
- template errors that can't be attributed;
- a dead expected switch.

**OpenSM runs with `stay_on_fatal`**, so that a fatal fabric condition, such
as a tenant's duplicated GUID, doesn't make it exit.

**OpenSM exiting on two leaders.** If OpenSM exits on two successive leaders
within the cooldown, that is treated as a shared failure too: the
leadership stays put, and the leader raises an alarm and retries starting
OpenSM.

#### The cooldown

The discretionary triggers, a configuration no longer allowed, missing
switches and failing applies, don't move leadership again within
`[ha] step_down_cooldown`, five minutes say, of its last move, recorded in
the lease row. So a fault can't bounce it between nodes, restarting OpenSM
and the SA each time.

The cooldown never delays:

- a lost lease;
- a self-stop;
- SIGTERM;
- a hung apply loop;
- OpenSM exiting.

#### Live candidates

A "live" candidate is one whose heartbeat is current and that isn't in the
target set.

#### If OpenSM won't stop

- **If the watchdog doesn't stop OpenSM**, as when OpenSM's container is
  paused with it, the leader kills OpenSM's container, which stops a paused
  container too.
- **If it still can't verify that OpenSM stopped**, it stops renewing both
  the watchdog's lease and the Galera lease, and doesn't release the lease.
  So a successor waits for it as an unconfirmed target.

## Timeouts

A takeover takes up to:

- `T` for the lease to expire,
- plus `T_watchdog` for an unconfirmed target,
- plus starting OpenSM and its first sweep.

A job in flight during a takeover must not time out, or networking-mlnx
sets its port to ERROR.

### Cluster settings

Every value is a cluster setting in Galera, so that all instances agree:

- **The first instance stores its `[ha]` settings.**
- **An instance whose settings differ** from the stored ones in any key both
  have refuses to start.
- **A key only the instance has**, from a newer version, is added to the
  stored settings, with a warning.
- **A key only the stored settings have**, from an older version, is
  ignored.

### The inequalities

So:

1. **The job timeout** > the sum of:
   - the time a job can spend before the leader fails: the larger of a poll
     interval, a sweep (`sweep_timeout`) and a fabric check, and the apply
     loop's bound `B`;
   - `T`;
   - the expiry poll's interval;
   - `T_reach`;
   - Ask;
   - `T_watchdog`;
   - OpenSM's start and first sweep (`start_timeout`, which in HA mode
     counts as the first sweep, with its pkey errors);
   - a fabric check.
2. **`T_self` + the liveness thread's period + a lease call's timeout <
   `T`.**
3. **`T_self` > the deployment's MariaDB failover time**, so that losing
   one Galera node, or the VIP moving, doesn't stop OpenSM. That includes
   the time the surviving Galera nodes take to evict a lost one,
   `evs.suspect_timeout`, 5 s by default, plus a view change. During that,
   every node's commits block, whichever node was lost: a leader whose own
   Galera node is healthy still can't renew meanwhile.
4. **Nova's `vif_plugging_timeout` and Ironic's wait for ports** > the
   journal's sync interval + its retry backoff + the job timeout.

### OpenSM's image must already be on the node

Starting OpenSM in that sum assumes its image is already on the node: a
pull from a registry has no bound, and may fail just when a takeover needs
it.

- **Every instance checks for it.** From its start, every instance checks
  every minute that its node has OpenSM's image, and pulls it, on a thread
  of its own, if not.
- **A node without the image isn't eligible.** It reaches no switches,
  since the reach scan runs in that image, and the scan never pulls it. So
  it isn't eligible to take the lease until the pull completes, and its
  lease thread never waits on a registry.
- **Starting OpenSM doesn't pull it either.** An image removed after the
  node last scanned fails the start, so that a takeover steps down for
  another candidate rather than wait holding the lease, and a frozen node
  retries.
- **It stays in HAProxy's rotation** meanwhile, since the API doesn't need
  the image.
- **Only a missing image is pulled.** An instance only pulls an image its
  node lacks, so that a tag that moves can't change OpenSM between one
  start and the next, though it can after the image is removed.
  Deployments pin the image by digest, and pull it on every node
  themselves too.

### The defaults

| Value | Default |
| --- | --- |
| Poll | 2 s |
| `sweep_timeout` | 30 s |
| A fabric check | 5 s |
| `B` | 60 s |
| `T` | 30 s |
| `T_reach`, how long candidates wait for one that reaches every switch | 30 s |
| Ask | 5 s |
| `T_watchdog` | 45 s |
| HA mode's `start_timeout` | 60 s |
| `T_self` | 20 s |
| Renewals and the liveness thread | Every 2 s |
| Every lease call | Times out within 3 s |

**The job timeout.** With these, the job timeout must exceed
60 + 30 + 2 + 30 + 5 + 45 + 60 + 5 = 237 s.

- HA mode's default job timeout is therefore 300 s.
- The single-node default stays 180 s, since it has no takeover.
- With per-port M_Keys, a takeover's start verifies the ports and waits
  for the turn-on, 1.5 × `start_timeout` more, 327 s. So docs/kayobe.md
  sets 360 s, still below Nova's 420 s.

**Nova and Ironic.** Nova's `vif_plugging_timeout` must then exceed 300 s
plus the journal's sync interval of 10 s and its backoff. So HA
deployments set it to 420 s, and Ironic's wait for ports likewise; the
deployment role does both.

**A slower OpenSM.** A deployment whose OpenSM takes longer to sweep must
raise all three, and HA mode refuses cluster settings that break these
inequalities.

**`T_self`.** It is 20 s, with renewals and the liveness thread every 2 s,
and every lease call timing out within 3 s, so that a call blocked on an
unreachable database can't delay the self-stop: 20 + 2 + 3 < 30, leaving
5 s to stop OpenSM and for the clocks' drift.

**MariaDB failover.** A MariaDB failover behind Kolla's HAProxy, which
notices a dead Galera node in about 10 s, fits within `T_self`. A longer
one stops OpenSM, as in Failure scenarios.

### A failed DELETE

A failed DELETE is retried by networking-mlnx, `retry_count` times, forever
by default. So with the relative delete effects it completes once a leader
sweeps again. With a finite `retry_count`, a DELETE that runs out of
retries blocks every later create of its GUID until the journal is
repaired.

## Configuration

The leader writes both of OpenSM's files:

- **`opensm.conf`** is built from the hardening groups and the operator's
  overrides. The manager sets the keys it depends on itself, such as
  `partition_config_file`, and refuses overrides of them.
- **`partitions.conf`** is rendered from a Jinja template, in a sandboxed
  environment with undefined variables as errors.

**Checks before writing.** Before writing a rendered file, the manager
parses it. It refuses to apply it, quarantining the object that breaks it,
unless all of these hold, as the single-node manager already checks:

- the default partition is defined;
- every network's partition, and the flat networks' partition, has exactly
  its ports as full members, and no keyword;
- a port's GUID is in only one of those, and in no other partition, the
  default partition included, unless flat networks use the default
  partition (`flat_pkey = 0x7fff`);
- no partition but a network's has `ALL`, `ALL_CAS` or `ALL_VCAS` as full or
  both members, except the default partition with
  `[opensm] shared_default_partition`;
- every partition has a pkey, and a static partition's isn't a network's;
- no line is too long for OpenSM.

So a template, however it is edited, can't bridge ports of different
networks.

### Configuration epochs

#### The configuration hash

The **configuration hash** covers everything that decides the partitions
and how OpenSM protects the fabric:

- the template;
- `default_partition`;
- `flat_pkey`;
- `shared_default_partition`;
- the IPoIB settings;
- the hardening groups as resolved (so a release that adds a group to `all`
  changes it);
- `key_lease_period`;
- the `opensm.conf` overrides.

It leaves out:

- **node-specific options**, such as `guid`, the port OpenSM binds to, and
  paths, which differ between nodes by design;
- **rolling options**, an allowlist in code: `sweep_interval`, the routing
  options (`routing_engine` and the like) and the logging options. A change
  to them moves no protection, so it needs no epoch: roll it out as an
  upgrade;
- **generated values**: the keys, which every instance reads from Galera,
  and the generated `allowed_sm_guids` list. A key or list set in
  `[opensm_conf]` is an override, and is covered.

**How it is computed.** The hash is an HMAC-SHA256 of the canonical JSON of
those settings, keyed by a random secret stored in Galera beside the keys.
So a hash, which `--config-epoch` prints, doesn't let anyone test guesses
of an overridden key. Each instance records its hash in its node row at
registration.

#### The epoch

The **epoch** is stored in Galera: a number and the hashes it allows, one,
or two. `--config-epoch` prints it, with each node's hash and whether it's
allowed.

**With no epoch stored**, as after an upgrade from a release without them,
every hash is allowed, and the takeover's compare-and-set stores an epoch
with the taker's hash.

#### Only an instance whose hash is allowed is a candidate

- A candidate whose hash isn't allowed stays registered and heartbeats,
  and logs an alarm, once per epoch.
- While it can't be a candidate for a reason other than its reach, it
  heartbeats a reach of 0, so that no leader steps down for it. Such
  reasons are:
  - its hash or settings;
  - a live release without epochs;
  - or, with `allowed_sm`, no port GUIDs.

#### The epoch moves by itself once the nodes agree

**The rule.** An instance records a new epoch with only its hash, and
stores its cluster settings, when:

- its hash isn't one the epoch allows, or its cluster settings differ from
  the stored ones; and
- every live node row, its own and the leader's included, has that hash.

**Settings agree within an epoch.** The hash covers the cluster settings,
so the nodes an epoch allows agree on them. An instance whose settings
differ registers and heartbeats, so that its row counts, but isn't a
candidate.

**Only in the safe direction.** The waits of Timeouts assume every instance
that may run OpenSM has the same settings. So, unless the target set is
empty, an epoch moves them only in the safe direction:

- `lease_timeout` and `T_watchdog` not shorter;
- `T_self`, the period and the call timeout not longer.

Otherwise a successor under the new settings could take over, and leave a
target unconfirmed, before an old leader's OpenSM is bound to have stopped.

- **A change in the unsafe direction** needs every node stopped cleanly, and
  raises an alarm meanwhile.
- **If the leader dies uncleanly** while the nodes with the new settings
  wait, its entry stays and nothing can take over. The alarm and
  `--allow-config` say to run one node with the old settings until it
  leads, which clears the entry, then roll the change again.

**Conflicts.** A takeover rewrites the epoch row, so that it conflicts,
under Galera, with an epoch recorded meanwhile.

**Two-hash epochs.** A two-hash epoch stores the settings of the node that
recorded it. A node of the other hash whose settings differ stays blocked
until they match.

**Live rows.** A live row is one not marked stopped whose heartbeat moved
within `T`, as for live candidates.

**So a rolling change needs no command:**

1. As each node is reconfigured and restarted, its hash isn't allowed and
   it isn't a candidate, while the leader keeps leading on the old one.
2. Once the last node, the leader, is restarted, or has been dead for `T`,
   every live row agrees, the epoch moves, and an updated node takes over.

A node that comes back with the old configuration isn't allowed, and, being
live, stops the epoch moving back. A release that changes what the hash
covers rolls out the same way.

#### Frozen nodes and leaders

- **A frozen node adopts its frozen lease** at a restart whatever its hash,
  since a freeze keeps OpenSM where it is, and raises the alarm. It hands
  over once the freeze ends, as below.
- **A leader whose hash is no longer allowed steps down**, once an eligible
  candidate exists: live, with an allowed hash and a non-zero reach. This
  is a discretionary trigger (see Stepping down). Otherwise it keeps
  leading, and raises an alarm.

#### Recording an epoch by hand

`--allow-config [<hash>]` records an epoch by hand, allowing the hash of
the configuration it runs with, and the given one if any. Use it to move to
a configuration before every node has it, or to allow two at once.

**Changing a key** needs a key rotation procedure, which is future work.
Until then the manager refuses to start with keys other than the stored
ones.

#### Releases before epochs

Instances of releases before epochs record no hash, and never check one.
During a rolling upgrade to this release they can still lead. So a node
whose configuration names one of the secured profile's experimental groups
(see The secured profile) isn't eligible while a live row has no hash, and
raises an alarm. Upgrade every node with the configuration unchanged
first, then name the groups.

### The secured profile

The groups specs/secured-profile.md adds, beyond `m_key` and `sm_key`, are
allowed in HA mode as experimental, as that spec's HA mode section
describes. In summary:

#### The leader fence

**What it guards.** These records are written only by the leader:

- the records those groups keep in Galera: per-port M_Keys' rows and state,
  the class keys' turn-off record, and whether a start found the per-port
  M_Key base stored;
- the expected switches the leader records itself (specs/zero-touch.md).

**How.** Each such transaction first does a compare-and-set on the leader
fence: it raises the fence to its own term, and fails if the fence is
above it.

**A fresh nonce each time.** Each raise also writes a fresh nonce to the
row, so that it always changes the row. Galera certifies transactions only
against rows both changed, and an update that left the fence as it was
would let a deposed leader's transaction commit beside the successor's
raise.

**At a takeover.** A takeover raises the fence, and commits the raise,
before it reads the records: a raise in a session that only reads is
rolled back, and Galera certifies nothing against it. So a deposed
leader's write either commits first, and is read, or fails.

**Operators' commands** write without it, as they do in single node, since
they change only what their operation names.

#### Node port GUIDs

Every instance probes its host's physical InfiniBand ports, as
`allowed_sm`'s probe does:

- at registration;
- then at every reach refresh until a probe succeeds.

It records them in its node row, and among the known SM ports, in one
transaction. A probe that fails or finds no port changes nothing.

#### `allowed_sm`

- The list is the union of the node rows' port GUIDs. An override must
  contain all of them, and can't be `0`.
- While the list is in effect:
  - a node without port GUIDs isn't a candidate;
  - a leader refuses to start OpenSM while another target's node row has
    none.
- A list change, such as a node joining, takes effect at the next takeover.
- A candidate outside the running leader's list may take over as any other:
  see specs/secured-profile.md, HA mode, for the edge case that leaves.

#### `m_key_per_port`

- **The rows.** The leader keeps the rows as the single-node manager does.
- **OpenSM's cache.** A takeover renders OpenSM's cache from the rows, never
  from what its host's cache volume holds, which an older term left.
- **The start request.** Its hashes cover the rendered cache files as well
  as `opensm.conf` and `partitions.conf`, and are written once they're
  rendered. Its deadline, `t0` + `T_self`, is worked out then, from the
  latest renewal, since the verify before it may take longer than
  `T_self`. The write is refused once a step-down has begun.
- **A start after a stop** within the same takeover, as when a turn-on
  fails, removes that stop request.
- **The reach probe** uses the prober along the recorded links, with each
  port's key, rather than `ibswitches` with one key.
- **The takeover loop's bound** allows for the longest start with per-port
  M_Keys: a turn-on whose success rule fails, stops OpenSM, verifies the
  ports again and starts it once more, `4 × start_timeout` longer.
- **A takeover's longest, which `job_timeout` must exceed**, counts the
  usual start, which verifies the ports and waits for the turn-on's success
  rule, `1.5 × start_timeout` longer: a failed turn-on is a one-off, and
  may outlast a job.

#### `class_keys` and `sa_rate`

These need nothing beyond the leader fence: the seed is in Galera, so every
leader derives the same keys, which hardware test 3's second-host run
checks.

## Galera specifics

The manager uses the deployment's Galera through its usual database VIP, as
the OpenStack services do. SQLAlchemy makes the backend a
`[database] connection` URL, and SQLite stays for single-node deployments.

### Writes go to one Galera node

Kolla's HAProxy sends MariaDB traffic to one active backend. Every request
bumps the generation row, so writes from several Galera nodes would
conflict at commit.

### Retry on deadlock

Galera reports a certification conflict as a deadlock at commit. Retry the
whole request transaction, validation included, as oslo.db does for
OpenStack services.

**The operators' commands retry theirs too**: a freeze request races the
leader's renewal of the same lease row, and Galera aborts one of them. A
deadlock is a certain abort, unlike a lost connection, and each command's
write is a compare-and-set, so retrying it is safe.

### Retry constraint errors too

**Why they happen.** Every request's transaction bumps the generation
first, so requests through one Galera node run one at a time, and their
validation sees each other's commits. Two requests racing through different
Galera nodes both change the generation row, so certification aborts one as
a deadlock. A pkey or GUID clash that reaches a unique constraint, or a
port whose network went meanwhile, is only a backstop for that.

**What happens.** Either way the whole request runs again, up to five times
with a short random pause, and its validation then gives the answer:

- waiting for the pkey or GUID; or
- 400 for a network that is gone.

Never 400 from the constraint itself, since such a request can succeed
later.

**Why retrying is safe.** Both are certain aborts, so a retry can't apply a
request twice. A lost connection, whose commit may have happened, isn't
retried.

**A conflict that outlasts the retries** is answered 500, which
networking-mlnx resends.

### Read what the cluster committed

**The setting.** Every manager session sets `wsrep_sync_wait = 7`, so that
every statement, reads, updates, deletes and inserts alike, waits until its
Galera node has applied what the cluster had committed when it began.
Kolla's VIP fails over between Galera nodes.

**Why reads alone, `1`, aren't enough.** A request's transaction begins with
the generation bump, an `UPDATE`, and no read after it in the transaction
waits. So a node that lags the cluster, by apply lag or while it rejoins
after an outage, would answer from its older state. It could refuse a valid
request with a final 400 for state it hasn't applied yet, such as a port on
a network created meanwhile, as the nemesis found.

**Not in a Primary component.** A request whose node isn't in a Primary
component, or isn't ready, gets 503, which networking-mlnx retries.

**Lease calls.** A lease call so makes two causal waits, its status check's
and its statement's, each bounded by the call timeout. So a leader's tick
under sustained lag takes up to six call timeouts, below `T_self` with the
defaults.

### UTC

- Timestamps and deadlines are `DATETIME(6)` values set and compared with
  `UTC_TIMESTAMP(6)`, so that no session's timezone or daylight saving
  change can age a job. SQLite uses its own UTC functions.
- Galera's nodes need synchronised clocks, within much less than the job
  timeout, since a deadline is judged by whichever node serves the read.
- Lease expiry doesn't use the database's clock.

### Schema migrations

Alembic, run by one instance at deploy time, rather than `create_all` at
startup.

### One instance per name

- **Registration.** An instance registers a UUID on its node row at start.
- **Restarts.** A restarted instance takes over its name once the old
  UUID's heartbeat has been unchanged for `T`.
- **A missing row.** An instance whose heartbeat finds no row for its node,
  as after someone deleted it by hand, puts its row back, so that it is
  never live without one, unseen by the others. It does so only while it is
  a candidate or leads, not while it registers a new UUID or once it has
  stopped.
- **An existing row** is left alone, whichever UUID it holds. One that holds
  another instance's UUID means that instance registered on the name, and
  this one logs an error.

### HAProxy

All three instances are active HAProxy backends, round robin with no
session stickiness. HAProxy checks `/healthz`, which fails while the
instance can't reach the database.

## Upgrades

### Rolling

1. Upgrade the candidates one at a time.
2. Then have the leader release the lease, which a candidate takes without
   a fence, and upgrade it.

- **Schema changes must be compatible with the version before**, so that
  old and new instances can share the database during the upgrade. A
  version that can't, says so, and needs every instance stopped.
- **Each release of the lease restarts OpenSM on another node**, so its SA
  state is rebuilt.

### From a version with `[ha] manual_takeover`

An instance of the old version with it set waits for an operator to confirm
every takeover it leads, and the new version's CLI can't confirm one
outside a freeze. So set it to false and roll that out before upgrading.
The new version ignores the option, with a warning, and the stored setting.

### From single node

Do this with the manager stopped, which stops OpenSM too, so the fabric has
no subnet manager meanwhile:

1. Copy the networks, ports and keys into Galera, and continue the
   generation above the single-node one, which the partitions file's
   header carries.
2. Record the single-node host as verified stopped, so that the first
   leader's target set is empty.
3. Copy the jobs, keeping their IDs, since journal rows that are monitoring
   a job would otherwise get 404 and send their requests again.
4. Keep terminal jobs as they are. Give every unfinished job, `Pending` or
   `Running`:
   - the migration's generation;
   - a deadline;
   - status `Pending`;
   - and its effect, which for a delete needs only its object's identity,
     by the relative rule.

The unfinished jobs then complete, or fail, once the first leader has
swept.

### Job history

Keep finished jobs for longer than networking-mlnx can poll one, a day,
say, then purge them from any instance, since the purge is idempotent. A
poll of a purged job gets 404, and networking-mlnx would send its request
again.

## Failure scenarios

Every case fails closed: a port waits rather than becoming ACTIVE before the
leader's OpenSM has swept its membership with the whole fabric in view.

Where a row says a candidate asks the old SM to stop, note that the first
implementation doesn't send the Ask yet (see Fencing, Known limitations).

| Failure | What happens | Outcome |
| --- | --- | --- |
| The leader's manager crashes | Its OpenSM dies with it, through the PID namespace. After `T` a candidate takes the lease, asks the old SM to stop, waits `T_watchdog`, and starts OpenSM | Jobs wait through the takeover |
| The leader's apply loop hung, its renewal thread still running | Its renewal thread stops renewing the Galera lease and asks the watchdog to stop OpenSM, whose lease also lapses. A candidate takes over as above | As above |
| The leader's manager paused or hung | Its liveness thread stops renewing the watchdog's lease, and OpenSM stops before the unconfirmed wait ends (see Fencing). A candidate takes over as above | As above |
| The leader's OpenSM container paused or wedged, its manager healthy | Its fabric checks go unanswered, so it steps down, killing OpenSM's container if the watchdog can't stop it, and releases the lease | A candidate takes over |
| The leader's node paused | Nothing on the node runs. A candidate takes over as above, unconfirmed | **The known limitation:** a resumed OpenSM can act as a stale master until its watchdog kills it, and can undo a completed job meanwhile; the fabric check finds it, so no job completes, and an alarm is raised |
| The leader cut off from the management network, OpenSM still running | Galera runs on the management network, so its renewals fail and it stops its OpenSM within `T_self`, as below | As below |
| The leader cut off from Galera only, or in a minority partition | Its renewals fail, so it stops its OpenSM within `T_self`. The majority side's candidates see the lease unrenewed for `T`, elect a leader, and take over after the unconfirmed wait | Jobs wait through the takeover. No two masters |
| The leader shut down cleanly | It fails `/healthz`, stops and verifies its OpenSM, and releases the lease | A candidate takes over at once, without waiting |
| A takeover's winner dies before starting OpenSM | Its targets, and itself, stay in the target set. The next candidate waits for them all | Jobs wait longer |
| A leader on a fragment of a split fabric | Its fabric checks fail: switches are missing. A live candidate that reaches more switches makes it step down, and takes over | Jobs wait through the takeover; alarm |
| An expected switch dies | Every node misses it, so nobody reaches more and leadership stays | Grants to ports the check places, on pkeys no port may hold stale, complete (While a switch is missing); every other job waits until an operator runs `--confirm-fabric`, or, for a spine with per-port M_Key rows, the prober shows every link to it down; alarm (specs/zero-touch.md) |
| A node lacks OpenSM's image, and can't pull it | Its reach is 0, so it isn't eligible; it stays in HAProxy's rotation, and logs an error every minute | Nothing, while another candidate has the image; alarm |
| A cold start, every port in Init | Candidates need a link up and Init or above; Active isn't required | A candidate that reaches every switch takes the lease and starts OpenSM |
| Any one Galera node lost, even a candidate's | Every node's commits block until the survivors evict it, after `evs.suspect_timeout`, 5 s by default, and a view change; the leader's renewals fail meanwhile, but its liveness thread keeps the watchdog's lease | Within `T_self`, nothing happens |
| A MariaDB failover | Failed reads reset the candidates' timers, and the leader's renewals fail meanwhile | Within `T_self`, nothing happens. Longer, the leader stops its OpenSM, and a leader is elected once a Primary component is back, so the SA is down meanwhile, and jobs in flight can time out and are resent |
| A request runs late, from an instance paused after accepting it, after the object's later requests | A create or update below the object's revision, or not past its tombstone's, or of a deleted network, is refused; a deletion is applied (see Late requests) | No deleted port comes back to its partition; a late deletion after a host move can leave a live port out of its partition, with a warning |
| A managed node's SM appears | The fabric checks find it; the leader asks it to stop | No job completes until it is gone; alarm |
| A tenant runs an SM | It has no keys, so it can't master or set ports | An alarm; jobs carry on unless it is master |
| Galera unavailable, or no Primary component | Requests and `/healthz` fail; no takeover is possible, and the leader stops its OpenSM within `T_self` | The fabric runs without a subnet manager: forwarding carries on with the switches' tables, but SA queries, multicast joins and link changes go unhandled until the database is back. networking-mlnx retries its requests |
| OpenSM exits | The leader steps down, verifying that no OpenSM runs, and a candidate takes over at once | Jobs wait through the takeover |
| An object breaks the render or OpenSM's parse | It is quarantined and its jobs fail | Every other change carries on |
| A port's pkey table can't be set | Jobs involving its GUID wait, and the leader retries | Other jobs carry on; the leadership stays |
| A deposed leader writes late | Its compare-and-set on the term fails | Nothing happens |
| A port is created then deleted before a sweep | No swept body has a membership attributed to it | The create times out and fails; the delete completes |
| Frozen, and Galera unavailable | The leader holds: no self-stop, and its watchdog holds | OpenSM keeps running; requests get 503 from HAProxy until Galera returns |
| Frozen, and the leader's manager restarts or OpenSM exits | The frozen node's manager starts OpenSM again for the frozen term | A short gap without an SA |
| Frozen, and the frozen node fails | Nobody may take over | No subnet manager until an operator unfreezes; alarm |

## Alternatives considered

| Option | Why not |
| --- | --- |
| OpenSM's own master and standbys on every node | A standby takes over by itself, with whatever file it has, which nobody may have applied, and jobs need evidence from every node. One OpenSM at a time makes a job's evidence one sweep. Its election would keep the SA up through a failure, and work without Galera, which this design gives up |
| Raft among the manager instances | Galera already gives replicated state with quorum. Raft would add a second consensus that can disagree with it, and a Raft leader is still a lease that a paused process can outlive, so fencing is needed anyway |
| tooz's MySQL backend for the lease | It uses `GET_LOCK`, which Galera doesn't replicate. tooz with etcd would do, where a deployment runs etcd |
| Keeping a leader's OpenSM running while it can't reach Galera | A MariaDB outage would then not stop the subnet manager, but a leader in a minority partition would run alongside the majority's new leader, with nothing to confirm it stopped. Self-stop makes a partition safe without confirmed fencing, at the cost of no subnet manager during a total database outage |
| Taking over at once, at a boosted priority, without self-stop or the unconfirmed wait, so a stale master steps down | Every failure, not only a paused node, would leave a stale master sweeping until it notices the other one, and it can undo a completed job, such as put back a deleted port, before it steps down |
| Fencing by M_Key rotation | OpenSM has no supported online key rotation; revisit with key rotation |
| Answering 503 without a leader | networking-mlnx backs off on it, and resends a job poll answered with it; recording requests whenever Galera is up is simpler and loses nothing |

## Implementation plan

- [ ] Verify on a fabric with two OpenSMs, real or simulated: SMInfo
      DISABLE; the SA's SMInfo records of another SM; OpenSM's log when it finds one; how it treats
      an SM without its SM_Key and M_Key, as a tenant's would be;
      directed-route reachability of switches from a candidate's port in
      Init.
- [x] Verify on the test host: `docker kill` stops a paused container under
      cgroup v2; OpenSM 5.17's codes for pkey-table errors, the MAD layer's
      3113, 3119 and 3120 included.
- [x] Single node: a DELETE of an unknown object creates a job rather than
      answering 404; the checks of rendered files; the managed driver; and
      reconciling the file with the database.
- [x] Single node: the pkey error codes, recorded per GUID; quarantine of
      the objects that break a render or OpenSM's parse; the render's
      attribution of partitions and memberships to objects.
- [x] Single node: jobs record their effect and deadline, and complete only
      when the effect holds in the swept body.
- [x] Single node: the fabric checks, against expected switches and other
      SMs, by the SA's SMInfo records.
- [ ] Single node: a PortInfo check that clears a switch port's recorded
      enforcement error. Until then, cleared enforcement holds every job
      until an operator runs `--clear-enforcement-errors`; and other SMs
      are found only by the SA's SMInfo records, not OpenSM's log.
- [ ] Single node: map a node GUID and port number, and a node
      description, to a port GUID, so that fewer pkey errors hold every job.
- [x] Database: a `[database] connection` URL, Alembic migrations, schema
      version, cluster settings agreed by every instance,
      `wsrep_sync_wait = 7`, UTC timestamps, the generation bump first in
      every request, and 64-bit counters.
- [x] Configuration epochs: the hash in each node row, the epoch in
      Galera, candidates and step-downs by it, `--config-epoch` and
      `--allow-config`.
- [x] The secured profile's groups in HA mode, experimental: the leader
      fence, node port GUIDs, `--forget-node`, and each group as The
      secured profile says. Their hardware runs in HA mode are still to
      come.
- [x] Complete grants on a partial check (see While a switch is missing):
      placing ports from the check's own scan (`ibnetdiscover` where
      the check runs `ibswitches`), at the checks either side of the
      sweep, vports through OpenSM's virtualization dump, and virtual
      functions whose vport doesn't exist yet through their hosts' learnt
      physical ports; the possibly
      held memberships with their hosts, extended before each write, and
      tainted pkeys and hosts; each port's `binding:vnic_type`, recorded
      from the port networking-mlnx sends; `--confirm-fabric
      --reset-held`; the fresh sweep after a partial period; and a
      failover test with a leaf paused in the fake fabric. Revocations
      stay for a whole fabric until the open question on the SA below is
      answered on a fabric with more than one switch.
- [x] Retry deadlocks and constraint races in the request path, as Galera's
      certification conflicts and two instances racing for a pkey or GUID
      raise them, before answering. Never as 400, since such a request can
      succeed later.
- [x] The lease, with Galera: target-set entries of node, instance UUID and
      term; the verified-stop mark cleared on taking the lease; taking on a
      Primary component only, renewing, self-stop after `T_self`, releasing
      on step-down and SIGTERM, expiry on the monotonic clock, terms in
      every compare-and-set, and the target set.
- [ ] A coordination driver interface for the lease, with Galera as its
      first driver: today `GaleraLease` is the only implementation, so this
      matters once a second is wanted (see Coordination drivers).
- [x] The manager's liveness thread, with each loop's progress bound,
      gating both the watchdog's lease and the Galera renewal; the host's
      `/proc` mounted read-only for verified stops.
- [x] Cluster settings refused when they break the inequalities in
      Timeouts.
- [x] The watchdog: an entrypoint for OpenSM's container, with the lease and
      single-use start requests on tmpfs, the file hashes, `T_watchdog` from
      the cluster setting, and SIGKILL of OpenSM on SIGTERM.
- [x] Takeovers: the unconfirmed wait, rejoining with a verified stop, and
      the required keys check.
- [ ] Takeovers: SMInfo DISABLE behind `fence_ask`. The Ask is still a stub
      that logs (see Fencing), pending the two-OpenSM verification above,
      which needs a fabric no one else shares.
- [x] Freeze: `--freeze` and `--unfreeze`, the takeover compare-and-set
      requiring not frozen, 503 for new requests, the leader's hold and the
      watchdog's held lease, restarting OpenSM only on the frozen node, and
      candidates' timers restarted at the unfreeze.
- [ ] Freeze: `--drain`, and `--for`, without which a frozen leader's held
      watchdog lease has no deadline.
- [ ] `--confirm-fenced <node>`, with `fence_manual` (see Manual fencing).
- [x] A CLI to confirm the expected switches, `--confirm-fabric`, which
      also drops one that died.
- [ ] A CLI to force a step-down.
- [x] The managed-SM fabric check, by node rows' port GUIDs, and
      `--forget-node` (see Retiring a node).
- [ ] The nodes in `--status`.
- [x] Tests: the failover suite, three instances against a three-node
      Galera cluster in containers with a fake OpenSM under the real
      watchdog, with faults injected and a monitor that no two OpenSMs ever
      act at once; a seeded nemesis of overlapping faults under live
      requests, with a history checker; and the unit tests against
      MariaDB. CI runs the unit tests on SQLite and MariaDB and the
      failover suite on every pull request that touches HA code, and the
      nemesis nightly and before every release.
- [x] Deployment, documented for Kayobe (docs/kayobe.md) rather than as a
      role: the HAProxy backend, healthcheck, database credentials, the
      containers' stop timeout, expected switches, and a deployment check
      that exactly one controller runs OpenSM and every instance is healthy.
- [ ] Deployment: a check that control traffic doesn't run over the
      InfiniBand fabric.
- [x] Late requests: revisions recorded on networks, ports and deferred
      requests, tombstones, the checks after the generation bump, the
      deletion's body read, tombstones dropped after 7 days, and
      `--forget-revisions` (see Late requests).
- [x] Counters wide enough for a cluster's life: the generation, the
      applied generation and job IDs as 64-bit integers, since every request
      bumps the generation and a 32-bit one overflows after 2^31, about 7
      years at 10 requests a second, after which, in MariaDB's strict mode,
      every request would fail. The lease's counter and heartbeats are
      64-bit already, and terms only rise with takeovers.
- [x] A soak test: a cluster left for 12 to 24 hours under steady requests
      and no faults keeps one leader, term and OpenSM process throughout,
      fails no job, and doesn't grow its memory or file descriptors. A
      12-hour run with the production timings passed, its managers' memory
      flat after the first four hours.

## Open questions

- Does a ConnectX-5 keep a vport's pkey table across a change of its
  virtual function's GUID, or a reset of the function? If it does, a VF
  placed through its host's physical ports could start with a stale
  table (see While a switch is missing).
- Does OpenSM 5.17 honour SMInfo DISABLE, and how quickly?
- For confirmed fencing: can the switches' ports be disabled reliably with
  the M_Key, and does a disabled port stay disabled whatever its neighbour
  does?
- How does OpenSM 5.17 log another SM, and do its SA's SMInfo records show
  every SM on the fabric?
- Which sites need confirmed fencing, and which steps: SSH to the OpenSM
  hosts, M_Key-protected switch ports, or BMCs?
- What is the deployment's MariaDB failover time, and are 30 s for `T`,
  20 s for `T_self` and 300 s for the job timeout right?
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
- While a switch is missing:
  - When does the SA drop a lost port's NodeRecord, and its vports'?
  - What would let a revocation complete safely, with evidence that each
    place that held the pkey no longer does?
  - When is the virtualization dump rewritten: at every vport change, only
    some, and when a physical port is lost?
  - Does OpenSM name the vport's GUID when it can't set a vport's pkey
    table?

  Seen on the DevStack's ConnectX-5s, with OpenSM 5.17.2 and an instance
  booted on a virtual function:
  - Nova set the VF's GUID to the port's client ID's;
  - the SA listed it as a NodeRecord of its own, with its own LID, read
    without an SA_Key;
  - the physical port's GUIDInfo records held only its own GUID;
  - OpenSM rewrote the virtualization dump when the vport appeared,
    mapping it to its physical port and index, ACTIVE;
  - and setting the VF's link state to disable on the host left the vport
    ACTIVE in OpenSM's view, and its NodeRecord in the SA, for over two
    minutes, though OpenSM rewrote the dump within two seconds.

## Future improvements

### Podman

The managed driver runs OpenSM's container through the Docker API, and
relies on OpenSM's container joining the manager's PID namespace. Neither
has been tested with Podman, whose Docker-compatible socket the Kayobe
guide would use (docs/kayobe.md).

### Confirmed fencing

Fencing that doesn't depend on the target's cooperation, which removes the
paused-node limitation in Fencing. Each step would be behind its own
cluster setting, off by default: `fence_container`, `fence_switch_ports`
and `fence_power`. Points to settle when it is built:

- With a confirming step enabled, fall back to an unconfirmed takeover after
  `T_watchdog` if every enabled step fails, since self-stop makes
  partitions safe on its own; otherwise a leader cut off from the
  management network, which SSH can't reach, leaves the fabric without an
  SM until an operator acts.
- Unfencing uses step 2's `status` over SSH, so the SSH script and
  `ssh_address` would be needed on every node whenever any fencing step
  is enabled.
- The fence settings would be cluster settings, so a takeover's fencing
  doesn't depend on which instance wins.
- The failure table rows for a crashed or paused leader would list step 3
  as well as step 2.
- Power fencing, step 4, also needs BMC settings in the `nodes` table; a
  check at startup that every node has them when `fence_power` is on; a
  re-check every `check_interval` that a power-fenced node stays off, since
  a BMC's power-restore policy can turn it on; and unfencing, in which the
  operator powers the node on, its manager registers as in Unfencing, and
  the leader clears the fence record once step 2's `status` shows no
  OpenSM, with no ports to enable.

The earlier design follows, for reference.

#### The fencing ladder

Before a new leader starts OpenSM, every other node in the target set must
be unable to act on the fabric. That can't depend on those nodes'
cooperation: one may be paused, hung, or cut off from the management network
while its OpenSM still runs. For each target, the new leader records its
fence intent, the node, the step and the switch ports, with a term
compare-and-set before it acts, so that a successor can finish or undo it.
It then climbs a ladder of the steps that are enabled, in order.

Each step is behind its own flag, and only step 1 is on by default:

| Step | Flag | Default | Confirms | Needs |
| --- | --- | --- | --- | --- |
| 1. Ask | `[ha] fence_ask` | on | never | the SM_Key |
| 2. Stop it on the host | `[ha] fence_container` | off | yes | the fencing SSH script on every node |
| 3. Disable its switch ports | `[ha] fence_switch_ports` | off | yes | M_Key protection, and every node's static map |
| 4. Power it off | `[ha] fence_power` | off | yes | a future improvement; BMC settings |

With a confirming step enabled, the target leaves the set once one of them
confirms. With none enabled, as by default, it leaves the set unconfirmed:
after step 1, if enabled, and once `T_watchdog` has passed since the lease
expired, by when the target's watchdog has killed its OpenSM if the
watchdog can run at all (see Unconfirmed takeovers, with the ladder).

The steps:

1. **Ask.** Send the target's SM a SubnSet(SMInfo) with the DISABLE modifier
   and the SM_Key, which a live OpenSM honours by going to NOT-ACTIVE. It
   can't be confirmed, since a frozen SM neither honours nor answers it, so
   it never counts on its own.
2. **Stop it on the host.** Over SSH to the target's published address,
   with a key restricted to a forced command, a small script with two
   subcommands, `kill` and `status`: kill the target's OpenSM container,
   and confirm through the host's process table that no OpenSM process
   runs on the host, in any container or outside one, such as a
   distribution's `opensm` service. Under cgroup v2,
   `docker kill` stops a paused container at once, as checked on the test
   host. This needs the target to be reachable and running.
3. **Disable its switch ports.** With the M_Key, send SubnSet(PortInfo) to
   every switch port cabled to one of the target's adapters, setting it to
   Disabled, and confirm by querying that it is disabled and its link down.
   The switch acts on it in its own firmware, whatever the target is doing,
   and the target can't undo it, having no path to the switch. This covers
   a paused or hung node, and one cut off from the management network.
   - **Which ports:** those whose neighbours, in a fabric scan, have one of
     the target's adapters' node GUIDs, cross-checked with the static map.
     A port whose link is already down, so that its neighbour can't be read,
     is disabled if the static map names it.
   - **Never the wrong one:** a port is never disabled if its neighbour is
     one of the fencer's own adapters, or a live node's, one whose heartbeat
     is current and that isn't in the target set, so that a
     fencer can't cut itself or a healthy peer off, even if adapters moved
     between hosts.
   - **Lasting:** while the target is fenced, the leader asserts and checks
     that its ports are still disabled every `check_interval`, and after
     every heavy sweep, since a switch reboot or its management can enable
     them again.
4. **Power it off.** A future improvement, not in the first release.
   Optional, and off by default: only for a node whose
   BMC settings are configured, with `[ha] fence_power = true`. Through
   the target's BMC, with IPMI or Redfish, as Pacemaker's `fence_ipmilan`
   and `fence_redfish` do, and confirmed by querying the power state. It
   must power the node off, not reboot it, since a booting node could start
   an OpenSM of its own. It is tried only once steps 2 and 3 have failed,
   since it stops everything on the node, not just its InfiniBand ports.

The switch ports are the main fence: step 3 covers a node that is paused,
hung or cut off from the management network, and touches only its
InfiniBand ports. Step 4 adds the case step 3 can't handle, a target whose
switch can't be reached or won't take the change, at the cost of BMC
credentials on every manager. Without it, such a target is left to the
operator, below.

Steps 3 and 4 are never used against the fencer's own node: a new leader
that is also in the target set, for example because its manager restarted,
counts its own old OpenSM as stopped only on step 2's check, run locally.

**Where to reach a node.**

- **The address.** Each instance's configuration names the address fencers
  reach it on: `[ha] ssh_address`, an IP address or name on the management
  network, and `[ha] ssh_port`, 22 by default.
- **Published in its node row.** The instance publishes them in its node
  row when it registers and with every heartbeat, so a changed
  configuration takes effect without touching the others. It publishes its
  SSH host key with them, read from the host's key file, so that fencers
  pin it rather than trust on first use.
- **Read from the database.** A fencer reads the target's address and key
  from the database, never from its own configuration.
- **Checked at start.** An instance refuses an `ssh_address` that resolves
  to one of its InfiniBand interfaces, since fencing disables those. HA
  mode with `fence_container` on refuses to start while it isn't set.

Fencing a node stops its InfiniBand traffic, its own storage or workloads on
the fabric included, until it is unfenced. Every step must be idempotent,
since a successor repeats it.

With a confirming step enabled, if none confirms within
`[ha] fence_timeout`, the takeover doesn't start OpenSM:

- the new leader keeps the lease, retries the ladder and raises an alarm;
- an operator can confirm a fence with `--confirm-fenced` (see Manual
  fencing), after checking that the target's OpenSM can't act;
- the fabric runs on the target's OpenSM, if it still runs, or without a
  subnet manager, until then.

HA mode refuses to start unless each enabled step has what it needs:

- step 2: the fencing SSH script is installed, and this instance's
  `ssh_address` is set and not on an InfiniBand interface; a target whose
  row has no address can't be fenced by step 2;
- step 3: M_Key protection is on, at protection level 1 or more, so that
  only the managers can disable switch ports, and every node's static map
  is configured;
- step 4: refused until it is implemented.

`[ha] fence_manual = true` makes a takeover wait for an operator to
confirm the fence with `--confirm-fenced` when its enabled confirming steps
fail. With no confirming step enabled and `fence_manual` off, HA mode logs
a warning at start, and the health check reports it.

#### Manual fencing

Still to come, with `fence_manual`. The operator fences the target
themselves, by stopping its OpenSM or its node, or disabling its switch
port, and then confirms it:

```sh
alternative-fabric-manager --config-file … --confirm-fenced <node>
```

- **It names a node**, since the target set can hold several entries; the
  operator gets the node from `--status`, which lists the target set's
  entries waiting for a fence, each with its node, instance and term, and
  what each enabled step last reported.
- **It confirms what it read.** It reads the node's entries, and records
  the confirmation as a compare-and-set naming each entry's instance and
  term, as `--force-unfreeze` names the frozen holder's: an entry added
  since, such as the node rejoining with a new instance and becoming a
  target again, isn't covered, and the command says so and exits non-zero.
  The leader then removes each confirmed entry from the target set as
  "manual", as it would for a confirming step.
- **Not for a held entry**, a frozen leader's: the command refuses, and
  points to `--force-unfreeze`, since a freeze has its own two-phase rules
  (see Freeze).
- **Nothing checks the claim.** The help and `--status` say what the
  operator must check first: that no `opensm` process runs on the target
  node, or that the node is off, or that its switch port is disabled.
- **Rejoining:** a node whose fence was confirmed by hand rejoins as one
  that left the target set unconfirmed does (see Rejoining), since the
  manager can't tell what the operator did to it.

It is a separate command from `--force-unfreeze`, which ends a freeze and
names no node: `--force-unfreeze` stays for freezes only.

#### Unconfirmed takeovers, with the ladder

By default no step confirms, so a takeover relies on the old leader
fencing itself, and on step 1:

- **A crashed manager:** its OpenSM died with it, through the PID
  namespace.
- **A paused or hung manager:** its watchdog stops renewing, and kills
  OpenSM within `T_watchdog`.
- **A leader cut off from the management network or Galera, or in a
  minority partition:** it stops its own OpenSM within `T_self`, before a
  candidate on the majority side can take over (see The lease, Self-stop).
  If that somehow failed, step 1 reaches its OpenSM in band, and a live
  OpenSM honours DISABLE by going to NOT-ACTIVE; failing that, the new
  leader's OpenSM starts in STANDBY or the fabric check finds another
  master, so the start fails, no job completes, and an alarm is raised.
- **Not covered: a paused node, or a paused OpenSM container.** Nothing on
  it runs, its watchdog included, so its OpenSM can resume as master
  before its watchdog kills it, and sweep a stale partitions file until it
  notices the new master; it can undo a completed job, such as put back a
  deleted port, for that time. Enabling step 2 or step 3 covers it.

So the defaults suit sites that accept that risk, or that can't give the
managers SSH to the OpenSM hosts or M_Key-protected switch ports;
production sites should enable step 2, step 3, or both.

The keys are then in Galera and its backups, as well as in OpenSM's files
and log, so the backups need the same protection.

#### Unfencing

- **A node that left the target set unconfirmed, or by step 2 alone,** has
  no disabled ports. It needs only steps 1 to 4 and 6 below. The leader
  clears its fence record, with a term compare-and-set, once step 4 has
  verified it, so that its OpenSM is known stopped before it becomes a
  candidate again.
- **A node whose ports step 3 disabled** needs every step.

A fenced node's switch ports stay disabled until its OpenSM is known stopped
and unable to start by itself:

1. Its manager comes back and registers, with a new instance UUID, and sees
   that it doesn't hold the lease. A manager that finds it lost the lease
   without restarting does the same, with a new UUID.
2. It tells its watchdog to stop OpenSM, if it still runs, for example after
   a network partition rather than a crash, and records that OpenSM is
   stopped. The watchdog only starts OpenSM on a single-use start request
   for a term the node holds, so OpenSM can't come back by itself.
3. It asks to be unfenced, in its node row.
4. The leader checks independently, with step 2's `status`, that no OpenSM
   runs on the node's host, and that its configuration's hash is allowed.
5. The leader enables the node's switch ports, confirms that they are up,
   and its OpenSM brings the node's ports to Active and sets their pkey
   tables. The node's fence record is cleared with a term compare-and-set.
6. The node becomes a candidate again.

If the leader can't verify, the ports stay disabled until an operator
unfences the node with the CLI.

### Coordination drivers

Other drivers for the lease's exclusion, where a site runs the service;
each must meet the guarantee in The lease:

- **etcd:** a lease with a TTL that etcd expires itself, kept alive by the
  leader, and the leader key created in a transaction conditional on its
  absence. Expiry no longer needs the candidates' counter-watching. The
  holder times self-stop on its own clock from when it sent the last
  keep-alive that etcd acknowledged, with `T_self` against the TTL as
  against `T`, since a client may report a failed keep-alive late. etcd
  must be a version whose lease renewal is linearizable, so that a leader
  cut off in an etcd minority can't renew; which versions those are must be
  checked. The `etcd3` client may be used directly: tooz's lock API may not
  expose what this needs.
- **Redis:** not suitable as it stands. With Sentinel or Cluster, Redis
  replicates asynchronously, so a failover can lose the lease key, and a
  successor acquire it at once, before the old leader's self-stop or
  watchdog has stopped its OpenSM: two masters, whatever the term in
  Galera, since Galera doesn't fence OpenSM. Redlock doesn't remove that,
  and a single Redis without failover is a single point of failure.

With a driver other than Galera, the lease and the state are in different
systems:

- **Losing the lease store is the safety rule:** a holder that hasn't
  renewed for `T_self` stops its OpenSM, as with Galera. `T_self` must
  then also exceed the lease store's own failover time, and the failure
  table needs rows for its outages.
- **Losing Galera is a liveness rule:** while the leader holds the lease,
  nobody else can take over, but no job can complete. A leader that can't
  write to Galera for `T_self` stops and verifies its OpenSM, then releases
  the lease, so that a candidate that reaches Galera can lead.
