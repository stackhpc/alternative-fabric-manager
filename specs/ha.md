# High availability

Status: accepted for implementation, 2026-11-05.

Run a fabric manager instance on every OpenSM node, behind HAProxy, with the
shared state in the deployment's Galera cluster. Exactly one instance, the
**leader**, runs OpenSM at a time. The others serve the API and stand by to
take over. Leadership is a lease in Galera: Galera's quorum is the only
consensus, and there is no Raft.

Running one OpenSM, rather than OpenSM's own master and standbys on every
node, means no standby can take over with partitions that nobody applied,
and a job's evidence is one OpenSM's sweep. The cost is that a takeover
starts an OpenSM rather than promoting a running one: until it has swept,
there is no subnet administrator (SA), so new connections, IPoIB joins and
ports coming up wait, and its multicast groups are rebuilt.

Three principles carry the safety argument:

- **A job completes only on evidence that the fabric has its effect:** the
  current leader's OpenSM swept a body in which the job's effect holds, in
  the leader's current term, while the leader saw the whole fabric and no
  other subnet manager that could master or set ports.
- **A new leader starts OpenSM only once every OpenSM that may have run
  since the last verified stop is stopped:** it was released cleanly, its
  own node verified it stopped, or, unconfirmed, it was asked to stop, if
  `fence_ask` is on, and `T_watchdog` has passed since the lease expired, by when its self-stop or
  watchdog has killed it if either can run. A paused node is the known
  exception, until confirmed fencing (see Fencing).
- **When in doubt, wait:** only the majority side of a Galera partition can
  elect a leader, a leader that can't renew stops its OpenSM, and no job
  completes while the fabric check sees another master.

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

HAProxy sends each request to any of the three instances, which are all
active backends, round robin with no session stickiness. Each instance
records requests in Galera and answers polls from it. Only the leader
renders the partitions file and runs OpenSM. No other managed subnet
manager may run on the fabric, nor one embedded in a switch, and the leader
checks after every sweep that no other SM could master or set ports (see Job
completion).

The cluster's own control traffic, Galera and the management network, must
not run over the InfiniBand fabric that OpenSM manages: losing the subnet
manager would then cut the managers off from Galera too, and every leader
would stop itself. The deployment role checks this.

## Shared state

| Table | Holds |
| --- | --- |
| `networks` | Network ID, type, pkey (unique, NULL for flat networks), name |
| `ports` | Port ID, network (foreign key), GUID (unique), host |
| `state` | The `generation` counter; the schema version; the configuration epoch, the configuration hashes it allows, and the cluster settings (see Configuration) |
| `jobs` | Operation, object, the GUIDs and pkeys it involves, generation, effect, deadline, status, error, timestamps (UTC) |
| `lease` | One row: the leader's node and instance UUID, its term, its renewal counter, whether it was released, the **target set** (see The lease), when leadership last moved, and the **freeze**: whether it is frozen, the frozen leader's node, instance UUID and term, who froze it, when, and when it expires (see Freeze) |
| `nodes` | Per node: name, instance UUID, heartbeat counter, configuration hash, the number of expected switches it reaches, its adapters' node GUIDs, and whether its OpenSM is verified stopped (see Fencing, Rejoining) |
| `applied` | The leader's last swept generation, its body, which object each partition and membership in it comes from, the GUIDs whose pkey tables OpenSM couldn't set, the switch ports whose enforcement OpenSM cleared, the leader's term, and the last fabric check's result and time |
| `quarantine` | Objects left out of rendering, and why |
| `expected_switches` | The switch GUIDs a complete fabric has (see Job completion) |
| `deferred` | A request whose pkey or GUID another object still holds, or a port on a network that waits or is quarantined, its values and its job |

networking-mlnx orders requests per object only, so a network reusing a
deleted network's VLAN, or a port taking over a deleted port's GUID, can
arrive before the deletion. Such a request is not refused, which
networking-mlnx would take as final: its job is created at once, and the
request is applied when the deletion arrives, or its job fails at its
deadline. A later request for the same object supersedes it. A port on a
quarantined network waits in the same way, until the network's next request
releases it. A genuine clash, such as one VLAN on two physical networks,
therefore fails at the deadline rather than at once, and networking-mlnx
retries it until an operator resolves it.

A request's transaction starts by bumping the generation, with
`UPDATE state SET generation = generation + 1`, which locks the row. Only
then does it read and validate, with locking reads, so that its checks see
every change committed before it. It validates against the configuration
stored in Galera, such as the pkeys that the template's static partitions
reserve and `flat_pkey`, not the instance's own, so that every instance
accepts the same requests. It then records the change and creates its job
with that generation, its effect, and its deadline, its creation time plus
the job timeout, so that every instance agrees on when it times out. Every
write goes to one Galera node (see Galera specifics), so requests are
serialised on the generation row, and a job's generation is exactly the
first state that includes it.

A DELETE of a network or port that isn't recorded still bumps the
generation and creates a job, with its identity taken from the request's
body, which networking-mlnx sends on DELETE as on create. A job records the
GUIDs and pkeys it involves when the request arrives, since a network's
deletion removes its ports from the tables before the job is checked.

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

Every effect also needs the swept generation to be at least the job's. So a
delete completes once a sweep of a state at or after it has nothing from its
object, whatever else holds the same pkey or GUID by then. A port created at
G and deleted at G+1 never has a membership attributed to it in a swept
body, so its create times out and fails, while its delete completes.

## The lease

The lease row names the leader and its **term**, a counter that only goes
up. Every write a leader makes to Galera, such as a record of what it
swept or which targets left the set, is a compare-and-set on its term, so a
deposed leader's late writes are refused. The term fences only Galera: a
running OpenSM sweeps without asking anyone, so OpenSM itself is fenced by
the target set, self-stop and the waits in Fencing.

- **Renewal:** the leader increments the lease's renewal counter every few
  seconds, matching its instance UUID and term, on a thread of its own that
  runs through applies and takeovers alike. It renews only under the same
  progress condition as the watchdog's liveness thread (see The watchdog):
  if a loop hangs, the Galera lease lapses with the watchdog's, so
  a candidate takes over, rather than the fabric being left without a
  subnet manager under a lease that is still renewed; the renewal thread
  also asks the watchdog to stop OpenSM. A renewal that matches no row
  means it lost the lease: it stops OpenSM at once (see Stepping down).
