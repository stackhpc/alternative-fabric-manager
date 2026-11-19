import copy
import datetime
import io
import subprocess
import tarfile
import threading
import types

import pytest

from alternative_fabric_manager.drivers import UNATTRIBUTED, ReloadError, SweepReport
from alternative_fabric_manager.drivers import docker as docker_driver
from tests import test_prober_script

BEFORE = "Nov 05 16:59:14 0x04 -> do_sweep:\n" + "*" * 10 + " SUBNET UP " + "*" * 10
# What OpenSM logs after SIGHUP when only the partitions file has changed
SWEEP = """\
Nov 05 16:59:16 190569 [275FE640] 0x04 -> handle_hup_signal:
********************* INITIATING PKEY SWEEP **********************
Nov 05 16:59:16 190584 [275FE640] 0x02 -> handle_hup_signal: Only \
partitions file changed, Running pkey sweep (pkey mgr only)
Nov 05 16:59:16 191669 [275FE640] 0x04 -> handle_hup_signal:
********************** PKEY SWEEP COMPLETE ***********************"""


class FakeContainer:
    def __init__(self, before="", after="", status="running"):
        self.status = status
        self.log = before
        self.after = after
        self.signals = []

    def kill(self, signal):
        self.signals.append(signal)
        self.log = "\n".join(filter(None, (self.log, self.after)))

    def logs(self, stdout, stderr, since=None):
        return self.log.encode()


class FakeClient:
    def __init__(self, container):
        self.containers = self
        self.container = container

    def get(self, name):
        assert name == "opensm"
        return self.container


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(docker_driver, "POLL_INTERVAL", 0)


def _reloader(container, timeout=1):
    return docker_driver.DockerReloader("opensm", timeout, client=FakeClient(container))


def test_waits_for_the_pkey_sweep():
    container = FakeContainer(after=SWEEP)
    _reloader(container).reload()
    assert container.signals == ["SIGHUP"]


def test_ignores_a_sweep_from_before_the_signal():
    container = FakeContainer(before=BEFORE)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_needs_the_signal_to_be_handled():
    # A sweep that was already running when the signal came doesn't count.
    container = FakeContainer(after=BEFORE)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_parse_error_fails():
    container = FakeContainer(
        after=SWEEP.replace(
            "Nov 05 16:59:16 191669",
            "Nov 05 16:59:16 191000 [275FE640] 0x01 -> PARSE ERROR line 3\n"
            "Nov 05 16:59:16 191669",
        )
    )
    with pytest.raises(ReloadError, match="couldn't read"):
        _reloader(container).reload()


def test_stopped_container_fails():
    container = FakeContainer(status="exited")
    with pytest.raises(ReloadError, match="exited"):
        _reloader(container).reload()
    assert container.signals == []


STARTED = """\
Nov 05 17:00:01 [275FE640] 0x02 -> SUBNET UP
*************************** SUBNET UP ****************************"""


class FakeManagedContainer(FakeContainer):
    """OpenSM's container, with its volume's /var/cache/opensm as Docker's
    archive API shows it: each file's bytes, mode, owner and time"""

    def __init__(self, log="", status="running", started="running", events=None):
        super().__init__(before=log, status=status)
        self.started = started
        self.removed = False
        self.events = [] if events is None else events
        self.cache = {}
        self.put_result = True
        self.execs = []
        self.exec_result = (0, b"")

    def reload(self):
        pass

    def start(self):
        self.events.append("start")
        self.status = self.started

    def remove(self, force, v=False):
        self.removed = "with volumes" if v else True
        self.events.append("remove")

    def get_archive(self, path):
        directory, _, name = path.rpartition("/")
        assert directory == "/var/cache/opensm"
        if name not in self.cache:
            raise docker_driver.docker.errors.NotFound(f"Could not find {path}")
        body, info = self.cache[name]
        # Docker names the file by its base name
        info = copy.copy(info)
        info.name = name
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tar:
            tar.addfile(info, io.BytesIO(body))
        # A stream of chunks, as docker-py gives it
        value = data.getvalue()
        return iter([value[:100], value[100:]]), {"name": name}

    def put_archive(self, path, data):
        assert path == "/var/cache/opensm"
        self.events.append("put_archive")
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                self.cache[member.name] = (tar.extractfile(member).read(), member)
        return self.put_result

    def exec_run(self, command, environment=None, stdout=True, stderr=True):
        self.execs.append((command, environment, stdout, stderr))
        result = self.exec_result
        return result(command, environment) if callable(result) else result


class FakeManagedClient:
    def __init__(self, existing=None, log=STARTED, status="running"):
        self.containers = self
        self.existing = existing
        self.log = log
        self.status = status
        self.created = []
        self.events = []
        # OpenSM's image is on the host, unless a test says otherwise
        self.images = self.api = FakeImages(present=True)

    def get(self, name):
        if self.existing is None:
            raise docker_driver.docker.errors.NotFound("no container")
        return self.existing

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.events.append("create")
        self.existing = FakeManagedContainer(
            self.log, status="created", started=self.status, events=self.events
        )
        return self.existing

    def run(self, **kwargs):
        raise AssertionError("containers.run() would pull a missing image")


@pytest.fixture
def managed_config(config):
    config.driver = "managed"
    config.start_timeout = 0.05
    config.opensm_conf = {"sm_priority": "15"}
    return config


