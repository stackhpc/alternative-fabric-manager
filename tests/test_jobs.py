"""Jobs complete only when OpenSM has swept their effect on a sound fabric"""

import datetime

import pytest

from alternative_fabric_manager import db, effects, partitions
from alternative_fabric_manager.drivers import (
    Fabric,
    PartitionsRejected,
    ReloadError,
    SweepReport,
)
from tests import conftest
from tests.conftest import GUID, NETWORK_ID, PORT_ID

OTHER = "d13bba05-eb07-45ba-ace2-765706b2d701"
OTHER_PORT = "e13bba05-eb07-45ba-ace2-765706b2d701"
SWITCH = 0xEC0D9A0300B12560
OWN = 0x040973FFFFC75168
# The host's other port, which OpenSM may be bound to instead
OWN_OTHER_PORT = 0x040973FFFFC75169


def _status(manager, job_id):
    return manager.get_job(job_id).status


def _fabric(switches=(SWITCH,), sms=((OWN, 3),)):
    return Fabric(switches, sms, {OWN, OWN_OTHER_PORT})


@pytest.fixture
def fabric(manager, reloader):
    """A fabric with one switch, confirmed"""
    reloader.fabric_result = _fabric()
    assert manager.confirm_switches() == [SWITCH]
    return reloader


