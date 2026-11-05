# High availability

Status: proposed, 2026-11-05.

Run one fabric manager instance on every OpenSM node, behind HAProxy, with
the shared state in the deployment's Galera cluster. Each instance applies
the state to its local OpenSM only. Galera's quorum is the only consensus:
there is no Raft and no leader.

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
 |     | file +   |   |     | file +   |   |     | file +   |
 |     | SIGHUP   |   |     | SIGHUP   |   |     | SIGHUP   |
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
its own node, master or standby. Nothing crosses between nodes except
through Galera, and the OpenSMs elect their master over the fabric by
themselves.

## Shared state

The database holds the whole desired state, so any instance can serve any
request and rebuild its OpenSM's file from scratch. The single-node schema
moves to Galera with two changes: jobs record a generation, and a new
`nodes` table records what each OpenSM has applied.

| Table | Holds | Change from the single-node design |
| --- | --- | --- |
| `networks` | Network ID, pkey (unique), name | None |
| `ports` | Port ID, network, GUID, host | None |
| `state` | The `generation` counter, bumped by every change | Bumped when a request is recorded, not when it is applied |
| `jobs` | Operation, object, status, error, timestamps | New `generation` column: the generation that includes the change |
| `nodes` | Node name, applied generation, last heartbeat, state (`active`, `fenced`, `removed`), last error | New |

A request records its change, bumps the generation and creates its job in
one transaction, so a job's generation is exactly the first state that
includes it.

## Requests and applying

Any instance takes a request. Every instance applies the latest state to
its own OpenSM, so no instance writes to another node.

1. HAProxy sends networking-mlnx's request to any healthy instance.
2. The instance validates it, then in one transaction records the change,
   bumps the generation and creates a job with that generation. It answers
   with the job's path.
3. Every instance polls the generation, every second or so, and is woken
   at once when it took the request itself.
4. An instance whose OpenSM is behind reads the whole state at the newest
   generation, renders the partitions file, writes it atomically and sends
   its local OpenSM SIGHUP.
5. Once OpenSM logs the end of the pkey sweep, the instance records that
   its node has applied that generation.

Applying is level-triggered: an instance always renders the newest state,
never replays changes, so a node that was down catches up in one apply. The
file's header carries the generation and a per-apply counter, so that a
retry of the same generation still changes the file and OpenSM still
sweeps on SIGHUP.

A standby OpenSM gets the same file and SIGHUP. It doesn't program the
fabric, but it has the current partitions ready when it takes over.

## Job completion

A job is Completed once every `active` node has applied its generation.
networking-mlnx holds the port DOWN until then, so no port goes ACTIVE
while any OpenSM that could become master lacks its partition.

Status is computed when a job is read, not stored by whichever instance
applied it, so any instance answers the poll the same way:

| Status | When |
| --- | --- |
| Pending | Some `active` node hasn't applied the job's generation |
| Completed | Every `active` node has applied it: the minimum applied generation of `active` nodes is at least the job's |
| Failed | The request was valid but the job timed out: an `active` node didn't apply it within the job timeout, for example 120 seconds |

A `fenced` node doesn't hold jobs back, because its OpenSM is shut down and
can't program the fabric. A `removed` node is no longer part of the set. An
instance that fails to apply records the error on its node row, so a Failed
job can say which node held it back.

## Node liveness and fencing

An OpenSM whose file is behind must not become master, because it would
apply old partitions to the whole fabric. The OpenSMs elect their master
among themselves by `sm_priority`, so the manager can't stop a takeover; it
can only make sure a stale OpenSM isn't running as a candidate.

- **Heartbeat:** each instance updates its node row every few seconds, with
  the database's clock (`NOW()`).
- **Self-fencing:** an instance that can't reach the database for the fence
  timeout, for example 30 seconds, shuts down its local OpenSM. When the
  database is back, it applies the current generation to the file first and
  only then starts OpenSM again, so OpenSM never runs with an old file. It
  then marks its node `active`.
- **Fencing others:** an instance that sees another node's heartbeat older
  than twice the fence timeout marks it `fenced`, so jobs stop waiting for
  it. By then, the node has fenced itself.
- **Removal:** an operator marks a node `removed` when it leaves the
  cluster. Adding a node is the reverse: it starts `fenced` and becomes
  `active` once it has applied the current generation.

Self-fencing must also happen when the manager itself dies, or a crashed
instance leaves a stale OpenSM running while the others stop waiting for it.
So the local OpenSM must not outlive its manager:

- **Recommended: the manager supervises OpenSM.** The manager starts OpenSM
  as its child process, in one container, only after applying the current
  generation, and OpenSM exits with it. Restarting the container restarts
  both, in that order.
- **Alternative: a watchdog.** The manager touches a heartbeat file every
  few seconds, and a small loop beside OpenSM, in its container, stops
  OpenSM when the file is older than the fence timeout. OpenSM stays a
  separate image, but there are two processes that must agree.

So for HA, the manager does control OpenSM's lifecycle, as a supervised
process rather than through a Docker driver.

The second timeout being longer than the first is what makes this safe: a
node is only left out of job completion after it has shut down its OpenSM.
This relies on the node's clock running at a sane rate, not on clocks
agreeing.

If every node loses the database, every OpenSM is shut down and the fabric
runs without a subnet manager. Established links keep working, but nothing
new comes up until the database is back. Whether to shut down the last
OpenSM, or keep it running with possibly stale partitions, is an open
question.

