"""The prober's wire format, and its verify against a simulated fabric

The simulator, tests/fabric_sim.py, answers a Get only as InfiniBand would,
and records every Get it was sent, so that the tests can assert what was
never sent: above all, that a tenant's port isn't sent a key the rules
don't allow it.
"""

import dataclasses
import random

import pytest

from alternative_fabric_manager import port_keys, prober
from alternative_fabric_manager.drivers import ReloadError
from alternative_fabric_manager.port_keys import DERIVED, M_KEY, UNKNOWN, PortState
from alternative_fabric_manager.prober import FULL, SWITCHES, Get, ProbeError
from tests.fabric_sim import (
    BASE,
    EARLIER,
    NOW,
    OWN,
    S1,
    S2,
    T1,
    T2,
    T3,
    T4,
    M,
    derived,
    fabric,
)

# The wire format


def test_a_gets_repr_hides_its_key():
    get = Get("n1", "nodeinfo", "0,1", None, M)
    assert f"{M:x}" not in repr(get)
    assert f"{M:x}" not in str(get)
    assert get.line() == f"n1 nodeinfo 0,1 - 0x{M:016x}"
    assert Get("p1", "portinfo", "0", 7, 0).line().endswith(" 7 0x0000000000000000")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": "a b"},
        {"id": "x;reboot"},
        {"id": ""},
        {"id": "x" * 33},
        {"attr": "nodeset"},
        {"route": "1"},
        {"route": "0,"},
        {"route": "0,1;reboot"},
        {"route": "0,255"},
        {"route": "0,0"},
        {"route": "0" + ",1" * 64},
        {"port": 255},
        {"port": -1},
        {"port": "1"},
        {"port": True},
        {"key": -1},
        {"key": 2**64},
        {"key": "0x1"},
        {"key": True},
    ],
)
def test_a_get_is_checked_strictly(kwargs):
    values = {"id": "n1", "attr": "nodeinfo", "route": "0,1", "port": None, "key": M}
    values.update(kwargs)
    with pytest.raises(ValueError) as raised:
        Get(**values)
    assert f"{M:x}" not in str(raised.value)


def test_plan_refuses_duplicate_ids():
    with pytest.raises(ValueError, match="two Gets"):
        prober.plan([Get("a", "nodeinfo", "0", None, 1)] * 2)


def test_environment_carries_the_plan():
    gets = [Get("a", "nodeinfo", "0", None, M), Get("b", "portinfo", "0,3", 0, M)]
    env = prober.environment(gets, 3, 8)
    assert env == {
        "AFM_PROBES": f"a nodeinfo 0 - 0x{M:016x}\nb portinfo 0,3 0 0x{M:016x}",
        "AFM_PROBE_TIMEOUT": "3",
        "AFM_PROBE_PARALLEL": "8",
    }
    for bad in ((0, 8), (3, 0), (1.5, 8), (3, True)):
        with pytest.raises(ValueError):
            prober.environment(gets, *bad)


GETS = [Get("a", "nodeinfo", "0", None, M), Get("b", "portinfo", "0", None, M)]
OUTPUT = (
    "OWN\n0x00000000000000a0\n0xffffffffffffffff\n0x0000000000000000\n"
    "0x00000000000000A1\nEND OWN\n"
    "BEGIN b\nEND b 124\n"
    "BEGIN a\nNodeType:Channel Adapter\nPortGuid:0x00000000000000a0\nEND a 0\n"
)


def test_parse():
    own, answers = prober.parse(OUTPUT, GETS)
    # Placeholders of unassigned virtual functions aren't ours.
    assert own == [0xA0, 0xA1]
    assert answers == {
        "a": {"NodeType": "Channel Adapter", "PortGuid": "0x00000000000000a0"},
        "b": None,
    }