def test_managed_start(managed_config):
    old = FakeManagedContainer()
    client = FakeManagedClient(existing=old)
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    driver.start()

    # With its anonymous volumes, or a start retried at every check leaves
    # one behind each time.
    assert old.removed == "with volumes"
    (kwargs,) = client.created
    assert "detach" not in kwargs
    assert client.events == ["create", "start"]
    assert kwargs["pid_mode"] == "container:alternative-fabric-manager"
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert kwargs["network_mode"] == "host"
    assert kwargs["command"][:2] == ["-F", "/etc/opensm/opensm.conf"]
    assert kwargs["volumes"][0] == "/etc/opensm:/etc/opensm:ro"
    assert driver.running()


def test_managed_start_without_manager_container(managed_config):
    managed_config.manager_container = ""
    client = FakeManagedClient()
    docker_driver.ManagedDocker(managed_config, client=client).start()
    assert "pid_mode" not in client.created[0]


def test_managed_start_fails_if_opensm_exits(managed_config):
    client = FakeManagedClient(log="opensm: cannot bind", status="exited")
    with pytest.raises(ReloadError, match="exited"):
        docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_start_times_out(managed_config):
    client = FakeManagedClient(log="still discovering")
    with pytest.raises(ReloadError, match="didn't start"):
        docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_start_as_standby_fails(managed_config):
    # Another subnet manager is master: this one must not count as applying
    # anything.
    client = FakeManagedClient(log="0x02 -> Entering STANDBY state")
    with pytest.raises(ReloadError):
        docker_driver.ManagedDocker(managed_config, client=client).start()


def test_managed_running(managed_config):
    client = FakeManagedClient()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    assert not driver.running()
    client.existing = FakeManagedContainer(status="exited")
    assert not driver.running()


def test_managed_stop(managed_config):
    container = FakeManagedContainer()
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    driver.stop()
    assert container.removed


def test_a_sweep_must_start_after_the_signal():
    # A heavy sweep already under way may finish after OpenSM logs the signal,
    # without having read the new file.
    already = (
        "Nov 05 16:59:16 190569 [275FE640] 0x04 -> handle_hup_signal:\n"
        "*************************** SUBNET UP ****************************"
    )
    container = FakeContainer(after=already)
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(container, timeout=0.05).reload()


def test_failed_start_removes_the_container(managed_config):
    client = FakeManagedClient(log="0x01 -> PARSE ERROR line 3")
    with pytest.raises(ReloadError, match="couldn't read"):
        docker_driver.ManagedDocker(managed_config, client=client).start()
    assert client.existing.removed


# What OpenSM 5.17 logged when SIGHUP found an unchanged file: a heavy sweep,
# which reads the partitions file after "HEAVY SWEEP COMPLETE"
HEAVY = """\
Nov 05 18:10:00 237426 [5A1FE640] 0x04 -> handle_hup_signal: No configuration \
changes, heavy sweep suggestion: TRUE
********************* INITIATING HEAVY SWEEP *********************
********************** HEAVY SWEEP COMPLETE **********************
Nov 05 18:10:00 240189 [5A1FE640] 0x01 -> parse_part_conf: PARSE ERROR: line 5: \
no partition definition found
PARSE ERROR: line 5: no partition definition found
Nov 05 18:10:00 242710 [5A1FE640] 0x02 -> SUBNET UP
*************************** SUBNET UP ****************************"""


def test_heavy_sweep_parse_error_fails():
    with pytest.raises(ReloadError, match="couldn't read"):
        _reloader(FakeContainer(after=HEAVY)).reload()


def test_heavy_sweep_ends_with_subnet_up():
    # Not with HEAVY SWEEP COMPLETE, which comes before the partitions file
    # is read
    partial = HEAVY.split("Nov 05 18:10:00 240189")[0]
    with pytest.raises(ReloadError, match="didn't finish"):
        _reloader(FakeContainer(after=partial), timeout=0.05).reload()
    clean = "\n".join(line for line in HEAVY.splitlines() if "PARSE" not in line)
    _reloader(FakeContainer(after=clean)).reload()


# OpenSM 5.17's own messages, from its binary
PKEY_BY_DESCRIPTION = (
    "pkey_mgr_update_port: ERR 0505: Failed to set PKey 0x8064 in block 0 idx 2 "
    "for compute-0 mlx5_0"
)
PKEY_BY_NODE = (
    "pkey_mgr_update_port: ERR 0509: pkey_mgr_update_pkey_entry() failed to update "
    "pkey table block 0 for node 0x0002c90300001234 port 1 (compute-0 mlx5_0)"
)
PKEY_BY_PORT = (
    "osm_vm: ERR 5703: Failure initiating PKeyTable set request to VPort "
    "0x0002c90300005678, VPort index 1 of port 0x0002c90300001235 (compute-0)"
)
ENFORCEMENT = (
    "pkey_mgr_update_peer_port: ERR 0507: Not enough pkey entries (32 <= 33) on "
    "switch 0xec0d9a0300b12560 port 7 (SwitchIB). Clearing Enforcement bit"
)


def test_a_pkey_error_without_a_port_guid_fails_closed():
    for line in (PKEY_BY_DESCRIPTION, PKEY_BY_NODE):
        report = SweepReport()
        docker_driver._note_pkey_error(report, line)
        # A node GUID isn't the port's, so it mustn't count as one.
        assert report.pkey_errors == set()
        assert report.unattributed


