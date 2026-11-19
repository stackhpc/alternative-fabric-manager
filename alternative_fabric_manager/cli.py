# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import logging
import sys

import uvicorn

from alternative_fabric_manager import allowed_sm, api, db, drivers, port_keeper
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod

DEFAULT_CONFIG = "/etc/alternative-fabric-manager/afm.conf"
# How --confirm-fabric marks a switch it found for the first time
PENDING_MARK = (
    " (new: not trusted until you confirm the fabric again; check that it's a switch)"
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="An InfiniBand fabric manager for networking-mlnx"
    )
    parser.add_argument("--config-file", default=DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--show-keys",
        action="store_true",
        help="Print the fabric's OpenSM keys, class key seed and per-port "
        "M_Key base, which are secrets, for diagnostic tools such as "
        "smpquery -y <m_key>, and exit. With per-port M_Keys, a port holds "
        "its own key: take -y's from --port-m-key instead",
    )
    parser.add_argument(
        "--confirm-fabric",
        action="store_true",
        help="Record the switches OpenSM sees now as the fabric's expected "
        "ones, which every fabric check must find, and, with per-port M_Keys, "
        "the links its cache lists, and exit. With the m_key_per_port "
        "hardening group named, it also arms the turn-on of per-port M_Keys, "
        "which the manager's next start begins, once every port is found "
        "ready; otherwise it says why not and exits non-zero",
    )
    enforcement = parser.add_mutually_exclusive_group()
    enforcement.add_argument(
        "--show-enforcement-errors",
        action="store_true",
        help="List the switch ports whose partition enforcement OpenSM "
        "cleared, as <switch GUID>:<port>, and exit. While any is listed, no "
        "job completes",
    )
    enforcement.add_argument(
        "--clear-enforcement-errors",
        action="store_true",
        help="Forget the switch ports --show-enforcement-errors lists, list "
        "them, and exit; jobs then complete again. Check first that each "
        "port enforces again: OpenSM needn't report one it still can't",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="HA: show the lease's holder, term and freeze, and exit",
    )
    parser.add_argument(
        "--db-upgrade",
        action="store_true",
        help="Bring the [database] connection database's schema up to date, "
        "and exit; run once per upgrade, before starting the new version",
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="HA: keep OpenSM on the current leader's node, whatever happens to "
        "Galera, until --unfreeze; new requests get 503 meanwhile",
    )
    parser.add_argument(
        "--unfreeze", action="store_true", help="HA: end a freeze, and exit"
    )
    parser.add_argument(
        "--force-unfreeze",
        action="store_true",
        help="HA: end a freeze whose frozen leader can't acknowledge the "
        "unfreeze, so that another instance may take over. Only after "
        "stopping that leader's OpenSM and checking that no opensm process "
        "runs on its host: nothing checks this for you (see --status)",
    )
    parser.add_argument(
        "--config-epoch",
        action="store_true",
        help="HA: show the configuration epoch, the hashes it allows, this "
        "configuration's hash, and each node's, and exit",
    )
    parser.add_argument(
        "--allow-config",
        nargs="?",
        const="",
        metavar="HASH",
        help="HA: record a new configuration epoch that allows this "
        "configuration's hash, and HASH too if given, and exit. A leader whose "
        "configuration isn't allowed hands over to a live candidate whose is",
    )
    parser.add_argument(
        "--forget-node",
        metavar="NODE",
        help="HA: remove a retired node's row, so that its ports leave the "
        "allowed_sm_guids list at the next takeover, and exit. Refused while "
        "the node may run OpenSM, or its manager is live; its ports stay "
        "known SM ports until --drop-sm-port",
    )
    parser.add_argument(
        "--forget-revisions",
        action="store_true",
        help="After restoring Neutron's database from a backup, whose "
        "revisions go back and deleted IDs return: forget every deleted "
        "object and recorded revision, so that no current request is "
        "refused as late, and exit",
    )
    ports = parser.add_argument_group(
        "per-port M_Keys",
        "With the m_key_per_port hardening group (docs/opensm-hardening.md). "
        "Each runs beside the running manager, and exits. GUIDs are hex, "
        "with or without 0x",
    )
    commands = ports.add_mutually_exclusive_group()
    commands.add_argument(
        "--port-key-states",
        action="store_true",
        help="List each recorded port's GUID, which key it holds (m_key, "
        "derived or unknown), and whether it lapsed, is reachable or was "
        "released, never a key; and the target",
    )
    commands.add_argument(
        "--port-m-key",
        metavar="GUID",
        help="Print the M_Key the port holds now, a secret, only to feed a "
        "diagnostic tool's -y, as in KEY=$(... --port-m-key GUID)",
    )
    commands.add_argument(
        "--add-link",
        nargs=2,
        metavar=("GUID:PORT", "PEER_GUID:PORT"),
        help="Record a link that OpenSM hasn't, both directions, once you "
        "know what's attached: a switch port with its link up and no "
        "recorded neighbour fails every fabric check",
    )
    commands.add_argument(
        "--release-port",
        metavar="GUID",
        help="Complete a lost port's held jobs as they are, and exempt it from "
        "the fabric check's rule on lost ports, until it answers its key "
        "again. It changes nothing on the port",
    )
    commands.add_argument(
        "--forget-port-keys",
        action="store_true",
        help="Drop every recorded port and link, once per-port M_Keys are off "
        "and every port holds the stored M_Key",
    )
    commands.add_argument(
        "--forget-port",
        metavar="GUID",
        help="Drop a removed port and its links; refused for a port seen in "
        "the last day",
    )
    sms = parser.add_argument_group(
        "allowed SMs",
        "With the allowed_sm hardening group, or [opensm_conf] "
        "allowed_sm_guids (docs/opensm-hardening.md). OpenSM ignoring an SM on "
        "a known SM port contests the fabric; on any other port, it's only "
        "logged. Each runs beside the running manager, and exits",
    )
    sm_commands = sms.add_mutually_exclusive_group()
    sm_commands.add_argument(
        "--known-sm-ports",
        action="store_true",
        help="List the known SM ports: this host's physical ports, as the "
        "last start found them, and those recorded with --record-sm-port",
    )
    sm_commands.add_argument(
        "--record-sm-port",
        metavar="GUID",
        help="Record a port whose host may hold the fabric's keys, such as "
        "another host that ran AFM's OpenSM, so that an SM there that OpenSM "
        "ignores contests the fabric",
    )
    sm_commands.add_argument(
        "--drop-sm-port",
        metavar="GUID",
        help="Forget a known SM port once its adapter is no longer in a host "
        "with the fabric's keys, as when it's reused in a tenant's host; "
        "refused for this host's own ports",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = config_mod.Config.load(args.config_file)
    except config_mod.ConfigError as e:
        sys.exit(f"alternative-fabric-manager: {e}")

    if args.db_upgrade:
        if not config.connection:
            sys.exit(
                "alternative-fabric-manager: --db-upgrade needs [database] "
                "connection; an SQLite file upgrades itself"
            )
        db.upgrade(config.connection)
        return

    try:
        db.make_sessionmaker(config.database_url)
    except db.SchemaError as e:
        sys.exit(f"alternative-fabric-manager: {e}")

    if args.status:
        from alternative_fabric_manager.ha import lease as lease_mod

        lease = lease_mod.GaleraLease(
            db.make_sessionmaker(config.database_url, config.call_timeout)
        )
        from alternative_fabric_manager.ha import operator

        try:
            state = lease.read()
        except lease_mod.NotPrimary as e:
            sys.exit(f"alternative-fabric-manager: {e}")
        print("\n".join(operator.status(state)))
        return

    if (
        args.config_epoch
        or args.allow_config is not None
        or args.forget_node is not None
    ):
        _epochs(config, args)
        return

    if args.freeze or args.unfreeze or args.force_unfreeze:
        from alternative_fabric_manager.ha import lease as lease_mod
        from alternative_fabric_manager.ha import operator

        sessionmaker = db.make_sessionmaker(config.database_url, config.call_timeout)
        lease = lease_mod.GaleraLease(sessionmaker)
        try:
            if args.freeze:
                node = operator.freeze(config, lease, sessionmaker)
                print(f"Frozen on {node}")
            elif args.unfreeze:
                if operator.unfreeze(config, lease, config.lease_timeout):
                    print("Unfrozen")
                else:
                    sys.exit(
                        "alternative-fabric-manager: the leader hasn't acknowledged "
                        "the unfreeze; no takeover until it does, its node's stop "
                        "is verified, or --force-unfreeze"
                    )
            else:
                node, term = operator.force_unfreeze(lease)
                print(
                    f"Forced the unfreeze: {node}'s OpenSM, term {term}, is taken "
                    "as stopped, and another instance may take over"
                )
        except (operator.OperatorError, lease_mod.NotPrimary) as e:
            sys.exit(f"alternative-fabric-manager: {e}")
        return

    if args.show_keys:
        manager = manager_mod.Manager(
            config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
        )
        for name, value in manager.keys().items():
            print(f"{name} {value}")
        return

    if args.confirm_fabric:
        manager = manager_mod.Manager(
            config,
            db.make_sessionmaker(config.database_url),
            drivers.make_driver(config),
        )
        try:
            confirmation = manager.confirm_fabric()
        except drivers.ReloadError as e:
            # The switches weren't recorded: the fabric couldn't be asked, as
            # when this host's port answers no key the manager holds. The
            # links OpenSM's cache lists may have been, as a check records
            # them.
            sys.exit(
                f"alternative-fabric-manager: can't confirm the fabric: {e} "
                "(no switch was confirmed)"
            )
        except port_keeper.PortKeysError as e:
            # Such as the base not yet stored by a start
            sys.exit(f"alternative-fabric-manager: can't confirm the fabric: {e}")
        if not confirmation.switches:
            sys.exit("alternative-fabric-manager: OpenSM sees no switches")
        for guid in confirmation.switches:
            mark = PENDING_MARK if guid in confirmation.pending else ""
            print(f"0x{guid:016x}{mark}")
        if confirmation.pending:
            print(
                f"{len(confirmation.pending)} switches are new, and pending: none "
                "is expected or trusted until --confirm-fabric finds it again, "
                "so check that each is a switch, not an adapter that answers as "
                "one, and run it again"
            )
        if confirmation.note:
            print(confirmation.note)
        if confirmation.problems:
            sys.stdout.flush()
            sys.exit(
                "alternative-fabric-manager: the switches are confirmed, but "
                "per-port M_Keys aren't armed:\n"
                + "\n".join(f"- {problem}" for problem in confirmation.problems)
            )
        return

    if args.forget_revisions:
        manager = manager_mod.Manager(
            config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
        )
        count = manager.forget_revisions()
        print(f"Forgot {count} deleted objects and every recorded revision")
        return

    if (
        args.port_key_states
        or args.port_m_key is not None
        or args.add_link is not None
        or args.release_port is not None
        or args.forget_port_keys
        or args.forget_port is not None
    ):
        try:
            _port_keys(config, args)
        except port_keeper.PortKeysError as e:
            sys.exit(f"alternative-fabric-manager: {e}")
        return

    if (
        args.known_sm_ports
        or args.record_sm_port is not None
        or args.drop_sm_port is not None
    ):
        try:
            _allowed_sms(config, args)
        except (allowed_sm.AllowedSMError, port_keeper.PortKeysError) as e:
            sys.exit(f"alternative-fabric-manager: {e}")
        return

    if args.show_enforcement_errors or args.clear_enforcement_errors:
        manager = manager_mod.Manager(
            config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
        )
        ports = (
            manager.clear_enforcement_errors()
            if args.clear_enforcement_errors
            else manager.enforcement_errors()
        )
        for port in ports:
            print(port)
        return

    if config.ha:
        _serve_ha(config, args)
        return

    manager = manager_mod.Manager(
        config,
        db.make_sessionmaker(config.database_url),
        drivers.make_driver(config),
    )
    app = api.create_app(config, manager)
    if hasattr(manager.driver, "ensure_image"):
        try:
            manager.driver.ensure_image()
        except Exception as e:  # noqa: BLE001 - as a daemon restart raises
            # The start below tries again, and reports what fails.
            logging.getLogger(__name__).error("Can't pull OpenSM's image: %s", e)
    try:
        manager.start()
    except config_mod.ConfigError as e:
        # A [opensm_conf] key_mgr_seed that isn't the stored seed
        sys.exit(f"alternative-fabric-manager: {e}")
    try:
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level="debug" if args.debug else "info",
        )
    finally:
        manager.stop()


