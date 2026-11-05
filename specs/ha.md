# High availability

Status: proposed, 2026-11-05.

Run one fabric manager instance on every OpenSM node, behind HAProxy, with
the shared state in the deployment's Galera cluster. Each instance applies
the state to its local OpenSM only. Galera's quorum is the only consensus:
there is no Raft and no leader.

Two principles carry the safety argument:

- **A job completes only on evidence that the fabric has its effect:** the
  master subnet manager that the fabric has elected confirmed a sweep of a
  state in which the job's effect still holds, and every other subnet
  manager that could take over has a file that the master accepted.
- **A node is left out of that only once it is known not to run OpenSM,**
  by enforcement that doesn't depend on its manager: a watchdog beside
  OpenSM, and an external fence agent. Without that knowledge, a node stays
  in, and jobs fail closed.

The design rests on two OpenSM behaviours that must be confirmed on a fabric
with a standby subnet manager before it is built (see Open questions):

- a standby OpenSM doesn't sweep, so it doesn't apply a partitions file on
  SIGHUP;
- a standby that takes over runs a heavy sweep, which reads the partitions
  file on disk at that moment.

## Architecture

```
                 Neutron server (mlnx_sdn_assist)
                               |
                 HAProxy: all three backends active
           ____________________|____________________
          |                    |                    |
 +--------v-------+   +--------v-------+   +--------v-------+
 | OpenSM node 1  |   | OpenSM node 2  |   | OpenSM node 3  |
 |   manager      |   |   manager      |   |   manager      |
 |     | file +   |   |     | file     |   |     | file     |
 |     | SIGHUP   |   |     |          |   |     |          |
 |   watchdog     |   |   watchdog     |   |   watchdog     |
 |   OpenSM       |   |   OpenSM       |   |   OpenSM       |
 |   (master)     |   |   (standby)    |   |   (standby)    |
 +--------^-------+   +--------^-------+   +--------^-------+
          |____________________|____________________|
                               |
     Galera: networks, ports, jobs, generations, nodes, acknowledgements
          plus each node's fence agent, run by its peers
```

HAProxy sends each request to any of the three instances, which are all
active backends, round robin with no session stickiness. Each instance
records the request in Galera and applies Galera's state to the OpenSM on
its own node. Nothing crosses between nodes except through Galera and the
fence agents, and the OpenSMs elect their master over the fabric by
themselves. No other subnet manager may run on the fabric, such as one
embedded in a switch, since it would take over with partitions the managers
don't control.

## Shared state

The database holds the whole desired state, so any instance can serve any
request and rebuild its OpenSM's file from scratch.

| Table | Holds | Change from the single-node design |
| --- | --- | --- |
| `networks` | Network ID, pkey (unique), name | None |
| `ports` | Port ID, network, GUID (unique), host | None |
| `state` | The `generation` counter; the schema version; the configuration epoch and the hash of the configuration that renders and runs OpenSM | Generation bumped when a request is recorded. Version, epoch and hash are new |
| `jobs` | Operation, object, generation, the effect it expects, status, error, timestamps | New generation and effect columns. Timestamps in UTC, set by the database |
| `nodes` | Node name, instance UUID, membership epoch, state (`joining`, `active`, `fencing`, `fenced`, `removed`), heartbeat counter, OpenSM role, last error | New |
| `acks` | Per node and membership epoch: the generation whose file it wrote and the master accepted (`ready`), and, while its OpenSM is master, the generation it swept (`swept`) with its master term | New |

A request records its change, bumps the generation and creates its job in
one transaction. The generation row is locked by the update that bumps it,
and every write goes to one Galera node (see Galera specifics), so requests
are serialised on that row and a job's generation is exactly the first
state that includes it.

A job's **effect** is what must hold in the fabric once it completes: that a
GUID is a full member of a network's partition (a port's create or update),
that it isn't a member of a partition (a port's deletion, or the earlier
network of a port that moved), that a partition exists or doesn't (a
network's create or deletion).

## Requests and applying

Any instance takes a request. Every instance applies the latest state to its
own OpenSM, so no instance writes to another node.

