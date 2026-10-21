"""Tests for #172: skip-and-continue packing of oversized ranked memories."""

from __future__ import annotations

from dataclasses import replace
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from menhir.domain.recall import EventAuthorityVerdict, RecallResult, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.services.context_builder import ContextBuilderService
import menhir.services.context_builder as cb


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


def _recall_result(memories: list[ScoredMemory]) -> RecallResult:
    return RecallResult(
        query="test query",
        preset="knowledge",
        results=memories,
        candidates_evaluated=len(memories),
        nodes_touched=len(memories),
    )


def _build_service(recall_result: RecallResult) -> ContextBuilderService:
    mock_recall = AsyncMock()
    mock_recall.recall = AsyncMock(return_value=recall_result)
    return ContextBuilderService(recall_service=mock_recall)


def _event_verdict(**overrides) -> EventAuthorityVerdict:
    base = dict(
        predicate="acquired", object_key="red notebook", object_display="a red notebook",
        valid_at="2026-07-22T09:30:00Z", stated_span="I bought a red notebook.",
        assertion_key="asrt-7", episode_uuid="ep-7", turn_evidence_uuid="te-7",
        domain="stationery", time_basis="explicit", status="leads", gate="pass",
        reason="unique grounded lead", subject_uuid="ent-self",
        has_foundation=True, kind="latest",
    )
    base.update(overrides)
    return EventAuthorityVerdict(**base)


_STALE = {
    "stale_anchor": True,
    "stale_reason": "file_changed_after_anchor",
    "dirty_at": "2026-07-08T12:00:00Z",
    "anchored_at": "2026-07-01T00:00:00Z",
    "path": "src/foo.py",
}

# Heuristic mode: budget = floor(max_tokens * 0.5). Deterministic arithmetic
# (ceil(len/3)) so the packing arithmetic is exact under patching.
@contextlib.contextmanager
def _heuristic_mode():
    with patch("menhir.services.context_builder._ESTIMATION_MODE", "heuristic"), \
            patch("menhir.services.context_builder._tiktoken_available", False):
        yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_memory_skipped_but_later_memories_packed() -> None:
    memories = [
        _mem("m1", "small one", "first small observation", 0.9),
        _mem("m2", "huge one", "x" * 1200, 0.8),
        _mem("m3", "small three", "third small observation", 0.7),
        _mem("m4", "small four", "fourth small observation", 0.6),
    ]
    with _heuristic_mode():
        context = await _build_service(_recall_result(memories)).build_context(
            "test query", max_tokens=200
        )

    assert "[Memory 1]" in context.context
    assert "[Memory 3]" in context.context
    assert "[Memory 4]" in context.context
    assert "[Memory 2]" not in context.context
    assert "x" * 50 not in context.context
    assert context.truncated is True
    assert context.memory_count == 3
    assert context.token_estimate <= 200
    assert context.memory_ids == ["m1", "m3", "m4"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_order_preserved_among_packed_memories() -> None:
    memories = [
        _mem("m1", "small one", "first small observation", 0.9),
        _mem("m2", "huge one", "x" * 1200, 0.8),
        _mem("m3", "small three", "third small observation", 0.7),
        _mem("m4", "small four", "fourth small observation", 0.6),
    ]
    with _heuristic_mode():
        context = await _build_service(_recall_result(memories)).build_context(
            "test query", max_tokens=200
        )

    assert context.context.index("[Memory 3]") < context.context.index("[Memory 4]")
    assert context.context.index("[Memory 1]") < context.context.index("[Memory 3]")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_oversized_memory_skipped_with_advisory_and_later_stale_packed_with_advisory() -> None:
    stale_oversized = replace(
        _mem("m2", "huge stale", "x" * 1200, 0.8), stale_anchor_info=dict(_STALE)
    )
    stale_small = replace(
        _mem("m4", "small stale", "fourth small observation", 0.6),
        stale_anchor_info=dict(_STALE),
    )
    memories = [
        _mem("m1", "small one", "first small observation", 0.9),
        stale_oversized,
        _mem("m3", "small three", "third small observation", 0.7),
        stale_small,
    ]
    with _heuristic_mode():
        context = await _build_service(_recall_result(memories)).build_context(
            "test query", max_tokens=400
        )

    # Oversized stale memory: no line AND no orphan advisory.
    assert "[Memory 2]" not in context.context
    body = context.context.split("[Memory 1]", 1)[1].split("[Memory 3]", 1)[0]
    assert "Stale file anchor" not in body
    # Later stale memory that fits is packed WITH its advisory (atomic).
    tail = context.context.split("[Memory 4]", 1)[1]
    assert "Stale file anchor" in tail
    assert context.memory_ids == ["m1", "m3", "m4"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_memories_fit_truncated_false_all_labels_present() -> None:
    memories = [
        _mem("m1", "small one", "first small observation", 0.9),
        _mem("m2", "small two", "second small observation", 0.8),
        _mem("m3", "small three", "third small observation", 0.7),
    ]
    with _heuristic_mode():
        context = await _build_service(_recall_result(memories)).build_context(
            "test query", max_tokens=200
        )

    assert context.truncated is False
    for label in ("[Memory 1]", "[Memory 2]", "[Memory 3]"):
        assert label in context.context
    assert context.memory_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_closed_event_verdict_still_packs_no_ranked_memories() -> None:
    base = _recall_result([_mem("m1", "tempting", "tempting but unverified", 0.99)])
    result = RecallResult(
        **{
            **base.__dict__,
            "event_authority_layer": (_event_verdict(
                status="advisory", kind="predecessor", gate="anchor",
                reason="anchor not uniquely resolved by assertion_key",
                object_key=None, object_display=None, valid_at=None,
                stated_span=None, episode_uuid=None, turn_evidence_uuid=None,
                domain=None, time_basis=None,
            ),),
        }
    )

    context = await _build_service(result).build_context(
        "test query", max_tokens=200
    )

    assert "[Event authority: UNRESOLVED]" in context.context
    assert "[Memory 1]" not in context.context
    assert context.memory_count == 0


@pytest.mark.unit
def test_tiktoken_declared_and_tokenizer_mode_pinned() -> None:
    import tiktoken  # noqa: F401

    assert cb._ESTIMATION_MODE == "tokenizer"
    assert cb._tiktoken_available is True
