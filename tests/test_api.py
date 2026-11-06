import pathlib

import pytest

from alternative_fabric_manager import db
from tests import conftest
from tests.conftest import AUTH, GUID, NETWORK_ID, PORT_ID, URL

OTHER = "d13bba05-eb07-45ba-ace2-765706b2d701"


def _post(client, kind, body):
    return client.post(f"{URL}/{kind}", json=body, headers=AUTH)


def _job(client, response):
    assert response.status_code == 202, response.text
    path = response.json()
    assert path.startswith("app/jobs/")
    return client.get("/ufmRestV3/" + path, headers=AUTH).json()


def _file(config):
    return pathlib.Path(config.partitions_file).read_text()


def _expire(manager):
    """Put every open job past its deadline"""
    import datetime

    with manager.sessionmaker() as session:
        for job in session.query(db.Job):
            job.deadline = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        session.commit()


def _members(config):
    """The partitions file's members, by pkey"""
    from alternative_fabric_manager import partitions

    found = {}
    for pkey, members, _line in partitions._parse(_file(config)):
        found.setdefault(pkey, {}).update(members)
    return found


def test_requests_need_the_token(client):
    for headers in (
        {},
        {"Authorization": "Basic wrong"},
        {"Authorization": conftest.TOKEN},
    ):
        response = client.post(
            f"{URL}/Network", json=conftest.network(), headers=headers
        )
        assert response.status_code == 401


def test_health_needs_no_token(client):
    assert client.get("/healthz").status_code == 200


def test_unknown_domain(client):
    response = client.post(
        "/ufmRestV3/other/Network", json=conftest.network(), headers=AUTH
    )
    assert response.status_code == 404


def test_network_job_completes_once_applied(client, manager, config, reloader):
    response = _post(client, "Network", conftest.network())
    assert _job(client, response)["Status"] == "Pending"
    assert reloader.calls == 0

    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert reloader.calls == 1
    assert f"net-{NETWORK_ID}=0x0064, indx0, ipoib, rate=3, mtu=4, sl=0 :" in _file(
        config
    )
    assert _members(config)[0x64] == {}


def test_port_is_a_full_member(client, manager, config):
    _post(client, "Network", conftest.network())
    response = _post(client, "Port", conftest.port())
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert _members(config)[0x64] == {GUID: "full"}


@pytest.mark.parametrize(
    "client_id",
    [
        conftest.CLIENT_ID,
        # The legacy prefix, which Ironic's ramdisk also uses
        "ff:00:00:00:00:00:02:00:00:02:c9:00:12:34:56:00:00:78:21:b6",
        conftest.CLIENT_ID.upper(),
    ],
)
def test_guid_from_client_id(client, manager, config, client_id):
    _post(client, "Network", conftest.network())
    _post(client, "Port", conftest.port(client_id=client_id))
    manager.apply_pending()
    assert f"0x{GUID:016x}=full" in _file(config)


def test_client_id_by_option_number(client, manager, config):
    # Ironic sets the option by its number.
    body = conftest.port()
    body["extra_dhcp_opts"][0]["opt_name"] = "61"
    _post(client, "Network", conftest.network())
    assert _post(client, "Port", body).status_code == 202


@pytest.mark.parametrize(
    "client_id", [None, "not-a-guid", "20:12:34", "20:00:00:00:00:00:00:00:00"]
)
def test_port_without_a_usable_client_id_is_rejected(client, client_id):
    _post(client, "Network", conftest.network())
    response = _post(client, "Port", conftest.port(client_id=client_id))
    assert response.status_code == 400


def test_port_with_two_client_ids_is_rejected(client):
    body = conftest.port()
    body["extra_dhcp_opts"].append(
        {"opt_name": "61", "opt_value": "20:00:00:00:00:00:00:00:01"}
    )
    _post(client, "Network", conftest.network())
    assert _post(client, "Port", body).status_code == 400


def test_ipv6_client_id_is_ignored(client):
    body = conftest.port()
    body["extra_dhcp_opts"].append(
        {
            "opt_name": "client-id",
            "opt_value": "20:00:00:00:00:00:00:00:01",
            "ip_version": 6,
        }
    )
    _post(client, "Network", conftest.network())
    assert _post(client, "Port", body).status_code == 202


def test_port_on_unknown_network_is_rejected(client):
    # It can't be put in a partition, so it must not become ACTIVE.
    assert _post(client, "Port", conftest.port()).status_code == 400


