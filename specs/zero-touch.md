# Zero-touch operation

**What it decides.** Which operator steps the manager does by itself:

- recording the **expected switches**;
- **switch trust**, which it drops;
- the **per-port M_Key turn-on**, and its turn-off.

It applies in single node and in HA mode. In HA mode the leader does what
the single-node manager does, behind the leader fence (specs/ha.md, The
secured profile).

**Who it's for.** Developers changing the fabric check, the prober or the
per-port M_Key code, and reviewers checking those changes.

**Status.** Implemented: `tests/test_zero_touch.py` covers it, and the code
cites it. Per-port M_Keys, which most of it concerns, are experimental.

**The principle.** The operator should have nothing to do in normal
running (AGENTS.md, Usability):

- A manual step stays only where automating it would open a real hole.
- A step that protects an edge case gives way to usability.

`--confirm-fabric` stays, as a shortcut, and for the one case below that
needs a person (What stays manual).

## Terms

| Term | Meaning |
| --- | --- |
| **Expected switches** | The completeness list: the switches every fabric check must find, kept in `expected_switches` rows. A check, or HA mode's reach probe, counts the expected switches it finds. |
| **`confirm_checks`** | `[opensm] confirm_checks`: how many fabric checks in a row must see a switch before the manager records it. 12 by default, a minute at the default `check_interval` of 5 seconds. |
| **Rows** | Per-port M_Key rows: the ports and links the per-port M_Key code records. "With rows" means they exist. |
| **Source** | Where a check's list of switches comes from: `ibswitches`' node GUIDs without rows, or the prober's GUIDs with them. |
| **Leaf** | A switch whose adapters are reached only through it. |
| **Target** | Whether per-port M_Keys should be on or off. OpenSM's next start applies it, so it can differ from what OpenSM runs with. |
| **Arming** | Recording the target on, once the arming's checks pass. |
| **Applying** | Restarting OpenSM so that its start applies the target. |
| **Failed turn-on** | A turn-on set back to off after failing, and recorded in `state`. |
| **Contested** | A check's own finding of another master, or of an SM the SA records that may hold the keys. |
| **Epoch** | HA mode's configuration epoch: the configuration hashes leaders may run with (`--config-epoch`). |

## Running the commands

The commands below run in the manager's container, as README.md shows
them. In a Kayobe HA deployment the container is `alternative_fabric_manager`
(docs/kayobe.md): substitute that name in each command.

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf <command>
```

> [!WARNING]
> Naming `m_key_per_port` in `[opensm] hardening` is the decision to turn
> per-port M_Keys on, and the manager then arms and applies the turn-on by
> itself, re-keying every port. On a deployment that names it,
> `--confirm-fabric` arms the turn-on at once. Agree the hardening with
> whoever shares the fabric. docs/opensm-hardening.md, Per-port M_Keys,
> has the procedure for turning them on and off.

## What stays manual

| Step | Why it stays manual |
| --- | --- |
| Removing a switch that may still forward | Automating it could let jobs complete past a live switch. |
| Naming and removing hardening groups | It's the operator's decision, agreed with whoever shares the fabric. |
| What specs/ha.md keeps manual for safety | For example `--force-unfreeze`, and `--forget-node` for a node that died. |

### Removing a switch that may still forward

A switch missing from `ibswitches` may be either:

- gone; or
- alive, with its management agent hung, its links up and its pkey tables
  stale.

Dropping a live one would let jobs complete past it. The manager can't
always tell the two apart:

- **Without per-port M_Key rows,** nothing tells them apart.
- **With rows,** only a switch whose every recorded link is down can be
  told gone. A leaf switch never is: the adapters behind it are reached
  only through it.

So a removed or replaced switch needs `--confirm-fabric`:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --confirm-fabric
```

