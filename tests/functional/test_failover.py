"""Failover tests: three instances against a three-node Galera cluster

Run with AFM_FUNCTIONAL=1 (they need Docker, as AFM_DOCKER, default
"sudo docker", and the docker.io/library/mariadb:10.11 image). Each test
starts three instances, each on its own Galera node, with fake_opensm.py
as OpenSM under the real watchdog, injects a fault, and checks, all the
while, that no two OpenSMs act as master at once.
"""

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
    """Three instances, a fabric directory, and a monitor of masters"""

    def __init__(self, tmp_path, password):
        self.tmp = tmp_path
        self.password = password
        self.fabric = str(tmp_path / "fabric")
        os.makedirs(self.fabric)
        self.processes = {}
        self.violations = []
        self.allow_two = False
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def _watch(self):
        while not self._stop.is_set():
            found = runner_mod.masters(self.fabric)
            if len(found) > 1 and not self.allow_two:
                self.violations.append((time.monotonic(), found))
            time.sleep(0.05)

    def start(self, i):
        settings = {
            "node": f"node-{i}",
            "token": TOKEN,
            "port": 8190 + i,
            "connection": url(i, self.password),
            "config_dir": str(self.tmp / f"config-{i}"),
            "state_dir": str(self.tmp / f"state-{i}"),
            "fabric": self.fabric,
            "timings": TIMINGS,
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
        # Fake OpenSMs that outlived their instances, if a test failed
        for entry in os.listdir(self.fabric):
            if entry.startswith("sm-"):
                try:
                    with open(os.path.join(self.fabric, entry)) as f:
                        os.kill(json.load(f)["pid"], signal.SIGKILL)
                except (OSError, ValueError, KeyError):
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
def cluster(galera, tmp_path):
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
    cluster = Cluster(tmp_path, galera)
    for i in NODES:
        cluster.start(i)
    yield cluster
    cluster.stop()
    for i in NODES:
        query(i, galera, "SET GLOBAL wsrep_provider_options='gmcast.isolate=0'")
        docker("unpause", f"afm-galera-{i}", check=False)
    if cluster.violations:
        print(cluster.logs())
    assert not cluster.violations, f"Two masters at once: {cluster.violations[:3]}"


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
        token=TOKEN, connection=url(1, cluster.password), **TIMINGS
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
    operator.confirm_takeover(lease, term)
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
