import pytest

from alternative_fabric_manager import config as config_mod


def _load(tmp_path, text):
    path = tmp_path / "afm.conf"
    path.write_text(text)
    return config_mod.Config.load(str(path))


def test_load(tmp_path):
    config = _load(
        tmp_path,
        """
[api]
port = 9000
token = secret

[opensm]
driver = none
ipoib_rate = 7
default_partition = Default=0x7fff : ALL=limited, SELF=full ;
""",
    )
    assert config.port == 9000
    assert config.token == "secret"
    assert config.ipoib_rate == 7
    assert config.default_partition.startswith("Default=0x7fff : ALL=limited")
    assert config.domain == "cloudx"


@pytest.mark.parametrize(
    "text",
    [
        "[api]\n",
        "[api]\ntoken = x\nport = eighty\n",
        "[api]\ntoken = x\n[opensm]\ndriver = restart\n",
        "[api]\ntoken = x\n[opensm]\ndefault_partition = Default=0x7fff\n",
    ],
)
def test_invalid(tmp_path, text):
    with pytest.raises(config_mod.ConfigError):
        _load(tmp_path, text)


def test_missing_file(tmp_path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.Config.load(str(tmp_path / "missing.conf"))


@pytest.mark.parametrize(
    "value, groups",
    [
        (None, set(config_mod.HARDENING_GROUPS)),
        ("all", set(config_mod.HARDENING_GROUPS)),
        ("true", set(config_mod.HARDENING_GROUPS)),
        ("none", set()),
        ("false", set()),
        ("m_key", {"m_key"}),
        ("m_key, Enforcement", {"m_key", "enforcement"}),
    ],
)
def test_hardening_option(tmp_path, value, groups):
    text = "[api]\ntoken = x\n"
    if value is not None:
        text += f"[opensm]\nhardening = {value}\n"
    assert _load(tmp_path, text).hardening_groups == groups


def test_unknown_hardening_group(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="mkey"):
        _load(tmp_path, "[api]\ntoken = x\n[opensm]\nhardening = mkey\n")


def test_managed_opensm_options_cant_be_overridden(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="partition_config_file"):
        _load(tmp_path, "[api]\ntoken = x\n[opensm_conf]\npartition_config_file = /x\n")


def test_show_keys(tmp_path, capsys):
    from alternative_fabric_manager import cli

    path = tmp_path / "afm.conf"
    path.write_text(
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\ndriver = none\nconfig_dir = {tmp_path}\n"
    )
    cli.main(["--config-file", str(path), "--show-keys"])
    first = capsys.readouterr().out
    assert [line.split()[0] for line in first.splitlines()] == [
        "m_key",
        "sm_key",
        "sa_key",
    ]
    cli.main(["--config-file", str(path), "--show-keys"])
    assert capsys.readouterr().out == first


@pytest.mark.parametrize(
    "text, match",
    [
        ("[opensm]\nhardening = none, m_key\n", "either"),
        ("[opensm]\ncheck_interval = 0\n", "positive"),
        ("[opensm]\nipoib_mtu = 6\n", "ipoib_mtu"),
        ("[opensm]\nflat_pkey = 100\n", "flat_pkey"),
        ("[opensm_conf]\npart_enforce = off\n", "part_enforce"),
        ("[opensm_conf]\nlog_flags = 0x01\n", "log_flags"),
        ("[opensm_conf]\nlog_file = /tmp/x\n", "log_file"),
        ("[opensm]\nopensm_args = -P /x\n", "unknown"),
    ],
)
def test_more_invalid(tmp_path, text, match):
    with pytest.raises(config_mod.ConfigError, match=match):
        _load(tmp_path, "[api]\ntoken = x\n" + text)


def test_hex_and_bool_options(tmp_path):
    config = _load(
        tmp_path,
        "[api]\ntoken = x\n[opensm]\nflat_pkey = 0x7fff\n"
        "shared_default_partition = yes\n",
    )
    assert config.flat_pkey == 0x7FFF
    assert config.shared_default_partition is True


def test_ha_needs_a_job_timeout_longer_than_a_takeover():
    from alternative_fabric_manager import config as config_mod

    base = dict(
        token="t", ha=True, connection="mysql+pymysql://u:p@h/afm", driver="managed"
    )
    with pytest.raises(config_mod.ConfigError, match="job_timeout must exceed"):
        config_mod.Config(**base).validate()
    config_mod.Config(**base, job_timeout=300).validate()
    with pytest.raises(config_mod.ConfigError, match="shutdown_timeout"):
        config_mod.Config(**base, job_timeout=300, shutdown_timeout=40).validate()


def test_ha_needs_the_m_key_and_sm_key():
    from alternative_fabric_manager import config as config_mod

    base = dict(
        token="t",
        ha=True,
        connection="mysql+pymysql://u:p@h/afm",
        driver="managed",
        job_timeout=300,
        manager_container="afm",
    )
    with pytest.raises(config_mod.ConfigError, match="sm_key"):
        config_mod.Config(**base, hardening="m_key, enforcement").validate()
    config_mod.Config(**base, hardening="m_key, sm_key").validate()
