"""Where a check's own scan places each port: ibnetdiscover's output, the
prober's links, and OpenSM's virtualization dump (specs/ha.md, Where a port
is). The samples in tests/data are from OpenSM 5.17.2, a SwitchIB and
ConnectX-5s, with one VM on a virtual function."""

import io
import pathlib
import tarfile
import types

from alternative_fabric_manager import port_keys, prober, scan
from alternative_fabric_manager.drivers import docker as docker_driver

DATA = pathlib.Path(__file__).parent / "data"
SWITCH = "ec0d9a0300b12560"
HOST = 0x040973FFFFC75168
VF = 0xFA163E000062A357


def _sample(name):
    return (DATA / name).read_text()


def test_ibnetdiscover_places_every_adapter_on_its_switch_port():
    placed = scan.parse_ibnetdiscover(_sample("ibnetdiscover.txt"))
    # The parenthesised GUIDs lose their leading zeros, and are read as
    # numbers all the same.
    assert placed[HOST] == ["port", SWITCH, 8]
    assert placed[0x040973FFFFC75124] == ["port", SWITCH, 6]
    assert placed[0xB88303FFFF6B9208] == ["port", SWITCH, 1]
    # The switch's aggregation node is an adapter on its port 37, and trips
    # nothing.
    assert placed[0xEC0D9A0300B12568] == ["port", SWITCH, 37]
    assert len(placed) == 7
    # The switch itself isn't an adapter port.
    assert int(SWITCH, 16) not in placed


def test_a_port_listed_at_two_places_isnt_placed():
    text = _sample("ibnetdiscover.txt").replace(
        '[1](40973ffffc75168) \t"S-ec0d9a0300b12560"[8]',
        '[1](40973ffffc75168) \t"S-ec0d9a0300b12560"[9]',
    )
    placed = scan.parse_ibnetdiscover(text)
    assert HOST not in placed
    assert 0x040973FFFFC75124 in placed


def test_one_end_of_a_link_is_enough():
    switch_only = "\n".join(
        line
        for line in _sample("ibnetdiscover.txt").splitlines()
        if not line.startswith("[1](")
    )
    assert scan.parse_ibnetdiscover(switch_only)[HOST] == ["port", SWITCH, 8]


def test_text_that_doesnt_parse_places_nothing():
    assert scan.parse_ibnetdiscover("ibnetdiscover: iberror: failed\n") == {}
    assert scan.parse_virtualization_dump("garbage, more\n") == []


def test_the_dump_lists_vports_by_physical_port_index_and_state():
    entries = scan.parse_virtualization_dump(_sample("opensm-virtualization.dump"))
    assert (VF, HOST, 7, "ACTIVE") in entries
    # Index 0 is the physical port itself.
    assert (HOST, HOST, 0, "ACTIVE") in entries
    assert len(entries) == 9


def test_the_dump_places_an_active_vf_on_its_physical_ports_switch_port():
    physical = scan.parse_ibnetdiscover(_sample("ibnetdiscover.txt"))
    entries = scan.parse_virtualization_dump(_sample("opensm-virtualization.dump"))
    placed = scan.place(physical, entries)
    assert placed[VF] == ["vport", f"{HOST:016x}", 7, "ACTIVE", "port", SWITCH, 8]
    # A physical port listed at index 0 is placed by the scan.
    assert placed[HOST] == ["port", SWITCH, 8]


def _dump_with(*rows):
    head, _, tail = _sample("opensm-virtualization.dump").partition("END_VPORTS")
    return head + "".join(row + "\n" for row in rows) + "END_VPORTS" + tail


def test_a_duplicate_dump_entry_isnt_placed():
    physical = scan.parse_ibnetdiscover(_sample("ibnetdiscover.txt"))
    dump = _dump_with(
        "0x040973ffffc75124,     3, 0xfa163e000062a357, 0xfa163e000062a357,   1, "
        "ACTIVE, 1,     0,   186,   186"
    )
    placed = scan.place(physical, scan.parse_virtualization_dump(dump))
    assert VF not in placed


