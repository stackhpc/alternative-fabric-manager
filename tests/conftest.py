import os

import pytest
from fastapi import testclient

from alternative_fabric_manager import api, db
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.drivers import SweepReport

TOKEN = "dGVzdDp0ZXN0"
AUTH = {"Authorization": f"Basic {TOKEN}"}
URL = "/ufmRestV3/cloudx"
NETWORK_ID = "c13bba05-eb07-45ba-ace2-765706b2d701"
PORT_ID = "72c56c48-e9b8-4dcf-b3a7-0813bb3bd839"
# mlnx_infiniband's client ID for MAC 12:34:56:78:21:b6, and its GUID
CLIENT_ID = "20:12:34:56:00:00:78:21:b6"
GUID = 0x12345600007821B6


class FakeReloader:
    """A driver whose OpenSM is always running"""

    def __init__(self):
        self.calls = 0
        self.error = None
        self.during = None
        self.starts = 0
        self.is_running = True
        self.stopped = False
        # What reload() reports, and what fabric() returns: None for no
        # fabric to check, a Fabric, or an exception to raise
        self.report = None
        self.fabric_result = None
        self.fabric_calls = []

    def start(self):
        self.starts += 1
        self.is_running = True

    def running(self):
        return self.is_running

    def stop(self):
        self.stopped = True

    def reload(self):
        self.calls += 1
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return self.report or SweepReport()

    def fabric(self, m_key=None, sa_key=None):
        self.fabric_calls.append((m_key, sa_key))
        if isinstance(self.fabric_result, Exception):
            raise self.fabric_result
        return self.fabric_result


# AFM_TEST_DATABASE_URL runs the tests against a server, such as
# mysql+pymysql://root:<password>@127.0.0.1:3399/afm_test: its database is
# dropped and created again for each test.
SERVER_URL = os.environ.get("AFM_TEST_DATABASE_URL")


def _fresh_server_database(url):
    import sqlalchemy as sa

    parsed = sa.engine.make_url(url)
    engine = sa.create_engine(parsed.set(database=""))
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{parsed.database}`")
        connection.exec_driver_sql(f"CREATE DATABASE `{parsed.database}`")
    engine.dispose()
    db.upgrade(url)
    return url


@pytest.fixture
def config(tmp_path):
    return config_mod.Config(
        token=TOKEN,
        driver="none",
        database=":memory:",
        connection=_fresh_server_database(SERVER_URL) if SERVER_URL else "",
        config_dir=str(tmp_path),
    )


@pytest.fixture
def reloader():
    return FakeReloader()


@pytest.fixture
def manager(config, reloader, monkeypatch):
    monkeypatch.setattr(manager_mod, "RETRY_DELAY", 0)
    return manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), reloader
    )


@pytest.fixture
def client(config, manager):
    return testclient.TestClient(api.create_app(config, manager))


def network(network_id=NETWORK_ID, segmentation_id=100, **kwargs):
    """A network as networking-mlnx sends it"""
    return dict(
        {
            "id": network_id,
            "name": "net1",
            "provider:network_type": "vlan",
            "provider:physical_network": "ibphysnet",
            "provider:segmentation_id": segmentation_id,
            "network_qos_policy": None,
        },
        **kwargs,
    )


def port(port_id=PORT_ID, network_id=NETWORK_ID, client_id=CLIENT_ID, **kwargs):
    """A port as networking-mlnx sends it"""
    opts = (
        [{"opt_name": "client-id", "opt_value": client_id, "ip_version": 4}]
        if client_id
        else []
    )
    return dict(
        {
            "id": port_id,
            "network_id": network_id,
            "binding:host_id": "compute-0",
            "binding:vnic_type": "direct",
            "mac_address": "12:34:56:78:21:b6",
            "device_owner": "compute:nova",
            "extra_dhcp_opts": opts,
            "network_qos_policy": None,
        },
        **kwargs,
    )
