import pathlib

import pytest
from fastapi import testclient

from alternative_fabric_manager import api, db
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


def _seed(manager):
    with manager.sessionmaker() as session:
        secret = session.get(db.Secret, "key_mgr_seed")
        return secret and secret.value


@pytest.mark.parametrize(
    "hardening, driver",
    [("none", "none"), ("none", "managed"), ("all", "docker"), ("all", "managed")],
)
def test_start_generates_the_seed(manager, config, reloader, hardening, driver):
    # Whatever the groups and driver, so that every backup holds the seed
    # before any group keys a port with it
    import re

    config.hardening = hardening
    config.driver = driver
    manager.start()
    manager.stop()
    seed = _seed(manager)
    assert re.fullmatch("0x[0-9a-f]{16}", seed) and int(seed, 16)
    manager.start()
    manager.stop()
    assert _seed(manager) == seed
    assert reloader.starts == 2


def _base(manager):
    with manager.sessionmaker() as session:
        secret = session.get(db.Secret, "m_key_base")
        return secret and secret.value


@pytest.mark.parametrize(
    "hardening, driver",
    [
        ("none", "none"),
        ("none", "managed"),
        ("m_key, sm_key", "managed"),
        ("all", "docker"),
        ("all", "managed"),
    ],
)
def test_start_generates_the_base(manager, config, reloader, caplog, hardening, driver):
    # Whatever the groups and driver, as the seed is, so that every backup
    # holds the per-port M_Key base a release before any port is keyed from it
    import logging
    import re

    caplog.set_level(logging.DEBUG)
    config.hardening = hardening
    config.driver = driver
    manager.start()
    manager.stop()
    base = _base(manager)
    assert re.fullmatch("0x[0-9a-f]{16}", base) and int(base, 16)
    assert base not in (manager.keys()["m_key"], manager.keys()["key_mgr_seed"])
    manager.start()
    manager.stop()
    assert _base(manager) == base
    assert reloader.starts == 2
    assert base not in caplog.text
    if driver == "managed":
        with open(config.opensm_conf_file) as f:
            content = f.read()
        assert base not in content and "m_key_base" not in content


def test_the_base_is_kept_from_the_next_start(manager, config):
    # A start that stored the base can't begin per-port keys; the next can.
    assert not manager.base_kept()
    manager.start()
    manager.stop()
    assert _base(manager)
    assert not manager.base_kept()
    manager.start()
    manager.stop()
    assert manager.base_kept()
    manager.start()
    manager.stop()
    assert manager.base_kept()


def test_keys_before_the_first_start_dont_store_the_base(manager, config):
    # As --show-keys calls it: only a start stores the base and the seed, so
    # the first start still stores it, and isn't one that kept it.
    keys = manager.keys()
    assert "m_key_base" not in keys and "key_mgr_seed" not in keys
    manager.start()
    manager.stop()
    assert not manager.base_kept()
    manager.start()
    manager.stop()
    assert manager.base_kept()


def test_a_lost_base_isnt_kept(manager, config):
    # A restored database without the base: the start that stores it again
    # records that it did, whatever an earlier start recorded.
    manager.start()
    manager.stop()
    manager.start()
    manager.stop()
    assert manager.base_kept()
    with manager.sessionmaker() as session:
        session.delete(session.get(db.Secret, "m_key_base"))
        session.commit()
    manager.start()
    manager.stop()
    assert not manager.base_kept()


def test_ha_starts_record_the_base_behind_the_leader_fence(manager, config):
    # A takeover's start records it, raising the fence to its term first
    # (specs/ha.md, The secured profile).
    from alternative_fabric_manager import manager as manager_mod

    config.ha = True
    manager.term = 3
    manager.start()
    manager.stop()
    manager.start()
    manager.stop()
    assert _base(manager)
    assert manager.base_kept()
    with manager.sessionmaker() as session:
        assert session.get(db.State, manager_mod.LEADER_FENCE).value == 3


def test_a_deposed_leaders_fenced_write_fails(manager, config):
    from alternative_fabric_manager import manager as manager_mod

    config.ha = True
    manager.term = 5
    with manager.fenced_session() as session:
        session.commit()
    manager.term = 4
    with (
        pytest.raises(manager_mod.StaleLeader),
        manager.fenced_session() as session,
    ):
        session.add(db.State(name="written", value=1))
        session.commit()
    with manager.sessionmaker() as session:
        assert session.get(db.State, "written") is None
        assert session.get(db.State, manager_mod.LEADER_FENCE).value == 5
    # An operator's command, outside any term, isn't fenced.
    manager.term = 0
    with manager.fenced_session() as session:
        session.add(db.State(name="written", value=1))
        session.commit()


def test_every_fenced_write_changes_the_fence_row(manager, config):
    # So that Galera certifies a deposed leader's transaction against the
    # successor's raise
    from alternative_fabric_manager import manager as manager_mod

    config.ha = True
    manager.term = 2
    nonces = set()
    for _ in range(3):
        with manager.fenced_session() as session:
            session.commit()
        with manager.sessionmaker() as session:
            nonces.add(session.get(db.State, manager_mod.LEADER_FENCE).text)
    assert len(nonces) == 3


def test_key_lease_period_reaches_opensm_conf(manager, config, caplog):
    config.driver = "managed"
    config.key_lease_period = 60
    with caplog.at_level("INFO"):
        manager.start()
        manager.stop()
    with open(config.opensm_conf_file) as f:
        assert "m_key_lease_period 60" in f.read().splitlines()
    assert "key leases written: m_key_lease_period 60 s" in caplog.text
    # The keys never appear in the log.
    assert not any(v in caplog.text for v in manager.keys().values())


