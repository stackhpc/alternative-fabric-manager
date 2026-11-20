"""Per-port M_Keys' rules (specs/secured-profile.md, Per-port M_Keys)

Every key here is an obviously fake small number.
"""

import datetime
import hashlib
import inspect
import random
import re
import struct

import pytest

from alternative_fabric_manager import db, port_keys
from alternative_fabric_manager.port_keys import DERIVED, M_KEY, UNKNOWN, Link
from alternative_fabric_manager.port_keys import PortState as Port

M = 0x1234  # the stored m_key
BASE = 0xBB  # the per-port M_Key base
T0 = datetime.datetime(2026, 11, 16, 10, 0, tzinfo=datetime.timezone.utc)
T1 = T0 + datetime.timedelta(minutes=1)
T2 = T0 + datetime.timedelta(minutes=2)

HOST = 0xA1  # this host's port
S1, S2, S3 = 0x51, 0x52, 0x53  # switches' port 0 GUIDs
X, Y = 0xB1, 0xB2  # other hosts' ports


def _reference_key(base, guid):
    """The derivation, computed another way: SHA-1 of the two big-endian
    64-bit values, the digest padded to 24 bytes and its three 64-bit words
    XORed, which is folding byte i into byte i mod 8"""
    # SHA-1 is OpenSM's choice, which the keys must match.
    digest = hashlib.sha1(struct.pack(">QQ", base, guid)).digest()  # noqa: S324
    digest += bytes(4)
    a, b, c = struct.unpack(">QQQ", digest)
    return a ^ b ^ c


def _derived(guid):
    return _reference_key(BASE, guid)


def _ports(*ports):
    return {port.guid: port for port in ports}


def _links(*links):
    return {link.key: link for link in links}


def _both(guid, port, peer, peer_port, last_seen=None):
    """Both directions of a link, as neighbors has them"""
    return [
        Link(guid, port, peer, peer_port, last_seen),
        Link(peer, peer_port, guid, port, last_seen),
    ]


# The derivation


@pytest.mark.parametrize(
    "base, guid, key",
    [
        # Computed apart from the module, as _reference_key does
        (0x1, 0x2, 0x2A8EDCD3A893A40F),
        (0xBB, 0x0002C90300001234, 0x7C9DB8B30C1C9B77),
        (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF, 0x844B148325EAA4A3),
        (0x1234, 0x1, 0x55BC524453AB1D20),
    ],
)
def test_port_m_key_is_opensms_derivation(base, guid, key):
    # osm_subn_calc_mkey, from OpenSM 5.17.2's disassembly, which test 5
    # confirmed on every port (How OpenSM derives the keys)
    assert port_keys.port_m_key(base, guid) == key


def test_port_m_key_matches_an_independent_fold():
    # Seeded test inputs, not keys
    rng = random.Random(5)  # noqa: S311
    for _ in range(200):
        base, guid = rng.getrandbits(64), rng.getrandbits(64) or 1
        assert port_keys.port_m_key(base, guid) == _reference_key(base, guid)


def test_a_zero_guid_gets_0():
    # As OpenSM gives it
    assert port_keys.port_m_key(BASE, 0) == 0


def test_the_fingerprint_is_sha256_of_a_label_and_the_key():
    # The state table holds it to notice a restored base that differs from
    # the one the ports were keyed from, never the key itself.
    value = port_keys.fingerprint(BASE)
    assert (
        value
        == hashlib.sha256(
            port_keys._FINGERPRINT_LABEL + BASE.to_bytes(8, "big")
        ).hexdigest()
    )
    assert port_keys._FINGERPRINT_LABEL
    assert value != hashlib.sha256(BASE.to_bytes(8, "big")).hexdigest()
    assert len(value) == 64 and f"{BASE:x}" not in value
    assert port_keys.fingerprint(BASE + 1) != value


# Conversions at the edge


def test_keys_convert_at_the_edge():
    assert port_keys.parse_key("0x0000000000001234") == M
    assert port_keys.format_key(M) == "0x0000000000001234"
    assert port_keys.guid_text(X) == "00000000000000b1"
    assert port_keys.parse_guid("00000000000000B1") == X


@pytest.mark.parametrize("text", ["0xnotakey77", "0x1ffffffffffffffff", None])
def test_a_bad_key_raises_without_its_text(text):
    with pytest.raises(ValueError) as e:
        port_keys.parse_key(text)
    assert "77" not in str(e.value) and "1ff" not in str(e.value)
    # Nor chained from int()'s error, whose message quotes the text
    assert e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__


