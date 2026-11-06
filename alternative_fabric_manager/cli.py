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

from alternative_fabric_manager import api, db, drivers
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod

DEFAULT_CONFIG = "/etc/alternative-fabric-manager/afm.conf"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="An InfiniBand fabric manager for networking-mlnx"
    )
    parser.add_argument("--config-file", default=DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--show-keys",
        action="store_true",
        help="Print the fabric's OpenSM keys, for diagnostic tools such as "
        "smpquery -y <m_key>, and exit",
    )
    parser.add_argument(
        "--confirm-switches",
        action="store_true",
        help="Record the switches OpenSM sees now as the fabric's expected "
        "ones, which every fabric check must find, and exit",
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
        "--confirm-takeover",
        action="store_true",
        help="HA: while a freeze ends and its frozen leader can't acknowledge "
        "it, confirm that that leader's OpenSM can't act, so that another "
        "instance may take over",
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

        print("\n".join(operator.status(lease.read())))
        return

    if args.freeze or args.unfreeze or args.confirm_takeover:
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
                        "is verified, or --confirm-takeover"
                    )
            else:
                node, term = operator.confirm_takeover(lease)
                print(f"Confirmed that {node}'s OpenSM, term {term}, can't act")
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

    if args.confirm_switches:
        manager = manager_mod.Manager(
            config,
            db.make_sessionmaker(config.database_url),
            drivers.make_driver(config),
        )
        switches = manager.confirm_switches()
        if not switches:
            sys.exit("alternative-fabric-manager: OpenSM sees no switches")
        for guid in switches:
            print(f"0x{guid:016x}")
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
    manager.start()
    try:
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level="debug" if args.debug else "info",
        )
    finally:
        manager.stop()


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
    coordinator = coordinator_mod.Coordinator(
        config, lease_mod.GaleraLease(sessionmaker), driver, manager
    )
    app = api.create_app(config, manager, coordinator)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
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
    threading.Thread(target=coordinator.start, name="coordinator", daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