def _port_keys(config, args):
    """The per-port M_Key commands, beside the running manager"""
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
    )
    keeper = manager.port_keeper
    if args.port_key_states:
        print("\n".join(keeper.states()))
    elif args.port_m_key is not None:
        print(keeper.port_m_key(port_keeper.parse_guid(args.port_m_key)))
    elif args.add_link is not None:
        local, peer = (port_keeper.parse_endpoint(text) for text in args.add_link)
        added = keeper.add_link(local, peer)
        print(f"Recorded the link, and {added} new ports")
    elif args.release_port is not None:
        guid = port_keeper.parse_guid(args.release_port)
        keeper.release_port(guid)
        print(f"Released 0x{guid:016x} until it answers its key again")
    elif args.forget_port_keys:
        ports, links = keeper.forget_port_keys()
        print(f"Forgot {ports} ports and {links} links")
    else:
        guid = port_keeper.parse_guid(args.forget_port)
        links = keeper.forget_port(guid)
        print(f"Forgot 0x{guid:016x} and {links} links")


def _allowed_sms(config, args):
    """The allowed SM commands, beside the running manager"""
    manager = manager_mod.Manager(
        config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
    )
    sms = manager.allowed_sms
    if args.known_sm_ports:
        print("\n".join(sms.known_ports()))
    elif args.record_sm_port is not None:
        guid = port_keeper.parse_guid(args.record_sm_port)
        if sms.record(guid):
            print(f"Recorded 0x{guid:016x} as a known SM port")
        else:
            print(f"0x{guid:016x} was a known SM port already")
    else:
        guid = port_keeper.parse_guid(args.drop_sm_port)
        sms.drop(guid)
        print(f"Dropped 0x{guid:016x} from the known SM ports")


