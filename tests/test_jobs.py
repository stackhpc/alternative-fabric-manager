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


def _status(manager, job_id):
    return manager.get_job(job_id).status


def _fabric(switches=(SWITCH,), sms=((OWN, 3),)):
    return Fabric(switches, sms, OWN)


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
