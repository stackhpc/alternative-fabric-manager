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


def test_hardening_option(tmp_path):
    assert _load(tmp_path, "[api]\ntoken = x\n").hardening is True
    config = _load(tmp_path, "[api]\ntoken = x\n[opensm]\nhardening = false\n")
    assert config.hardening is False
    with pytest.raises(config_mod.ConfigError):
        _load(tmp_path, "[api]\ntoken = x\n[opensm]\nhardening = maybe\n")


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
