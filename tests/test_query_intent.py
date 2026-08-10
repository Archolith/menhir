"""Tests for the task-intent classifier (IntentOracle producer, pure domain)."""

from __future__ import annotations

import pytest

from menhir.domain.query_intent import (
    IntentConfidence,
    TaskIntent,
    classify_intent,
    primary_intent,
)


@pytest.mark.parametrize("text,expected", [
    ("why is the floor failing", TaskIntent.DEBUG_FAILURE),
    ("have we already tried this floor", TaskIntent.AVOID_REPEAT),
    ("why did we choose the floor", TaskIntent.EXPLAIN_DECISION),
    ("is the floor doc still accurate", TaskIntent.VERIFY_CURRENTNESS),
    ("which benchmark verifies the floor", TaskIntent.EVIDENCE_LOOKUP),
    ("what changed in the floor", TaskIntent.CHANGE_ANALYSIS),
    ("what should i do next on the floor", TaskIntent.PLAN_NEXT_ACTION),
    ("how does the floor work", TaskIntent.UNDERSTAND_SYSTEM),
])
def test_primary_intent(text: str, expected: TaskIntent) -> None:
    hits, conf = classify_intent(text)
    assert primary_intent(hits) is expected
    assert conf is IntentConfidence.HIGH


def test_default_is_low_confidence() -> None:
    hits, conf = classify_intent("floor recall vector cosine")
    assert primary_intent(hits) is TaskIntent.UNDERSTAND_SYSTEM
    assert conf is IntentConfidence.LOW


def test_returns_literal_cue() -> None:
    hits, _ = classify_intent("why is it failing")
    assert any(h.cue == "failing" for h in hits)


def test_multi_hit_and_precedence() -> None:
    # AVOID_REPEAT ("already tried") + DEBUG_FAILURE ("failing"); precedence -> AVOID primary.
    hits, _ = classify_intent("have we already tried fixing the failing floor")
    intents = {h.intent for h in hits}
    assert TaskIntent.AVOID_REPEAT in intents
    assert TaskIntent.DEBUG_FAILURE in intents
    assert primary_intent(hits) is TaskIntent.AVOID_REPEAT


def test_debug_vs_explain_separable() -> None:
    assert primary_intent(classify_intent("why did we choose the floor")[0]) is TaskIntent.EXPLAIN_DECISION
    assert primary_intent(classify_intent("why is the floor failing")[0]) is TaskIntent.DEBUG_FAILURE
