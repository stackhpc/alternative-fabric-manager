import pathlib

import pytest

from alternative_fabric_manager import db
from tests import conftest
from tests.conftest import AUTH, GUID, NETWORK_ID, PORT_ID, URL


def _post(client, kind, body):
    return client.post(f"{URL}/{kind}", json=body, headers=AUTH)


def _job(client, response):
    assert response.status_code == 202, response.text
    path = response.json()
    assert path.startswith("app/jobs/")
    return client.get("/ufmRestV3/" + path, headers=AUTH).json()


def _file(config):
    return pathlib.Path(config.partitions_file).read_text()


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
    assert f"net-{NETWORK_ID}=0x0064, ipoib, rate=3, mtu=4, sl=0 :  ;" in _file(config)


def test_port_is_a_full_member(client, manager, config):
    _post(client, "Network", conftest.network())
    response = _post(client, "Port", conftest.port())
    manager.apply_pending()

    assert _job(client, response)["Status"] == "Completed"
    assert f"0x{GUID:016x}=full ;" in _file(config)


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


def test_partition_key_can_only_be_used_once(client):
    _post(client, "Network", conftest.network())
    response = _post(
        client,
        "Network",
        conftest.network(network_id="d13bba05-eb07-45ba-ace2-765706b2d701"),
    )
    assert response.status_code == 400


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
    lines = _file(config).splitlines()
    assert f"0x{GUID:016x}=full" in next(line for line in lines if "=0x00c8" in line)
    assert f"0x{GUID:016x}" not in next(line for line in lines if "=0x0064" in line)


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


def test_failed_reload_fails_jobs(client, manager, config, reloader):
    from alternative_fabric_manager.drivers import ReloadError

    config.apply_attempts = 2
    reloader.error = ReloadError("OpenSM is down")
    response = _post(client, "Network", conftest.network())
    manager.apply_pending()

    job = _job(client, response)
    assert job["Status"] == "Failed"
    assert job["Error"] == "OpenSM is down"
    assert reloader.calls == 2


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


def test_nothing_pending_does_nothing(manager, reloader):
    manager.apply_pending()
    assert reloader.calls == 0


def test_running_jobs_are_applied_after_a_restart(tmp_path, config, reloader):
    from alternative_fabric_manager import manager as manager_mod

    config.database = str(tmp_path / "afm.sqlite")
    first = manager_mod.Manager(config, db.make_sessionmaker(config.database), reloader)
    job_id = first.put_network(NETWORK_ID, conftest.network())
    # Stopped while applying
    first._snapshot()

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
    manager._write_current()
    manager.driver.start()
    assert f"net-{NETWORK_ID}=0x0064" in seen[0]


def test_stopped_opensm_is_started_again(manager, config, reloader):
    reloader.is_running = False
    manager._check_opensm()
    assert reloader.starts == 1
    reloader.starts = 0
    manager._check_opensm()
    assert reloader.starts == 0


def test_invalid_template_fails_jobs_without_writing(
    client, manager, config, reloader, tmp_path
):
    from alternative_fabric_manager import partitions

    path = tmp_path / "bad.j2"
    path.write_text("{{ default_partition }}\nstorage=0x0064 : ALL=full ;\n")
    manager.template = partitions.load_template(str(path))
    response = _post(client, "Network", conftest.network())
    manager.apply_pending()

    job = _job(client, response)
    assert job["Status"] == "Failed"
    assert "0x0064" in job["Error"]
    assert reloader.calls == 0
    assert not pathlib.Path(config.partitions_file).exists()


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


def test_flat_network_ports_join_the_default_partition(client, manager, config):
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
    assert f"Default=0x7fff : 0x{GUID:016x}=full ;" in _file(config)
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