@pytest.mark.parametrize(
    "output",
    [
        "",
        OUTPUT.replace("OWN\n", "", 1),
        OUTPUT.replace("END OWN\n", ""),
        OUTPUT.replace("0x00000000000000a0\n", "a0\n", 1),
        OUTPUT.replace("BEGIN b\nEND b 124\n", ""),
        OUTPUT + "BEGIN b\nEND b 0\n",
        OUTPUT + "BEGIN c\nEND c 0\n",
        OUTPUT.replace("END a 0", "END b 0"),
        OUTPUT.replace("END a 0\n", ""),
        OUTPUT.replace("NodeType:", "Mkey:0x2222222222222222\nNodeType:"),
        OUTPUT.replace("NodeType:Channel Adapter", "NodeType:Channel;Adapter"),
        OUTPUT.replace("NodeType:", "NodeType:Switch\nNodeType:"),
        OUTPUT + "stray\n",
        OUTPUT.replace("END b 124", "END b x"),
    ],
)
def test_parse_refuses_what_breaks_the_contract(output):
    with pytest.raises(ProbeError) as raised:
        prober.parse(output, GETS)
    assert isinstance(raised.value, ReloadError)
    # Its message never holds the output.
    assert "2222" not in str(raised.value)
    assert "Channel" not in str(raised.value)


def check(sim, rows=None, links=None, **kwargs):
    values = {
        "m_key": M,
        "base": BASE,
        "target_on": True,
        "nothing_derived": False,
        "run": sim.run,
        "scope": FULL,
        "now": NOW,
    }
    values.update(kwargs)
    return prober.verify(
        sim.rows() if rows is None else rows,
        sim.links() if links is None else links,
        **values,
    )


def test_a_whole_fabric_on_derived_keys():
    sim = fabric()
    result = check(sim)
    assert result.complete and not result.own_port_lost
    assert result.own_guid == OWN
    assert result.switches == {S1, S2}
    assert result.answered == {OWN, S1, S2, T1, T2, T3}
    assert result.keyed == result.answered
    assert result.link_down == {T4}
    assert result.unanswered == set()
    assert result.unrecorded == []
    for guid in (OWN, S1, S2, T1, T2, T3):
        assert set(sim.keys_sent(guid)) == {derived(guid)}
        row = result.ports[guid]
        assert row.reachable and row.holds == DERIVED and row.last_seen == NOW
    assert result.ports[T4] == sim.rows()[T4]
    assert result.ports[S1].switch is True and result.ports[T1].switch is False
    assert result.ports[S1].node_guid == S1 + 0x1000


# Rule 1: this host's port first, at route 0, with both keys


def test_rule1_this_hosts_port_is_probed_first_at_route_0():
    sim = fabric()
    check(sim)
    # The first batch only asks ibstat, the next Gets this host's port.
    assert sim.batches[0] == []
    assert set(sim.batches[1]) == {OWN}
    assert all(route_guid == OWN for route_guid in sim.batches[1])


def test_rule1_prefers_recorded_candidates():
    sim = fabric()
    sim.own_guids = [0xAF, OWN]
    result = check(sim)
    assert result.own_guid == OWN
    # The unrecorded candidate's derived key was never tried.
    assert derived(0xAF) not in sim.keys_sent(OWN)
    assert sim.keys_sent(OWN)[0] == derived(OWN)


def test_rule1_tries_both_keys_the_recorded_one_first():
    sim = fabric()
    sim.ports[OWN].key = derived(OWN)
    rows = sim.rows(holds={OWN: M_KEY})
    result = check(sim, rows=rows)
    sent = sim.keys_sent(OWN)
    assert sent[:2] == [M, M]  # NodeInfo and PortInfo
    assert sent[2:4] == [derived(OWN)] * 2
    assert result.ports[OWN].holds == DERIVED


