"""A randomized nemesis against three instances, with a history checker

Run with AFM_FUNCTIONAL=1 AFM_NEMESIS=1 (and Docker, as for
test_failover.py). AFM_NEMESIS_SECONDS sets how long faults are injected
(default 300), and AFM_NEMESIS_SEED the schedule's seed (default random,
printed, and saved with the history), so that a failing run can be
replayed.

Client workers drive networks and ports through random instances, as
networking-mlnx does: one object's requests in order, each job polled until
it ends, a failed job resent, 503 and unreachable instances retried. A
nemesis meanwhile pauses, kills and stops managers, pauses whole nodes,
wedges OpenSM, hangs sweeps, cuts links, and isolates or pauses Galera
nodes, a few at a time, from a seeded schedule. Then everything heals, and
the recorded history is checked against the invariants in AGENTS.md:

- never two OpenSMs acting at once (the cluster fixture's monitor and
  heartbeat-overlap check, as in every failover test);
- no 400 to a valid request, no 404 to a DELETE, no 500;
- a job seen Completed stays Completed, and before it was seen, some
  OpenSM swept a partitions file of at least the job's generation;
- every job ends once the faults heal, the database matches what the
  client's completed requests say, and the leader's OpenSM sweeps the
  latest generation;
- the client made progress while the faults ran.
"""

import json
import os
import random
import signal
import subprocess
import threading
import time
import urllib.error
import uuid

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db
from tests.functional.test_failover import (
    NODES,
    TAKEOVER,
    _isolate,
    _operator,
    docker,
    wait_for,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AFM_FUNCTIONAL") and os.environ.get("AFM_NEMESIS")),
    reason="set AFM_FUNCTIONAL=1 AFM_NEMESIS=1",
)

SECONDS = float(os.environ.get("AFM_NEMESIS_SECONDS", "300"))
WORKERS = 3


class History:
    """Every client operation and nemesis event, on the monotonic clock
    that the fakes' logs use"""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []

    def add(self, **event):
        event.setdefault("t", time.monotonic())
        with self.lock:
            self.events.append(event)

    def of(self, kind):
        with self.lock:
            return [e for e in self.events if e["kind"] == kind]


class Client:
    """One worker: its own networks and ports, one request at a time, as
    networking-mlnx orders them per object"""

    def __init__(self, cluster, history, w, rng, stop):
        self.cluster = cluster
        self.history = history
        self.w = w
        self.rng = rng
        self.stop = stop
        # What the completed requests say exists: {network: {port, ...}}
        self.model = {}
        self.completed = 0

    def _instance(self):
        return self.rng.choice(NODES)

    def _send(self, method, path, body=None):
        """A request, retried on 503 and on an unreachable instance, through
        random instances; the job's path"""
        end = time.monotonic() + 600
        while time.monotonic() < end:
            i = self._instance()
            started = time.monotonic()
            try:
                status, job = self.cluster.request(i, method, path, body)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                self.history.add(
                    kind="send",
                    w=self.w,
                    method=method,
                    path=path,
                    via=i,
                    status=None,
                    error=repr(e),
                    started=started,
                )
                time.sleep(0.5)
                continue
            self.history.add(
                kind="send",
                w=self.w,
                method=method,
                path=path,
                via=i,
                status=status,
                job=job,
                started=started,
            )
            if status == 202:
                return job
            if status == 503:
                time.sleep(0.5)
                continue
            # Anything else breaks the contract; the checker reports it.
            return None
        raise AssertionError(f"{method} {path} never accepted")

    def _wait(self, job):
        """Poll a job until it ends, through random instances"""
        end = time.monotonic() + 900
        while time.monotonic() < end:
            i = self._instance()
            try:
                status, body = self.cluster.request(i, "GET", "/" + job)
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.5)
                continue
            if status == 200 and body["Status"] in ("Completed", "Failed"):
                self.history.add(
                    kind="done", w=self.w, job=job, status=body["Status"], via=i
                )
                return body["Status"]
            time.sleep(0.5)
        raise AssertionError(f"{job} never ended")

    def _do(self, method, path, body=None):
        """Send until a job completes, resending failed ones, as
        networking-mlnx's journal does"""
        for _attempt in range(20):
            job = self._send(method, path, body)
            if job is None:
                return False
            if self._wait(job) == "Completed":
                self.completed += 1
                return True
        raise AssertionError(f"{method} {path} failed 20 times")

    def run(self):
        cycle = 0
        while not self.stop.is_set():
            cycle += 1
            net = str(uuid.uuid4())
            vlan = 300 + self.w * 50 + cycle % 50
            ok = self._do(
                "POST",
                "/cloudx/Network",
                {
                    "id": net,
                    "name": f"nemesis-{self.w}-{cycle}",
                    "provider:network_type": "vlan",
                    "provider:physical_network": "ibphysnet",
                    "provider:segmentation_id": vlan,
                },
            )
            if not ok:
                return
            self.model[net] = set()
            if self.stop.is_set():
                return
            port = str(uuid.uuid4())
            a, b = self.w, cycle % 256
            ok = self._do(
                "POST",
                "/cloudx/Port",
                {
                    "id": port,
                    "network_id": net,
                    "binding:host_id": "compute-0",
                    "mac_address": f"12:34:57:{a:02x}:{b:02x}:01",
                    "extra_dhcp_opts": [
                        {
                            "opt_name": "client-id",
                            "opt_value": f"20:12:34:57:00:00:{a:02x}:{b:02x}:01",
                        }
                    ],
                },
            )
            if not ok:
                return
            self.model[net].add(port)
            if self.stop.is_set():
                return
            if not self._do("DELETE", f"/cloudx/Port/{port}"):
                return
            self.model[net].discard(port)
            if self.stop.is_set():
                return
            if not self._do("DELETE", f"/cloudx/Network/{net}"):
                return
            del self.model[net]