- **Only a Primary component counts.** Every lease read and write runs in
  a transaction that first checks `wsrep_cluster_status = 'Primary'` and
  `wsrep_ready = ON` on the node serving it, and counts as failed
  otherwise. So only instances that reach the majority side of a Galera
  partition can renew, watch or take the lease: the majority elects a
  leader, and the minority can't.
- **Self-stop:** the leader times its renewals on its own monotonic clock,
  from when each was sent. If none has succeeded for `T_self`, shorter than
  `T`, it stops OpenSM itself through the watchdog, which needs no
  database, and verifies locally that no OpenSM runs (see Stepping down).
  A leader in a minority partition, or cut off from Galera, so stops its
  OpenSM before any candidate on the majority side can see the lease
  expire, since that takes successful reads spanning `T` after its last
  renewal. That is self-fencing.
- **Expiry:** every other instance watches the renewal counter on its own
  monotonic clock. The lease has expired once successful reads spanning `T`
  all show the same counter, or once it was released. A failed read resets
  the timer, so that a database outage, which stops renewals and reads
  alike, never makes a lease look expired when the database returns.
  Nothing compares the database's clock with any other. Safety doesn't rest
  on these reads, though: a read from a lagging node could show a stale
  counter and so a false expiry, but the takeover's compare-and-set
  includes the counter it saw, so it matches no row, or Galera's
  certification aborts it. Lease reads use `wsrep_sync_wait = 1`, as every
  session does, so false expiries are rare.
- **The target set** holds an entry for every instance that may have run
  OpenSM since the last stop that was released or verified: its node, its
  instance UUID, and the term in which it was added. A takeover never
  removes an entry, except with a term compare-and-set that records that
  the instance released the lease, that its node verified its stop after
  the entry was added, or that it left unconfirmed (see Fencing). So a
  takeover whose winner dies midway leaves its targets, and itself, for the
  next.
- **Taking the lease:** a candidate first checks that its own fabric port's
  physical link is up, its logical state Init or above (Active is not
  required, since only a subnet manager makes a port Active, which would
  deadlock a cold start), and its neighbouring switch port not disabled. It counts the
  expected switches it can reach with directed-route SMPs, and publishes
  the count in its node row every few seconds. Only a candidate that reaches
  every expected switch may take the lease, or, once no candidate has for
  `T_reach`, the one that reaches most. It then takes the lease with a
  compare-and-set that includes the counter it saw unchanged, setting itself
  as leader with the term incremented, adding an entry for itself to the
  target set, and clearing its node's verified-stop mark, all in the same
  compare-and-set, so that no earlier mark can let it leave the set later.
  The same compare-and-set removes older entries for its own node, with
  other instance UUIDs, but only where its node's verified-stop term, read
  in that compare-and-set, is at least the entry's term, so that an entry
  added after the verification, such as by a takeover whose outcome was
  unknown, stays. Only the winner proceeds. It
  waits for every other entry to leave the target set (see Fencing), and
  only once none is left but its own starts OpenSM. Before it writes the
  start request, it checks that its last renewal succeeded within
  `T_self`, and the start request carries the deadline `t0` + `T_self`, past
  which the watchdog refuses it, so that a winner that stalls after its
  wait can't start OpenSM once it may have lost the lease.
- **Releasing the lease:** a leader that stops cleanly steps down: it stops
  OpenSM, verifies through the host's process table that no OpenSM runs,
  removes its own entry from the target set and marks the lease released,
  with a term compare-and-set. A successor can take it at once, and starts
  OpenSM at once if no other entry is left. A release removes only the
  releaser's entry: if others remain, for example because the releaser was
  itself waiting on an unconfirmed target, the successor treats the
  release as the expiry, so it asks them again and waits `T_watchdog` from
  when it took the lease.
  - **On SIGTERM**, as from `docker stop` or a host shutdown, the manager
    fails `/healthz` at once, so HAProxy stops sending it requests, steps
    down without waiting for that, and exits; it serves requests until it
    exits. The step-down has a bound, `[ha] shutdown_timeout`, 20 s by
    default. The container is created with a stop timeout above it, 30 s,
    as `--stop-timeout` or StopTimeout, since dockerd stops containers at a
    host shutdown with the timeout set when they were created, 10 s by
    default, not with `docker stop -t`. If the database can't be reached
    within it, the manager still stops OpenSM and verifies it, but exits
    without releasing the lease, so a successor waits for the lease to
    expire and the unconfirmed wait; that is safe, since its OpenSM is
    verified stopped. If the manager is killed anyway, OpenSM dies with it,
    since the manager is PID 1 of the namespace OpenSM's container joins.
  - **A candidate** that stops cleanly marks its node row as stopped, so
    that it isn't counted as a live candidate.
  - **After a self-stop**, once the leader reaches a Primary component
    again, it releases the lease the same way, if it still holds it, and
    removes itself from the target set, since it verified its OpenSM
    stopped. If a successor has taken the lease meanwhile, it is a
    candidate like any other.

A takeover whose own commit outcome is unknown counts as taken: the
instance adds itself to the target set before it can know, so a successor
waits for it as for any target.

A candidate's configuration hash must be one the current configuration
epoch allows, and its OpenSM must be verified stopped.

The lease's exclusion sits behind a small coordination driver interface:
acquire it, renew it, release it, and watch it for expiry. Every driver
must guarantee that no other instance can acquire the lease, or see it
expire, before `t0` + `T`, where `t0` is when the holder sent its last
renewal that was acknowledged, with expiry timed from when the first read
showing that renewal returned, not from when it was sent, and that only a
quorum of the driver's
store can grant or renew it. Self-stop is timed on the holder's monotonic
clock from `t0`, whatever the driver. The term, the target set, and the
outcome-unknown rule stay in Galera whatever the driver: a takeover first
acquires the lease from the driver, then, in one Galera compare-and-set
conditioned on the term it read before acquiring, bumps the term and adds
its target-set entry, before it does anything else, so that the term never
repeats and Galera refuses a lower term's writes. An acquirer that hasn't
done so by `t0` + `T_self` of its own acquisition abandons the attempt,
since its lease may have lapsed. Galera is the first driver, since every Kolla deployment has it;
others are a future improvement (see Coordination drivers). Jobs, objects
and the applied state stay in Galera too.