## Galera specifics

The manager uses the deployment's Galera through its usual database VIP,
like the OpenStack services do. SQLAlchemy makes the backend a
`[database] connection` URL, and SQLite stays for single-node deployments.

- **Writes go to one Galera node.** Kolla's HAProxy already sends MariaDB
  traffic to one active backend. Every request bumps the generation row, so
  writes from several Galera nodes would conflict at commit.
- **Retry on deadlock.** Galera reports a certification conflict as a
  deadlock at commit. Wrap each request's transaction in a retry, as
  oslo.db does for OpenStack services.
- **Read your own writes.** A job created on one instance is often polled
  through another. With every instance on the same database VIP, that is
  the same Galera node. An instance connected to another Galera node needs
  `wsrep_sync_wait = 1`, so that its reads wait for writes that are already
  committed elsewhere.
- **One clock.** Heartbeats and job timeouts use the database's `NOW()`,
  never the instance's own clock.
- **Schema migrations.** Use Alembic, run by one instance at deploy time,
  rather than `create_all` at startup.

All three instances are active HAProxy backends, round robin with no
session stickiness, and HAProxy checks `/healthz`. That works because an
instance holds no state of its own: it records every request in Galera,
answers every poll from Galera, and only applies changes to its local
OpenSM, which every instance does anyway. `/healthz` fails while the
instance can't reach the database, so HAProxy stops sending it requests.

## Failure scenarios

Every case fails closed: a port waits or goes to ERROR rather than becoming
ACTIVE without its partition on every OpenSM that could be master.

| Failure | What happens | Outcome |
| --- | --- | --- |
| One manager instance down | HAProxy stops sending it requests. Its OpenSM stops with it. Its node's heartbeat ages, it is marked `fenced`, and jobs stop waiting for it | Jobs pause for up to twice the fence timeout, then carry on with two nodes |
| Instance up, its OpenSM down | Its applies fail and its node stays behind | Jobs fail after the job timeout until OpenSM is back, or an operator removes the node |
| Master OpenSM's node lost | A standby OpenSM takes over with a file that is current, since every job waited for it | No partition change |
| A node cut off from Galera | It shuts down its OpenSM after the fence timeout. The others mark it `fenced` after twice that. On reconnect it applies the current generation, then starts OpenSM | Jobs pause, then carry on. Its OpenSM can't take over with an old file |
| Galera loses quorum | Requests fail and `/healthz` fails. Every instance shuts down its OpenSM | networking-mlnx retries its requests. The fabric runs without a subnet manager until Galera is back (see Open questions) |
| Instance crashes mid-apply | The file may or may not be written. Its node's applied generation isn't recorded | It applies again on restart. The file always changes, so OpenSM always sweeps |
| Two instances apply at once | Each only writes its own node's file | No conflict |

## Alternatives considered

| Option | Why not |
| --- | --- |
| Raft among the manager instances, with a single leader | Galera already gives replicated state with quorum, so Raft would duplicate it, with a consensus implementation, its own log and membership changes to maintain. A leader also can't control which OpenSM becomes master: only fencing stops a stale one |
| One active manager, behind keepalived, that pushes the file to every OpenSM node over SSH | It needs remote access to every node, and the manager's own failover still needs shared state. A node that misses a push has no way to catch up by itself |
| Each instance with its own SQLite, behind HAProxy | A request reaches one instance, so the other OpenSMs never see it, and a poll on another instance finds no job |
| Leader election with tooz, over the MySQL or etcd backend | Not needed now, since nothing must run on only one instance. Use it if that changes, rather than a Raft of our own |

## Implementation plan

The single-node code already renders the whole state and applies it
level-triggered, so these are additions rather than a rewrite. Each step
keeps single-node SQLite working.

- [ ] Database: a `[database] connection` URL, Alembic migrations, and a
      retry on deadlock around each request's transaction.
- [ ] Generations: bump the generation when a request is recorded, store it
      on the job, and give the file a per-apply counter.
- [ ] Nodes: a `[node] name` option, the `nodes` table, heartbeats with the
      database's clock, and the applier recording its applied generation.
- [ ] Job status computed from the active nodes' applied generations, with
      a job timeout.
- [ ] Polling: each instance checks the generation every second, as well as
      being woken by its own requests.
- [ ] `/healthz` checks the database.
- [ ] Fencing: the manager supervises OpenSM in one image, starts it only
      once current, shuts it down when it loses the database and starts it
      again after catching up. Others mark stale nodes `fenced`. A CLI to
      mark a node `removed`.
- [ ] Tests: three instances against one MariaDB in CI, with faults
      injected: an instance killed, the database cut off, an apply failing.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck and database credentials.

## Open questions

- When Galera loses quorum, should the last OpenSM keep running with
  possibly stale partitions, or shut down, leaving the fabric without a
  subnet manager? Shutting down is safe for isolation, but nothing new
  comes up until the database is back.
- Supervising OpenSM inside the manager's image, or a watchdog beside the
  existing OpenSM image?
- Timeouts: fence timeout (30 s?), job timeout (120 s?), and how they fit
  networking-mlnx's own retry and Ironic's wait for ports.
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
- Do all OpenSMs share one `sm_priority`, or do we prefer one master? The
  design doesn't depend on it.
