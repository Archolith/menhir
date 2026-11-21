"""P7: source memories carry a relevance rank, and build_context admits them one at a time.

Recall keeps the section oldest-first on the wire but stamps each entry's cosine rank, so
``build_context`` can admit the best matches first within its token budget instead of
dropping the whole section when it does not fit.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from menhir.domain.recall import RecallResult, ScoredMemory, SourceMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.context_builder import (
    SOURCE_MEMORY_HEADER,
    ContextBuilderService,
    _pack_source_memories,
    estimate_tokens,
)
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

# ---------------------------------------------------------------------------
# Recall: rank field
# ---------------------------------------------------------------------------


def _meta(uuid: str, name: str) -> dict:
    from datetime import datetime, timezone

    return {
        "uuid": uuid, "name": name, "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": "content", "summary": None,
        "last_accessed": datetime(2026, 9, 1, tzinfo=UTC),
        "edge_count": 1, "sharpness": 0.1, "freshness": "ACTIVE", "user_flagged": False,
    }


def _row(uuid: str, reference_time: str, cosine: float, session: str = "s1") -> dict:
    return {
        "uuid": uuid, "content": f"content of {uuid}", "source": "claude-code",
        "session_id": session, "reference_time": reference_time, "cosine": cosine,
    }


def _svc(graphiti, adapter) -> RecallService:
    graphiti.search_scored_results = [("entity-1", "Entity One", 0.9)]
    adapter.candidate_metadata = [_meta("entity-1", "Entity One")]
    return RecallService(
        graphiti_client=graphiti, graph_adapter=adapter, scoring_service=ScoringService(),
    )


@pytest.mark.asyncio
async def test_lane_ranks_by_cosine_and_keeps_oldest_first_order(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Search returns best-first; the newest episode is the best match.
    stub_memory_graph_adapter.episode_embedding_results = [
        _row("ep-c", "2026-03-01", 0.95),
        _row("ep-a", "2026-01-01", 0.80),
        _row("ep-b", "2026-02-01", 0.60),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", tuning=RetrievalTuningConfig(enable_source_memories=True), include_session=True,
    )

    section = result.source_memories
    assert section is not None
    assert [sm.uuid for sm in section] == ["ep-a", "ep-b", "ep-c"]  # display: oldest first
    assert {sm.uuid: sm.rank for sm in section} == {"ep-c": 1, "ep-a": 2, "ep-b": 3}


@pytest.mark.asyncio
async def test_lane_rank_ties_keep_search_order(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _row("ep-z", "2026-01-02", 0.7),
        _row("ep-y", "2026-01-01", 0.7),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", tuning=RetrievalTuningConfig(enable_source_memories=True), include_session=True,
    )
    assert {sm.uuid: sm.rank for sm in result.source_memories} == {"ep-z": 1, "ep-y": 2}


@pytest.mark.asyncio
async def test_lane_ranks_are_by_cosine_under_session_spread(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Spread picks each session's best first; the rank still reflects cosine.
    stub_memory_graph_adapter.episode_embedding_results = [
        _row("s1-best", "2026-01-01", 0.9, "s1"),
        _row("s1-next", "2026-01-02", 0.8, "s1"),
        _row("s2-best", "2026-01-03", 0.5, "s2"),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q",
        tuning=RetrievalTuningConfig(
            enable_source_memories=True, source_memory_limit=3, source_memory_spread=True,
        ),
        include_session=True,
    )
    assert {sm.uuid: sm.rank for sm in result.source_memories} == {
        "s1-best": 1, "s1-next": 2, "s2-best": 3,
    }


def test_recall_response_model_carries_rank() -> None:
    from menhir.api.routes_support import SourceMemoryResponse

    sm = SourceMemoryResponse(uuid="u", content="c", cosine=0.5, rank=2)
    assert sm.rank == 2
    assert SourceMemoryResponse(uuid="u", content="c", cosine=0.5).rank is None


# ---------------------------------------------------------------------------
# build_context: one-at-a-time admission
# ---------------------------------------------------------------------------


def _tokens(text: str) -> int:
    return estimate_tokens(text + "\n\n")[0]


def _src(uuid: str, when: str, chars: int, rank: int | None, cosine: float = 0.5) -> SourceMemory:
    return SourceMemory(
        uuid=uuid, content=f"{uuid}:" + "w " * (chars // 2), reference_time=when,
        cosine=cosine, source="t", rank=rank,
    )


def test_pack_all_fit_matches_previous_whole_block() -> None:
    sources = (_src("a", "2026-01-01", 40, 2), _src("b", "2026-02-01", 40, 1))
    block, tokens, cut = _pack_source_memories(sources, 10_000, _tokens)
    expected = "\n".join(
        [SOURCE_MEMORY_HEADER]
        + [f"  - {s.reference_time} | {s.content}" for s in sources]
    )
    assert block == expected and tokens == _tokens(expected) and cut is False


def test_pack_first_ranked_source_alone_exceeds_budget() -> None:
    big = _src("big", "2026-01-01", 4000, 1)
    small1 = _src("s1", "2026-03-01", 40, 2)
    small2 = _src("s2", "2026-02-01", 40, 3)
    # Room for both small sources, far short of the big one.
    budget = _tokens("\n".join(
        [SOURCE_MEMORY_HEADER]
        + [f"  - {s.reference_time} | {s.content}" for s in (small2, small1)]
    )) + 5
    block, tokens, cut = _pack_source_memories((big, small2, small1), budget, _tokens)

    assert block is not None and cut is True and tokens <= budget
    assert "big:" not in block
    assert block.index("s2:") < block.index("s1:")  # admitted ones shown oldest first


def test_pack_budget_fits_k_of_n_keeps_best_ranks() -> None:
    # Five equal-size sources in chronological order; ranks put the newest best.
    sources = tuple(
        _src(f"e{i}", f"2026-0{i + 1}-01", 400, rank=5 - i) for i in range(5)
    )
    two = "\n".join(
        [SOURCE_MEMORY_HEADER] + [f"  - {s.reference_time} | {s.content}" for s in sources[3:]]
    )
    budget = _tokens(two) + 5  # room for two, not three
    block, tokens, cut = _pack_source_memories(sources, budget, _tokens)

    assert cut is True and tokens <= budget
    present = [s.uuid for s in sources if f"{s.uuid}:" in block]
    assert present == ["e3", "e4"]  # ranks 2 and 1


def test_pack_without_rank_falls_back_to_cosine() -> None:
    sources = (
        _src("lo", "2026-01-01", 400, None, cosine=0.2),
        _src("hi", "2026-02-01", 400, None, cosine=0.9),
    )
    one = "\n".join(
        [SOURCE_MEMORY_HEADER, f"  - {sources[1].reference_time} | {sources[1].content}"]
    )
    block, _, cut = _pack_source_memories(sources, _tokens(one) + 5, _tokens)
    assert cut is True and "hi:" in block and "lo:" not in block


def test_pack_nothing_fits_returns_none() -> None:
    block, tokens, cut = _pack_source_memories((_src("a", "t", 400, 1),), 3, _tokens)
    assert (block, tokens, cut) == (None, 0, True)


def _item(uuid: str, content: str) -> ScoredMemory:
    return ScoredMemory(
        uuid=uuid, name=uuid.upper(), content=content, scope="PERSISTENT",
        memory_type="SEMANTIC", final_score=1.0,
        breakdown=RelevanceBreakdown(
            semantic_similarity=1.0, adjacency_bonus=0.0, recency_bonus=0.0,
            prominence_bonus=0.0, conflict_bonus=0.0, type_boost=0.0,
            preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
        ),
    )


async def _build(result: RecallResult, max_tokens: int) -> Any:
    class _Stub:
        async def recall(self, *args: Any, **kwargs: Any) -> RecallResult:
            return result

    return await ContextBuilderService(recall_service=_Stub()).build_context(
        "q", max_tokens=max_tokens,
    )


@pytest.mark.asyncio
async def test_build_context_admits_partial_sources_under_tight_budget() -> None:
    # Ten 2,000-char sources (F5's shape) cannot all fit beside the item; the old code
    # dropped the whole section. Now the best-ranked ones fill the remaining budget.
    sources = tuple(
        _src(f"ep{i}", f"2026-01-{i + 10}", 2000, rank=10 - i) for i in range(10)
    )
    result = RecallResult(
        query="q", preset="knowledge", results=[_item("e1", "an item")],
        candidates_evaluated=1, nodes_touched=0, source_memories=sources,
    )
    out = await _build(result, max_tokens=2000)

    assert out.truncated is True
    assert "Source memories" in out.context
    present = [i for i in range(10) if f"ep{i}:" in out.context]
    assert present, "at least one source must be admitted"
    # Admitted set is exactly the best ranks: a suffix of the chronological list here.
    assert present == list(range(10 - len(present), 10))
    assert len(present) < 10
    assert out.memory_ids == ["e1"]  # items unaffected
    assert out.token_estimate <= 2000


@pytest.mark.asyncio
async def test_build_context_unchanged_when_all_sources_fit() -> None:
    sources = (_src("a", "2026-01-01", 40, 2), _src("b", "2026-02-01", 40, 1))
    result = RecallResult(
        query="q", preset="knowledge", results=[_item("e1", "an item")],
        candidates_evaluated=1, nodes_touched=0, source_memories=sources,
    )
    out = await _build(result, max_tokens=4000)
    expected_block = "\n".join(
        [SOURCE_MEMORY_HEADER] + [f"  - {s.reference_time} | {s.content}" for s in sources]
    )
    assert expected_block in out.context
    assert out.truncated is False
