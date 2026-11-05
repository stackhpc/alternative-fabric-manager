# High availability

Status: proposed, 2026-11-05.

Run a fabric manager instance on every OpenSM node, behind HAProxy, with the
shared state in the deployment's Galera cluster. Exactly one instance, the
**leader**, runs OpenSM at a time. The others serve the API and stand by to
take over. Leadership is a lease in Galera: Galera's quorum is the only
consensus, and there is no Raft.

Running one OpenSM, rather than OpenSM's own master and standbys on every
node, means no standby can take over with partitions that nobody applied,
and a job's evidence is one OpenSM's sweep. The cost is that a takeover
starts an OpenSM, rather than promoting a running one.

Three principles carry the safety argument:

- **A job completes only on evidence that the fabric has its effect:** the
  current leader's OpenSM swept a file in which the job's effect holds,
  in the leader's current term.
- **A new leader starts OpenSM only once the old leader's OpenSM is known
  unable to act:** it released cleanly, or a fence that doesn't depend on
  its cooperation confirmed it is stopped or cut off from the fabric.
- **When in doubt, wait:** a takeover that can't confirm a fence doesn't
  happen, and a fenced node's fabric port stays disabled until its OpenSM
  is known stopped.

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
renders the partitions file and runs OpenSM. No other subnet manager may run
on the fabric, such as one embedded in a switch: the leader checks that the
fabric's master SM is its own OpenSM (see Job completion).

## Shared state

| Table | Holds |
| --- | --- |
| `networks` | Network ID, pkey (unique, NULL for flat networks), name |
| `ports` | Port ID, network (foreign key), GUID (unique), host |
| `state` | The `generation` counter; the schema version; the configuration epoch and the configuration hashes it allows |
| `jobs` | Operation, object, generation, effect, status, error, timestamps (UTC) |
| `lease` | One row: the leader's node and instance UUID, its term, its renewal counter, and the takeover in progress, if any |
| `nodes` | Per node: name, instance UUID, heartbeat counter, configuration hash, the GUIDs of its fabric ports and the switch ports they are cabled to, its fencing state, and its fence agents' settings |
| `applied` | The leader's last swept generation and body, with its term |
| `generations` | Generations OpenSM rejected, and why |

A request's transaction starts by bumping the generation, with
`UPDATE state SET generation = generation + 1`, which locks the row. Only
then does it read and validate, so that its checks see every change
committed before it, under InnoDB's repeatable read. It then records the
change and creates its job with that generation and its effect. Every write
goes to one Galera node (see Galera specifics), so requests are serialised
on the generation row, and a job's generation is exactly the first state
that includes it.

A job's **effect** is a set of assertions about the partitions file, in
terms of GUIDs and pkeys, taken from the request:

| Request | Effect |
| --- | --- |
| Create or update a VLAN network | Its pkey's partition exists |
| Delete a network | Its pkey's partition doesn't exist; none of its ports' GUIDs is in a network's partition |
| Create or update a port | Its GUID is a full member of its network's partition (or the flat partition), and of no other network's partition |
| Delete a port | Its GUID is in no network's partition, nor the flat partition |

A DELETE of a network or port that isn't recorded takes its pkey or GUID
from the request's body, which networking-mlnx sends on DELETE as on create,
and still bumps the generation and creates a job.

## The lease

The lease row names the leader and its **term**, a counter that only goes
up. Every action of a leader on OpenSM, and every record of what it swept,
carries the term, and is a compare-and-set on it, so a deposed leader's late
writes are refused.

- **Renewal:** the leader increments the lease's renewal counter every few
  seconds, matching its instance UUID and term. A renewal that matches no
  row means it lost the lease: it stops OpenSM at once (see Stepping down).
- **Expiry:** every other instance watches the renewal counter on its own
  monotonic clock. Once it hasn't changed for `T`, the lease has expired,
  and the instance may try to take over. Nothing compares the database's
  clock with any other.
