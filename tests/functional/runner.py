"""OpenSM as processes under the real watchdog, for the failover tests

It is both the manager's driver (start, running, reload, fabric, stop) and
the coordinator's runner (prepare, write_lease, stop, verify_stopped, ask).
The "OpenSM" is fake_opensm.py, which registers itself in a shared fabric
directory, where the tests count the masters.
"""

import json
import os
import signal
import subprocess
import sys
import time

from alternative_fabric_manager.drivers import Fabric, ReloadError, SweepReport
from alternative_fabric_manager.ha import watchdog as wd

FAKE = os.path.join(os.path.dirname(__file__), "fake_opensm.py")
SWITCH = 0x1


def guid_of(node):
    return 0x1000 + int(node.rsplit("-", 1)[-1])


def process_state(pid):
    """A process's state letter, or None if it is gone"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def masters(fabric):
    """The fake SMs that can act as master now: alive, not stopped, and not
    disabled. A paused (stopped) one can't act until it resumes."""
    found = []
    for name in os.listdir(fabric):
        if not name.startswith("sm-") or not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(fabric, name)) as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        state = process_state(entry["pid"])
        if state is None or state in ("Z", "X"):
            continue
        if entry.get("master") and state not in ("T", "t"):
            found.append(entry)
    return found


class ProcessRunner:
    def __init__(self, config, fabric):
        self.config = config
        self.fabric_dir = fabric
        self.state_dir = config.ha_state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        self.node = config.node
        self.guid = guid_of(self.node)
        self.watchdog = None
        self._start = None

    # The coordinator's runner

    def prepare(self, instance, term, deadline):
        for name in (wd.STOP,):
            try:
                os.unlink(os.path.join(self.state_dir, name))
            except OSError:
                pass
        try:
            os.unlink(os.path.join(self.fabric_dir, f"ask-{self.node}"))
        except OSError:
            pass
        self._start = (instance, term, deadline)

    def write_lease(self, instance, term, deadline):
        wd.write_atomically(
            self.state_dir,
            wd.LEASE,
            {
                "boot_id": wd.boot_id(),
                "instance": instance,
                "term": term,
                "deadline": deadline,
                "held": deadline is None,
            },
        )

    def _own_sms(self):
        pids = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().split(b"\0")
            except OSError:
                continue
            # The fake itself, python fake_opensm.py ..., not the watchdog,
            # whose arguments name it too
            if (
                len(cmdline) > 1
                and cmdline[1].endswith(b"fake_opensm.py")
                and self.node.encode() in cmdline
            ):
                state = process_state(int(pid))
                if state not in (None, "Z", "X"):
                    pids.append(int(pid))
        return pids

    def verify_stopped(self):
        return not self._own_sms()

    def ask(self, node):
        with open(os.path.join(self.fabric_dir, f"ask-{node}"), "w") as f:
            f.write("disable")

    # The manager's driver

    def start(self):
        instance, term, deadline = self._start
        hashes = {
            path: wd.sha256(path)
            for path in (self.config.partitions_file, self.config.opensm_conf_file)
            if os.path.exists(path)
        }
        wd.write_atomically(
            self.state_dir,
            wd.START,
            {
                "boot_id": wd.boot_id(),
                "instance": instance,
                "term": term,
                "deadline": deadline,
                "hashes": hashes,
            },
        )
        self.watchdog = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "alternative_fabric_manager.ha.watchdog",
                "--state-dir",
                self.state_dir,
                "--die-with",
                str(os.getpid()),
                "--",
                sys.executable,
                FAKE,
                "--fabric",
                self.fabric_dir,
                "--node",
                self.node,
                "--guid",
                str(self.guid),
                "--start-delay",
                os.environ.get("AFM_FAKE_START_DELAY", "1.5"),
                *(
                    ["--ignore-ask"]
                    if os.environ.get("AFM_FAKE_HONOURS_ASK") != "1"
                    else []
                ),
            ],
            start_new_session=True,
        )
        end = time.monotonic() + self.config.start_timeout
        while time.monotonic() < end:
            if self._own_sms() and self._sweeps() is not None:
                return SweepReport()
            if self.watchdog.poll() is not None:
                raise ReloadError("The watchdog refused to start OpenSM")
            time.sleep(0.1)
        raise ReloadError("OpenSM didn't start")

    def _sweeps(self):
        pids = self._own_sms()
        if not pids:
            return None
        try:
            with open(os.path.join(self.fabric_dir, f"sweeps-{pids[0]}")) as f:
                return int(f.read() or 0)
        except (OSError, ValueError):
            return None

    def running(self):
        return (
            self.watchdog is not None
            and self.watchdog.poll() is None
            and bool(self._own_sms())
        )

    def reload(self):
        pids = self._own_sms()
        if not pids:
            raise ReloadError("OpenSM isn't running")
        before = self._sweeps() or 0
        os.kill(pids[0], signal.SIGHUP)
        end = time.monotonic() + self.config.sweep_timeout
        while time.monotonic() < end:
            if (self._sweeps() or 0) > before:
                return SweepReport()
            time.sleep(0.05)
        raise ReloadError("No sweep")

    def fabric(self, m_key=None, sa_key=None):
        return Fabric(
            {SWITCH},
            [(int(entry["guid"]), 3) for entry in masters(self.fabric_dir)],
            {self.guid},
        )

    def stop(self):
        with open(os.path.join(self.state_dir, wd.STOP), "w") as f:
            f.write("stop")
        if self.watchdog is not None:
            try:
                self.watchdog.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.watchdog.pid, signal.SIGKILL)
                self.watchdog.wait()
        # One left by an earlier instance of this node
        for pid in self._own_sms():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        end = time.monotonic() + 5
        while self._own_sms() and time.monotonic() < end:
            time.sleep(0.05)
