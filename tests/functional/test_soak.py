"""A long run with no faults: a long-lived OpenSM is never killed

Run with AFM_FUNCTIONAL=1 AFM_SOAK=1 (and Docker, as for test_failover.py).
AFM_SOAK_SECONDS sets how long, 12 hours by default; set
AFM_FUNCTIONAL_OFFSET, such as 100, to run it beside the usual functional
tests on other ports and containers.

Three instances, with the production timings, serve a steady trickle of
networks and ports for the whole run. Every minute it samples each
instance's leader, term, OpenSM process, memory and file descriptors into
soak.json in its test directory, and at the end checks that one leader,
term and OpenSM process lasted throughout, that no job failed, and that
memory and file descriptors didn't grow: so that no deadline, bound or
counter that only a long run reaches kills OpenSM (see specs/ha.md,
Implementation plan). The cluster fixture also checks, as in every
failover test, that no two OpenSMs ever acted at once.
"""

import json
import os
import random
import threading
import time

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import db
from tests.functional import runner as runner_mod
from tests.functional.test_failover import NODES, TIMINGS, _operator
from tests.functional.test_nemesis import Client, History

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AFM_FUNCTIONAL") and os.environ.get("AFM_SOAK")),
    reason="set AFM_FUNCTIONAL=1 AFM_SOAK=1",
)

SECONDS = float(os.environ.get("AFM_SOAK_SECONDS", str(12 * 3600)))
SAMPLE = 60.0
# Memory and descriptors are compared with a sample taken once the cluster
# has settled, not with its first moments.
BASELINE = min(600.0, SECONDS / 4)
WORKERS = 2

# The production timings, with HA mode's job and start timeouts, as the
# Kayobe guide sets them: only these reach the deadlines and bounds a real
# deployment's long-lived OpenSM lives under.
_DEFAULTS = config_mod.Config(token="soak")
PRODUCTION = {name: getattr(_DEFAULTS, name) for name in TIMINGS}
PRODUCTION.update(job_timeout=300.0, start_timeout=60.0)


def _proc(pid):
    """A process's resident memory in kB and open descriptors, or None"""
    try:
        with open(f"/proc/{pid}/status") as f:
            rss = next(int(line.split()[1]) for line in f if line.startswith("VmRSS:"))
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, StopIteration, ValueError):
        return None
    return {"rss_kb": rss, "fds": fds}


def _sample(cluster, lease):
    try:
        state = lease.read()
        term, holder, counter = state.term, state.holder_node, state.counter
    except Exception as e:  # noqa: BLE001 - recorded, and checked below
        term, holder, counter = None, repr(e), None
    sample = {
        "t": time.monotonic(),
        "term": term,
        "holder": holder,
        "counter": counter,
        "masters": [entry["pid"] for entry in runner_mod.masters(cluster.fabric)],
        "nodes": {},
    }
    for i in NODES:
        process = cluster.processes[i]
        sample["nodes"][i] = {
            "manager": _proc(process.pid) if process.poll() is None else None,
            "watchdogs": {pid: _proc(pid) for pid in cluster.watchdog_pids(i)},
            "fakes": cluster.fake_pids(i),
        }
    return sample


@pytest.mark.timings(**PRODUCTION)
def test_a_long_lived_opensm_is_never_killed(cluster, tmp_path):
    leader = cluster.wait_leader(timeout=600)
    _config, lease, sessionmaker = _operator(cluster)
    term = lease.read().term
    opensm = cluster.sm_pids(leader)
    assert len(opensm) == 1
    rng = random.Random(0)
    history = History()
    stop = threading.Event()
    clients = [
        Client(cluster, history, w, random.Random(rng.random()), stop, interval=5.0)
        for w in range(WORKERS)
    ]
    errors = []

    def work(client):
        try:
            client.run()
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(f"worker {client.w}: {e!r}")

    threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in clients]
    for thread in threads:
        thread.start()
    samples = []
    started = time.monotonic()
    path = tmp_path / "soak.json"
    while time.monotonic() - started < SECONDS:
        samples.append(_sample(cluster, lease))
        # Written as it goes, so that an interrupted run still tells
        path.write_text(json.dumps(samples, indent=1))
        last = samples[-1]
        # Fail fast: a lost leader, term or OpenSM is the finding
        assert last["term"] == term, f"the term moved: {last}"
        assert last["masters"] == opensm, f"OpenSM changed or stopped: {last}"
        stop.wait(SAMPLE)
    stop.set()
    for thread in threads:
        thread.join(600)
    samples.append(_sample(cluster, lease))
    path.write_text(json.dumps(samples, indent=1))

    problems = list(errors)
    with sessionmaker() as session:
        failed = session.scalar(
            sa.select(sa.func.count())
            .select_from(db.Job)
            .where(db.Job.status == db.JobStatus.FAILED)
        )
    if failed:
        problems.append(f"{failed} jobs failed")
    completed = sum(c.completed for c in clients)
    if completed < SECONDS / 60:
        problems.append(f"only {completed} requests completed")
    # Growth, from the settled baseline to the end
    baseline = next(s for s in samples if s["t"] - started >= BASELINE)
    end = samples[-1]
    for i in NODES:
        before, after = baseline["nodes"][i]["manager"], end["nodes"][i]["manager"]
        if before is None or after is None:
            problems.append(f"node-{i}'s manager died")
            continue
        if after["rss_kb"] > before["rss_kb"] * 1.5 + 65536:
            problems.append(f"node-{i}'s memory grew: {before} to {after}")
        if after["fds"] > before["fds"] + 32:
            problems.append(f"node-{i}'s descriptors grew: {before} to {after}")
    print(
        f"{(end['t'] - started) / 3600:.1f} h, term {term}, node-{leader}'s "
        f"OpenSM {opensm[0]} throughout, {completed} requests completed, "
        f"lease counter {end['counter']}"
    )
    assert not problems, "; ".join(problems)