def test_key_entries_show_no_key():
    entries = port_keys.KeyEntries({X: 0x777777})
    assert entries[X] == 0x777777
    for text in (repr(entries), str(entries), f"{entries}"):
        assert "777777" not in text and str(0x777777) not in text
        assert "1 GUIDs" in text


def test_the_module_never_logs():
    # A log line could carry a key (The design, Logs).
    source = inspect.getsource(port_keys)
    assert "import logging" not in source and "LOG." not in source
    assert not re.search(r"\bprint\(", source)


# OpenSM's cache files


def test_guid2mkey_parses_as_opensm_writes_it():
    # One pair per line, a blank line after each, hex in either case and
    # unpadded
    text = "0x00000000000000b1 0x0000000000001234\n\n0XB2 0xABC\n\n\n"
    assert dict(port_keys.parse_guid2mkey(text)) == {X: M, Y: 0xABC}
    assert dict(port_keys.parse_guid2mkey("")) == {}


@pytest.mark.parametrize(
    "line",
    [
        "0xb1 0x777777 extra",
        "0xb1",
        "b1 0x777777",
        "0xb1 777777",
        "0xb1 0x77777777777777777",
        "0xb1:1 0x777777",
    ],
)
def test_a_malformed_guid2mkey_raises_without_the_line(line):
    # The line may hold a key; a bad read leaves the rows as they were.
    with pytest.raises(ValueError, match="line 3") as e:
        port_keys.parse_guid2mkey("0xb2 0x1\n\n" + line + "\n")
    assert "777777" not in str(e.value)


def test_guid2mkey_with_two_keys_for_a_guid_raises():
    with pytest.raises(ValueError, match="two keys") as e:
        port_keys.parse_guid2mkey("0xb1 0x777777\n0xb1 0x666666\n")
    assert "777777" not in str(e.value) and "666666" not in str(e.value)


def test_neighbors_parses_both_directions():
    text = (
        "0x00000000000000a1:1 0x0000000000000051:1\n\n0X51:1 0xA1:1\n0x51:12 0xb1:1\n"
    )
    assert port_keys.parse_neighbors(text) == [
        Link(HOST, 1, S1, 1),
        Link(S1, 1, HOST, 1),
        Link(S1, 12, X, 1),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "0xa1:1",
        "0xa1:1 0x51",
        "0xa1 0x51:1",
        "0xa1:x 0x51:1",
        "0xa1:1 0x51:1 0x52:1",
        "a1:1 0x51:1",
        "0xa1:255 0x51:1",
        "0xa1:1 0x51:-1",
    ],
)
def test_a_malformed_neighbors_raises(line):
    with pytest.raises(ValueError, match="line 2"):
        port_keys.parse_neighbors("0x51:1 0xa1:1\n" + line + "\n")


def test_neighbors_with_two_peers_for_a_port_raises():
    with pytest.raises(ValueError, match="two peers"):
        port_keys.parse_neighbors("0xa1:1 0x51:1\n0xa1:1 0x52:1\n")


def test_guid2mkey_renders_as_opensm_writes_it():
    # 16 lower-case hex digits, sorted, a blank line after each entry
    entries = port_keys.KeyEntries({Y: 0xABC, X: M})
    text = port_keys.render_guid2mkey(entries)
    assert text == (
        "0x00000000000000b1 0x0000000000001234\n\n"
        "0x00000000000000b2 0x0000000000000abc\n\n"
    )
    assert port_keys.parse_guid2mkey(text) == entries
    assert port_keys.render_guid2mkey({}) == ""


def test_neighbors_renders_every_link_sorted():
    links = _links(*_both(S1, 3, X, 1), *_both(HOST, 1, S1, 1, T0))
    text = port_keys.render_neighbors(links)
    assert text == (
        "0x0000000000000051:1 0x00000000000000a1:1\n"
        "0x0000000000000051:3 0x00000000000000b1:1\n"
        "0x00000000000000a1:1 0x0000000000000051:1\n"
        "0x00000000000000b1:1 0x0000000000000051:3\n"
    )
    assert port_keys.parse_neighbors(text) == sorted(
        (
            Link(link.guid, link.port, link.peer_guid, link.peer_port)
            for link in links.values()
        ),
        key=lambda link: link.key,
    )


# Classifying an entry


