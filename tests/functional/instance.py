"""One fabric manager instance in HA mode, for the failover tests

Run as a process: python -m tests.functional.instance <json settings>. Its
OpenSM is fake_opensm.py, under the real watchdog. SIGTERM steps it down.
"""

import json
import logging
import os
import sys
import threading

import uvicorn

from alternative_fabric_manager import api, db
from alternative_fabric_manager import config as config_mod
from alternative_fabric_manager import manager as manager_mod
from alternative_fabric_manager.ha import coordinator as coordinator_mod
from alternative_fabric_manager.ha import lease as lease_mod
from tests.functional.runner import ProcessRunner


def main():
    settings = json.loads(sys.argv[1])
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s {settings['node']} %(levelname)s %(name)s: %(message)s",
    )
    config = config_mod.Config(
        token=settings["token"],
        port=settings["port"],
        connection=settings["connection"],
        driver="managed",
        config_dir=settings["config_dir"],
        ha=True,
        node=settings["node"],
        ha_state_dir=settings["state_dir"],
        **settings["timings"],
    )
    config.validate()
    os.makedirs(config.config_dir, exist_ok=True)
    sessionmaker = db.make_sessionmaker(config.connection, config.call_timeout)
    runner = ProcessRunner(config, settings["fabric"])
    manager = manager_mod.Manager(config, sessionmaker, runner)
    lease = lease_mod.GaleraLease(sessionmaker)
    coordinator = coordinator_mod.Coordinator(config, lease, runner, manager)
    app = api.create_app(config, manager, coordinator)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Step down before uvicorn stops, since it raises the signal
            # again once it has, which ends the process.
            coordinator.shutdown()
            super().handle_exit(sig, frame)

    server = Server(
        uvicorn.Config(app, host="127.0.0.1", port=config.port, log_level="warning")
    )
    threading.Thread(target=coordinator.start, daemon=True).start()
    server.run()
    logging.getLogger(__name__).warning("Stopped")
    os._exit(0)


if __name__ == "__main__":
    main()
