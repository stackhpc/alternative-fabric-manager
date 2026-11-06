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
    parser.add_argument(
        "--clear-enforcement-errors",
        action="store_true",
        help="Forget the switch ports whose partition enforcement OpenSM "
        "cleared, once they are dealt with, and exit; until then no job "
        "completes",
    )
    parser.add_argument(
        "--db-upgrade",
        action="store_true",
        help="Bring the [database] connection database's schema up to date, "
        "and exit; run once per upgrade, before starting the new version",
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

    if args.clear_enforcement_errors:
        manager = manager_mod.Manager(
            config, db.make_sessionmaker(config.database_url), drivers.NoOpenSM()
        )
        for port in manager.clear_enforcement_errors():
            print(port)
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


if __name__ == "__main__":
    main()
