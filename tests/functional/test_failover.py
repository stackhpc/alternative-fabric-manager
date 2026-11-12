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

    def __init__(self, tmp_path, password, timings=None, env=None):
        self.tmp = tmp_path
        self.password = password
        self.timings = dict(TIMINGS, **(timings or {}))
        # The instances' environment, as for the fakes' settings
        self.env = dict(os.environ, **(env or {}))
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
                before = time.monotonic()
                found = self.active()
                after = time.monotonic()
                # The scan saw them at some time between: allowed if either
                # end of it was
                if (
                    len(found) > 1
                    and not self._allowed(before)
                    and not self._allowed(after)
                ):
                    self.violations.append((after, found))
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
        """Overlaps of two fakes' active runs outside an allowed window. The
        runs are known to within a heartbeat: one ends at its last active
        heartbeat, up to 0.1 s before the fake died or was disabled, and the
        first heartbeat follows its exec by a few ms. So this misses an
        overlap shorter than that, which only the monitor's samples might
        catch, but catches any longer one, however brief the monitor's
        view of it."""
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
            env=self.env,
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

    def health(self, i):
        """Instance i's /healthz status, or None if it doesn't answer"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{8190 + i}/healthz", timeout=5
            ) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError):
            return None

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
    timings = request.node.get_closest_marker("timings")
    env = request.node.get_closest_marker("env")
    cluster = Cluster(
        tmp_path,
        galera,
        timings.kwargs if timings else None,
        env.kwargs if env else None,
    )
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


def _operator(cluster, node=1):
    from alternative_fabric_manager import config as config_mod
    from alternative_fabric_manager.ha import lease as lease_mod

    config = config_mod.Config(
        token=TOKEN, connection=url(node, cluster.password), **cluster.timings
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
    # Both candidates' links down: neither may take over, even once T_reach
    # has passed, so a broken eligibility check can't pass by luck.
    leader = cluster.wait_leader()
    down, up = (i for i in NODES if i != leader)
    for i in (down, up):
        with open(os.path.join(cluster.fabric, f"linkdown-node-{i}"), "w") as f:
            f.write("down")
    cluster.signal(leader, signal.SIGKILL)
    end = time.monotonic() + (
        cluster.timings["lease_timeout"]
        + cluster.timings["reach_timeout"]
        + cluster.timings["watchdog_timeout"]
        + 5
    )
    while time.monotonic() < end:
        assert not cluster.fake_pids(down) and not cluster.fake_pids(up)
        time.sleep(0.5)
    os.unlink(os.path.join(cluster.fabric, f"linkdown-node-{up}"))
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
    end = time.monotonic() + cluster.timings["lease_timeout"] + 20
    while lease.read().term == term:
        assert time.monotonic() < end, "no takeover"
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


def cluster_size(cluster, i):
    result = query(i, cluster.password, "show status like 'wsrep_cluster_size'")
    if result.returncode != 0 or not result.stdout.split():
        return None
    return int(result.stdout.split()[-1])


def _isolate(cluster, i, on, wait=True):
    """Cut node i's Galera node off from the others, or join it again, and
    wait until the rest see it gone, or it is back"""
    result = query(
        i,
        cluster.password,
        f"SET GLOBAL wsrep_provider_options='gmcast.isolate={int(on)}'",
    )
    assert result.returncode == 0, result.stderr
    if wait:
        other = next(j for j in NODES if j != i)
        wait_for(
            lambda: cluster_size(cluster, other) == (2 if on else 3),
            60,
            f"Galera node {i} {'isolated' if on else 'back'}",
        )


def _unstoppable(cluster, i, on):
    path = os.path.join(cluster.fabric, f"unstoppable-node-{i}")
    if on:
        with open(path, "w") as f:
            f.write("fault")
    elif os.path.exists(path):
        os.unlink(path)


def test_an_opensm_that_cant_be_stopped_holds_every_job(cluster):
    # A leader whose self-stop and watchdog both fail (see Why the
    # unconfirmed wait is enough): its OpenSM ignores the ask, so after the
    # unconfirmed wait two OpenSMs act, and the fabric checks must hold
    # every job until the old one stops.
    leader = cluster.wait_leader()
    apply_change(cluster, leader, 1)
    watchdogs, fakes = cluster.watchdog_pids(leader), cluster.fake_pids(leader)
    assert watchdogs and fakes
    for pid in watchdogs:
        os.kill(pid, signal.SIGSTOP)
    _unstoppable(cluster, leader, True)
    with cluster.two_allowed():
        isolated = time.monotonic()
        _isolate(cluster, leader, True)
        # The majority's successor starts its OpenSM only after the lease
        # expires and the unconfirmed wait, with the old one still running.
        successor = wait_for(
            lambda: next(
                (i for i in NODES if i != leader and cluster.fake_pids(i)), None
            ),
            TAKEOVER + 30,
            "the successor's OpenSM",
        )
        # Its first heartbeat follows the process's exec by some ms.
        started = wait_for(
            lambda: cluster.first_active(f"node-{successor}", after=0),
            5,
            "the successor's first heartbeat",
        )
        timings = cluster.timings
        assert started - isolated >= (
            timings["lease_timeout"]
            + timings["watchdog_timeout"]
            - timings["ha_period"]
        )
        assert _paused_or_alive(fakes)
        job = _send(cluster, successor, "/cloudx/Network", network(2, 102))
        end = time.monotonic() + 10 * timings["check_interval"]
        while time.monotonic() < end:
            _not_completed(cluster, successor, job)
            time.sleep(0.5)
        # Held because the fabric check finds the other master
        _config, _lease, sessionmaker = _operator(cluster, successor)
        with sessionmaker() as session:
            assert session.get(db.Applied, 1).contested
        # The old node rejoins Galera, and its watchdog resumes, finds its
        # lease lapsed, and kills OpenSM.
        _isolate(cluster, leader, False)
        for pid in watchdogs:
            os.kill(pid, signal.SIGCONT)
        wait_for(lambda: not _paused_or_alive(fakes), 10, "the old OpenSM killed")
    _unstoppable(cluster, leader, False)
    cluster.job_completes(successor, job, TAKEOVER + 60)


@pytest.mark.env(AFM_FAKE_HONOURS_ASK="1")
def test_an_opensm_that_cant_be_stopped_honours_the_ask(cluster):
    # The same fault, with an OpenSM that honours SMInfo DISABLE: the
    # successor's ask makes it NOT-ACTIVE, so two never act at once. This
    # exercises the coordinator's ask, through the harness's runner; the
    # production driver doesn't send it yet (see specs/ha.md, Fencing).
    leader = cluster.wait_leader()
    watchdogs, fakes = cluster.watchdog_pids(leader), cluster.fake_pids(leader)
    for pid in watchdogs:
        os.kill(pid, signal.SIGSTOP)
    _unstoppable(cluster, leader, True)
    _isolate(cluster, leader, True)
    successor = cluster.wait_leader(other_than=leader)
    # Still running, but disabled: the monitors count it as not acting.
    assert _paused_or_alive(fakes)
    apply_change(cluster, successor, 1)
    _isolate(cluster, leader, False)
    for pid in watchdogs:
        os.kill(pid, signal.SIGCONT)
    # Its watchdog resumes, finds its lease lapsed, and kills it.
    wait_for(lambda: not _paused_or_alive(fakes), 10, "the old OpenSM killed")
    _unstoppable(cluster, leader, False)
    assert cluster.leader() == successor


def _taken_after(lease, term, timeout):
    """When the lease's term first exceeds a term, as the test sees it"""
    return wait_for(
        lambda: time.monotonic() if lease.read().term > term else None,
        timeout,
        f"a term after {term}",
    )