## Requests and applying

1. HAProxy sends networking-mlnx's request to any instance. The instance
   records it whenever Galera is up, leader or not, and answers with the
   job's path. A job's status is always answered, never with 503, since
   networking-mlnx answers anything but a timeout on a job poll by sending
   its request again. The exception is a Galera outage, when every
   `/healthz` fails and HAProxy itself answers 503: networking-mlnx then
   sends its requests again, which, with its default unlimited
   `retry_count`, only delays them. While the cluster is frozen, new
   requests are answered with 503 too, but job polls still get their
   status (see Freeze).
2. The leader polls the generation, every second or so, and is woken at once
   when it took the request itself.
3. When it is behind, it reads the generation and the whole state in one
   consistent-read transaction, renders the partitions file without the
   quarantined objects, checks it (see Configuration), writes it atomically
   and fsyncs it, sends OpenSM SIGHUP, and waits for the sweep that starts
   after the signal to end (see OpenSM errors).
4. After the sweep, it checks the fabric (see Job completion), then records
   the generation, the body, its attribution and the GUIDs with pkey errors
   in `applied`, with a compare-and-set on its term.

Applying is level-triggered: the leader always renders the newest state,
never replays changes. The file's header carries the generation and an
attempt counter, so that a retry still changes the file and OpenSM runs a
pkey sweep. Every `check_interval`, the leader also reconciles: if its file
isn't what it last applied, or the state rendered at that generation
differs, it applies again.

### Quarantine

State is cumulative, so an object that makes a render fail would otherwise
fail every later change, deletes included. So a failure is attributed to an
object, and only that object is left out:

- a check of the rendered file names the network or port whose partition or
  membership broke it;
- an OpenSM parse error names a line, which holds a partition's definition
  and up to 20 of its memberships; the objects on that line that changed
  since the last sweep are blamed, and if none did, it is a failure that
  can't be attributed.

The leader quarantines the object, fails its jobs, and applies the rest
again. An object is released from quarantine by its next change or its
deletion. If a failure can't be attributed, for example a template error,
the leader keeps the last body it swept, fails the jobs of that generation,
and raises an alarm; validating requests against the stored configuration
makes that a template problem, not a request one.

### OpenSM errors

OpenSM skips lines of a partitions file it can't parse and still finishes
the sweep, and it logs, rather than fails on, a port whose pkey table it
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

Most of these name the port only by its node's description (0502, 0503,
0505, 0506, 0508, 0512), or by its node GUID and port number (0509, 0511),
or name none (0514), not by its
port GUID, and 0F12 names nothing. Only 1503, 5703 and 6B06 give a port
GUID, and the MAD layer's 3113, 3119 and 3120 name a destination by a GUID
that needn't be the port's. An error that can't be traced to a port GUID
fails closed: it is
recorded as unattributed, and no job of that sweep completes; the leader
sweeps again while a job waits on it. So does a 0507 or 0513 that names no
switch port.

A pkey error on a port's GUID is recorded in `applied`, so jobs whose
effects involve that GUID don't complete, and the leader retries at the next
reconcile. It doesn't count towards stepping down: one bad adapter mustn't
move the leadership. Codes outside this list, such as the SA's `ERR 45xx`
for requests between ports that share no pkey, are about clients, not about
applying the file. The list is matched by code, `ERR 0507:` for example; a
newer OpenSM's codes need checking before it is supported.

## Job completion

networking-mlnx holds a port DOWN until its job completes. A job at
generation G is **Completed** when, in `applied`:

1. the leader's term is the current term;
2. the swept generation is at least G;
3. the job's effect holds in the swept body, and no GUID it involves had a
   pkey error in that sweep;
4. the leader's last fabric check, recorded in `applied`, passed, was
   taken after that sweep, and is no older than `2 × check_interval`;
5. the sweep is still valid: OpenSM is still running on the file it swept,
   and the sweep started after a passing check that followed the last
   check to see another master or the leader's OpenSM not master, which
   `applied` records with the sweep, so that every instance, not only the
   leader, can judge a job. A leader clears the last check before it stops
   OpenSM; after a crash, other instances can still judge a job on the
   last sweep for up to `2 × check_interval`, which is safe, since its
   effect was swept and checked.

Recording a sweep clears the last check, so that a sweep is judged only by
a check taken after it. OpenSM's first sweep after it starts, a
takeover's included, counts as a sweep, unless the last recorded check,
whatever its term, was contested: then the leader waits for a
passing check and sweeps again with SIGHUP. A check that sees another master, or the leader's
OpenSM not master, invalidates the sweep, since the other SM may have set
pkey tables meanwhile, and could undo a sweep made while it acts: the
leader makes no sweep while the last check was contested, sweeps again
only once a check passes, and jobs complete only on that sweep and a check
after it. Whether OpenSM's
pkey sweep resets pkey tables that it believes are already right, or a
heavy sweep is needed, is part of the two-OpenSM test.

The **fabric checks** run after every sweep and every `check_interval`:

- **Complete topology:** OpenSM discovered every switch in
  `expected_switches`. That list is configured at deployment, or learnt
  from sweeps and confirmed by an operator, so that a leader on a fragment
  of a split fabric, or one whose own link is down, doesn't count its
  fragment as the fabric.
- **No other subnet manager that matters:** the leader looks at the SMs
  its own SA knows, by their SMInfo records, and OpenSM's log since the last
  check, and sorts them by port GUID and by where they sit:
  - another managed node's SM, one whose port GUID is one of a managed
    node's adapters', fails the check, and the leader asks it to stop, as in
    Fencing, step 1, and raises an alarm, since it may have the fabric's
    keys;
  - an unknown SM, such as one a bare metal tenant runs, fails the check
    only if it is master or the leader's OpenSM went to STANDBY. Without the
    SM_Key and M_Key, which HA mode requires (see Fencing, Required keys),
    it can't win a handover or set ports, so otherwise it only raises an
    alarm. How OpenSM 5.17 treats such an SM is part of the
    two-OpenSM hardware test.