With rows, a dead leaf's ports need `--release-port` too, one GUID per run,
or their jobs wait (specs/secured-profile.md, Lost ports and jobs):

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --release-port <guid>
```

That's a hardware change, made by someone at the fabric anyway.

### Backing up the database is not a gate

Backing up the database before per-port M_Keys begin is no longer a step
the manager waits for. The old gate never checked a backup either: it
required a restart.

The base is stored a release before any port can be keyed with it. From
then on, these backups hold it:

- every deployment's routine backups;
- the deploy-host skill's backup before each redeploy.

Galera's replication isn't a backup, so HA deployments need the same
backups.

**Losing the database with no backup taken since** is an operator failure,
and an edge case. But its cost is high with leases of 0:

- every port keeps a key no SM holds;
- a power cycle of the whole fabric clears only volatile ones;
- adapters with a non-volatile M_Key need reconfiguring on their own
  hosts, which may be tenants'.

The default `[opensm] key_lease_period`, an hour, is expected to make such
a loss recoverable by waiting. That is not yet shown on hardware. A
deployment that sets it to 0 accepts that cost, and
docs/opensm-hardening.md says so.

A start that stored the base still doesn't begin per-port M_Keys
(specs/secured-profile.md, A new base key).

## Switch trust is dropped

**Before this spec,** a switch the prober found for the first time was
pending: neither expected nor trusted, until a second `--confirm-fabric`
found it again. Trust gated four things:

1. which switches the prober routes through;
2. which rows count as infrastructure in `keys_to_try`;
3. which switches a check counts;
4. when a full verify is re-run.

It protects too little to cost an operator step:

- **Spoofing a switch needs custom adapter firmware.** The adapter's
  firmware answers SMPs; a virtual function can't answer for itself at
  all. Only a bare metal tenant could try, with firmware the adapter
  accepts. For recent ConnectX adapters, that means NVIDIA-signed.
- **OpenSM trusts NodeInfo anyway.** Its discovery and directed-route
  sweeps go through whatever answers as a switch, carrying the derived
  keys of the ports beyond it, whatever AFM trusts. So OpenSM and the
  prober alike show a spoofer on a route those keys. That is the
  duplicate-GUID limit specs/secured-profile.md already accepts, since it
  needs OpenSM to have recorded the spoofer's links.
- **The stored M_Key goes only where OpenSM would send it.** `keys_to_try`
  changes for a switch that meets all of these:
  - it isn't this host's own port;
  - the target is on;
  - anything is possibly derived.

  Such a switch is sent the stored M_Key only if its row holds it, because
  OpenSM sends it only where `guid2mkey` names it. A row that's `unknown`
  gets its derived key alone, as a tenant's does. So treating every switch
  as infrastructure hands a spoofer no key OpenSM wouldn't.

**What replaces trust:**

- The prober follows every row that answers as a switch, as
  `--confirm-fabric` already does (`follow_unconfirmed`).
- The expected switches are only the completeness list.
- What a check, or HA mode's reach probe, counts is the expected switches
  found. A new switch, or a spoofer, never makes a fragment look complete,
  nor a candidate reach more.
- `pending` stays in the schema, for the previous version during a rolling
  upgrade. The new code ignores it, and writes it false.

## Expected switches

The applier's fabric check (the leader's, in HA mode) counts, in memory,
how many checks in a row have seen each switch. It compares the count with
`[opensm] confirm_checks`, 12 by default (a minute at the default
`check_interval`).

### What each check does

| Case | What happens |
| --- | --- |
| None recorded yet | Recorded once a non-empty set has been stable. |
| A new switch | Added once stable. |
| The source changed | Recorded switches translated by node GUID. |
| A missing switch | Fails the check; dropped only on evidence its links are down. |
| A switch row that doesn't answer, behind a link that's up | Fails the check, expected or not. |

**None recorded yet**, as on a new deployment:

- Once every switch of a non-empty set has been seen by `confirm_checks`
  checks in a row, the set is recorded, and logged.
- Jobs wait meanwhile. The check says "No expected switches yet:
  recording them once the fabric has been stable for N checks".

**A new switch**, seen by `confirm_checks` checks in a row, is added.

**The source changed.** Switches are known by `ibswitches`' node GUIDs
without per-port M_Key rows, and by the prober's GUIDs with them. These can
differ.

- The source the list was recorded from is kept in `state`.
- Each `expected_switches` row keeps the switch's node GUID beside the GUID
  it was recorded by (from the prober's NodeInfo, or `ibswitches`' own).
- When the check's source differs, each recorded switch is translated by
  its node GUID. So the translation works both ways, as after
  `--forget-port-keys`.
- A recorded switch the new source can't match stays as it is: expected,
  and failing the check as missing. So a switch already missing isn't
  dropped by the change.

**A missing switch** fails the check, as today. Grants that don't depend on
it still complete (specs/ha.md, While a switch is missing).

- **With rows,** it's dropped once the prober shows every recorded link to
  it down, from the switch ports and adapters at its other ends. That is
  the same evidence of a link down that a job's port gives
  (specs/secured-profile.md, Lost ports and jobs).
- That can happen for a spine. A leaf whose adapters are reached only
  through it never shows it (What stays manual).
- A switch whose links are up but which doesn't answer stays, and holds
  jobs, since OpenSM can't program it.
- The evidence is the check's own verify, never an older one, so a link
  that came back up since isn't missed.

**A switch row that doesn't answer, behind a link that's up,** fails the
check, expected or not. It may forward with stale pkey tables, as a hung
expected switch may. It fails the check until either:

- it answers; or
- an operator runs `--release-port` for it:

  ```sh
  docker exec alternative-fabric-manager alternative-fabric-manager \
    --config-file /etc/alternative-fabric-manager/afm.conf --release-port <guid>
  ```

So a spine dropped while its links were down, that comes back with its
agent hung, holds jobs again, rather than letting them complete past it.

An adapter answering as a switch that then refuses its key holds the
fabric the same way, as a lost non-Neutron port does
(specs/secured-profile.md, Lost ports and jobs), and the alarm names it.

### When nothing changes

Nothing is added, dropped or re-recorded, and the counts start again, while
any of these holds:

- **The check couldn't query the fabric.** This includes a verify that ran
  out of time: part of the switches would look like all of them, and a
  switch it didn't reach like one that's gone. A check that fails because
  the scoped verify of a job's ports ran out of time fails as a check whose
  query failed.
- **The check was contested.**
- **OpenSM isn't master.**

"Contested" here is the check's own finding (another master, or an SM the
SA records that may hold the keys). It is not the manager's lasting flag,
which only a passing check clears:

- a fabric with no expected switches yet can't pass, so with the flag the
  list could never be recorded;
- a check that sees no other SM is evidence enough for a list of switches,
  which no SM's sweep changes.

In HA mode, recording, adding and dropping expected switches are leader
writes, behind the leader fence.

## The per-port M_Key turn-on, and turn-off

Naming `m_key_per_port` is the decision to turn per-port M_Keys on.

### Arming

At each fabric check that passes, while all of these hold:

- the group is named;
- the target is off;
- no failed turn-on is recorded;

the manager runs the arming's checks (specs/secured-profile.md, Turning it
on):

- the base stored by an earlier start;
- links recorded;
- every port on `m_key`.

It runs them from the check's last full verify, rather than a prober run of
its own. That verify must be complete, and taken since OpenSM last started.

- **Once they pass,** it records the target on.
- **In HA mode,** it does so only while the epoch allows exactly this
  instance's hash. An epoch that also allows a configuration without the
  group would let leaders with and without it alternate, re-keying the
  fabric each time.
- **While they fail,** it logs why, once per change of reason, naming the
  ports that block it.

### Applying

The manager restarts OpenSM, so that its start applies the target, at a
check where all of these hold:

- the check passes;
- no job is open;
- the target differs from what OpenSM runs with.

**Limits and retries:**

| Rule | Value |
| --- | --- |
| Restarts to apply the target | At most once an hour |
| Where the last attempt is kept | `state`, written only when a restart is made |
| A falling-back start's log | A warning, rising to an error at the second in a row |
| A turn-on that falls back | Recorded as a failed turn-on at the third in a row |
| A turn-off that falls back | Keeps retrying hourly |
| Applying whatever the jobs | Once a day has passed since the target changed |

The hourly cap is so that a start that keeps falling back isn't repeated
every check.

**A deferred job isn't open here.** It waits for another object, perhaps for
days, and isn't applied until it stops waiting, so a restart can't affect
it.

**A freeze.** A leader held by a freeze doesn't apply, since a freeze keeps
OpenSM where it is. It applies once the freeze ends.

**A start that falls back.** A start that falls back to the target OpenSM
ran on, because its verify failed, counts:

- its log rises from a warning to an error at the second in a row;
- the third in a row on a turn-on is recorded as a failed turn-on, with the
  target set back to off. OpenSM still runs with off, so nothing moves and
  the manager stops restarting.

A turn-off keeps retrying hourly: it's the safe direction, and the one a
removed group needs.

**A busy cloud** may have no quiet check. Once a day has passed since the
target changed, the manager applies at a passing check whatever the jobs,
and logs that it does.

**The part-way turn-on's retries** (specs/secured-profile.md, Turning it on)
come under the same hourly cap, with OpenSM left running on the target
between them. Their trigger is the target on and its success rule not yet
met, since the target then equals what OpenSM runs with.

**Jobs meanwhile** complete only on the per-port evidence they already
need: each port answering its key, or showing its link down, after the
sweep.

**How the restart happens:**

- **Single node:** the manager stops OpenSM, and the applier's next
  iteration starts it again, as after OpenSM stops.
- **HA mode:** the leader steps down and releases the lease. Any eligible
  node, itself included, takes it, and the takeover's start applies the
  target.

This covers:

- a turn-on armed automatically or by `--confirm-fabric`;
- a turn-off after the group is removed;
- a start whose verify failed and started OpenSM on the previous target.

### A failed turn-on

A failed turn-on is set back, as today, and recorded in `state` with:

- the base's fingerprint;
- the configuration hash it failed under. In single node, this is the same
  HMAC, keyed by a secret in the database, since a plain hash of a
  configuration with a key set by hand would let anyone who sees it test
  guesses of the key.

The hash is never shown.

While a failed turn-on is recorded, nothing arms again. Either of these
clears it:

- a change of configuration;
- `--confirm-fabric`.

`--port-key-states` shows, beside the ports, whether a failed turn-on is
recorded, what clears it, and the arming's blocking reason:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --port-key-states
```

`--confirm-fabric` arms in HA mode under the same epoch rule as the
leader's checks.

So a fabric that can't be turned on isn't re-keyed in a loop, across
restarts and takeovers alike. The record is logged at every start, with
what to check.

## What this changes in specs/secured-profile.md

This spec overrides, for per-port M_Keys:

- **The prober:** no switch is pending. Every row that answers as a switch
  is followed, and checks count the expected ones found.
- **`keys_to_try`:** a switch other than this host's own port, with the
  target on and anything possibly derived, gets the stored M_Key only if
  its row holds it.
- **Turning it on:** naming the group arms it, at a passing check, and the
  manager applies it. `--confirm-fabric` arms it at once, and no longer
  needs a second run for new switches.
- **HA mode:** the reach probe follows every switch, and counts the
  expected ones it finds.
