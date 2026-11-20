# Zero-touch operation

The operator should have nothing to do in normal running. A manual step
stays only where automating it would open a real hole; one that protects
an edge case gives way to usability (AGENTS.md, Usability). This spec
makes the expected switches, switch trust and the per-port M_Key turn-on
automatic, in single node and in HA mode, where the leader does what the
single-node manager does, behind the leader fence (specs/ha.md, The
secured profile). `--confirm-fabric` stays, as a shortcut, and for the one
case below that needs a person.

## What stays manual, and why

- **Removing a switch that may still forward.** A switch missing from
  `ibswitches` may be gone, or alive with its management agent hung, its
  links up and its pkey tables stale: dropping a live one would let jobs
  complete past it. Without per-port M_Key rows nothing tells the two
  apart, and with them only a switch whose every recorded link is down can
  be told gone, which a leaf switch never is: the adapters behind it are
  reached only through it. So a removed or replaced switch needs
  `--confirm-fabric`, and, with rows, a dead leaf's ports need
  `--release-port` too, or their jobs wait (Lost ports and jobs). That's
  a hardware change, made by someone at the fabric anyway.
- **Naming and removing hardening groups**: the operator's decision,
  agreed with whoever shares the fabric.
- **What specs/ha.md keeps manual for safety**, such as `--force-unfreeze`
  and `--forget-node` for a node that died.

Backing up the database before per-port M_Keys begin is no longer a step
the manager waits for. The old gate never checked a backup either: it
required a restart. The base is stored a release before any port can be
keyed with it, and every deployment's routine backups, and the deploy-host
skill's before each redeploy, hold it from then on; Galera's replication
isn't a backup, so HA deployments need the same backups. Losing the
database with no backup taken since is an operator failure, and an edge
case, but its cost is high with leases of 0: every port keeps a key no SM
holds, a power cycle of the whole fabric clears only volatile ones, and
adapters with a non-volatile M_Key need reconfiguring on their own hosts,
which may be tenants'. The default `[opensm] key_lease_period`, an hour,
is expected to make such a loss recoverable by waiting (not yet shown on
hardware); a deployment that sets it to 0
accepts that cost, and docs/opensm-hardening.md says so. A
start that stored the base still doesn't begin them (A new base key).

## Switch trust is dropped

Until now, a switch the prober found for the first time was pending,
neither expected nor trusted, until a second `--confirm-fabric` found it
again. Trust gated four things: which switches the prober routes through,
which rows count as infrastructure in `keys_to_try`, which switches a
check counts, and when a full verify is re-run. It protects too little to
cost an operator step:

- **Spoofing a switch needs custom adapter firmware.** The adapter's
  firmware answers SMPs; a virtual function can't answer for itself at
  all. Only a bare metal tenant could try, with firmware the adapter
  accepts, which for recent ConnectX adapters means NVIDIA-signed.
- **OpenSM trusts NodeInfo anyway.** Its discovery and directed-route
  sweeps go through whatever answers as a switch, carrying the derived
  keys of the ports beyond it, whatever AFM trusts. A spoofer on a route
  is shown those keys by OpenSM and the prober alike; that is the
  duplicate-GUID limit specs/secured-profile.md already accepts, since it
  needs OpenSM to have recorded the spoofer's links.
- **The stored M_Key goes only where OpenSM would send it.** `keys_to_try`
  changes so that a switch other than this host's own port, with the
  target on and anything possibly derived, is sent the stored M_Key only
  if its row holds it: OpenSM sends it only where `guid2mkey` names it.
  A row that's `unknown` gets its derived key alone, as a tenant's does.
  So treating every switch as infrastructure hands a spoofer no key
  OpenSM wouldn't.

So the prober follows every row that answers as a switch, as
`--confirm-fabric` already does (`follow_unconfirmed`). The expected
switches are only the completeness list, and what a check, or HA mode's
reach probe, counts is the expected switches found: a new switch, or a
spoofer, never makes a fragment look complete, nor a candidate reach
more. `pending` stays in the
schema, for the previous version during a rolling upgrade; the new code
ignores it, and writes it false.

## Expected switches

The applier's fabric check (the leader's, in HA mode) counts, in memory,
how many checks in a row have seen each switch: `[opensm] confirm_checks`,
12 by default (a minute at the default `check_interval`).

- **None recorded yet**, as on a new deployment: once every switch of a
  non-empty set has been seen by `confirm_checks` checks in a row, the set
  is recorded, and logged. Jobs wait meanwhile, the check saying "No
  expected switches yet: recording them once the fabric has been stable
  for N checks".
- **A new switch**, seen by `confirm_checks` checks in a row, is added.
- **The source changed.** Switches are known by `ibswitches`' node GUIDs
  without per-port M_Key rows, and by the prober's GUIDs with them, which
  can differ. The source the list was recorded from is kept in `state`.
  When the check's source differs, each recorded switch is translated by
  its node GUID, which each `expected_switches` row keeps beside the GUID
  it was recorded by (from the prober's NodeInfo, or `ibswitches`' own),
  so that the translation works both ways, as after `--forget-port-keys`;
  one the new source can't match stays as it is, expected, and fails the
  check as missing, so that a switch already missing isn't dropped by the
  change.