def _epochs(config, args):
    """The HA commands on configuration epochs and node rows"""
    from alternative_fabric_manager.ha import lease as lease_mod
    from alternative_fabric_manager.ha import operator

    if not config.ha:
        sys.exit("alternative-fabric-manager: only in HA mode")
    lease = lease_mod.GaleraLease(
        db.make_sessionmaker(config.database_url, config.call_timeout)
    )
    try:
        if args.config_epoch:
            print("\n".join(operator.config_epoch(config, lease)))
        elif args.allow_config is not None:
            epoch, hashes = operator.allow_config(config, lease, args.allow_config)
            print(f"Recorded configuration epoch {epoch}, allowing {', '.join(hashes)}")
        else:
            operator.forget_node(config, lease, args.forget_node)
            print(f"Forgot node {args.forget_node}")
    except (operator.OperatorError, lease_mod.NotPrimary) as e:
        sys.exit(f"alternative-fabric-manager: {e}")


def _serve_ha(config, args):
    """One instance of an HA cluster: the coordinator decides whether this
    instance runs OpenSM (see specs/ha.md)"""
    import threading

    from alternative_fabric_manager.drivers import ha_docker
    from alternative_fabric_manager.ha import coordinator as coordinator_mod
    from alternative_fabric_manager.ha import lease as lease_mod

    sessionmaker = db.make_sessionmaker(config.database_url, config.call_timeout)
    holder = {}
    driver = ha_docker.HAManagedDocker(
        config, m_key=lambda: holder["manager"]._key("m_key")
    )
    manager = manager_mod.Manager(config, sessionmaker, driver)
    holder["manager"] = manager
    # With per-port M_Key rows, the reach probe uses the prober
    driver.reach_switches = lambda: manager.port_keeper.reach_switches(
        ha_docker.REACH_TIMEOUT, driver.reach_name
    )
    coordinator = coordinator_mod.Coordinator(
        config,
        lease_mod.GaleraLease(sessionmaker),
        driver,
        manager,
        hash_config=lambda key: config_mod.config_hash(config, key),
    )
    # The start request's deadline, worked out as it's written
    driver.start_gate = coordinator.start_gate
    app = api.create_app(config, manager, coordinator)
    image_stop = threading.Event()

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            image_stop.set()
            # Step down before uvicorn stops: it raises the signal again once
            # it has, which ends the process.
            coordinator.shutdown()
            super().handle_exit(sig, frame)

    server = Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="debug" if args.debug else "info",
        )
    )
    # Before any takeover needs it, and on every instance, so that a
    # takeover doesn't wait on a registry (see specs/ha.md, Timeouts)
    threading.Thread(
        target=driver.keep_image, args=(image_stop,), name="image", daemon=True
    ).start()
    threading.Thread(target=coordinator.start, name="coordinator", daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