- **Taking the lease:** a candidate takes the lease with a compare-and-set
  that includes the counter it saw unchanged, setting itself as leader with
  the term incremented, and the old leader as the takeover's target. Only
  the winner proceeds. It then fences the old leader (see Fencing), and
  only on a confirmed fence starts OpenSM.
- **Releasing the lease:** a leader that stops cleanly, for example for an
  upgrade, stops OpenSM, verifies that it has stopped, and clears the
  lease's target. A successor then needs no fence.

A candidate must have a configuration whose hash the current configuration
epoch allows, and must not itself be fenced.

## Requests and applying

1. HAProxy sends networking-mlnx's request to any instance. With no leader,
   for example during a takeover, it answers 503, which networking-mlnx
   retries, rather than accepting a job that can't complete in time.
2. The instance records the change and its job, as above, and answers with
   the job's path.
3. The leader polls the generation, every second or so, and is woken at once
   when it took the request itself.
4. When it is behind, it reads the generation and the whole state in one
   consistent-read transaction, renders the partitions file, checks it (see
   Configuration), writes it atomically and fsyncs it, sends OpenSM SIGHUP,
   and waits for the sweep that starts after the signal to end, with no
   errors (see OpenSM errors).
5. It records the generation and the body it swept in `applied`, with a
   compare-and-set on its term.

Applying is level-triggered: the leader always renders the newest state,
never replays changes. The file's header carries the generation and an
attempt counter, so that a retry still changes the file and OpenSM runs a
pkey sweep. Every `check_interval`, the leader also reconciles: if its file
isn't what it last applied, or the state rendered at that generation
differs, it applies again.

### OpenSM errors

OpenSM skips lines of a partitions file it can't parse and still finishes
the sweep, and it logs, rather than fails on, a port whose pkey table it
couldn't set. The leader treats both as a failed apply when they appear
between the signal and the end of the sweep:

- a partitions file parse error, `PARSE ERROR` from `parse_part_conf`;
- the pkey manager's and virtualisation manager's errors setting pkey
  tables, by their codes in OpenSM 5.17, from its binary's messages:

| Code | Meaning | Treated as |
| --- | --- | --- |
| `ERR 0502` | No memory for a pending pkey entry | A failed apply of that port |
| `ERR 0505`, `ERR 0508` | A pkey couldn't be set in a block and index of a port's table | A failed apply of that port |
| `ERR 0506`, `ERR 0509` | A port's pkey table block couldn't be updated | A failed apply of that port |
| `ERR 0512` | A pkey couldn't be set because the port's table is full | A failed apply of that port |
| `ERR 0507`, `ERR 0513` | A switch port has too few pkey entries, so OpenSM **clears its enforcement bit**: the switch stops enforcing partitions on that port | A failed apply of every job, since isolation is lost at that switch port, and an alarm |
| `ERR 0F12` | A PKeyTable request couldn't be started | A failed apply of that port |
| `ERR 1503`, `ERR 5703` | A virtual port's pkey table couldn't be read or set | A failed apply of that port |
| `ERR 6B06` | A virtual port's partition item couldn't be allocated | A failed apply of that port |

Codes outside this list, such as the SA's `ERR 45xx` for requests between
ports that share no pkey, are about clients, not about applying the file,
and aren't treated as failures. The list is matched by code, `ERR 0507:`
for example, since the message texts may change between OpenSM versions;
a newer OpenSM's codes need checking before it is supported.

A parse error is a property of the generation, not of the node: the same
file fails on every OpenSM. So the leader records the generation as
rejected, fails its jobs, writes and applies the last body it swept again,
and doesn't render again until the state changes. A pkey-table error on a
port fails the jobs whose effects involve that port's GUID, and the leader
retries at the next reconcile.

## Job completion

networking-mlnx holds a port DOWN until its job completes. A job at
generation G is **Completed** when, in `applied`:

1. the leader's term is the current term;
2. the swept generation is at least G;
3. the job's effect holds in the swept body, parsed as the checks parse it;
4. the leader has checked, since its OpenSM started in this term, that the
   fabric's master SM is its own OpenSM, by its port's GUID.

Because the effect is checked against what was actually swept, a later
generation can't complete an earlier job whose effect it undid: a port
created at G and deleted at G+1 never has its create's effect in a swept
body, so the create times out and fails.

A job is **Failed** once its generation was rejected, or once the
database's UTC time is past its creation time plus the job timeout, and it
isn't Completed. Otherwise it is **Pending**. Status is computed when a job
is read, in that order, and written to its row the first time it is
terminal, with `UPDATE ... WHERE status IN ('Pending', 'Running')`; a
reader whose update matches no row returns the stored status.

A job whose effect can't hold in the current state, for example a port's
create after the port was deleted, may be failed at once rather than at its
timeout. That is an optimisation, not what makes completion safe.

A request whose commit outcome is unknown, and which networking-mlnx sends
again, creates a second job with the same effect. That is harmless.

## Fencing

Before a new leader starts OpenSM, the old leader's OpenSM must be unable to
act on the fabric. That can't depend on the old leader's cooperation: it may
be paused, hung, or cut off from the management network while its OpenSM
still runs. The new leader climbs a ladder, and the takeover proceeds once
step 2, 3 or 4 confirms:

1. **Ask.** Send the old SM a SubnSet(SMInfo) with the DISABLE modifier and
   the SM_Key, which a live OpenSM honours by going to NOT-ACTIVE. It can't
   be confirmed, since a frozen SM neither honours nor answers it, so it
   never counts on its own; it only makes the next steps gentler.
2. **Kill the container.** Over SSH, with a key restricted to one forced
   command, kill the old node's OpenSM container and confirm, through the
   container runtime, that it has exited. This covers a paused or hung
   manager, and a paused OpenSM container: under cgroup v2, `docker kill`
   stops a paused container at once, as checked on this host. It needs the
   node to be reachable and running.
3. **Disable its switch ports.** With the M_Key, send SubnSet(PortInfo) to
   the switch ports the old node's adapters are cabled to, setting them to
   Disabled, and confirm by querying them that they are disabled and their
   links down. The switch acts on it in its own firmware, whatever the old
   node is doing, and the old node can't undo it: it has no path to the
   switch. This covers a paused or hung node, and one cut off from the
   management network. Before disabling a port, the leader checks that the
   port's neighbour is the GUID the node registered, so that a wrong
   mapping can't cut off another node. It needs M_Key protection, so that
   only the managers can do it.
4. **Power it off.** Optionally, through the node's BMC, with IPMI or
   Redfish, as Pacemaker's `fence_ipmilan` and `fence_redfish` do, where the
   deployment has BMC credentials.

Fencing is recorded in the node's row: the fencer, the step that confirmed,
and a deadline. Only the instance that took the lease runs the fence. If it
dies, the lease expires again, and the next candidate takes it and repeats
the fence, so every step must be idempotent. Disabling a fenced node's ports
is a lasting state until it is unfenced: its fabric traffic, its storage or
workloads on InfiniBand included, stops.

If no step confirms within `[ha] fence_timeout`, the takeover doesn't start
OpenSM. The new leader keeps the lease, answers 503, retries the ladder, and
raises an alarm; an operator can confirm a fence with the CLI, which asks
them to check that the old node's OpenSM can't act. The fabric then runs on
the old OpenSM, if it still runs, or without a subnet manager, until then.

HA mode refuses to start unless every node has the container-runtime agent
and switch-port fencing configured, or `[ha] fencing = manual` is set,
which makes every takeover wait for an operator.

### The priority boost

