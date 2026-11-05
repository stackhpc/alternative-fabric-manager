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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = config_mod.Config.load(args.config_file)
    except config_mod.ConfigError as e:
        sys.exit(f"alternative-fabric-manager: {e}")

    manager = manager_mod.Manager(
        config,
        db.make_sessionmaker(config.database),
        drivers.make_reloader(config),
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
