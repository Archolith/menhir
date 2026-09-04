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


@pytest.mark.unit
def test_llm_outcomes_are_recorded(monkeypatch):
    """REVIEW P2. The similarity wrapper cannot see what the LLM decided, so on its own it leaves
    the exact branch the RCA implicated -- an escalation returning duplicate_candidate_id = -1,
    which mints another node -- unrecorded."""
    from menhir.infrastructure.graphiti_model_patches import _record_resolution_outcomes

    recorded: list[dict] = []

    import menhir.infrastructure.telemetry.recorders as recorders

    def _capture(*, component, event, state, episode_uuid=None, details=None, **kw):
        recorded.append({"event": event, "details": details or {}})

    monkeypatch.setattr(recorders, "record_lifecycle_event", _capture)

    extracted = [_node("Rachel", "e1"), _node("Chicago", "e2"), _node("user", "e3")]
    # e1 resolved onto an existing candidate; e2 kept its own uuid (a new node); e3 unresolved.
    resolved = [_node("Rachel", "existing-1"), extracted[1], None]
    state = _state(resolved, [])
    _record_resolution_outcomes(
        SimpleNamespace(embedder=None), extracted, [[object()], [object()], []],
        [0, 1, 2], state, set(),
    )

    assert recorded, "no resolution outcome event was recorded"
    d = recorded[0]["details"]
    assert d["llm_selected_candidate"] == 1
    assert d["llm_selected_new"] == 2
    assert d["escalated_to_llm"] == 3
    assert d["unresolved_after_llm"] == 1
    assert d["no_candidates_new"] == 1
    assert "embedding_dimension" in d and "embedding_model" in d


@pytest.mark.unit
def test_outcome_telemetry_never_raises():
    """Sits in the dedup hot path for every entity."""
    from menhir.infrastructure.graphiti_model_patches import _record_resolution_outcomes

    _record_resolution_outcomes(None, [SimpleNamespace()], [None], [0], _state([None], []), set())


@pytest.mark.unit
def test_candidate_score_bounds_are_measured_not_inferred():
    """Graphiti's candidate search discards the cosine score it ranked by, so the bound is
    measured here from the two name embeddings. The RCA's mechanism is a window saturated at
    cosine 1.0; that signature has to be visible in telemetry to be attributable."""
    from menhir.infrastructure.graphiti_model_patches import _candidate_score_bounds

    extracted = _node("user", "e1")
    extracted.name_embedding = [1.0, 0.0]
    identical = _node("user", "c1")
    identical.name_embedding = [1.0, 0.0]
    orthogonal = _node("Rachel", "c2")
    orthogonal.name_embedding = [0.0, 1.0]

    low, high, measured = _candidate_score_bounds([extracted], [[identical, orthogonal]])

    assert measured == 2
    assert high == pytest.approx(1.0)
    assert low == pytest.approx(0.0)


@pytest.mark.unit
def test_unmeasurable_candidates_report_zero_pairs_not_a_zero_score():
    """Candidates hydrated without name_embedding are unmeasurable. Reporting 0.0 would read as
    'no similar candidates', which is the opposite of what an unmeasured window means."""
    from menhir.infrastructure.graphiti_model_patches import _candidate_score_bounds

    low, high, measured = _candidate_score_bounds([_node("user", "e1")], [[_node("user", "c1")]])

    assert (low, high, measured) == (None, None, 0)


@pytest.mark.unit
def test_prompt_sections_size_the_fields_graphiti_serializes():
    from menhir.infrastructure.graphiti_model_patches import _measure_prompt_sections

    candidate = _node("Rachel", "c1")
    candidate.summary = "x" * 500

    sizes = _measure_prompt_sections([_node("user", "e1")], [candidate])

    assert sizes["entity_count"] == 1
    assert sizes["candidate_count"] == 1
    # The candidate summary is sliced to 120 characters in the dedupe prompt, so the measurement
    # must not report the full 500.
    assert sizes["candidate_chars"] == len("Rachel") + 120


@pytest.mark.unit
def test_outcome_event_carries_the_score_and_prompt_fields(monkeypatch):
    from menhir.infrastructure.graphiti_model_patches import _record_resolution_outcomes

    recorded: list[dict] = []
    import menhir.infrastructure.telemetry.recorders as recorders

    monkeypatch.setattr(
        recorders,
        "record_lifecycle_event",
        lambda **kw: recorded.append(kw.get("details") or {}),
    )

    _record_resolution_outcomes(
        SimpleNamespace(embedder=None),
        [_node("user", "e1")],
        [[]],
        [],
        _state([None], []),
        set(),
        [{"entity_count": 1, "entity_chars": 4, "candidate_count": 15, "candidate_chars": 900}],
    )

    d = recorded[0]
    assert d["candidate_scores_measured"] == 0
    assert d["candidate_score_max"] is None
    assert d["llm_prompt_batches"] == 1
    assert d["llm_prompt_candidate_count_max"] == 15
    assert d["llm_prompt_candidate_chars_max"] == 900