As an opt-in, `[ha] unfenced_takeover = priority`, a new leader whose fence
can't be confirmed may start OpenSM anyway at a boosted priority: OpenSM's
masters are elected by priority, so a stale master that resumes should see a
higher-priority master and step down. Leaders normally run at a base
priority, and drop back to it, which needs an OpenSM restart, once the old
leader is confirmed fenced or unfenced. At most one possibly-alive node may
run boosted, recorded in the lease row; a takeover that finds one waits for
a fence.

It isn't a fence: a resumed stale master sweeps until it notices the other
master, and may set ports' pkey tables first. It stays off until testing on
a fabric with two SMs shows that a lower-priority stale master steps down
before it touches pkeys. Jobs still need the new leader's own sweep, so
nothing completes on a stale SM's word.

## Unfencing

A fenced node's switch ports stay disabled until its OpenSM is known stopped
and unable to start by itself:

1. Its manager comes back and registers, with a new instance UUID, and sees
   that it doesn't hold the lease.
2. It tells its watchdog to stop OpenSM, if it still runs, for example after
   a network partition rather than a crash, and records that OpenSM is
   stopped. The watchdog only starts OpenSM on a start request for a term
   the node holds, so OpenSM can't come back by itself.
3. It asks to be unfenced, in its node row.
4. The leader checks independently, through the container-runtime agent,
   that the node's OpenSM container isn't running, and that its
   configuration's hash is allowed.
5. The leader re-enables the node's switch ports, confirms that they are up,
   and its OpenSM brings the node's ports to Active and sets their pkey
   tables.
6. The node becomes a candidate again.

If the leader can't verify, the ports stay disabled until an operator
unfences the node with the CLI.

## The watchdog

OpenSM's container runs a small watchdog as its PID 1, with no restart
policy. It starts OpenSM only on a start request from the manager, and kills
it with SIGKILL, then exits, so that the container stops, when its lease
expires, when the manager asks it to stop, or when OpenSM exits.

- **The lease** is renewed by the manager's own liveness: its heartbeat
  thread renews it only while the apply loop makes progress, and every
  runtime call has a timeout. A paused, hung or crashed manager stops
  renewing it, and OpenSM stops within `T_watchdog`. Losing the database
  doesn't stop renewal: a leader cut off from Galera keeps its OpenSM
  running, so a MariaDB outage doesn't leave the fabric without a subnet
  manager, and it is fenced if another instance takes over.
- **The lease and the start request** are on a tmpfs the two containers
  share, so they don't survive a reboot, tagged with the host's boot ID, the
  instance UUID and the term. The deadline is in the host's monotonic clock,
  which every container on the host shares. The watchdog refuses a request
  for another boot, instance or term.
- **Before starting OpenSM,** the watchdog checks that the partitions file
  exists and has the generation the start request names: with no file,
  OpenSM would make every port a full member of the default partition.

The watchdog can't cover a pause of its own container, or of the node: a
frozen OpenSM can resume before the watchdog does. Fencing covers that,
since a takeover fences before it starts another OpenSM.

The single-node manager starts OpenSM again whenever it stops. In HA mode it
doesn't: an OpenSM that stops makes its leader step down, and a new leader's
takeover, possibly the same node's, starts it.

### Stepping down

A leader steps down, by stopping OpenSM through the watchdog, verifying that
the container exited, and releasing the lease, when its renewal matches no
row, when OpenSM exits, when it can't apply for reasons other than a
rejected generation past a threshold, or when its configuration is no longer
allowed. If it can't verify that OpenSM stopped, it stops renewing the
watchdog's lease, which kills OpenSM within `T_watchdog`, and doesn't
release the lease, so that a successor fences it.

## Timeouts

A takeover takes up to `T` for the lease to expire, plus the fence, plus
starting OpenSM and its first sweep. Jobs in flight during a takeover must
not time out, or networking-mlnx sets their ports to ERROR, and a failed
DELETE blocks every later create of its GUID until the journal is repaired.
So:

`job timeout > T + fence_timeout + OpenSM start + first sweep`

