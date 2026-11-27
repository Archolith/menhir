"""#873: the hook never shows a stale memory without its advisory; TODOs and dedup behave."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.cli.output import format_hook_output, hook_context_max_tokens
from menhir.domain.recall import RecallResult, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.services.context_builder import (
    STALE_ADVISORY_PREFIX,
    ContextBuilderService,
    _deduplicate,
    estimate_tokens,
)

_BREAKDOWN = RelevanceBreakdown(
    semantic_similarity=0.9, adjacency_bonus=0.0, recency_bonus=0.0, prominence_bonus=0.0,
    conflict_bonus=0.0, type_boost=0.0, preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1,
    delta=0.0,
)


def _mem(uuid: str, content: str, score: float = 0.9, *, stale: bool = False) -> ScoredMemory:
    mem = ScoredMemory(
        uuid=uuid, name=uuid, content=content, scope="PERSISTENT", memory_type="SEMANTIC",
        final_score=score, breakdown=_BREAKDOWN,
    )
    if stale:
        mem = replace(mem, stale_anchor_info={"stale_anchor": True, "path": "src/a.py"})
    return mem


def _recall(memories: list[ScoredMemory]) -> RecallResult:
    return RecallResult(
        query="q", preset="knowledge", results=memories,
        candidates_evaluated=len(memories), nodes_touched=len(memories),
    )


def _service(memories: list[ScoredMemory], todos: list[dict] | None = None) -> ContextBuilderService:
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=_recall(memories))
    adapter = MagicMock()
    adapter.list_todos_matching_query.return_value = todos or []
    return ContextBuilderService(recall_service=recall, graph_adapter=adapter)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [80, 120, 160, 200, 260, 320, 400, 700])
async def test_hook_output_never_has_stale_memory_without_advisory(max_tokens: int) -> None:
    memories = [
        _mem(f"stale-{i}", f"stale claim number {i} about the parser " * 3, 0.9 - i * 0.01, stale=True)
        for i in range(8)
    ]
    todos = [{"uuid": "t1", "content": "open work item", "priority": "high"}]
    budget = hook_context_max_tokens([], todos, [], max_tokens)
    result = await _service(memories).build_context("q", max_tokens=budget)
    output = format_hook_output([], result.context, "q", todos=todos, max_tokens=max_tokens)

    stale_memories = sum(1 for i in range(8) if f"stale claim number {i} " in output)
    advisories = output.count(STALE_ADVISORY_PREFIX)
    assert stale_memories == advisories
    assert stale_memories >= 1 or max_tokens < 160


@pytest.mark.unit
def test_formatter_trim_near_boundary_keeps_advisory_with_memory() -> None:
    advisory = f"{STALE_ADVISORY_PREFIX} src/a.py changed after this memory was anchored."
    entries = []
    for i in range(6):
        entries.append(f"[Memory {i}] m{i}: " + "claim words " * 12)
        entries.append(advisory)
    context = "\n\n".join(entries)
    full_tokens, _ = estimate_tokens(context)
    for max_tokens in range(60, full_tokens * 3, 7):
        output = format_hook_output([], context, "q", max_tokens=max_tokens)
        memory_count = output.count("[Memory ")
        assert output.count(STALE_ADVISORY_PREFIX) == memory_count, (max_tokens, output)


@pytest.mark.unit
def test_formatter_never_char_cuts_a_stale_memory_that_cannot_fit() -> None:
    advisory = f"{STALE_ADVISORY_PREFIX} src/a.py changed."
    context = "[Memory 1] m: " + "word " * 400 + "\n\n" + advisory
    output = format_hook_output([], context, "q", max_tokens=100)
    assert "[Memory 1]" not in output
    assert STALE_ADVISORY_PREFIX not in output


@pytest.mark.unit
@pytest.mark.asyncio
async def test_todo_shown_by_hook_is_not_repeated_by_build_context() -> None:
    shared = {"uuid": "t-shared", "content": "fix the parser", "priority": "high"}
    other = {"uuid": "t-other", "content": "parser docs", "priority": "normal"}
    svc = _service([_mem("m1", "parser note")], todos=[shared, other])
    result = await svc.build_context("parser", max_tokens=1000, exclude_todo_uuids={"t-shared"})
    assert result.context.count("fix the parser") == 0
    assert result.context.count("parser docs") == 1

    todos = [shared]
    output = format_hook_output([], result.context, "parser", todos=todos, max_tokens=1500)
    assert output.count("fix the parser") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exclusion_still_returns_three_other_todos() -> None:
    todos = [{"uuid": f"t{i}", "content": f"todo {i}", "priority": "normal"} for i in range(5)]
    svc = _service([_mem("m1", "note")], todos=todos)
    result = await svc.build_context("todo", max_tokens=1000, exclude_todo_uuids={"t0"})
    assert "todo 0" not in result.context
    assert svc.graph_adapter.list_todos_matching_query.call_args.kwargs["limit"] == 4


@pytest.mark.unit
def test_dedup_jaccard_stage_keeps_highest_scorer() -> None:
    low = _mem("low", "the quick brown fox jumps over the lazy dog today", 0.40)
    high = _mem("high", "the quick brown fox jumps over the lazy dog today now", 0.95)
    kept = _deduplicate([low, high])
    assert [m.uuid for m in kept] == ["high"]
    assert [m.uuid for m in _deduplicate([high, low])] == ["high"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", list(range(60, 900, 23)))
async def test_multi_paragraph_stale_memory_never_loses_its_advisory(max_tokens: int) -> None:
    """A memory's own blank lines must not become cut points that separate it from its advisory."""
    topics = [
        ("lease", "renewal", "fencing", "token", "expiry"),
        ("cypher", "index", "planner", "cardinality", "scan"),
        ("embedding", "vector", "dimension", "cosine", "model"),
        ("hook", "prompt", "budget", "injection", "trim"),
        ("queue", "worker", "backoff", "retry", "dead-letter"),
    ]
    memories = [
        _mem(
            f"para-{i}",
            f"head of claim {i} about the parser.\n\n" + (" ".join(topics[i]) + " ") * 3
            + f"\n\n \n\ntail of claim {i}.",
            0.9 - i * 0.01,
            stale=True,
        )
        for i in range(5)
    ]
    # Pack generously so the formatter is the one that has to cut (a scrubbed list section or
    # a different estimate can leave the hook with less room than build_context had).
    result = await _service(memories).build_context("q", max_tokens=5000)
    assert result.context.count(STALE_ADVISORY_PREFIX) == 5
    output = format_hook_output([], result.context, "q", max_tokens=max_tokens)

    for i in range(5):
        if f"head of claim {i} " in output:
            assert f"tail of claim {i}." in output
    heads = sum(1 for i in range(5) if f"head of claim {i} " in output)
    assert heads == output.count(STALE_ADVISORY_PREFIX)