@pytest.mark.parametrize("segmentation_id", [None, "x", 0, 0x7FFF, 40000])
def test_network_needs_a_partition_key(client, segmentation_id):
    response = _post(
        client, "Network", conftest.network(segmentation_id=segmentation_id)
    )
    assert response.status_code == 400


def test_a_partition_key_in_use_waits_for_its_network(client, manager, config):
    _post(client, "Network", conftest.network())
    response = _post(client, "Network", conftest.network(network_id=OTHER))
    manager.apply_pending()
    # One partition key, one network: the second waits, and fails at its
    # deadline if the first is never deleted.
    assert _job(client, response)["Status"] == "Pending"
    assert f"net-{OTHER}" not in _file(config)
    _expire(manager)
    job = _job(client, response)
    assert job["Status"] == "Failed"
    assert "Timed out waiting: partition key 0x0064" in job["Error"]


def test_body_needs_an_id(client):
    body = conftest.network()
    del body["id"]
    assert _post(client, "Network", body).status_code == 400


def test_update_moves_port(client, manager, config):
    other = "d13bba05-eb07-45ba-ace2-765706b2d701"
    _post(client, "Network", conftest.network())
    _post(
        client,
        "Network",
        conftest.network(network_id=other, segmentation_id=200),
    )
    _post(client, "Port", conftest.port())
    response = client.put(
        f"{URL}/Port/{PORT_ID}",
        json=conftest.port(network_id=other),
        headers=AUTH,
    )
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    members = _members(config)
    assert members[0xC8] == {GUID: "full"}
    assert members[0x64] == {}


def test_delete_port(client, manager, config):
    _post(client, "Network", conftest.network())
    _post(client, "Port", conftest.port())
    manager.apply_pending()
    response = client.request(
        "DELETE", f"{URL}/Port/{PORT_ID}", json=conftest.port(), headers=AUTH
    )
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert f"0x{GUID:016x}" not in _file(config)


def test_delete_unknown_waits_for_an_apply(client, manager, reloader):
    # networking-mlnx sends a DELETE again when its job failed, and takes 404
    # as done, while OpenSM may still have the GUID until an apply succeeds.
    for kind in ("Port", "Network"):
        response = client.delete(f"{URL}/{kind}/{PORT_ID}", headers=AUTH)
        assert _job(client, response)["Status"] == "Pending"
        manager.apply_pending()
        assert _job(client, response)["Status"] == "Completed"
    assert reloader.calls == 2


def test_delete_after_a_failed_job_is_applied(client, manager, config, reloader):
    from alternative_fabric_manager.drivers import ReloadError

    _post(client, "Network", conftest.network())
    _post(client, "Port", conftest.port())
    manager.apply_pending()
    config.apply_attempts = 1
    reloader.error = ReloadError("OpenSM is down")
    first = client.delete(f"{URL}/Port/{PORT_ID}", headers=AUTH)
    manager.apply_pending()
    assert _job(client, first)["Status"] == "Pending"
    _expire(manager)
    assert _job(client, first)["Status"] == "Failed"

    reloader.error = None
    again = client.delete(f"{URL}/Port/{PORT_ID}", headers=AUTH)
    manager.apply_pending()
    assert _job(client, again)["Status"] == "Completed"
    assert f"0x{conftest.GUID:016x}" not in _file(config)


def test_delete_network_removes_its_ports(client, manager, config):
    _post(client, "Network", conftest.network())
    _post(client, "Port", conftest.port())
    response = client.delete(f"{URL}/Network/{NETWORK_ID}", headers=AUTH)
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert NETWORK_ID not in _file(config)
    assert f"0x{GUID:016x}" not in _file(config)
    # Its ports went with it, so a port can't join it until it is sent again.
    assert (
        client.post(f"{URL}/Port", json=conftest.port(), headers=AUTH).status_code
        == 400
    )


def test_failed_reload_fails_jobs_at_their_deadline(client, manager, config, reloader):
    from alternative_fabric_manager.drivers import ReloadError

    config.apply_attempts = 2
    reloader.error = ReloadError("OpenSM is down")
    response = _post(client, "Network", conftest.network())
    manager.apply_pending()
    assert reloader.calls == 2
    # The next sync may still apply it in time.
    assert _job(client, response)["Status"] == "Pending"

    _expire(manager)
    job = _job(client, response)
    assert job["Status"] == "Failed"
    assert "Timed out" in job["Error"]