def test_a_pkey_error_with_a_port_guid_names_it():
    report = SweepReport()
    docker_driver._note_pkey_error(report, PKEY_BY_PORT)
    assert 0x0002C90300001235 in report.pkey_errors
    # The virtual port's own GUID, which is its Neutron port's
    assert 0x0002C90300005678 in report.pkey_errors
    assert not report.unattributed


def test_cleared_enforcement_names_the_switch_port():
    report = SweepReport()
    docker_driver._note_pkey_error(report, ENFORCEMENT)
    assert report.enforcement_errors == {"ec0d9a0300b12560:7"}
    report = SweepReport()
    docker_driver._note_pkey_error(report, "x: ERR 0513: something else")
    assert report.enforcement_errors == {UNATTRIBUTED}


def test_pkey_errors_are_not_partitions_errors():
    for line in (PKEY_BY_DESCRIPTION, PKEY_BY_PORT, ENFORCEMENT):
        assert not docker_driver._is_partitions_error(line.replace("osm_vm", "prtn"))
    assert docker_driver._is_partitions_error("osm_prtn_make_new: ERR 0A01: x")


IBSWITCHES = (
    'Switch\t: 0xec0d9a0300b12560 ports 41 "SwitchIB Mellanox Technologies" '
    "base port 0 lid 2 lmc 0\n"
)
SMIR = """SMInfoRecord dump:
\t\tRID
\t\tLID.....................1
\t\tSMInfo dump:
\t\tGUID....................0x040973ffffc75168
\t\tSM_Key..................0x0000000000000000
\t\tActCount................1234
\t\tPriority................15
\t\tSMState.................3
SMInfoRecord dump:
\t\tRID
\t\tLID.....................9
\t\tSMInfo dump:
\t\tGUID....................0x0002c90300001235
\t\tSM_Key..................0x0000000000000000
\t\tActCount................5
\t\tPriority................0
\t\tSMState.................2
"""
IBSTAT_P = (
    "0x040973ffffc75168\n0xffffffffffffffff\n0xffffffffffffffff\n0x0000000000000000\n"
)


class FakeExecContainer:
    status = "running"

    def __init__(self):
        self.commands = []

    def exec_run(self, command):
        self.commands.append(command)
        outputs = {"ibswitches": IBSWITCHES, "saquery": SMIR, "ibstat": IBSTAT_P}
        return 0, outputs[command[0]].encode()


def test_fabric_parses_the_tools_output():
    container = FakeExecContainer()
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    driver = docker_driver.DockerReloader("opensm", 30, client=client)
    fabric = driver.fabric(m_key="0x11", sa_key="0x22")
    assert fabric.switches == {0xEC0D9A0300B12560}
    assert fabric.sms == [(0x040973FFFFC75168, 3), (0x0002C90300001235, 2)]
    # Placeholder GUIDs of unassigned virtual functions aren't ours.
    assert fabric.own_guids == {0x040973FFFFC75168}
    assert ["ibswitches", "-y", "0x11"] in container.commands
    assert ["saquery", "--smkey", "0x22", "SMIR"] in container.commands


MAD_PKEY_FAILURES = (
    "sm_mad_ctrl_send_err_cb: ERR 3113: MAD completed in error (IB_TIMEOUT): "
    "SubnSet(P_KeyTable), attr_mod 0x0, TID 0x1234, dest_guid 0x0002c90300001234",
    "sm_mad_ctrl_rcv_callback: ERR 3119: Set method failed for attribute 0x16 "
    "(PKeyTable)",
)


def test_mad_failures_setting_pkey_tables_fail_closed():
    for line in MAD_PKEY_FAILURES:
        report = SweepReport()
        docker_driver._note_pkey_error(report, line)
        assert report.unattributed, line
    # Other attributes' failures aren't pkey errors.
    report = SweepReport()
    docker_driver._note_pkey_error(
        report,
        "x: ERR 3113: MAD completed in error (IB_TIMEOUT): SubnGet(NodeInfo), "
        "attr_mod 0x0, TID 0x1, dest_guid 0x1",
    )
    assert not report.unattributed


def test_managed_start_reports_its_first_sweep(managed_config):
    client = FakeManagedClient(log=MAD_PKEY_FAILURES[1] + "\n" + STARTED)
    report = docker_driver.ManagedDocker(managed_config, client=client).start()
    assert report is not None and report.unattributed


@pytest.mark.parametrize(
    "line",
    [
        "pkey_mgr_process_partition_table: ERR 0503: Failed to obtain P_Key "
        "0x8064 block and index for compute-0 mlx5_0",
        "pkey_mgr_update_port: ERR 0511: Failed to set PortInfo for node "
        "0xec0d9a0300b12560 port 7 (SwitchIB)",
        "pkey_mgr_update_port: ERR 0514: Failed to allocate backup blocks",
    ],
)
def test_more_pkey_manager_errors_fail_closed(line):
    report = SweepReport()
    docker_driver._note_pkey_error(report, line)
    assert report.unattributed
    assert not docker_driver._is_partitions_error(line)