A failed check raises an alarm, and no job completes until a check passes
again.

A job is **Failed** once its object is quarantined, or once the database's
UTC time is past its stored deadline, and it isn't Completed. Otherwise it
is **Pending**. Status is computed when a job is read, in that order, and
written to its row the first time it is terminal, with
`UPDATE ... WHERE status IN ('Pending', 'Running')`; a reader whose update
matches no row returns the stored status.

A request whose commit outcome is unknown, and which networking-mlnx sends
again, creates a second job with the same effect. That is harmless.

## Fencing

Before a new leader starts OpenSM, every other node in the target set must
be unable to act on the fabric. The first release has no fence that
confirms this without the target's cooperation; the ladder that would,
killing OpenSM's container over SSH, disabling the target's switch ports
and powering it off, is a future improvement (see Confirmed fencing).
Instead, a target leaves the set, with a term compare-and-set, once:

- **it stopped cleanly:** it released the lease, having verified that its
  OpenSM stopped (see Stepping down);
- **it verified its own stop:** after a self-stop or a restart, its manager
  stopped OpenSM through its watchdog, verified through the host's process
  table that no OpenSM runs, and recorded that in its node row, with an
  instance UUID other than the entry's and the lease term read in the same
  transaction, which must be at least the entry's term (see Rejoining);
- **or, unconfirmed:** the new leader has asked it to stop, step 1 below,
  if `fence_ask` is on, and `T_watchdog` has passed since the lease expired,
  or since the successor took a released lease, by when the target's
  self-stop or watchdog has killed its OpenSM, if either can run at all.

1. **Ask.** Send the target's SM a SubnSet(SMInfo) with the DISABLE modifier
   and the SM_Key, and the fabric's M_Key, since the target's port is
   M_Key protected, which a live OpenSM honours by going to NOT-ACTIVE. It
   can't be confirmed, since a frozen SM neither honours nor answers it, so
   it never counts on its own. `[ha] fence_ask`, a cluster setting, is on by
   default. It is best effort: the new leader sends it by directed route,
   from its reachability scan, to every port of the target's adapters, by
   their node GUIDs in the target's node row, and a send that times out,
   or that has no route, as for a crashed or powered-off target, counts as
   asked.

`[ha] manual_takeover = true`, a cluster setting, makes every takeover wait
for an operator to confirm with the CLI that the old leader's OpenSM can't
act, instead of the unconfirmed wait. A clean release and a verified stop
still count without an operator.

### Why the unconfirmed wait is enough

Let `t0` be when the leader sent its last renewal that succeeded. No
candidate sees the lease expire before `t0` + `T`, and the unconfirmed start
comes `T_watchdog` after that. Meanwhile:

- **A crashed manager:** its OpenSM died with it, through the PID
  namespace.
- **A manager cut off from Galera, or in a minority partition:** it stops
  its OpenSM by `t0` + `T_self` + the time to stop it, before `t0` + `T`
  (see The lease, Self-stop).
- **A paused or hung manager:** its liveness thread stops renewing the
  watchdog's lease by `t0` + `T_self` + its period + a call timeout, which
  is less than `t0` + `T`, so the watchdog kills OpenSM by `t0` + `T` +
  `T_watchdog`, before the new leader starts. That needs every node's
  watchdog to use the same `T_watchdog`, so it is a cluster setting that
  each instance passes to its watchdog, and `T_self` + the liveness
  period + the call timeout < `T`.
- **A leader whose self-stop and watchdog both failed:** step 1 reaches its
  OpenSM in band, and a live OpenSM honours DISABLE. Failing that, the new
  leader's OpenSM starts in STANDBY, or the fabric check finds another
  master, so the start fails, no job completes, and an alarm is raised.

**Not covered: a paused node, or a paused OpenSM container.** Nothing on
it runs, its watchdog included, so its OpenSM can resume as master before
its watchdog kills it, and sweep a stale partitions file until it notices
the new master. For that time it can undo a completed job, such as put
back a deleted port. The new leader's fabric checks find the other master,
so no job completes meanwhile, and an alarm is raised. This is the first
release's known limitation; confirmed fencing removes it.

### Rejoining

A node that left the target set unconfirmed rejoins as a candidate once
its OpenSM is known stopped and unable to start by itself:

1. Its manager comes back and registers, with a new instance UUID, and sees
   that it doesn't hold the lease. A manager that finds it lost the lease
   without restarting does the same, with a new UUID.
2. It tells its watchdog to stop OpenSM, if it still runs, for example after
   a network partition rather than a crash, removes the watchdog's lease
   and any start request from the tmpfs, since a crashed manager leaves
   them behind with no watchdog to act on them, verifies through the
   host's process table that no OpenSM runs, and records that in its node
   row. The watchdog only starts OpenSM on a single-use start request for a
   term the node holds, so OpenSM can't come back by itself.
3. If its configuration's hash is allowed, it becomes a candidate again.

### Seeing the host's processes

The manager is PID 1 of its own PID namespace, which OpenSM's container
joins, so its own `/proc` shows only OpenSM's container. It verifies that
no OpenSM runs on the host, in any container or none, through the host's
`/proc`, mounted read-only into its container at `/host/proc`, looking for
any process whose executable is `opensm`.

### Required keys

HA mode needs the managed driver, which starts OpenSM itself, and refuses
the others: with no OpenSM to check, every check would pass. It also
refuses to start unless M_Key protection is on, at protection
level 1 or more, and OpenSM has a generated SM_Key: the `m_key` and
`sm_key` hardening groups. Without them, a tenant's SM could set ports or
win a handover, and step 1 has no key to authenticate with. The keys are
then in Galera and its backups, as well as in OpenSM's files and log, so
the backups need the same protection.

## The watchdog

