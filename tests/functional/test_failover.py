"""Failover tests: three instances against a three-node Galera cluster

Run with AFM_FUNCTIONAL=1 (they need Docker, as AFM_DOCKER, default
"sudo docker", and the docker.io/library/mariadb:10.11 image). Each test
starts three instances, each on its own Galera node, with fake_opensm.py
as OpenSM under the real watchdog, injects a fault, and checks, all the
while, that no two OpenSMs act as master at once.
"""

import contextlib
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db
from tests.functional import runner as runner_mod

pytestmark = pytest.mark.skipif(
    not os.environ.get("AFM_FUNCTIONAL"), reason="set AFM_FUNCTIONAL=1"
)

DOCKER = shlex.split(os.environ.get("AFM_DOCKER", "sudo docker"))
IMAGE = "docker.io/library/mariadb:10.11"
NODES = (1, 2, 3)
TOKEN = "dGVzdDp0ZXN0"
TIMINGS = {
    "lease_timeout": 8.0,
    "self_stop_timeout": 4.0,
    "watchdog_timeout": 4.0,
    "reach_timeout": 2.0,
    "ha_period": 0.5,
    "call_timeout": 1.0,
    "sweep_timeout": 5.0,
    "start_timeout": 10.0,
    "check_interval": 1.0,
    "job_timeout": 90.0,
    "shutdown_timeout": 10.0,
}
# A takeover's longest, with these timings: expiry, the unconfirmed wait,
# and a start, with margin
TAKEOVER = 8 + 4 + 10 + 10


def docker(*args, check=True):
    return subprocess.run([*DOCKER, *args], check=check, capture_output=True, text=True)


def sql_port(i):
    return 3400 + i


def query(i, password, statement):
    return docker(
        "exec",
        f"afm-galera-{i}",
        "mariadb",
        "-uroot",
        f"-p{password}",
        "-h127.0.0.1",
        f"-P{sql_port(i)}",
        "-N",
        "-e",
        statement,
        check=False,
    )


