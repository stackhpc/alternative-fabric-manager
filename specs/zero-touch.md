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

- **Removing a switch, without per-port M_Key rows.** A switch missing
  from `ibswitches` may be gone, or alive with its management agent hung,
  its links still up and its pkey tables stale: nothing without rows tells
  the two apart, and dropping a live one would let jobs complete past it.
  `--confirm-fabric` after a switch is removed or replaced. With rows, the
  links tell (Expected switches).
- **Naming and removing hardening groups**: the operator's decision,
  agreed with whoever shares the fabric.
- **What specs/ha.md keeps manual for safety**, such as `--force-unfreeze`
  and `--forget-node` for a node that died.

Backing up the database before per-port M_Keys begin is no longer a step
the manager waits for. The base is stored a release before any port can be
keyed with it, and every deployment's routine backups, and the deploy-host
skill's before each redeploy, hold it from then on. Losing the database
with no backup taken since is an operator failure, and, with leases of 0,
its cost is power-cycling the fabric's devices; that is an edge case.
A start that stored the base still doesn't begin them (A new base key).

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

So the prober follows, and a check counts, every row that answers as a
switch, as `--confirm-fabric` already does (`follow_unconfirmed`). The
expected switches are only the completeness list. `pending` stays in the
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
  can differ. The source the list was recorded from is kept in `state`;
  when the check's source differs, the list is recorded afresh, as when
  none is, rather than reporting every switch missing.
- **A missing switch** fails the check, as today. With rows, it's dropped
  once the prober shows every recorded link to it down, from the switch
  ports and adapters at its other ends: the same evidence of a link down
  that a job's port gives (Lost ports and jobs). A switch whose links are
  up but which doesn't answer stays, and holds jobs, since OpenSM can't
  program it. Without rows, it stays until `--confirm-fabric`.

Nothing is added, dropped or re-recorded while the check couldn't query
the fabric, the last check was contested, or OpenSM isn't master. In HA
mode these are leader writes, behind the leader fence.

## The per-port M_Key turn-on, and turn-off

Naming `m_key_per_port` is the decision to turn per-port M_Keys on.

**Arming.** At each fabric check that passes, while the group is named,
the target is off, and no failed turn-on is recorded, the manager runs the
arming's checks (Turning it on: the base stored by an earlier start, links
recorded, every port on `m_key`), from the check's last full verify
rather than a prober run of its own. Once they pass, it records the target
on. In HA mode, only while the epoch allows exactly this instance's hash:
an epoch that also allows a configuration without the group would let
leaders with and without it alternate, re-keying the fabric each time.
While they fail, it logs why, once per change of reason, naming the ports
that block it.

**Applying.** At a check that passes with no open job, while the target
differs from what OpenSM runs with, the manager restarts OpenSM so that
its start applies it, at most once an hour (the last attempt kept in
`state`), so that a start that keeps falling back isn't repeated every
check:

- **single node:** it stops OpenSM, and the applier's next iteration
  starts it again, as after OpenSM stops;
- **HA mode:** the leader steps down and releases the lease; any eligible
  node, itself included, takes it, and the takeover's start applies it.

That covers a turn-on armed automatically or by `--confirm-fabric`, a
turn-off after the group is removed, and a start whose verify failed and
started OpenSM on the previous target.

**A failed turn-on** is set back, as today, and recorded in `state`, with
the base's fingerprint and the configuration hash it failed under. While
it's recorded, nothing arms again: a change of configuration, or
`--confirm-fabric`, clears it. So a fabric that can't be turned on isn't
re-keyed in a loop, across restarts and takeovers alike. The record is
logged at every start, with what to check.
