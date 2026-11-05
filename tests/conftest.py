import pytest
from fastapi import testclient

from alternative_fabric_manager import api, db
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod

TOKEN = "dGVzdDp0ZXN0"
AUTH = {"Authorization": f"Basic {TOKEN}"}
URL = "/ufmRestV3/cloudx"
NETWORK_ID = "c13bba05-eb07-45ba-ace2-765706b2d701"
PORT_ID = "72c56c48-e9b8-4dcf-b3a7-0813bb3bd839"
# mlnx_infiniband's client ID for MAC 12:34:56:78:21:b6, and its GUID
CLIENT_ID = "20:12:34:56:00:00:78:21:b6"
GUID = 0x12345600007821B6


class FakeReloader:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.during = None

    def reload(self):
        self.calls += 1
        if self.during:
            self.during()
        if self.error:
            raise self.error


@pytest.fixture
def config(tmp_path):
    return config_mod.Config(
        token=TOKEN,
        reload="none",
        database=":memory:",
        partitions_file=str(tmp_path / "partitions.conf"),
    )


@pytest.fixture
def reloader():
    return FakeReloader()


@pytest.fixture
def manager(config, reloader, monkeypatch):
    monkeypatch.setattr(manager_mod, "RETRY_DELAY", 0)
    return manager_mod.Manager(config, db.make_sessionmaker(config.database), reloader)


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
