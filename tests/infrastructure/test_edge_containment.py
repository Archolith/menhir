"""Contained-edge pruning: restated sub-facts go, unique detail and the self node stay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.edge_containment import fact_tokens, prune_contained_edges
from menhir.infrastructure.model_profiles import resolve_model_profile

pytestmark = pytest.mark.unit


def _node(uuid: str) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid, name=uuid)


def _edge(src: str, tgt: str, fact: str, episodes=None, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        source_node_uuid=src,
        target_node_uuid=tgt,
        fact=fact,
        episodes=list(episodes or ["ep-1"]),
        valid_at=extra.get("valid_at"),
        invalid_at=extra.get("invalid_at"),
        attributes=extra.get("attributes", {}),
    )


def _graph(*edges):
    uuids = sorted({u for e in edges for u in (e.source_node_uuid, e.target_node_uuid)})
    return [_node(u) for u in uuids], list(edges), {u: [0] for u in uuids}


def test_restated_sub_fact_on_another_pair_is_dropped_with_its_orphan() -> None:
    full = _edge("user", "jeans", "The user bought new black jeans from Levi's and loves them.")
    restated = _edge("jeans", "levis", "The user's new black jeans are from Levi's.")
    nodes, edges, index_map = _graph(full, restated)

    assert prune_contained_edges(nodes, edges, index_map, {"user"}) == (1, 1)

    assert edges == [full]
    assert [n.uuid for n in nodes] == ["jeans", "user"]
    assert "levis" not in index_map


def test_edge_with_a_number_or_date_the_keeper_lacks_is_kept() -> None:
    full = _edge("premium", "insurer", "The user's insurance premium went down.")
    detail = _edge("user", "premium", "The user's insurance premium went down by $20 in March.")
    nodes, edges, index_map = _graph(full, detail)

    # The detailed edge absorbs the bare one, never the other way round.
    assert prune_contained_edges(nodes, edges, index_map, {"insurer"}) == (1, 0)
    assert edges == [detail]


def test_negated_edge_is_not_absorbed_by_the_positive_one() -> None:
    positive = _edge("user", "jazz", "The user likes jazz played live at small clubs.")
    negated = _edge("user", "jazz", "The user does not like jazz.")
    nodes, edges, index_map = _graph(positive, negated)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)
    assert edges == [positive, negated]


def test_timestamp_mismatch_blocks_the_drop() -> None:
    full = _edge("user", "car", "The user had the car serviced at the dealership.")
    dated = _edge("user", "car", "The user had the car serviced.", valid_at="2023-03-15")
    nodes, edges, index_map = _graph(full, dated)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)


def test_attributes_the_keeper_lacks_block_the_drop() -> None:
    full = _edge("user", "car", "The user had the car serviced at the dealership.")
    tagged = _edge("user", "car", "The user had the car serviced.", attributes={"cost": 120})
    nodes, edges, index_map = _graph(full, tagged)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)


def test_role_reversal_on_the_same_pair_is_kept() -> None:
    called = _edge("user", "ann", "The user called Ann.")
    called_back = _edge("ann", "user", "Ann called the user.")
    nodes, edges, index_map = _graph(called, called_back)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)


def test_equal_token_sets_keep_the_longer_fact_and_merge_episodes() -> None:
    short = _edge("self", "loom", "User owns a loom.", episodes=["ep-2"])
    longer = _edge("user", "loom", "The user owns the loom.", episodes=["ep-1"])
    nodes, edges, index_map = _graph(short, longer)

    assert prune_contained_edges(nodes, edges, index_map, {"self"}) == (1, 0)
    assert edges == [longer]
    assert longer.episodes == ["ep-1", "ep-2"]


def test_protected_self_node_survives_even_when_orphaned() -> None:
    full = _edge("dolly", "harpsichord", "The furniture dolly moved Odile's harpsichord.")
    restated = _edge("self", "dolly", "furniture dolly")
    nodes, edges, index_map = _graph(full, restated)

    assert prune_contained_edges(nodes, edges, index_map, {"self"}) == (1, 0)
    assert "self" in {n.uuid for n in nodes}
    assert "self" in index_map


def test_node_already_edgeless_before_pruning_is_untouched() -> None:
    full = _edge("user", "jeans", "The user bought black jeans from Levi's.")
    restated = _edge("jeans", "levis", "black jeans from Levi's")
    nodes, edges, index_map = _graph(full, restated)
    nodes.append(_node("loose"))
    index_map["loose"] = [0]

    prune_contained_edges(nodes, edges, index_map)

    assert "loose" in {n.uuid for n in nodes}
    assert "loose" in index_map


def test_synthetic_membership_edges_are_never_pruned() -> None:
    full = _edge("user", "apple", "The user listed apple under fruit for the market stall.")
    synthetic = _edge("apple", "fruit", "[synthetic] apple is listed under fruit")
    nodes, edges, index_map = _graph(full, synthetic)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)


def test_single_edge_is_a_no_op() -> None:
    only = _edge("user", "loom", "User owns a loom.")
    nodes, edges, index_map = _graph(only)

    assert prune_contained_edges(nodes, edges, index_map) == (0, 0)
    assert edges == [only]


def test_fact_tokens_keep_digits_and_negations() -> None:
    tokens = fact_tokens("The user doesn't own 2 boots from Zara's store.")

    assert {"doesn", "t", "2", "boot", "zara", "store", "own"} <= tokens
    assert "user" not in tokens and "the" not in tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "kept_edges"), [("gpt-6-luna", 1), ("gpt-4o-mini", 2)])
async def test_extraction_hook_prunes_only_under_the_gpt6_profile(
    monkeypatch, model: str, kept_edges: int
) -> None:
    pytest.importorskip("graphiti_core")
    import menhir.infrastructure.graphiti_extraction_policy as policy

    full = _edge("user", "jeans", "The user bought new black jeans from Levi's.")
    restated = _edge("jeans", "levis", "The user's black jeans are from Levi's.")

    class _FakeClients:
        def __init__(self) -> None:
            self.llm_client = SimpleNamespace(model=model, config=SimpleNamespace(base_url=None))

        def model_copy(self, *, update: dict) -> "_FakeClients":
            copy = _FakeClients()
            copy.llm_client = update["llm_client"]
            return copy

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        nodes, edges, index_map = _graph(full, restated)
        return nodes, edges, index_map

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    receipt = policy.begin_extraction_receipt("ep-1", "user: I bought black jeans from Levi's.")
    try:
        context = SimpleNamespace(
            clients=_FakeClients(),
            episode=SimpleNamespace(uuid="ep-1"),
            previous_episodes=[],
            entity_types=None,
            excluded_entity_types=None,
            edge_type_map={},
            edge_types=None,
            custom_extraction_instructions=None,
        )
        result = await policy.MenhirExtractionHook().extract_single_episode(context)
    finally:
        policy.clear_extraction_receipt()

    assert len(result.edges) == kept_edges
    assert receipt.resolved_edge_count == kept_edges
    assert receipt.contained_edges_pruned == 2 - kept_edges


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-luna", True),
        ("gpt-4o-mini", False),
        ("openai/gpt-4o-mini", False),
        ("gpt-5.6", False),
        ("deepseek-v4-flash", False),
    ],
)
def test_pruning_is_enabled_only_for_the_gpt6_profile(model: str, expected: bool) -> None:
    assert resolve_model_profile(model).prune_contained_edges is expected