class Nemesis:
    """Faults from a seeded schedule, a few at a time, each healed after a
    random time; at most one Galera fault at once, so a majority remains"""

    def __init__(self, cluster, history, rng):
        self.cluster = cluster
        self.history = history
        self.rng = rng
        self.lock = threading.Lock()
        # node -> fault name, and the Galera fault if any
        self.busy = {}
        self.galera_busy = False
        self.heals = []

    # Each fault returns its heal.

    def pause_manager(self, i):
        self.cluster.signal(i, signal.SIGSTOP)
        return lambda: self.cluster.signal(i, signal.SIGCONT)

    def kill_manager(self, i):
        self.cluster.signal(i, signal.SIGKILL)
        return lambda: self._restart(i)

    def term_manager(self, i):
        self.cluster.signal(i, signal.SIGTERM)
        return lambda: self._restart(i)

    def _restart(self, i):
        try:
            self.cluster.processes[i].wait(30)
        except subprocess.TimeoutExpired:
            self.cluster.processes[i].kill()
            self.cluster.processes[i].wait()
        self.cluster.start(i)

    def pause_node(self, i):
        # Manager, watchdog and OpenSM, as a paused host or container
        pids = [
            *self.cluster.watchdog_pids(i),
            *self.cluster.fake_pids(i),
        ]
        self.cluster.signal(i, signal.SIGSTOP)
        for pid in pids:
            _kill(pid, signal.SIGSTOP)

        def heal():
            # The watchdog first: if its lease lapsed meanwhile, it kills
            # OpenSM before OpenSM can run again.
            for pid in self.cluster.watchdog_pids(i):
                _kill(pid, signal.SIGCONT)
            time.sleep(0.5)
            for pid in pids:
                _kill(pid, signal.SIGCONT)
            self.cluster.signal(i, signal.SIGCONT)

        return heal

    def wedge_opensm(self, i):
        pids = self.cluster.fake_pids(i)
        for pid in pids:
            _kill(pid, signal.SIGSTOP)

        def heal():
            for pid in pids:
                _kill(pid, signal.SIGCONT)

        return heal

    def _file_fault(self, name, i):
        path = os.path.join(self.cluster.fabric, f"{name}-node-{i}")
        with open(path, "w") as f:
            f.write("fault")
        return lambda: os.path.exists(path) and os.unlink(path)

    def hang_sweep(self, i):
        return self._file_fault("hang", i)

    def link_down(self, i):
        return self._file_fault("linkdown", i)

    def isolate_galera(self, i):
        _isolate(self.cluster, i, True)
        return lambda: _isolate(self.cluster, i, False)

    def pause_galera(self, i):
        docker("pause", f"afm-galera-{i}")
        return lambda: docker("unpause", f"afm-galera-{i}", check=False)

    NODE_FAULTS = (
        "pause_manager",
        "kill_manager",
        "term_manager",
        "pause_node",
        "wedge_opensm",
        "hang_sweep",
        "link_down",
    )
    GALERA_FAULTS = ("isolate_galera", "pause_galera")

    def _pick(self):
        with self.lock:
            if len(self.busy) + self.galera_busy >= 2:
                return None
            name = self.rng.choice(self.NODE_FAULTS + self.GALERA_FAULTS)
            galera = name in self.GALERA_FAULTS
            if galera and self.galera_busy:
                return None
            free = [i for i in NODES if i not in self.busy]
            if not free:
                return None
            i = self.rng.choice(free)
            # A node fault and a Galera fault on different nodes, so that
            # the node being faulted twice doesn't hide either
            self.busy[i] = name
            if galera:
                self.galera_busy = True
            return name, i, galera

    def _apply(self, name, i, galera, hold):
        self.history.add(kind="fault", fault=name, node=i, phase="start")
        try:
            heal = getattr(self, name)(i)
        except Exception as e:  # noqa: BLE001 - a fault that couldn't apply
            self.history.add(
                kind="fault", fault=name, node=i, phase="error", error=repr(e)
            )
            heal = None
        try:
            time.sleep(hold)
            if heal:
                heal()
        finally:
            self.history.add(kind="fault", fault=name, node=i, phase="heal")
            with self.lock:
                del self.busy[i]
                if galera:
                    self.galera_busy = False

    def run(self, until):
        while time.monotonic() < until:
            time.sleep(self.rng.uniform(1, 6))
            picked = self._pick()
            if picked is None:
                continue
            name, i, galera = picked
            hold = self.rng.uniform(2, 25)
            thread = threading.Thread(
                target=self._apply, args=(name, i, galera, hold), daemon=True
            )
            thread.start()
            self.heals.append(thread)
        for thread in self.heals:
            thread.join(120)


