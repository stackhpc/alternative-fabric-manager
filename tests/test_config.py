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
        (None, set(config_mod.ALL_HARDENING_GROUPS)),
        ("all", set(config_mod.ALL_HARDENING_GROUPS)),
        ("true", set(config_mod.ALL_HARDENING_GROUPS)),
        ("none", set()),
        ("false", set()),
        ("m_key", {"m_key"}),
        ("m_key, Enforcement", {"m_key", "enforcement"}),
        ("all, babbling_ports", set(config_mod.ALL_HARDENING_GROUPS)),
        ("yes, m_key", set(config_mod.ALL_HARDENING_GROUPS)),
        # Removing groups: from all, named or by default
        ("all, -sa_trust", set(config_mod.ALL_HARDENING_GROUPS) - {"sa_trust"}),
        ("-sa_trust", set(config_mod.ALL_HARDENING_GROUPS) - {"sa_trust"}),
        (
            "-sa_trust, -SA_KEY",
            set(config_mod.ALL_HARDENING_GROUPS) - {"sa_trust", "sa_key"},
        ),
        ("- babbling_ports", set(config_mod.ALL_HARDENING_GROUPS) - {"babbling_ports"}),
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


@pytest.mark.parametrize(
    "value, match",
    [
        # A misspelt removal would otherwise leave the group on unnoticed.
        ("all, -sa_trusts", "unknown groups: sa_trusts"),
        ("-all", "unknown groups: all"),
        ("-none", "unknown groups: none"),
        # Contradictions
        ("all, sa_trust, -sa_trust", "both names and removes sa_trust"),
        ("m_key, sm_key, -sa_trust", "only from all"),
        ("none, -sa_trust", "either"),
        # A blank value, as a template that rendered nothing gives
        ("", "empty"),
        (" , ", "empty"),
    ],
)
def test_unclear_removals_are_refused(tmp_path, value, match):
    with pytest.raises(config_mod.ConfigError, match=match):
        _load(tmp_path, f"[api]\ntoken = x\n[opensm]\nhardening = {value}\n")


def test_ha_refuses_removing_its_required_keys():
    # HA mode's Ask and fencing rest on them (specs/ha.md, Required keys).
    config = config_mod.Config(ha=True, hardening="-m_key")
    assert any("m_key" in p for p in config_mod._ha_problems(config))


def test_hardening_groups_outside_all(tmp_path, monkeypatch):
    # A group outside all, as later opt-in groups will be, is chosen only by
    # naming it, so an upgrade never turns it on by itself.
    six = set(config_mod.ALL_HARDENING_GROUPS)
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
    # Only a start stores the seed and the base (test_show_keys_stores_no_
    # late_key), so a fresh database has neither yet.
    assert [line.split()[0] for line in first.splitlines()] == [
        "m_key",
        "sm_key",
        "sa_key",
    ]
    cli.main(["--config-file", str(path), "--show-keys"])
    assert capsys.readouterr().out == first
    _start_once(path)
    cli.main(["--config-file", str(path), "--show-keys"])
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == [
        "m_key",
        "sm_key",
        "sa_key",
        "key_mgr_seed",
        "m_key_base",
    ]


def _start_once(path):
    """Start and stop a manager with the configuration at path, as a
    deployment's first start would"""
    from alternative_fabric_manager import db, drivers
    from alternative_fabric_manager import manager as manager_mod

    config = config_mod.Config.load(str(path))
    sessionmaker = db.make_sessionmaker(config.database_url)
    manager = manager_mod.Manager(config, sessionmaker, drivers.NoOpenSM())
    manager.start()
    manager.stop()