def test_rule1_a_second_candidate_is_this_hosts_port():
    # smpquery's port is the second of two recorded ports of this host.
    sim = fabric()
    other = 0xA1
    sim.own = other
    sim.own_guids = [OWN, other]
    sim.add(other)
    sim.cables = {
        (other if guid == OWN else guid, port): (
            other if peer == OWN else peer,
            peer_port,
        )
        for (guid, port), (peer, peer_port) in sim.cables.items()
    }
    rows = sim.rows()
    rows[OWN] = PortState(OWN, holds=DERIVED, reachable=True)
    result = check(sim, rows=rows, links=sim.links())
    assert result.own_guid == other
    assert result.answered >= {other, S1, S2, T1}
    # OWN's derived key went to the other port first, and got no answer.
    assert sim.keys_sent(other)[:2] == [derived(OWN)] * 2
    assert not result.own_port_lost


def test_rule1_this_hosts_port_lost():
    sim = fabric()
    sim.ports[OWN].key = 0x3333333333333333
    result = check(sim)
    assert result.own_port_lost
    assert result.own_guid is None
    assert result.unanswered == {OWN}
    assert not result.ports[OWN].reachable
    # Nothing beyond it was sent anything.
    assert {to for to, *_ in sim.sent} == {OWN}
    assert set(sim.keys_sent(OWN)) == {derived(OWN), M}
    assert "neither key" in str(result)


def test_rule1_no_port_on_this_host_is_lost_too():
    sim = fabric()
    sim.own_guids = []
    result = check(sim)
    assert result.own_port_lost and sim.sent == []


def test_rule1_m_key_is_sent_once_for_all_candidates():
    sim = fabric()
    sim.ports[OWN].key = M
    sim.own_guids = [OWN, 0xA1, 0xA2]
    rows = sim.rows()
    rows[0xA1] = PortState(0xA1, holds=M_KEY)
    rows[0xA2] = PortState(0xA2, holds=M_KEY)
    result = check(sim, rows=rows)
    assert result.own_guid == OWN
    assert sim.keys_sent(OWN).count(M) == 2  # one NodeInfo, one PortInfo


# Rule 2: switches first, and only through switches that answer


def test_rule2_every_switch_before_any_hca():
    sim = fabric()
    check(sim)
    order = [guid for batch in sim.batches for guid in batch]
    last_switch = max(i for i, guid in enumerate(order) if guid in (S1, S2))
    first_hca = min(i for i, guid in enumerate(order) if guid in (T1, T2, T3))
    assert last_switch < first_hca


def test_rule2_a_deadline_keeps_this_host_and_the_switches():
    sim = fabric()
    clock = [0.0]

    def tick(sim):
        # Out of time once every switch has answered
        if S2 in {guid for batch in sim.batches for guid in batch}:
            clock[0] = 100.0

    sim.on_batch = tick
    rows = sim.rows()
    result = check(sim, rows=rows, deadline=50.0, clock=lambda: clock[0])
    assert not result.complete
    assert result.switches == {S1, S2}
    assert result.answered >= {OWN, S1, S2}
    # The tenants were never sent anything, and keep their rows.
    for tenant in (T1, T2, T3):
        assert sim.keys_sent(tenant) == []
        assert result.ports[tenant] is rows[tenant]
    assert "incomplete" in str(result)


def test_rule2_a_switch_that_doesnt_answer_isnt_followed():
    sim = fabric()
    sim.ports[S1].key = 0x3333333333333333
    result = check(sim)
    assert S1 in result.unanswered
    for guid in (S2, T1, T2, T3, T4):
        assert sim.keys_sent(guid) == []
    assert result.switches == set()


def test_rule2_a_switch_not_yet_known_isnt_routed_through():
    sim = fabric()
    rows = sim.rows()
    rows[S2] = dataclasses.replace(rows[S2], switch=None)
    result = check(sim, rows=rows)
    # It answered, and is recorded a switch, but nothing behind it was
    # probed, and it isn't counted.
    assert S2 in result.answered
    assert result.ports[S2].switch is True
    assert S2 not in result.switches
    assert sim.keys_sent(T3) == []


def test_rule2_a_switch_must_say_so_again():
    sim = fabric()
    sim.ports[S2].switch = False  # its NodeInfo now says it's an HCA
    result = check(sim)
    assert S2 not in result.switches
    assert result.ports[S2].switch is False
    assert sim.keys_sent(T3) == []