OpenSM's container keeps joining the manager's PID namespace, so that OpenSM
dies with the manager whenever the manager exits. Its entrypoint is a small
watchdog, which starts OpenSM only on a start request, and kills it with
SIGKILL when its lease expires, when the manager asks it to stop, or when
OpenSM exits; then the watchdog exits too, so the container stops. The
container has no restart policy. The watchdog runs in its own container's
cgroup, so pausing the manager's container doesn't pause it.

- **The lease** is renewed by a liveness thread of the manager every
  period, 2 s, only while the manager's loops make progress: no iteration
  of a loop has run longer than that loop's bound. Each bound is the sum
  of the timeouts of the calls one iteration makes, plus a margin:
  - the **apply loop**, one apply attempt per iteration in HA mode, with
    retries in later iterations: a sweep, 30 s, a fabric check, 5 s, and
    up to five database transactions of 3 s, so `B` is 60 s. The HA apply
    loop must be built to that: at most five transactions and one sweep
    per iteration, so a re-render after a quarantine, or a rejected line,
    waits for the next iteration;
  - the **takeover loop**, which runs a takeover and OpenSM's start
    separately from the apply loop: Ask, 5 s, the unconfirmed wait,
    `T_watchdog`, and `start_timeout`, 60 s in HA mode, so `B_takeover` is
    120 s. Waits with no bound, for an operator under `manual_takeover`
    or an unacknowledged unfreeze, poll in iterations of a few seconds
    each;
  - the lease renewal loop: a call timeout, 3 s.

  A call that fails or times out still finishes its iteration. `T_watchdog` must
  exceed the period plus a call timeout, so that a healthy manager's
  watchdog lease never lapses; it is 45 s. Each renewal sets the deadline
  to the earlier of now + `T_watchdog` and `t0` + `T_self` + `T_watchdog`,
  from the same clock reading it checked `T_self` against, so that a stall
  between the check and the write can't extend OpenSM's life. A paused, hung or crashed manager stops renewing, and OpenSM
  stops within `T_watchdog`. The liveness thread also stops renewing once
  no lease renewal has succeeded for `T_self`, so that if the leader's own
  self-stop fails, the watchdog kills OpenSM anyway, within `T_self` +
  `T_watchdog`.
- **The lease and the start request** are on a tmpfs the two containers
  share, so they don't survive a reboot, tagged with the host's boot ID, the
  instance UUID and the term. The deadline is in the host's monotonic clock,
  which every container on the host shares. A start request is consumed
  when used, and a stop removes both files, so a later `docker start` of
  the container can't start OpenSM.
- **On SIGTERM,** as at a host shutdown, the watchdog kills OpenSM with
  SIGKILL and exits, rather than leave OpenSM to be reparented; the manager
  then verifies through the process table that no OpenSM runs.
- **OpenSM must start as master.** An OpenSM that enters STANDBY has found
  another SM, so the start failed: the leader stops it and treats it as the
  fabric check finding another SM.
- **Before starting OpenSM,** the watchdog checks the hashes of
  `partitions.conf` and `opensm.conf` against those the start request
  names: with no partitions file, OpenSM would make every port a full member
  of the default partition, and an old `opensm.conf` could have other keys.

The watchdog can't cover a pause of its own container, or of the node: a
frozen OpenSM can resume before the watchdog does. That is the first
release's known limitation (see Fencing).

The single-node manager starts OpenSM again whenever it stops. In HA mode it
doesn't by itself: an OpenSM that stops makes its leader step down, and a
new takeover, possibly by the same node, starts it, except for the shared
failure in Stepping down, when the leader starts it again itself.

### Freeze

An operator can freeze the cluster for maintenance, such as of Galera
itself: OpenSM stays on the current leader's node, and isn't stopped or
moved, until the cluster is unfrozen.

- **Agreement is Galera's, and the leader's.** `alternative-fabric-manager
  --freeze` writes a freeze request to the lease row, with a
  compare-and-set on the current term, on a Primary component, refused
  unless a leader holds the lease, has no other entry in the target set,
  has OpenSM running, and its last fabric check, in the current term,
  passed. The request carries a deadline, the database's
  `UTC_TIMESTAMP()` + `T_self`. The cluster is frozen only once the leader
  acknowledges the request, in the same row, with a compare-and-set that
  requires the request to be before its deadline, judged by
  `UTC_TIMESTAMP()` in that statement, which it does only while it is
  renewing normally. If no acknowledgement arrives, the command withdraws
  the request; and whether or not anyone withdraws it, a request past its
  deadline unacknowledged counts as withdrawn, in the acknowledgement's
  compare-and-set and in every takeover's alike, so a command that dies
  can't leave a pending request blocking takeovers. While a request is
  pending, every takeover's compare-and-set requires it to be withdrawn
  or past its deadline, and once the cluster is frozen, requires it not
  to be frozen, so no other instance
  can take over, whatever happens to Galera or the network meanwhile. A
  majority vote among the managers over another channel wouldn't do: a
  candidate outside that majority could still reach Galera's.
  `--drain` first waits, up to the job timeout, for open jobs to end.
- **The cluster can only be frozen while Galera is up**, and while the
  leader can renew: a leader that can't reach a Primary component may be
  on the minority side of a partition, so it can't decide to keep its
  OpenSM by itself, and a leader that self-stops never acknowledges.
- **The leader holds** only once it has read back, on a Primary component,
  that its acknowledgement committed. A leader that self-stopped meanwhile
  and then finds its acknowledgement committed holds as the frozen node,
  and starts OpenSM again, rather than release the lease, since a frozen
  leader never releases; an acknowledgement whose outcome is
  unknown doesn't count until then, and the leader keeps timing self-stop
  meanwhile, since the request may have passed its deadline. The frozen
  leader then stops timing self-stop, ignores every step-down trigger but SIGTERM, never
  releases the lease, and gives its watchdog a held lease, whose deadline
  is the freeze's expiry, if it has one, on the host's monotonic clock,
  plus `T_self`. OpenSM so survives a Galera outage or a hung manager. The
  leader keeps renewing, so that its `t0` is fresh at the unfreeze, and
  applying and checking, while Galera is up, and raises an
  alarm while frozen.