@pytest.mark.parametrize(
    "entry, base, holds",
    [
        (M, BASE, M_KEY),
        (_derived(X), BASE, DERIVED),
        # Another port's derived key isn't this one's
        (_derived(Y), BASE, UNKNOWN),
        (0x999, BASE, UNKNOWN),
        (None, BASE, UNKNOWN),
        # Nothing derives without a base
        (_derived(X), None, UNKNOWN),
        (M, None, M_KEY),
    ],
)
def test_classify_compares_with_both_candidates(entry, base, holds):
    assert port_keys.classify(X, entry, M, base) == holds


def test_a_port_state_holds_only_known_values():
    with pytest.raises(ValueError):
        Port(X, holds="0x1234")


# Folding at a fabric check


def _fold_check(ports, links, neighbors, guid2mkey, now=T1):
    return port_keys.fold_check(
        ports, links, port_keys.parse_neighbors(neighbors), guid2mkey, M, BASE, now
    )


def test_check_each_neighbors_line_replaces_its_local_ports_link():
    # A re-cabled port, as OpenSM saw it (Reading OpenSM's cache)
    links = _links(*_both(S1, 3, X, 1, T0))
    _, folded, summary = _fold_check({}, links, "0x51:3 0xb2:1\n0xb2:1 0x51:3\n", {})
    assert folded[(S1, 3)] == Link(S1, 3, Y, 1, T1)
    assert folded[(Y, 1)] == Link(Y, 1, S1, 3, T1)
    # X's own line wasn't in the file, and its row pointed back to the
    # re-cabled switch port, so it goes: a half-link is never followed.
    assert (X, 1) not in folded
    assert (summary.links_added, summary.links_changed) == (1, 1)


def test_check_gives_every_listed_guid_a_row():
    ports, _, summary = _fold_check({}, {}, "0x51:3 0xb1:1\n", {})
    assert set(ports) == {S1, X}
    assert ports[X] == Port(X, holds=UNKNOWN, last_seen=T1)
    assert summary.ports_added == 2


def test_check_records_only_guids_neighbors_lists():
    # A virtual function appears only in guid2mkey: it never gets a row, so
    # they don't accumulate (requirement 2).
    vf = 0xF1
    ports, _, _ = _fold_check({}, {}, "0x51:3 0xb1:1\n", {vf: M, X: M})
    assert vf not in ports


@pytest.mark.parametrize(
    "entry, holds",
    [(M, M_KEY), (_derived(X), DERIVED), (0x999, UNKNOWN)],
)
def test_check_takes_holds_from_the_entry(entry, holds):
    for before in (M_KEY, DERIVED, UNKNOWN):
        ports = _ports(Port(X, holds=before, last_seen=T0))
        folded, _, _ = _fold_check(ports, {}, "0x51:3 0xb1:1\n", {X: entry})
        assert folded[X].holds == holds
        assert folded[X].last_seen == T1


@pytest.mark.parametrize("before", [M_KEY, DERIVED, UNKNOWN])
def test_check_without_an_entry_keeps_what_a_row_holds(before):
    # A port OpenSM lost still holds its key: forgetting that it holds m_key
    # would lose it for good when it returns.
    ports = _ports(Port(X, holds=before, last_seen=T0))
    folded, _, summary = _fold_check(ports, {}, "0x51:3 0xb1:1\n", {})
    assert folded[X].holds == before
    assert summary.ports_changed == 0


def test_check_a_new_row_without_an_entry_is_unknown():
    folded, _, _ = _fold_check({}, {}, "0x51:3 0xb1:1\n", {})
    assert folded[X].holds == UNKNOWN


def test_check_keeps_rows_and_links_absent_from_the_files():
    # A port that's down now may come back holding its key.
    ports = _ports(Port(Y, holds=M_KEY, reachable=True, last_seen=T0))
    links = _links(*_both(S1, 4, Y, 1, T0))
    folded_ports, folded_links, _ = _fold_check(ports, links, "0x51:3 0xb1:1\n", {})
    assert folded_ports[Y] == ports[Y]
    assert folded_links[(S1, 4)] == links[(S1, 4)]
    assert folded_links[(Y, 1)] == links[(Y, 1)]


def test_check_an_unlisted_rows_entry_still_says_what_it_holds():
    # OpenSM keeps the key it last gave a port it has lost; the row keeps its
    # last_seen, since the entry doesn't show the port is present.
    ports = _ports(Port(Y, holds=M_KEY, last_seen=T0))
    folded, _, summary = _fold_check(ports, {}, "0x51:3 0xb1:1\n", {Y: _derived(Y)})
    assert folded[Y].holds == DERIVED
    assert folded[Y].last_seen == T0
    assert summary.ports_changed == 1


