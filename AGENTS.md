# AGENTS.md

Guidance for coding agents working on the alternative fabric manager (AFM),
a UFM-compatible SDN controller for networking-mlnx that drives OpenSM
partitions. Read README.md for what it does, and specs/ha.md before touching
anything under `alternative_fabric_manager/ha/`.

## Layout

| Path | What |
| --- | --- |
| `alternative_fabric_manager/api.py` | The UFM REST subset networking-mlnx uses (FastAPI) |
| `alternative_fabric_manager/manager.py` | Requests, jobs, rendering, sweeping, fabric checks, job completion |
| `alternative_fabric_manager/partitions.py` | Rendering, validating and attributing partitions.conf |
| `alternative_fabric_manager/effects.py` | What each job needs to hold in a swept file |
| `alternative_fabric_manager/drivers/` | Running OpenSM: `managed` (spawns its container), `docker`, `none` |
| `alternative_fabric_manager/db.py`, `migrations/` | SQLAlchemy models; Alembic for server databases |
| `alternative_fabric_manager/ha/` | HA: `lease.py` (Galera driver), `watchdog.py`, `coordinator.py`, `operator.py` |
| `specs/ha.md` | The accepted HA design: the source of truth for HA behaviour |
| `docs/opensm-hardening.md` | The hardened OpenSM defaults and their rationale |
| `docs/kayobe.md` | Deploying HA mode with Kayobe's `controller_containers`, and configuring InfiniBand hosts, Neutron, Nova and Ironic |
| `docs/infiniband-networks.md` | Creating InfiniBand networks and booting virtualised and bare metal instances on them |
| `tests/` | Unit tests; `tests/functional/` is the failover harness |

## Commands

```sh
uv sync                                   # dependencies
uv run ruff format . && uv run ruff check .
uv run pytest -q                          # unit tests, SQLite
AFM_TEST_DATABASE_URL=mysql+pymysql://root:<pw>@127.0.0.1:<port>/afm_test \
  uv run pytest -q                        # unit tests against MariaDB
AFM_FUNCTIONAL=1 uv run pytest tests/functional   # failover tests (Docker)
AFM_FUNCTIONAL=1 AFM_NEMESIS=1 uv run pytest -s \
  tests/functional/test_nemesis.py        # randomized faults (Docker)
```

Run ruff and the unit tests before every commit. Run the failover suite
and the nemesis after any change under `ha/`, to `manager.py`'s request
handling or HA hooks, to the driver interface, or to the schema, and quote
the nemesis seed it printed in the commit message or PR; see the
`functional-tests` skill. Run the unit tests against MariaDB too after a
schema change. CI runs the failover suite on pull requests that touch HA
code, the nemesis nightly and before every release (see
`.github/workflows/functional.yaml`), and the unit tests against MariaDB
on every pull request, but those don't replace running them first.

The functional tests' Galera containers (`afm-galera-1..3`, ports
3401-3403) are shared by every session on the host: before starting a
run, check that none is running (`docker ps`), and if another session is
working here, agree with it who runs when.

## The invariants

Every change must keep these; the reviews and tests are built around them.

- **A job completes only on evidence:** a sweep, started after a passing
  fabric check that followed any contested one, in which OpenSM had the
  job's effect (relative to its own object), followed by a passing check
  taken after the sweep, with OpenSM still running. Fail closed: an error
  that can't be traced to a port holds every job of the sweep.
- **A 400 is final for networking-mlnx.** Only answer 400 for requests that
  can never succeed. A request that may succeed later waits (`deferred`) or
  gets 503 (frozen). A DELETE always gets a job, never 404.
- **HA: never two OpenSMs acting at once.** A leader self-stops before any
  majority candidate can see its lease expire; a takeover waits for every
  other target to leave the set; the watchdog kills OpenSM when its lease
  lapses. Every leader write is a compare-and-set on the term.
- **The database is the source of truth**; the partitions file is
  re-rendered from it.

When changing HA behaviour, change specs/ha.md first, then the code, then
the tests, and have the change reviewed (see the `review-loop` skill).

## Conventions

- Python 3.10+, ruff (line length 88), FastAPI, SQLAlchemy 2, uv.
- Comments and docstrings say why, in plain sentences. Keep the spec's
  terms: term, target set, self-stop, held, unconfirmed takeover.
- Commits: 50/72 messages, `git commit --signoff`, authored as the user,
  with an `Assisted-By:` trailer naming the model. Commit and push to the
  working branch; never force-push.
- Don't put review transcripts or reviewers' comments in the repo: fold
  their substance into the spec or code.
- Schema changes: a new Alembic revision under `migrations/versions/`, and
  `_ADDED_COLUMNS` in db.py for SQLite's in-place upgrade. Only amend a
  revision that has never been deployed.

## Safety on a shared fabric

Development usually happens on a DevStack host set up as networking-mlnx's
DevStack guide describes, whose InfiniBand fabric others may share. AFM
there manages the fabric's OpenSM, so:

- **Never print OpenSM's keys** (M_Key, SM_Key, SA_Key). `--show-keys`
  prints them; use it only to feed a diagnostic tool, as in
  `KEY=$(... --show-keys | awk '/^m_key/{print $2}')`, never to display.
- **Agree the hardening with whoever shares the fabric** before changing
  `[opensm] hardening`. With M_Key protection on, SMP tools (`ibswitches`,
  `smpquery`, `ibportstate`) need `-y $KEY`. Never disable switch ports or
  send SMInfo Sets on a fabric others share.
- **AFM's database holds the fabric's keys.** Never drop or recreate it;
  back it up before every redeploy (see the `deploy-host` skill).
- Don't connect to eswitchd on tcp://127.0.0.1:60001 by hand; it exits on
  messages it can't decode.
- Changes to a shared DevStack's configuration need its owner's agreement.
- Delete OpenStack resources by explicit ID only.