1. HAProxy sends networking-mlnx's request to any healthy instance.
2. The instance validates it, then in one transaction records the change,
   bumps the generation and creates a job with that generation and its
   effect. In the same transaction, every unfinished job whose effect the
   change undoes is marked superseded (see Job completion). It answers with
   the job's path. A DELETE of a network or port that isn't recorded still
   creates a job at the current generation, rather than answering 404.
3. Every instance polls the generation, every second or so, and is woken at
   once when it took the request itself.
4. An instance whose node is behind reads the generation and the whole state
   in one consistent-read transaction: the generation row is its first
   plain `SELECT`, with no `FOR UPDATE`, so that under InnoDB's repeatable
   read every later read sees the same snapshot. It renders the partitions
   file, checks it (see Configuration), writes it atomically and fsyncs it.
5. It learns its OpenSM's role from OpenSM itself, `sminfo` on the local
   port, and the fabric's master from the master SM's GUID that `sminfo`
   reports.
   - **Master:** it sends OpenSM SIGHUP, waits for the sweep that starts
     after the signal to end, with no partitions file errors, and records
     `swept = generation` for its node, its membership epoch and its master
     term.
   - **Standby:** it records `ready = generation` only once a master has
     recorded `swept` at least that generation, and its file is byte for
     byte the one the master swept, apart from the header. Every node
     renders with the same configuration (see Configuration), so the files
     are identical, and the master's OpenSM has parsed that exact content.
     A standby's own OpenSM doesn't read its file until it takes over, so a
     file only the manager's parser has accepted never counts.
6. Acknowledgements only move forward within a membership epoch and a master
   term, with `GREATEST()`.

A node's **master term** starts when its instance sees its OpenSM become the
fabric's master. At the start of a term, `swept` is cleared, and the
instance waits for the takeover's heavy sweep to end, without partitions
file errors, before recording the generation of the file it took over with.
An error then is a failed takeover: the instance stops its OpenSM, as in
Fencing itself.

Applying is level-triggered: an instance always renders the newest state,
never replays changes, so a node that was down catches up in one apply. The
file's header carries the generation and an attempt counter, so that a
retry still changes the file and OpenSM runs a pkey sweep. The file lives on
persistent storage on the node. Every `check_interval`, an instance also
reconciles: if its file isn't what it last applied, or the state rendered at
that generation differs, it applies again.

## Job completion

networking-mlnx holds a port DOWN until its job completes, so a job may
complete only on evidence that its effect is in the fabric and will stay
there through a takeover.

A job at generation G is **Completed** when all of these hold:

1. it wasn't superseded;
2. at least `[ha] min_active_nodes` nodes are in the **completion set**, the
   nodes in `joining`, `active` or `fencing`;
3. the fabric's master SM, the GUID that the instances' `sminfo` report, is
   the OpenSM of a node in the completion set, which has recorded, in its
   current membership epoch and master term, `swept` at least G;
4. every other node in the completion set has recorded, in its current
   membership epoch, `ready` at least G.

A newer generation satisfies an older job only because of condition 1: the
job's effect holds in every later state that doesn't supersede it. Without
it, a port's create at G followed by its deletion at G+1 would complete the
create once a node applied G+1, though the port was never in its partition.
A change that undoes an unfinished job's effect supersedes it, in the same
transaction:

- deleting a port, or moving it to another network, supersedes its
  unfinished create or update;
- deleting a network supersedes the unfinished jobs of the network and of
  its ports;
- creating a port again supersedes its unfinished deletion, and creating a
  network again its unfinished deletion.

A superseded job is Failed, with the reason. networking-mlnx sends each
object's requests in order, waiting for each one's job, so this only
happens when its journal is bypassed or out of step, and failing closed is
right then.

A job is **Failed** once it is superseded, or once the database's UTC time
is past its creation time plus the job timeout, for example 120 seconds,
and it isn't Completed. Otherwise it is **Pending**. Status is computed when
a job is read, in that order, and written to its row the first time it is
terminal, with `UPDATE ... WHERE status IN ('Pending', 'Running')`, so that
it never moves back.

The master changes only by election, which every instance sees in
`sminfo`. During an election, or until the new master's takeover sweep is
confirmed, condition 3 doesn't hold, so jobs wait. A standby that has only
written a generation can't complete a job, even if the master has just
died: until a new master has swept it, no OpenSM has applied it.

