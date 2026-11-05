import types

import pytest

from alternative_fabric_manager import partitions

DEFAULT = "Default=0x7fff, ipoib : ALL=full, SELF=full ;"
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
        f"net-net1=0x0064, ipoib, rate=3, mtu=4, sl=0 : 0x{GUID:016x}=full ;",
    ]
    partitions.validate(content, {0x64: {GUID}})


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
    with pytest.raises(partitions.InvalidPartitions):
        partitions.validate(content, {0x64: {GUID}})


def test_valid_variants():
    # Comments, defmember, a decimal pkey, multicast groups and keywords in a
    # static partition
    content = (
        f"{DEFAULT}  # the default\n"
        f"net=100, ipoib, defmember=full : 0x{GUID:x} ;\n"
        "storage=0x0200, ipoib :\n"
        "    mgid=ff12:401b::0707,sl=1\n"
        "    ALL_SWITCHES=full ;\n"
    )
    partitions.validate(content, {0x64: {GUID}})