def test_jobs_past_their_deadline_fail_in_the_loop(client, manager, reloader):
    from alternative_fabric_manager.drivers import ReloadError

    reloader.error = ReloadError("OpenSM is down")
    response = _post(client, "Network", conftest.network())
    _expire(manager)
    manager.evaluate_jobs()
    with manager.sessionmaker() as session:
        assert session.query(db.Job).one().status == db.JobStatus.FAILED
    assert _job(client, response)["Status"] == "Failed"


def test_change_during_apply_waits_for_the_next(client, manager, reloader):
    first = _post(client, "Network", conftest.network())
    second = []
    reloader.during = lambda: (
        second.append(_post(client, "Port", conftest.port())) if not second else None
    )
    manager.apply_pending()

    assert _job(client, first)["Status"] == "Completed"
    assert _job(client, second[0])["Status"] == "Pending"
    manager.apply_pending()
    assert _job(client, second[0])["Status"] == "Completed"


def test_file_changes_on_every_apply(client, manager, config):
    # Otherwise OpenSM might not run a pkey sweep on SIGHUP.
    _post(client, "Network", conftest.network())
    manager.apply_pending()
    before = _file(config)
    client.put(f"{URL}/Network/{NETWORK_ID}", json=conftest.network(), headers=AUTH)
    manager.apply_pending()
    assert _file(config) != before


def test_first_sync_applies_then_settles(manager, reloader):
    # A new manager can't know what OpenSM has, so it applies once.
    manager.apply_pending()
    assert reloader.calls == 1
    manager.apply_pending()
    assert reloader.calls == 1


def test_running_jobs_are_applied_after_a_restart(tmp_path, config, reloader):
    from alternative_fabric_manager import manager as manager_mod

    config.database = str(tmp_path / "afm.sqlite")
    first = manager_mod.Manager(config, db.make_sessionmaker(config.database), reloader)
    job_id = first.put_network(NETWORK_ID, conftest.network())

    second = manager_mod.Manager(
        config, db.make_sessionmaker(config.database), reloader
    )
    second.apply_pending()
    assert second.get_job(job_id).status == db.JobStatus.COMPLETED


def test_unknown_job_is_not_found(client):
    assert client.get("/ufmRestV3/app/jobs/42", headers=AUTH).status_code == 404


def test_start_writes_the_state_before_opensm(manager, config, reloader):
    seen = []
    reloader.start = lambda: seen.append(_file(config))
    manager.put_network(NETWORK_ID, conftest.network())
    manager._start_opensm()
    assert f"net-{NETWORK_ID}=0x0064" in seen[0]


def test_stopped_opensm_is_started_again(manager, config, reloader):
    reloader.is_running = False
    manager._check_opensm()
    assert reloader.starts == 1
    reloader.starts = 0
    manager._check_opensm()
    assert reloader.starts == 0


def test_a_network_clashing_with_the_template_is_quarantined(
    client, manager, config, reloader, tmp_path
):
    from alternative_fabric_manager import partitions

    path = tmp_path / "bad.j2"
    path.write_text("{{ default_partition }}\nstorage=0x0064 : ALL_SWITCHES=full ;\n")
    manager.template = partitions.load_template(str(path))
    response = _post(client, "Network", conftest.network())
    manager.apply_pending()

    job = _job(client, response)
    assert job["Status"] == "Failed"
    assert "Quarantined" in job["Error"] and "0x0064" in job["Error"]
    # The rest is still applied.
    assert reloader.calls == 1
    assert f"net-{NETWORK_ID}" not in _file(config)


def test_an_unattributable_failure_keeps_the_last_file(
    client, manager, config, reloader
):
    _applied(client, manager)
    good = _file(config)
    calls = reloader.calls
    manager.template = type(
        "Broken", (), {"render": lambda self, context: "nonsense\n"}
    )()
    response = _post(
        client, "Network", conftest.network(network_id=OTHER, segmentation_id=200)
    )
    manager.apply_pending()

    job = _job(client, response)
    assert job["Status"] == "Failed"
    # OpenSM is made to sweep the last good partitions again, not the new ones.
    assert reloader.calls == calls + 1
    assert _file(config).split("# Apply ")[0] == good.split("# Apply ")[0]
    # And not again until the state changes
    manager.apply_pending()
    assert reloader.calls == calls + 1