def _winner(lease, term):
    """The node that took the lease after a term, once it has"""
    state = lease.read()
    if state.term > term and state.holder_node:
        return int(state.holder_node.rsplit("-", 1)[1]), state.term
    return None


def test_a_winner_that_dies_mid_takeover_is_waited_for(cluster):
    # The winner takes the lease and is killed during its unconfirmed wait,
    # before it starts OpenSM: the next takeover waits for both targets.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGKILL)
    winner, winner_term = wait_for(
        lambda: _winner(lease, term), cluster.timings["lease_timeout"] + 20, "a winner"
    )
    cluster.signal(winner, signal.SIGKILL)
    assert not cluster.fake_pids(winner), "the winner started OpenSM already"
    third = next(i for i in NODES if i not in (leader, winner))
    taken = _taken_after(lease, winner_term, TAKEOVER + 30)
    assert cluster.wait_leader(other_than=leader) == third
    assert lease.read().term == winner_term + 1
    # The third node waited out the unconfirmed wait for both targets,
    # though neither had a live OpenSM, which nothing else here would show.
    started = cluster.first_active(f"node-{third}", after=0)
    assert started - taken >= cluster.timings["watchdog_timeout"] - 1
    assert cluster.first_active(f"node-{winner}", after=0) is None
    apply_change(cluster, third, 1)