def test_rule2_a_switch_behind_a_switch():
    sim = fabric()
    sim.add(0x53, switch=True, ports=4)
    sim.add(0xB5)
    sim.cable(S2, 3, 0x53, 1)
    sim.cable(0x53, 2, 0xB5, 1)
    result = check(sim)
    assert 0x53 in result.switches
    assert 0xB5 in result.keyed
    gets = [(to, port) for to, _key, attr, port in sim.sent if to == 0xB5]
    assert gets == [(0xB5, None), (0xB5, None)]


# Rule 3: nothing is sent behind a link that's down


def test_rule3_a_port_behind_a_down_link_is_sent_nothing():
    sim = fabric()
    rows = sim.rows()
    result = check(sim, rows=rows)
    assert sim.keys_sent(T4) == []
    assert result.link_down == {T4}
    assert result.ports[T4] is rows[T4]
    assert result.link_states[(S1, 5)] is False


def test_rule3_this_hosts_link_down():
    sim = fabric()
    sim.down |= {(OWN, 1), (S1, 1)}
    result = check(sim)
    assert result.link_down == {S1}
    assert {to for to, *_ in sim.sent} == {OWN}


def test_rule3_a_link_state_not_known_isnt_followed():
    sim = fabric()
    rows = sim.rows()
    # S1 answers its NodeInfo, and PortInfo for port 0, but no port's
    # PortInfo
    original = sim.answer

    def answer(guid, deliverable, get):
        if guid == S1 and get.attr == "portinfo" and get.port:
            return None
        return original(guid, deliverable, get)

    sim.answer = answer
    result = check(sim, rows=rows)
    assert S1 in result.answered
    for tenant in (T1, T2, T4):
        assert sim.keys_sent(tenant) == []
        assert tenant not in result.link_down
        assert result.ports[tenant] is rows[tenant]
    assert sim.keys_sent(S2) == []


def test_rule3_link_state_is_asked_of_the_switch_with_its_key():
    sim = fabric()
    check(sim, scope={T1})
    switch_port_gets = [
        (key, port) for to, key, attr, port in sim.sent if to == S1 and port
    ]
    assert (derived(S1), 3) in switch_port_gets
    assert {key for key, _ in switch_port_gets} == {derived(S1)}


# Rule 4: NodeInfo and PortInfo with the same key, and the GUID must match


def test_rule4_each_get_is_nodeinfo_and_portinfo_with_one_key():
    sim = fabric()
    check(sim)
    for guid in (T1, T2, T3):
        gets = [(attr, key, port) for to, key, attr, port in sim.sent if to == guid]
        assert sorted(gets) == [
            ("nodeinfo", derived(guid), None),
            ("portinfo", derived(guid), None),
        ]
    switch_gets = [
        (attr, port) for to, _key, attr, port in sim.sent if to == S2 and not port
    ]
    assert sorted(switch_gets, key=str) == [("nodeinfo", None), ("portinfo", 0)]


def test_rule4_a_wrong_guid_on_the_route_doesnt_count():
    # The rows say T1 is on S1 port 3; another port, which answers any key,
    # is there now.
    sim = fabric()
    rows = sim.rows(holds={T1: M_KEY})
    links = sim.links()
    del sim.ports[T1]
    sim.add(0xB9, protect=0)
    sim.cable(S1, 3, 0xB9, 1)
    result = check(sim, rows=rows, links=links)
    assert T1 not in result.answered
    assert result.unconfirmed == {T1}
    assert not result.ports[T1].reachable
    assert result.ports[T1].holds == M_KEY
    # Only the first key went there.
    assert sim.keys_sent(0xB9) == [M, M]


def test_rule4_an_answer_needs_both_gets():
    sim = fabric()
    sim.no_portinfo.add(T1)
    result = check(sim)
    assert result.unconfirmed == {T1}
    assert T1 not in result.answered
    assert sim.keys_sent(T1) == [derived(T1)] * 2


