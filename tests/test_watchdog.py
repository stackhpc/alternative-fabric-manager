"""The watchdog runs OpenSM only under its lease (see specs/ha.md)"""

import threading
import time

from alternative_fabric_manager.ha import watchdog as wd


def _lease(directory, seconds=5.0, term=1):
    deadline = None if seconds is None else time.monotonic() + seconds
    wd.write_atomically(
        directory,
        wd.LEASE,
        {
            "boot_id": wd.boot_id(),
            "instance": "u",
            "term": term,
            "deadline": deadline,
            "held": seconds is None,
        },
    )


def _start(directory, term=1, hashes=None, seconds=5.0):
    wd.write_atomically(
        directory,
        wd.START,
        {
            "boot_id": wd.boot_id(),
            "instance": "u",
            "term": term,
            "deadline": time.monotonic() + seconds,
            "hashes": hashes or {str(_conf(directory)): wd.sha256(_conf(directory))},
        },
    )


def _conf(directory):
    path = directory / "opensm.conf"
    if not path.exists():
        path.write_text("conf")
    return path


def _run(directory, wait=2.0):
    dog = wd.Watchdog(str(directory), ["sleep", "60"])
    result = {}
    thread = threading.Thread(target=lambda: result.update(rc=dog.run(wait)))
    thread.start()
    return dog, thread, result


def _started(dog, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if dog.process is not None:
            return True
        time.sleep(0.05)
    return False


def test_opensm_dies_when_its_lease_lapses(tmp_path):
    _lease(tmp_path, seconds=1.0)
    _start(tmp_path)
    dog, thread, result = _run(tmp_path)
    assert _started(dog)
    # The start request was consumed.
    assert not (tmp_path / wd.START).exists()
    thread.join(5)
    assert not thread.is_alive()
    assert dog.process.poll() is not None
    assert result["rc"] == 0


def test_renewal_keeps_it_running_and_stop_kills_it(tmp_path):
    _lease(tmp_path, seconds=0.6)
    _start(tmp_path)
    dog, thread, _ = _run(tmp_path)
    assert _started(dog)
    for _ in range(5):
        time.sleep(0.3)
        _lease(tmp_path, seconds=0.6)
    assert dog.process.poll() is None
    (tmp_path / wd.STOP).write_text("")
    thread.join(5)
    assert dog.process.poll() is not None


def test_a_held_lease_has_no_deadline(tmp_path):
    _lease(tmp_path, seconds=None)
    _start(tmp_path)
    dog, thread, _ = _run(tmp_path)
    assert _started(dog)
    time.sleep(1.0)
    assert dog.process.poll() is None
    dog.stopping = True
    thread.join(5)
    assert dog.process.poll() is not None


def test_start_requests_for_another_term_or_files_are_refused(tmp_path):
    config = tmp_path / "opensm.conf"
    config.write_text("a")
    _lease(tmp_path, term=2)
    for request in (
        dict(term=1),
        dict(term=2, hashes={str(config): "0" * 64}),
        dict(term=2, seconds=-1),
    ):
        _start(tmp_path, **request)
        dog = wd.Watchdog(str(tmp_path), ["sleep", "60"])
        assert dog.run(1.0) == 1
        assert dog.process is None


def test_a_matching_request_with_hashes_starts(tmp_path):
    config = tmp_path / "opensm.conf"
    config.write_text("a")
    _lease(tmp_path, seconds=0.5)
    _start(tmp_path, hashes={str(config): wd.sha256(config)})
    dog, thread, _ = _run(tmp_path)
    assert _started(dog)
    thread.join(5)


def test_no_request_no_opensm(tmp_path):
    _lease(tmp_path)
    dog = wd.Watchdog(str(tmp_path), ["sleep", "60"])
    assert dog.run(0.5) == 1
    assert dog.process is None


def test_opensm_dies_with_its_manager(tmp_path):
    import subprocess

    manager = subprocess.Popen(["sleep", "60"])
    _lease(tmp_path, seconds=30)
    _start(tmp_path)
    dog = wd.Watchdog(str(tmp_path), ["sleep", "60"], die_with=manager.pid)
    thread = threading.Thread(target=dog.run, args=(2.0,))
    thread.start()
    assert _started(dog)
    manager.kill()
    manager.wait()
    thread.join(5)
    assert dog.process.poll() is not None


def test_missing_fields_are_no_permission(tmp_path):
    # A lease without a deadline that doesn't say it's held
    wd.write_atomically(
        tmp_path, wd.LEASE, {"boot_id": wd.boot_id(), "instance": "u", "term": 1}
    )
    assert not wd.lease_valid(wd._read(tmp_path, wd.LEASE))
    _lease(tmp_path)
    lease = wd._read(tmp_path, wd.LEASE)
    base = {"boot_id": wd.boot_id(), "instance": "u", "term": 1}
    # No hashes
    assert not wd.start_allowed(lease, dict(base, deadline=time.monotonic() + 5))[0]
    # No deadline, with a lease that isn't held
    hashes = {str(_conf(tmp_path)): wd.sha256(_conf(tmp_path))}
    assert not wd.start_allowed(lease, dict(base, hashes=hashes))[0]
    assert wd.start_allowed(
        lease, dict(base, hashes=hashes, deadline=time.monotonic() + 5)
    )[0]


def test_the_kill_reaches_a_wrappers_children(tmp_path):
    import os

    marker = tmp_path / "child.pid"
    _lease(tmp_path, seconds=30)
    _start(tmp_path)
    # A wrapper that doesn't exec, as an image's entrypoint might be
    dog = wd.Watchdog(
        str(tmp_path), ["sh", "-c", f"sleep 60 & echo $! > {marker}; wait"]
    )
    thread = threading.Thread(target=dog.run, args=(2.0,))
    thread.start()
    assert _started(dog)
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    child = int(marker.read_text())
    (tmp_path / wd.STOP).write_text("")
    thread.join(5)
    time.sleep(0.2)
    try:
        os.kill(child, 0)
        with open(f"/proc/{child}/stat") as f:
            alive = f.read().split()[2] != "Z"
    except ProcessLookupError:
        alive = False
    assert not alive


def test_sighup_reaches_opensm_and_doesnt_end_the_watchdog(tmp_path):
    import signal
    import subprocess
    import sys

    marker = tmp_path / "hups"
    script = tmp_path / "opensm.py"
    script.write_text(
        "import signal, time\n"
        f"path = {str(marker)!r}\n"
        "signal.signal(signal.SIGHUP, lambda *_: open(path, 'a').write('hup\\n'))\n"
        "while True:\n    time.sleep(0.1)\n"
    )
    _lease(tmp_path, seconds=30)
    _start(tmp_path)
    dog = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alternative_fabric_manager.ha.watchdog",
            "--state-dir",
            str(tmp_path),
            "--",
            sys.executable,
            str(script),
        ]
    )
    try:
        for _ in range(50):
            if not (tmp_path / wd.START).exists():
                break
            time.sleep(0.1)
        time.sleep(0.5)
        dog.send_signal(signal.SIGHUP)
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.read_text() == "hup\n"
        assert dog.poll() is None
    finally:
        (tmp_path / wd.STOP).write_text("")
        dog.wait(5)