def test_show_keys_stores_no_late_key(tmp_path):
    # A command beside the manager mustn't store the seed or the per-port
    # M_Key base: the next start would find it stored, and take it as held
    # by a backup since an earlier start, which it may not be.
    from alternative_fabric_manager import cli, db, drivers
    from alternative_fabric_manager import manager as manager_mod

    path = tmp_path / "afm.conf"
    path.write_text(
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\ndriver = none\nconfig_dir = {tmp_path}\n"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["--config-file", str(path), "--show-keys"])
    config = config_mod.Config.load(str(path))
    sessionmaker = db.make_sessionmaker(config.database_url)
    with sessionmaker() as session:
        assert session.get(db.Secret, "m_key_base") is None
        assert session.get(db.Secret, "key_mgr_seed") is None
        assert session.get(db.State, manager_mod.BASE_KEPT) is None
    manager = manager_mod.Manager(config, sessionmaker, drivers.NoOpenSM())
    manager.start()
    manager.stop()
    # The first start stored it, so it isn't kept yet; the next start is.
    assert not manager.base_kept()
    manager.start()
    manager.stop()
    assert manager.base_kept()


def _seed_override_conf(tmp_path):
    # A seed that isn't the one the manager stores: a start stores a random
    # seed first, then the configuration overrides it.
    path = tmp_path / "afm.conf"
    text = (
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\ndriver = none\nconfig_dir = {tmp_path}\n"
    )
    path.write_text(text)
    _start_once(path)
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
        ("[opensm_conf]\nm_key_base = 0x12\n", "m_key_base"),
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


# m_key_per_port (specs/secured-profile.md, Per-port M_Keys, The design)

PER_PORT = "[api]\ntoken = x\n[opensm]\nhardening = all, m_key_per_port\n"


def test_all_doesnt_include_m_key_per_port(tmp_path):
    # Experimental, and it leaves state on ports: an upgrade mustn't turn it
    # on (Choosing the group).
    assert "m_key_per_port" in config_mod.HARDENING_GROUPS
    assert "m_key_per_port" not in config_mod.ALL_HARDENING_GROUPS
    text = "[api]\ntoken = x\n[opensm]\nhardening = all\n"
    assert "m_key_per_port" not in _load(tmp_path, text).hardening_groups


def test_all_with_m_key_per_port_names_it(tmp_path):
    groups = _load(tmp_path, PER_PORT).hardening_groups
    assert groups == set(config_mod.ALL_HARDENING_GROUPS) | {"m_key_per_port"}


@pytest.mark.parametrize(
    "hardening",
    ["-m_key, m_key_per_port", "all, -m_key, m_key_per_port", "sm_key, m_key_per_port"],
)
def test_m_key_per_port_needs_the_m_key_group(tmp_path, hardening):
    # Per-port keys are M_Key protection; without m_key nothing sets it.
    # ("-m_key, m_key_per_port" names a group beside a removal, which
    # parse_hardening refuses already.)
    text = f"[api]\ntoken = x\n[opensm]\nhardening = {hardening}\n"
    with pytest.raises(config_mod.ConfigError):
        _load(tmp_path, text)


def test_removing_m_key_beside_m_key_per_port_says_why(tmp_path):
    text = "[api]\ntoken = x\n[opensm]\nhardening = all, -m_key, m_key_per_port\n"
    with pytest.raises(config_mod.ConfigError, match="needs the m_key group"):
        _load(tmp_path, text)


def test_m_key_per_port_with_the_m_key_group_loads(tmp_path):
    text = "[api]\ntoken = x\n[opensm]\nhardening = m_key, m_key_per_port\n"
    assert _load(tmp_path, text).hardening_groups == {"m_key", "m_key_per_port"}


def test_ha_refuses_m_key_per_port():
    # Leaders with and without the group could alternate until
    # configuration epochs are enforced (requirement 5).
    config = config_mod.Config(
        token="t",
        ha=True,
        connection="mysql+pymysql://u:p@h/afm",
        job_timeout=300,
        hardening="all, m_key_per_port",
    )
    assert any(
        "per-port M_Keys aren't supported in HA mode yet" in problem
        for problem in config_mod._ha_problems(config)
    )
    with pytest.raises(config_mod.ConfigError, match="HA mode yet"):
        config.validate()
    config.hardening = "all"
    config.validate()


@pytest.mark.parametrize("driver", ["docker", "none"])
def test_m_key_per_port_needs_the_managed_driver(tmp_path, driver):
    # The manager writes OpenSM's cache volume and runs the prober in
    # OpenSM's image, which only the managed driver can.
    with pytest.raises(config_mod.ConfigError, match="managed driver"):
        _load(tmp_path, PER_PORT + f"driver = {driver}\n")


@pytest.mark.parametrize(
    "volumes",
    [
        "/lib/modules:/lib/modules:ro,opensm_logs:/var/log",
        # Read-only: the manager couldn't copy the cache into it
        "opensm_cache:/var/cache/opensm:ro",
        # A mode is a list of options: any ro among them is read-only.
        "opensm_cache:/var/cache/opensm:ro,z",
        "opensm_cache:/var/cache/opensm:z,ro",
        "opensm_cache:/var/cache/opensm:z,ro,opensm_logs:/var/log",
        "opensm_cache:/var/cache",
        "/var/cache/opensm",
    ],
)
def test_m_key_per_port_needs_a_writable_cache_volume(tmp_path, volumes):
    # Without the volume, the copy would hit the read-only root filesystem.
    with pytest.raises(config_mod.ConfigError, match="/var/cache/opensm"):
        _load(tmp_path, PER_PORT + f"volumes = {volumes}\n")


@pytest.mark.parametrize(
    "volumes",
    [
        config_mod.DEFAULT_VOLUMES,
        "opensm_cache:/var/cache/opensm/",
        "opensm_cache:/var/cache/opensm:rw",
        " /srv/cache:/var/cache/opensm:z , opensm_logs:/var/log",
    ],
)
def test_a_writable_cache_volume_is_enough(tmp_path, volumes):
    config = _load(tmp_path, PER_PORT + f"volumes = {volumes}\n")
    assert "m_key_per_port" in config.hardening_groups


@pytest.mark.parametrize(
    "option",
    ["m_key", "m_key_per_port", "m_key_protection_level", "m_key_lookup"],
)
def test_m_key_per_port_refuses_overrides_of_its_options(tmp_path, option):
    # The m_key line is the base while the target is on, and OpenSM forces
    # the others with the option (requirement 6).
    with pytest.raises(config_mod.ConfigError, match=option):
        _load(tmp_path, PER_PORT + f"[opensm_conf]\n{option} = 0x1\n")


@pytest.mark.parametrize("value", ["TRUE", "FALSE"])
def test_m_key_per_port_by_hand_is_refused(tmp_path, value):
    # By hand, every port would be re-keyed with no record of which key it
    # holds (requirement 6), whatever the value.
    text = f"[api]\ntoken = x\n[opensm_conf]\nm_key_per_port = {value}\n"
    with pytest.raises(config_mod.ConfigError, match="m_key_per_port"):
        _load(tmp_path, text)


def test_without_the_group_m_key_options_can_be_overridden(tmp_path):
    # As before the group existed
    config = _load(
        tmp_path,
        "[api]\ntoken = x\n[opensm_conf]\nm_key_lookup = FALSE\n"
        "m_key_protection_level = 1\n",
    )
    assert config.opensm_conf["m_key_lookup"] == "FALSE"


def test_port_verify_interval(tmp_path):
    assert _load(tmp_path, "[api]\ntoken = x\n").port_verify_interval == 60
    config = _load(tmp_path, "[api]\ntoken = x\n[opensm]\nport_verify_interval = 90\n")
    assert config.port_verify_interval == 90


@pytest.mark.parametrize("value", ["0", "-1"])
def test_port_verify_interval_must_be_positive(tmp_path, value):
    text = f"[api]\ntoken = x\n[opensm]\nport_verify_interval = {value}\n"
    with pytest.raises(config_mod.ConfigError, match="port_verify_interval"):
        _load(tmp_path, text)


# The per-port M_Key commands (specs/secured-profile.md, Per-port M_Keys,
# Commands), each run beside the running manager


def _per_port_conf(tmp_path, hardening="all, m_key_per_port", extra=""):
    from alternative_fabric_manager import db, drivers
    from alternative_fabric_manager import manager as manager_mod

    path = tmp_path / "afm.conf"
    path.write_text(
        f"[api]\ntoken = x\n[database]\ndatabase = {tmp_path}/afm.sqlite\n"
        f"[opensm]\nconfig_dir = {tmp_path}\nhardening = {hardening}\n{extra}"
    )
    # The keys a start stores, as the manager's first start of this release
    # would have before an operator runs these commands
    config = config_mod.Config.load(str(path))
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
    )
    manager._stores_late_keys = True
    manager.keys()
    return path


