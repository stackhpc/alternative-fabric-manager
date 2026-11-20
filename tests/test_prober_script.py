"""The prober's script, run for real by sh against fake smpquery and ibstat

Nothing here can run a real smpquery or ibstat: PATH holds only the fakes
and a directory of links to the few tools the script and the fakes need,
each checked not to be an InfiniBand tool, and every test checks that the
fake is what ran.
"""

import os
import shutil
import subprocess
import time

import pytest

from alternative_fabric_manager import prober
from alternative_fabric_manager.prober import Get

KEY = 0x5EC12E7000C0FFEE
OTHER_KEY = 0x0BADBADBADBADBAD
REFUSED_KEY = 0x00000000DEADBEEF
OWN = 0x0002C903000E0B72

# What smpquery prints, as libibmad's dump functions format it, with the
# M_Key that must never leave the container
FAKE_SMPQUERY = r"""#!/bin/sh
dir=$AFM_FAKE_DIR
echo "start $(date +%s.%N) $$" >> "$dir/times"
echo "$*" >> "$dir/invocations"
pid=$PPID
i=0
while [ "$i" -lt 6 ] && [ "$pid" -gt 1 ]; do
    tr '\0' ' ' < "/proc/$pid/cmdline" >> "$dir/ancestors.$$"
    echo >> "$dir/ancestors.$$"
    pid=$(awk '{ print $4 }' "/proc/$pid/stat")
    i=$((i + 1))
done
[ "$1" = -D ] && [ "$2" = -y ] || { echo "bad arguments" >&2; exit 9; }
key=$3 attr=$4 route=$5 port=${6:-}
echo "ibwarn: [$$] smp_query_via: sending with m_key $key" >&2
case $route in
    0,9) sleep 30 ;;
esac
if [ "$key" = 0x00000000deadbeef ]; then
    echo "ibwarn: [$$] mad_rpc: _do_madrpc failed; dport (DR path $route) $key" >&2
    echo "smpquery: iberror: failed: operation $attr: $attr query failed" >&2
    exit 255
fi
sleep "${AFM_FAKE_SLEEP:-0}"
case $attr in
nodeinfo)
    echo "# Node info: DR path slid 65535; dlid 65535; $route"
    echo "BaseVers:........................1"
    echo "NodeType:........................Channel Adapter"
    echo "NumPorts:........................1"
    echo "SystemGuid:......................0x0002c903000e0b73"
    echo "Guid:............................0x0002c903000e0b70"
    echo "PortGuid:........................0x0002c903000e0b72"
    echo "LocalPort:.......................1"
    echo "VendorId:........................0x0002c9"
    ;;
portinfo)
    echo "# Port info: DR path slid 65535; dlid 65535; $route port ${port:-0}"
    echo "Mkey:............................$key"
    echo "GidPrefix:.......................0xfe80000000000000"
    echo "Lid:.............................1"
    echo "LocalPort:.......................1"
    echo "LinkState:.......................Active"
    echo "PhysLinkState:...................LinkUp"
    echo "ProtectBits:.....................2"
    echo "LinkState:.......................Down"
    echo "NodeType:........................Switch; rm -rf /"
    echo "BEGIN g1"
    echo "END g1 0"
    ;;
esac
echo "end $(date +%s.%N) $$" >> "$dir/times"
"""

FAKE_IBSTAT = """#!/bin/sh
echo ibstat >> "$AFM_FAKE_DIR/invocations"
[ "$1" = -p ] || exit 1
echo 0x0002c903000e0b72
echo 0xffffffffffffffff
echo 0x0000000000000000
"""

# What the script and the fakes run, besides themselves
TOOLS = (
    "sh",
    "timeout",
    "xargs",
    "awk",
    "grep",
    "sleep",
    "date",
    "tr",
    "cat",
)


@pytest.fixture
def fakes(tmp_path):
    return make_fakes(tmp_path)