def test_rule4_switches_are_counted_by_the_guid_their_route_answered():
    # Not by NodeInfo's Guid, which the node reports about itself: the
    # fabric check and trust compare the row's own GUID.
    sim = fabric()
    sim.ports[S1].node_guid = 0x0EC0D9A0300B12560
    result = check(sim, scope=SWITCHES)
    assert result.switches == {S1, S2}
    assert result.ports[S1].node_guid == 0x0EC0D9A0300B12560


# Rule 5: which keys, in which order


@pytest.mark.parametrize("scope", [FULL, SWITCHES, frozenset({T1, T2, T3})])
def test_rule5_target_on_tenants_recorded_derived_are_never_sent_m_key(scope):
    sim = fabric()
    for tenant in (T1, T2, T3):
        sim.ports[tenant].key = M  # not what the rows say
    rows = sim.rows(holds={T1: DERIVED, T2: UNKNOWN, T3: DERIVED})
    result = check(sim, rows=rows, scope=scope)
    for tenant in (T1, T2, T3):
        assert M not in sim.keys_sent(tenant)
    if scope is not SWITCHES:
        assert result.unanswered == {T1, T2, T3}
        for tenant in (T1, T2, T3):
            assert not result.ports[tenant].reachable
            assert result.ports[tenant].holds == rows[tenant].holds


def test_rule5_the_recorded_key_goes_first():
    # A port still on m_key during a slow transition isn't sent its derived
    # key at every check.
    sim = fabric()
    sim.ports[T2].key = M
    result = check(sim, rows=sim.rows(holds={T2: M_KEY}))
    assert sim.keys_sent(T2) == [M, M]
    assert result.ports[T2].holds == M_KEY


def test_rule5_the_second_key_only_after_no_answer():
    sim = fabric()
    sim.ports[T2].key = derived(T2)
    result = check(sim, rows=sim.rows(holds={T2: M_KEY}))
    assert sim.keys_sent(T2) == [M, M, derived(T2), derived(T2)]
    assert result.ports[T2].holds == DERIVED


def test_rule5_nothing_derived_allows_m_key_after_the_derived_key():
    sim = fabric()
    sim.ports[T1].key = M
    result = check(sim, rows=sim.rows(holds={T1: UNKNOWN}), nothing_derived=True)
    assert sim.keys_sent(T1) == [derived(T1)] * 2 + [M, M]
    assert result.ports[T1].holds == M_KEY


def test_rule5_target_off_tries_both():
    sim = fabric()
    sim.ports[T1].key = M
    result = check(sim, rows=sim.rows(holds={T1: DERIVED}), target_on=False)
    assert sim.keys_sent(T1) == [derived(T1)] * 2 + [M, M]
    assert result.ports[T1].holds == M_KEY


def test_rule5_a_row_not_known_to_be_a_switch_isnt_infrastructure():
    sim = fabric()
    sim.ports[S2].key = M
    rows = sim.rows(holds={S2: UNKNOWN})
    rows[S2] = dataclasses.replace(rows[S2], switch=None)
    result = check(sim, rows=rows)
    assert sim.keys_sent(S2) == [derived(S2)] * 2
    assert S2 in result.unanswered


def test_rule5_switches_get_both_keys():
    # A switch whose row isn't derived, or with the target off: both keys
    for holds, target_on in ((UNKNOWN, True), (DERIVED, False)):
        sim = fabric()
        sim.ports[S1].key = M
        result = check(sim, rows=sim.rows(holds={S1: holds}), target_on=target_on)
        assert sim.keys_sent(S1)[:4] == [derived(S1)] * 2 + [M, M]
        assert result.ports[S1].holds == M_KEY
        # Its ports' link states are asked with the key it answered.
        assert all(key == M for to, key, _attr, port in sim.sent if to == S1 and port)


def test_rule5_a_derived_switch_isnt_sent_m_key_once_keys_are_on():
    # An adapter claiming a confirmed switch's GUID on its route could
    # answer: with the target on and anything derived, a switch recorded
    # derived gets only its derived key, which the real one holds.
    sim = fabric()
    sim.ports[S1].key = M
    result = check(sim, rows=sim.rows(holds={S1: DERIVED}))
    assert set(sim.keys_sent(S1)) == {derived(S1)}
    assert S1 not in result.keyed