def test_a_port_completes_on_a_sound_fabric(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    job = manager.put_port(PORT_ID, conftest.port())
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_pkey_errors_keep_a_port_pending_and_are_retried(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    network = manager.put_network(OTHER, conftest.network(OTHER, 200))
    job = manager.put_port(PORT_ID, conftest.port())
    fabric.report = SweepReport(pkey_errors={GUID})
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    # Other objects' jobs aren't held up.
    assert _status(manager, network) == db.JobStatus.COMPLETED

    calls = fabric.calls
    fabric.report = None
    manager.reconcile()
    assert fabric.calls == calls + 1
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_cleared_enforcement_holds_every_job_until_cleared(manager, fabric):
    fabric.report = SweepReport(enforcement_errors={f"{SWITCH:016x}:3"})
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    fabric.report = None
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING

    assert manager.clear_enforcement_errors() == [f"{SWITCH:016x}:3"]
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


@pytest.mark.parametrize(
    "seen, detail",
    [
        (_fabric(switches=()), "Missing switches"),
        (_fabric(sms=((OWN, 3), (0x1234, 3))), "Another subnet manager is master"),
        (ReloadError("ibswitches failed"), "Can't query the fabric"),
    ],
)
def test_an_unsound_fabric_holds_jobs(manager, fabric, seen, detail):
    job = manager.put_network(NETWORK_ID, conftest.network())
    fabric.fabric_result = seen
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    with manager.sessionmaker() as session:
        applied = session.get(db.Applied, 1)
        assert not applied.check_ok
        assert detail in applied.check_detail

    fabric.fabric_result = _fabric()
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_a_standby_subnet_manager_is_only_logged(manager, fabric):
    fabric.fabric_result = _fabric(sms=((OWN, 3), (0x1234, 2)))
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_unconfirmed_switches_hold_jobs(manager, reloader):
    reloader.fabric_result = _fabric()
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    manager.confirm_switches()
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_a_stale_fabric_check_holds_jobs(manager, fabric, monkeypatch):
    from alternative_fabric_manager import manager as manager_mod

    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.sync()
    manager.check_fabric()
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=3 * manager.config.check_interval
    )
    monkeypatch.setattr(manager_mod, "_now", lambda: later)
    # Keep the job inside its deadline.
    manager.config.job_timeout = 10 * manager.config.check_interval
    manager.evaluate_jobs()
    assert _status(manager, job) == db.JobStatus.PENDING


def test_the_keys_reach_the_fabric_check(manager, config, reloader):
    config.hardening = "m_key"
    manager.check_fabric()
    m_key, sa_key = reloader.fabric_calls[-1]
    assert m_key == manager.keys()["m_key"]
    assert sa_key is None


def test_a_quarantined_port_is_released_by_its_next_request(
    manager, fabric, monkeypatch
):
    manager.put_network(NETWORK_ID, conftest.network())
    first = manager.put_port(PORT_ID, conftest.port())
    real = partitions.validate

    def validate(content, *args, **kwargs):
        if f"0x{GUID:016x}" in content:
            raise partitions.InvalidPartitions("bad port", guids={GUID})
        return real(content, *args, **kwargs)

    monkeypatch.setattr(partitions, "validate", validate)
    manager.reconcile()
    job = manager.get_job(first)
    assert job.status == db.JobStatus.FAILED
    assert "Quarantined port" in job.error

    monkeypatch.setattr(partitions, "validate", real)
    manager.reconcile()
    # Still left out, until it's sent again
    with open(manager.config.partitions_file) as f:
        assert f"0x{GUID:016x}" not in f.read()
    again = manager.put_port(PORT_ID, conftest.port())
    manager.reconcile()
    assert _status(manager, again) == db.JobStatus.COMPLETED


def test_a_rejected_line_quarantines_what_changed_on_it(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    port = manager.put_port(PORT_ID, conftest.port())
    network = manager.put_network(OTHER, conftest.network(OTHER, 200))

    real = fabric.reload

    def reload():
        with open(manager.config.partitions_file) as f:
            content = f.read()
        for number, line in enumerate(content.splitlines(), 1):
            if f"0x{GUID:016x}" in line:
                raise PartitionsRejected("PARSE ERROR", line=number)
        return real()

    fabric.reload = reload
    manager.reconcile()
    assert _status(manager, port) == db.JobStatus.FAILED
    assert _status(manager, network) == db.JobStatus.COMPLETED


def test_an_unchanged_rejected_line_fails_the_new_generation(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    job = manager.put_network(OTHER, conftest.network(OTHER, 200))

    def reload():
        # The default partition's line, which no object changed
        raise PartitionsRejected("PARSE ERROR", line=3)

    fabric.reload = reload
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.FAILED


def test_a_superseded_port_create_does_not_complete(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    first = manager.put_port(PORT_ID, conftest.port())
    second = manager.put_port(PORT_ID, conftest.port(network_id=OTHER))
    manager.reconcile()
    assert _status(manager, first) == db.JobStatus.PENDING
    assert _status(manager, second) == db.JobStatus.COMPLETED


def test_a_delete_completes_whatever_else_has_the_guid(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_port(PORT_ID, conftest.port())
    manager.reconcile()
    deleted = manager.delete_port(PORT_ID)
    # The GUID's new port, as after an instance is rebuilt
    created = manager.put_port(OTHER_PORT, conftest.port(port_id=OTHER_PORT))
    manager.reconcile()
    assert _status(manager, deleted) == db.JobStatus.COMPLETED
    assert _status(manager, created) == db.JobStatus.COMPLETED


def test_effects_hold_relative_to_their_object():
    attribution = {
        "partitions": {"100": "net-a"},
        "members": {"port-a": [[100, "full"]]},
    }
    assert effects.holds(effects.network_present("net-a", 100), attribution)
    assert not effects.holds(effects.network_present("net-b", 100), attribution)
    assert effects.holds(effects.network_present("net-f", None), attribution)
    assert effects.holds(effects.port_present("port-a", 100, "1"), attribution)
    assert not effects.holds(effects.port_present("port-a", 200, "1"), attribution)
    assert effects.holds(effects.port_absent("port-b"), attribution)
    assert not effects.holds(effects.port_absent("port-a"), attribution)
    assert not effects.holds(effects.network_absent("net-a", [], []), attribution)
    assert not effects.holds(
        effects.network_absent("net-x", ["port-a"], ["1"]), attribution
    )
    assert effects.holds(effects.network_absent("net-x", ["port-b"], []), attribution)
    assert not effects.holds({"kind": "unknown"}, attribution)
    effect = effects.port_present("port-a", 100, "1")
    assert effects.loads(effects.dumps(effect)) == effect


def test_opensm_bound_to_another_local_port_is_not_another_master(manager, fabric):
    fabric.fabric_result = _fabric(sms=((OWN_OTHER_PORT, 3),))
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_an_untraceable_pkey_error_holds_every_job(manager, fabric):
    job = manager.put_network(NETWORK_ID, conftest.network())
    fabric.report = SweepReport(unattributed=True)
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    # It's swept again while a job waits on it.
    calls = fabric.calls
    fabric.report = None
    manager.reconcile()
    assert fabric.calls == calls + 1
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_pkey_errors_with_no_waiting_job_arent_swept_again(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    job = manager.put_port(PORT_ID, conftest.port())
    fabric.report = SweepReport(pkey_errors={GUID})
    manager.reconcile()
    manager.get_job(job)
    with manager.sessionmaker() as session:
        session.get(db.Job, job).status = db.JobStatus.FAILED
        session.commit()
    calls = fabric.calls
    manager.reconcile()
    assert fabric.calls == calls


def test_an_effect_swept_by_the_deadline_completes(manager, fabric, monkeypatch):
    from alternative_fabric_manager import manager as manager_mod

    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.sync()
    manager.check_fabric()
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=manager.config.job_timeout + 1
    )
    real = manager_mod._now
    # Past the deadline, with the fabric check still fresh
    monkeypatch.setattr(manager_mod, "_now", lambda: later)
    with manager.sessionmaker() as session:
        session.get(db.Applied, 1).check_time = later
        session.commit()
    assert _status(manager, job) == db.JobStatus.COMPLETED
    monkeypatch.setattr(manager_mod, "_now", real)


def test_a_port_on_a_quarantined_network_waits_for_it(manager, fabric, monkeypatch):
    # networking-mlnx resends failed objects independently, so a port may
    # come before its network's next request; a 400 would be final.
    manager.put_network(NETWORK_ID, conftest.network())
    real = partitions.validate
    broken = [True]

    def validate(content, *args, **kwargs):
        if broken[0] and f"net-{NETWORK_ID}" in content:
            raise partitions.InvalidPartitions("bad network", pkeys={0x64})
        return real(content, *args, **kwargs)

    monkeypatch.setattr(partitions, "validate", validate)
    manager.reconcile()
    port = manager.put_port(PORT_ID, conftest.port())
    manager.reconcile()
    assert _status(manager, port) == db.JobStatus.PENDING
    assert "quarantined" in manager.get_job(port).error

    broken[0] = False
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    assert _status(manager, port) == db.JobStatus.COMPLETED


def _reject_guid(manager, fabric, times=None):
    """Have OpenSM reject the line with the port's GUID"""
    real = fabric.reload
    count = [0]

    def reload():
        with open(manager.config.partitions_file) as f:
            content = f.read()
        for number, line in enumerate(content.splitlines(), 1):
            if f"0x{GUID:016x}" in line and (times is None or count[0] < times):
                count[0] += 1
                raise PartitionsRejected("PARSE ERROR", line=number)
        return real()

    fabric.reload = reload


def test_a_rejected_start_quarantines_against_the_last_sweep(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    job = manager.put_port(PORT_ID, conftest.port())
    starts = []

    def start():
        with open(manager.config.partitions_file) as f:
            content = f.read()
        starts.append(content)
        for number, line in enumerate(content.splitlines(), 1):
            if f"0x{GUID:016x}" in line:
                raise PartitionsRejected("PARSE ERROR", line=number)

    fabric.start = start
    manager._start_opensm()
    assert len(starts) == 2
    assert _status(manager, job) == db.JobStatus.FAILED
    # And later requests aren't held up by it
    other = manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.reconcile()
    assert _status(manager, other) == db.JobStatus.COMPLETED


def test_a_restart_doesnt_make_a_bad_port_the_baseline(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    job = manager.put_port(PORT_ID, conftest.port())
    # A driver whose start doesn't start OpenSM
    manager._start_opensm()
    _reject_guid(manager, fabric)
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.FAILED
    other = manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.reconcile()
    assert _status(manager, other) == db.JobStatus.COMPLETED


def test_an_object_sent_again_during_the_sweep_isnt_quarantined(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    first = manager.put_port(PORT_ID, conftest.port())
    again = []
    _reject_guid(manager, fabric, times=1)
    rejecting = fabric.reload

    def reload():
        if not again:
            again.append(manager.put_port(PORT_ID, conftest.port()))
        return rejecting()

    fabric.reload = reload
    manager.reconcile()
    # The first rendered request is unattributable now, but the one OpenSM
    # never saw is applied.
    assert _status(manager, first) == db.JobStatus.FAILED
    manager.reconcile()
    assert _status(manager, again[0]) == db.JobStatus.COMPLETED


def test_a_request_during_a_failed_sweep_is_tried(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    job = manager.put_network(OTHER, conftest.network(OTHER, 200))
    later = []

    def reload():
        if not later:
            later.append(manager.delete_network(OTHER))
            # The default partition's line, which no object changed
            raise PartitionsRejected("PARSE ERROR", line=3)
        return SweepReport()

    fabric.reload = reload
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.FAILED
    manager.reconcile()
    assert _status(manager, later[0]) == db.JobStatus.COMPLETED


def test_a_failed_restore_is_retried(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    results = [PartitionsRejected("PARSE ERROR", line=3), ReloadError("down")]

    def reload():
        if results:
            raise results.pop(0)
        return SweepReport()

    fabric.reload = reload
    manager.reconcile()
    calls = []
    fabric.reload = lambda: calls.append(1) or SweepReport()
    manager.reconcile()
    assert calls == [1]
    with open(manager.config.partitions_file) as f:
        assert f"net-{OTHER}" not in f.read()
    manager.reconcile()
    assert calls == [1]


def test_many_culprits_are_all_quarantined(manager, fabric, monkeypatch):
    ids = [f"{i:08x}-eb07-45ba-ace2-765706b2d701" for i in range(30)]
    for i, network_id in enumerate(ids):
        manager.put_network(network_id, conftest.network(network_id, 100 + i))
    real = partitions.validate

    def validate(content, *args, **kwargs):
        for i, network_id in enumerate(ids):
            if f"net-{network_id}" in content:
                raise partitions.InvalidPartitions("bad", pkeys={100 + i})
        return real(content, *args, **kwargs)

    monkeypatch.setattr(partitions, "validate", validate)
    manager.reconcile()
    with manager.sessionmaker() as session:
        assert session.query(db.Quarantine).count() == 30


def test_a_reused_vlan_waits_for_the_old_networks_deletion(manager, fabric):
    # networking-mlnx orders requests per object only: a network reusing a
    # deleted network's VLAN can arrive before the deletion.
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    created = manager.put_network(OTHER, conftest.network(OTHER, 100))
    manager.reconcile()
    assert _status(manager, created) == db.JobStatus.PENDING
    assert "waiting" in manager.get_job(created).error
    # A port on the waiting network waits with it.
    port = manager.put_port(PORT_ID, conftest.port(network_id=OTHER))
    deleted = manager.delete_network(NETWORK_ID)
    manager.reconcile()
    assert _status(manager, deleted) == db.JobStatus.COMPLETED
    assert _status(manager, created) == db.JobStatus.COMPLETED
    assert manager.get_job(created).error == ""
    assert _status(manager, port) == db.JobStatus.COMPLETED
    with manager.sessionmaker() as session:
        assert session.query(db.Deferred).count() == 0


def test_a_guid_waits_for_its_old_ports_deletion(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_port(PORT_ID, conftest.port())
    manager.reconcile()
    created = manager.put_port(OTHER_PORT, conftest.port(port_id=OTHER_PORT))
    manager.reconcile()
    assert _status(manager, created) == db.JobStatus.PENDING
    manager.delete_port(PORT_ID)
    manager.reconcile()
    assert _status(manager, created) == db.JobStatus.COMPLETED


def test_a_later_request_supersedes_a_waiting_one(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    first = manager.put_network(OTHER, conftest.network(OTHER, 100))
    second = manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.reconcile()
    assert manager.get_job(first).status == db.JobStatus.FAILED
    assert "Superseded" in manager.get_job(first).error
    assert _status(manager, second) == db.JobStatus.COMPLETED


def test_deleting_a_waiting_network_drops_it_and_its_ports(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    created = manager.put_network(OTHER, conftest.network(OTHER, 100))
    port = manager.put_port(PORT_ID, conftest.port(network_id=OTHER))
    deleted = manager.delete_network(OTHER)
    manager.reconcile()
    assert _status(manager, created) == db.JobStatus.FAILED
    assert _status(manager, port) == db.JobStatus.FAILED
    assert _status(manager, deleted) == db.JobStatus.COMPLETED
    manager.delete_network(NETWORK_ID)
    manager.reconcile()
    with manager.sessionmaker() as session:
        assert session.query(db.Network).count() == 0
        assert session.query(db.Deferred).count() == 0


def test_a_timed_out_wait_is_dropped(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    created = manager.put_network(OTHER, conftest.network(OTHER, 100))
    with manager.sessionmaker() as session:
        session.get(db.Job, created).deadline = datetime.datetime(
            2000, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.commit()
    manager.reconcile()
    assert _status(manager, created) == db.JobStatus.FAILED
    # The deletion, now, doesn't bring it back.
    manager.delete_network(NETWORK_ID)
    manager.reconcile()
    with manager.sessionmaker() as session:
        assert session.get(db.Network, OTHER) is None


def test_a_waiting_guid_change_doesnt_complete_on_the_old_guid(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.put_port(PORT_ID, conftest.port())
    holder = "20:12:34:56:00:00:00:00:02"
    manager.put_port(OTHER_PORT, conftest.port(OTHER_PORT, OTHER, client_id=holder))
    manager.reconcile()
    # The port takes the GUID that the other port, whose deletion is late,
    # still holds.
    job = manager.put_port(PORT_ID, conftest.port(client_id=holder))
    manager.reconcile()
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.COMPLETED
    assert manager.get_job(job).error == ""


def test_a_port_on_a_network_whose_wait_ended_is_refused(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    created = manager.put_network(OTHER, conftest.network(OTHER, 100))
    with manager.sessionmaker() as session:
        session.get(db.Job, created).deadline = datetime.datetime(
            2000, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.commit()
    assert _status(manager, created) == db.JobStatus.FAILED
    with pytest.raises(partitions.InvalidRequest, match="no partition"):
        manager.put_port(PORT_ID, conftest.port(network_id=OTHER))


def test_ports_waiting_on_a_dropped_network_fail(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    created = manager.put_network(OTHER, conftest.network(OTHER, 100))
    port = manager.put_port(PORT_ID, conftest.port(network_id=OTHER))
    with manager.sessionmaker() as session:
        session.get(db.Job, created).deadline = datetime.datetime(
            2000, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.commit()
    manager.reconcile()
    job = manager.get_job(port)
    assert job.status == db.JobStatus.FAILED
    assert "ended unapplied" in job.error


def test_an_applied_wait_keeps_its_deadline_and_leaves_quarantine(
    manager, fabric, monkeypatch
):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.reconcile()
    update = manager.put_network(OTHER, conftest.network(OTHER, 100))
    with manager.sessionmaker() as session:
        before = session.get(db.Job, update).deadline
    # The network's current state is quarantined while its update waits.
    real = partitions.validate
    broken = [True]

    def validate(content, *args, **kwargs):
        if broken[0] and f"net-{OTHER}=0x00c8" in content:
            raise partitions.InvalidPartitions("bad network", pkeys={200})
        return real(content, *args, **kwargs)

    monkeypatch.setattr(partitions, "validate", validate)
    manager.reconcile()
    assert _status(manager, update) == db.JobStatus.PENDING
    broken[0] = False
    manager.delete_network(NETWORK_ID)
    manager.reconcile()
    assert _status(manager, update) == db.JobStatus.COMPLETED
    with manager.sessionmaker() as session:
        assert session.get(db.Job, update).deadline == before
        assert session.get(db.Quarantine, ("network", OTHER)) is None


def test_a_waiting_ports_effect_has_its_networks_pkey_when_applied(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 100))
    port = manager.put_port(PORT_ID, conftest.port(network_id=OTHER))
    # The waiting network is sent again with another VLAN, before the
    # holder of the first is deleted.
    manager.put_network(OTHER, conftest.network(OTHER, 300))
    manager.reconcile()
    assert _status(manager, port) == db.JobStatus.COMPLETED
    with manager.sessionmaker() as session:
        assert effects.loads(session.get(db.Job, port).effect)["pkey"] == 300


def test_jobs_wait_while_opensm_isnt_master(manager, fabric):
    fabric.fabric_result = _fabric(sms=((OWN, 2), (0x1234, 1)))
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    assert _status(manager, job) == db.JobStatus.PENDING
    with manager.sessionmaker() as session:
        assert "isn't master" in session.get(db.Applied, 1).check_detail


def test_an_existing_networks_ended_wait_doesnt_fail_its_ports(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    holder = "20:12:34:56:00:00:00:00:02"
    manager.put_port(OTHER_PORT, conftest.port(OTHER_PORT, OTHER, client_id=holder))
    manager.reconcile()
    # The network's VLAN change waits on another network's pkey, and a port
    # on it waits on a GUID.
    update = manager.put_network(NETWORK_ID, conftest.network(NETWORK_ID, 200))
    port = manager.put_port(PORT_ID, conftest.port(client_id=holder))
    with manager.sessionmaker() as session:
        session.get(db.Job, update).deadline = datetime.datetime(
            2000, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.commit()
    manager.reconcile()
    assert _status(manager, update) == db.JobStatus.FAILED
    assert _status(manager, port) == db.JobStatus.PENDING
    manager.delete_port(OTHER_PORT)
    manager.reconcile()
    assert _status(manager, port) == db.JobStatus.COMPLETED


def test_a_waiting_request_isnt_failed_by_a_render_it_wasnt_in(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.reconcile()
    waiting = manager.put_network(OTHER, conftest.network(OTHER, 100))

    def reload():
        # The default partition's line, which no object changed
        raise PartitionsRejected("PARSE ERROR", line=3)

    fabric.reload = reload
    manager.reconcile()
    assert _status(manager, waiting) == db.JobStatus.PENDING


def test_a_sweep_needs_a_fabric_check_after_it(manager, fabric):
    manager.check_fabric()
    job = manager.put_network(NETWORK_ID, conftest.network())
    manager.sync()
    # The last check is from before the sweep.
    assert _status(manager, job) == db.JobStatus.PENDING
    manager.check_fabric()
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_another_master_invalidates_the_last_sweep(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    manager.reconcile()
    job = manager.put_network(OTHER, conftest.network(OTHER, 200))
    manager.sync()
    # Another SM masters for a while, after the sweep.
    fabric.fabric_result = _fabric(sms=((OWN, 2), (0x1234, 3)))
    manager.check_fabric()
    fabric.fabric_result = _fabric()
    manager.check_fabric()
    manager.evaluate_jobs()
    assert _status(manager, job) == db.JobStatus.PENDING
    calls = fabric.calls
    manager.reconcile()
    assert fabric.calls == calls + 1
    assert _status(manager, job) == db.JobStatus.COMPLETED


def test_a_start_that_swept_is_recorded(manager, fabric):
    manager.put_network(NETWORK_ID, conftest.network())
    job = manager.put_network(OTHER, conftest.network(OTHER, 200))
    fabric.start = lambda: SweepReport()
    manager._start_opensm()
    manager.check_fabric()
    manager.evaluate_jobs()
    assert _status(manager, job) == db.JobStatus.COMPLETED