def test_a_vport_that_isnt_active_or_on_a_placed_port_isnt_placed():
    physical = scan.parse_ibnetdiscover(_sample("ibnetdiscover.txt"))
    down = _sample("opensm-virtualization.dump").replace(
        "1, ACTIVE, 1,     0,   185", "1, DOWN, 1,     0,   185"
    )
    assert VF not in scan.place(physical, scan.parse_virtualization_dump(down))
    # Its physical port is behind a missing switch.
    del physical[HOST]
    entries = scan.parse_virtualization_dump(_sample("opensm-virtualization.dump"))
    assert VF not in scan.place(physical, entries)


def test_a_physical_port_listed_as_anothers_vport_isnt_placed():
    physical = scan.parse_ibnetdiscover(_sample("ibnetdiscover.txt"))
    dump = _dump_with(
        "0x040973ffffc75124,     2, 0x040973ffffc75168, 0x040973ffffc75168,   1, "
        "ACTIVE, 1,     0,   187,   187"
    )
    assert HOST not in scan.place(physical, scan.parse_virtualization_dump(dump))


def test_a_dump_without_the_columns_it_needs_places_no_vport():
    dump = _sample("opensm-virtualization.dump").replace("VPortGUID", "Renamed")
    assert scan.parse_virtualization_dump(dump) == []


def test_prober_mode_places_reached_adapters_by_the_recorded_links():
    switch, adapter, far = 0x51, 0xA1, 0xA2
    ports = {
        switch: port_keys.PortState(switch, switch=True),
        adapter: port_keys.PortState(adapter, switch=False),
        far: port_keys.PortState(far, switch=False),
    }
    links = {}
    for local, peer in (((switch, 3), (adapter, 1)), ((switch, 4), (far, 1))):
        links[local] = port_keys.Link(*local, *peer)
        links[peer] = port_keys.Link(*peer, *local)
    result = prober.VerifyResult(
        ports=ports, switches={switch}, answered={switch, adapter}
    )
    placed = scan.from_verify([result], ports, links)
    assert placed == {adapter: ["port", f"{switch:016x}", 3]}
    # A switch the verify didn't find places nothing.
    missing = prober.VerifyResult(ports=ports, answered={adapter})
    assert scan.from_verify([missing], ports, links) == {}


# The driver


class ScanContainer:
    status = "running"
    id = "c1"

    def __init__(self, dump=None, log=""):
        self.commands = []
        self.dump = dump
        self.log = log

    def exec_run(self, command):
        self.commands.append(command)
        return 0, _sample("ibnetdiscover.txt").encode()

    def get_archive(self, path):
        assert path == scan.VIRTUALIZATION_DUMP
        if self.dump is None:
            raise docker_driver.docker.errors.NotFound(path)
        body = self.dump.encode()
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tar:
            info = tarfile.TarInfo("opensm-virtualization.dump")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        return iter([data.getvalue()]), {}

    def logs(self, stdout, stderr, since=None, timestamps=False):
        return self.log.encode()


def _driver(container):
    client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    return docker_driver.DockerReloader("opensm", 30, client=client)


def test_the_scan_is_ibnetdiscover_with_the_m_key_as_ibswitches_has_it():
    container = ScanContainer()
    placed = _driver(container).scan(m_key="0x11")
    assert placed[HOST] == ["port", SWITCH, 8]
    assert container.commands == [["ibnetdiscover", "-y", "0x11"]]
    _driver(container).scan()
    assert container.commands[-1] == ["ibnetdiscover"]


def test_the_dump_is_read_from_opensms_container():
    text = _sample("opensm-virtualization.dump")
    assert _driver(ScanContainer(dump=text)).virtualization_dump() == text
    # None written, as without virtualization
    assert _driver(ScanContainer()).virtualization_dump() == ""


def test_sweep_starts_are_counted_from_the_log():
    container = ScanContainer(
        log=(
            "2026-11-20T10:00:00.000000001Z OpenSM: INITIATING HEAVY SWEEP\n"
            "2026-11-20T10:00:01.000000001Z OpenSM: SUBNET UP\n"
        )
    )
    driver = _driver(container)
    assert driver.sweep_starts() == ("c1", 1)
    # Lines already read aren't counted again.
    assert driver.sweep_starts() == ("c1", 1)
    container.log += "2026-11-20T10:00:02.000000001Z OpenSM: INITIATING PKEY SWEEP\n"
    assert driver.sweep_starts() == ("c1", 2)
    # Another container starts again from its own log.
    container.id = "c2"
    assert driver.sweep_starts() == ("c2", 2)
