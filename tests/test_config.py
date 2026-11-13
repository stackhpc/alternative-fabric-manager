import contextlib
import io

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
        ("all, babbling_ports", set(config_mod.HARDENING_GROUPS)),
        ("yes, m_key", set(config_mod.HARDENING_GROUPS)),
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


@pytest.mark.parametrize("value", ["all, none", "none, m_key", "no, all"])
def test_no_hardening_stands_alone(tmp_path, value):
    with pytest.raises(config_mod.ConfigError, match="either"):
        _load(tmp_path, f"[api]\ntoken = x\n[opensm]\nhardening = {value}\n")


def test_unknown_hardening_group_beside_all(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="unknown groups: mkey"):
        _load(tmp_path, "[api]\ntoken = x\n[opensm]\nhardening = all, mkey\n")


def test_hardening_groups_outside_all(tmp_path, monkeypatch):
    # A group outside all, as later opt-in groups will be, is chosen only by
    # naming it, so an upgrade never turns it on by itself.
    six = set(config_mod.HARDENING_GROUPS)
    monkeypatch.setattr(
        config_mod, "HARDENING_GROUPS", config_mod.HARDENING_GROUPS + ("seventh",)
    )
    for value, groups in (
        ("all", six),
        ("all, seventh", six | {"seventh"}),
        ("seventh, m_key", {"seventh", "m_key"}),
    ):
        text = f"[api]\ntoken = x\n[opensm]\nhardening = {value}\n"
        assert _load(tmp_path, text).hardening_groups == groups


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
        "key_mgr_seed",
    ]
    cli.main(["--config-file", str(path), "--show-keys"])
    assert capsys.readouterr().out == first


def _seed_override_conf(tmp_path):
    # A seed that isn't the one the manager stores: a random seed is stored
    # first, then the configuration overrides it.
    from alternative_fabric_manager import cli

    path = tmp_path / "afm.conf"
    text = (
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\ndriver = none\nconfig_dir = {tmp_path}\n"
    )
    path.write_text(text)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["--config-file", str(path), "--show-keys"])
    path.write_text(text + "[opensm_conf]\nkey_mgr_seed = 0x1\n")
    return path


def test_show_keys_with_a_refused_seed(tmp_path, capsys):
    # The seed override is refused when OpenSM would start, not when an
    # operator looks at the keys.
    from alternative_fabric_manager import cli

    path = _seed_override_conf(tmp_path)
    cli.main(["--config-file", str(path), "--show-keys"])
    names = [line.split()[0] for line in capsys.readouterr().out.splitlines()]
    assert "key_mgr_seed" in names


def test_a_refused_seed_stops_a_single_node_start(tmp_path, monkeypatch):
    from alternative_fabric_manager import cli

    served = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: served.append(a))
    path = _seed_override_conf(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["--config-file", str(path)])
    assert "key_mgr_seed" in str(e.value.code)
    assert not served


@pytest.mark.parametrize(
    "text, match",
    [
        ("[opensm]\nhardening = none, m_key\n", "either"),
        ("[opensm]\ncheck_interval = 0\n", "positive"),
        ("[opensm]\nkey_lease_period = -1\n", "key_lease_period"),
        ("[opensm]\nkey_lease_period = 65536\n", "key_lease_period"),
        ("[opensm]\nipoib_mtu = 6\n", "ipoib_mtu"),
        ("[opensm]\nflat_pkey = 100\n", "flat_pkey"),
        ("[opensm_conf]\npart_enforce = off\n", "part_enforce"),
        ("[opensm_conf]\nlog_flags = 0x01\n", "log_flags"),
        ("[opensm_conf]\nlog_file = /tmp/x\n", "log_file"),
        ("[opensm]\nopensm_args = -P /x\n", "unknown"),
        ("[opensm_conf]\nkey_mgr_seed = 0\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = 0x0\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = seed\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = 0x12_34\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = 010\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = -1\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = 0x1ffffffffffffffff\n", "key_mgr_seed"),
        ("[opensm_conf]\nkey_mgr_seed = 18446744073709551616\n", "key_mgr_seed"),
    ],
)
def test_more_invalid(tmp_path, text, match):
    with pytest.raises(config_mod.ConfigError, match=match):
        _load(tmp_path, "[api]\ntoken = x\n" + text)


def test_key_lease_period(tmp_path):
    assert _load(tmp_path, "[api]\ntoken = x\n").key_lease_period == 0
    config = _load(tmp_path, "[api]\ntoken = x\n[opensm]\nkey_lease_period = 60\n")
    assert config.key_lease_period == 60


@pytest.mark.parametrize("lease, warned", [(0, False), (60, True), (3600, False)])
def test_a_lease_shorter_than_a_takeover_is_warned_about(lease, warned):
    # Allowed, for test fabrics, but every unclean takeover is then a lapse.
    config = config_mod.Config(key_lease_period=lease)
    assert config_mod.takeover_bound(config) < 3600
    assert bool(config_mod.ha_warnings(config)) is warned


def test_a_seed_override_loads(tmp_path):
    # Whether it's the stored seed is for the manager to check, on start.
    config = _load(tmp_path, "[api]\ntoken = x\n[opensm_conf]\nkey_mgr_seed = 0x12\n")
    assert config.opensm_conf["key_mgr_seed"] == "0x12"


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


def test_a_removed_option_is_ignored_with_a_warning(tmp_path, caplog):
    from alternative_fabric_manager import config as config_mod

    path = tmp_path / "afm.conf"
    path.write_text("[api]\ntoken = t\n[ha]\nmanual_takeover = true\n")
    config = config_mod.Config.load(str(path))
    assert not hasattr(config, "manual_takeover")
    assert "manual_takeover was removed" in caplog.text


def test_single_node_starts_opensm_even_if_the_pull_fails(tmp_path, monkeypatch):
    # The start pulls again, and reports what fails.
    import uvicorn

    from alternative_fabric_manager import cli, drivers
    from alternative_fabric_manager import manager as manager_mod

    calls = []

    class Driver(drivers.NoOpenSM):
        def ensure_image(self):
            calls.append("pull")
            raise ConnectionError("daemon gone")

    monkeypatch.setattr(drivers, "make_driver", lambda _config: Driver())
    monkeypatch.setattr(
        manager_mod.Manager, "start", lambda self: calls.append("start")
    )
    monkeypatch.setattr(manager_mod.Manager, "stop", lambda self: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append("serve"))
    path = tmp_path / "afm.conf"
    path.write_text(
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\ndriver = none\nconfig_dir = {tmp_path}\n"
    )
    cli.main(["--config-file", str(path)])
    assert calls == ["pull", "start", "serve"]