and Nova's and Ironic's waits for ports must be longer still. With the
example values, `T` 30 s, `fence_timeout` 60 s, OpenSM start and sweep
30 s, the job timeout must exceed two minutes; 300 s is a safer default.
`T_watchdog`, the watchdog's lease, can be short, 15 s, since it depends
only on the manager's own liveness.

## Configuration

The leader writes both of OpenSM's files:

- **`opensm.conf`** is built from the hardening groups and the operator's
  overrides. The manager sets the keys it depends on itself, such as
  `partition_config_file`, and refuses overrides of them.
- **`partitions.conf`** is rendered from a Jinja template, in a sandboxed
  environment with undefined variables as errors.

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
networks.

### Configuration epochs

The configuration's hash is stored with an epoch, which allows one hash, or
two during a rolling change. Only an instance whose configuration is
allowed may become leader; a leader whose configuration is no longer
allowed steps down. Only one OpenSM runs, so a change is applied by the
leader restarting it:

- **Rolling changes** are an allowlist in code: `sweep_interval`, routing,
  logging and the manager's own timeouts. The operator records the new
  hash, updates the candidates, then the leader, which restarts OpenSM.
- **Everything else** is fabric-affecting, the keys included: the operator
  records the new epoch with only the new hash, so that only updated
  candidates can lead, and the leader steps down. Changing a key needs a
  key rotation procedure, which is future work: until then the manager
  refuses to start with keys other than the stored ones.

During a change, requests get 503 while there is no leader.

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
- **UTC.** Timestamps are `DATETIME(6)` values set with
  `UTC_TIMESTAMP(6)`, and the timeout compares them with it, so that no
  session's timezone or daylight saving change can age a job. SQLite uses
  its own UTC functions. Galera's nodes need synchronised clocks, within
  much less than the job timeout, since a job's timeout is judged by
  whichever node serves the read. Lease expiry doesn't use the database's
  clock.
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
  version that can't, says so, and needs every instance stopped.
- **From single node:** with the manager stopped, which stops OpenSM too,
  so the fabric has no subnet manager meanwhile:
  1. copy the networks, ports and keys into Galera, and continue the
     generation above the single-node one, which the partitions file's
     header carries;
  2. copy the jobs, keeping their IDs, since journal rows that are
     monitoring a job would otherwise get 404 and send their requests
     again;
  3. keep terminal jobs as they are, and give every unfinished job,
     `Pending` or `Running`, the migration's generation, status `Pending`,
     and its effect, from its operation and object.

  The unfinished jobs then complete, or fail, once the first leader has
  swept a state in which their effects hold.
- **Job history.** Purge finished jobs after a retention period, from any
  instance, since the purge is idempotent.

## Failure scenarios

Every case fails closed: a port waits, or its request gets 503, rather than
becoming ACTIVE before the leader's OpenSM has swept its partition.

| Failure | What happens | Outcome |
| --- | --- | --- |
| The leader's manager crashes | Its watchdog kills OpenSM within `T_watchdog`. After `T` a candidate takes the lease, fences the old node (its container is already stopped), and starts OpenSM | Jobs wait through the takeover |
| The leader's manager paused or hung | As above: the watchdog lease isn't renewed | As above |
| The leader's node, or OpenSM's container, paused | Nothing on the node runs. A candidate takes the lease and fences it: the container kill if the runtime answers, otherwise its switch ports are disabled | Jobs wait through the takeover. A resumed OpenSM has no link |
| The leader cut off from the management network, OpenSM still running | It keeps renewing its watchdog lease. A candidate takes the lease, can't reach it over SSH, and disables its switch ports | As above |
| The leader cut off from Galera only | Its OpenSM keeps running. A candidate takes the lease and fences it | As above |
| No fence step can confirm | The new leader doesn't start OpenSM, answers 503 and raises an alarm | The fabric runs on the old OpenSM, or without one, until an operator confirms a fence |
| Galera unavailable | Requests and `/healthz` fail; no takeover is possible | The leader's OpenSM keeps running. networking-mlnx retries its requests |
| OpenSM exits | The leader steps down, verifying the container exited, and a candidate takes over without fencing | Jobs wait through the takeover |
| OpenSM rejects a generation | It is recorded as rejected, its jobs fail, and the leader applies the last body it swept again | Later changes carry on. No node is fenced |
| A port's pkey table can't be set | The jobs involving its GUID fail, and the leader retries at the next reconcile | Other jobs carry on |
| A deposed leader writes late | Its compare-and-set on the term fails | Nothing happens |
| A port is created then deleted before a sweep | The create's effect never holds in a swept body | The create times out and fails; the delete completes |

