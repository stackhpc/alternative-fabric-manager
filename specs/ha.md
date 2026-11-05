# High availability

Status: proposed, 2026-11-05. Revised after review the same day.

Run one fabric manager instance on every OpenSM node, behind HAProxy, with
the shared state in the deployment's Galera cluster. Each instance applies
the state to its local OpenSM only. Galera's quorum is the only consensus:
there is no Raft and no leader.

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
 |   OpenSM       |   |   OpenSM       |   |   OpenSM       |
 |   (master)     |   |   (standby)    |   |   (standby)    |
 +--------^-------+   +--------^-------+   +--------^-------+
          |____________________|____________________|
                               |
     Galera: networks, ports, jobs, generations, node heartbeats
```

HAProxy sends each request to any of the three instances, which are all
active backends, round robin with no session stickiness. Each instance
records the request in Galera and applies Galera's state to the OpenSM on
its own node. Nothing crosses between nodes except through Galera, and the
OpenSMs elect their master over the fabric by themselves. No other subnet
manager may run on the fabric, such as one embedded in a switch, since it
would take over with partitions the managers don't control.

## Shared state

The database holds the whole desired state, so any instance can serve any
request and rebuild its OpenSM's file from scratch. The single-node schema
moves to Galera with these changes:

| Table | Holds | Change from the single-node design |
| --- | --- | --- |
| `networks` | Network ID, pkey (unique), name | None |
| `ports` | Port ID, network, GUID, host | None |
| `state` | The `generation` counter, bumped by every change; the schema version; a hash of the `[opensm]` rendering options | Generation bumped when a request is recorded, not when it is applied. The version and hash are new |
| `jobs` | Operation, object, status, error, timestamps | New `generation` column: the generation that includes the change. Timestamps set by the database (`DATETIME(6)` defaulting to `NOW(6)`), not by the instance |
| `nodes` | Node name, instance UUID, state (`active`, `fenced`, `removed`), applied generation, heartbeat counter, last error | New |

A request records its change, bumps the generation and creates its job in
one transaction. The generation row is locked by the update that bumps it,
and every write goes to one Galera node (see Galera specifics), so requests
are serialised on that row and a job's generation is exactly the first
state that includes it.

Every instance must render the file the same way, since a standby's file is
never checked by OpenSM until it takes over. An instance refuses to start
if its `[opensm]` rendering options don't match the stored hash.

## Requests and applying

Any instance takes a request. Every instance applies the latest state to its
own OpenSM, so no instance writes to another node.

1. HAProxy sends networking-mlnx's request to any healthy instance.
2. The instance validates it, then in one transaction records the change,
   bumps the generation and creates a job with that generation. It answers
   with the job's path. A DELETE of a network or port that isn't recorded
   still creates a job at the current generation, rather than answering
   404 (see Job completion).
3. Every instance polls the generation, every second or so, and is woken at
   once when it took the request itself.
4. An instance whose node is behind reads the generation and the whole state
   in one consistent-read transaction: the generation row is its first
   plain `SELECT`, with no `FOR UPDATE`, so that under InnoDB's repeatable
   read every later read sees the same snapshot. It renders the partitions
   file, writes it atomically and fsyncs it.
5. If its OpenSM is the master, it sends it SIGHUP and waits for OpenSM to
   log the end of the pkey sweep. If its OpenSM is a standby, the written
   file is enough: the standby reads it when it takes over. The instance
   learns its OpenSM's state from OpenSM itself, for example `sminfo` on
   the local port.
6. It records its node's applied generation with
   `applied = GREATEST(applied, :generation)`, so it only moves forward.

Applying is level-triggered: an instance always renders the newest state,
never replays changes, so a node that was down catches up in one apply. The
file's header carries the generation and a per-apply counter, so that a
retry of the same generation still changes the file and OpenSM still sweeps
on SIGHUP. The file lives on persistent storage on the node.

## Job completion

A job is Completed once every `active` node has applied its generation, and
at least `[ha] min_active_nodes` nodes are active. networking-mlnx holds the
port DOWN until then, so no port goes ACTIVE while any OpenSM that could
become master lacks its partition. The minimum keeps the condition from
holding when no node is active, for example on first deployment or after a
mistake marking nodes `removed`.

Status is computed when a job is read, in this order, and written to the
job's row the first time it is terminal, with
`UPDATE ... WHERE status = 'Pending'`, so that it never moves back:

| Status | When |
| --- | --- |
| Completed | At least `min_active_nodes` nodes are `active`, and the minimum applied generation of `active` nodes is at least the job's |
| Failed | Otherwise, once the database's `NOW(6)` is past the job's creation time plus the job timeout, for example 120 seconds |
| Pending | Otherwise |

A `fenced` node doesn't hold jobs back, because its OpenSM is shut down and
can't program the fabric. A `removed` node is no longer part of the set. An
instance that fails to apply records the error on its node row, so a Failed
job can say which node held it back.

A DELETE of a network or port that isn't recorded answers with a new job at
the current generation, never 404. Otherwise a DELETE whose first job
failed, which networking-mlnx sends again, would get 404, which it treats as
done, while a lagging OpenSM may still have the GUID in the old network's
partition. The new job completes once every active node has caught up.

A request whose commit outcome is unknown, and which networking-mlnx sends
again, creates a second job. That is harmless, since applying is
level-triggered.

## Node liveness and fencing

An OpenSM whose file is behind must not become master, because it would
apply old partitions to the whole fabric. The OpenSMs elect their master
among themselves by `sm_priority`, so the manager can't stop a takeover; it
can only make sure a stale OpenSM isn't running as a candidate.

### Heartbeats and leases

- Each instance runs a heartbeat on its own thread every few seconds:
  `UPDATE nodes SET counter = counter + 1 WHERE name = :name AND
  instance = :uuid`, then reads back its node's state. The heartbeat never
  writes `state`.
- The instance's lease runs from the **start** of its last successful
  heartbeat transaction, on its own monotonic clock. The database client has
  connect and read timeouts well under the fence timeout, so a hung call
  can't hold the thread.
- When the lease is older than the fence timeout `T`, the instance shuts
  down its local OpenSM. A separate deadline thread does this, so it
  happens even if the rest of the instance is stuck.
- An instance that reads back its own state as `fenced` or `removed` shuts
  down its OpenSM at once.

### Fencing others

Every instance watches the other nodes' heartbeat counters on its own
monotonic clock. When a node's counter hasn't changed for `2T` as seen
locally, it marks the node fenced with a compare-and-set:
`UPDATE nodes SET state = 'fenced' WHERE name = :name AND state = 'active'
AND counter = :seen`. That node's own lease ran out at most `T` after its
last heartbeat started, so it has shut down its OpenSM before the others
stop waiting for it. Nothing compares the database's clock with another
clock; heartbeat timing only needs each node's clock to run at a sane rate.

### Joining and rejoining

A node that starts, or that was fenced and can reach the database again,
joins in this order:

1. it marks its node `active`, so jobs from then on wait for it;
2. it applies the newest generation;
3. it starts OpenSM;
4. it records the applied generation.

Jobs pause for at most the time this takes. A new node is added the same
way. Before every start of OpenSM, the supervisor checks that the
partitions file exists and has the generation just applied: with no file,
OpenSM would make every port a full member of the default partition.

### Supervising OpenSM

The local OpenSM must never outlive or out-run its manager. The manager runs
OpenSM as its child process, in one container, with the manager (or tini
with the manager as its only child) as the container's PID 1, so OpenSM is
killed when the manager dies. The child also gets `PR_SET_PDEATHSIG`. If
OpenSM exits on its own, the manager marks its node `fenced` itself, which is
safe since OpenSM isn't running, and rejoins as above. A node whose applies
keep failing, past a threshold, does the same rather than hold every job
until it times out.

A watchdog beside the existing OpenSM image was considered instead. It needs
the manager to touch a heartbeat file, and a hung manager whose file-touch
thread still runs would leave a stale OpenSM. Supervising avoids that, so for
HA the manager does control OpenSM's lifecycle, as a supervised process
rather than through a Docker driver. The OpenSM log is then the child's
output, which the manager reads for the sweep markers.

### Removal

An operator marks a node `removed` when it leaves the cluster. The CLI
refuses unless the node is already `fenced` and its heartbeat counter is
stale, so a running OpenSM is never removed from the set. An instance that
finds its own node `removed` shuts down its OpenSM and stays stopped.

### Timeouts

All three instances use the same database VIP, so a MariaDB outage longer
than `T` fences every node at once and leaves the fabric without a subnet
manager. Rolling MariaDB restarts and Galera recovery make this the common
case, not a corner case. So `T` must be well above the deployment's MariaDB
failover time, at the cost of jobs pausing for up to `2T` when a node dies.
After the master OpenSM stops, a standby takes over after about
`sminfo_polling_timeout` × `polling_retry_number`, 40 seconds by default.

Give all OpenSMs the same `sm_priority`. With unequal priorities, a
higher-priority OpenSM that rejoins forces a handover and a heavy sweep
every time.

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
- **Map constraint errors.** A pkey clash that reaches the unique constraint
  is a 400, not a 500.
- **Read your own writes.** Every manager session sets
  `wsrep_sync_wait = 1`, so a read waits for writes already committed on
  other Galera nodes. Kolla's VIP fails over to another Galera node, and a
  job created just before would otherwise be missing from the next poll.
- **Database clock.** Job timestamps and timeouts use `NOW(6)`, set by the
  database. Heartbeat timing doesn't use it (see Fencing others).
- **Schema migrations.** Alembic, run by one instance at deploy time, rather
  than `create_all` at startup.
- **One instance per name.** An instance registers a UUID on its node row
  at start, and refuses to start if another live instance holds the name.

All three instances are active HAProxy backends, round robin with no
session stickiness, and HAProxy checks `/healthz`. That works because an
instance holds no state of its own: it records every request in Galera,
answers every poll from Galera, and only applies changes to its local
OpenSM, which every instance does anyway. `/healthz` fails while the
instance can't reach the database, so HAProxy stops sending it requests.

## Upgrades

- **No mixed versions.** The schema version is stored, and an instance
  refuses to run against a database newer than it understands. An older
  single-node instance would complete jobs after applying only its own
  node.
- **From single node.** Move the SQLite data into Galera keeping the job
  IDs, since journal rows that are monitoring a job would otherwise get 404
  and send their requests again.
- **Job history.** Purge finished jobs after a retention period, from any
  instance, since the purge is idempotent.

## Failure scenarios

Every case fails closed: a port waits or goes to ERROR rather than becoming
ACTIVE without its partition on every OpenSM that could be master.

| Failure | What happens | Outcome |
| --- | --- | --- |
| One manager instance down | HAProxy stops sending it requests. Its OpenSM dies with it. Its heartbeat counter stops, and after `2T` it is marked `fenced` | Jobs pause for up to `2T`, then carry on with two nodes |
| Manager hung, OpenSM still running | Its lease runs out on its deadline thread, which shuts down OpenSM. After `2T` the others mark it `fenced` | As above |
| OpenSM exits, manager alive | The manager marks its node `fenced`, then rejoins | Jobs carry on with two nodes meanwhile |
| An apply keeps failing | After the threshold, the node fences itself and rejoins | Jobs pause up to the threshold, then carry on |
| Master OpenSM's node lost | A standby takes over after about 40 s, with a file that is current, since every job waited for it | No partition change |
| A node cut off from Galera | It shuts down its OpenSM after `T`. The others mark it `fenced` after `2T`. When the database is back it rejoins: active, apply, start OpenSM | Jobs pause, then carry on. Its OpenSM can't take over with an old file |
| A node fenced while alive | Its heartbeat reads back `fenced`, and it shuts down OpenSM at once, then rejoins | Jobs carry on |
| MariaDB VIP outage longer than `T` | Every instance shuts down its OpenSM. Requests and `/healthz` fail | The fabric runs without a subnet manager until the database is back. networking-mlnx retries its requests |
| A job failed, and networking-mlnx sends its DELETE again | The object is gone, so the DELETE gets a new job at the current generation | Completes once every active node has caught up |
| Instance crashes mid-apply | The file may or may not be written. Its applied generation isn't recorded | It applies again on restart. The file always changes, so OpenSM always sweeps |

## Alternatives considered

| Option | Why not |
| --- | --- |
| Raft among the manager instances, with a single leader | Galera already gives replicated state with quorum, so Raft would duplicate it, with a consensus implementation, its own log and membership changes to maintain. A leader also can't control which OpenSM becomes master: only fencing stops a stale one |
| One active manager, behind keepalived, that pushes the file to every OpenSM node over SSH | It needs remote access to every node, and the manager's own failover still needs shared state. A node that misses a push has no way to catch up by itself |
| Each instance with its own SQLite, behind HAProxy | A request reaches one instance, so the other OpenSMs never see it, and a poll on another instance finds no job |
| A watchdog beside the existing OpenSM image | A hung manager whose file-touch thread still runs leaves a stale OpenSM (see Supervising OpenSM) |
| Leader election with tooz, over the MySQL or etcd backend | Not needed now, since nothing must run on only one instance. Use it if that changes, rather than a Raft of our own |

## Implementation plan

The single-node code already renders the whole state and applies it
level-triggered, so these are additions rather than a rewrite. Each step
keeps single-node SQLite working.

- [ ] Verify the standby behaviours on a fabric with two OpenSMs.
- [ ] Single node, now: a DELETE of an unknown object creates a job rather
      than answering 404.
- [ ] Database: a `[database] connection` URL, Alembic migrations, schema
      version and rendering hash, `wsrep_sync_wait`, deadlock retry around
      each request, constraint errors as 400, database timestamps.
- [ ] Generations: bump the generation when a request is recorded, store it
      on the job, and read the state in one consistent snapshot.
- [ ] Nodes: `[node] name`, the `nodes` table with an instance UUID,
      heartbeats on their own thread, leases on the monotonic clock.
- [ ] Job status computed from the active nodes' applied generations, with
      `min_active_nodes`, a job timeout, and terminal statuses written once.
- [ ] Supervisor: OpenSM as the manager's child in one image, its log read
      from its output, its state read with `sminfo`, SIGHUP only to the
      master, the join order, self-fencing on lease expiry, OpenSM exit or
      repeated apply failures.
- [ ] Fencing others by compare-and-set, and a CLI to mark a fenced node
      `removed`.
- [ ] Job purge.
- [ ] Tests: three instances against one MariaDB in CI, with faults
      injected: an instance killed, an instance paused, the database cut
      off, OpenSM killed, an apply failing.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck and database credentials.

## Open questions

- Does a standby OpenSM ignore SIGHUP's sweep, and does it read the
  partitions file when it takes over? Upstream's heavy sweep re-parses the
  file, but this hasn't been checked on this OpenSM build.
- `T`, the job timeout and `min_active_nodes`: what is the deployment's
  MariaDB failover time, and how do these fit networking-mlnx's retries and
  Ironic's wait for ports?
- When MariaDB is unavailable for longer than `T`, should the last OpenSM
  keep running with possibly stale partitions rather than leave the fabric
  without a subnet manager? Shutting down is safe for isolation.
- Should each instance also check that the fabric's master subnet manager is
  one of the managed nodes, and fail jobs if not?
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
