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
  since the last confirmed stop is known unable to act:** it was released
  cleanly, or a fence that doesn't depend on its cooperation confirmed it is
  stopped or cut off from the fabric.
- **When in doubt, wait:** a takeover that can't confirm a fence doesn't
  start OpenSM, and a fenced node's fabric ports stay disabled until its
  OpenSM is known stopped.

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

The cluster's own control traffic, Galera, the management network and the
fence agents' SSH, must not run over the InfiniBand ports of the nodes that
can be fenced, since fencing disables those ports. The deployment role
checks this.

## Shared state

| Table | Holds |
| --- | --- |
| `networks` | Network ID, type, pkey (unique, NULL for flat networks), name |
| `ports` | Port ID, network (foreign key), GUID (unique), host |
| `state` | The `generation` counter; the schema version; the configuration epoch, the configuration hashes it allows, and the cluster settings (see Configuration) |
| `jobs` | Operation, object, the GUIDs and pkeys it involves, generation, effect, deadline, status, error, timestamps (UTC) |
| `lease` | One row: the leader's node and instance UUID, its term, its renewal counter, whether it was released, the **target set** (see The lease), and when leadership last moved |
| `nodes` | Per node: name, instance UUID, heartbeat counter, configuration hash, the number of expected switches it reaches, its adapters' node GUIDs and the switch ports they are cabled to (the static map), its fencing state and fence record, and its fence agents' settings |
| `applied` | The leader's last swept generation, its body, which object each partition and membership in it comes from, the GUIDs whose pkey tables OpenSM couldn't set, the switch ports whose enforcement OpenSM cleared, the leader's term, and the last fabric check's result and time |
| `quarantine` | Objects left out of rendering, and why |
| `expected_switches` | The switch GUIDs a complete fabric has (see Job completion) |

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
up. Every action of a leader on OpenSM or the fabric, and every record of
what it swept or fenced, is a compare-and-set on its term, so a deposed
leader's late writes are refused.

- **Renewal:** the leader increments the lease's renewal counter every few
  seconds, matching its instance UUID and term, on a thread of its own that
  runs through applies and fencing alike. A renewal that matches no row
  means it lost the lease: it stops OpenSM at once (see Stepping down).
- **Expiry:** every other instance watches the renewal counter on its own
  monotonic clock. The lease has expired once successful reads spanning `T`
  all show the same counter, or once it was released. A failed read resets
  the timer, so that a database outage, which stops renewals and reads
  alike, never makes a lease look expired when the database returns.
  Nothing compares the database's clock with any other.
- **The target set** holds every node that may have run OpenSM since the
  last stop that was released or confirmed. A takeover never removes a node
  from it, except with a term compare-and-set that records the node's
  confirmed fence or verified stop. So a takeover whose winner dies
  mid-fence leaves its targets, and itself, for the next.
- **Taking the lease:** a candidate first checks that its own fabric port's
  physical link is up, its logical state Init or above (Active is not
  required, since only a subnet manager makes a port Active, which would
  deadlock a cold start), and its neighbouring switch port not disabled. It counts the
  expected switches it can reach with directed-route SMPs, and publishes
  the count in its node row every few seconds. Only a candidate that reaches
  every expected switch may take the lease, or, once no candidate has for
  `T_reach`, the one that reaches most. It then takes the lease with a
  compare-and-set that includes the counter it saw unchanged, setting itself
  as leader with the term incremented, and adding itself to the target
  set. Only the winner
  proceeds. It fences every other node in the target set (see Fencing), and
  only once none is left but itself starts OpenSM.
- **Releasing the lease:** a leader that stops cleanly, for example for an
  upgrade, stops OpenSM, verifies that it has stopped (see Fencing, step 2),
  removes itself from the target set and marks the lease released. A
  successor can take it at once, and needs no fence.

A takeover whose own commit outcome is unknown counts as taken: the
instance adds itself to the target set before it can know, so a successor
fences it.

A candidate's configuration hash must be one the current configuration
epoch allows, and the candidate must not itself be fenced.