## Alternatives considered

| Option | Why not |
| --- | --- |
| OpenSM's own master and standbys on every node | A standby takes over by itself, with whatever file it has, which nobody may have applied, and jobs need evidence from every node. One OpenSM at a time makes a job's evidence one sweep |
| Raft among the manager instances | Galera already gives replicated state with quorum. Raft would add a second consensus that can disagree with it, and a Raft leader is still a lease that a paused process can outlive, so fencing is needed anyway |
| tooz's MySQL backend for the lease | It uses `GET_LOCK`, which Galera doesn't replicate. tooz with etcd would do, where a deployment runs etcd |
| Renewing the watchdog's lease only on database success | A MariaDB outage would then stop every OpenSM; fencing at takeover covers a leader cut off from the database |
| The priority boost as the only way to take over | It isn't a fence: a resumed stale master may set pkeys before it steps down. It is an opt-in, after testing |
| Fencing by M_Key rotation | OpenSM has no supported online key rotation; revisit with key rotation |

## Implementation plan

- [ ] Verify on a fabric with two OpenSMs: SMInfo DISABLE; switch-port
      disabling and its confirmation with the M_Key; how a lower-priority
      stale master behaves; `sminfo` with the M_Key and SM_Key.
- [x] Verify on this host: `docker kill` kills a paused container's
      processes under cgroup v2; OpenSM 5.17's codes for pkey-table errors
      (see OpenSM errors).
- [x] Single node: a DELETE of an unknown object creates a job rather than
      answering 404; the checks of rendered files; the managed driver; and
      reconciling the file with the database.
- [ ] Single node: treat pkey manager errors as failed applies, and record
      rejected generations rather than retrying them.
- [ ] Single node: jobs record their effect, and complete only when it holds
      in the swept body.
- [ ] Database: a `[database] connection` URL, Alembic migrations, schema
      version, configuration epochs, `wsrep_sync_wait`, UTC timestamps,
      deadlock retry, the generation bump first in every request, constraint
      errors as 400.
- [ ] The lease: taking, renewing, releasing, expiry on the monotonic clock,
      terms in every compare-and-set, 503 without a leader.
- [ ] The watchdog: an entrypoint for OpenSM's container, with the lease and
      start requests on tmpfs.
- [ ] Fencing: SMInfo DISABLE, the restricted SSH agent, switch-port
      disabling with the neighbour check, optional BMC agents, recording,
      retry by the next leader, and unfencing.
- [ ] A CLI to confirm fences, unfence nodes, and record configuration
      epochs.
- [ ] Tests: three instances against one MariaDB in CI, and a Galera cluster
      in containers, with faults injected: the leader killed, paused, cut
      off from the database or the network; a fence failing; OpenSM
      rejecting a generation; a deposed leader writing late.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck, database credentials, SSH keys and the switch-port map.

## Open questions

- Does OpenSM 5.17 honour SMInfo DISABLE, and how quickly?
- Can the switches' ports be disabled reliably with the M_Key, and does a
  disabled port stay disabled whatever its neighbour does?
- What is the deployment's MariaDB failover time, and are 30 s for `T` and
  300 s for the job timeout right?
- Do the OpenSM nodes have BMCs the manager may use?
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