def test_check_keeps_what_a_verify_recorded():
    # node_guid, switch, lapsed, reachable and released are the prober's.
    ports = _ports(
        Port(S1, node_guid=S1, switch=True, holds=M_KEY, lapsed=True, reachable=True)
    )
    folded, _, _ = _fold_check(ports, {}, "0x51:3 0xb1:1\n", {S1: _derived(S1)})
    assert folded[S1] == Port(
        S1,
        node_guid=S1,
        switch=True,
        holds=DERIVED,
        lapsed=True,
        reachable=True,
        last_seen=T1,
    )


def test_a_fold_leaves_its_arguments_as_they_were():
    ports = _ports(Port(X, holds=M_KEY, last_seen=T0))
    links = _links(*_both(S1, 3, X, 1, T0))
    before = (dict(ports), dict(links))
    _fold_check(ports, links, "0x51:3 0xb2:1\n", {X: _derived(X)})
    assert (ports, links) == before


def test_a_fold_summary_holds_no_key():
    _, _, summary = _fold_check({}, {}, "0x51:3 0xb1:1\n", {X: 0x777777})
    assert "777777" not in str(summary) and "777777" not in repr(summary)
    assert "2 ports added" in str(summary)


# Folding before a start


def _fold_start(ports, links, neighbors, guid2mkey, neighbors_time, keys_time):
    return port_keys.fold_start(
        ports,
        links,
        port_keys.parse_neighbors(neighbors),
        guid2mkey,
        M,
        BASE,
        neighbors_time,
        keys_time,
    )


def test_start_a_newer_file_wins():
    # After a crash within a check interval of a new port being keyed
    ports = _ports(Port(X, holds=M_KEY, last_seen=T0))
    links = _links(Link(S1, 3, Y, 1, T0))
    folded_ports, folded_links, _ = _fold_start(
        ports, links, "0x51:3 0xb1:1\n", {X: _derived(X)}, T1, T1
    )
    assert folded_ports[X].holds == DERIVED
    assert folded_ports[X].last_seen == T1
    assert folded_links[(S1, 3)] == Link(S1, 3, X, 1, T1)


def test_start_newer_rows_win():
    # After a lost volume, or a verify since OpenSM last wrote its cache
    ports = _ports(Port(X, holds=DERIVED, last_seen=T2))
    links = _links(Link(S1, 3, X, 1, T2))
    folded_ports, folded_links, summary = _fold_start(
        ports, links, "0x51:3 0xb2:1\n", {X: M}, T1, T1
    )
    assert folded_ports[X] == ports[X]
    assert folded_links[(S1, 3)] == links[(S1, 3)]
    assert (summary.ports_changed, summary.links_changed) == (0, 0)


def test_start_a_row_wins_a_tie():
    ports = _ports(Port(X, holds=DERIVED, last_seen=T1))
    folded, _, _ = _fold_start(ports, {}, "0x51:3 0xb1:1\n", {X: M}, T1, T1)
    assert folded[X].holds == DERIVED


def test_start_a_row_without_last_seen_loses():
    ports = _ports(Port(X, holds=M_KEY))
    links = _links(Link(S1, 3, Y, 1))
    folded_ports, folded_links, _ = _fold_start(
        ports, links, "0x51:3 0xb1:1\n", {X: _derived(X)}, T0, T0
    )
    assert folded_ports[X].holds == DERIVED
    assert folded_links[(S1, 3)].peer_guid == X


def test_start_compares_holds_with_guid2mkeys_time_and_links_with_neighbors():
    ports = _ports(Port(X, holds=M_KEY, last_seen=T1))
    links = _links(Link(S1, 3, Y, 1, T1))
    # neighbors older than the rows, guid2mkey newer
    folded_ports, folded_links, _ = _fold_start(
        ports, links, "0x51:3 0xb1:1\n", {X: _derived(X)}, T0, T2
    )
    assert folded_ports[X].holds == DERIVED
    assert folded_ports[X].last_seen == T1
    assert folded_links[(S1, 3)].peer_guid == Y