def test_rule5_no_derived_key_without_a_base():
    sim = fabric()
    for guid in sim.ports:
        sim.ports[guid].key = M
    result = check(sim, base=None, target_on=False, rows=sim.rows())
    assert {key for _to, key, *_ in sim.sent} == {M}
    assert result.answered == {OWN, S1, S2, T1, T2, T3}


def test_rule5_property_every_key_sent_is_allowed():
    """Whatever the fabric holds and the rows say, a port is sent only the
    keys keys_to_try allows it, in its order"""
    rng = random.Random(5)  # noqa: S311 - a reproducible test, not a key
    others = [0x3333333333333333]
    for _ in range(300):
        sim = fabric()
        holds = {}
        for guid, port in sim.ports.items():
            port.key = rng.choice([M, derived(guid), *others])
            port.protect = rng.choice([0, 1, 2, 2, 2, 3])
            holds[guid] = rng.choice([M_KEY, DERIVED, UNKNOWN])
        rows = sim.rows(holds=holds, switch_known=rng.random() < 0.8)
        target_on = rng.random() < 0.6
        nothing = rng.random() < 0.3
        scope = rng.choice([FULL, SWITCHES, {T1}, {T3, T2}, set()])
        check(
            sim,
            rows=rows,
            scope=scope,
            target_on=target_on,
            nothing_derived=nothing,
        )
        for guid in sim.ports:
            row = rows[guid]
            infrastructure = port_keys.is_infrastructure(row, OWN)
            allowed = port_keys.keys_to_try(
                row.holds, target_on, infrastructure, nothing, own_port=guid == OWN
            )
            values = [M if name == M_KEY else derived(guid) for name in allowed]
            sent = sim.keys_sent(guid)
            if guid == OWN:
                assert set(sent) <= set(values)
                continue
            # Link-state Gets to a switch use the key it answered, one of
            # these too; the first key sent is the one the row records.
            assert set(sent) <= set(values), (guid, holds[guid], target_on)
            if sent:
                assert sent[0] == values[0]
            if (
                target_on
                and not nothing
                and not infrastructure
                and row.holds in (DERIVED, UNKNOWN)
            ):
                assert M not in sent


# Rule 6: what an answer records


def test_rule6_protect_bits_2_shows_the_key_and_clears_a_release():
    sim = fabric()
    rows = sim.rows(
        holds={T1: UNKNOWN},
        changes={T1: {"lapsed": True, "released": True, "reachable": False}},
    )
    result = check(sim, rows=rows)
    row = result.ports[T1]
    assert row.holds == DERIVED and row.reachable
    assert not row.lapsed and not row.released
    assert row.last_seen == NOW
    assert T1 in result.keyed
    # The rows given are left as they were.
    assert rows[T1].holds == UNKNOWN


@pytest.mark.parametrize("protect", [0, 1])
def test_rule6_a_port_that_answers_any_key_keeps_what_it_holds(protect):
    sim = fabric()
    sim.ports[T1].protect = protect
    rows = sim.rows(
        holds={T1: M_KEY}, changes={T1: {"released": True, "reachable": False}}
    )
    result = check(sim, rows=rows)
    row = result.ports[T1]
    assert row.holds == M_KEY
    assert row.reachable and row.last_seen == NOW
    assert row.lapsed is (protect == 0)
    # Answering any key isn't answering its key.
    assert row.released
    assert T1 in result.answered and T1 not in result.keyed


def test_rule6_no_answer_is_unreachable_and_keeps_what_it_holds():
    sim = fabric()
    sim.ports[T1].key = 0x3333333333333333
    result = check(sim, rows=sim.rows(changes={T1: {"lapsed": True}}))
    row = result.ports[T1]
    assert not row.reachable
    assert row.holds == DERIVED and row.lapsed and row.last_seen == EARLIER
    assert T1 in result.unanswered