class FakeImage:
    # OpenSM's image declares these, as the real one does.
    attrs = {"Config": {"Volumes": {"/var/cache/opensm/": {}, "/var/log/": {}}}}


class FakeImages:
    """A host's images, and the low-level pull's stream"""

    def __init__(self, present=False, failures=0, stream_error=None):
        self.present = present
        self.failures = failures
        self.stream_error = stream_error
        self.pulls = []
        self.gets = []

    def get(self, name):
        self.gets.append(name)
        if not self.present:
            raise docker_driver.docker.errors.ImageNotFound("no image")
        return FakeImage()

    def pull(self, repository, tag, stream, decode):
        self.pulls.append(
            f"{repository}@{tag}" if ":" in tag else f"{repository}:{tag}"
        )
        if self.failures:
            self.failures -= 1
            raise docker_driver.docker.errors.APIError("registry down")
        if self.stream_error:
            return iter([{"status": "Pulling"}, {"error": self.stream_error}])
        self.present = True
        return iter([{"status": "Downloaded newer image"}])


def _image_client(images):
    client = FakeManagedClient()
    client.images = images
    client.api = images
    return client


def test_ensure_image_pulls_a_missing_image(managed_config):
    images = FakeImages()
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    driver.ensure_image()
    assert images.pulls == [managed_config.image]
    assert set(images.gets) == {managed_config.image}


def test_a_start_pulls_a_missing_image(managed_config):
    # Creating a container doesn't pull, as running one did, and nothing
    # else in single-node mode brings back an image pruned while it ran.
    images = FakeImages()
    client = _image_client(images)
    docker_driver.ManagedDocker(managed_config, client=client).start()
    assert images.pulls == [managed_config.image]
    assert len(client.created) == 1


def test_ensure_image_pulls_by_digest(managed_config):
    managed_config.image = "ghcr.io/stackhpc/docker-opensm@sha256:" + "a" * 64
    images = FakeImages()
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    driver.ensure_image()
    assert images.pulls == [managed_config.image]


def test_ensure_image_never_pulls_one_already_here(managed_config):
    # A moving tag mustn't change which OpenSM runs from one start to the next.
    images = FakeImages(present=True)
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    driver.ensure_image()
    assert images.pulls == []


def test_ensure_image_reports_an_error_in_the_pull_stream(managed_config):
    images = FakeImages(stream_error="unauthorized: authentication required")
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    with pytest.raises(docker_driver.docker.errors.APIError, match="unauthorized"):
        driver.ensure_image()


class _StopAfter(threading.Event):
    """Set after n waits, so that keep_image's loop ends in a test"""

    def __init__(self, n):
        super().__init__()
        self.n = n

    def wait(self, timeout=None):
        self.n -= 1
        if self.n <= 0:
            self.set()
        return self.is_set()


def test_keep_image_retries_until_the_pull_succeeds(managed_config):
    images = FakeImages(failures=2)
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    driver.keep_image(_StopAfter(4), interval=0)
    assert len(images.pulls) == 3
    assert images.present


