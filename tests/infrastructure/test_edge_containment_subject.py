"""Containment needs the dropped edge's subject to be an endpoint of its keeper (CF-259)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.edge_containment import prune_contained_edges

pytestmark = pytest.mark.unit


def _edge(src: str, tgt: str, fact: str) -> SimpleNamespace:
    return SimpleNamespace(
        source_node_uuid=src,
        target_node_uuid=tgt,
        fact=fact,
        episodes=["ep-1"],
        valid_at=None,
        invalid_at=None,
        attributes={},
    )


def _graph(*edges):
    uuids = sorted({u for e in edges for u in (e.source_node_uuid, e.target_node_uuid)})
    nodes = [SimpleNamespace(uuid=u, name=u) for u in uuids]
    return nodes, list(edges), {u: [0] for u in uuids}


@pytest.mark.parametrize(
    ("dropped", "keeper"),
    [
        (_edge("user", "paris", "User lives in Paris"), _edge("mom", "paris", "User's mom lives in Paris")),
        (_edge("user", "cats", "User has 2 cats"), _edge("sister", "cats2", "User's sister has 2 cats")),
        (
            _edge("bob", "alice", "Bob works for Alice"),
            _edge("carol", "alice", "Carol and Bob works for Alice"),
        ),
    ],
)
def test_fact_about_another_subject_is_not_pruned(dropped, keeper) -> None:
    nodes, edges, index_map = _graph(dropped, keeper)
    before = {n.uuid for n in nodes}

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)

    assert edges == [dropped, keeper]
    assert {n.uuid for n in nodes} == before


def test_fact_whose_subject_is_a_keeper_endpoint_is_still_pruned() -> None:
    full = _edge("user", "jeans", "user bought black jeans from Levi's")
    restated = _edge("jeans", "levis", "jeans are from Levi's")
    nodes, edges, index_map = _graph(full, restated)

    assert prune_contained_edges(nodes, edges, index_map, {"user"}) == (1, 1)
    assert edges == [full]


def _named(nodes, **names):
    return [SimpleNamespace(uuid=n.uuid, name=names.get(n.uuid, n.uuid)) for n in nodes]


def test_target_hidden_behind_a_stopword_name_is_kept() -> None:
    keeper = _edge("carol", "alice", "Carol works for Alice")
    dropped = _edge("carol", "user", "Carol works for the user")
    nodes, edges, index_map = _graph(keeper, dropped)
    nodes = _named(nodes, user="User")

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)
    assert edges == [keeper, dropped]


def test_target_named_in_the_keeper_fact_is_pruned() -> None:
    full = _edge("user", "jeans", "user bought black jeans from Levi's")
    restated = _edge("jeans", "levis", "jeans are from Levi's")
    nodes, edges, index_map = _graph(full, restated)
    nodes = _named(nodes, levis="Levi's")

    assert prune_contained_edges(nodes, edges, index_map, {"user"}) == (1, 1)


def test_target_absent_from_the_keeper_fact_is_kept() -> None:
    keeper = _edge("user", "premium", "The user's insurance premium went down by $20 in March.")
    dropped = _edge("premium", "insurer", "The user's insurance premium went down.")
    nodes, edges, index_map = _graph(keeper, dropped)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)
    assert edges == [keeper, dropped]


def test_fact_whose_subject_is_the_keeper_target_is_still_pruned() -> None:
    full = _edge("user", "jeans", "user bought black jeans from Levi's")
    restated = _edge("jeans", "levis", "black jeans")
    nodes, edges, index_map = _graph(full, restated)
    nodes = _named(nodes, levis="Levi's")

    assert prune_contained_edges(nodes, edges, index_map, {"user"}) == (1, 1)
