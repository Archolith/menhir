"""Offline: UnmergeCoordinator._build_restore_plan separates Graphiti fact edges from plain edges.

A fact edge is a RELATES_TO snapshot relationship with a non-None ``fact``, a truthy ``uuid`` and a
peer that is neither the survivor nor the absorbed node. Those go to ``fact_out`` / ``fact_in`` and are
EXCLUDED from ``out_rels`` / ``in_rels``; everything else is unchanged.
"""

from __future__ import annotations

from menhir.services.unmerge_coordinator import UnmergeCoordinator

S, A, P, Q, EP = "s-uuid", "a-uuid", "p-uuid", "q-uuid", "ep-uuid"


class _StubJournal:
    def __init__(self) -> None:
        self.ready_calls = 0

    def _ensure_ready(self) -> None:
        self.ready_calls += 1


def _coordinator() -> UnmergeCoordinator:
    return UnmergeCoordinator(graph_adapter=None, journal=_StubJournal())


def _rel(rtype, direction, peer, props=None, peer_labels=("Entity",)):
    return {
        "type": rtype,
        "direction": direction,
        "properties": dict(props or {}),
        "peer_uuid": peer,
        "peer_labels": list(peer_labels),
        "peer_identity": {"uuid": peer},
    }


def _node(uuid, rels):
    return {"uuid": uuid, "labels": ["Entity"], "properties": {"uuid": uuid, "name": uuid},
            "relationships": rels}


def _plan(absorbed_rels, survivor_rels=()):
    return _coordinator()._build_restore_plan(
        _node(S, list(survivor_rels)), _node(A, list(absorbed_rels))
    )


def test_fact_edges_land_in_fact_lists_and_leave_plain_lists():
    """Fails if the plan keeps fact edges in out_rels/in_rels (pre-change behaviour) or drops them."""
    out_props = {"uuid": "u-out", "fact": "a owns p", "episodes": ["e1"]}
    in_props = {"uuid": "u-in", "fact": "q employs a", "episodes": ["e2"]}
    plan = _plan([
        _rel("RELATES_TO", "out", P, out_props),
        _rel("RELATES_TO", "in", Q, in_props),
    ])
    assert plan["fact_out"] == [{"uuid": "u-out", "peer_uuid": P, "properties": out_props}]
    assert plan["fact_in"] == [{"uuid": "u-in", "peer_uuid": Q, "properties": in_props}]
    assert plan["out_rels"] == []
    assert plan["in_rels"] == []


def test_plain_relates_to_without_fact_stays_in_out_rels():
    """Fails if any RELATES_TO is treated as a fact edge regardless of the fact property."""
    no_fact = {"weight": 0.4}
    none_fact = {"uuid": "u-x", "fact": None}
    plan = _plan([
        _rel("RELATES_TO", "out", P, no_fact),
        _rel("RELATES_TO", "out", Q, none_fact),
    ])
    assert plan["fact_out"] == [] and plan["fact_in"] == []
    assert [(r["peer_uuid"], r["properties"]) for r in plan["out_rels"]] == [
        (P, no_fact), (Q, none_fact)
    ]


def test_fact_edge_to_survivor_is_not_a_fact_move():
    """Fails if a fact edge between absorbed and survivor is moved (it would become a self-loop)."""
    props = {"uuid": "u-as", "fact": "a duplicates s"}
    plan = _plan([_rel("RELATES_TO", "out", S, props), _rel("RELATES_TO", "in", S, props)])
    assert plan["fact_out"] == [] and plan["fact_in"] == []
    assert [r["peer_uuid"] for r in plan["out_rels"]] == [S]
    assert [r["peer_uuid"] for r in plan["in_rels"]] == [S]


def test_fact_edge_without_uuid_stays_in_out_rels():
    """Fails if a fact edge lacking an identity uuid is routed to the fact move (no key to match)."""
    for props in ({"fact": "no uuid"}, {"uuid": "", "fact": "empty uuid"}):
        plan = _plan([_rel("RELATES_TO", "out", P, props)])
        assert plan["fact_out"] == []
        assert [r["properties"] for r in plan["out_rels"]] == [props]


def test_non_relates_to_type_with_fact_property_is_not_a_fact_edge():
    """Fails if the type check is dropped and any edge carrying fact+uuid is moved."""
    props = {"uuid": "u-m", "fact": "odd"}
    plan = _plan([_rel("OTHER", "out", P, props)])
    assert plan["fact_out"] == []
    assert [r["type"] for r in plan["out_rels"]] == ["OTHER"]