def test_start_keeps_or_adds_what_only_one_side_has():
    ports = _ports(Port(Y, holds=DERIVED, last_seen=T0))
    links = _links(Link(S1, 4, Y, 1, T0))
    folded_ports, folded_links, summary = _fold_start(
        ports, links, "0x51:3 0xb1:1\n", {X: M}, T1, T1
    )
    assert folded_ports[Y] == ports[Y]
    assert folded_links[(S1, 4)] == links[(S1, 4)]
    assert folded_ports[X] == Port(X, holds=M_KEY, last_seen=T1)
    assert folded_links[(S1, 3)] == Link(S1, 3, X, 1, T1)
    assert summary.ports_added == 2 and summary.links_added == 1


def test_start_without_an_entry_keeps_what_a_row_holds():
    ports = _ports(Port(X, holds=M_KEY, last_seen=T0))
    folded, _, _ = _fold_start(ports, {}, "0x51:3 0xb1:1\n", {}, T1, T1)
    assert folded[X].holds == M_KEY
    assert folded[X].last_seen == T1


# Restores


def test_restore_entries_that_are_neither_key_are_a_problem():
    # A database restored from before the ports were keyed (Restores)
    entries = port_keys.KeyEntries({X: 0x777777, Y: M})
    problem = port_keys.restore_problem(entries, M, BASE, None)
    assert "0x00000000000000b1" in problem
    assert "777777" not in problem


def test_restore_without_a_base_a_derived_entry_is_a_problem():
    entries = {X: _derived(X)}
    assert port_keys.restore_problem(entries, M, None, None)


def test_restore_a_base_that_doesnt_match_its_fingerprint_is_a_problem():
    recorded = port_keys.fingerprint(BASE + 1)
    problem = port_keys.restore_problem({}, M, BASE, recorded)
    assert "fingerprint" in problem
    assert port_keys.restore_problem({}, M, None, recorded)


@pytest.mark.parametrize("recorded", [None, port_keys.fingerprint(BASE)])
def test_restore_m_key_and_derived_entries_are_no_problem(recorded):
    entries = {X: M, Y: _derived(Y), S1: _derived(S1)}
    assert port_keys.restore_problem(entries, M, BASE, recorded) is None


def test_restore_problem_names_at_most_five_guids():
    entries = {guid: 0x777777 for guid in range(1, 9)}
    problem = port_keys.restore_problem(entries, M, BASE, None)
    assert "8 ports" in problem and "3 more" in problem


@pytest.mark.parametrize(
    "group_named, target_on, holds, adopt",
    [
        # A restored database that predates the turn-on, with the ports on
        # the base's derived keys: adopt rather than re-key back to m_key.
        (True, False, DERIVED, True),
        (True, False, M_KEY, False),
        (True, False, UNKNOWN, False),
        # Already on
        (True, True, DERIVED, False),
        # Without the group it's an ordinary turn-off.
        (False, False, DERIVED, False),
    ],
)
def test_should_adopt_only_a_named_groups_derived_rows(
    group_named, target_on, holds, adopt
):
    ports = _ports(Port(X, holds=M_KEY), Port(Y, holds=holds))
    assert port_keys.should_adopt(ports, group_named, target_on, None) is adopt


def test_should_adopt_never_with_a_fingerprint():
    # The database has seen a turn-on: derived rows are a turn-off's
    # leftovers, such as a node that was down, not a restore.
    ports = _ports(Port(X, holds=DERIVED))
    assert not port_keys.should_adopt(ports, True, False, port_keys.fingerprint(1))


# The prober's keys


@pytest.mark.parametrize(
    "ports, recorded, volume, nothing",
    [
        ({}, None, {}, True),
        (_ports(Port(X, holds=M_KEY), Port(Y, holds=UNKNOWN)), None, [M_KEY], True),
        # A fingerprint means ports were keyed from the base.
        ({}, port_keys.fingerprint(BASE), {}, False),
        (_ports(Port(X, holds=DERIVED)), None, {}, False),
        ({}, None, {X: DERIVED}, False),
        ({}, None, [UNKNOWN, DERIVED], False),
    ],
)
def test_nothing_derived_needs_no_fingerprint_row_or_volume_entry(
    ports, recorded, volume, nothing
):
    assert port_keys.nothing_derived(ports, recorded, volume) is nothing