def test_keep_image_survives_errors_that_are_not_dockers(managed_config):
    # A daemon restart mid-pull raises requests' errors, not DockerException.
    images = FakeImages()
    calls = []

    def pull(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("daemon restarted")
        return FakeImages.pull(images, *args, **kwargs)

    client = _image_client(images)
    client.api = types.SimpleNamespace(pull=pull)
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    driver.keep_image(_StopAfter(3), interval=0)
    assert images.present


def test_keep_image_pulls_again_after_the_image_is_removed(managed_config):
    images = FakeImages(present=True)
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    stop = _StopAfter(1)
    driver.keep_image(stop, interval=0)
    assert images.pulls == []
    images.present = False
    driver.keep_image(_StopAfter(1), interval=0)
    assert images.pulls == [managed_config.image]


def test_keep_image_stops_when_asked(managed_config):
    images = FakeImages(failures=1000)
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    stop = threading.Event()
    stop.set()
    driver.keep_image(stop, interval=0)
    assert images.pulls == []


def test_ensure_image_pulls_latest_for_an_untagged_name(managed_config):
    managed_config.image = "ghcr.io/stackhpc/docker-opensm"
    images = FakeImages()
    driver = docker_driver.ManagedDocker(managed_config, client=_image_client(images))
    driver.ensure_image()
    assert images.pulls == ["ghcr.io/stackhpc/docker-opensm:latest"]


def test_ensure_image_fails_if_the_pull_leaves_no_image(managed_config):
    images = FakeImages()
    client = _image_client(images)
    client.api = types.SimpleNamespace(pull=lambda *a, **k: iter([{"status": "?"}]))
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    with pytest.raises(docker_driver.docker.errors.ImageNotFound):
        driver.ensure_image()


def test_has_image_takes_any_404_as_absent(managed_config):
    # docker-py makes a 404 ImageNotFound only by its wording.
    class Images:
        def get(self, _name):
            raise docker_driver.docker.errors.NotFound("image not known")

    client = FakeManagedClient()
    client.images = Images()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    assert driver.has_image() is False


# Creating, preparing and starting OpenSM's container


def test_start_prepares_the_created_container_before_starting_it(managed_config):
    client = FakeManagedClient()
    seen = []

    def prepare(container):
        seen.append(container.status)
        client.events.append("prepare")

    docker_driver.ManagedDocker(managed_config, client=client).start(prepare=prepare)
    assert seen == ["created"]
    assert client.events == ["create", "prepare", "start"]


@pytest.mark.parametrize(
    "error",
    [
        ReloadError("the prober exited with status 3"),
        docker_driver.docker.errors.APIError("put failed"),
        ValueError("a bug"),
    ],
)
def test_a_failed_prepare_removes_the_container_unstarted(managed_config, error):
    client = FakeManagedClient()

    def prepare(container):
        raise error

    driver = docker_driver.ManagedDocker(managed_config, client=client)
    with pytest.raises(type(error) if isinstance(error, ValueError) else ReloadError):
        driver.start(prepare=prepare)
    assert "start" not in client.events
    assert client.existing.removed


# OpenSM's cache in its volume


def _cache_file(text, mode=0o644, mtime=1763290000, kind=tarfile.REGTYPE):
    info = tarfile.TarInfo("x")
    info.size = len(text.encode())
    info.mode = mode
    info.mtime = mtime
    info.type = kind
    return text.encode(), info


def test_cache_round_trip(managed_config):
    client = FakeManagedClient()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    container = client.create()
    container.cache["guid2mkey"] = _cache_file("0x1 0x2\n\n" * 3)
    container.cache["guid2lid"] = _cache_file("0x1 1 1\n")
    files = {
        "neighbors": "0x00000000000000a0:1 0x0000000000000051:1\n",
        "guid2mkey": "0x00000000000000a0 0x2222222222222222\n\n",
    }
    driver.write_cache(container, files)
    for name, text in files.items():
        body, info = container.cache[name]
        # Replaced, not merged, and only root can read them
        assert body.decode() == text
        assert info.mode == 0o600
        assert (info.uid, info.gid, info.uname, info.gname) == (0, 0, "root", "root")
        assert info.isfile()
    # What isn't written is left as it was.
    assert container.cache["guid2lid"][0] == b"0x1 1 1\n"

    read = driver.read_cache(container)
    assert set(read) == {"neighbors", "guid2mkey"}
    for name, text in files.items():
        assert read[name][0] == text
        assert read[name][1].tzinfo == datetime.timezone.utc
        assert abs(read[name][1].timestamp() - container.cache[name][1].mtime) < 1


def test_read_cache_leaves_out_missing_files_and_reads_opensms_container(
    managed_config,
):
    container = FakeManagedContainer()
    container.cache["neighbors"] = _cache_file("0xa:1 0xb:1\n", mtime=1763290000)
    client = FakeManagedClient(existing=container)
    files = docker_driver.ManagedDocker(managed_config, client=client).read_cache()
    assert files == {
        "neighbors": (
            "0xa:1 0xb:1\n",
            datetime.datetime(2025, 11, 16, 10, 46, 40, tzinfo=datetime.timezone.utc),
        )
    }


@pytest.mark.parametrize(
    "entry",
    [
        (b"", _cache_file("", kind=tarfile.SYMTYPE)[1]),
        ("0x1 0xé\n".encode(), _cache_file("0x1 0xé\n")[1]),
    ],
)
def test_read_cache_refuses_what_isnt_a_text_file(managed_config, entry):
    container = FakeManagedContainer()
    container.cache["guid2mkey"] = entry
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    with pytest.raises(ReloadError) as raised:
        driver.read_cache()
    assert "0x1" not in str(raised.value)


def test_cache_docker_errors_are_reload_errors(managed_config):
    container = FakeManagedContainer()

    def fail(*_args):
        raise docker_driver.docker.errors.APIError("daemon unhappy")

    container.get_archive = fail
    container.put_archive = fail
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    with pytest.raises(ReloadError, match="read OpenSM's cache"):
        driver.read_cache()
    with pytest.raises(ReloadError, match="write OpenSM's cache"):
        driver.write_cache(container, {"neighbors": ""})


def test_write_cache_fails_if_docker_doesnt_write(managed_config):
    container = FakeManagedContainer()
    container.put_result = False
    driver = docker_driver.ManagedDocker(managed_config, client=FakeManagedClient())
    with pytest.raises(ReloadError, match="didn't write"):
        driver.write_cache(container, {"neighbors": ""})
    with pytest.raises(ValueError):
        driver.write_cache(container, {"../etc/passwd": ""})


# The prober's runners

KEY = 0x5EC12E7000C0FFEE
GETS = [
    docker_driver.probes.Get("n1", "nodeinfo", "0", None, KEY),
    docker_driver.probes.Get("p1", "portinfo", "0,1", 3, KEY),
]
PROBED = (
    b"OWN\n0x0002c903000e0b72\nEND OWN\n"
    b"BEGIN n1\nPortGuid:0x0002c903000e0b72\nEND n1 0\nBEGIN p1\nEND p1 124\n"
)


def test_the_exec_prober_runs_in_opensms_container(managed_config):
    container = FakeManagedContainer()
    container.exec_result = (0, PROBED)
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    own, answers = driver.prober(timeout=4, parallel=7)(GETS)
    assert own == [0x0002C903000E0B72]
    assert answers == {"n1": {"PortGuid": "0x0002c903000e0b72"}, "p1": None}
    ((command, environment, stdout, stderr),) = container.execs
    assert command == ["sh", "-c", docker_driver.probes.SCRIPT]
    assert f"{KEY:016x}" not in " ".join(command)
    assert environment == {
        "AFM_PROBES": f"n1 nodeinfo 0 - 0x{KEY:016x}\np1 portinfo 0,1 3 0x{KEY:016x}",
        "AFM_PROBE_TIMEOUT": "4",
        "AFM_PROBE_PARALLEL": "7",
    }
    # Only stdout, which holds only whitelisted fields
    assert (stdout, stderr) == (True, False)


@pytest.mark.parametrize(
    ("result", "match"),
    [
        ((2, b"OWN\nMkey:0x5ec12e7000c0ffee\n"), "status 2"),
        ((0, b"\xff\xfe"), "isn't text"),
        ((0, b"OWN\nEND OWN\nMkey:0x5ec12e7000c0ffee\n"), "outside a block"),
    ],
)
def test_the_exec_prober_fails_without_showing_its_output(
    managed_config, result, match
):
    container = FakeManagedContainer()
    container.exec_result = result
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    with pytest.raises(docker_driver.probes.ProbeError, match=match) as raised:
        driver.prober()(GETS)
    assert "c0ffee" not in str(raised.value)
    assert isinstance(raised.value, ReloadError)


def test_the_exec_prober_needs_opensm_running(managed_config):
    container = FakeManagedContainer(status="exited")
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    with pytest.raises(ReloadError, match="isn't running"):
        driver.prober()(GETS)
    assert container.execs == []


def test_the_exec_prober_runs_the_real_script(managed_config, tmp_path):
    # The script, run by sh against fake smpquery and ibstat, through the
    # runner: what the driver hands the container is what the script takes.
    fakes = test_prober_script.make_fakes(tmp_path)

    def run(command, environment):
        result = subprocess.run(
            [str(fakes / "tools" / "sh"), *command[1:]],
            env=test_prober_script.fake_environment(fakes, environment),
            capture_output=True,
            timeout=60,
        )
        return result.returncode, result.stdout

    container = FakeManagedContainer()
    container.exec_result = run
    driver = docker_driver.ManagedDocker(
        managed_config, client=FakeManagedClient(existing=container)
    )
    own, answers = driver.prober()(GETS)
    assert own == [0x0002C903000E0B72]
    assert answers["n1"]["PortGuid"] == "0x0002c903000e0b72"
    assert answers["p1"]["ProtectBits"] == "2"
    calls = test_prober_script.invocations(fakes)
    assert f"-D -y 0x{KEY:016x} portinfo 0,1 3" in calls


class FakeHelper:
    def __init__(self, code=0, output=PROBED):
        self.code = code
        self.output = output
        self.events = []

    def start(self):
        self.events.append("start")

    def wait(self, timeout):
        self.events.append(("wait", timeout))
        return {"StatusCode": self.code}

    def logs(self, stdout, stderr):
        self.events.append(("logs", stdout, stderr))
        return self.output

    def remove(self, force, v=False):
        self.events.append(("remove", v))


def _helper_driver(managed_config, helper, present=True):
    client = FakeManagedClient()
    client.images = FakeImages(present=present)
    client.api = client.images

    def create(**kwargs):
        client.created.append(kwargs)
        return helper

    client.create = create
    return docker_driver.ManagedDocker(managed_config, client=client), client


def test_the_helper_prober_runs_a_container_of_its_own(managed_config):
    helper = FakeHelper()
    driver, client = _helper_driver(managed_config, helper)
    own, answers = driver.prober(helper=True, timeout=3, parallel=16)(GETS)
    assert own == [0x0002C903000E0B72] and answers["p1"] is None
    (kwargs,) = client.created
    assert kwargs["image"] == managed_config.image
    assert kwargs["entrypoint"] == ["sh", "-c"]
    assert kwargs["command"] == [docker_driver.probes.SCRIPT]
    assert kwargs["environment"]["AFM_PROBES"].count(f"0x{KEY:016x}") == 2
    assert kwargs["network_mode"] == "host" and kwargs["privileged"]
    # The image's volumes are tmpfs mounts, and removing it removes any
    # anonymous volume too: a helper run every few seconds left one set
    # each time, and filled a host's Docker disk.
    assert kwargs["tmpfs"] == {"/var/cache/opensm": "", "/var/log": ""}
    assert helper.events[0] == "start"
    assert helper.events[1][0] == "wait" and helper.events[1][1] > 3
    assert helper.events[2:] == [("logs", True, False), ("remove", True)]
    # Never pulled
    assert client.images.pulls == []


def test_the_helper_prober_fails_and_is_removed(managed_config):
    helper = FakeHelper(code=3, output=b"Mkey:0x5ec12e7000c0ffee")
    driver, _client = _helper_driver(managed_config, helper)
    with pytest.raises(docker_driver.probes.ProbeError, match="status 3") as raised:
        driver.prober(helper=True)(GETS)
    assert "c0ffee" not in str(raised.value)
    assert helper.events[-1] == ("remove", True)

    helper = FakeHelper()

    def wait(timeout):
        raise docker_driver.requests.exceptions.ReadTimeout("read timed out")

    helper.wait = wait
    driver, _client = _helper_driver(managed_config, helper)
    with pytest.raises(ReloadError, match="ReadTimeout"):
        driver.prober(helper=True)(GETS)
    assert helper.events[-1] == ("remove", True)


def test_the_helper_prober_never_pulls_a_missing_image(managed_config):
    helper = FakeHelper()
    driver, client = _helper_driver(managed_config, helper, present=False)
    with pytest.raises(ReloadError, match="isn't on this host"):
        driver.prober(helper=True)(GETS)
    assert client.created == [] and client.images.pulls == []


# The fabric check with the prober's switches


def test_fabric_takes_the_probers_switches():
    container = FakeExecContainer()
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    driver = docker_driver.DockerReloader("opensm", 30, client=client)
    fabric = driver.fabric(m_key="0x11", sa_key="0x22", switches={0x51, 0x52})
    assert fabric.switches == {0x51, 0x52}
    assert [command[0] for command in container.commands] == ["saquery", "ibstat"]
    assert fabric.sms and fabric.own_guids == {0x040973FFFFC75168}


def test_a_failed_tool_shows_no_key():
    container = FakeExecContainer()
    smir = SMIR.replace("0x0000000000000000", "0x5ec12e7000c0ffee")
    container.exec_run = lambda command: (1, smir.encode())
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    driver = docker_driver.DockerReloader("opensm", 30, client=client)
    with pytest.raises(ReloadError) as raised:
        driver.fabric(m_key="0x11", sa_key="0x22")
    assert "c0ffee" not in str(raised.value)


# What OpenSM logs about ports it can't reach, and its keys, in OpenSM
# 5.17.2's formats, from its binary and libosmvendor's

DR_SEND_ERROR = (
    "Nov 16 10:13:52 412345 [7F1E2D3C] 0x01 -> umad_receiver: ERR 5411: DR SMP "
    "Send completed with error (IB_TIMEOUT) -- dropping"
)
PERFMGR_5411 = (
    "Nov 16 10:13:52 412345 [7F1E2D3C] 0x01 -> perfmgr_send_cr_mad: PerfMgr: ERR "
    "5411: Failed to send clear counters MAD for SwitchIB (0xec0d9a0300b12560) port 3"
)
UNREACHED_NODEINFO = (
    "Nov 16 10:13:52 412345 [7F1E2D3C] 0x01 -> sm_mad_ctrl_send_err_cb: ERR 3113: "
    "MAD completed in error (IB_TIMEOUT): SubnGet(NodeInfo), attr_mod 0x0, "
    "TID 0x1a2b, dest_guid 0x0002c903000e0b72"
)
UNREACHED_PORTINFO = UNREACHED_NODEINFO.replace("NodeInfo", "PortInfo").replace(
    "0x0002c903000e0b72", "0x0000000000000000"
)
TRAP = (
    "Nov 16 10:13:52 412345 [7F1E2D3C] 0x02 -> trap_rcv_process_request: Received "
    "Generic Notice type:1 num:256 (Bad M_Key) Producer:2 (Switch) from LID:5 "
    "TID:0x0000000000001234"
)
NOTICE_M_KEY = "\t\t\t\tm_key....................0x5ec12e7000c0ffee"
SENDING = (
    "Nov 16 10:13:52 412345 [7F1E2D3C] 0x80 -> osm_send_req_mad: Sending NodeInfo "
    "(0x11) SubnGet request, modifier 0x0, TID 0x1a2b, MKey 0x5ec12e7000c0ffee"
)


def _report(*lines):
    report = SweepReport()
    for line in lines:
        docker_driver._note(report, line)
    return report


def test_a_dr_send_error_is_recorded_but_not_perfmgrs():
    assert _report(DR_SEND_ERROR).dr_errors
    report = _report(PERFMGR_5411)
    assert not report.dr_errors
    assert not report.unattributed and report.unreached == set()


def test_unreached_ports_by_dest_guid():
    report = _report(UNREACHED_NODEINFO, UNREACHED_PORTINFO)
    assert report.unreached == {0x0002C903000E0B72, 0}
    # Not pkey errors
    assert not report.unattributed and report.pkey_errors == set()
    # A Set, or another attribute's Get, isn't one.
    set_line = UNREACHED_NODEINFO.replace("SubnGet", "SubnSet")
    assert _report(set_line).unreached == set()
    assert _report(MAD_PKEY_FAILURES[0]).unreached == set()
    # Today's P_KeyTable handling is unchanged.
    assert _report(MAD_PKEY_FAILURES[0]).unattributed


def test_bad_m_key_traps_are_counted():
    report = _report(TRAP, NOTICE_M_KEY, TRAP)
    assert report.bad_m_key_traps == 2
    assert _report(TRAP.replace("num:256", "num:257")).bad_m_key_traps == 0


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        (TRAP, None),
        (TRAP.replace("0x02 -> trap_rcv_process_request: Received", "Reporting"), None),
        (SENDING, None),
        (SENDING.replace("Sending", "Setting").replace("SubnGet", "SubnSet"), None),
        (NOTICE_M_KEY, "\t\t\t\tm_key....................<masked>"),
        (
            "x: ERR 8B04: Non-Zero MAD SM_Key: 0x1234 != SM_Key: 0xabcd; SA MAD",
            "x: ERR 8B04: Non-Zero MAD SM_Key: <masked> != SM_Key: <masked>; SA MAD",
        ),
        (
            "x: ERR 4608: Request from non-trusted requester: Given "
            "SM_Key:0x00000000000000ab",
            "x: ERR 4608: Request from non-trusted requester: Given SM_Key:<masked>",
        ),
        (
            "x: ERR 2F18: Got SM (0x1) with sm_key 0x0000000000000001 that",
            "x: ERR 2F18: Got SM (0x1) with sm_key <masked> that",
        ),
        ("MKEY=0x1, sa_key: 0x2", "MKEY=<masked>, sa_key: <masked>"),
        # Not keys
        ("m_key_lease_period.......0x3C", "m_key_lease_period.......0x3C"),
        (
            "Port 0x1 has unknown M_Key, protection level 2",
            "Port 0x1 has unknown M_Key, protection level 2",
        ),
    ],
)
def test_keys_are_masked_and_trap_lines_dropped(line, shown):
    assert docker_driver._mask_keys(line) == shown