- **A missing switch** fails the check, as today, though grants that
  don't depend on it still complete (specs/ha.md, While a switch is
  missing). With rows, it's dropped once the prober shows every
  recorded link to it down, from the switch ports and adapters at its
  other ends: the same evidence of a link down
  that a job's port gives (Lost ports and jobs). That can happen for a
  spine; a leaf whose adapters are reached only through it never shows it
  (What stays manual). A switch whose links are up but which doesn't
  answer stays, and holds jobs, since OpenSM can't program it. The
  evidence is the check's own verify, never an older one, so a link that
  came back up since isn't missed.
- **A switch row that doesn't answer, behind a link that's up,** fails
  the check, expected or not, until it answers or an operator runs
  `--release-port` for it: it may forward with stale pkey tables, as a
  hung expected switch may. So a spine dropped while its links were down
  that comes back with its agent hung holds jobs again, rather than
  letting them complete past it. An adapter answering as a switch that
  then refuses its key holds the fabric the same way, as a lost
  non-Neutron port does (Lost ports and jobs), and the alarm names it.

Nothing is added, dropped or re-recorded while the check couldn't query
the fabric, which includes a verify that ran out of time (part of the
switches would look like all of them, and a switch it didn't reach like
one that's gone), the check was contested, or OpenSM isn't master; the
counts start again. Contested is the check's own finding (another master,
or an SM the SA records that may hold the keys), not the manager's
lasting flag, which only a passing check clears: a fabric with no
expected switches yet can't pass, so with the flag the list could never
be recorded, and a check that sees no other SM is evidence enough for a
list of switches, which no SM's sweep changes. A check that fails for
the scoped verify of a job's ports running out of time fails as a check
whose query failed. In HA mode these are leader writes, behind the
leader fence.

## The per-port M_Key turn-on, and turn-off

Naming `m_key_per_port` is the decision to turn per-port M_Keys on.

**Arming.** At each fabric check that passes, while the group is named,
the target is off, and no failed turn-on is recorded, the manager runs the
arming's checks (Turning it on: the base stored by an earlier start, links
recorded, every port on `m_key`), from the check's last full verify
rather than a prober run of its own; that verify must be complete, and
taken since OpenSM last started. Once they pass, it records the target
on. In HA mode, only while the epoch allows exactly this instance's hash:
an epoch that also allows a configuration without the group would let
leaders with and without it alternate, re-keying the fabric each time.
While they fail, it logs why, once per change of reason, naming the ports
that block it.

**Applying.** At a check that passes with no open job, while the target
differs from what OpenSM runs with, the manager restarts OpenSM so that
its start applies it, at most once an hour (the last attempt kept in
`state`, written only when a restart is made), so that a start that keeps
falling back isn't repeated every check. A deferred job isn't open here:
it waits for another object, perhaps for days, and isn't applied until it
stops waiting, so a restart can't affect it. A leader held by a freeze
doesn't apply, since a freeze keeps OpenSM where it is; it applies once
the freeze ends. A start that falls back to the target OpenSM ran on,
because its verify failed, counts: its log rises from a warning to an
error at the second in a row, and the third in a row on a turn-on is
recorded as a failed turn-on, with the target set back to off, which
OpenSM still runs with, so nothing moves and it stops restarting. A
turn-off keeps retrying hourly: it's the safe direction, and the one a
removed group needs. A busy cloud may have no quiet check: once a day has passed since
the target changed, it applies at a passing check whatever the jobs, and
logs that it does. The part-way turn-on's retries (Turning it on) come
under the same hourly cap, with OpenSM left running on the target between
them: their trigger is the target on and its success rule not yet met,
since the target then equals what OpenSM runs with. Jobs meanwhile
complete only on the per-port evidence they already need (each port
answering its key, or showing its link down, after the sweep):

- **single node:** it stops OpenSM, and the applier's next iteration
  starts it again, as after OpenSM stops;
- **HA mode:** the leader steps down and releases the lease; any eligible
  node, itself included, takes it, and the takeover's start applies it.

That covers a turn-on armed automatically or by `--confirm-fabric`, a
turn-off after the group is removed, and a start whose verify failed and
started OpenSM on the previous target.

**A failed turn-on** is set back, as today, and recorded in `state`, with
the base's fingerprint and the configuration hash it failed under (in
single node, the same HMAC, keyed by a secret in the database, since a
plain hash of a configuration with a key set by hand would let anyone who
sees it test guesses of the key). The hash is never shown. While it's
recorded, nothing arms again: a change of configuration, or
`--confirm-fabric`, clears it. `--port-key-states` shows whether one is
recorded, and what clears it, and the arming's blocking reason, beside
the ports. `--confirm-fabric` arms in HA mode under the same epoch rule as
the leader's checks. So a fabric that can't be turned on isn't
re-keyed in a loop, across restarts and takeovers alike. The record is
logged at every start, with what to check.

## What this changes in specs/secured-profile.md

This spec overrides, for per-port M_Keys:
- The prober: no switch is pending; every row that answers as a switch is
  followed, and checks count the expected ones found.
- `keys_to_try`: a switch other than this host's own port, with the target
  on and anything possibly derived, gets the stored M_Key only if its row
  holds it.
- Turning it on: naming the group arms it, at a passing check, and the
  manager applies it; `--confirm-fabric` arms it at once, and no longer
  needs a second run for new switches.
- HA mode: the reach probe follows every switch, and counts the expected
  ones it finds.
