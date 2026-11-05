from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import opensm_conf

ALL = set(config_mod.HARDENING_GROUPS)


def test_render():
    content = opensm_conf.render({"sm_priority": "15", "log_flags": "0x07"})
    lines = content.splitlines()
    assert "partition_config_file /etc/opensm/partitions.conf" in lines
    assert "daemon FALSE" in lines
    assert "log_flags 0x07" in lines
    assert "sm_priority 15" in lines
    assert content.endswith("\n")


def test_render_without_overrides():
    assert "From [opensm_conf]" not in opensm_conf.render({})


def test_hardening():
    keys = {"m_key": "0x1234", "sm_key": "0x5678", "sa_key": "0x9abc"}
    lines = opensm_conf.render({}, hardening=ALL, keys=keys).splitlines()
    for line in (
        "m_key 0x1234",
        "sm_key 0x5678",
        "sa_key 0x9abc",
        "m_key_protection_level 2",
        "sa_enhanced_trust_model TRUE",
        "part_enforce both",
    ):
        assert line in lines


def test_overrides_win_over_hardening():
    content = opensm_conf.render(
        {"m_key_protection_level": "1", "sa_key": "0x1"},
        hardening=ALL,
        keys={"m_key": "0x1234", "sm_key": "0x5678", "sa_key": "0x9abc"},
    )
    lines = content.splitlines()
    assert "m_key_protection_level 1" in lines
    assert "m_key_protection_level 2" not in lines
    assert "sa_key 0x1" in lines
    assert "sa_key 0x9abc" not in lines
    assert "m_key 0x1234" in lines


def test_no_hardening():
    content = opensm_conf.render({}, hardening=set(), keys={"m_key": "0x1234"})
    assert "m_key" not in content
    assert "Hardening" not in content


def test_one_hardening_group():
    # An operator enables only M_Key protection with one option.
    lines = opensm_conf.render(
        {}, hardening={"m_key"}, keys={"m_key": "0x1234", "sm_key": "0x5678"}
    ).splitlines()
    assert "m_key 0x1234" in lines
    assert "m_key_protection_level 2" in lines
    assert not any(line.startswith(("sm_key", "sa_", "part_enforce")) for line in lines)


def test_new_key():
    keys = {opensm_conf.new_key() for _ in range(20)}
    assert len(keys) == 20
    for key in keys:
        assert len(key) == 18 and int(key, 16) != 0