def test_a_winner_paused_mid_takeover_doesnt_start_opensm_later(cluster):
    # As above, but the winner is paused, and resumes once the third node
    # leads: it has lost the lease, and must not start OpenSM.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGKILL)
    winner, winner_term = wait_for(
        lambda: _winner(lease, term), cluster.timings["lease_timeout"] + 20, "a winner"
    )
    cluster.signal(winner, signal.SIGSTOP)
    assert not cluster.fake_pids(winner), "the winner started OpenSM already"
    third = next(i for i in NODES if i not in (leader, winner))
    taken = _taken_after(lease, winner_term, TAKEOVER + 30)
    assert cluster.wait_leader(other_than=leader) == third
    assert lease.read().term == winner_term + 1
    # The third node waited out the unconfirmed wait for both targets,
    # though neither had a live OpenSM, which nothing else here would show.
    started = cluster.first_active(f"node-{third}", after=0)
    assert started - taken >= cluster.timings["watchdog_timeout"] - 1
    cluster.signal(winner, signal.SIGCONT)
    # Past its start timeout and a few periods: it never starts one.
    time.sleep(cluster.timings["start_timeout"] + 5)
    assert cluster.first_active(f"node-{winner}", after=0) is None
    assert cluster.leader() == third
    apply_change(cluster, winner, 1)


def test_opensm_exiting_moves_the_lease_at_once(cluster):
    # OpenSM exits under a leader that isn't frozen: it steps down and
    # releases, so a successor takes over without waiting for expiry.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    started = time.monotonic()
    for pid in cluster.fake_pids(leader):
        os.kill(pid, signal.SIGKILL)
    # A new term, at once: whoever wins the released lease, possibly the
    # same node again, with a new OpenSM
    new = wait_for(
        lambda: cluster.leader() if lease.read().term > term else None,
        TAKEOVER,
        "a new leadership",
    )
    assert time.monotonic() - started < cluster.timings["lease_timeout"]
    apply_change(cluster, new, 1)


def _exits_within(cluster, i, seconds):
    try:
        cluster.processes[i].wait(seconds)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"node-{i}'s manager didn't exit in {seconds} s") from None


def test_sigterm_with_the_database_unreachable_stops_opensm_in_bound(cluster):
    # SIGTERM while the leader's Galera node hangs: the step-down is bounded
    # by shutdown_timeout, OpenSM is stopped and verified, but the lease
    # isn't released, so the successor waits for expiry.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(
        cluster, next(i for i in NODES if i != leader)
    )
    term = lease.read().term
    docker("pause", f"afm-galera-{leader}")
    started = time.monotonic()
    cluster.signal(leader, signal.SIGTERM)
    _exits_within(cluster, leader, cluster.timings["shutdown_timeout"] + 5)
    assert not cluster.fake_pids(leader), "OpenSM outlived its manager"
    docker("unpause", f"afm-galera-{leader}")
    successor = cluster.wait_leader(other_than=leader)
    assert lease.read().term > term
    # Not released: the successor waited for the lease to expire.
    assert time.monotonic() - started >= cluster.timings["lease_timeout"] - 1
    apply_change(cluster, successor, 1)


def test_sigterm_to_a_candidate_marks_it_stopped(cluster):
    leader = cluster.wait_leader()
    candidate = next(i for i in NODES if i != leader)
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    cluster.signal(candidate, signal.SIGTERM)
    _exits_within(cluster, candidate, cluster.timings["shutdown_timeout"] + 5)
    rows = {name: stopped for name, _uuid, _hb, _reach, stopped in lease.nodes()}
    assert rows[f"node-{candidate}"]
    # The leader is undisturbed.
    assert cluster.leader() == leader and lease.read().term == term


