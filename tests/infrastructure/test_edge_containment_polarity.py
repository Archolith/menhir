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


@pytest.mark.parametrize(
    ("keeper", "inner"),
    [
        # R2-1: a narrower negative does not imply the general negative.
        ("The user does not like jazz played at loud clubs.", "The user does not like jazz."),
        # R2-1: denial and reporting verbs do not assert the embedded fact.
        ("The user denies that she likes jazz.", "The user likes jazz."),
        ("The user denies liking jazz.", "The user likes jazz."),
        ("The user said she likes jazz.", "The user likes jazz."),
        ("The user thinks Ann likes jazz.", "Ann likes jazz."),
    ],
)
@pytest.mark.parametrize("pairs", ["same", "cross"])
def test_negated_or_embedded_pairs_are_kept(keeper: str, inner: str, pairs: str) -> None:
    inner_edge = _edge(inner) if pairs == "same" else _edge(inner, "ann", "jazz")
    (pruned, _), edges = _prune(_edge(keeper), inner_edge)

    assert pruned == 0
    assert {e.fact for e in edges} == {keeper, inner}


def test_same_pair_plain_restatement_is_left_to_graphiti() -> None:
    full = _edge("The user likes jazz played at small clubs.")
    short = _edge("The user likes jazz.")

    (pruned, _), edges = _prune(full, short)

    assert pruned == 0
    assert len(edges) == 2


def test_cross_pair_plain_restatement_is_still_pruned() -> None:
    full = _edge("The user bought black jeans from Levi's.", "user", "jeans")
    short = _edge("black jeans from Levi's", "jeans", "levis")

    (pruned, _), edges = _prune(full, short)

    assert pruned == 1
    assert edges == [full]


@pytest.mark.asyncio
async def test_extraction_hook_keeps_negated_and_denied_facts(monkeypatch) -> None:
    pytest.importorskip("graphiti_core")
    import menhir.infrastructure.graphiti_extraction_policy as policy

    extracted = [
        _edge("The user denies that she likes jazz.", "user", "jazz"),
        _edge("The user likes jazz.", "self", "jazz"),
        _edge("The user does not like jazz played at loud clubs.", "user", "clubs"),
        _edge("The user does not like jazz.", "self", "jazz"),
    ]

    class _FakeClients:
        def __init__(self) -> None:
            self.llm_client = SimpleNamespace(model="gpt-6-luna",
                                              config=SimpleNamespace(base_url=None))

        def model_copy(self, *, update: dict) -> "_FakeClients":
            copy = _FakeClients()
            copy.llm_client = update["llm_client"]
            return copy

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        uuids = sorted({u for e in extracted for u in (e.source_node_uuid, e.target_node_uuid)})
        return [SimpleNamespace(uuid=u, name=u) for u in uuids], list(extracted), {
            u: [0] for u in uuids}

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    receipt = policy.begin_extraction_receipt("ep-1", "user: I don't like jazz.")
    try:
        context = SimpleNamespace(
            clients=_FakeClients(), episode=SimpleNamespace(uuid="ep-1"), previous_episodes=[],
            entity_types=None, excluded_entity_types=None, edge_type_map={}, edge_types=None,
            custom_extraction_instructions=None)
        result = await policy.MenhirExtractionHook().extract_single_episode(context)
    finally:
        policy.clear_extraction_receipt()

    assert {e.fact for e in result.edges} == {e.fact for e in extracted}
    assert receipt.contained_edges_pruned == 0


def test_markers_are_read_before_stopword_removal() -> None:
    assert fact_markers("The user might buy it.") == {"might"}
    assert fact_markers("The user can't swim.") == {"can", "not"}
    assert fact_markers("The user cannot swim.") == {"can", "not"}
    assert fact_markers("The user likes jazz.") == frozenset()