## Requests and applying

1. HAProxy sends networking-mlnx's request to any instance. The instance
   records it whenever Galera is up, leader or not, and answers with the
   job's path. A job's status is always answered, never with 503, since
   networking-mlnx answers anything but a timeout on a job poll by sending
   its request again. The exception is a Galera outage, when every
   `/healthz` fails and HAProxy itself answers 503: networking-mlnx then
   sends its requests again, which, with its default unlimited
   `retry_count`, only delays them.
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
| `ERR 0507`, `ERR 0513` | A switch port has too few pkey entries, so OpenSM **clears its enforcement bit**: the switch stops enforcing partitions on that port | Recorded against the switch port in `applied` until a PortInfo query shows its enforcement on again; a pkey error on every port behind it meanwhile, and an alarm. The leader may disable the switch port, as in Fencing, step 3, since a host behind it can send traffic with any pkey |
| `ERR 0F12` | A PKeyTable request couldn't be started | A pkey error on that port |
| `ERR 1503`, `ERR 5703` | A virtual port's pkey table couldn't be read or set | A pkey error on that port |
| `ERR 6B06` | A virtual port's partition item couldn't be allocated | A pkey error on that port |

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
4. the leader's last fabric check, recorded in `applied`, passed, and is
   no older than `2 × check_interval`.

The **fabric checks** run after every sweep and every `check_interval`:

- **Complete topology:** OpenSM discovered every switch in
  `expected_switches`. That list is configured at deployment, or learnt
  from sweeps and confirmed by an operator, so that a leader on a fragment
  of a split fabric, or one whose own link is down, doesn't count its
  fragment as the fabric.
- **No other subnet manager that matters:** the leader looks at the SMs
  its own SA knows, by their SMInfo records, and OpenSM's log since the last
  check, and sorts them by port GUID and by where they sit:
  - another managed node's SM, one whose port GUID is a managed node's and
    whose switch port is the one the static map names for it, fails the
    check, and is fenced, since it may have the fabric's keys;
  - an SM with a managed node's port GUID but on another switch port, as a
    tenant spoofing a GUID would be, is treated as unknown, with an alarm;
  - an unknown SM, such as one a bare metal tenant runs, fails the check
    only if it is master or the leader's OpenSM went to STANDBY. Without the
    SM_Key and M_Key it can't win a handover or set ports, so otherwise it
    only raises an alarm, and the leader may disable its switch port, as in
    Fencing, step 3. How OpenSM 5.17 treats such an SM is part of the
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
be unable to act on the fabric. That can't depend on those nodes'
cooperation: one may be paused, hung, or cut off from the management network
while its OpenSM still runs. For each target, the new leader records its
fence intent, the node, the step and the switch ports, with a term
compare-and-set before it acts, so that a successor can finish or undo it.
It then climbs a ladder, and the target leaves the set once step 2, 3 or 4
confirms:

1. **Ask.** Send the target's SM a SubnSet(SMInfo) with the DISABLE modifier
   and the SM_Key, which a live OpenSM honours by going to NOT-ACTIVE. It
   can't be confirmed, since a frozen SM neither honours nor answers it, so
   it never counts on its own.
2. **Stop it on the host.** Over SSH, with a key restricted to a forced
   command, a small script with two subcommands, `kill` and `status`: kill
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
4. **Power it off.** Optionally, through the target's BMC, with IPMI or
   Redfish, as Pacemaker's `fence_ipmilan` and `fence_redfish` do. It must
   power the node off, not reboot it, since a booting node could start an
   OpenSM of its own.

Steps 3 and 4 are never used against the fencer's own node: a new leader
that is also in the target set, for example because its manager restarted,
counts its own old OpenSM as stopped only on step 2's check, run locally.

Fencing a node stops its InfiniBand traffic, its own storage or workloads on
the fabric included, until it is unfenced. Every step must be idempotent,
since a successor repeats it.