def test_guid_with_the_top_bit_set(client, manager, config):
    # OpenStack's MAC addresses start with fa:16:3e, so their GUIDs do too.
    _post(client, "Network", conftest.network())
    response = _post(
        client, "Port", conftest.port(client_id="20:fa:16:3e:00:00:12:34:56")
    )
    manager.apply_pending()
    assert _job(client, response)["Status"] == "Completed"
    assert "0xfa163e0000123456=full" in _file(config)


def _flat_network():
    return conftest.network(
        **{"provider:network_type": "flat", "provider:segmentation_id": None}
    )


def test_flat_network_ports_join_the_flat_partition(client, manager, config):
    # networking-mlnx sends ports on flat networks, and their networks' PUT
    # and DELETE.
    _post(client, "Network", _flat_network())
    response = _post(client, "Port", conftest.port())
    update = client.put(
        f"{URL}/Network/{NETWORK_ID}", json=_flat_network(), headers=AUTH
    )
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert _job(client, update)["Status"] == "Completed"
    # Their own partition, with indx0, outside the VLAN range
    assert _members(config)[0x7FFE] == {GUID: "full"}
    assert f"net-{NETWORK_ID}" not in _file(config)


def test_flat_networks_dont_clash(client):
    _post(client, "Network", _flat_network())
    other = dict(_flat_network(), id="d13bba05-eb07-45ba-ace2-765706b2d701")
    assert _post(client, "Network", other).status_code == 202


def test_flat_ports_with_a_limited_default_partition(client, manager, config):
    # The only way ports on flat networks reach each other then
    config.default_partition = "Default=0x7fff, ipoib : ALL=limited, SELF=full ;"
    _post(client, "Network", _flat_network())
    response = _post(client, "Port", conftest.port())
    manager.apply_pending()
    assert _job(client, response)["Status"] == "Completed"


@pytest.mark.parametrize("network_type", ["vxlan", "geneve", "local"])
def test_network_without_a_partition_is_rejected(client, network_type):
    response = _post(
        client, "Network", conftest.network(**{"provider:network_type": network_type})
    )
    assert response.status_code == 400


def test_opensm_conf_is_written_with_shared_keys(manager, config, tmp_path):
    import os

    config.driver = "managed"
    config.opensm_conf = {"sm_priority": "15"}
    manager._start_opensm()
    with open(config.opensm_conf_file) as f:
        lines = f.read().splitlines()
    assert "sm_priority 15" in lines
    assert "m_key_protection_level 2" in lines
    m_key = next(line for line in lines if line.startswith("m_key "))
    assert os.stat(config.opensm_conf_file).st_mode & 0o777 == 0o600
    # The same keys after a restart, and for every OpenSM of the fabric
    assert manager.keys() == manager.keys()
    manager._start_opensm()
    with open(config.opensm_conf_file) as f:
        assert m_key in f.read().splitlines()


def test_opensm_conf_only_for_the_managed_driver(manager, config):
    manager._start_opensm()
    assert not pathlib.Path(config.opensm_conf_file).exists()


def test_a_guid_belongs_to_one_port(client, manager, config):
    # A port whose deletion never arrived, or two ports with one client ID,
    # would otherwise be in two networks' partitions.
    _post(client, "Network", conftest.network())
    _post(client, "Network", conftest.network(network_id=OTHER, segmentation_id=200))
    assert _post(client, "Port", conftest.port()).status_code == 202
    response = _post(
        client,
        "Port",
        conftest.port(port_id="e13bba05-eb07-45ba-ace2-765706b2d701", network_id=OTHER),
    )
    manager.apply_pending()
    assert _job(client, response)["Status"] == "Pending"
    assert _members(config)[0xC8] == {}
    # The same port moving is fine.
    response = client.put(
        f"{URL}/Port/{PORT_ID}", json=conftest.port(network_id=OTHER), headers=AUTH
    )
    assert response.status_code == 202


def test_reserved_pkeys_are_refused(config, reloader, tmp_path, monkeypatch):
    from fastapi import testclient

    from alternative_fabric_manager import api, db
    from alternative_fabric_manager import manager as manager_mod

    path = tmp_path / "partitions.j2"
    path.write_text(
        "{{ default_partition }}\nstorage=0x0064 : ALL_SWITCHES=full ;\n"
        "{% for n in networks %}{{ n.name }}={{ n.pkey_hex }} :"
        "{% for g in n.guids %} {{ g }}=full{% endfor %} ;\n{% endfor %}"
    )
    config.partitions_template = str(path)
    manager = manager_mod.Manager(config, db.make_sessionmaker(":memory:"), reloader)
    client = testclient.TestClient(api.create_app(config, manager))
    assert _post(client, "Network", conftest.network()).status_code == 400
    assert (
        _post(client, "Network", conftest.network(segmentation_id=101)).status_code
        == 202
    )