def test_sigterm_during_a_takeover_releases_without_starting_opensm(cluster):
    # A winner told to stop during its unconfirmed wait steps down: it
    # releases, never starting OpenSM, and the third node still waits for
    # the dead leader's entry.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGKILL)
    winner, winner_term = wait_for(
        lambda: _winner(lease, term), cluster.timings["lease_timeout"] + 20, "a winner"
    )
    stopped = time.monotonic()
    cluster.signal(winner, signal.SIGTERM)
    _exits_within(cluster, winner, cluster.timings["shutdown_timeout"] + 5)
    third = next(i for i in NODES if i not in (leader, winner))
    assert cluster.wait_leader(other_than=leader) == third
    assert cluster.first_active(f"node-{winner}", after=0) is None
    assert lease.read().term == winner_term + 1
    # Released, so no wait for expiry, but the dead leader's entry still
    # took the unconfirmed wait.
    started = cluster.first_active(f"node-{third}", after=0)
    timings = cluster.timings
    assert started - stopped >= timings["watchdog_timeout"] - 1
    assert (
        started - stopped < timings["lease_timeout"] + timings["watchdog_timeout"] - 1
    )
    apply_change(cluster, third, 1)


@pytest.mark.timings(lease_timeout=14.0, self_stop_timeout=10.0)
def test_a_galera_blip_shorter_than_self_stop_keeps_opensm(cluster):
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    pids = cluster.sm_pids(leader)
    for i in NODES:
        docker("pause", f"afm-galera-{i}")
    time.sleep(2)
    for i in NODES:
        docker("unpause", f"afm-galera-{i}")
    time.sleep(cluster.timings["lease_timeout"])
    assert cluster.sm_pids(leader) == pids
    assert lease.read().term == term
    apply_change(cluster, leader, 1)


def test_two_galera_nodes_down_stops_every_opensm(cluster):
    # The survivor isn't a Primary component: nobody may lead.
    leader = cluster.wait_leader()
    survivor = leader
    down = [i for i in NODES if i != survivor]
    for i in down:
        docker("pause", f"afm-galera-{i}")
    wait_for(
        lambda: not cluster.active(),
        cluster.timings["lease_timeout"] + 10,
        "every OpenSM stopped",
    )
    # And none starts while it lasts.
    end = time.monotonic() + cluster.timings["lease_timeout"] + 5
    while time.monotonic() < end:
        assert not cluster.active()
        time.sleep(0.5)
    for i in down:
        docker("unpause", f"afm-galera-{i}")
    leader = cluster.wait_leader()
    apply_change(cluster, leader, 1)


# Losing any Galera node blocks every node's commits until the survivors
# evict it, after evs.suspect_timeout, 5 s, so T_self must outlast that (see
# specs/ha.md, Timeouts); the other tests' 4 s doesn't.
@pytest.mark.timings(lease_timeout=14.0, self_stop_timeout=10.0)
def test_a_candidate_cut_off_from_galera_disturbs_nothing(cluster):
    leader = cluster.wait_leader()
    candidate = next(i for i in NODES if i != leader)
    _config, lease, _sessionmaker = _operator(cluster, leader)
    term = lease.read().term
    pids = cluster.sm_pids(leader)
    started = time.monotonic()
    _isolate(cluster, candidate, True)
    # The eviction stalled every node's commits for longer than T_watchdog,
    # so the leader's OpenSM lived through it on the liveness thread.
    assert time.monotonic() - started > cluster.timings["watchdog_timeout"]
    # HAProxy would stop sending the cut-off candidate requests, but not the
    # leader, whose Galera node is in the majority (see Galera specifics).
    wait_for(lambda: cluster.health(candidate) == 503, 30, "the candidate unhealthy")
    wait_for(lambda: cluster.health(leader) == 200, 30, "the leader healthy")
    time.sleep(cluster.timings["lease_timeout"] + cluster.timings["watchdog_timeout"])
    assert cluster.sm_pids(leader) == pids and lease.read().term == term
    assert not cluster.fake_pids(candidate)
    _isolate(cluster, candidate, False)
    wait_for(lambda: cluster.health(candidate) == 200, 30, "the candidate healthy")
    time.sleep(cluster.timings["lease_timeout"])
    assert cluster.sm_pids(leader) == pids and lease.read().term == term
    apply_change(cluster, candidate, 1)