@pytest.mark.parametrize(
    "driver, hardening, opensm_conf, expected",
    [
        # What's written, not what's asked for
        ("managed", "enforcement", {}, "key leases written: none"),
        ("docker", "all", {}, "key leases written: none"),
        (
            "managed",
            "all",
            {"m_key_lease_period": "0"},
            "key leases written: m_key_lease_period permanent",
        ),
    ],
)
def test_the_start_log_states_the_leases_written(
    manager, config, caplog, driver, hardening, opensm_conf, expected
):
    config.driver = driver
    config.hardening = hardening
    config.key_lease_period = 600
    config.opensm_conf = opensm_conf
    with caplog.at_level("INFO"):
        manager.start()
        manager.stop()
    assert expected in caplog.text


def test_a_lease_override_is_warned_about(manager, config, caplog):
    config.driver = "managed"
    config.key_lease_period = 60
    config.opensm_conf = {"m_key_lease_period": "0"}
    manager.start()
    manager.stop()
    with open(config.opensm_conf_file) as f:
        assert "m_key_lease_period 0" in f.read().splitlines()
    assert "wins over [opensm] key_lease_period" in caplog.text


@pytest.mark.parametrize("hardening", ["all", "m_key, sa_trust"])
def test_opensm_conf_has_no_seed(manager, config, hardening):
    from alternative_fabric_manager import opensm_conf

    config.driver = "managed"
    config.hardening = hardening
    manager.start()
    manager.stop()
    assert _seed(manager)
    with open(config.opensm_conf_file) as f:
        content = f.read()
    assert "key_mgr_seed" not in content
    assert "m_key_base" not in content
    assert _base(manager) not in content
    # As before the seed and the base were kept
    keys = {
        k: v
        for k, v in manager.keys().items()
        if k not in ("key_mgr_seed", "m_key_base")
    }
    assert content == opensm_conf.render(
        {}, hardening=config.hardening_groups, keys=keys
    )


def test_a_seed_override_on_a_fresh_database_is_stored(manager, config, reloader):
    # Ports may already be keyed with it, set by hand: a random seed would
    # strand them.
    config.driver = "managed"
    config.opensm_conf = {"key_mgr_seed": "0x1"}
    manager.start()
    manager.stop()
    assert _seed(manager) == "0x0000000000000001"
    assert reloader.starts == 1
    with open(config.opensm_conf_file) as f:
        assert "key_mgr_seed 0x0000000000000001" in f.read().splitlines()


def test_show_keys_stores_no_seed_for_an_override(manager, config):
    # --show-keys before the first start mustn't store a random seed that the
    # start would then refuse the override for: it stores no seed at all,
    # and the start stores the override.
    config.opensm_conf = {"key_mgr_seed": "18"}
    keys = manager.keys()
    assert "key_mgr_seed" not in keys
    manager.start()
    manager.stop()
    assert manager.keys()["key_mgr_seed"] == "0x0000000000000012"


def test_a_key_stored_meanwhile_is_read_back(manager, monkeypatch):
    # Another instance storing the keys between our read and our commit is a
    # conflict on the primary key; the stored keys win.
    from alternative_fabric_manager import opensm_conf

    real = opensm_conf.new_key
    raced = []

    def new_key():
        if not raced:
            raced.append(1)
            with manager.sessionmaker() as other:
                for name in opensm_conf.KEYS:
                    other.add(db.Secret(name=name, value="0x00000000000000aa"))
                other.commit()
        return real()

    monkeypatch.setattr(opensm_conf, "new_key", new_key)
    assert set(manager.keys().values()) == {"0x00000000000000aa"}


def test_a_changed_seed_is_refused(manager, config, reloader):
    from alternative_fabric_manager import config as config_mod
    from alternative_fabric_manager import opensm_conf

    config.driver = "managed"
    manager.start()
    manager.stop()
    seed = _seed(manager)
    pathlib.Path(config.opensm_conf_file).unlink()
    other = opensm_conf.new_key()
    config.opensm_conf = {"key_mgr_seed": other}
    with pytest.raises(config_mod.ConfigError, match="key_mgr_seed") as e:
        manager.start()
    assert seed not in str(e.value) and other not in str(e.value)
    assert reloader.starts == 1
    assert manager._thread is None
    assert not pathlib.Path(config.opensm_conf_file).exists()
    assert _seed(manager) == seed


def test_the_stored_seed_may_be_overridden_with(manager, config, reloader):
    # Whatever the spelling, opensm.conf gets the stored one, which OpenSM
    # can't read differently.
    config.driver = "managed"
    manager.start()
    manager.stop()
    seed = _seed(manager)
    for spelling in ("0X" + seed[2:].upper(), hex(int(seed, 16)), str(int(seed, 16))):
        config.opensm_conf = {"key_mgr_seed": spelling}
        manager.start()
        manager.stop()
        with open(config.opensm_conf_file) as f:
            assert f"key_mgr_seed {seed}" in f.read().splitlines()
    assert reloader.starts == 4


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


def test_healthz_fails_when_the_coordinator_says_so(config, manager):
    class Coordinator:
        role = "leader"
        frozen = False

        def healthy(self):
            return False, "Can't reach a Primary component of the database"

    client = testclient.TestClient(api.create_app(config, manager, Coordinator()))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "Primary" in response.text


def test_a_node_not_serving_answers_503(client, manager, monkeypatch):
    def not_serving(*_args):
        raise db.NotServing("Galera node is Joined, still catching up")

    monkeypatch.setattr(manager, "put_port", not_serving)
    response = client.post(f"{URL}/Port", json=conftest.port(), headers=AUTH)
    assert response.status_code == 503