def test_mentions_retention_and_rebound_are_unchanged():
    """Fails if fact handling disturbs MENTIONS/RETENTION_SOURCE routing or the rebound subtraction."""
    absorbed = [
        _rel("MENTIONS", "in", EP, {"conf": 0.7}, ("Episodic",)),
        _rel("MENTIONS", "in", "ep-shared", {}, ("Episodic",)),
        _rel("RETENTION_SOURCE", "in", EP, {}, ("Episodic",)),
        _rel("RETENTION_SOURCE", "in", "ep-shared", {}, ("Episodic",)),
        _rel("RELATES_TO", "out", P, {"weight": 0.9}),
    ]
    survivor = [
        _rel("MENTIONS", "in", "ep-shared", {}, ("Episodic",)),
        _rel("RETENTION_SOURCE", "in", "ep-shared", {}, ("Episodic",)),
    ]
    plan = _plan(absorbed, survivor)
    assert plan["rebound_episodes"] == [EP]
    assert plan["rebound_retention_sources"] == [EP]
    assert sorted((r["type"], r["peer_uuid"]) for r in plan["in_rels"]) == sorted([
        ("MENTIONS", EP), ("MENTIONS", "ep-shared"),
        ("RETENTION_SOURCE", EP), ("RETENTION_SOURCE", "ep-shared"),
    ])
    assert plan["out_rels"] == [
        {"type": "RELATES_TO", "peer_uuid": P, "properties": {"weight": 0.9}}
    ]
    assert plan["fact_out"] == [] and plan["fact_in"] == []
    assert plan["absorbed_labels"] == ["Entity"]


# --------------------------------------------------------------------------------------------
# Predicate edge cases (shared with the merge's Cypher half) and plan de-duplication.
# --------------------------------------------------------------------------------------------

from menhir.domain import merge_snapshot as ms  # noqa: E402


def test_peer_without_entity_label_is_not_a_moved_fact():
    """Fails if the Entity-label requirement is dropped (a non-Entity peer would be moved)."""
    props = {"uuid": "u-ep", "fact": "odd peer"}
    rel = _rel("RELATES_TO", "out", P, props, peer_labels=("Episodic",))
    assert not ms.is_moved_fact(rel, survivor_uuid=S, absorbed_uuid=A)
    plan = _plan([rel])
    assert plan["fact_out"] == []
    assert [r["peer_uuid"] for r in plan["out_rels"]] == [P]
    # Missing peer_labels entirely behaves the same.
    bare = _rel("RELATES_TO", "out", P, props)
    del bare["peer_labels"]
    assert not ms.is_moved_fact(bare, survivor_uuid=S, absorbed_uuid=A)


def test_non_string_or_empty_uuid_is_not_a_moved_fact():
    """Fails if int / empty / None uuids are accepted as fact identities."""
    for bad in (5, "", None):
        rel = _rel("RELATES_TO", "out", P, {"uuid": bad, "fact": "f"})
        assert not ms.is_moved_fact(rel, survivor_uuid=S, absorbed_uuid=A), bad
        plan = _plan([rel])
        assert plan["fact_out"] == [], bad
        assert len(plan["out_rels"]) == 1, bad
    good = _rel("RELATES_TO", "out", P, {"uuid": "u-ok", "fact": "f"})
    assert ms.is_moved_fact(good, survivor_uuid=S, absorbed_uuid=A)


def test_peer_equal_to_survivor_or_absorbed_is_not_a_moved_fact():
    """Fails if a self-loop or survivor-adjacent fact is treated as moved."""
    for peer in (S, A):
        rel = _rel("RELATES_TO", "in", peer, {"uuid": "u-x", "fact": "f"})
        assert not ms.is_moved_fact(rel, survivor_uuid=S, absorbed_uuid=A), peer
    assert ms.is_moved_fact(
        _rel("RELATES_TO", "in", P, {"uuid": "u-x", "fact": "f"}),
        survivor_uuid=S, absorbed_uuid=A,
    )


def test_duplicate_uuid_peer_pairs_produce_one_fact_entry():
    """Fails if parallel same-(uuid, peer) edges yield two entries (restore would move twice), or if
    different peers / directions sharing a uuid are wrongly collapsed."""
    p1 = {"uuid": "u-dup", "fact": "first"}
    p2 = {"uuid": "u-dup", "fact": "second"}
    plan = _plan([
        _rel("RELATES_TO", "out", P, p1),
        _rel("RELATES_TO", "out", P, p2),
        _rel("RELATES_TO", "out", Q, p1),
        _rel("RELATES_TO", "in", P, p1),
    ])
    assert sorted((f["uuid"], f["peer_uuid"]) for f in plan["fact_out"]) == [
        ("u-dup", P), ("u-dup", Q)
    ]
    assert [(f["uuid"], f["peer_uuid"]) for f in plan["fact_in"]] == [("u-dup", P)]
    assert plan["out_rels"] == [] and plan["in_rels"] == []


def test_moved_fact_uuids_is_sorted_and_keeps_repeats():
    """Fails if the set is de-duplicated (the merge guard compares multiplicity) or unsorted, or if
    ineligible rels leak in."""
    absorbed = _node(A, [
        _rel("RELATES_TO", "out", P, {"uuid": "u-b", "fact": "f"}),
        _rel("RELATES_TO", "in", Q, {"uuid": "u-a", "fact": "f"}),
        _rel("RELATES_TO", "out", P, {"uuid": "u-b", "fact": "f2"}),
        _rel("RELATES_TO", "out", S, {"uuid": "u-skip-survivor", "fact": "f"}),
        _rel("RELATES_TO", "out", P, {"weight": 1}),
        _rel("RELATES_TO", "out", P, {"uuid": 5, "fact": "f"}),
    ])
    assert ms.moved_fact_uuids(absorbed, survivor_uuid=S) == ["u-a", "u-b", "u-b"]