def test_a_killed_leader_rejoins_and_leads_again(cluster):
    # A killed manager leaves its watchdog's files behind; restarted, it
    # rejoins as a candidate without starting OpenSM, and leads again once
    # it is the only one. (The watchdog's refusal of stale files is unit
    # tested in test_watchdog.py: here nothing would start one.)
    leader = cluster.wait_leader()
    cluster.signal(leader, signal.SIGKILL)
    successor = cluster.wait_leader(other_than=leader)
    state = cluster.tmp / f"state-{leader}"
    assert any(state.iterdir()), "no watchdog files were left behind"
    cluster.start(leader)
    time.sleep(cluster.timings["lease_timeout"])
    assert not cluster.fake_pids(leader)
    assert cluster.leader() == successor
    for i in NODES:
        if i != leader:
            cluster.signal(i, signal.SIGKILL)
    assert cluster.wait_leader(other_than=successor) == leader
    apply_change(cluster, leader, 1)


def _fault(cluster, name, i, on=True):
    path = os.path.join(cluster.fabric, f"{name}-node-{i}")
    if on:
        with open(path, "w") as f:
            f.write("fault")
    elif os.path.exists(path):
        os.unlink(path)


def _switches(cluster, guids, node=None):
    """The switches every node, or one node, reaches"""
    name = f"switches-node-{node}" if node else "switches"
    path = os.path.join(cluster.fabric, name)
    with open(f"{path}.tmp", "w") as f:
        f.write(",".join(f"{g:x}" for g in guids))
    os.replace(f"{path}.tmp", path)


def _expect_switches(cluster, guids):
    _switches(cluster, guids)
    _config, _lease, sessionmaker = _operator(cluster)
    with sessionmaker() as session:
        for guid in guids:
            if session.get(db.ExpectedSwitch, f"{guid:016x}") is None:
                session.add(db.ExpectedSwitch(guid=f"{guid:016x}"))
        session.commit()


def test_a_wedged_opensm_hands_over_to_a_live_candidate(cluster):
    # OpenSM paused, its manager healthy: the SA doesn't answer, and after
    # three unanswered checks the leader hands over, without the cooldown.
    leader = cluster.wait_leader()
    _config, lease, _sessionmaker = _operator(cluster)
    term = lease.read().term
    wedged = cluster.fake_pids(leader)
    for pid in wedged:
        os.kill(pid, signal.SIGSTOP)
    wait_for(
        lambda: not _paused_or_alive(wedged),
        TAKEOVER + 30,
        "the wedged OpenSM stopped",
    )
    new = wait_for(
        lambda: cluster.leader() if lease.read().term > term else None,
        TAKEOVER + 30,
        "a new leadership",
    )
    apply_change(cluster, new, 1)


def test_a_hung_apply_loop_loses_the_lease(cluster):
    # A sweep that never returns: the liveness condition fails after the
    # apply loop's bound, renewals stop, the watchdog kills OpenSM, and a
    # successor takes over and completes the job.
    leader = cluster.wait_leader()
    _fault(cluster, "hang", leader)
    job = _send(cluster, leader, "/cloudx/Network", network(1, 101))
    successor = cluster.wait_leader(other_than=leader, timeout=TAKEOVER + 60)
    _fault(cluster, "hang", leader, on=False)
    cluster.job_completes(successor, job, TAKEOVER + 60)