A DELETE of a network or port that isn't recorded answers with a new job at
the current generation, never 404, so that a DELETE that networking-mlnx
sends again after its first job failed completes only once the fabric
reflects it.

A request whose commit outcome is unknown, and which networking-mlnx sends
again, creates a second job. That is harmless: applying is level-triggered,
and the two jobs have the same effect.

## Node liveness and fencing

An OpenSM whose file is behind must not become master, because it would
apply old partitions to the whole fabric. The OpenSMs elect their master
among themselves by `sm_priority`, so the manager can't stop a takeover. It
can only make sure that a node it no longer waits for doesn't run OpenSM.
That can't depend on the node's manager, which may be paused, hung or cut
off: a paused process runs no deadline thread, and a PID namespace only
kills OpenSM when the manager exits, not while it is paused.

### Membership epochs

Every time a node registers or rejoins, its membership epoch is
incremented, in the same transaction as its state becomes `joining` and its
acknowledgements are cleared. Every acknowledgement carries the epoch, and
completion only counts acknowledgements of a node's current epoch, so a
rejoining node never counts with what it acknowledged before.

### Heartbeats

Each instance runs a heartbeat on its own thread every few seconds: it
increments its node row's counter, matching its instance UUID and
membership epoch, and reads back its node's state. The heartbeat never
writes `state`. The database client has connect and read timeouts well
under the lease, so a hung call can't hold the thread.

### The watchdog

OpenSM's container runs a small watchdog beside OpenSM, as its entrypoint,
which starts OpenSM and kills it with SIGKILL when its lease expires. The
manager renews the lease after each successful heartbeat, by writing a
deadline to a file the two containers share: the start of that heartbeat
transaction plus the lease `T`, in the host's monotonic clock
(`CLOCK_MONOTONIC`, which every container on the host shares). The watchdog
checks the deadline every second.

So OpenSM stops within `T` of the manager's last successful heartbeat if the
manager is paused, hung, crashed or cut off from the database, while OpenSM
and the watchdog run. The watchdog also kills OpenSM if the lease file is
missing, and starts it only when the manager asks, after the join steps
below.

The watchdog can't cover a pause of OpenSM's own container, or of the whole
node: a frozen OpenSM can resume as master before the watchdog runs. Only a
fence agent covers that.

### Fencing other nodes

A node whose heartbeat counter hasn't changed for `2T`, as another instance
sees it on its own monotonic clock, is suspect. The instance that sees it:

1. moves it from `active` or `joining` to `fencing` with a compare-and-set
   that includes the counter it saw, the node's instance UUID and its
   membership epoch, so that a node that has since rejoined, or a
   replacement instance, isn't affected; an observer resets its timer for a
   node whenever its instance UUID or epoch changes;
2. runs the node's fence agent, `[ha] fence_agent`: a command that powers
   the node off, or kills its OpenSM container through the node's container
   runtime, and that returns success only once it has confirmed that OpenSM
   can't run there, as a STONITH agent does;
3. on success, moves the node from `fencing` to `fenced`, with the same
   compare-and-set. Only then does it leave the completion set.

A node in `fencing` stays in the completion set: jobs keep waiting for it,
and fail at their timeout, rather than complete without it. If the fence
agent fails, or none is configured, the node stays in `fencing` and an
operator has to fence it by other means and mark it `fenced` or `removed`
with the CLI, which asks them to confirm that OpenSM isn't running there.
Without a fence agent, a node whose instance dies therefore makes every job
fail until an operator acts. That is the cost of failing closed, and the
reason to configure one.

### Fencing itself

An instance takes its own node out of the completion set only once it has
verified that its OpenSM has stopped: its container has exited, as the
container runtime reports. It does that:

- when it reads back its own state as `fencing`, `fenced` or `removed`;
- when OpenSM exits, or its takeover fails;
- when its applies keep failing, past a threshold.

It stops OpenSM, verifies that it stopped, then moves its node to `fenced`,
and rejoins. If it can't verify that OpenSM stopped, it leaves its node in
the completion set, so that jobs fail rather than complete without it,
records why, and stops renewing the lease, so that the watchdog kills OpenSM
within `T`. Its peers then fence it as above.

### Joining and rejoining