- **Requests wait.** Every instance reads the freeze within a poll
  interval, and then answers new requests with 503, which networking-mlnx
  retries, unlike 400. Job polls are still answered. Requests recorded
  before the freeze are applied as usual if Galera is up; ports created
  during a freeze stay DOWN, so Nova's wait fails an instance whose port
  waits longer than it.
- **Restarts on the frozen node.** OpenSM's container dies with the
  manager, through the PID namespace. While frozen, only the frozen node's
  manager may start OpenSM again, after a restart or after OpenSM exits.
  A restarted manager has a new instance UUID: it adopts the frozen lease
  with a compare-and-set that matches the frozen node and term, replaces
  the instance UUID, replaces the old instance's target-set entry with
  its own, and clears its node's verified-stop mark, as a takeover does,
  so that a stop it verified before adopting can't later let a candidate
  take over, or its entry leave the set, while its OpenSM runs; and only
  then starts OpenSM, with a start request for the frozen term that carries the
  held deadline rather than `t0` + `T_self`. Rejoining's removal of the
  watchdog's files doesn't apply to the frozen node while it is frozen.
  With Galera down, it may restart OpenSM from the watchdog's held lease
  only before that lease's deadline, never after the freeze may have
  ended, and it adopts the lease as soon as Galera returns. If the frozen node itself fails, the fabric has no subnet
  manager until an operator unfreezes the cluster: that is what a freeze
  trades.
- **Unfreezing is two-phase**, and needs Galera. `--unfreeze`, or the
  freeze's expiry, read by any instance that reaches a Primary component,
  marks the freeze as ending. The frozen leader acknowledges it, in this
  order, which is required: it gives its watchdog a lease with a deadline
  again, resumes timing self-stop, renews, and only then records the
  acknowledgement in the lease row. Only then is the cluster
  unfrozen, and every candidate restarts its expiry timer from that
  acknowledgement, treating it as a renewal. Until the leader
  acknowledges, no candidate takes over on expiry: a leader that never
  acknowledges, because it is paused, hung, or cut off, leaves the lease
  only through a verified stop of its node, or an operator's confirmation
  that its OpenSM can't act, as with `manual_takeover`; its watchdog's
  held deadline, if the freeze had an expiry, stops OpenSM meanwhile. A
  takeover while the freeze is ending is a compare-and-set that requires
  either an operator's confirmation recorded in the lease row, or the
  frozen node's verified-stop term at least the frozen term, and it
  clears the freeze in the same statement.
- **SIGTERM while frozen** stops OpenSM, since the manager is PID 1 of its
  namespace, but doesn't release the lease; the node's manager starts it
  again when it comes back. Shutting the frozen node down for good needs an
  unfreeze first.

### Stepping down

A leader steps down, by stopping OpenSM through the watchdog, verifying
through the host's process table that no OpenSM runs, and releasing the
lease,
when:

- its renewal matches no row;
- no renewal has succeeded on a Primary component for `T_self` (the
  self-stop: it can't release the lease then, so it releases it once it
  can, as in The lease);
- it gets SIGTERM, within `[ha] shutdown_timeout`;
- OpenSM exits;
- its configuration is no longer allowed, and a live candidate with an
  allowed configuration exists; otherwise it keeps leading and raises an
  alarm;
- a loop overruns its bound: it stops OpenSM, verifies, and releases the
  lease if it can;
- its fabric checks fail to answer, or OpenSM fails to answer them, for
  three `check_interval`s in a row, as a paused or wedged OpenSM under a
  healthy manager would, and a live candidate exists;
- its fabric checks find expected switches missing, and a live candidate
  reports reaching more of them;
- it can't write, signal or sweep, as opposed to a rejected body, for three
  `check_interval`s in a row, and a live candidate exists.

Failures that every node would have the same way never make a leader step
down: quarantine, pkey errors, template errors that can't be attributed, a
dead expected switch. OpenSM runs with `stay_on_fatal`, so that a fatal
fabric condition, such as a tenant's duplicated GUID, doesn't make it exit;
and if OpenSM exits on two successive leaders within the cooldown, that is
treated as a shared failure too: the leadership stays put, and the leader
raises an alarm and retries starting OpenSM. And the discretionary
triggers, a configuration no longer allowed, missing switches and failing
applies, don't move leadership again within `[ha] step_down_cooldown`, five
minutes say, of its last move, recorded in the lease row, so that a fault
can't bounce it between nodes, restarting OpenSM and the SA each time. The
cooldown never delays a lost lease, a self-stop, SIGTERM, a hung apply
loop, or OpenSM exiting.
A "live" candidate is one whose heartbeat is current and that isn't in the
target set. If the watchdog doesn't stop OpenSM, as when OpenSM's
container is paused with it, the leader kills OpenSM's container, which
stops a paused container too. If it still can't verify that OpenSM
stopped, it stops renewing both the watchdog's lease and the Galera
lease, and doesn't release the lease, so that a successor waits for it as
an unconfirmed target.

## Timeouts

A takeover takes up to `T` for the lease to expire, plus `T_watchdog` for
an unconfirmed target, plus starting OpenSM and its first sweep. A job in flight during a takeover must
not time out, or networking-mlnx sets its port to ERROR. So, with every
value a cluster setting in Galera, so that all instances agree:

- job timeout > the time a job can spend before the leader fails, the
  larger of a poll interval, a sweep (`sweep_timeout`) and a fabric check,
  and the apply loop's bound `B`, + `T` + the expiry poll's interval +
  `T_reach` + Ask + `T_watchdog` + OpenSM's start and first sweep
  (`start_timeout`, which in HA mode counts as the first sweep, with its
  pkey errors) + a fabric check;
- `T_self` + the liveness thread's period + a lease call's timeout < `T`;
- `T_self` > the deployment's MariaDB failover time, so that losing one
  Galera node, or the VIP moving, doesn't stop OpenSM;
- Nova's `vif_plugging_timeout` and Ironic's wait for ports > the journal's
  sync interval + its retry backoff + the job timeout.

