"""Tests for #172: adaptive brief timeline (instant ordering + long-running gate)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from menhir.domain.brief_builder import build_timeline_bundle
from menhir.domain.recall import RecallResult, ScoredMemory, TemporalFact
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.services.context_builder import ContextBuilderService


_DEFAULT_BREAKDOWN = RelevanceBreakdown(
    semantic_similarity=0.9,
    adjacency_bonus=0.0,
    recency_bonus=0.0,
    prominence_bonus=0.0,
    conflict_bonus=0.0,
    type_boost=0.0,
    preset="knowledge",
    alpha=0.2,
    beta=0.1,
    gamma=0.1,
    delta=0.0,
)


def _mem(uuid: str, name: str, content: str, score: float) -> ScoredMemory:
    return ScoredMemory(
        uuid=uuid,
        name=name,
        content=content,
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        final_score=score,
        breakdown=_DEFAULT_BREAKDOWN,
    )


def _fact(
    fact: str,
    valid_at: str,
    *,
    invalid_at: str | None = None,
    current: bool = True,
) -> TemporalFact:
    return TemporalFact(
        fact, valid_at, invalid_at, None, None, current,
        "current_belief" if current else "superseded_belief",
    )


def _recall_result(memories: list[ScoredMemory]) -> RecallResult:
    return RecallResult(
        query="test query",
        preset="knowledge",
        results=memories,
        candidates_evaluated=len(memories),
        nodes_touched=len(memories),
    )


def _brief_service(
    recall_result: RecallResult, *, min_timeline_points: int = 3,
) -> ContextBuilderService:
    mock_recall = AsyncMock()
    mock_recall.recall = AsyncMock(return_value=recall_result)
    return ContextBuilderService(
        recall_service=mock_recall,
        brief_builder_enabled=True,
        brief_min_timeline_points=min_timeline_points,
    )


def test_same_day_history_orders_by_instant_and_dedups_by_instant():
    # Facts attached in scrambled instant order; m1 also repeats m2's fact text at a
    # different instant to prove the dedup key is (instant, text), not (day, text).
    memories = [
        ScoredMemory(**{
            **_mem("m1", "steps", None, 0.8).__dict__,
            "temporal_facts": (
                _fact("step three", "2026-01-01T00:03:00Z"),
                _fact("step one", "2026-01-01T00:01:00Z"),
            ),
        }),
        ScoredMemory(**{
            **_mem("m2", "steps", None, 0.9).__dict__,
            "temporal_facts": (
                _fact("step one", "2026-01-01T00:05:00Z"),
            ),
        }),
        ScoredMemory(**{
            **_mem("m3", "steps", None, 0.7).__dict__,
            "temporal_facts": (
                _fact("step five", "2026-01-01T00:05:00Z"),
            ),
        }),
    ]
    bundle = build_timeline_bundle(memories)
    assert bundle is not None
    assert list(bundle.lines) == [
        "- [2026-01-01 00:01] step one (current)",
        "- [2026-01-01 00:03] step three (current)",
        "- [2026-01-01 00:05] step one (current)",
        "- [2026-01-01 00:05] step five (current)",
    ]


def test_distinct_day_history_keeps_day_labels():
    memories = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (_fact("crash reported", "2026-01-01"),),
        }),
        ScoredMemory(**{
            **_mem("m2", "b", None, 0.8).__dict__,
            "temporal_facts": (_fact("patch landed", "2026-01-02"),),
        }),
    ]
    bundle = build_timeline_bundle(memories)
    assert bundle is not None
    assert list(bundle.lines) == [
        "- [2026-01-01] crash reported (current)",
        "- [2026-01-02] patch landed (current)",
    ]


def test_same_minute_distinct_instants_use_seconds():
    memories = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (
                _fact("tick", "2026-01-01T00:01:10Z"),
                _fact("tock", "2026-01-01T00:01:40Z"),
            ),
        }),
    ]
    bundle = build_timeline_bundle(memories)
    assert bundle is not None
    assert list(bundle.lines) == [
        "- [2026-01-01 00:01:10] tick (current)",
        "- [2026-01-01 00:01:40] tock (current)",
    ]


def test_superseded_until_follows_label_precision():
    memories = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (
                _fact(
                    "old value", "2026-01-01T00:01:00Z",
                    invalid_at="2026-01-01T00:09:00Z", current=False,
                ),
                _fact("new value", "2026-01-01T00:09:00Z"),
            ),
        }),
    ]
    bundle = build_timeline_bundle(memories)
    assert bundle is not None
    assert list(bundle.lines) == [
        "- [2026-01-01 00:01] old value (superseded until 2026-01-01 00:09)",
        "- [2026-01-01 00:09] new value (current)",
    ]


def test_utc_suffix_parses_and_unparseable_sorts_last_without_raising():
    memories = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (
                _fact("zoned", "2026-01-01T00:01:00Z[UTC]"),
                _fact("broken", "not-a-timestamp"),
                _fact("later", "2026-01-01T00:02:00Z[UTC]"),
            ),
        }),
    ]
    bundle = build_timeline_bundle(memories)
    assert bundle is not None
    assert list(bundle.lines) == [
        "- [2026-01-01 00:01] zoned (current)",
        "- [2026-01-01 00:02] later (current)",
        "- [not-a-time] broken (current)",
    ]


def test_gate_returns_none_below_min_distinct_instants():
    two = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (
                _fact("first", "2026-01-01T00:01:00Z"),
                _fact("second", "2026-01-01T00:02:00Z"),
            ),
        }),
    ]
    three = [
        ScoredMemory(**{
            **two[0].__dict__,
            "temporal_facts": (
                _fact("first", "2026-01-01T00:01:00Z"),
                _fact("second", "2026-01-01T00:02:00Z"),
                _fact("third", "2026-01-01T00:03:00Z"),
            ),
        }),
    ]
    assert build_timeline_bundle(two, min_points=3) is None
    assert build_timeline_bundle(three, min_points=3) is not None
    single = [
        ScoredMemory(**{
            **_mem("m1", "a", None, 0.9).__dict__,
            "temporal_facts": (_fact("only", "2026-01-01"),),
        }),
    ]
    assert build_timeline_bundle(single) is not None


@pytest.mark.asyncio
async def test_context_builder_gate_hides_timeline_below_min_points():
    def _mems(count: int):
        facts = tuple(
            _fact(f"step {i}", f"2026-01-01T00:0{i}:00Z") for i in range(1, count + 1)
        )
        return [ScoredMemory(**{
            **_mem("m1", "steps", None, 0.9).__dict__,
            "temporal_facts": facts,
        })]

    with patch("menhir.services.context_builder._ESTIMATION_MODE", "heuristic"), \
            patch("menhir.services.context_builder._tiktoken_available", False):
        two = await _brief_service(_recall_result(_mems(2))).build_context(
            "test query", max_tokens=4000,
        )
        assert "=== Timeline ===" not in two.context

        three = await _brief_service(_recall_result(_mems(3))).build_context(
            "test query", max_tokens=4000,
        )
        assert "=== Timeline ===" in three.context

        relaxed = await _brief_service(
            _recall_result(_mems(2)), min_timeline_points=1,
        ).build_context("test query", max_tokens=4000)
        assert "=== Timeline ===" in relaxed.context


def test_settings_parse_clamp_and_default(monkeypatch):
    from menhir.config.settings_model import MemorySettings

    assert MemorySettings.frontier_brief_min_timeline_points == 3

    monkeypatch.setenv("MENHIR_FRONTIER_BRIEF_MIN_TIMELINE_POINTS", "5")
    assert MemorySettings.from_env().frontier_brief_min_timeline_points == 5

    monkeypatch.setenv("MENHIR_FRONTIER_BRIEF_MIN_TIMELINE_POINTS", "0")
    assert MemorySettings.from_env().frontier_brief_min_timeline_points == 1