def test_a_failed_start_shows_no_key(managed_config):
    log = "\n".join(
        [
            "opensm: starting",
            UNREACHED_NODEINFO,
            SENDING,
            TRAP,
            "Generic Notice dump:",
            NOTICE_M_KEY,
            "x: ERR 2F18: Got SM (0x1) with sm_key 0x5ec12e7000c0ffee that",
            TRAP,
        ]
    )
    client = FakeManagedClient(log=log, status="exited")
    with pytest.raises(ReloadError, match="exited while starting") as raised:
        docker_driver.ManagedDocker(managed_config, client=client).start()
    message = str(raised.value)
    assert "c0ffee" not in message.lower()
    assert "Bad M_Key" not in message and "MKey" not in message
    # The last five lines that may be shown
    assert message.count(" / ") == 4
    assert "m_key....................<masked>" in message


def test_a_partitions_error_shows_no_key(managed_config):
    line = "0x01 -> PARSE ERROR line 3: m_key 0x5ec12e7000c0ffee"
    client = FakeManagedClient(log=line)
    with pytest.raises(ReloadError) as raised:
        docker_driver.ManagedDocker(managed_config, client=client).start()
    assert "c0ffee" not in str(raised.value)


def test_pkey_warnings_show_no_key(caplog):
    caplog.set_level("WARNING")
    docker_driver._note(
        SweepReport(),
        "x: ERR 3113: MAD completed in error (IB_TIMEOUT): SubnSet(P_KeyTable), "
        "attr_mod 0x0, TID 0x1, dest_guid 0x1, MKey 0x5ec12e7000c0ffee",
    )
    assert "pkey table" in caplog.text
    assert "c0ffee" not in caplog.text