If no step confirms within `[ha] fence_timeout`, the takeover doesn't start
OpenSM. The new leader keeps the lease, retries the ladder and raises an
alarm; an operator can confirm a fence with the CLI, which asks them to
check that the target's OpenSM can't act. The fabric runs on the target's
OpenSM, if it still runs, or without a subnet manager, until then.

HA mode refuses to start unless:

- M_Key protection is on, at protection level 1 or more, so that only the
  managers can disable switch ports;
- every node has the container-runtime agent and its static map
  configured, or `[ha] fencing = manual` is set, which makes every takeover
  wait for an operator.

The keys are then in Galera and its backups, as well as in OpenSM's files
and log, so the backups need the same protection.

## Unfencing

A node fenced by step 2 alone has no disabled ports: it needs only steps 1
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

## The watchdog

OpenSM's container keeps joining the manager's PID namespace, so that OpenSM
dies with the manager whenever the manager exits. Its entrypoint is a small
watchdog, which starts OpenSM only on a start request, and kills it with
SIGKILL when its lease expires, when the manager asks it to stop, or when
OpenSM exits; then the watchdog exits too, so the container stops. The
container has no restart policy. The watchdog runs in its own container's
cgroup, so pausing the manager's container doesn't pause it.

- **The lease** is renewed by a liveness thread of the manager, which
  renews it only while the manager's apply loop and lease renewal loop make
  progress: each iteration finishing, successfully or by timing out, within
  its bound. The bound allows for the longest blocking call, such as a
  sweep's wait, and every runtime and database call has a timeout shorter
  than that. A database call that fails or times out still finishes its
  iteration. So `T_watchdog` must exceed `sweep_timeout` and the call
  timeouts. A paused, hung or crashed manager stops renewing, and OpenSM
  stops within `T_watchdog`. Losing the database doesn't stop renewal: a
  leader cut off from Galera keeps its OpenSM running, so a MariaDB outage
  doesn't leave the fabric without a subnet manager, and it is fenced if
  another instance takes over.
- **The lease and the start request** are on a tmpfs the two containers
  share, so they don't survive a reboot, tagged with the host's boot ID, the
  instance UUID and the term. The deadline is in the host's monotonic clock,
  which every container on the host shares. A start request is consumed
  when used, and step 2's `kill` removes both files, so a later
  `docker start` of the container can't start OpenSM.
- **OpenSM must start as master.** An OpenSM that enters STANDBY has found
  another SM, so the start failed: the leader stops it and treats it as the
  fabric check finding another SM.
- **Before starting OpenSM,** the watchdog checks the hashes of
  `partitions.conf` and `opensm.conf` against those the start request
  names: with no partitions file, OpenSM would make every port a full member
  of the default partition, and an old `opensm.conf` could have other keys.

The watchdog can't cover a pause of its own container, or of the node: a
frozen OpenSM can resume before the watchdog does. Fencing covers that,
since a takeover fences every target before it starts another OpenSM.

The single-node manager starts OpenSM again whenever it stops. In HA mode it
doesn't: an OpenSM that stops makes its leader step down, and a new
takeover, possibly by the same node, starts it.

### Stepping down

A leader steps down, by stopping OpenSM through the watchdog, verifying with
step 2's `status` locally that no OpenSM runs, and releasing the lease,
when:

- its renewal matches no row;
- OpenSM exits;
- its configuration is no longer allowed, and a live candidate with an
  allowed configuration exists; otherwise it keeps leading and raises an
  alarm;
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
raises an alarm and retries starting OpenSM. And leadership doesn't move again within
`[ha] step_down_cooldown`, five minutes say, of its last move, recorded in
the lease row, unless the leader lost its lease or its OpenSM, so that a
fault can't bounce it between nodes, restarting OpenSM and the SA each time.
A "live" candidate is one whose heartbeat is current and that isn't in the
target set. If it can't verify that OpenSM stopped, it
stops renewing the watchdog's lease, which kills OpenSM within
`T_watchdog`, and doesn't release the lease, so that a successor fences it.

## Timeouts