def _cli(path, *args):
    from alternative_fabric_manager import cli

    cli.main(["--config-file", str(path), *args])


def test_port_key_commands(tmp_path, capsys):
    path = _per_port_conf(tmp_path)
    _cli(path, "--add-link", "0x51:3", "b1:1")
    assert "2 new ports" in capsys.readouterr().out
    _cli(path, "--port-key-states")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "target off, OpenSM last started off"
    assert "0x00000000000000b1 - unknown -" in out
    _cli(path, "--release-port", "0xB1")
    capsys.readouterr()
    _cli(path, "--port-key-states")
    assert "0x00000000000000b1 - unknown released" in capsys.readouterr().out
    # A row that's unknown has no key to print, and nor has a GUID with no row
    # while others exist.
    for guid in ("b1", "c1"):
        with pytest.raises(SystemExit) as e:
            _cli(path, "--port-m-key", guid)
        assert str(e.value.code).startswith("alternative-fabric-manager: ")
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit, match="is named"):
        _cli(path, "--forget-port-keys")
    # Never seen, so not present: it can be forgotten.
    _cli(path, "--forget-port", "b1")
    assert "2 links" in capsys.readouterr().out
    path = _per_port_conf(tmp_path, hardening="all")
    _cli(path, "--forget-port-keys")
    assert capsys.readouterr().out == "Forgot 1 ports and 0 links\n"