@pytest.mark.timings(step_down_cooldown=0.0)
def test_a_dead_switch_holds_jobs_until_an_operator_removes_it(cluster):
    # Every node misses the switch, so nobody reaches more, leadership
    # stays, and no job completes until an operator removes it.
    leader = cluster.wait_leader()
    _expect_switches(cluster, [runner_mod.SWITCH, 2])
    apply_change(cluster, leader, 1)
    _config, lease, sessionmaker = _operator(cluster)
    term = lease.read().term
    # The candidates lose the switch first, and publish it, so that the
    # leader, whose checks then fail, never sees a stale reach of 2 from
    # one of them and hands over.
    for i in NODES:
        if i != leader:
            _switches(cluster, [runner_mod.SWITCH], node=i)
    wait_for(
        lambda: all(
            reach == 1
            for name, _uuid, _hb, reach, _stopped in lease.nodes()
            if name != f"node-{leader}"
        ),
        30,
        "the candidates' reach published",
    )
    _switches(cluster, [runner_mod.SWITCH], node=leader)
    job = _send(cluster, leader, "/cloudx/Network", network(2, 102))
    end = time.monotonic() + 10 * cluster.timings["check_interval"]
    while time.monotonic() < end:
        _not_completed(cluster, leader, job)
        time.sleep(0.5)
    assert cluster.leader() == leader and lease.read().term == term
    with sessionmaker() as session:
        session.execute(
            sa.delete(db.ExpectedSwitch).where(db.ExpectedSwitch.guid == f"{2:016x}")
        )
        session.commit()
    cluster.job_completes(leader, job, 60)
    assert lease.read().term == term


@pytest.mark.timings(step_down_cooldown=0.0)
def test_a_leader_on_a_fragment_hands_over_to_a_node_reaching_more(cluster):
    # A split fabric: the leader's port reaches one switch of two, the
    # others both. It hands over, and the successor completes jobs.
    leader = cluster.wait_leader()
    _expect_switches(cluster, [runner_mod.SWITCH, 2])
    apply_change(cluster, leader, 1)
    _switches(cluster, [runner_mod.SWITCH], node=leader)
    successor = cluster.wait_leader(other_than=leader, timeout=TAKEOVER + 60)
    apply_change(cluster, successor, 2)


def _freeze(cluster, node=1):
    from alternative_fabric_manager.ha import operator

    config, lease, sessionmaker = _operator(cluster, node)

    def frozen():
        try:
            return operator.freeze(config, lease, sessionmaker)
        except operator.OperatorError:
            # Until the leader's first fabric check passes
            return None

    wait_for(frozen, 30, "a freeze")
    return config, lease


def _unfreeze(cluster, config, lease):
    from alternative_fabric_manager.ha import operator

    assert operator.unfreeze(config, lease, 30)


def _holds(cluster, leader, lease, term, seconds, same_pids=True):
    """Through a time: the term stays, no other node runs OpenSM, and,
    if asked, the leader keeps the same OpenSM. A read may fail while Galera
    evicts or rejoins a node, but some must succeed."""
    pids = cluster.sm_pids(leader)
    end = time.monotonic() + seconds
    reads = 0
    while time.monotonic() < end:
        try:
            current = lease.read().term
        except Exception:  # noqa: BLE001 - a view change
            current = None
        if current is not None:
            reads += 1
            assert current == term
        for i in NODES:
            if i != leader:
                assert not cluster.fake_pids(i), f"node-{i} started OpenSM"
        if same_pids:
            assert cluster.sm_pids(leader) == pids
        time.sleep(0.5)
    assert reads >= 3, "too few reads of the lease succeeded"


def _past_expiry(cluster):
    return cluster.timings["lease_timeout"] + cluster.timings["watchdog_timeout"] + 2


def test_a_frozen_leaders_paused_manager_keeps_opensm(cluster):
    leader = cluster.wait_leader()
    config, lease = _freeze(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGSTOP)
    # Its watchdog's lease is held: OpenSM stays, and nobody takes over.
    _holds(cluster, leader, lease, term, _past_expiry(cluster))
    cluster.signal(leader, signal.SIGCONT)
    _unfreeze(cluster, config, lease)
    apply_change(cluster, leader, 1)


def test_a_frozen_leader_cut_off_from_galera_keeps_opensm(cluster):
    leader = cluster.wait_leader()
    # Through a Galera node that stays in the majority
    config, lease = _freeze(cluster, next(i for i in NODES if i != leader))
    term = lease.read().term
    _isolate(cluster, leader, True)
    # The majority sees no renewals, but the freeze is in Galera.
    _holds(cluster, leader, lease, term, _past_expiry(cluster))
    _isolate(cluster, leader, False)
    _holds(cluster, leader, lease, term, cluster.timings["lease_timeout"])
    _unfreeze(cluster, config, lease)
    apply_change(cluster, leader, 1)


