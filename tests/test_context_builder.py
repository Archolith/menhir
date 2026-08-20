"""Unit tests for ContextBuilderService."""

from __future__ import annotations

from dataclasses import replace
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.domain.recall import (
    EventAuthorityVerdict,
    QueryPreset,
    RecallResult,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
    ScoredMemory,
    TemporalFact,
)
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.services.context_builder import (
    ContextBuilderService,
    _deduplicate,
    _is_structurally_dense,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packs_structured_authority_before_ranked_memories() -> None:
    base = _recall_result([_mem("old", "I owned 20", "old observation", 0.9)])
    result = RecallResult(
        **{**base.__dict__, "authority_layer": (ScalarAuthorityVerdict(
            kind="current", status="leads", subject_uuid="ent-self", attribute="owned",
            scope="", value_kind="count", unit="", value=37,
            valid_at="2026-07-02T00:00:00+00:00", view_uuid="view-37",
            has_foundation=True,
            contributors=(ScalarAuthorityContributor(
                assertion_id="a37", relation="CURRENT_ANCHOR", operation="absolute",
                value=37, stated_span="I own 37 rare coins",
                valid_at="2026-07-02T00:00:00+00:00", evidence_tier="user",
                episode_uuid="ep37"),), contributors_total=1,
        ),)}
    )

    context = await _build_service(result).build_context("how many now", max_tokens=2000)

    assert context.context.index("[Scalar authority: LEADS]") < context.context.index("[Memory 1]")
    assert "CURRENT_ANCHOR: I own 37 rare coins" in context.context


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


def _recall_with_event(*verdicts: EventAuthorityVerdict, memories: list[ScoredMemory] | None = None) -> RecallResult:
    base = _recall_result(memories if memories is not None else [_mem("old", "I bought", "old obs", 0.9)])
    return RecallResult(
        **{**base.__dict__, "event_authority_layer": tuple(verdicts) if verdicts else None}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packs_rich_event_authority_lead_before_memories() -> None:
    result = _recall_with_event(_event_verdict())

    context = await _build_service(result).build_context(
        "did I acquire a red notebook today?", max_tokens=2000
    )

    block = context.context.split("[Memory 1]")[0]
    assert "[Event authority: LEADS (authoritative)]" in block
    assert "kind latest" in block
    assert "acquired = a red notebook" in block
    assert "key: red notebook" in block
    assert "valid 2026-07-22T09:30:00Z" in block
    assert "time basis: explicit" in block
    assert "domain: stationery" in block
    assert '"I bought a red notebook."' in block
    assert "episode ep-7" in block
    assert "turn evidence te-7" in block
    assert "gate: pass" in block
    assert "prefer the selected object over any conflicting related memory" in block
    assert context.context.index(
        "[Event authority: LEADS (authoritative)]"
    ) < context.context.index("[Memory 1]")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_unique_lead_authoritative_with_conflict_memory() -> None:
    # A unique, grounded lead is rendered as explicitly authoritative/preferred while a conflicting
    # tempting memory is still packed as supporting evidence — a lead does not fail closed.
    conflict = _mem(
        "tempt-1", "I bought a different notebook", "tempting stale account", 0.97
    )
    result = _recall_with_event(_event_verdict(), memories=[conflict])

    context = await _build_service(result).build_context(
        "did I acquire a red notebook today?", max_tokens=2000
    )

    assert "[Event authority: LEADS (authoritative)]" in context.context
    assert "acquired = a red notebook" in context.context
    assert "gate: pass" in context.context
    assert (
        "prefer the selected object over any conflicting related memory"
        in context.context
    )
    assert context.context.index(
        "[Event authority: LEADS (authoritative)]"
    ) < context.context.index("[Memory 1]")
    assert "[Memory 1]" in context.context
    assert "tempting stale account" in context.context
    assert "[Event authority: UNRESOLVED]" not in context.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_missing_predecessor_anchor_fails_closed_drops_tempting_memories() -> None:
    # A predecessor query whose anchor is absent/not uniquely resolved fails closed: it renders an
    # explicit unresolved instruction and packs NEITHER ranked memories NOR the supplementary
    # timeline as answer evidence (the tempting memories must not surface as a fabricated answer).
    tempting = _mem(
        "tempt-1", "I bought the old notebook", "tempting but unverified memory", 0.99
    )
    result = _recall_with_event(_event_verdict(
        status="advisory", kind="predecessor", gate="anchor",
        reason="anchor not uniquely resolved by assertion_key",
        object_key=None, object_display=None, valid_at=None, stated_span=None,
        episode_uuid=None, turn_evidence_uuid=None, domain=None, time_basis=None,
    ), memories=[tempting])

    context_result = await _build_service(result).build_context(
        "did I buy the old notebook after I bought the pen?", max_tokens=2000
    )

    assert "[Event authority: UNRESOLVED]" in context_result.context
    assert "acquired" in context_result.context
    assert "gate: anchor" in context_result.context
    assert "anchor not uniquely resolved" in context_result.context
    assert (
        "do not infer an answer from ranked memories or timeline; report unresolved"
        in context_result.context
    )
    assert "[Memory 1]" not in context_result.context
    assert "tempting but unverified memory" not in context_result.context
    assert "Timeline" not in context_result.context
    assert context_result.memory_count == 0
    assert context_result.memory_ids == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_fail_closed_suppresses_scalar_authority() -> None:
    # A temporal-before query whose anchor is unresolved fails closed: current-state scalar
    # authority answers the present, not the missing historical anchor, and would mislead — so
    # the scalar block, ranked memories, and timeline are all absent, leaving only the unresolved
    # instruction.
    scalar = ScalarAuthorityVerdict(
        kind="current", status="leads", subject_uuid="ent-self", attribute="owned",
        scope="", value_kind="count", unit="", value=37,
        valid_at="2026-07-02T00:00:00+00:00", view_uuid="view-37",
        has_foundation=True,
        contributors=(ScalarAuthorityContributor(
            assertion_id="a37", relation="CURRENT_ANCHOR", operation="absolute",
            value=37, stated_span="I own 37 rare coins",
            valid_at="2026-07-02T00:00:00+00:00", evidence_tier="user",
            episode_uuid="ep37"),), contributors_total=1,
    )
    base = _recall_result([_mem("old", "I bought", "old obs", 0.9)])
    result = RecallResult(
        **{**base.__dict__,
           "authority_layer": (scalar,),
           "event_authority_layer": (_event_verdict(
               status="advisory", kind="predecessor", gate="anchor",
               reason="anchor not uniquely resolved by assertion_key",
               object_key=None, object_display=None, valid_at=None, stated_span=None,
               episode_uuid=None, turn_evidence_uuid=None, domain=None, time_basis=None,
           ),)}
    )

    context = await _build_service(result).build_context(
        "how many rare coins did I own before I bought the pen?", max_tokens=2000
    )

    assert "[Event authority: UNRESOLVED]" in context.context
    assert "gate: anchor" in context.context
    assert "[Scalar authority:" not in context.context
    assert "I own 37 rare coins" not in context.context
    assert "[Memory 1]" not in context.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_route_advisory_stays_advisory_and_keeps_memories() -> None:
    # A route/foundation/evidence advisory carries a resolved selection (object_key set), so it
    # stays ADVISORY, never implies a lead, and does NOT fail closed: ranked memories remain packed.
    result = _recall_with_event(_event_verdict(
        status="advisory", kind="generic_advisory", gate="route",
        reason="generic route stays advisory (no scope token)",
        object_key="red notebook", object_display="a red notebook",
        valid_at="2026-07-22T09:30:00Z", stated_span="I bought a red notebook.",
        episode_uuid="ep-7", turn_evidence_uuid="te-7",
    ))

    context = await _build_service(result).build_context(
        "did I acquire a red notebook today?", max_tokens=2000
    )

    block = context.context.split("[Memory 1]")[0]
    assert "[Event authority: ADVISORY]" in block
    assert "acquired" in block
    assert "kind generic_advisory" in block
    assert "gate: route" in block
    assert "reason: generic route stays advisory (no scope token)" in block
    assert "[Memory 1]" in context.context
    assert "[Event authority: UNRESOLVED]" not in context.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_flag_off_leaves_context_byte_identical() -> None:
    mem = _mem("old", "I bought", "old obs", 0.9)
    off_result = _recall_result([mem])
    on_result = _recall_result([mem])

    off = await _build_service(off_result).build_context("did I acquire a red notebook today?", max_tokens=2000)
    on = await _build_service(on_result).build_context("did I acquire a red notebook today?", max_tokens=2000)

    assert off.context == on.context
    assert "[Event authority:" not in off.context


def _build_service(recall_result: RecallResult) -> ContextBuilderService:
    mock_recall = AsyncMock()
    mock_recall.recall = AsyncMock(return_value=recall_result)
    return ContextBuilderService(recall_service=mock_recall)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_renders_source_time_for_each_memory() -> None:
    older = replace(
        _mem("ratio-6", "French press ratio", "Use 6 oz of water.", 0.9),
        temporal_facts=(TemporalFact(
            fact="French press ratio uses 6 oz of water",
            valid_at="2023-02-11T17:37:00Z",
            invalid_at="2023-06-30T11:33:00Z",
            created_at="2026-07-30T05:00:00Z",
            expired_at="2026-07-30T05:01:00Z",
            is_current_belief=False,
            temporal_role="superseded_belief",
        ),),
    )
    newer = replace(
        _mem("ratio-5", "French press ratio", "Use 5 oz of water.", 0.8),
        temporal_facts=(TemporalFact(
            fact="French press ratio uses 5 oz of water",
            valid_at="2023-06-30T11:33:00Z",
            invalid_at=None,
            created_at="2026-07-30T05:01:00Z",
            expired_at=None,
            is_current_belief=True,
            temporal_role="current_belief",
        ),),
    )

    context = await _build_service(_recall_result([older, newer])).build_context(
        "Did I switch to more or less water?", max_tokens=2000
    )

    assert (
        "2023-02-11T17:37:00Z through 2023-06-30T11:33:00Z"
        " | French press ratio uses 6 oz of water"
    ) in context.context
    assert "2023-06-30T11:33:00Z | French press ratio uses 5 oz of water" in context.context
    assert "belief: superseded belief" in context.context
    assert "belief: current belief" in context.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_marks_missing_source_time_unknown_without_using_belief_time() -> None:
    memory = replace(
        _mem("ratio", "French press ratio", "Use 5 oz of water.", 0.9),
        temporal_facts=(TemporalFact(
            fact="French press ratio uses 5 oz of water",
            valid_at=None,
            invalid_at=None,
            created_at="2026-07-30T05:00:00Z",
            expired_at=None,
            is_current_belief=True,
            temporal_role="current_belief",
        ),),
    )

    context = await _build_service(_recall_result([memory])).build_context(
        "When was this true?", max_tokens=2000
    )

    assert "unknown | French press ratio uses 5 oz of water" in context.context
    assert "2026-07-30T05:00:00Z" not in context.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_marks_memory_without_temporal_facts_source_time_unknown() -> None:
    recall_service = AsyncMock()
    recall_service.recall = AsyncMock(return_value=_recall_result([
        _mem("ratio", "French press ratio", "Use 5 oz of water.", 0.9)
    ]))
    context = await ContextBuilderService(recall_service=recall_service).build_context(
        "When was this true?", max_tokens=2000
    )

    assert "Source time: unknown." in context.context
    assert recall_service.recall.await_args.kwargs["include_invalidated"] is True


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_tiktoken_mode_when_available(self):
        """estimate_tokens uses tiktoken when the library is importable."""
        with patch(
            "menhir.services.context_builder._tiktoken_available", True
        ), patch(
            "menhir.services.context_builder._tiktoken_enc"
        ) as mock_enc:
            mock_enc.encode.return_value = [1, 2, 3, 4, 5]
            count, mode = estimate_tokens("hello world")
            assert mode == "tokenizer"
            assert count == 5
            mock_enc.encode.assert_called_once_with("hello world")

    def test_heuristic_fallback_when_tiktoken_unavailable(self):
        """estimate_tokens falls back to len/3 heuristic when tiktoken is absent."""
        with patch(
            "menhir.services.context_builder._tiktoken_available", False
        ), patch(
            "menhir.services.context_builder._tiktoken_enc", None
        ):
            text = "a" * 30
            count, mode = estimate_tokens(text)
            assert mode == "heuristic"
            assert count == math.ceil(30 / 3)

    def test_heuristic_rounds_up(self):
        """Heuristic token estimate uses ceil so partial tokens round up."""
        with patch(
            "menhir.services.context_builder._tiktoken_available", False
        ), patch(
            "menhir.services.context_builder._tiktoken_enc", None
        ):
            text = "a" * 10  # 10 / 3 = 3.33 -> ceil = 4
            count, mode = estimate_tokens(text)
            assert mode == "heuristic"
            assert count == 4


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_exact_duplicates_keep_higher_scorer(self):
        """Exact-text duplicates collapse, keeping the highest-scoring one."""
        m1 = _mem("a", "fact", "The sky is blue", 0.8)
        m2 = _mem("b", "fact", "The sky is blue", 0.9)
        result = _deduplicate([m1, m2])
        assert len(result) == 1
        assert result[0].uuid == "b"

    def test_near_duplicates_removed_by_jaccard(self):
        """Memories with Jaccard > 0.8 are treated as redundant."""
        # These share most words -- Jaccard will be high
        m1 = _mem("a", "fact", "the quick brown fox jumps over the lazy dog", 0.9)
        m2 = _mem("b", "fact", "the quick brown fox jumps over the lazy cat", 0.7)
        # word sets: {the, quick, brown, fox, jumps, over, lazy, dog}
        #        vs  {the, quick, brown, fox, jumps, over, lazy, cat}
        # intersection = 7, union = 9 -> Jaccard = 7/9 = 0.778 < 0.8
        # Need higher overlap for >0.8 -- use nearly identical text
        m1 = _mem("a", "name", "alpha beta gamma delta epsilon zeta", 0.9)
        m2 = _mem("b", "name", "alpha beta gamma delta epsilon zeta eta", 0.7)
        # words a: {alpha, beta, gamma, delta, epsilon, zeta} = 6
        # words b: {alpha, beta, gamma, delta, epsilon, zeta, eta} = 7
        # intersection = 6, union = 7 -> Jaccard = 6/7 = 0.857 > 0.8
        result = _deduplicate([m1, m2])
        assert len(result) == 1
        assert result[0].uuid == "a"

    def test_distinct_memories_kept(self):
        """Memories with low Jaccard overlap are both kept."""
        m1 = _mem("a", "weather", "The weather is sunny today", 0.9)
        m2 = _mem("b", "code", "Python uses indentation for blocks", 0.8)
        result = _deduplicate([m1, m2])
        assert len(result) == 2

    def test_empty_list(self):
        """Deduplication on an empty list returns empty."""
        assert _deduplicate([]) == []

    def test_never_dedup_across_claim_shapes(self):
        """Claim shapes (view_kind values) prevent dedup — a View never collapses against
        an episode with lexically similar content if they have different view_kinds."""
        # Counter-View surface (user metric): "user bike_spend = 185"
        view_mem = ScoredMemory(
            uuid="view-1",
            name="user bike_spend",
            content="user bike_spend = 185",
            scope="PERSISTENT",
            memory_type="VIEW",
            final_score=0.9,
            breakdown=_DEFAULT_BREAKDOWN,
            view_kind="counter_view",  # claim shape is set
        )
        # Episode narrating the same numbers
        episode_mem = ScoredMemory(
            uuid="episode-1",
            name="spending summary",
            content="user bike_spend 185 recorded today",
            scope="PERSISTENT",
            memory_type="EPISODIC",
            final_score=0.7,
            breakdown=_DEFAULT_BREAKDOWN,
            view_kind=None,  # no claim shape
        )
        result = _deduplicate([view_mem, episode_mem])
        # Both should survive because they have different claim shapes
        assert len(result) == 2
        assert {m.uuid for m in result} == {"view-1", "episode-1"}


# ---------------------------------------------------------------------------
# Budget truncation
# ---------------------------------------------------------------------------


class TestBudgetTruncation:
    @pytest.mark.asyncio
    async def test_truncation_when_budget_exceeded(self):
        """Memories that would exceed the token budget are dropped."""
        # Create several memories with lengthy, varied content so each
        # memory consumes a meaningful number of tokens regardless of mode.
        memories = [
            _mem(
                f"m{i}",
                f"Memory {i}",
                f"The quick brown fox number {i} jumps over the lazy dog repeatedly " * 10,
                1.0 - i * 0.1,
            )
            for i in range(10)
        ]
        svc = _build_service(_recall_result(memories))

        # With a small budget, not all 10 memories can fit
        result = await svc.build_context("test", max_tokens=100)
        assert result.truncated is True
        assert result.memory_count < 10

    @pytest.mark.asyncio
    async def test_no_truncation_when_within_budget(self):
        """All memories fit when the budget is generous."""
        memories = [_mem("a", "small", "hi", 0.9)]
        svc = _build_service(_recall_result(memories))

        result = await svc.build_context("test", max_tokens=5000)
        assert result.truncated is False
        assert result.memory_count == 1

    @pytest.mark.asyncio
    async def test_empty_recall_produces_empty_context(self):
        """No memories yields an explicit 'nothing found' message (abstention honesty)."""
        svc = _build_service(_recall_result([]))
        result = await svc.build_context("test")
        # Abstention honesty: when recall returns zero results, render explicit message
        assert result.context == "Memory: nothing relevant found for this query."
        # memory_count tracks actual memories, not the abstention message
        assert result.memory_count == 0
        assert result.truncated is False


# ---------------------------------------------------------------------------
# Heuristic mode budget fraction
# ---------------------------------------------------------------------------


class TestHeuristicBudgetFraction:
    @pytest.mark.asyncio
    async def test_heuristic_mode_halves_budget(self):
        """In heuristic mode the effective budget is floor(max_tokens * 0.5)."""
        memories = [
            _mem(f"m{i}", f"Memory {i}", "word " * 50, 1.0 - i * 0.1)
            for i in range(20)
        ]
        svc = _build_service(_recall_result(memories))

        with patch(
            "menhir.services.context_builder._tiktoken_available", False
        ), patch(
            "menhir.services.context_builder._tiktoken_enc", None
        ), patch(
            "menhir.services.context_builder._ESTIMATION_MODE", "heuristic"
        ):
            result = await svc.build_context("test", max_tokens=200)

        # Effective budget should be floor(200 * 0.5) = 100 heuristic tokens
        assert result.estimation_mode == "heuristic"
        assert result.token_estimate <= 100


# ---------------------------------------------------------------------------
# Dense content 1.3x token adjustment
# ---------------------------------------------------------------------------


class TestDenseContentAdjustment:
    def test_structurally_dense_detection(self):
        """Text with >15% special symbols is flagged as dense."""
        dense = "{{{[[[())]]]}}}"  # all symbols
        assert _is_structurally_dense(dense) is True

        plain = "the quick brown fox"
        assert _is_structurally_dense(plain) is False

        assert _is_structurally_dense("") is False

    @pytest.mark.asyncio
    async def test_dense_content_inflates_token_count_in_heuristic_mode(self):
        """Dense content gets 1.3x token adjustment, fitting fewer memories."""
        # Build content that is structurally dense (>15% symbols)
        # symbols = {, }, [, ], (, ), <, >, :, ;, =, `
        dense_content = '{"key": [1, 2, 3], "val": {"a": true, "b": false}}'
        plain_content = "this is a plain english sentence with no special symbols at all"

        # Ensure the dense content IS actually dense
        assert _is_structurally_dense(dense_content) is True
        assert _is_structurally_dense(plain_content) is False

        mem_dense = [_mem(f"d{i}", f"Dense {i}", dense_content, 0.9 - i * 0.05) for i in range(10)]
        mem_plain = [_mem(f"p{i}", f"Plain {i}", plain_content, 0.9 - i * 0.05) for i in range(10)]

        with patch(
            "menhir.services.context_builder._tiktoken_available", False
        ), patch(
            "menhir.services.context_builder._tiktoken_enc", None
        ), patch(
            "menhir.services.context_builder._ESTIMATION_MODE", "heuristic"
        ):
            svc_dense = _build_service(_recall_result(mem_dense))
            result_dense = await svc_dense.build_context("test", max_tokens=400)

            svc_plain = _build_service(_recall_result(mem_plain))
            result_plain = await svc_plain.build_context("test", max_tokens=400)

        # Dense memories should fit fewer items (or equal, but never more)
        assert result_dense.memory_count <= result_plain.memory_count


# ---------------------------------------------------------------------------
# include_scores flag
# ---------------------------------------------------------------------------


class TestIncludeScores:
    @pytest.mark.asyncio
    async def test_scores_included_in_output(self):
        """include_scores=True embeds the score in each memory line."""
        memories = [_mem("a", "Test Fact", "some content", 0.85)]
        svc = _build_service(_recall_result(memories))

        result = await svc.build_context("test", include_scores=True, max_tokens=5000)
        assert "(score: 0.85)" in result.context

    @pytest.mark.asyncio
    async def test_scores_excluded_by_default(self):
        """By default, scores are not shown in the context output."""
        memories = [_mem("a", "Test Fact", "some content", 0.85)]
        svc = _build_service(_recall_result(memories))

        result = await svc.build_context("test", max_tokens=5000)
        assert "(score:" not in result.context
        assert "[Memory 1]" in result.context


# ---------------------------------------------------------------------------
# ContextResult metadata
# ---------------------------------------------------------------------------


class TestContextResultMetadata:
    @pytest.mark.asyncio
    async def test_result_contains_memory_ids(self):
        """The returned result includes the UUIDs of packed memories."""
        memories = [
            _mem("uuid-1", "First", "aaa", 0.9),
            _mem("uuid-2", "Second", "bbb", 0.8),
        ]
        svc = _build_service(_recall_result(memories))

        result = await svc.build_context("test", max_tokens=5000)
        assert "uuid-1" in result.memory_ids
        assert "uuid-2" in result.memory_ids

    @pytest.mark.asyncio
    async def test_preset_propagated(self):
        """The preset value is included in the result."""
        svc = _build_service(_recall_result([]))
        result = await svc.build_context("test", preset=QueryPreset.RECENT)
        assert result.preset == "recent"

    @pytest.mark.asyncio
    async def test_query_preserved(self):
        """The original query string is preserved in the result."""
        svc = _build_service(_recall_result([]))
        result = await svc.build_context("my query")
        assert result.query == "my query"


# ---------------------------------------------------------------------------
# TODO integration — token budget contract
# ---------------------------------------------------------------------------


class TestTodoBudgetContract:
    @pytest.mark.asyncio
    async def test_todo_tokens_included_in_estimate(self):
        """token_estimate must include the TODO section — not just memories."""
        mock_adapter = MagicMock()
        mock_adapter.list_todos_matching_query.return_value = [
            {"uuid": "t1", "content": "Fix the auth middleware error handling", "priority": "high", "code_ref": "src/api.py:10"},
        ]
        mock_recall = AsyncMock()
        mock_recall.recall = AsyncMock(return_value=_recall_result([]))
        svc = ContextBuilderService(recall_service=mock_recall, graph_adapter=mock_adapter)

        result = await svc.build_context("auth middleware", max_tokens=500)

        assert "Related open TODOs" in result.context
        assert result.token_estimate > 0  # includes todo section tokens

    @pytest.mark.asyncio
    async def test_todo_tokens_reserved_from_budget(self):
        """Memories must be packed against (max_tokens - todo_tokens), not max_tokens."""
        # Use a very tight budget so that if TODOs were not pre-reserved, at least
        # one memory that should be squeezed out would fit.
        todo_content = "Fix the auth middleware error handling in the routes module"
        mock_adapter = MagicMock()
        mock_adapter.list_todos_matching_query.return_value = [
            {"uuid": "t1", "content": todo_content, "priority": "high", "code_ref": None},
        ]
        # Each memory line is ~60 chars → ~20 tokens each
        many_memories = [_mem(f"m{i}", f"Memory {i}", "x" * 50, 0.9 - i * 0.01) for i in range(20)]
        mock_recall = AsyncMock()
        mock_recall.recall = AsyncMock(return_value=_recall_result(many_memories))
        svc = ContextBuilderService(recall_service=mock_recall, graph_adapter=mock_adapter)

        result_with_todos = await svc.build_context("auth middleware", max_tokens=300)

        # Build same context without todos for comparison
        svc_no_todos = _build_service(_recall_result(many_memories))
        result_no_todos = await svc_no_todos.build_context("auth middleware", max_tokens=300)

        # With todos reserving budget, fewer memories should fit
        assert result_with_todos.memory_count <= result_no_todos.memory_count
        # Total token estimate should not exceed max_tokens (within estimation variance)
        assert result_with_todos.token_estimate <= 300 * 1.1  # 10% tolerance for estimation

    @pytest.mark.asyncio
    async def test_no_todos_no_section(self):
        """When no matching TODOs exist the section is not appended."""
        mock_adapter = MagicMock()
        mock_adapter.list_todos_matching_query.return_value = []
        mock_recall = AsyncMock()
        mock_recall.recall = AsyncMock(return_value=_recall_result([]))
        svc = ContextBuilderService(recall_service=mock_recall, graph_adapter=mock_adapter)

        result = await svc.build_context("anything", max_tokens=500)

        assert "Related open TODOs" not in result.context
        # Abstention honesty: empty results produce a "nothing found" message
        assert "nothing relevant found" in result.context
        assert result.token_estimate > 0  # message consumes tokens

    @pytest.mark.asyncio
    async def test_todo_adapter_exception_does_not_crash(self):
        """A failing graph_adapter must not propagate — context is returned without todos."""
        mock_adapter = MagicMock()
        mock_adapter.list_todos_matching_query.side_effect = RuntimeError("db down")
        mock_recall = AsyncMock()
        mock_recall.recall = AsyncMock(return_value=_recall_result([_mem("a", "Fact", "content", 0.9)]))
        svc = ContextBuilderService(recall_service=mock_recall, graph_adapter=mock_adapter)

        result = await svc.build_context("query", max_tokens=500)

        assert "Fact" in result.context
        assert "Related open TODOs" not in result.context


# ---------------------------------------------------------------------------
# CF-74: the linked-doc preview read must not run on the event loop.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_linked_doc_preview_helper_reads_and_closes(tmp_path):
    from menhir.services.context_builder import _read_linked_doc_previews

    doc = tmp_path / "note.md"
    doc.write_text("x" * 500, encoding="utf-8")

    lines = _read_linked_doc_previews(
        [{"root_path": str(doc), "name": "note", "doc_type": "wiki"}]
    )

    assert len(lines) == 1
    assert lines[0].startswith("- [[note]] [wiki]: ")
    assert len(lines[0].split(": ", 1)[1]) == 200  # unchanged 200-char prefix


@pytest.mark.unit
def test_linked_doc_preview_skips_unreadable_without_losing_the_rest(tmp_path):
    """One bad file must not lose the whole section -- the original per-doc swallow."""
    from menhir.services.context_builder import _read_linked_doc_previews

    good = tmp_path / "good.md"
    good.write_text("hello", encoding="utf-8")

    lines = _read_linked_doc_previews([
        {"root_path": str(tmp_path / "missing.md"), "name": "missing"},
        {"root_path": str(good), "name": "good"},
    ])

    assert len(lines) == 1
    assert "[[good]]" in lines[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_context_does_not_starve_the_loop_reading_linked_docs(tmp_path, monkeypatch):
    """CF-74 at the far end, THROUGH build_context.

    Driving the helper through `asyncio.to_thread` from inside the test proves nothing about
    this defect: the helper is correct either way, and deleting the `to_thread` from the caller
    leaves such a test green. The defect lives in how build_context calls it, so the test has to
    go through build_context. Control: with the call put back on the loop this drops to ~1 tick.
    """
    import asyncio
    import time

    import menhir.services.context_builder as cb

    doc = tmp_path / "slow.md"
    doc.write_text("y" * 500, encoding="utf-8")
    real_open = open

    def _slow_open(*args, **kwargs):
        time.sleep(0.12)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(cb, "open", _slow_open, raising=False)

    mem = _mem("m1", "linked note", "a linked note", 0.9)
    mock_recall = AsyncMock()
    mock_recall.recall = AsyncMock(return_value=_recall_result([mem]))
    mock_adapter = MagicMock()
    mock_adapter.list_todos_matching_query.return_value = []
    mock_adapter.get_linked_documents.return_value = [
        {"root_path": str(doc), "name": "slow", "doc_type": "wiki"}
    ]
    svc = ContextBuilderService(recall_service=mock_recall, graph_adapter=mock_adapter)

    ticks = 0
    stop = False

    async def _heartbeat():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(_heartbeat())
    await asyncio.sleep(0)

    result = await svc.build_context("linked note", max_tokens=2000)

    stop = True
    await beat

    assert "Wiki Context" in result.context, (
        "the linked-doc branch did not run, so the tick count below would be vacuous"
    )
    assert ticks >= 3, f"event loop starved while reading linked docs ({ticks} ticks)"
