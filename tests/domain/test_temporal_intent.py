"""Tests for temporal-intent detection (Chronostratum Rung 2)."""

from __future__ import annotations

from menhir.domain.temporal import TemporalQuery
from menhir.domain.temporal_intent import classify_temporal_intent


def test_default_is_current_belief() -> None:
    i = classify_temporal_intent("where does the card mapping live")
    assert i.query is TemporalQuery.CURRENT_BELIEF
    assert i.include_invalidated is False


def test_explicit_current_cue() -> None:
    i = classify_temporal_intent("what is the recall floor currently")
    assert i.query is TemporalQuery.CURRENT_BELIEF
    assert i.cue == "currently"


def test_history_cue_includes_invalidated() -> None:
    i = classify_temporal_intent("what did we believe before the load-order fix")
    assert i.query is TemporalQuery.AS_KNOWN_AT
    assert i.include_invalidated is True
    assert i.cue in ("what did we believe", "before")


def test_regression_cue() -> None:
    i = classify_temporal_intent("did the partition logic regress")
    assert i.query is TemporalQuery.AS_KNOWN_AT
    assert i.include_invalidated is True


def test_as_of_date_is_world_time() -> None:
    i = classify_temporal_intent("what was true as of 2026-01-08")
    assert i.query is TemporalQuery.AS_OF_WORLD
    assert i.as_of == "2026-01-08"
    assert i.include_invalidated is True


def test_as_of_precedence_over_history_cue() -> None:
    # both an "as of <date>" and a history word; world-time wins
    i = classify_temporal_intent("what did we believe as of 2026-01-07")
    assert i.query is TemporalQuery.AS_OF_WORLD
    assert i.as_of == "2026-01-07"