def _kill(pid, sig):
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _swept(cluster):
    """Every sweep the fakes recorded: (time, node, pid, generation)"""
    found = []
    for name in os.listdir(cluster.fabric):
        if name.startswith("generations-") and name.endswith(".log"):
            node = name[len("generations-") : -len(".log")]
            with open(os.path.join(cluster.fabric, name)) as f:
                for line in f:
                    t, pid, generation = line.split()
                    found.append((float(t), node, int(pid), int(generation)))
    return sorted(found)


def check(cluster, history, clients):
    """The invariants over the whole history; a list of problems"""
    problems = []
    # The contract: no 400 to a valid request, no 404 to a DELETE, no 500
    for e in history.of("send"):
        if e["status"] is not None and e["status"] not in (202, 503):
            problems.append(
                f"{e['method']} {e['path']} got {e['status']} via {e['via']}"
            )
    _config, _lease, sessionmaker = _operator(cluster)
    with sessionmaker() as session:
        jobs = {f"app/jobs/{job.id}": job for job in session.scalars(sa.select(db.Job))}
        networks = {n.id for n in session.scalars(sa.select(db.Network))}
        ports = {p.id: p.network_id for p in session.scalars(sa.select(db.Port))}
        state_generation = session.scalar(
            sa.select(db.State.value).where(db.State.name == "generation")
        )
    swept = _swept(cluster)
    # A job seen Completed stays so, and was swept before it was seen
    for e in history.of("done"):
        job = jobs.get(e["job"])
        if job is None:
            problems.append(f"{e['job']} isn't in the database")
            continue
        if e["status"] == "Completed":
            if job.status != db.JobStatus.COMPLETED:
                problems.append(f"{e['job']} was Completed, now {job.status.value}")
            evidence = [s for s in swept if s[0] <= e["t"] and s[3] >= job.generation]
            if not evidence:
                problems.append(
                    f"{e['job']} ({job.operation} {job.object_id}, generation "
                    f"{job.generation}) was Completed with no sweep of its "
                    "generation before"
                )
    # Every job has ended
    for path, job in jobs.items():
        if job.status not in (db.JobStatus.COMPLETED, db.JobStatus.FAILED):
            problems.append(f"{path} still {job.status.value}")
    # The database matches what the completed requests say
    expected_networks = set()
    expected_ports = {}
    for client in clients:
        for net, net_ports in client.model.items():
            expected_networks.add(net)
            for port in net_ports:
                expected_ports[port] = net
    if networks != expected_networks:
        problems.append(
            f"networks: {sorted(networks - expected_networks)} unexpected, "
            f"{sorted(expected_networks - networks)} missing"
        )
    if ports != expected_ports:
        problems.append(f"ports: {ports} where the client expects {expected_ports}")
    # The leader's OpenSM swept the latest generation
    if not any(s[3] >= (state_generation or 0) for s in swept):
        problems.append(f"no sweep of the latest generation, {state_generation}")
    # Progress while the faults ran
    if sum(c.completed for c in clients) < WORKERS:
        problems.append("the client hardly progressed")
    return problems