def test_a_start_reports_what_it_couldnt_reach(managed_config):
    log = "\n".join([DR_SEND_ERROR, UNREACHED_NODEINFO, TRAP, STARTED])
    client = FakeManagedClient(log=log)
    report = docker_driver.ManagedDocker(managed_config, client=client).start()
    assert report.dr_errors
    assert report.unreached == {0x0002C903000E0B72}
    assert report.bad_m_key_traps == 1


def test_a_sweep_reports_what_it_couldnt_reach():
    after = SWEEP.replace(
        "Nov 05 16:59:16 191669",
        f"{DR_SEND_ERROR}\n{UNREACHED_PORTINFO}\n{TRAP}\nNov 05 16:59:16 191669",
    )
    # Before the signal, and so not this sweep's
    container = FakeContainer(before=f"{DR_SEND_ERROR}\n{TRAP}", after=after)
    report = _reloader(container).reload()
    assert report.dr_errors
    assert report.unreached == {0}
    assert report.bad_m_key_traps == 1


# The helper prober's container holds the plan, keys included, in its
# configuration: it has a fixed name, and is removed wherever it's left.


class NamedClient(FakeManagedClient):
    """A client that tells containers apart by name"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.named = {}
        self.removed = []

    def get(self, name):
        if name in self.named:
            return self.named[name]
        return super().get(name)

    def leave(self, name):
        client = self

        class Left:
            def remove(self, force, v=False):
                client.removed.append(name)
                del client.named[name]

        self.named[name] = Left()


def test_the_helper_prober_has_a_fixed_name_and_replaces_one_left_behind(
    managed_config,
):
    client = NamedClient()
    client.create = lambda **kwargs: client.created.append(kwargs) or FakeHelper()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    client.leave("opensm-prober")
    driver.prober(helper=True)(GETS)
    assert client.removed == ["opensm-prober"]
    assert client.created[0]["name"] == "opensm-prober"


def test_start_and_stop_remove_a_helper_prober_left_behind(managed_config):
    client = NamedClient()
    driver = docker_driver.ManagedDocker(managed_config, client=client)
    client.leave("opensm-prober")
    driver.start()
    assert client.removed == ["opensm-prober"]
    client.leave("opensm-prober")
    driver.stop()
    assert client.removed == ["opensm-prober"] * 2
    # None left: nothing to remove, and no error
    driver.stop()


def test_a_cache_volume_modes_options_reach_docker_together(managed_config):
    managed_config.volumes = "opensm_cache:/var/cache/opensm:z,ro,opensm_logs:/var/log"
    driver = docker_driver.ManagedDocker(managed_config, client=FakeManagedClient())
    volumes = driver._run_kwargs()["volumes"]
    assert volumes[1:] == [
        "opensm_cache:/var/cache/opensm:z,ro",
        "opensm_logs:/var/log",
    ]