def test_a_frozen_leaders_restarted_manager_restarts_opensm_there(cluster):
    # OpenSM dies with its manager; the restarted manager adopts the frozen
    # lease, in the same term, and starts OpenSM again on the same node.
    leader = cluster.wait_leader()
    config, lease = _freeze(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGKILL)
    cluster.processes[leader].wait()
    wait_for(lambda: not cluster.fake_pids(leader), 10, "OpenSM dead")
    _holds(cluster, leader, lease, term, 3, same_pids=False)
    cluster.start(leader)
    wait_for(lambda: cluster.leader() == leader, TAKEOVER, "OpenSM restarted")
    _holds(cluster, leader, lease, term, 3)
    _unfreeze(cluster, config, lease)
    apply_change(cluster, leader, 1)


def test_opensm_exiting_while_frozen_restarts_on_the_frozen_node(cluster):
    leader = cluster.wait_leader()
    config, lease = _freeze(cluster)
    term = lease.read().term
    old = cluster.fake_pids(leader)
    for pid in old:
        os.kill(pid, signal.SIGKILL)
    wait_for(
        lambda: cluster.leader() == leader and cluster.sm_pids(leader) != old,
        TAKEOVER,
        "OpenSM restarted on the frozen node",
    )
    _holds(cluster, leader, lease, term, 3)
    _unfreeze(cluster, config, lease)
    apply_change(cluster, leader, 1)


def test_sigterm_while_frozen_keeps_the_lease_for_the_node(cluster):
    # SIGTERM stops OpenSM but doesn't release the frozen lease: nobody
    # takes over, and the node's manager starts OpenSM again when it's back.
    leader = cluster.wait_leader()
    config, lease = _freeze(cluster)
    term = lease.read().term
    cluster.signal(leader, signal.SIGTERM)
    _exits_within(cluster, leader, cluster.timings["shutdown_timeout"] + 5)
    assert not cluster.fake_pids(leader)
    assert not lease.read().released
    _holds(cluster, leader, lease, term, _past_expiry(cluster), same_pids=False)
    cluster.start(leader)
    wait_for(lambda: cluster.leader() == leader, TAKEOVER, "OpenSM restarted")
    _unfreeze(cluster, config, lease)
    apply_change(cluster, leader, 1)


def test_a_late_request_from_a_paused_instance_is_refused(cluster):
    # An instance paused after the kernel accepted a port's create holds it;
    # networking-mlnx times out, resends it elsewhere, and deletes the port.
    # When the paused instance resumes, it must not bring the port back
    # (see specs/ha.md, Late requests).
    leader = cluster.wait_leader()
    paused, other = (i for i in NODES if i != leader)
    apply_change(cluster, leader, 1)
    _send(cluster, leader, "/cloudx/Network", dict(network(2, 102), revision_number=1))
    late = dict(port(2, 2), revision_number=5)
    cluster.signal(paused, signal.SIGSTOP)
    held = threading.Thread(
        target=lambda: _try(cluster, paused, "POST", "/cloudx/Port", late),
        daemon=True,
    )
    held.start()
    held.join(7)  # the client's timeout, as networking-mlnx's
    job = _send(cluster, other, "/cloudx/Port", late)
    cluster.job_completes(other, job, TAKEOVER + 60)
    status, job = cluster.request(
        other, "DELETE", f"/cloudx/Port/{PORT.format(2)}", dict(late, revision_number=6)
    )
    assert status == 202
    cluster.job_completes(other, job, TAKEOVER + 60)
    cluster.signal(paused, signal.SIGCONT)
    time.sleep(5)
    _config, _lease, sessionmaker = _operator(cluster, leader)
    with sessionmaker() as session:
        assert session.get(db.Port, PORT.format(2)) is None, "the late create ran"
        assert session.get(db.Tombstone, ("port", PORT.format(2))) is not None
    assert "a late request" in (cluster.tmp / f"instance-{paused}.log").read_text()


def _try(cluster, i, method, path, body):
    try:
        cluster.request(i, method, path, body)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
