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
def test_prompt_sections_count_candidate_attributes():
    """REVIEW P2. Measuring only name/labels/summary undercounts by whatever matters most: a
    candidate carrying a 1,000-character attribute was reported as nine characters, so the number
    could not answer what is actually filling a saturated dedupe prompt."""
    from menhir.infrastructure.graphiti_model_patches import _measure_prompt_sections

    candidate = _node("Rachel", "c1")
    candidate.summary = "x" * 500
    candidate.attributes = {"bio": "y" * 1000}

    sizes = _measure_prompt_sections([_node("user", "e1")], [candidate])

    assert sizes["candidate_chars"] > 1000, "the candidate attribute was not counted"
    # ...while the summary is still sliced to 120 in the prompt, so the full 500 is NOT counted.
    assert sizes["candidate_chars"] < 1000 + 500


@pytest.mark.unit
def test_prompt_sections_count_the_episode_and_previous_episodes():
    """The other half of the undercount: both were omitted entirely."""
    from types import SimpleNamespace

    from menhir.infrastructure.graphiti_model_patches import _measure_prompt_sections

    sizes = _measure_prompt_sections(
        [_node("user", "e1")],
        [],
        None,
        SimpleNamespace(content="e" * 300),
        [SimpleNamespace(content="p" * 200, valid_at=None)],
    )

    assert sizes["episode_chars"] >= 300
    assert sizes["previous_episode_count"] == 1
    assert sizes["previous_episode_chars"] >= 200
    assert sizes["total_chars"] >= sizes["episode_chars"] + sizes["previous_episode_chars"]


@pytest.mark.unit
def test_prompt_measurement_never_raises_on_malformed_input():
    """It runs per dedupe batch in the ingest path."""
    from menhir.infrastructure.graphiti_model_patches import _measure_prompt_sections

    sizes = _measure_prompt_sections([object()], [object()], object(), object(), object())
    assert sizes["entity_count"] == 1


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
        [
            {
                "entity_count": 1,
                "entity_chars": 100,
                "candidate_count": 15,
                "candidate_chars": 900,
                "episode_chars": 200,
                "previous_episode_count": 0,
                "previous_episode_chars": 0,
                "total_chars": 1200,
            }
        ],
    )

    d = recorded[0]
    assert d["llm_prompt_batches"] == 1
    assert d["llm_prompt_candidate_count_max"] == 15
    assert d["llm_prompt_candidate_chars_max"] == 900
    assert d["llm_prompt_total_chars_max"] == 1200
    # Per-candidate cosine scores are deliberately NOT here: graphiti's search discards the score
    # it ranked by, so any value would have been measured from embeddings that are None in
    # production. The saturation signature stays visible in candidate_count_max.
    assert "candidate_score_max" not in d
    assert "candidate_count_max" in d
