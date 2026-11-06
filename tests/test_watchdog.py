"""The watchdog runs OpenSM only under its lease (see specs/ha.md)"""

import threading
import time

from alternative_fabric_manager.ha import watchdog as wd


def _lease(directory, seconds=5.0, term=1):
    deadline = None if seconds is None else time.monotonic() + seconds
    wd.write_atomically(
        directory,
        wd.LEASE,
        {"boot_id": wd.boot_id(), "instance": "u", "term": term, "deadline": deadline},
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
            "hashes": hashes or {},
        },
    )


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
