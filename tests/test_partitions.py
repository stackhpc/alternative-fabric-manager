import types

import pytest

from alternative_fabric_manager import partitions

DEFAULT = "Default=0x7fff, ipoib : ALL=full, SELF=full ;"  # shared
GUID = 0x12345600007821B6
OTHER = 0x0409_73FF_FFC7_5168


def _network(pkey=0x64, network_id="net1"):
    return types.SimpleNamespace(id=network_id, pkey=pkey)


def _render(config, networks, template=None, generation=7):
    return partitions.render(
        config, networks, generation, template or partitions.load_template()
    )


def test_default_template(config):
    content = _render(config, [(_network(), [GUID, GUID])])
    assert content.splitlines()[1:] == [
        "# Generation 7",
        config.default_partition,
        f"net-net1=0x0064, indx0, ipoib, rate=3, mtu=4, sl=0 : 0x{GUID:016x}=full ;",
    ]
    partitions.validate(content, {0x64: {GUID}})


def test_default_template_splits_large_partitions(config):
    # OpenSM reads a definition and its members from one line, into a fixed
    # buffer, and merges definitions of the same name and pkey.
    guids = range(1, 1001)
    content = _render(config, [(_network(), guids)])
    lines = [line for line in content.splitlines() if line.startswith("net-net1")]
    assert len(lines) == 50
    assert lines[0].startswith("net-net1=0x0064, indx0, ipoib")
    assert lines[1].startswith("net-net1=0x0064 : ")
    assert max(len(line) for line in content.splitlines()) < partitions.MAX_LINE
    partitions.validate(content, {0x64: set(guids)})


def test_long_lines_are_refused():
    members = ", ".join(f"0x{guid:016x}=full" for guid in range(1, 100))
    with pytest.raises(partitions.InvalidPartitions, match="longer"):
        partitions.validate(f"{DEFAULT}\nnet=0x0064 : {members} ;", {0x64: set()})


def test_operator_template(config, tmp_path):
    path = tmp_path / "partitions.j2"
    path.write_text(
        "{{ default_partition }}\n"
        "storage=0x0200 : ALL_SWITCHES=full ;\n"
        "{% for n in networks %}"
        "{{ n.name }}={{ n.pkey_hex }}, ipoib, mtu=5 : "
        "{{ n.guids | map('string') | join('=full, ') }}=full ;\n"
        "{% endfor %}"
    )
    content = _render(
        config, [(_network(), [GUID])], partitions.load_template(str(path))
    )
    assert "storage=0x0200" in content
    assert "mtu=5" in content
    partitions.validate(content, {0x64: {GUID}})


def test_header_is_always_written(config, tmp_path):
    # The generation makes the file change, so that OpenSM sweeps on SIGHUP.
    path = tmp_path / "partitions.j2"
    path.write_text("{{ default_partition }}")
    content = _render(config, [], partitions.load_template(str(path)))
    assert "# Generation 7" in content
    assert content.endswith(";\n")


def test_undefined_variable_fails(config, tmp_path):
    path = tmp_path / "partitions.j2"
    path.write_text("{{ default_partition }}\n{{ nonsense }}\n")
    with pytest.raises(partitions.InvalidPartitions, match="template"):
        _render(config, [], partitions.load_template(str(path)))


def test_template_is_sandboxed(config, tmp_path):
    path = tmp_path / "partitions.j2"
    path.write_text("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises(partitions.InvalidPartitions):
        _render(config, [], partitions.load_template(str(path)))


@pytest.mark.parametrize(
    "content",
    [
        # No default partition
        f"net=0x0064 : 0x{GUID:x}=full ;",
        # A network's port missing
        f"{DEFAULT}\nnet=0x0064 : ;",
        # A network's partition missing
        DEFAULT,
        # Limited, both, or no membership for a network's port
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=limited ;",
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=both ;",
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x} ;",
        # Another member, or a keyword, in a network's partition
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full, 0x{OTHER:x}=full ;",
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full, ALL_VCAS=full ;",
        # Added by a second definition of the same pkey
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full ;\nmore=0x8064 : 0x{OTHER:x} ;",
        # A static partition with a network's port
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full ;\nstorage=0x0200 : 0x{GUID:x} ;",
        # A partition without a pkey, which OpenSM would choose
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full ;\nstorage : ALL ;",
        # Not a member
        f"{DEFAULT}\nnet=0x0064 : 0x{GUID:x}=full, nonsense ;",
    ],
)
def test_invalid(content):
    # With ALL=full allowed in the default partition, so that each case fails
    # for its own reason
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(content, {0x64: {GUID}}, shared_default=True)