def test_rule6_ports_not_probed_keep_their_rows():
    sim = fabric()
    rows = sim.rows()
    result = check(sim, rows=rows, scope=SWITCHES)
    for guid in (T1, T2, T3, T4):
        assert result.ports[guid] is rows[guid]
        assert sim.keys_sent(guid) == []
    assert result.switches == {S1, S2}


def test_rule6_a_port_without_a_row_isnt_given_one():
    sim = fabric()
    rows = sim.rows()
    del rows[OWN]
    result = check(sim, rows=rows)
    assert result.own_guid == OWN
    assert OWN not in result.ports


# Rule 7: a full verify's switch ports


def test_rule7_full_reports_link_states_and_unrecorded_ports():
    sim = fabric()
    sim.add(0xC6)
    sim.cable(S1, 6, 0xC6, 1)  # attached, but OpenSM hasn't recorded it
    links = {key: link for key, link in sim.links().items() if 0xC6 not in key[::2]}
    links = {key: link for key, link in links.items() if link.peer_guid != 0xC6}
    result = check(sim, links=links)
    assert result.link_states[(S1, 1)] is True
    assert result.link_states[(S1, 5)] is False
    assert result.link_states[(S1, 7)] is False
    assert len([key for key in result.link_states if key[0] == S1]) == 8
    assert len([key for key in result.link_states if key[0] == S2]) == 4
    assert result.unrecorded == [(S1, 6)]
    assert sim.keys_sent(0xC6) == []


def test_rule7_only_a_full_verify_reports_unrecorded_ports():
    sim = fabric()
    sim.cable(S1, 6, T4, 2)
    result = check(sim, scope=SWITCHES)
    assert result.unrecorded == []


# Rule 8: a deadline


def test_rule8_a_deadline_before_anything_ran():
    sim = fabric()
    rows = sim.rows()
    result = check(sim, rows=rows, deadline=0.0, clock=lambda: 1.0)
    assert not result.complete
    assert sim.sent == [] and sim.batches == []
    assert result.ports == rows and not result.own_port_lost


# Scopes


def test_switches_scope_sends_nothing_to_hcas():
    sim = fabric()
    result = check(sim, scope=SWITCHES)
    assert {to for to, *_ in sim.sent} == {OWN, S1, S2}
    assert result.answered == {OWN, S1, S2}
    assert result.unrecorded == []


def test_jobs_scope_gets_only_their_ports_and_the_switches_to_them():
    sim = fabric()
    result = check(sim, scope={T3})
    assert {to for to, *_ in sim.sent} == {OWN, S1, S2, T3}
    assert result.answered == {OWN, S1, S2, T3}
    # Only the switch ports on the way were asked
    assert {(to, port) for to, _k, _a, port in sim.sent if port} == {(S1, 2), (S2, 2)}
    result = check(fabric(), scope={T1})
    assert S2 not in result.answered


def test_jobs_scope_a_port_behind_a_down_link():
    sim = fabric()
    result = check(sim, scope={T4})
    assert result.link_down == {T4}
    assert sim.keys_sent(T4) == []


def test_jobs_scope_an_unrecorded_guid_is_sent_nothing():
    sim = fabric()
    result = check(sim, scope={0xDEAD})
    assert {to for to, *_ in sim.sent} == {OWN}
    assert result.answered == {OWN}


# Errors and results


def test_a_failed_run_raises():
    def run(gets):
        raise ProbeError("the prober exited with status 3")

    with pytest.raises(ReloadError):
        check(fabric(), run=run)


def test_a_result_holds_no_key():
    sim = fabric()
    result = check(sim, rows=sim.rows(holds={T1: M_KEY}))
    text = repr(result) + str(result) + repr(result.counts())
    for guid in sim.ports:
        assert f"{derived(guid):x}" not in text
        assert str(derived(guid)) not in text
    assert f"{M:x}" not in text and str(M) not in text
    assert result.counts()["switches"] == 2
    assert result.gets == len(sim.sent)


