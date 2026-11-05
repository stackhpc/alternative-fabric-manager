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
reload = none
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
        "[api]\ntoken = x\n[opensm]\nreload = restart\n",
        "[api]\ntoken = x\n[opensm]\ndefault_partition = Default=0x7fff\n",
    ],
)
def test_invalid(tmp_path, text):
    with pytest.raises(config_mod.ConfigError):
        _load(tmp_path, text)


def test_missing_file(tmp_path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.Config.load(str(tmp_path / "missing.conf"))