# Every combination of holds, target, infrastructure and nothing derived
# (The prober): infrastructure gets both, the recorded key first; otherwise
# derived iff the target is on or the row says derived, m_key iff the row
# says m_key, the target is off, or nothing can hold a derived key.
KEYS_TO_TRY = [
    # holds, target_on, infrastructure, nothing_derived, keys
    *[
        (holds, target, True, nothing, keys)
        for holds, keys in (
            (M_KEY, (M_KEY, DERIVED)),
            (DERIVED, (DERIVED, M_KEY)),
            (UNKNOWN, (DERIVED, M_KEY)),
        )
        for target in (True, False)
        for nothing in (True, False)
        if (target, nothing) != (True, False) or holds == M_KEY
    ],
    # A switch, with the target on once anything can be derived, gets m_key
    # only if its row holds it, as OpenSM sends it only where guid2mkey
    # names it (specs/zero-touch.md): an unknown switch row gets its derived
    # key alone, as a tenant's does
    (DERIVED, True, True, False, (DERIVED,)),
    (UNKNOWN, True, True, False, (DERIVED,)),
    (M_KEY, True, False, False, (M_KEY, DERIVED)),
    (DERIVED, True, False, False, (DERIVED,)),
    (UNKNOWN, True, False, False, (DERIVED,)),
    (M_KEY, True, False, True, (M_KEY, DERIVED)),
    (DERIVED, True, False, True, (DERIVED, M_KEY)),
    (UNKNOWN, True, False, True, (DERIVED, M_KEY)),
    (M_KEY, False, False, False, (M_KEY,)),
    (DERIVED, False, False, False, (DERIVED, M_KEY)),
    (UNKNOWN, False, False, False, (M_KEY,)),
    (M_KEY, False, False, True, (M_KEY,)),
    (DERIVED, False, False, True, (DERIVED, M_KEY)),
    (UNKNOWN, False, False, True, (M_KEY,)),
]


def test_the_table_covers_every_combination():
    combinations = {row[:4] for row in KEYS_TO_TRY}
    assert len(combinations) == len(KEYS_TO_TRY) == 3 * 2 * 2 * 2


@pytest.mark.parametrize("holds, target_on, infra, nothing, keys", KEYS_TO_TRY)
def test_keys_to_try_follows_the_probers_rule(holds, target_on, infra, nothing, keys):
    assert port_keys.keys_to_try(holds, target_on, infra, nothing) == keys


@pytest.mark.parametrize("holds", [DERIVED, UNKNOWN])
def test_this_hosts_own_port_keeps_both_keys(holds):
    # Nothing else can answer as it, so it's sent both whatever its row says.
    assert port_keys.keys_to_try(holds, True, True, False, own_port=True) == (
        DERIVED,
        M_KEY,
    )


@pytest.mark.parametrize("holds", [DERIVED, UNKNOWN])
def test_a_switch_gets_m_key_only_if_its_row_holds_it(holds):
    # With the target on and anything derived, a switch not on m_key is sent
    # what OpenSM would send it: its derived key alone.
    assert port_keys.keys_to_try(holds, True, True, False) == (DERIVED,)
    assert port_keys.keys_to_try(M_KEY, True, True, False) == (M_KEY, DERIVED)


@pytest.mark.parametrize("holds", [DERIVED, UNKNOWN])
def test_a_tenants_port_is_never_sent_m_key_once_anything_is_derived(holds):
    # What keeps the old key from tenants: with the target on, a port
    # recorded derived or unknown that doesn't answer its derived key is
    # unreachable, never sent m_key.
    assert M_KEY not in port_keys.keys_to_try(holds, True, False, False)


def test_keys_to_try_refuses_an_unknown_holds():
    with pytest.raises(ValueError):
        port_keys.keys_to_try("0x1234", True, False, False)


@pytest.mark.parametrize(
    "port, infra",
    [
        (Port(HOST), True),
        (Port(S1, switch=True), True),
        (Port(X, switch=False), False),
        # Not known to be a switch until its NodeInfo answers
        (Port(S2, switch=None), False),
    ],
)
def test_infrastructure_is_this_hosts_port_or_a_switch(port, infra):
    assert port_keys.is_infrastructure(port, HOST) is infra


# What a start renders


def test_entries_with_the_target_on_are_m_key_for_ports_holding_it():
    # OpenSM would send each port its derived key; a port still on m_key
    # needs an entry to be reached (Before each start).
    ports = _ports(Port(X, holds=M_KEY), Port(Y, holds=DERIVED), Port(S1))
    assert dict(port_keys.guid2mkey_entries(ports, True, M, BASE)) == {X: M}


def test_entries_with_the_target_off_are_derived_for_ports_holding_one():
    # OpenSM would send m_key; a port on its derived key needs an entry.
    ports = _ports(Port(X, holds=M_KEY), Port(Y, holds=DERIVED), Port(S1))
    assert dict(port_keys.guid2mkey_entries(ports, False, M, BASE)) == {Y: _derived(Y)}