def test_nemesis(cluster, tmp_path):
    seed = int(os.environ.get("AFM_NEMESIS_SEED") or random.randrange(2**32))
    print(f"AFM_NEMESIS_SEED={seed}")
    rng = random.Random(seed)
    history = History()
    cluster.wait_leader()
    stop = threading.Event()
    clients = [
        Client(cluster, history, w, random.Random(rng.random()), stop)
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
    nemesis = Nemesis(cluster, history, random.Random(rng.random()))
    try:
        nemesis.run(time.monotonic() + SECONDS)
    finally:
        # Heal anything left: managers running, Galera whole, faults gone
        for name in os.listdir(cluster.fabric):
            if name.split("-")[0] in ("hang", "linkdown"):
                os.unlink(os.path.join(cluster.fabric, name))
        for i in NODES:
            docker("unpause", f"afm-galera-{i}", check=False)
            try:
                _isolate(cluster, i, False, wait=False)
            except AssertionError:
                pass  # its MariaDB not answering yet; the fixture retries
            for pid in (*cluster.watchdog_pids(i), *cluster.fake_pids(i)):
                _kill(pid, signal.SIGCONT)
            if cluster.processes[i].poll() is None:
                cluster.signal(i, signal.SIGCONT)
            else:
                cluster.start(i)
    history.add(kind="healed")
    cluster.wait_leader(timeout=TAKEOVER + 60)
    stop.set()
    for thread in threads:
        thread.join(900)
    # Every job ends, and the leader sweeps the latest generation
    _config, _lease, sessionmaker = _operator(cluster)

    def settled():
        with sessionmaker() as session:
            open_jobs = session.scalar(
                sa.select(sa.func.count())
                .select_from(db.Job)
                .where(db.Job.status.in_([db.JobStatus.PENDING, db.JobStatus.RUNNING]))
            )
        return open_jobs == 0

    wait_for(settled, 300, "every job to end")
    time.sleep(5)
    problems = errors + check(cluster, history, clients)
    with open(tmp_path / "history.json", "w") as f:
        json.dump(
            {
                "seed": seed,
                "events": history.events,
                "swept": _swept(cluster),
                "problems": problems,
            },
            f,
            indent=1,
            default=str,
        )
    faults = [e for e in history.of("fault") if e["phase"] == "start"]
    print(
        f"{len(faults)} faults, "
        f"{sum(c.completed for c in clients)} requests completed, "
        f"{sum(1 for e in history.of('done') if e['status'] == 'Failed')} jobs failed"
    )
    assert not problems, f"seed {seed}: " + "; ".join(problems[:10])