def test_port_m_key_with_no_rows_is_the_stored_m_key(tmp_path, capsys):
    path = _per_port_conf(tmp_path, hardening="all")
    _cli(path, "--show-keys")
    m_key = capsys.readouterr().out.splitlines()[0].split()[1]
    _cli(path, "--port-m-key", "0xb1")
    assert capsys.readouterr().out == f"{m_key}\n"


@pytest.mark.parametrize(
    "args, match",
    [
        (["--add-link", "0x51", "0xb1:1"], "GUID>:<port>"),
        (["--add-link", "0x51:1", "0x51:1"], "two different ports"),
        (["--release-port", "zz"], "isn't a GUID"),
        (["--release-port", "b1"], "no port"),
        (["--forget-port", "b1"], "no port"),
    ],
)
def test_port_key_commands_refuse_what_cant_be_done(tmp_path, args, match):
    path = _per_port_conf(tmp_path)
    with pytest.raises(SystemExit, match=match):
        _cli(path, *args)


def test_confirm_fabric_with_no_fabric_to_check_sees_no_switches(tmp_path):
    path = _per_port_conf(tmp_path, hardening="all", extra="driver = none\n")
    with pytest.raises(SystemExit, match="OpenSM sees no switches"):
        _cli(path, "--confirm-fabric")


def test_port_key_commands_are_refused_in_ha_mode(tmp_path, monkeypatch):
    path = _per_port_conf(tmp_path, hardening="all")
    config = config_mod.Config.load(str(path))
    # HA's own settings aren't the point here: only that ha is set
    config.ha = True
    monkeypatch.setattr(config_mod.Config, "load", lambda _path: config)
    with pytest.raises(SystemExit, match="HA mode"):
        _cli(path, "--port-key-states")


def test_port_key_commands_are_exclusive(tmp_path):
    path = _per_port_conf(tmp_path)
    with pytest.raises(SystemExit):
        _cli(path, "--port-key-states", "--forget-port-keys")


def test_parse_volumes_keeps_a_modes_options_together():
    assert config_mod.parse_volumes(" a:/x:ro,z , b:/y, c:/z:z,ro ,") == [
        "a:/x:ro,z",
        "b:/y",
        "c:/z:z,ro",
    ]
    # A path alone is an anonymous volume, not a mode's option.
    assert config_mod.parse_volumes("a:/x:ro,/scratch") == ["a:/x:ro", "/scratch"]
