# Zero-touch operation

The operator should have nothing to do in normal running. A manual step
stays only where automating it would open a real hole; one that protects
an edge case gives way to usability (AGENTS.md, Usability). This spec
makes the expected switches, switch trust and the per-port M_Key turn-on
automatic, in single node and in HA mode, where the leader does what the
single-node manager does. `--confirm-fabric` stays, as a shortcut that
nothing needs.

## What stays manual, and why

- **Backing up before per-port M_Keys can begin** (specs/secured-profile.md,
  A new base key): a base lost with leases of 0 strands every port, so a
  start that stored it can't begin them, and the manager says so.
- **Naming and removing hardening groups**: the operator's decision, agreed
  with whoever shares the fabric.
- **What specs/ha.md keeps manual for safety**, such as `--force-unfreeze`
  and `--forget-node` for a node that died.

## Switch trust is dropped

Until now, a switch the prober found for the first time was pending,
neither expected nor trusted, until a second `--confirm-fabric` found it
again: a tenant's adapter answering NodeInfo as a switch would otherwise
be routed through by the prober, and sent the stored M_Key. That protects
too little to cost an operator step:

- **Spoofing a switch needs custom adapter firmware.** The adapter's
  firmware answers SMPs; a virtual function can't answer for itself at
  all, its physical function's firmware does. Only a bare metal tenant
  could try, with firmware the adapter accepts, which for recent ConnectX
  adapters means NVIDIA-signed.
- **OpenSM trusts NodeInfo anyway.** Its discovery and directed-route
  sweeps go through whatever answers as a switch, carrying the keys of the
  ports beyond it, whatever AFM trusts. The gate covered AFM's prober
  alone, which follows the links OpenSM's own cache lists.
- **What it kept from a spoofer is little.** Before the turn-on every port
  holds the shared M_Key, which a tenant can read from its own port: that
  is why per-port M_Keys exist. After it, the stored M_Key opens only ports
  not yet re-keyed, and a port's derived key is its own.

So every expected switch is trusted. `pending` stays in the schema, for
the previous version during a rolling upgrade, and is cleared: nothing
writes it true.

## Expected switches

The applier's fabric check (the leader's, in HA mode) keeps, in memory,
the switches each check saw. `[opensm] confirm_checks`, 12 by default (a
minute at the default `check_interval`), is how many checks in a row a
switch must be seen by before it's added.

- **None recorded yet**, as on a new deployment: once every switch of a
  non-empty set has been seen by `confirm_checks` checks in a row, the set
  is recorded, and logged. Jobs wait meanwhile: the check reports "No
  expected switches yet: recording them once the fabric has been stable
  for N checks".
- **A new switch**, seen by `confirm_checks` checks in a row, is added.
- **A missing switch** fails the check, as today, and is logged at every
  check. One missing for `[opensm] forget_switch_after`, a day by default,
  is dropped, with an alarm: a dead or replaced switch then clears itself.
  The missing time is kept in the switch's row (`missing_since`), so a
  restart or a takeover doesn't reset it. 0 never drops one.

Nothing is added or dropped while the last check was contested, or
OpenSM isn't master, or the check couldn't query the fabric: a switch seen
by another SM's sweep isn't recorded, and one that a failed query didn't
see isn't missing.

The completeness check guards against a leader on a fragment of a split
fabric. Dropping a switch a day after it went makes the check pass on a
fragment that lasts that long; in HA mode, the step-down rule for missing
switches moves the leadership to a node that reaches more long before.

`--confirm-fabric` replaces the list with what's seen now, at once, and
arms the per-port turn-on at once if its checks pass: for an operator who
doesn't want to wait.

## The per-port M_Key turn-on

Naming `m_key_per_port` is the decision to turn per-port M_Keys on. The
manager arms the turn-on itself: at a fabric check that passes, while the
group is named and the target is off, every `port_verify_interval` at most,
it runs the arming's checks (Turning it on: the base stored by an earlier
start, links recorded, every port on `m_key`), and once they pass, records
the target on and applies it:

- **single node:** it stops OpenSM, and the applier's next iteration
  starts it again, which begins the turn-on, as after OpenSM stops;
- **HA mode:** the leader steps down, releasing the lease, if a live
  candidate that reaches something exists, so that a takeover begins it;
  otherwise it waits, and logs why.

A turn-on whose start fails is set back, as today, and the manager doesn't
arm it again until it restarts, so that a fabric that can't be turned on
isn't re-keyed in a loop. The failure is logged, with what to check.

Removing the group turns per-port M_Keys off at the next start, as today;
the manager applies that the same way, once, at the first passing check.