def test_valid_variants():
    # Comments, defmember, a decimal pkey, multicast groups and keywords in a
    # static partition
    content = (
        "Default=0x7fff : ALL=limited, SELF=full ;  # the default\n"
        f"net=100, ipoib, defmember=full : 0x{GUID:x} ;\n"
        "storage=0x0200, ipoib :\n"
        "    mgid=ff12:401b::0707,sl=1\n"
        "    ALL_SWITCHES=full, ALL_CAS=limited ;\n"
    )
    partitions.validate(content, {0x64: {GUID}})


LIMITED = "Default=0x7fff : ALL=limited, SELF=full ;"


def test_flat_ports_in_their_own_partition():
    content = f"{LIMITED}\nflat=0x7ffe, indx0 : 0x{GUID:x}=full ;"
    partitions.validate(content, {}, {GUID})
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(LIMITED, {}, {GUID})
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(f"{content}\nflat=0x7ffe : 0x{OTHER:x}=full ;", {}, {GUID})


def test_flat_ports_in_the_default_partition():
    # flat_pkey = 0x7fff
    with pytest.raises(partitions.InvalidPartitions, match="flat"):
        partitions.validate(LIMITED, {}, {GUID}, flat_pkey=0x7FFF)
    partitions.validate(
        f"{LIMITED}\nDefault=0x7fff : 0x{GUID:x}=full ;",
        {},
        {GUID},
        flat_pkey=0x7FFF,
    )
    # ALL=full covers them, where it is allowed.
    partitions.validate(DEFAULT, {}, {GUID}, flat_pkey=0x7FFF, shared_default=True)


def test_flat_ports_cant_be_in_another_partition():
    with pytest.raises(partitions.InvalidPartitions, match="isn't a network's"):
        partitions.validate(
            f"{LIMITED}\nflat=0x7ffe : 0x{GUID:x}=full ;\n"
            f"storage=0x0200 : 0x{GUID:x}=full ;",
            {},
            {GUID},
        )


def test_a_guid_in_two_networks_is_refused():
    content = (
        f"{LIMITED}\nnet=0x0064 : 0x{GUID:x}=full ;\nnet2=0x0065 : 0x{GUID:x}=full ;"
    )
    with pytest.raises(partitions.InvalidPartitions, match="partitions"):
        partitions.validate(content, {0x64: {GUID}, 0x65: {GUID}})
    content = (
        f"{LIMITED}\nnet=0x0064 : 0x{GUID:x}=full ;\nflat=0x7ffe : 0x{GUID:x}=full ;"
    )
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(content, {0x64: {GUID}}, {GUID})


@pytest.mark.parametrize(
    "partition",
    [
        "storage=0x0200 : ALL=full ;",
        "storage=0x0200 : ALL_CAS=both ;",
        "storage=0x0200, defmember=full : ALL_VCAS ;",
    ],
)
def test_tenant_keywords_as_full_members_are_refused(partition):
    with pytest.raises(partitions.InvalidPartitions, match="every tenant"):
        partitions.validate(f"{LIMITED}\n{partition}", {})


def test_shared_default_partition_needs_its_option():
    with pytest.raises(partitions.InvalidPartitions, match="every tenant"):
        partitions.validate(DEFAULT, {})
    partitions.validate(DEFAULT, {}, shared_default=True)


@pytest.mark.parametrize("member", ["0x_1", "\u0661", "1e5", "0x" + "1" * 17])
def test_strict_guids(member):
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(f"{LIMITED}\nstorage=0x0200 : {member} ;", {})


def test_default_template_with_flat_ports(config):
    flat = types.SimpleNamespace(id="flat1", pkey=None)
    content = _render(config, [(flat, [GUID]), (_network(), [OTHER])])
    assert "flat=0x7ffe, indx0, ipoib" in content
    assert "net-flat1" not in content
    partitions.validate(content, {0x64: {OTHER}}, {GUID})


def test_default_template_with_flat_ports_in_the_default(config):
    config.flat_pkey = 0x7FFF
    flat = types.SimpleNamespace(id="flat1", pkey=None)
    content = _render(config, [(flat, [GUID])])
    assert "flat=" not in content
    partitions.validate(content, {}, {GUID}, flat_pkey=0x7FFF)
