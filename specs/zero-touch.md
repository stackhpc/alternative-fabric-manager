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
which may be tenants'. A non-zero `[opensm] key_lease_period` makes such a
loss recoverable by waiting, and docs/opensm-hardening.md says so. A
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
  the node GUID the prober's NodeInfo gives beside its GUID; one the new
  source can't match stays as it is, expected, and fails the check as
  missing, so that a switch already missing isn't dropped by the change.
- **A missing switch** fails the check, as today. With rows, it's dropped
  once the prober shows every recorded link to it down, from the switch
  ports and adapters at its other ends: the same evidence of a link down
  that a job's port gives (Lost ports and jobs). That can happen for a
  spine; a leaf whose adapters are reached only through it never shows it
  (What stays manual). A switch whose links are up but which doesn't
  answer stays, and holds jobs, since OpenSM can't program it.

Nothing is added, dropped or re-recorded while the check couldn't query
the fabric, the last check was contested, or OpenSM isn't master. In HA
mode these are leader writes, behind the leader fence.

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
`state`), so that a start that keeps falling back isn't repeated every
check. A busy cloud may have no quiet check: once a day has passed since
the target changed, it applies at a passing check whatever the jobs, and
logs that it does. The part-way turn-on's retries (Turning it on) come
under the same hourly cap, with OpenSM left running on the target between
them:

- **single node:** it stops OpenSM, and the applier's next iteration
  starts it again, as after OpenSM stops;
- **HA mode:** the leader steps down and releases the lease; any eligible
  node, itself included, takes it, and the takeover's start applies it.

That covers a turn-on armed automatically or by `--confirm-fabric`, a
turn-off after the group is removed, and a start whose verify failed and
started OpenSM on the previous target.

**A failed turn-on** is set back, as today, and recorded in `state`, with
the base's fingerprint and the configuration hash it failed under (in
single node, the same canonical hash, as a plain SHA-256). While it's
recorded, nothing arms again: a change of configuration, or
`--confirm-fabric`, clears it. `--port-key-states` shows it, and the
arming's blocking reason, beside the ports. So a fabric that can't be turned on isn't
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