# Trust: by a switch row's own GUID, which its route's answer matched

CONFIRMED = {S1, S2}
X = 0xD1
BEHIND_X = 0xD2


def _impostor(sim, key):
    """An adapter on S1 port 6 that answers NodeInfo as a switch, with S1's
    GUID as its node GUID, with a port behind it"""
    sim.ports[S1].node_guid = S1
    sim.ports[S2].node_guid = S2
    sim.add(X, switch=True, key=key, node_guid=S1, ports=2)
    sim.cable(S1, 6, X, 1)
    sim.add(BEHIND_X)
    sim.cable(X, 2, BEHIND_X, 1)
    rows = sim.rows()
    rows[X] = PortState(X, switch=True, node_guid=S1, holds=DERIVED, reachable=True)
    return rows


@pytest.mark.parametrize("scope", [FULL, SWITCHES, frozenset({X})])
def test_trust_an_adapter_claiming_a_confirmed_switchs_guid_isnt_sent_m_key(scope):
    sim = fabric()
    rows = _impostor(sim, 0x3333333333333333)
    result = check(sim, rows=rows, scope=scope, trusted_switches=CONFIRMED)
    assert M not in sim.keys_sent(X)
    assert sim.keys_sent(X) == [derived(X)] * 2
    assert X not in result.switches


def test_trust_an_unconfirmed_switch_isnt_routed_through_or_counted():
    sim = fabric()
    rows = _impostor(sim, derived(X))
    result = check(sim, rows=rows, trusted_switches=CONFIRMED)
    assert X in result.keyed
    assert X not in result.switches and result.switches == CONFIRMED
    # A Get through it would show it the key.
    assert sim.keys_sent(BEHIND_X) == []


def test_trust_a_switch_whose_node_guid_differs_is_trusted_by_its_port_guid():
    sim = fabric()
    sim.ports[S1].node_guid = 0x0EC0D9A0300B12560
    sim.ports[S1].key = M
    # Unknown, which a tenant's port would be sent only its derived key for
    rows = sim.rows(holds={S1: UNKNOWN})
    result = check(sim, rows=rows, trusted_switches=CONFIRMED)
    # Infrastructure: both keys, so it answers the one it holds
    assert S1 in result.keyed and result.ports[S1].holds == M_KEY
    assert S1 in result.switches


def test_trust_with_none_confirmed_reaches_only_this_hosts_port_and_its_peer():
    sim = fabric()
    result = check(sim, trusted_switches=set())
    assert result.switches == set()
    assert {to for to, *_ in sim.sent} == {OWN, S1}


def test_trust_confirm_mode_follows_unconfirmed_switches_without_both_keys():
    sim = fabric()
    result = check(sim, trusted_switches={S1}, follow_unconfirmed=True)
    assert result.switches == {S1, S2}
    assert T3 in result.keyed
    assert check(fabric(), trusted_switches={S1}).switches == {S1}
    # Still a tenant's rule for the unconfirmed one: its derived key only
    sim = fabric()
    sim.ports[S2].key = M
    rows = sim.rows(holds={S2: DERIVED})
    result = check(sim, rows=rows, trusted_switches={S1}, follow_unconfirmed=True)
    assert M not in sim.keys_sent(S2)
    assert S2 not in result.switches


def test_a_half_link_isnt_followed():
    # T1 was re-cabled from S1 port 3 to port 6; S1 port 3's row still
    # names it, but T1's points to port 6.
    sim = fabric()
    links = sim.links()
    del sim.cables[(S1, 3)], sim.cables[(T1, 1)]
    sim.cable(S1, 6, T1, 1)
    links[(T1, 1)] = port_keys.Link(T1, 1, S1, 6)
    links[(S1, 6)] = port_keys.Link(S1, 6, T1, 1)
    result = check(sim, links=links)
    assert T1 in result.keyed and T1 not in result.link_down
    # S1 port 3's link state was never what T1 was judged by.
    assert result.unrecorded == []