With a 2 s poll, `sweep_timeout` 30 s, 5 s for a fabric check, `B` 60 s,
`T` 30 s, `T_reach` 30 s, 5 s for Ask, `T_watchdog` 45 s, and HA mode's
`start_timeout` of 60 s, the job timeout must exceed 60 + 30 + 2 + 30 + 5 +
45 + 60 + 5 = 237 s. HA mode's default job timeout is therefore 300 s; the
single-node default stays 180 s, since it has no takeover. Nova's
`vif_plugging_timeout` must then exceed 300 s plus the journal's sync
interval of 10 s and its backoff, so HA deployments set it to 420 s, and
Ironic's wait for ports likewise; the deployment role does both. A
deployment whose OpenSM takes longer to sweep must raise all three, and HA
mode refuses cluster settings that break these inequalities. `T_reach`, how long candidates wait for one that
reaches every switch, is 30 s. `T_self` is 20 s, with renewals and the
liveness thread every 2 s, and every lease call timing out within 3 s, so
that a call blocked on an unreachable database can't delay the self-stop:
20 + 2 + 3 < 30, leaving 5 s to stop OpenSM and for the clocks' drift. A
MariaDB failover behind Kolla's HAProxy, which notices a dead Galera node
in about 10 s, fits within `T_self`; a longer one stops OpenSM, as in
Failure scenarios.

A failed DELETE is retried by networking-mlnx, `retry_count` times, forever
by default, so with the relative delete effects it completes once a leader
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

Before writing a rendered file, the manager parses it and refuses to apply
it, quarantining the object that breaks it, unless all of these hold, as the
single-node manager already checks:

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

The configuration's hash covers everything that decides the partitions and
how OpenSM protects the fabric: the template, `default_partition`,
`flat_pkey`, `shared_default_partition`, the IPoIB settings, the hardening
groups, the keys, and the `opensm.conf` overrides, except node-specific ones,
such as the port GUID OpenSM binds to, and paths. The cluster settings, the
lease and job timeouts among them, are stored in Galera, not in each
instance's configuration. The hash is stored with an epoch, which allows one
hash, or two during a rolling change. Only an instance whose hash is
allowed may become leader; a leader whose hash is no longer allowed steps
down, once a candidate whose hash is allowed is live. A change is applied by
moving the leadership:

- **Rolling changes** are an allowlist in code: `sweep_interval`, routing
  and logging. The operator records a new epoch that allows both hashes,
  updates the candidates, then has the leader release the lease, as for an
  upgrade, and updates it, then records an epoch with only the new hash.
- **Everything else** is fabric-affecting, the keys included. The operator
  updates the candidates, then records an epoch with only the new hash, so
  that the leader steps down and an updated candidate takes over. Changing a
  key needs a key rotation procedure, which is future work: until then the
  manager refuses to start with keys other than the stored ones.

## Galera specifics

The manager uses the deployment's Galera through its usual database VIP, as
the OpenStack services do. SQLAlchemy makes the backend a
`[database] connection` URL, and SQLite stays for single-node deployments.

- **Writes go to one Galera node.** Kolla's HAProxy sends MariaDB traffic to
  one active backend. Every request bumps the generation row, so writes
  from several Galera nodes would conflict at commit.
- **Retry on deadlock.** Galera reports a certification conflict as a
  deadlock at commit. Retry the whole request transaction, validation
  included, as oslo.db does for OpenStack services.
- **Map constraint errors.** A pkey or GUID clash that reaches a unique
  constraint, or a port whose network is gone, is a 400, not a 500.
- **Read your own writes.** Every manager session sets
  `wsrep_sync_wait = 1`, so a read waits for writes already committed on
  other Galera nodes; Kolla's VIP fails over between Galera nodes.
- **UTC.** Timestamps and deadlines are `DATETIME(6)` values set and
  compared with `UTC_TIMESTAMP(6)`, so that no session's timezone or
  daylight saving change can age a job. SQLite uses its own UTC functions.
  Galera's nodes need synchronised clocks, within much less than the job
  timeout, since a deadline is judged by whichever node serves the read.
  Lease expiry doesn't use the database's clock.
- **Schema migrations.** Alembic, run by one instance at deploy time, rather
  than `create_all` at startup.
- **One instance per name.** An instance registers a UUID on its node row
  at start. A restarted instance takes over its name once the old UUID's
  heartbeat has been unchanged for `T`.

All three instances are active HAProxy backends, round robin with no
session stickiness, and HAProxy checks `/healthz`, which fails while the
instance can't reach the database.

## Upgrades

- **Rolling:** upgrade the candidates one at a time, then have the leader
  release the lease, which a candidate takes without a fence, and upgrade
  it. Schema changes must be compatible with the version before, so that
  old and new instances can share the database during the upgrade; a
  version that can't, says so, and needs every instance stopped. Each
  release of the lease restarts OpenSM on another node, so its SA state is
  rebuilt.
- **From single node:** with the manager stopped, which stops OpenSM too,
  so the fabric has no subnet manager meanwhile:
  1. copy the networks, ports and keys into Galera, and continue the
     generation above the single-node one, which the partitions file's
     header carries;
  2. record the single-node host as verified stopped, so that the first
     leader's target set is empty;
  3. copy the jobs, keeping their IDs, since journal rows that are
     monitoring a job would otherwise get 404 and send their requests
     again;
  4. keep terminal jobs as they are, and give every unfinished job,
     `Pending` or `Running`, the migration's generation, a deadline, status
     `Pending`, and its effect, which for a delete needs only its object's
     identity, by the relative rule.

  The unfinished jobs then complete, or fail, once the first leader has
  swept.
- **Job history.** Keep finished jobs for longer than networking-mlnx can
  poll one, a day, say, then purge them from any instance, since the purge
  is idempotent; a poll of a purged job gets 404, and networking-mlnx would
  send its request again.

## Failure scenarios

