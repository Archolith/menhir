"""Phase 5: which deterministic-resolution branch each ordinary node took.

The RCA could only *infer* that production was taking the ambiguous branch, because nothing
recorded it. With 66 exact-name `user` nodes against a 15-candidate window, `unique_exact_bind`
is arithmetically unreachable and every extraction lands in `multiple_exact_llm`, where a
`duplicate_candidate_id = -1` mints another fork. These counters make a recurrence attributable
instead of reconstructed after the fact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_model_patches import (
    _classify_dedup_branches,
    _patch_graphiti_dedup_branch_telemetry,
)


def _node(name: str, uuid: str = "x"):
    from graphiti_core.nodes import EntityNode

    return EntityNode(
        uuid=uuid, name=name, group_id="", labels=["Entity"],
        created_at=datetime.now(timezone.utc),
    )


def _indexes(normalized_existing: dict):
    return SimpleNamespace(normalized_existing=normalized_existing)


def _state(resolved, unresolved):
    return SimpleNamespace(resolved_nodes=resolved, unresolved_indices=unresolved, uuid_map={})


@pytest.mark.unit
def test_unique_exact_match_is_the_deterministic_bind():
    nodes = [_node("Rachel")]
    counts = _classify_dedup_branches(
        nodes, _indexes({"rachel": [object()]}), set(), _state([object()], [])
    )
    assert counts["unique_exact_bind"] == 1
    assert counts["multiple_exact_llm"] == 0


@pytest.mark.unit
def test_the_production_failure_mode_is_counted_distinctly():
    """The exact shape the RCA measured: many exact matches for one name, so the deterministic
    branch is unreachable and the node is escalated to the LLM."""
    nodes = [_node("user")]
    counts = _classify_dedup_branches(
        nodes, _indexes({"user": [object()] * 15}), set(), _state([None], [0])
    )
    assert counts["multiple_exact_llm"] == 1
    assert counts["unique_exact_bind"] == 0


@pytest.mark.unit
def test_low_entropy_name_with_no_exact_match_is_the_entropy_guard():
    """`user` is low entropy, so with zero exact matches it never reaches fuzzy matching."""
    nodes = [_node("user")]
    counts = _classify_dedup_branches(nodes, _indexes({}), set(), _state([None], [0]))
    assert counts["entropy_guard_skip"] == 1


@pytest.mark.unit
def test_counts_cover_every_node_exactly_once():
    nodes = [_node("user"), _node("Rachel"), _node("Chicago")]
    counts = _classify_dedup_branches(
        nodes,
        _indexes({"rachel": [object()], "chicago": [object()] * 3}),
        set(),
        _state([None, object(), None], [0, 2]),
    )
    assert sum(counts.values()) == len(nodes)


@pytest.mark.unit
def test_classification_never_raises_on_malformed_input():
    """Instrumentation sits in the dedup hot path; a defect here must not fail an ingest."""
    counts = _classify_dedup_branches(
        [SimpleNamespace()], _indexes({}), set(), _state([None], [])
    )
    assert sum(counts.values()) == 0


@pytest.mark.unit
def test_patch_is_idempotent_and_preserves_resolution_behavior():
    """Applied twice must not double-wrap, and the wrapped resolver must still resolve."""
    from graphiti_core.utils.maintenance import node_operations as no

    original = no._resolve_with_similarity
    try:
        _patch_graphiti_dedup_branch_telemetry()
        once = no._resolve_with_similarity
        _patch_graphiti_dedup_branch_telemetry()
        assert no._resolve_with_similarity is once, "patch double-wrapped the resolver"

        # Real resolution through the wrapper: one exact candidate must still bind.
        existing = _node("Rachel", uuid="existing-1")
        extracted = [_node("Rachel", uuid="extracted-1")]
        indexes = no._build_candidate_indexes([existing])
        state = no.DedupResolutionState(resolved_nodes=[None], uuid_map={}, unresolved_indices=[])
        no._resolve_with_similarity(extracted, indexes, state)
        assert state.resolved_nodes[0] is not None
        assert state.uuid_map["extracted-1"] == "existing-1"
    finally:
        no._resolve_with_similarity = original
