"""A stand-in for OpenSM, for the failover tests

It registers itself as a master in a shared fabric directory, counts
sweeps on SIGHUP, and goes NOT-ACTIVE, staying alive, when asked, as an
OpenSM does on SMInfo DISABLE. The tests count the masters there.
"""

import argparse
import json
import os
import signal
import sys
import time


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
    args = parser.parse_args()
    time.sleep(args.start_delay)
    pid = os.getpid()
    registry = os.path.join(args.fabric, f"sm-{pid}.json")
    sweeps = os.path.join(args.fabric, f"sweeps-{pid}")
    asked = os.path.join(args.fabric, f"ask-{args.node}")
    state = {"sweeps": 0}

    def write(path, data):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, path)

    def on_hup(*_):
        state["sweeps"] += 1
        write(sweeps, str(state["sweeps"]))

    signal.signal(signal.SIGHUP, on_hup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    write(
        registry,
        json.dumps({"pid": pid, "node": args.node, "guid": args.guid, "master": True}),
    )
    write(sweeps, "0")
    try:
        while True:
            if os.path.exists(asked) and not args.ignore_ask:
                write(
                    registry,
                    json.dumps(
                        {
                            "pid": pid,
                            "node": args.node,
                            "guid": args.guid,
                            "master": False,
                        }
                    ),
                )
            time.sleep(0.1)
    finally:
        for path in (registry, sweeps):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