A node that starts, or that was fenced and can reach the database again,
joins in this order:

1. in one transaction, it increments its membership epoch, clears its
   acknowledgements and becomes `joining`, so that jobs from then on wait
   for it, and its earlier acknowledgements no longer count;
2. it applies the newest generation;
3. it renews its lease and has the watchdog start OpenSM;
4. once it has recorded `ready`, or as master `swept`, it becomes `active`.

Before every start of OpenSM, the watchdog checks that the partitions file
exists and has the generation just applied: with no file, OpenSM would make
every port a full member of the default partition.

### Removal

An operator marks a node `removed` when it leaves the cluster. The CLI
refuses unless the node is already `fenced`, or the operator confirms that
its OpenSM isn't running. An instance that finds its own node `removed`
stops its OpenSM, verifies it, and stays stopped.

### Timeouts

All three instances use the same database VIP, so a MariaDB outage longer
than `T` stops every OpenSM, through the watchdogs, and leaves the fabric
without a subnet manager. Rolling MariaDB restarts and Galera recovery make
this the common case, not a corner case. So `T` must be well above the
deployment's MariaDB failover time, at the cost of jobs pausing for up to
`2T`, plus fencing, when a node dies. After the master OpenSM stops, a
standby takes over after about `sminfo_polling_timeout` ×
`polling_retry_number`, 40 seconds by default, and jobs wait for its
takeover sweep.

Give all OpenSMs the same `sm_priority`. With unequal priorities, a
higher-priority OpenSM that rejoins forces a handover and a heavy sweep
every time.

## Configuration

The manager writes both of OpenSM's files, so every node's OpenSM is
configured the same way:

- **`opensm.conf`** is built from the hardening groups and the operator's
  overrides, as `key value` pairs, such as `sm_priority`, `routing_engine`
  or `sweep_interval`. The manager sets the keys it depends on itself, such
  as `partition_config_file`, and refuses overrides of them.
- **`partitions.conf`** is rendered from a Jinja template, in a sandboxed
  environment with undefined variables as errors. The template gets the
  networks, each with its pkey and member GUIDs, the flat networks'
  partition, and the generation.

Before writing a rendered file, the manager parses it and refuses to apply
it, failing the jobs, unless all of these hold, as the single-node manager
already checks:

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
networks, for example with `shared=0x0200 : ALL=full ;`.

### Configuration epochs

The configuration's hash is stored with an epoch. An instance whose
configuration doesn't match the current epoch's hash doesn't start OpenSM,
and doesn't count in the completion set. Changes fall into two kinds:

- **Rolling changes** don't change which ports are in which partitions, or
  how OpenSMs authenticate and protect ports: `sweep_interval`, routing,
  logging, timeouts. The operator records the new configuration's hash as a
  new epoch, which allows both hashes during the change, and restarts the
  nodes one at a time, standbys first, each rejoining as above. Recording
  the end of the change drops the old hash.
- **Fabric-affecting changes** are everything else: the keys, the
  hardening groups, the default partition, the template, `flat_pkey`,
  `shared_default_partition` and the IPoIB settings. Two OpenSMs with
  different values must never both be able to master the fabric, so these
  aren't rolled. The operator stops every standby, changes the
  configuration, restarts the master's node, then the standbys. A key
  change additionally needs a key rotation procedure, which is future work:
  until then the manager refuses to start with keys other than the stored
  ones.

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
  constraint is a 400, not a 500.
- **Read your own writes.** Every manager session sets
  `wsrep_sync_wait = 1`, so a read waits for writes already committed on
  other Galera nodes; Kolla's VIP fails over between Galera nodes.
- **UTC.** Every session sets `time_zone = '+00:00'`, and timestamps are
  `DATETIME(6)` values in UTC, set with `UTC_TIMESTAMP(6)`, so that a
  daylight saving change, or a failover to a Galera node in another
  timezone, can't age a job by an hour. Galera's nodes need synchronised
  clocks, within much less than the job timeout, since a job's timeout is
  judged by whichever node serves the read. Heartbeat timing doesn't use the
  database's clock (see Heartbeats).
- **Schema migrations.** Alembic, run by one instance at deploy time, rather
  than `create_all` at startup.