def wait_for(condition, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {what}")


@pytest.fixture(scope="module")
def galera():
    password = secrets.token_hex(12)
    for i in NODES:
        docker("rm", "-f", f"afm-galera-{i}", check=False)

    def start(i, *extra):
        docker(
            "run",
            "-d",
            "--name",
            f"afm-galera-{i}",
            "--network",
            "host",
            "-e",
            f"MARIADB_ROOT_PASSWORD={password}",
            IMAGE,
            f"--port={sql_port(i)}",
            "--bind-address=127.0.0.1",
            "--wsrep-on=ON",
            "--wsrep-provider=/usr/lib/galera/libgalera_smm.so",
            "--wsrep-cluster-name=afmtest",
            f"--wsrep-node-name=n{i}",
            f"--wsrep-node-address=127.0.0.1:{4600 + i}",
            "--wsrep-cluster-address=gcomm://127.0.0.1:4601,127.0.0.1:4602,127.0.0.1:4603",
            f"--wsrep-provider-options=gmcast.listen_addr=tcp://127.0.0.1:{4600 + i};"
            f"ist.recv_addr=127.0.0.1:{4610 + i}",
            "--wsrep-sst-method=mariabackup",
            f"--wsrep-sst-auth=root:{password}",
            f"--wsrep-sst-receive-address=127.0.0.1:{4620 + i}",
            "--binlog-format=ROW",
            "--innodb-autoinc-lock-mode=2",
            *extra,
        )

    def size(i):
        result = query(i, password, "show status like 'wsrep_cluster_size'")
        return result.returncode == 0 and result.stdout.split()[-1:] == ["3"]

    start(1, "--wsrep-new-cluster")
    wait_for(lambda: query(1, password, "select 1").returncode == 0, 120, "node 1")
    start(2)
    start(3)
    wait_for(lambda: size(1), 240, "a cluster of three")
    for i in NODES:
        wait_for(
            lambda i=i: query(i, password, "select 1").returncode == 0, 120, f"node {i}"
        )
    yield password
    for i in NODES:
        docker("rm", "-f", f"afm-galera-{i}", check=False)


def url(i, password, database="afm"):
    return f"mysql+pymysql://root:{password}@127.0.0.1:{sql_port(i)}/{database}"


class Cluster:
    """Three instances, a fabric directory, and a monitor of OpenSMs"""

    def __init__(self, tmp_path, password, timings=None):
        self.tmp = tmp_path
        self.password = password
        self.timings = dict(TIMINGS, **(timings or {}))
        self.fabric = str(tmp_path / "fabric")
        os.makedirs(self.fabric)
        self.processes = {}
        self.violations = []
        self.monitor_errors = []
        # (start, end) monotonic times in which a test lets two OpenSMs act,
        # as for the known limitation of a paused node; end None while open
        self.allowed = []
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def _allowed(self, start, end=None):
        end = start if end is None else end
        return any(a <= start and (b is None or end <= b) for a, b in self.allowed)

    @contextlib.contextmanager
    def two_allowed(self):
        window = [time.monotonic(), None]
        self.allowed.append(window)
        try:
            yield
        finally:
            window[1] = time.monotonic()
            self.allowed[self.allowed.index(window)] = tuple(window)

    def active(self):
        """Fake OpenSMs that can act now: alive, not paused, and not
        disabled, whether or not they have registered yet"""
        disabled = set()
        for name in os.listdir(self.fabric):
            if name.startswith("sm-") and name.endswith(".json"):
                try:
                    with open(os.path.join(self.fabric, name)) as f:
                        entry = json.load(f)
                except (OSError, ValueError):
                    continue
                if not entry.get("master"):
                    disabled.add(entry["pid"])
        return [
            pid
            for pid, state in runner_mod.processes("fake", self.fabric).items()
            if state not in ("T", "t") and pid not in disabled
        ]

    def _watch(self):
        while not self._stop.is_set():
            try:
                found = self.active()
                now = time.monotonic()
                if len(found) > 1 and not self._allowed(now):
                    self.violations.append((now, found))
            except Exception as e:  # noqa: BLE001 - reported at teardown
                self.monitor_errors.append(repr(e))
            time.sleep(0.05)

    def lifetimes(self):
        """Each fake's runs of active heartbeats, as (start, end, pid, node):
        a gap longer than a few heartbeats, as when paused, ends a run"""
        runs = []
        for name in os.listdir(self.fabric):
            if not (name.startswith("life-") and name.endswith(".log")):
                continue
            pid = int(name[len("life-") : -len(".log")])
            run = None
            with open(os.path.join(self.fabric, name)) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) != 3:
                        continue
                    t, node, active = float(parts[0]), parts[1], parts[2] == "1"
                    if run and (not active or t - run[1] > 0.35):
                        runs.append((run[0], run[1], pid, node))
                        run = None
                    if active:
                        run = [run[0] if run else t, t]
            if run:
                runs.append((run[0], run[1], pid, node))
        return sorted(runs)

    def check_lifetimes(self):
        """Overlaps of two fakes' active runs outside an allowed window: an
        exact check, where the monitor only samples"""
        runs = self.lifetimes()
        overlaps = []
        for i, (_a0, a1, apid, anode) in enumerate(runs):
            for b0, b1, bpid, bnode in runs[i + 1 :]:
                if b0 >= a1:
                    break
                if bpid != apid and not self._allowed(b0, min(a1, b1)):
                    overlaps.append((anode, apid, bnode, bpid, b0, min(a1, b1)))
        return overlaps

    def first_active(self, node, after):
        """When node's first fake that started after a time began"""
        starts = [r[0] for r in self.lifetimes() if r[3] == node and r[0] > after]
        return min(starts) if starts else None

    def fake_pids(self, i):
        """Node i's fake OpenSMs, paused ones included"""
        return list(runner_mod.processes("fake", self.fabric, f"node-{i}"))

    def watchdog_pids(self, i):
        return list(runner_mod.processes("watchdog", self.fabric, f"node-{i}"))

    def generations(self, i):
        """The generations node i's fakes swept, in order"""
        path = os.path.join(self.fabric, f"generations-node-{i}.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [int(line.split()[2]) for line in f if line.strip()]

    def start(self, i):
        settings = {
            "node": f"node-{i}",
            "token": TOKEN,
            "port": 8190 + i,
            "connection": url(i, self.password),
            "config_dir": str(self.tmp / f"config-{i}"),
            "state_dir": str(self.tmp / f"state-{i}"),
            "fabric": self.fabric,
            "timings": self.timings,
        }
        log = open(self.tmp / f"instance-{i}.log", "a")  # noqa: SIM115
        self.processes[i] = subprocess.Popen(
            [sys.executable, "-m", "tests.functional.instance", json.dumps(settings)],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )

    def leader(self):
        found = runner_mod.masters(self.fabric)
        if len(found) == 1:
            return int(found[0]["node"].rsplit("-", 1)[1])
        return None

    def wait_leader(self, timeout=TAKEOVER + 30, other_than=None):
        return wait_for(
            lambda: self.leader() if self.leader() not in (None, other_than) else None,
            timeout,
            "a leader",
        )

    def sm_pids(self, i):
        return [
            entry["pid"]
            for entry in runner_mod.masters(self.fabric)
            if entry["node"] == f"node-{i}"
        ]

    def signal(self, i, sig):
        self.processes[i].send_signal(sig)

    def stop(self):
        self._stop.set()
        for process in self.processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.kill()
                process.wait()
        # Fake OpenSMs and watchdogs that outlived their instances, if a
        # test failed, registered or not, paused or not
        for kind in ("watchdog", "fake"):
            for pid in runner_mod.processes(kind, self.fabric):
                try:
                    os.kill(pid, signal.SIGCONT)
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def logs(self):
        out = []
        for i in self.processes:
            path = self.tmp / f"instance-{i}.log"
            if path.exists():
                out.append(path.read_text()[-4000:])
        return "\n".join(out)

    def request(self, i, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{8190 + i}/ufmRestV3{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {TOKEN}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def job_completes(self, i, job_path, timeout):
        def done():
            status, body = self.request(i, "GET", "/" + job_path)
            if status == 200 and body["Status"] == "Failed":
                raise AssertionError(f"Job failed: {body}")
            return status == 200 and body["Status"] == "Completed"

        return wait_for(done, timeout, f"job {job_path}")


@pytest.fixture
def cluster(galera, tmp_path, request):
    root = sa.create_engine(url(1, galera, ""))
    with root.begin() as connection:
        connection.exec_driver_sql("DROP DATABASE IF EXISTS afm")
        connection.exec_driver_sql("CREATE DATABASE afm")
    root.dispose()
    db.upgrade(url(1, galera))
    sessionmaker = db.make_sessionmaker(url(1, galera))
    with sessionmaker() as session:
        session.add(db.ExpectedSwitch(guid=f"{runner_mod.SWITCH:016x}"))
        session.commit()
    marker = request.node.get_closest_marker("timings")
    cluster = Cluster(tmp_path, galera, marker.kwargs if marker else None)
    for i in NODES:
        cluster.start(i)
    yield cluster
    monitor_alive = cluster._monitor.is_alive()
    cluster.stop()
    for i in NODES:
        query(i, galera, "SET GLOBAL wsrep_provider_options='gmcast.isolate=0'")
        docker("unpause", f"afm-galera-{i}", check=False)
    # So that the next test starts with a Primary component of three
    for i in NODES:
        wait_for(
            lambda i=i: (
                query(
                    i, galera, "show status like 'wsrep_cluster_size'"
                ).stdout.split()[-1:]
                == ["3"]
            ),
            120,
            f"node {i} back in a cluster of three",
        )
    overlaps = cluster.check_lifetimes()
    if cluster.violations or overlaps:
        print(cluster.logs())
    assert monitor_alive and not cluster.monitor_errors, (
        f"The monitor failed: {cluster.monitor_errors[:3]}"
    )
    assert not cluster.violations, f"Two OpenSMs at once: {cluster.violations[:3]}"
    assert not overlaps, (
        f"Two OpenSMs' lifetimes overlap: {overlaps[:3]}, allowed: {cluster.allowed}"
    )


NETWORK = "c13bba05-eb07-45ba-ace2-765706b2d70{}"
PORT = "72c56c48-e9b8-4dcf-b3a7-0813bb3bd83{}"


def network(n, vlan):
    return {
        "id": NETWORK.format(n),
        "name": f"net{n}",
        "provider:network_type": "vlan",
        "provider:physical_network": "ibphysnet",
        "provider:segmentation_id": vlan,
    }


def port(n, network_n):
    return {
        "id": PORT.format(n),
        "network_id": NETWORK.format(network_n),
        "binding:host_id": "compute-0",
        "mac_address": f"12:34:56:78:21:b{n}",
        "extra_dhcp_opts": [
            {"opt_name": "client-id", "opt_value": f"20:12:34:56:00:00:78:21:b{n}"}
        ],
    }


def _send(cluster, via, path, body):
    """A request, retried on 503, as networking-mlnx does"""

    def sent():
        status, job = cluster.request(via, "POST", path, body)
        if status == 503:
            return None
        assert status == 202, status
        return job

    return wait_for(sent, 20, f"{path} accepted")


def apply_change(cluster, via, n, timeout=TAKEOVER + 60):
    """A network and a port, through instance via, completed"""
    job = _send(cluster, via, "/cloudx/Network", network(n, 100 + n))
    cluster.job_completes(via, job, timeout)
    job = _send(cluster, via, "/cloudx/Port", port(n, n))
    cluster.job_completes(via, job, timeout)


def test_one_leader_and_jobs_through_any_instance(cluster):
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    follower = next(i for i in NODES if i != leader)
    apply_change(cluster, follower, 1)
    apply_change(cluster, leader, 2)
    # Applying partitions, by SIGHUP through the watchdog, keeps the leader.
    assert cluster.leader() == leader
    assert lease.read().term == term


def test_a_killed_leader_is_replaced(cluster):
    leader = cluster.wait_leader()
    cluster.signal(leader, signal.SIGKILL)
    successor = cluster.wait_leader(other_than=leader)
    apply_change(cluster, successor, 2)


def test_a_stopped_leader_hands_over_at_once(cluster):
    leader = cluster.wait_leader()
    started = time.monotonic()
    cluster.signal(leader, signal.SIGTERM)
    cluster.wait_leader(other_than=leader)
    # Released: no wait for expiry
    assert time.monotonic() - started < TIMINGS["lease_timeout"]


def test_a_paused_manager_loses_opensm_before_a_successor_starts(cluster):
    leader = cluster.wait_leader()
    cluster.signal(leader, signal.SIGSTOP)
    successor = cluster.wait_leader(other_than=leader)
    # It wakes up, finds it lost the lease, and doesn't start OpenSM again.
    cluster.signal(leader, signal.SIGCONT)
    time.sleep(TIMINGS["lease_timeout"])
    assert cluster.leader() == successor
    apply_change(cluster, leader, 3)


def test_a_leader_in_a_minority_stops_and_the_majority_elects(cluster):
    leader = cluster.wait_leader()
    query(
        leader, cluster.password, "SET GLOBAL wsrep_provider_options='gmcast.isolate=1'"
    )
    successor = cluster.wait_leader(other_than=leader)
    query(
        leader, cluster.password, "SET GLOBAL wsrep_provider_options='gmcast.isolate=0'"
    )
    time.sleep(TIMINGS["lease_timeout"])
    assert cluster.leader() == successor
    apply_change(cluster, successor, 4)


def test_without_galera_the_leader_stops_and_comes_back(cluster):
    cluster.wait_leader()
    for i in NODES:
        docker("pause", f"afm-galera-{i}")
    wait_for(lambda: not runner_mod.masters(cluster.fabric), 30, "self-stop")
    for i in NODES:
        docker("unpause", f"afm-galera-{i}")
    leader = cluster.wait_leader()
    apply_change(cluster, leader, 5)


def _operator(cluster):
    from alternative_fabric_manager import config as config_mod
    from alternative_fabric_manager.ha import lease as lease_mod

    config = config_mod.Config(
        token=TOKEN, connection=url(1, cluster.password), **cluster.timings
    )
    sessionmaker = db.make_sessionmaker(config.connection, config.call_timeout)
    return config, lease_mod.GaleraLease(sessionmaker), sessionmaker


def test_a_frozen_leader_keeps_opensm_through_a_galera_outage(cluster):
    from alternative_fabric_manager.ha import operator

    leader = cluster.wait_leader()
    apply_change(cluster, leader, 6)
    config, lease, sessionmaker = _operator(cluster)
    assert operator.freeze(config, lease, sessionmaker) == f"node-{leader}"
    # New requests wait, with 503, which networking-mlnx retries.
    follower = next(i for i in NODES if i != leader)
    wait_for(
        lambda: (
            cluster.request(follower, "POST", "/cloudx/Network", network(7, 107))[0]
            == 503
        ),
        10,
        "503 while frozen",
    )
    for i in NODES:
        docker("pause", f"afm-galera-{i}")
    # Well past T_self, and past T plus the watchdog's lease: OpenSM stays.
    end = time.monotonic() + TIMINGS["lease_timeout"] + TIMINGS["watchdog_timeout"] + 4
    while time.monotonic() < end:
        assert cluster.leader() == leader
        time.sleep(0.5)
    for i in NODES:
        docker("unpause", f"afm-galera-{i}")
    # Still frozen and held, with Galera back: no takeover.
    time.sleep(TIMINGS["lease_timeout"] + 2)
    assert cluster.leader() == leader
    assert operator.unfreeze(config, lease, 30)
    apply_change(cluster, follower, 7)
    # A normal failover after it
    cluster.signal(leader, signal.SIGKILL)
    successor = cluster.wait_leader(other_than=leader)
    apply_change(cluster, successor, 8)


def test_an_unfreeze_the_leader_never_acknowledges_needs_an_operator(cluster):
    from alternative_fabric_manager.ha import operator

    leader = cluster.wait_leader()
    config, lease, sessionmaker = _operator(cluster)

    def frozen():
        try:
            return operator.freeze(config, lease, sessionmaker)
        except operator.OperatorError:
            # Until the leader's first fabric check passes
            return None

    wait_for(frozen, 30, "a freeze")
    term = lease.read().term
    # The frozen leader's manager pauses; an unfreeze can't be acknowledged.
    cluster.signal(leader, signal.SIGSTOP)
    assert not operator.unfreeze(config, lease, 5)
    time.sleep(TIMINGS["lease_timeout"] + TIMINGS["watchdog_timeout"] + 2)
    # Nobody took over: the held OpenSM may still run.
    assert lease.read().term == term
    assert cluster.leader() == leader
    # The operator stops it, and confirms.
    for pid in cluster.sm_pids(leader):
        os.kill(pid, signal.SIGKILL)
    assert operator.force_unfreeze(lease)[1] == term
    successor = cluster.wait_leader(other_than=leader)
    cluster.signal(leader, signal.SIGCONT)
    apply_change(cluster, successor, 9)


def test_a_candidate_whose_link_is_down_doesnt_take_over(cluster):
    leader = cluster.wait_leader()
    down, up = (i for i in NODES if i != leader)
    with open(os.path.join(cluster.fabric, f"linkdown-node-{down}"), "w") as f:
        f.write("down")
    cluster.signal(leader, signal.SIGKILL)
    assert cluster.wait_leader(other_than=leader) == up


def _not_completed(cluster, via, job):
    status, body = cluster.request(via, "GET", "/" + job)
    assert status == 200 and body["Status"] in ("Pending", "Running"), body


def _paused_or_alive(pids):
    return any(runner_mod.process_state(pid) not in (None, "Z", "X") for pid in pids)


@pytest.mark.timings(watchdog_timeout=12.0)
def test_the_unconfirmed_wait_outlasts_a_paused_managers_opensm(cluster):
    # T_watchdog above T, as in production (45 s over 30 s): a paused
    # manager's watchdog kills its OpenSM only after a successor sees the
    # lease expire, so only the unconfirmed wait keeps the two apart.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    old = cluster.fake_pids(leader)
    assert old
    cluster.signal(leader, signal.SIGSTOP)
    bumped = wait_for(
        lambda: time.monotonic() if lease.read().term > term else None,
        cluster.timings["lease_timeout"] + 20,
        "a takeover",
    )
    # The wait is exercised: the old OpenSM still runs when the lease moves.
    assert _paused_or_alive(old), "the watchdog killed OpenSM before the takeover"
    successor = cluster.wait_leader(other_than=leader)
    started = cluster.first_active(f"node-{successor}", after=0)
    assert started - bumped >= cluster.timings["watchdog_timeout"] - 1, (
        f"the successor's OpenSM started {started - bumped:.1f} s after the "
        "takeover, within the unconfirmed wait"
    )
    # The teardown checks that the two OpenSMs' lifetimes never overlapped.
    cluster.signal(leader, signal.SIGCONT)
    apply_change(cluster, successor, 1)


def test_a_paused_nodes_stale_opensm_completes_no_job(cluster):
    # The known limitation (see specs/ha.md, Fencing): a whole node paused,
    # manager, watchdog and OpenSM, then only its OpenSM resumed after a
    # takeover, acts as a second master until its watchdog resumes and
    # kills it. No job may complete meanwhile.
    leader = cluster.wait_leader()
    apply_change(cluster, leader, 1)
    watchdogs, fakes = cluster.watchdog_pids(leader), cluster.fake_pids(leader)
    assert watchdogs and fakes
    cluster.signal(leader, signal.SIGSTOP)
    for pid in (*watchdogs, *fakes):
        os.kill(pid, signal.SIGSTOP)
    successor = cluster.wait_leader(other_than=leader)
    _config, _lease, sessionmaker = _operator(cluster)
    with cluster.two_allowed():
        for pid in fakes:
            os.kill(pid, signal.SIGCONT)
        job = _send(cluster, successor, "/cloudx/Network", network(2, 102))
        # Several checks' and sweeps' worth, with both masters acting
        end = time.monotonic() + 10 * cluster.timings["check_interval"]
        while time.monotonic() < end:
            _not_completed(cluster, successor, job)
            time.sleep(0.5)
        with sessionmaker() as session:
            assert session.get(db.Applied, 1).contested
        # Its watchdog resumes, finds its lease lapsed, and kills it.
        for pid in watchdogs:
            os.kill(pid, signal.SIGCONT)
        wait_for(lambda: not _paused_or_alive(fakes), 10, "the stale OpenSM killed")
    cluster.signal(leader, signal.SIGCONT)
    # Only now, after a check that passes and a sweep after it
    cluster.job_completes(successor, job, TAKEOVER + 60)


def test_a_job_in_flight_completes_on_the_successors_sweep(cluster):
    leader = cluster.wait_leader()
    follower = next(i for i in NODES if i != leader)
    _config, lease, sessionmaker = _operator(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGSTOP)
    job = _send(cluster, follower, "/cloudx/Network", network(3, 103))
    with sessionmaker() as session:
        generation = session.scalar(sa.select(sa.func.max(db.Job.generation)))
    # No OpenSM can sweep it until the successor's starts.
    while lease.read().term == term:
        _not_completed(cluster, follower, job)
        time.sleep(0.5)
    successor = cluster.wait_leader(other_than=leader)
    cluster.job_completes(follower, job, TAKEOVER + 60)
    # On evidence: the successor's OpenSM swept a file with the job in it,
    # and the sweep that counted is of the current term.
    assert max(cluster.generations(successor), default=-1) >= generation
    assert not cluster.generations(leader) or (
        max(cluster.generations(leader)) < generation
    )
    with sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        assert applied.term == lease.read().term > term
        assert applied.generation >= generation
    cluster.signal(leader, signal.SIGCONT)
