"""A stand-in for OpenSM, for the failover tests

It registers itself as a master in a shared fabric directory, counts
sweeps on SIGHUP, and goes NOT-ACTIVE, staying alive, when asked, as an
OpenSM does on SMInfo DISABLE. The tests count the masters there.

From the moment it starts, before it registers, it also appends a
heartbeat to life-<pid>.log every 0.1 s, with whether it is active (not
disabled), on the system's monotonic clock. A real OpenSM acts on the
fabric from its first sweep, before it is master, and a sampling monitor
can miss a short overlap, so the tests check these logs for any two
OpenSMs active at once (see Cluster.check_lifetimes). A paused fake writes
nothing, as it can't act. With --partitions, each sweep appends the
generation of the partitions file it read to generations-<node>.log.
"""

import argparse
import json
import os
import re
import signal
import sys
import time

HEARTBEAT = 0.1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fabric", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--guid", required=True)
    # Like OpenSM's first sweep: master only after a while
    parser.add_argument("--start-delay", type=float, default=0.0)
    # Like a frozen SM, which can't honour DISABLE: so the tests' monitor
    # sees any OpenSM that stays alive when it shouldn't
    parser.add_argument("--ignore-ask", action="store_true")
    parser.add_argument("--partitions")
    args = parser.parse_args()
    pid = os.getpid()
    registry = os.path.join(args.fabric, f"sm-{pid}.json")
    sweeps = os.path.join(args.fabric, f"sweeps-{pid}")
    asked = os.path.join(args.fabric, f"ask-{args.node}")
    life = open(os.path.join(args.fabric, f"life-{pid}.log"), "a", buffering=1)  # noqa: SIM115
    state = {"sweeps": 0, "active": True}

    def write(path, data):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, path)

    def heartbeat():
        life.write(f"{time.monotonic():.6f} {args.node} {int(state['active'])}\n")

    def record_generation():
        if not args.partitions:
            return
        try:
            with open(args.partitions) as f:
                match = re.search(r"^# Generation (\d+)$", f.read(), re.MULTILINE)
        except OSError:
            match = None
        generation = int(match.group(1)) if match else -1
        with open(os.path.join(args.fabric, f"generations-{args.node}.log"), "a") as f:
            f.write(f"{time.monotonic():.6f} {pid} {generation}\n")

    def on_hup(*_):
        record_generation()
        state["sweeps"] += 1
        write(sweeps, str(state["sweeps"]))

    def register(master):
        write(
            registry,
            json.dumps(
                {"pid": pid, "node": args.node, "guid": args.guid, "master": master}
            ),
        )

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    heartbeat()
    end = time.monotonic() + args.start_delay
    while time.monotonic() < end:
        time.sleep(min(HEARTBEAT, max(0.0, end - time.monotonic())))
        heartbeat()
    # The first sweep reads the partitions file, as OpenSM's does.
    record_generation()
    signal.signal(signal.SIGHUP, on_hup)
    register(True)
    write(sweeps, "0")
    try:
        while True:
            if state["active"] and os.path.exists(asked) and not args.ignore_ask:
                state["active"] = False
                register(False)
            heartbeat()
            time.sleep(HEARTBEAT)
    finally:
        for path in (registry, sweeps):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