A takeover takes up to `T` for the lease to expire, plus fencing, plus
starting OpenSM and its first sweep. A job in flight during a takeover must
not time out, or networking-mlnx sets its port to ERROR. So, with every
value a cluster setting in Galera, so that all instances agree:

- job timeout > `T` + `T_reach` + `fence_timeout` + OpenSM's start and first
  sweep;
- Nova's `vif_plugging_timeout` and Ironic's wait for ports > the journal's
  sync interval + its retry backoff + the job timeout.

With `T` 20 s, `T_reach` 30 s, `fence_timeout` 60 s, and 60 s for OpenSM's
start and first sweep, the job timeout must exceed 170 s; the default is
180 s, which leaves only 10 s, so a deployment whose OpenSM takes longer to
sweep must raise it. Nova's
default `vif_plugging_timeout` is 300 s, which leaves 120 s for the journal's
sync interval of 10 s and its backoff, so it holds, but only just: a
deployment that raises the job timeout must raise Nova's and Ironic's waits
too. `T_watchdog` must exceed `sweep_timeout`, 30 s, and the call timeouts;
the default is 45 s. `T_reach`, how long candidates wait for one that
reaches every switch, is 30 s.

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
| The leader's manager crashes | Its OpenSM dies with it, through the PID namespace. After `T` a candidate takes the lease, fences the old node (step 2 confirms), and starts OpenSM | Jobs wait through the takeover |
| The leader's manager paused or hung | Its watchdog's lease expires, and OpenSM stops within `T_watchdog`. A candidate takes over as above | As above |
| The leader's node, or OpenSM's container, paused | Nothing on the node runs. A candidate takes the lease and fences it: step 2 if its host answers, otherwise step 3 | Jobs wait through the takeover. A resumed OpenSM has no link |
| The leader cut off from the management network, OpenSM still running | It keeps renewing its watchdog's lease. A candidate takes the lease, can't reach it over SSH, and disables its switch ports | As above |
| The leader cut off from Galera only | Its OpenSM keeps running. A candidate takes the lease and fences it | As above |
| A takeover's winner dies mid-fence | Its targets, and itself, stay in the target set. The next candidate fences them all | Jobs wait longer |
| No fence step can confirm | The new leader doesn't start OpenSM and raises an alarm | The fabric runs on the old OpenSM, or without one, until an operator confirms a fence |
| A leader on a fragment of a split fabric | Its fabric checks fail: switches are missing. A live candidate that reaches more switches makes it step down, and takes over | Jobs wait through the takeover; alarm |
| An expected switch dies | Every node misses it, so nobody reaches more and leadership stays | No job completes until an operator removes the switch with the CLI; alarm |
| A cold start, every port in Init | Candidates need a link up and Init or above; Active isn't required | A candidate that reaches every switch takes the lease and starts OpenSM |
| A MariaDB failover | Failed reads reset the candidates' timers | No takeover; the leader's OpenSM keeps running |
| A managed node's SM appears | The fabric checks find it, and it is fenced | No job completes until then; alarm |
| A tenant runs an SM | It has no keys, so it can't master or set ports | An alarm; jobs carry on unless it is master |
| Galera unavailable | Requests and `/healthz` fail; no takeover is possible | The leader's OpenSM keeps running. networking-mlnx retries its requests |
| OpenSM exits | The leader steps down, verifying that no OpenSM runs, and a candidate takes over without fencing it | Jobs wait through the takeover |
| An object breaks the render or OpenSM's parse | It is quarantined and its jobs fail | Every other change carries on |
| A port's pkey table can't be set | Jobs involving its GUID wait, and the leader retries | Other jobs carry on; the leadership stays |
| A deposed leader writes late | Its compare-and-set on the term fails | Nothing happens |
| A port is created then deleted before a sweep | No swept body has a membership attributed to it | The create times out and fails; the delete completes |

## Alternatives considered