def make_fakes(tmp_path):
    """The fake tools in tmp_path/bin, and links to the real ones the
    script needs in tmp_path/tools: PATH is only ever those two"""
    fake_bin = tmp_path / "bin"
    tools = tmp_path / "tools"
    fake_bin.mkdir()
    tools.mkdir()
    for name, text in (("smpquery", FAKE_SMPQUERY), ("ibstat", FAKE_IBSTAT)):
        path = fake_bin / name
        path.write_text(text)
        path.chmod(0o755)
    for name in TOOLS:
        real = shutil.which(name, path="/usr/bin:/bin")
        assert real, name
        real = os.path.realpath(real)
        assert not os.path.basename(real).startswith(("smp", "ib", "sa")), real
        os.symlink(real, tools / name)
    return tmp_path


def fake_environment(fakes, script_environment):
    """The script's environment, with PATH holding only the fakes and the
    tools they and the script need"""
    return {
        "PATH": f"{fakes / 'bin'}:{fakes / 'tools'}",
        "AFM_FAKE_DIR": str(fakes),
        "LC_ALL": "C",
        **script_environment,
    }


def run_script(fakes, gets, timeout=5, parallel=4, extra=None, env=None):
    environment = fake_environment(
        fakes, {**prober.environment(gets, timeout, parallel), **(extra or {})}
    )
    if env is not None:
        environment = env
    return subprocess.run(
        [str(fakes / "tools" / "sh"), "-c", prober.SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def invocations(fakes):
    path = fakes / "invocations"
    return path.read_text().splitlines() if path.exists() else []


def gets_for(count, key=KEY, route="0"):
    out = []
    for i in range(count):
        out.append(Get(f"n{i}", "nodeinfo", route, None, key))
        out.append(Get(f"p{i}", "portinfo", route, None, key))
    return out


def test_whitelisted_fields_only_and_never_the_key(fakes):
    gets = gets_for(1) + [Get("s", "portinfo", "0,1", 7, OTHER_KEY)]
    result = run_script(fakes, gets)
    assert result.returncode == 0
    # The fake ran, with each Get's key
    calls = invocations(fakes)
    assert "ibstat" in calls
    assert f"-D -y 0x{KEY:016x} nodeinfo 0" in calls
    assert f"-D -y 0x{OTHER_KEY:016x} portinfo 0,1 7" in calls
    for text in (result.stdout, result.stderr):
        assert "Mkey" not in text
        assert f"{KEY:016x}" not in text.lower()
        assert f"{OTHER_KEY:016x}" not in text.lower()
        assert "ibwarn" not in text
    assert result.stderr == ""
    own, answers = prober.parse(result.stdout, gets)
    assert own == [OWN]
    assert answers["n0"] == {
        "NodeType": "Channel Adapter",
        "NumPorts": "1",
        "Guid": "0x0002c903000e0b70",
        "PortGuid": "0x0002c903000e0b72",
        "LocalPort": "1",
    }
    # The first of a repeated field counts; a value with anything but
    # letters, digits and spaces is dropped, and so are lines that look
    # like the script's own markers.
    assert answers["p0"] == {
        "LocalPort": "1",
        "LinkState": "Active",
        "PhysLinkState": "LinkUp",
        "ProtectBits": "2",
    }


def test_keys_come_only_from_the_environment(fakes):
    gets = gets_for(2)
    result = run_script(fakes, gets)
    assert result.returncode == 0
    assert f"{KEY:016x}" not in prober.SCRIPT
    # A file per fake smpquery: the Gets run in parallel, and lines appended
    # to one file would interleave.
    ancestors = [
        line
        for path in sorted(fakes.glob("ancestors.*"))
        for line in path.read_text().splitlines()
    ]
    assert ancestors
    # smpquery's own command line holds the key, as the spec accepts, and so
    # does timeout's, which runs it; nothing above them does: not the
    # per-Get shell, xargs, nor the script itself.
    above = [line for line in ancestors if not line.startswith("timeout ")]
    assert any(line.startswith("xargs ") for line in above)
    assert any("AFM_PROBES" in line for line in above)  # the script's own sh -c
    for line in above:
        assert f"{KEY:016x}" not in line


def test_a_get_that_times_out_has_no_answer(fakes):
    gets = [
        Get("slow", "nodeinfo", "0,9", None, KEY),
        Get("fast", "nodeinfo", "0", None, KEY),
    ]
    started = time.monotonic()
    result = run_script(fakes, gets, timeout=1)
    assert time.monotonic() - started < 10
    assert result.returncode == 0
    _, answers = prober.parse(result.stdout, gets)
    assert answers["slow"] is None
    assert answers["fast"]["PortGuid"] == "0x0002c903000e0b72"
    assert "END slow 124" in result.stdout or "END slow 137" in result.stdout


def test_a_refused_get_has_no_answer_and_no_fields(fakes):
    gets = [Get("r", "portinfo", "0,1", None, REFUSED_KEY)]
    result = run_script(fakes, gets)
    assert result.returncode == 0
    assert result.stdout.endswith("BEGIN r\nEND r 255\n")
    _, answers = prober.parse(result.stdout, gets)
    assert answers == {"r": None}
    assert "deadbeef" not in result.stdout + result.stderr


def test_parallel_blocks_dont_interleave_and_the_bound_holds(fakes):
    gets = gets_for(12)
    result = run_script(fakes, gets, parallel=3, extra={"AFM_FAKE_SLEEP": "0.2"})
    assert result.returncode == 0
    own, answers = prober.parse(result.stdout, gets)
    assert len(answers) == 24 and all(answers.values())
    # Each start and end, by process: at no instant were more than three
    # Gets running, and some ran at once.
    events = []
    for line in (fakes / "times").read_text().splitlines():
        kind, when, _pid = line.split()
        events.append((float(when), 0 if kind == "end" else 1))
    running = peak = 0
    for _when, delta in sorted(events):
        running += 1 if delta else -1
        peak = max(peak, running)
    assert 1 < peak <= 3


def test_many_parallel_blocks_parse(fakes):
    # bash line-buffers its own printf, so a block written by the shell
    # could interleave with another: this caught that.
    gets = gets_for(40)
    for _ in range(5):
        result = run_script(fakes, gets, parallel=16)
        assert result.returncode == 0
        _, answers = prober.parse(result.stdout, gets)
        assert all(answers.values())


def test_an_empty_plan_gives_only_this_hosts_guids(fakes):
    result = run_script(fakes, [])
    assert result.returncode == 0
    assert result.stdout == "OWN\n0x0002c903000e0b72\n0xffffffffffffffff\n" + (
        "0x0000000000000000\nEND OWN\n"
    )
    assert prober.parse(result.stdout, []) == ([OWN], {})
    assert invocations(fakes) == ["ibstat"]


def _env(fakes, **changes):
    env = {
        "PATH": f"{fakes / 'bin'}:{fakes / 'tools'}",
        "AFM_FAKE_DIR": str(fakes),
        **prober.environment(gets_for(1), 5, 4),
    }
    env.update(changes)
    return {name: value for name, value in env.items() if value is not None}


@pytest.mark.parametrize(
    "changes",
    [
        {"AFM_PROBES": None},
        {"AFM_PROBE_TIMEOUT": "0"},
        {"AFM_PROBE_TIMEOUT": "1; reboot"},
        {"AFM_PROBE_PARALLEL": ""},
        {"AFM_PROBE_PARALLEL": "0"},
        {"AFM_PROBES": "n0 nodeinfo 0 - 0x5ec12e7000c0ffee; reboot"},
        {"AFM_PROBES": "n0 nodeset 0 - 0x5ec12e7000c0ffee"},
        {"AFM_PROBES": "n0 nodeinfo 0 - 0x5EC12E7000C0FFEE"},
        {"AFM_PROBES": "n0 nodeinfo 0 - 0x5ec12e7000c0ffee\nn0 portinfo 0 - 0x0"},
        {"AFM_PROBES": "n0 nodeinfo $(reboot) - 0x5ec12e7000c0ffee"},
    ],
)
def test_a_malformed_plan_runs_nothing(fakes, changes):
    result = run_script(fakes, [], env=_env(fakes, **changes))
    assert result.returncode == 2
    assert result.stdout == ""
    assert invocations(fakes) == []


def test_missing_tools_fail_the_script(fakes):
    (fakes / "bin" / "smpquery").unlink()
    result = run_script(fakes, gets_for(1))
    assert result.returncode == 3
    assert result.stdout == ""


def test_a_failing_ibstat_fails_the_script(fakes):
    (fakes / "bin" / "ibstat").write_text("#!/bin/sh\nexit 1\n")
    result = run_script(fakes, gets_for(1))
    assert result.returncode != 0
    assert invocations(fakes) == []