def test_unknown_ports_get_no_entry():
    # OpenSM's own choice: a stale entry would beat the derivation
    # (requirement 3).
    ports = _ports(Port(X, holds=UNKNOWN))
    for target_on in (True, False):
        assert not port_keys.guid2mkey_entries(ports, target_on, M, BASE)


def test_entries_without_a_base_raise_without_a_key():
    with pytest.raises(ValueError) as e:
        port_keys.guid2mkey_entries(_ports(Port(X, holds=DERIVED)), False, M, None)
    assert f"{M:x}" not in str(e.value)


def test_entries_hold_no_key_in_their_repr():
    entries = port_keys.guid2mkey_entries(_ports(Port(X, holds=M_KEY)), True, M, BASE)
    assert "1234" not in repr(entries)


# Routes


def _fabric():
    """This host's port on S1 port 1; S1 to S2 on two links (2 and 4); S1
    to S3 on 5; S2 to S3 on 3; X on S1 port 3, Y on S2 port 2; and a link
    from X's second port to Z, which no route may cross"""
    z = 0xB3
    return _links(
        *_both(HOST, 1, S1, 1),
        *_both(S1, 2, S2, 1),
        *_both(S1, 4, S2, 4),
        *_both(S1, 5, S3, 1),
        *_both(S2, 3, S3, 2),
        *_both(S1, 3, X, 1),
        *_both(S2, 2, Y, 1),
        *_both(X, 2, z, 1),
    )


def test_routes_start_at_this_hosts_port():
    assert port_keys.next_hops(_fabric(), HOST, {}, set()) == [
        port_keys.Hop(HOST, (0,))
    ]


def test_routes_then_follow_this_hosts_link():
    # "0,<own port number>" reaches what its link reaches.
    hops = port_keys.next_hops(_fabric(), HOST, {HOST: (0,)}, set())
    assert hops == [port_keys.Hop(S1, (0, 1), HOST, 1)]
    assert hops[0].route_text == "0,1"


def test_routes_go_one_exit_port_per_switch_hop():
    routes = {HOST: (0,), S1: (0, 1)}
    hops = port_keys.next_hops(_fabric(), HOST, routes, {S1})
    assert hops == [
        port_keys.Hop(S2, (0, 1, 2), S1, 2),
        port_keys.Hop(X, (0, 1, 3), S1, 3),
        port_keys.Hop(S3, (0, 1, 5), S1, 5),
    ]


def test_routes_dont_follow_a_switch_that_doesnt_answer():
    # A route through a switch that doesn't answer isn't followed (The
    # fabric check).
    routes = {HOST: (0,), S1: (0, 1)}
    assert port_keys.next_hops(_fabric(), HOST, routes, set()) == []


def test_routes_never_cross_an_hca():
    # X answers, but as an HCA, so it isn't among the switches that
    # answered, and its second port's link isn't followed.
    hops = port_keys.all_routes(_fabric(), HOST, {S1, S2, S3})
    assert X in hops
    assert 0xB3 not in hops


def test_the_shortest_route_wins_then_the_lowest_exit_port():
    hops = port_keys.all_routes(_fabric(), HOST, {S1, S2, S3})
    # S3 directly from S1, not through S2; S2 by S1's port 2, not 4
    assert hops[S3].route == (0, 1, 5)
    assert hops[S2].route == (0, 1, 2)
    assert hops[Y] == port_keys.Hop(Y, (0, 1, 2, 2), S2, 2)
    assert set(hops) == {HOST, S1, S2, S3, X, Y}


def test_a_switch_that_doesnt_answer_routes_around_it():
    # S2 doesn't answer: Y, behind it only, gets no route; S3 still does.
    hops = port_keys.all_routes(_fabric(), HOST, {S1, S3})
    assert Y not in hops
    assert hops[S2].route == (0, 1, 2)


# Judgements over the rows


def test_changing_is_holds_that_differ_from_the_target():
    # Requirement 1's changing is that disagreement, with no state of its
    # own.
    ports = _ports(Port(Y, holds=DERIVED), Port(X, holds=M_KEY), Port(S1))
    assert [row.guid for row in port_keys.changing(ports, True)] == [S1, X]
    assert [row.guid for row in port_keys.changing(ports, False)] == [S1, Y]


