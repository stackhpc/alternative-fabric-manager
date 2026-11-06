---
name: functional-tests
description: Run AFM's functional failover tests (three HA instances against a three-node Galera cluster in containers, with a fake OpenSM under the real watchdog) and diagnose failures from their logs. Use after changing anything under alternative_fabric_manager/ha/, manager.py's HA hooks, the driver interface, or tests/functional/.
---

# Functional failover tests

`tests/functional/test_failover.py` starts a three-node Galera cluster in
containers (`afm-galera-1..3`, SQL ports 3401-3403, Galera 4601-4603), then,
for each test, a fresh `afm` database and three AFM instances as processes
(API ports 8191-8193), each on its own Galera node. Each instance's OpenSM is
`tests/functional/fake_opensm.py`, run by the real watchdog. A monitor fails
the test if two fake SMs are ever master at once.

## Run

```sh
cd /opt/stack/alternative-fabric-manager
rm -rf "$CLAUDE_JOB_DIR/tmp/ft"   # or any scratch dir
AFM_FUNCTIONAL=1 timeout 2400 uv run pytest -q tests/functional/test_failover.py \
  --basetemp="$CLAUDE_JOB_DIR/tmp/ft"
```

- Needs Docker; `AFM_DOCKER` (default `sudo docker`) is the command. The
  image is `docker.io/library/mariadb:10.11`, which bundles Galera.
- The full suite takes about 3-4 minutes. Select scenarios with `-k`.
- Run in the background if it may outlast a tool call's timeout.
- Run it twice after a timing-sensitive change: one pass can hide a race.

## Diagnose

Each test's directory under `--basetemp` has `instance-<n>.log` (the
instance's log and its watchdog's lines), `fabric/` (the fake SMs'
registry), and `state-<n>/` (the watchdog's lease and requests). A merged
timeline usually explains a failure:

```sh
d=$(ls -d "$CLAUDE_JOB_DIR"/tmp/ft/<test-prefix>*/ | head -1)
grep -h -E "WARNING|ERROR|watchdog" "$d"/instance-*.log \
  | grep -v "Deadlock\|alembic\|Duplicate" | sort | cut -c1-200
```

- **"Two masters at once"** is a safety bug until proven otherwise: find
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