- **One instance per name.** An instance registers a UUID on its node row
  at start, and refuses to start if another live instance holds the name.

All three instances are active HAProxy backends, round robin with no
session stickiness, and HAProxy checks `/healthz`. That works because an
instance holds no state of its own: it records every request in Galera,
answers every poll from Galera, and only applies changes to its local
OpenSM. `/healthz` fails while the instance can't reach the database, so
HAProxy stops sending it requests.

## Upgrades

- **No mixed versions.** The schema version is stored, and an instance
  refuses to run against a database newer than it understands. An older
  single-node instance would complete jobs after applying only its own
  node.
- **From single node**, with the manager stopped, so that nothing changes
  during the migration:
  1. copy the networks, ports and keys into Galera;
  2. copy the jobs, keeping their IDs, since journal rows that are
     monitoring a job would otherwise get 404 and send their requests
     again;
  3. keep terminal jobs as they are;
  4. give every unfinished job, `Pending` or `Running`, the migration's
     generation, the first one in Galera, and status `Pending`, and compute
     its effect from its operation and object;
  5. mark superseded any unfinished job whose effect doesn't hold in the
     migrated state, for example a port's create whose port is gone.

  The unfinished jobs then complete, or fail, by the rules above, once the
  nodes have applied the migration's generation.
- **Job history.** Purge finished jobs after a retention period, from any
  instance, since the purge is idempotent.

## Failure scenarios

Every case fails closed: a port waits or goes to ERROR rather than becoming
ACTIVE without its partition on the elected master, and on every OpenSM that
could take over.

| Failure | What happens | Outcome |
| --- | --- | --- |
| One manager instance crashes | HAProxy stops sending it requests. Its OpenSM dies with it, through the PID namespace, or its watchdog kills it within `T`. After `2T` a peer moves it to `fencing` and runs its fence agent | Jobs pause until it is `fenced`, then carry on with two nodes. Without a fence agent, they fail until an operator acts |
| Manager paused or hung, OpenSM running | Its lease isn't renewed, so its watchdog kills OpenSM within `T`. Peers fence it as above | As above |
| The whole node, or OpenSM's container, paused | Neither the manager nor the watchdog runs. Peers move it to `fencing` and run its fence agent, which powers it off or kills its container | Jobs wait until the agent confirms; without one, they fail until an operator acts. A resumed OpenSM can't master with stale partitions, since it was fenced before the node left the completion set |
| OpenSM exits, manager alive | The manager verifies that OpenSM has stopped, marks its node `fenced`, and rejoins | Jobs carry on with two nodes meanwhile |
| An apply keeps failing | Past the threshold, the manager stops OpenSM and verifies it before marking its node `fenced`. If it can't verify, its node stays in the completion set and it stops renewing the lease | Jobs carry on once it is fenced; otherwise they fail until its watchdog stops OpenSM and its peers fence it |
| Master OpenSM's node lost | A standby takes over after about 40 s, with a file the master had accepted. Its instance confirms the takeover sweep, and only then counts as the master | Jobs wait through the election, then complete on the new master's acknowledgement |
| A takeover fails to parse or sweep | The new master's instance stops its OpenSM, verifies it, and fences itself | Another standby takes over, or jobs fail |
| A node cut off from Galera | Its watchdog kills OpenSM within `T`. Peers fence it after `2T`. When it can reach the database again it rejoins with a new membership epoch | Jobs pause, then carry on. Its earlier acknowledgements don't count |
| A delayed observer fences after the node rejoined | The compare-and-set doesn't match the node's new membership epoch or instance UUID | Nothing happens |
| MariaDB VIP outage longer than `T` | Every watchdog kills its OpenSM. Requests and `/healthz` fail | The fabric runs without a subnet manager until the database is back. networking-mlnx retries its requests |
| A change supersedes an unfinished job | The job is marked superseded in the change's transaction | It fails, rather than complete on a state without its effect |
| Instance crashes mid-apply | The file may or may not be written. Its acknowledgement isn't recorded | It applies again on restart. The file always changes, so OpenSM always sweeps |

## Alternatives considered