@pytest.mark.parametrize("object_id", ["net;x", "a:b", "x\ny", "", "a" * 65])
def test_ids_must_be_safe(client, object_id):
    body = conftest.network(network_id=object_id)
    assert _post(client, "Network", body).status_code == 400


def test_opensm_starts_with_the_last_good_file(manager, config, reloader):
    manager.put_network(NETWORK_ID, conftest.network())
    manager._start_opensm()
    good = _file(config)
    # Something that can no longer be rendered, for example a template
    # changed since
    manager.template = type(
        "Broken", (), {"render": lambda self, context: "nonsense\n"}
    )()
    manager._start_opensm()
    assert _file(config) == good


def test_first_start_needs_a_good_file(manager, config):
    manager.template = type(
        "Broken", (), {"render": lambda self, context: "nonsense\n"}
    )()
    from alternative_fabric_manager import partitions

    with pytest.raises(partitions.InvalidPartitions):
        manager._start_opensm()


def test_database_and_opensm_conf_are_private(tmp_path, config, reloader):
    import os

    from alternative_fabric_manager import db
    from alternative_fabric_manager import manager as manager_mod

    config.driver = "managed"
    database = str(tmp_path / "afm.sqlite")
    manager = manager_mod.Manager(config, db.make_sessionmaker(database), reloader)
    manager._start_opensm()
    assert os.stat(database).st_mode & 0o777 == 0o600
    assert os.stat(config.opensm_conf_file).st_mode & 0o777 == 0o600


def _applied(client, manager):
    _post(client, "Network", conftest.network())
    _post(client, "Port", conftest.port())
    manager.apply_pending()


def test_reconcile_does_nothing_when_all_matches(client, manager, reloader):
    _applied(client, manager)
    calls = reloader.calls
    manager.reconcile()
    assert reloader.calls == calls


def test_reconcile_restores_an_edited_file(client, manager, config, reloader):
    _applied(client, manager)
    path = pathlib.Path(config.partitions_file)
    path.write_text(path.read_text().replace("=full", "=limited"))
    calls = reloader.calls
    manager.reconcile()
    assert reloader.calls == calls + 1
    assert _members(config)[0x64] == {GUID: "full"}


def test_reconcile_restores_a_missing_file(client, manager, config, reloader):
    _applied(client, manager)
    pathlib.Path(config.partitions_file).unlink()
    manager.reconcile()
    assert _members(config)[0x64] == {GUID: "full"}


def test_reconcile_retries_a_failed_apply(client, manager, config, reloader):
    from alternative_fabric_manager.drivers import ReloadError

    config.apply_attempts = 1
    reloader.error = ReloadError("OpenSM is down")
    _applied(client, manager)
    reloader.error = None
    calls = reloader.calls
    manager.reconcile()
    assert reloader.calls == calls + 1
    calls = reloader.calls
    manager.reconcile()
    assert reloader.calls == calls


def test_reconcile_applies_a_state_changed_without_a_job(client, manager, config):
    from alternative_fabric_manager import db

    _applied(client, manager)
    # As another instance sharing the database would
    with manager.sessionmaker() as session:
        session.query(db.Port).delete()
        session.commit()
    manager.reconcile()
    assert _members(config)[0x64] == {}


def test_reconcile_skips_a_stopped_opensm(client, manager, config, reloader):
    _applied(client, manager)
    pathlib.Path(config.partitions_file).unlink()
    reloader.is_running = False
    manager.reconcile()
    assert not pathlib.Path(config.partitions_file).exists()


def test_retries_change_the_file(client, manager, config, reloader):
    # So that OpenSM runs a pkey sweep, not a heavy one, on each attempt
    from alternative_fabric_manager.drivers import ReloadError

    seen = []
    errors = [ReloadError("once"), None]

    def reload():
        seen.append(_file(config))
        error = errors.pop(0)
        if error:
            raise error

    reloader.reload = reload
    config.apply_attempts = 2
    _applied(client, manager)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert "# Apply " in seen[1]