Every case fails closed: a port waits rather than becoming ACTIVE before the
leader's OpenSM has swept its membership with the whole fabric in view.

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
| `manual_takeover` set | A candidate takes the lease, asks the old SM to stop, and waits for an operator to confirm | The fabric runs without a subnet manager until then |
| A leader on a fragment of a split fabric | Its fabric checks fail: switches are missing. A live candidate that reaches more switches makes it step down, and takes over | Jobs wait through the takeover; alarm |
| An expected switch dies | Every node misses it, so nobody reaches more and leadership stays | No job completes until an operator removes the switch with the CLI; alarm |
| A cold start, every port in Init | Candidates need a link up and Init or above; Active isn't required | A candidate that reaches every switch takes the lease and starts OpenSM |
| A MariaDB failover | Failed reads reset the candidates' timers, and the leader's renewals fail meanwhile | Within `T_self`, nothing happens. Longer, the leader stops its OpenSM, and a leader is elected once a Primary component is back, so the SA is down meanwhile, and jobs in flight can time out and are resent |
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
- [ ] Database: a `[database] connection` URL, Alembic migrations, schema
      version, configuration epochs and cluster settings,
      `wsrep_sync_wait`, UTC timestamps, deadlock retry, the generation bump
      first in every request, constraint errors as 400.
- [ ] The lease, behind a coordination driver interface with Galera as
      its first driver: target-set entries of node, instance UUID and term;
      the verified-stop mark cleared on taking the lease; taking on a
      Primary component only, renewing, self-stop
      after `T_self`, releasing on step-down and SIGTERM, expiry on the
      monotonic clock, terms in every compare-and-set, and the target set.
- [ ] The manager's liveness thread, with each loop's progress bound,
      gating both the watchdog's lease and the Galera renewal; the host's
      `/proc` mounted read-only for verified stops.
- [ ] Cluster settings refused when they break the inequalities in
      Timeouts.
- [ ] The watchdog: an entrypoint for OpenSM's container, with the lease and
      single-use start requests on tmpfs, the file hashes, `T_watchdog` from
      the cluster setting, and SIGKILL of OpenSM on SIGTERM.
- [ ] Takeovers: SMInfo DISABLE behind `fence_ask`, the unconfirmed wait,
      `manual_takeover`, rejoining with a verified stop, and the required
      keys check.
- [ ] Freeze: `--freeze` (with `--drain` and `--for`) and `--unfreeze`,
      the takeover compare-and-set requiring not frozen, 503 for new
      requests, the leader's hold and the watchdog's held lease, restarting
      OpenSM only on the frozen node, and candidates' timers restarted at
      the unfreeze.
- [ ] A CLI to confirm a manual takeover, record configuration epochs,
      confirm or remove expected switches, and force a step-down.
- [ ] Tests: three instances against one MariaDB in CI, and a Galera cluster
      in containers, with faults injected: the leader killed, paused, cut
      off from the database or the network; a winner dying mid-takeover; a
      quarantined object; a deposed leader writing late; a fabric missing a
      switch. Among them:
      - the leader's Galera node partitioned into a minority: its OpenSM
        stops within `T_self`, the majority elects a leader, which starts
        OpenSM after `T` and the unconfirmed wait, and at no point do two
        OpenSMs run;
      - every Galera node down: the leader's OpenSM stops within `T_self`,
        and a leader is elected once a Primary component returns;
      - `docker stop` of the leader: the lease is released, the successor
        starts OpenSM within a few seconds, without waiting; and with the
        database unreachable, it still exits with OpenSM verified stopped;
      - the leader's manager paused: its watchdog kills OpenSM before the
        successor's unconfirmed wait ends;
      - frozen: Galera stopped, the leader's manager paused, and the
        leader's Galera node partitioned, with OpenSM kept on the frozen
        node throughout, new requests answered 503, and no takeover until
        the leader acknowledges the unfreeze, after which a takeover waits
        a full `T`; and an unfreeze while the leader's manager is paused,
        which leaves the lease until a verified stop or an operator's
        confirmation.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck, database credentials, the containers' stop timeout,
      expected switches, and the check that control traffic doesn't run over
      the InfiniBand fabric.

## Open questions

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

## Future improvements

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
   with a key restricted to a forced command, a small script with two subcommands, `kill` and `status`: kill
   the target's OpenSM container, and confirm through the host's process
   table that no OpenSM process runs on the host, in any container or
   outside one, such as a distribution's `opensm` service. Under cgroup v2,
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

**Where to reach a node.** Each instance's configuration names the address
fencers reach it on, `[ha] ssh_address`, an IP address or name on the
management network, and `[ha] ssh_port`, 22 by default. The instance
publishes them in its node row when it registers and with every heartbeat,
so a changed configuration takes effect without touching the others, with
its SSH host key, read from the host's key file, so that fencers pin it
rather than trust on first use. A fencer reads the target's address and key
from the database, never from its own configuration. At start, an instance
refuses an `ssh_address` that resolves to one of its InfiniBand interfaces,
since fencing disables those, and HA mode with `fence_container` on refuses
to start while it isn't set.

Fencing a node stops its InfiniBand traffic, its own storage or workloads on
the fabric included, until it is unfenced. Every step must be idempotent,
since a successor repeats it.

With a confirming step enabled, if none confirms within
`[ha] fence_timeout`, the takeover doesn't start OpenSM. The new leader keeps the lease, retries the ladder and raises an
alarm; an operator can confirm a fence with the CLI, which asks them to
check that the target's OpenSM can't act. The fabric runs on the target's
OpenSM, if it still runs, or without a subnet manager, until then.

HA mode refuses to start unless each enabled step has what it needs:

- step 2: the fencing SSH script is installed, and this instance's
  `ssh_address` is set and not on an InfiniBand interface; a target whose
  row has no address can't be fenced by step 2;
- step 3: M_Key protection is on, at protection level 1 or more, so that
  only the managers can disable switch ports, and every node's static map
  is configured;
- step 4: refused until it is implemented.

`[ha] fence_manual = true` makes every takeover wait for an operator to
confirm the fence with the CLI, whatever steps are enabled. With no
confirming step enabled and `fence_manual` off, HA mode logs a warning at
start, and the health check reports it.

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

A node that left the target set unconfirmed, or by step 2 alone, has no
disabled ports: it needs only steps 1
to 4 and 6 below, and the leader clears its fence record, with a term
compare-and-set, once step 4 has verified it, so that its OpenSM is known
stopped before it becomes a candidate again. A node whose ports step 3 disabled needs every step.

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
