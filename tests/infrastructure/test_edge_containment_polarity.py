"""Containment must never drop a fact whose truth or modality the keeper changes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.edge_containment import fact_markers, prune_contained_edges

pytestmark = pytest.mark.unit


def _edge(fact: str, src: str = "user", tgt: str = "jazz") -> SimpleNamespace:
    return SimpleNamespace(
        source_node_uuid=src,
        target_node_uuid=tgt,
        fact=fact,
        episodes=["ep-1"],
        valid_at=None,
        invalid_at=None,
        attributes={},
    )


def _prune(*edges):
    uuids = sorted({u for e in edges for u in (e.source_node_uuid, e.target_node_uuid)})
    nodes = [SimpleNamespace(uuid=u) for u in uuids]
    edge_list = list(edges)
    return prune_contained_edges(nodes, edge_list, {u: [0] for u in uuids}), edge_list


@pytest.mark.parametrize(
    ("keeper", "plain"),
    [
        ("The user does not like jazz.", "The user likes jazz."),
        ("The user doesn't like jazz.", "The user likes jazz."),
        ("The user never liked jazz.", "The user liked jazz."),
        ("The user cannot play jazz.", "The user plays jazz."),
        ("The user might like jazz.", "The user likes jazz."),
        ("The user will see jazz live.", "The user sees jazz live."),
        ("The user plans to see jazz live.", "The user sees jazz live."),
        ("The user wants to learn jazz piano.", "The user learns jazz piano."),
        ("The user stopped playing jazz.", "The user plays jazz."),
        ("The user no longer plays jazz.", "The user plays jazz."),
        ("The user used to play jazz.", "The user plays jazz."),
    ],
)
def test_marked_keeper_never_absorbs_the_plain_fact(keeper: str, plain: str) -> None:
    (pruned, _), edges = _prune(_edge(keeper), _edge(plain))

    assert pruned == 0
    assert {e.fact for e in edges} == {keeper, plain}


def test_inverse_polarity_on_another_endpoint_pair_is_kept() -> None:
    negative = _edge("The user does not own black jeans from Levi's.", "user", "jeans")
    positive = _edge("black jeans from Levi's", "jeans", "levis")

    (pruned, dropped_nodes), edges = _prune(negative, positive)

    assert (pruned, dropped_nodes) == (0, 0)
    assert len(edges) == 2


def test_same_markers_still_prune_the_restated_fact() -> None:
    full = _edge("The user does not like jazz played at loud clubs.")
    short = _edge("The user does not like jazz.")

    (pruned, _), edges = _prune(full, short)

    assert pruned == 1
    assert [e.fact for e in edges] == [full.fact]


def test_markers_are_read_before_stopword_removal() -> None:
    assert fact_markers("The user might buy it.") == {"might"}
    assert fact_markers("The user can't swim.") == {"can", "not"}
    assert fact_markers("The user cannot swim.") == {"can", "not"}
    assert fact_markers("The user likes jazz.") == frozenset()