| Option | Why not |
| --- | --- |
| OpenSM's own master and standbys on every node | A standby takes over by itself, with whatever file it has, which nobody may have applied, and jobs need evidence from every node. One OpenSM at a time makes a job's evidence one sweep. Its election would keep the SA up through a failure, and work without Galera, which this design gives up |
| Raft among the manager instances | Galera already gives replicated state with quorum. Raft would add a second consensus that can disagree with it, and a Raft leader is still a lease that a paused process can outlive, so fencing is needed anyway |
| tooz's MySQL backend for the lease | It uses `GET_LOCK`, which Galera doesn't replicate. tooz with etcd would do, where a deployment runs etcd |
| Renewing the watchdog's lease only on database success | A MariaDB outage would then stop every OpenSM; fencing at takeover covers a leader cut off from the database |
| Taking over unfenced, at a boosted priority, so a stale master steps down | A resumed stale master sweeps until it notices the other master, and can undo a completed job, such as put back a deleted port, before it steps down |
| Fencing by M_Key rotation | OpenSM has no supported online key rotation; revisit with key rotation |
| Answering 503 without a leader | networking-mlnx backs off on it, and resends a job poll answered with it; recording requests whenever Galera is up is simpler and loses nothing |

## Implementation plan

- [ ] Verify on a fabric with two OpenSMs: SMInfo DISABLE; switch-port
      disabling and its confirmation with the M_Key; the SA's SMInfo
      records of another SM; OpenSM's log when it finds one; how it treats
      an SM without its SM_Key and M_Key, as a tenant's would be;
      directed-route reachability of switches from a candidate's port in
      Init.
- [x] Verify on the test host: `docker kill` stops a paused container under
      cgroup v2; OpenSM 5.17's codes for pkey-table errors.
- [x] Single node: a DELETE of an unknown object creates a job rather than
      answering 404; the checks of rendered files; the managed driver; and
      reconciling the file with the database.
- [ ] Single node: the pkey error codes, recorded per GUID; quarantine of
      the objects that break a render or OpenSM's parse; the render's
      attribution of partitions and memberships to objects.
- [ ] Single node: jobs record their effect and deadline, and complete only
      when the effect holds in the swept body.
- [ ] Single node: the fabric checks, against expected switches and other
      SMs.
- [ ] Database: a `[database] connection` URL, Alembic migrations, schema
      version, configuration epochs and cluster settings,
      `wsrep_sync_wait`, UTC timestamps, deadlock retry, the generation bump
      first in every request, constraint errors as 400.
- [ ] The lease: taking, renewing, releasing, expiry on the monotonic clock,
      terms in every compare-and-set, and the target set.
- [ ] The watchdog: an entrypoint for OpenSM's container, with the lease and
      single-use start requests on tmpfs, and the file hashes.
- [ ] Fencing: the recorded intent, SMInfo DISABLE, the restricted SSH
      script, switch-port disabling with the fabric scan, the static map and
      the neighbour checks, its re-assertion, optional BMC agents, retry by
      the next leader, and unfencing.
- [ ] A CLI to confirm fences, unfence nodes, record configuration epochs,
      confirm or remove expected switches, and force a step-down.
- [ ] Tests: three instances against one MariaDB in CI, and a Galera cluster
      in containers, with faults injected: the leader killed, paused, cut
      off from the database or the network; a winner dying mid-fence; a
      fence failing; a quarantined object; a deposed leader writing late; a
      fabric missing a switch.
- [ ] Deployment: a Kayobe or Kolla-Ansible role with the HAProxy backend,
      healthcheck, database credentials, SSH keys, the static map, expected
      switches, and the check that control traffic doesn't run over fenced
      ports.

## Open questions

- Does OpenSM 5.17 honour SMInfo DISABLE, and how quickly?
- Can the switches' ports be disabled reliably with the M_Key, and does a
  disabled port stay disabled whatever its neighbour does?
- How does OpenSM 5.17 log another SM, and do its SA's SMInfo records show
  every SM on the fabric?
- What is the deployment's MariaDB failover time, and are 20 s for `T` and
  180 s for the job timeout right?
- Do the OpenSM nodes have BMCs the manager may use?
- Do the OpenSM nodes run the control plane, so they reach the database
  VIP, or are they separate hosts that need database access opened to them?