def test_a_transition_is_complete_when_every_reachable_port_holds_the_target():
    # Turning it on: a port that's down keeps its m_key row, and doesn't
    # hold the transition up.
    ports = _ports(
        Port(X, holds=DERIVED, reachable=True), Port(Y, holds=M_KEY, reachable=False)
    )
    assert port_keys.transition_complete(ports, True)
    assert not port_keys.transition_complete(ports, False)
    ports[Y] = Port(Y, holds=M_KEY, reachable=True)
    assert not port_keys.transition_complete(ports, True)


@pytest.mark.parametrize(
    "group_named, holds, refused",
    [
        (True, M_KEY, True),
        (False, DERIVED, True),
        (False, M_KEY, False),
        (False, UNKNOWN, False),
    ],
)
def test_forgetting_is_refused_while_named_or_any_row_is_derived(
    group_named, holds, refused
):
    # The rows render the cache that reaches ports on derived keys (Turning
    # it off, Commands).
    ports = _ports(Port(X, holds=M_KEY), Port(Y, holds=holds))
    assert bool(port_keys.forget_problem(ports, group_named)) is refused


# The database


@pytest.fixture
def session():
    with db.make_sessionmaker(":memory:")() as session:
        yield session


def test_rows_round_trip(session):
    ports = _ports(
        Port(
            S1,
            node_guid=S1,
            switch=True,
            holds=DERIVED,
            lapsed=True,
            reachable=True,
            released=True,
            last_seen=T0.replace(microsecond=5),
        ),
        Port(0xFFFFFFFFFFFFFFFE, holds=M_KEY),
        Port(X),
    )
    links = _links(*_both(S1, 3, X, 1, T1), *_both(HOST, 1, S1, 1))
    port_keys.save(session, ports, links)
    session.commit()
    assert port_keys.load(session) == (ports, links)
    assert session.get(db.FabricPort, "fffffffffffffffe").holds == M_KEY


def test_save_makes_the_rows_those_given(session):
    port_keys.save(session, _ports(Port(X), Port(Y)), _links(*_both(S1, 3, X, 1)))
    session.commit()
    ports = _ports(Port(X, holds=DERIVED))
    links = _links(Link(S1, 3, Y, 1))
    port_keys.save(session, ports, links)
    session.commit()
    assert port_keys.load(session) == (ports, links)


def test_the_target_is_off_until_recorded(session):
    assert port_keys.load_target(session) is False
    port_keys.save_target(session, True)
    session.commit()
    assert port_keys.load_target(session) is True
    assert session.get(db.State, port_keys.TARGET).value == 1
    port_keys.save_target(session, False)
    assert port_keys.load_target(session) is False


def test_the_fingerprint_is_kept_as_text(session):
    assert port_keys.load_fingerprint(session) is None
    port_keys.save_fingerprint(session, port_keys.fingerprint(BASE))
    session.commit()
    assert port_keys.load_fingerprint(session) == port_keys.fingerprint(BASE)
    row = session.get(db.State, port_keys.BASE_FINGERPRINT)
    assert row.value == 0
    port_keys.save_fingerprint(session, port_keys.fingerprint(BASE + 1))
    assert port_keys.load_fingerprint(session) == port_keys.fingerprint(BASE + 1)


@pytest.mark.parametrize("value", ["0x00000000000000bb", "bb", None, "A" * 64])
def test_only_a_fingerprint_is_stored_as_one(session, value):
    # A key passed by mistake would be stored in the clear.
    with pytest.raises(ValueError) as e:
        port_keys.save_fingerprint(session, value)
    assert "bb" not in str(e.value)


def test_a_half_link_is_neither_followed_nor_rendered():
    links = _links(*_both(S1, 3, X, 1, T0))
    links[(S1, 4)] = Link(S1, 4, Y, 1, T0)  # Y's row doesn't point back
    assert [link.key for link in port_keys.paired(links)] == [(S1, 3), (X, 1)]
    assert "0x0000000000000051:4" not in port_keys.render_neighbors(links)
    routes = port_keys.all_routes(links, S1, {S1})
    assert Y not in routes and X in routes


def test_start_fold_drops_a_recabled_ports_old_peer_too():
    links = _links(*_both(S1, 3, X, 1, T0))
    _, folded, _ = port_keys.fold_start(
        {},
        links,
        port_keys.parse_neighbors("0xb1:1 0x51:6\n0x51:6 0xb1:1\n"),
        port_keys.KeyEntries(),
        M,
        None,
        T1,
        T1,
    )
    assert (S1, 3) not in folded
    assert folded[(X, 1)].peer_port == 6
