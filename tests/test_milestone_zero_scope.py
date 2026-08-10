"""Milestone 0 scope lock and evaluation fixture tests."""

from __future__ import annotations

import json
from pathlib import Path

from menhir.config import MilestoneZeroScope, load_scope


def test_milestone_zero_scope_contract() -> None:
    scope = load_scope()
    assert scope.is_deferred("confidence_weighted_emotions")
    assert scope.is_deferred("automated_hook_promotion")
    assert "v1_lifecycle" in scope.frozen_features
    assert "lifecycle_stage_stale_archived" in scope.deferred_features
    assert (
        "retrieval_relevance_vs_raw_vector"
        in scope.success_metrics
    )


def test_milestone_zero_fixed_query_set_has_ten_items() -> None:
    payload_path = Path(__file__).resolve().parent / "fixtures" / "milestone0_query_set.json"
    raw = payload_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert len(payload) == 10
    assert all(
        {"id", "query", "intent", "expected_keywords", "notes"} <= set(item)
        for item in payload
    )
    assert all(isinstance(item["expected_keywords"], list) for item in payload)