| Option | Why not |
| --- | --- |
| Raft among the manager instances, with a single leader | Galera already gives replicated state with quorum, so Raft would duplicate it, with a consensus implementation, its own log and membership changes to maintain. A leader also can't control which OpenSM becomes master: only fencing stops a stale one |
| One active manager, behind keepalived, that pushes the file to every OpenSM node over SSH | It needs remote access to every node, and the manager's own failover still needs shared state. A node that misses a push has no way to catch up by itself |
| Each instance with its own SQLite, behind HAProxy | A request reaches one instance, so the other OpenSMs never see it, and a poll on another instance finds no job |
| Fencing only through the manager, by the PID namespace and a deadline thread | A paused manager runs neither, and a PID namespace doesn't stop OpenSM while the manager is paused |
| Counting a standby's written file as applied | The file may not parse in OpenSM, and no master may ever sweep that generation |
| Fencing by M_Key rotation: the master changes every port's M_Key, so a stale OpenSM can no longer program ports | Attractive, since it needs no fence agent, but OpenSM has no supported online key rotation; revisit with key rotation |
| Leader election with tooz, over the MySQL or etcd backend | Not needed now, since nothing must run on only one instance. Use it if that changes, rather than a Raft of our own |

## Implementation plan

The single-node code already renders the whole state, checks it, applies it
level-triggered and reconciles it, so these are additions rather than a
rewrite. Each step keeps single-node SQLite working.

- [ ] Verify the standby behaviours on a fabric with two OpenSMs, and how
      `sminfo` reports the master and each SM's state.
- [x] Single node: a DELETE of an unknown object creates a job rather than
      answering 404.
- [x] Single node: the checks of rendered files, including that no static
      partition bridges networks' ports.
- [x] Single node: the manager spawns OpenSM in its PID namespace, builds
      `opensm.conf` and renders `partitions.conf` from a checked template.
- [ ] Single node: jobs record their effect, and a change supersedes the
      unfinished jobs whose effect it undoes.
- [ ] Database: a `[database] connection` URL, Alembic migrations, schema
      version, configuration epoch and hash, `wsrep_sync_wait`, UTC
      sessions and timestamps, deadlock retry around each request, and
      constraint errors as 400.
- [ ] Generations: bump the generation when a request is recorded, store it
      on the job, and read the state in one consistent snapshot.
- [ ] Nodes: `[node] name`, the `nodes` and `acks` tables, instance UUIDs,
      membership epochs, heartbeats on their own thread.
- [ ] The watchdog: an entrypoint for OpenSM's container that starts OpenSM
      when the manager asks, and kills it when the lease expires.
- [ ] Roles: each instance reports its OpenSM's role and the fabric's master
      from `sminfo`, master terms, and the takeover sweep's confirmation.
- [ ] Job status computed from the master's `swept` and the others'
      `ready`, with `min_active_nodes`, superseding, a job timeout, and
      terminal statuses written once.
- [ ] Fencing: the `fencing` state, the fence agent interface, compare-and-set
      with instance UUID and epoch, verified self-fencing, and a CLI to mark
      nodes fenced or removed.
- [ ] Configuration epochs, and the procedure for fabric-affecting changes.
- [ ] Job purge, and the migration from single node.
- [ ] Tests: three instances against one MariaDB in CI, and a Galera cluster
      in containers, with faults injected: an instance killed, an instance
      paused, a node paused, the database cut off, OpenSM killed, an apply
      failing, a failed takeover, a delayed observer.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck, database credentials and fence agents.

## Open questions

- Does a standby OpenSM ignore SIGHUP's sweep, and does it read the
  partitions file when it takes over? Upstream's heavy sweep re-parses the
  file, but this hasn't been checked on this OpenSM build.
- How does `sminfo` report a standby, a master in an election, and a paused
  master? Condition 3 of completion relies on the fabric's view of the
  master.
- Which fence agents do deployments have: IPMI or Redfish power control of
  the OpenSM nodes, or only their container runtimes? A container runtime
  can't fence a paused node.
- `T`, the job timeout and `min_active_nodes`: what is the deployment's
  MariaDB failover time, and how do these fit networking-mlnx's retries and
  Ironic's wait for ports?
- When MariaDB is unavailable for longer than `T`, should the last OpenSM
  keep running with possibly stale partitions rather than leave the fabric
  without a subnet manager? Stopping is safe for isolation.
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
