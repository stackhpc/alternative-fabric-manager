---
name: functional-tests
description: Run AFM's functional failover tests (three HA instances against a three-node Galera cluster in containers, with a fake OpenSM under the real watchdog) and diagnose failures from their logs. Use after changing anything under alternative_fabric_manager/ha/, manager.py's HA hooks, the driver interface, or tests/functional/.
---

# Functional failover tests

`tests/functional/test_failover.py` starts a three-node Galera cluster in
containers (`afm-galera-1..3`, SQL ports 3401-3403, Galera 4601-4603), then,
for each test, a fresh `afm` database and three AFM instances as processes
(API ports 8191-8193), each on its own Galera node. Each instance's OpenSM is
`tests/functional/fake_opensm.py`, run by the real watchdog. Two checks fail
a test if two fake OpenSMs ever act at once, outside a window the test opens
with `cluster.two_allowed()`: a monitor that samples live, unpaused, not
disabled fakes from `/proc` every 50 ms, and, at teardown, an exact overlap
check of each fake's heartbeat log (`fabric/life-<pid>.log`). A test can
override the timings with `@pytest.mark.timings(...)`, for example a
`watchdog_timeout` above `lease_timeout`, as in production, to exercise the
unconfirmed wait.

## Run

```sh
cd /opt/stack/alternative-fabric-manager
rm -rf "${SCRATCH:-/tmp/afm}/ft"   # SCRATCH: any scratch directory
AFM_FUNCTIONAL=1 timeout 2400 uv run pytest -q tests/functional/test_failover.py \
  --basetemp="${SCRATCH:-/tmp/afm}/ft"
```

- Needs Docker; `AFM_DOCKER` (default `sudo docker`) is the command. The
  image is `docker.io/library/mariadb:10.11`, which bundles Galera.
- The full suite takes about 14 minutes. Select scenarios with `-k`.
- The nemesis, `tests/functional/test_nemesis.py`, needs `AFM_NEMESIS=1`
  too and takes about 6 minutes: 300 s of seeded random faults
  (`AFM_NEMESIS_SECONDS`), then a history check. It prints its
  `AFM_NEMESIS_SEED`; set that to replay a failing schedule, though not
  its exact timing. Run with `-s` to see its summary and any `NOTE:`
  lines, and read `history.json` in its test directory after a failure.
- Run in the background if it may outlast a tool call's timeout.
- Run it twice after a timing-sensitive change: one pass can hide a race.
- CI (`.github/workflows/functional.yaml`) runs the failover suite on pull
  requests that touch HA code, the nemesis nightly for 30 minutes, and the
  nemesis before every release. A failed run uploads the logs and
  `history.json` as an artifact, and its summary names the seed; the
  workflow can also be run by hand, with a seed to replay.
- The Galera containers are shared by every session on the host: check
  `docker ps` for `afm-galera-*` before starting, and take turns.

## Diagnose

Each test's directory under `--basetemp` has `instance-<n>.log` (the
instance's log and its watchdog's lines), `fabric/` (the fake SMs'
registry, their heartbeat logs, `life-<pid>.log`, and the partitions
generations each node's fakes swept, `generations-node-<n>.log`, all on the
system's monotonic clock), and `state-<n>/` (the watchdog's lease and requests). A merged
timeline usually explains a failure:

```sh
d=$(ls -d "${SCRATCH:-/tmp/afm}"/ft/<test-prefix>*/ | head -1)
grep -h -E "WARNING|ERROR|watchdog" "$d"/instance-*.log \
  | grep -v "Deadlock\|alembic\|Duplicate" | sort | cut -c1-200
```

- **"Two OpenSMs at once"** or **"lifetimes overlap"** is a safety bug until proven otherwise: find
  which instance started OpenSM, and what it knew about the lease then.
  Fix the code, and the spec if it allowed it.
- Galera deadlocks and duplicate-key errors at startup are expected: three
  instances race to create the first rows, and retry.
- "No renewal for … s: stopping OpenSM (self-stop)" right after a takeover
  means something blocked the lease thread (it shares the coordinator's
  lock).
- A test helper that sends requests must retry 503, as networking-mlnx does.

## Clean up

The fixture removes the Galera containers at the end of the module. After an
interrupted run, remove them by name: `sudo docker rm -f afm-galera-1
afm-galera-2 afm-galera-3`. Never touch other containers.
