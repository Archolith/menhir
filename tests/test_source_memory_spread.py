"""Source-memory session spread: selection and lane wiring."""

from __future__ import annotations

import pytest

from menhir.config import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.source_memory_spread import pool_size, spread_by_session
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


def _row(uuid: str, session: str | None, content: str | None = None, time: str = "2026-01-01") -> dict:
    return {"uuid": uuid, "session_id": session, "content": content if content is not None else f"text {uuid}",
            "source": "claude-code", "reference_time": time, "cosine": 0.5}


def _ids(rows) -> list[str]:
    return [r["uuid"] for r in rows]


def test_each_session_gets_its_best_row_before_a_second_from_any_session() -> None:
    # Cosine order: three from session A, then B's best, then C's best.
    rows = [_row("a1", "A"), _row("a2", "A"), _row("a3", "A"), _row("b1", "B"), _row("c1", "C"),
            _row("b2", "B")]
    assert _ids(spread_by_session(rows, 3)) == ["a1", "b1", "c1"]
    assert _ids(spread_by_session(rows, 5)) == ["a1", "b1", "c1", "a2", "a3"]


def test_more_sessions_than_slots_keeps_the_best_sessions_in_cosine_order() -> None:
    rows = [_row(f"s{i}", f"S{i}") for i in range(6)]
    assert _ids(spread_by_session(rows, 4)) == ["s0", "s1", "s2", "s3"]


def test_exact_content_repeats_are_dropped_keeping_the_first() -> None:
    rows = [_row("a1", "A", "I ride   my bike daily."), _row("b1", "B", "i ride my bike DAILY."),
            _row("b2", "B", "Something else")]
    assert _ids(spread_by_session(rows, 3)) == ["a1", "b2"]


def test_rows_without_a_session_are_each_their_own_session() -> None:
    rows = [_row("x1", None), _row("x2", ""), _row("x3", None)]
    # No sessions: plain cosine order, no reordering.
    assert _ids(spread_by_session(rows, 2)) == ["x1", "x2"]


def test_empty_contents_are_not_repeats_of_each_other() -> None:
    rows = [_row("e1", "A", ""), _row("e2", "B", "")]
    assert _ids(spread_by_session(rows, 2)) == ["e1", "e2"]


def test_limit_zero_and_pool_size_bounds() -> None:
    assert spread_by_session([_row("a", "A")], 0) == []
    assert pool_size(10) == 30 and pool_size(20) == 50 and pool_size(1) == 3


def _svc(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    return RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                         scoring_service=ScoringService())


@pytest.mark.asyncio
async def test_lane_fetches_a_larger_pool_and_spreads_it_when_on(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _row("a1", "A", time="2026-01-03"), _row("a2", "A", time="2026-01-03"),
        _row("a3", "A", time="2026-01-03"), _row("b1", "B", time="2026-01-01"),
        {"uuid": "", "session_id": "C", "content": "no uuid", "reference_time": "2026-01-02", "cosine": 0.4},
        _row("c1", "C", time="2026-01-02"),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", include_session=True,
        tuning=RetrievalTuningConfig(enable_source_memories=True, source_memory_limit=3,
                                     source_memory_spread=True),
    )
    assert stub_memory_graph_adapter.episode_embedding_calls[0]["limit"] == 9
    # One per session (a uuid-less row never takes a slot), then oldest first as before.
    assert [sm.uuid for sm in result.source_memories] == ["b1", "c1", "a1"]


@pytest.mark.asyncio
async def test_lane_is_unchanged_when_off(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _row("a1", "A", time="2026-01-03"), _row("a2", "A", "text a1", time="2026-01-02"),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", include_session=True,
        tuning=RetrievalTuningConfig(enable_source_memories=True, source_memory_limit=3,
                                     source_memory_spread=False),
    )
    assert stub_memory_graph_adapter.episode_embedding_calls[0]["limit"] == 3
    # Off keeps repeats and same-session rows: today's output.
    assert [sm.uuid for sm in result.source_memories] == ["a2", "a1"]


def test_spread_defaults_on_in_tuning_and_settings() -> None:
    assert RetrievalTuningConfig().source_memory_spread is True
    assert MemorySettings().retrieval_tuning().source_memory_spread is True
