from alternative_fabric_manager import opensm_conf


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
