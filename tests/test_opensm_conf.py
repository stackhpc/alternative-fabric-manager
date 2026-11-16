from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import opensm_conf

ALL = set(config_mod.ALL_HARDENING_GROUPS)


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


KEYS = {"m_key": "0x1234", "sm_key": "0x5678", "sa_key": "0x9abc"}


def test_keys_are_permanent_by_default():
    lines = opensm_conf.render({}, hardening=ALL, keys=KEYS).splitlines()
    assert "m_key_lease_period 0" in lines


def test_one_option_gives_every_key_a_lease(monkeypatch):
    # Including groups added later, such as the class keys
    monkeypatch.setitem(
        opensm_conf.HARDENING,
        "class_keys",
        {"vs_key_lease_period": "0", "cc_key_lease_period": "0", "vs_key_enable": "2"},
    )
    lines = opensm_conf.render(
        {}, hardening=ALL | {"class_keys"}, keys=KEYS, key_lease_period=60
    ).splitlines()
    leases = [line for line in lines if "lease_period" in line]
    assert sorted(leases) == [
        "cc_key_lease_period 60",
        "m_key_lease_period 60",
        "vs_key_lease_period 60",
    ]
    assert "vs_key_enable 2" in lines


def test_an_override_wins_over_the_lease_option():
    lines = opensm_conf.render(
        {"m_key_lease_period": "120"}, hardening=ALL, keys=KEYS, key_lease_period=60
    ).splitlines()
    assert "m_key_lease_period 120" in lines
    assert "m_key_lease_period 60" not in lines


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


def test_keys_use_64_bits():
    keys = [int(opensm_conf.new_key(), 16) for _ in range(200)]
    assert any(k >> 63 for k in keys)
    assert any(not k & 1 for k in keys)


def test_manager_sets_log_flags():
    assert "log_flags 0x07" in opensm_conf.render({}).splitlines()
    assert (
        "log_flags 0x07" not in opensm_conf.render({"log_flags": "0xff"}).splitlines()
    )


def test_no_group_writes_the_seed():
    # The seed is kept a release before any group uses it (see
    # specs/secured-profile.md, The seed), and changes no opensm.conf.
    assert "key_mgr_seed" in opensm_conf.KEYS
    keys = {name: opensm_conf.new_key() for name in opensm_conf.KEYS}
    without_seed = {k: v for k, v in keys.items() if k != "key_mgr_seed"}
    for groups in (ALL, {"m_key", "sa_trust"}, set()):
        content = opensm_conf.render({}, hardening=groups, keys=keys)
        assert "key_mgr_seed" not in content
        assert content == opensm_conf.render({}, hardening=groups, keys=without_seed)


def test_the_base_is_rendered_only_with_per_port_keys_on():
    # Naming m_key_per_port only permits per-port keys; until their target
    # is on, no group writes the base, whichever groups are chosen.
    import itertools

    keys = dict(KEYS, key_mgr_seed="0x00000000000000ee", m_key_base="0xbbbbbbbb")
    every = set(config_mod.HARDENING_GROUPS)
    for size in range(len(every) + 1):
        for groups in itertools.combinations(sorted(every), size):
            for port_keys in (False, True):
                content = opensm_conf.render(
                    {}, hardening=set(groups), keys=keys, port_keys=port_keys
                )
                assert "m_key_base" not in content
                written = port_keys and "m_key_per_port" in groups
                assert ("0xbbbbbbbb" in content) is written
                if written:
                    # Only ever as the m_key line
                    assert [
                        line for line in content.splitlines() if "0xbbbbbbbb" in line
                    ] == ["m_key 0xbbbbbbbb"]


def test_the_base_is_a_key():
    assert "m_key_base" in opensm_conf.KEYS


PER_PORT_KEYS = dict(KEYS, m_key_base="0x00000000000000bb")


def test_m_key_per_port_named_but_off_renders_as_without_it():
    # The target, not the group, decides whether OpenSM gets per-port keys
    # (specs/secured-profile.md, The design: the group permits them).
    without = opensm_conf.render({}, hardening=ALL, keys=PER_PORT_KEYS)
    for port_keys in ({}, {"port_keys": False}):
        named = opensm_conf.render(
            {}, hardening=ALL | {"m_key_per_port"}, keys=PER_PORT_KEYS, **port_keys
        )
        assert named == without
    assert "m_key_per_port" not in without
    assert "m_key_lookup" not in without
    assert "m_key 0x1234" in without.splitlines()


def test_m_key_per_port_on_writes_its_options_and_the_base():
    content = opensm_conf.render(
        {}, hardening=ALL | {"m_key_per_port"}, keys=PER_PORT_KEYS, port_keys=True
    )
    lines = content.splitlines()
    for line in (
        "m_key_per_port TRUE",
        "m_key_lookup TRUE",
        "m_key_protection_level 2",
        # OpenSM derives each port's key from its m_key line: the base
        "m_key 0x00000000000000bb",
    ):
        assert line in lines
    # The stored m_key, which tenants may have read, appears nowhere: ports
    # still holding it are reached through guid2mkey entries instead.
    assert "0x1234" not in content
    assert lines.count("m_key_protection_level 2") == 1


def test_port_keys_without_the_group_writes_nothing_of_it():
    content = opensm_conf.render({}, hardening=ALL, keys=PER_PORT_KEYS, port_keys=True)
    assert content == opensm_conf.render({}, hardening=ALL, keys=PER_PORT_KEYS)


def test_hardened_options_with_per_port_keys():
    options = opensm_conf.hardened_options(
        {"m_key", "m_key_per_port"}, PER_PORT_KEYS, port_keys=True
    )
    assert options["m_key"] == "0x00000000000000bb"
    assert options["m_key_per_port"] == "TRUE"
    assert "m_key_per_port" not in opensm_conf.hardened_options(
        {"m_key", "m_key_per_port"}, PER_PORT_KEYS
    )
