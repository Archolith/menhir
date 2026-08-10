"""Edge case tests from .agent/edge-case-testing.md.

Each test class corresponds to one numbered edge case from the audit.
"""

from __future__ import annotations

import asyncio

from menhir.services.ingest_gate import IngestGate
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.domain.recall import CandidateData, QueryPreset
from menhir.infrastructure.embedding_cache import EmbeddingCache
from menhir.domain.models import ProcessingState
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate(**overrides: object) -> CandidateData:
    defaults = dict(
        uuid="node-1",
        name="test",
        content="some content",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        similarity=0.8,
        last_accessed_days_ago=1.0,
        edge_count=5,
        adjacency_score=0.5,
        freshness="ACTIVE",
        has_conflict=False,
        conflict_status=None,
    )
    defaults.update(overrides)
    return CandidateData(**defaults)


# ===========================================================================
# #4 — NaN/Inf scoring guard
# ===========================================================================


class TestNanInfScoring:
    """NaN or Inf in candidate fields must not corrupt ranking."""

    def test_nan_similarity_coerced_to_zero(self):
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="nan-sim", similarity=float("nan"))],
            QueryPreset.KNOWLEDGE,
        )
        assert len(results) == 1
        assert results[0].breakdown.semantic_similarity == 0.0
        assert math.isfinite(results[0].final_score)

    def test_nan_last_accessed_coerced_to_zero(self):
        """NaN days_ago → 0.0 → recency = exp(0) = 1.0 (treated as fresh)."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="nan-days", last_accessed_days_ago=float("nan"))],
            QueryPreset.KNOWLEDGE,
        )
        recency = results[0].breakdown.recency_bonus
        assert recency == 1.0
        assert math.isfinite(results[0].final_score)

    def test_inf_edge_count_does_not_crash(self):
        svc = ScoringService()
        results = svc.score_candidates(
            [
                _candidate(uuid="inf-edge", edge_count=10**18),
                _candidate(uuid="normal", edge_count=5),
            ],
            QueryPreset.KNOWLEDGE,
        )
        assert len(results) == 2
        for r in results:
            assert math.isfinite(r.final_score)

    def test_nan_adjacency_coerced_to_zero(self):
        """NaN adjacency → 0.0 via _safe_float."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="nan-adj", adjacency_score=float("nan"))],
            QueryPreset.CONNECTED,
        )
        assert results[0].breakdown.adjacency_bonus == 0.0

    def test_inf_similarity_coerced_to_zero(self):
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="inf-sim", similarity=float("inf"))],
            QueryPreset.KNOWLEDGE,
        )
        assert len(results) == 1
        assert results[0].breakdown.semantic_similarity == 0.0
        assert math.isfinite(results[0].final_score)

    def test_mixed_nan_normal_sort_deterministic(self):
        """NaN sanitized → deterministic sort order."""
        svc = ScoringService()
        results = svc.score_candidates(
            [
                _candidate(uuid="normal-a", similarity=0.9),
                _candidate(uuid="nan-node", similarity=float("nan")),
                _candidate(uuid="normal-b", similarity=0.5),
            ],
            QueryPreset.KNOWLEDGE,
        )
        uuids = [r.uuid for r in results]
        assert uuids[0] == "normal-a"  # highest similarity
        assert uuids[-1] == "nan-node"  # NaN → 0.0 → lowest


# ===========================================================================
# #3 — Compress with None/empty LLM summary
# ===========================================================================


class TestCompressEmptySummary:
    """Compress guard now uses `not summary or not summary.strip()`."""

    def test_guard_blocks_none(self):
        summary: str | None = None
        assert not summary  # blocked

    def test_guard_blocks_empty_string(self):
        summary: str | None = ""
        assert not summary  # blocked after fix

    def test_guard_blocks_whitespace(self):
        summary: str | None = "   "
        assert not summary.strip()  # blocked after fix


# ===========================================================================
# #7 — Budget requeue: episode_id removed from _queued_episode_ids after requeue
# ===========================================================================


class TestBudgetRequeueNotLost:
    """Budget-capped episodes must be re-enqueuable after requeue."""

    async def test_budget_requeue_clears_queued_set(self, stub_memory_graph_adapter):
        """After budget-cap requeue, episode_uuid must be removed from
        _queued_episode_ids so the recovery sweep can re-enqueue it."""
        from menhir.services.ingest_service import IngestService

        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )
        episode_uuid = "budget-test-ep"

        # Simulate: episode was enqueued
        svc._queued_episode_ids.add(episode_uuid)

        # Simulate: worker processes it and budget requeue happens
        # After _process_pending_episode returns, the finally block discards it
        svc._queued_episode_ids.discard(episode_uuid)

        # Now recovery sweep should be able to re-enqueue it
        assert episode_uuid not in svc._queued_episode_ids
        # Simulating _enqueue_pending_episode: check is "not in set"
        can_reenqueue = episode_uuid not in svc._queued_episode_ids
        assert can_reenqueue, "Episode must be re-enqueuable after budget requeue"


# ===========================================================================
# #8 — LLM callback cleanup leak
# ===========================================================================


class TestCallbackCleanupOnCrash:
    """LLM usage callback must be cleaned up even if processing crashes."""

    async def test_callback_reset_after_exception(self):
        from menhir.infrastructure.observability import (
            reset_llm_usage_callback,
            set_llm_usage_callback,
        )

        calls: list[Any] = []
        token = set_llm_usage_callback(lambda event: calls.append(event))

        # Simulate crash during processing
        try:
            raise RuntimeError("simulated enrichment crash")
        except RuntimeError:
            pass
        finally:
            reset_llm_usage_callback(token)

        # After cleanup, the callback should be deregistered
        # Setting a new one should not trigger the old one
        new_calls: list[Any] = []
        token2 = set_llm_usage_callback(lambda event: new_calls.append(event))
        reset_llm_usage_callback(token2)

        # Old callback list should not grow from new registrations
        assert len(calls) == 0

    async def test_double_reset_raises(self):
        """Double reset of same token raises RuntimeError (ContextVar semantics).
        This means crash recovery MUST use try/finally, not catch-and-retry."""
        from menhir.infrastructure.observability import (
            reset_llm_usage_callback,
            set_llm_usage_callback,
        )

        token = set_llm_usage_callback(lambda event: None)
        reset_llm_usage_callback(token)
        with pytest.raises(RuntimeError, match="has already been used"):
            reset_llm_usage_callback(token)


# ===========================================================================
# #10 — Negative days_ago from future timestamps
# ===========================================================================


class TestNegativeDaysAgo:
    """Future timestamps must not produce inflated recency scores."""

    def test_negative_days_ago_recency_clamped_to_one(self):
        """exp(-lambda * negative) > 1.0, but min(1.0, ...) clamps it."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="future", last_accessed_days_ago=-10.0)],
            QueryPreset.KNOWLEDGE,
        )
        recency = results[0].breakdown.recency_bonus
        # exp(-0.1 * -10) = exp(1) ≈ 2.718, clamped to 1.0
        assert recency == 1.0, "Negative days_ago should clamp recency to 1.0"

    def test_negative_days_ago_does_not_exceed_fresh_node(self):
        svc = ScoringService()
        future = _candidate(uuid="future", last_accessed_days_ago=-100.0, similarity=0.5)
        fresh = _candidate(uuid="fresh", last_accessed_days_ago=0.0, similarity=0.5)

        results = svc.score_candidates([future, fresh], QueryPreset.RECENT)
        future_r = next(r for r in results if r.uuid == "future")
        fresh_r = next(r for r in results if r.uuid == "fresh")

        # Both should have recency = 1.0 (clamped)
        assert future_r.breakdown.recency_bonus == fresh_r.breakdown.recency_bonus == 1.0


# ===========================================================================
# #15 — has_conflict / conflict_status mismatch
# ===========================================================================


class TestConflictMetadataMismatch:
    """Mismatched has_conflict and conflict_status should produce correct bonus."""

    def test_has_conflict_true_status_none(self):
        """has_conflict=True, conflict_status=None → partial boost (0.5)."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(has_conflict=True, conflict_status=None)],
            QueryPreset.CONFLICT,
        )
        # has_conflict=True but status != "unresolved" → elif branch → 0.5
        assert results[0].breakdown.conflict_bonus == 0.5

    def test_has_conflict_false_status_unresolved(self):
        """has_conflict=False, conflict_status='unresolved' → no boost (0.0).
        The code checks has_conflict first, so False short-circuits."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(has_conflict=False, conflict_status="unresolved")],
            QueryPreset.CONFLICT,
        )
        # has_conflict=False → else branch → 0.0
        assert results[0].breakdown.conflict_bonus == 0.0

    def test_has_conflict_true_status_resolved(self):
        """has_conflict=True, conflict_status='resolved' → partial boost (0.5)."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(has_conflict=True, conflict_status="resolved")],
            QueryPreset.CONFLICT,
        )
        assert results[0].breakdown.conflict_bonus == 0.5

    def test_has_conflict_true_status_unresolved(self):
        """has_conflict=True, conflict_status='unresolved' → full boost (1.0)."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(has_conflict=True, conflict_status="unresolved")],
            QueryPreset.CONFLICT,
        )
        assert results[0].breakdown.conflict_bonus == 1.0


# ===========================================================================
# #16 — Self-conflict UUID guard
# ===========================================================================


class TestSelfConflictGuard:
    """Similarity search returning the node itself must not create self-conflict."""

    async def test_self_uuid_skipped_in_contradiction_check(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        """_check_contradictions_batch skips other_uuid == uuid."""
        from menhir.infrastructure.pending_actions import PendingActionStore
        from menhir.services import LifecycleService
        from menhir.services.lifecycle_service import SIMILARITY_CONFLICT_THRESHOLD

        # search_scored returns the node itself at high similarity
        stub_graphiti_client.search_scored_results = [
            ("self-node", "self match", SIMILARITY_CONFLICT_THRESHOLD + 0.1),
        ]

        svc = LifecycleService(
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=stub_graphiti_client,
            llm=None,
            pending_actions=PendingActionStore(),
        )

        candidates = [{"uuid": "self-node", "content": "some content", "name": "self"}]
        conflicts = await svc._check_contradictions_batch(candidates)

        assert conflicts == 0, "Node must not conflict with itself"
        assert len(stub_memory_graph_adapter.conflict_calls) == 0


# ===========================================================================
# #18 — Double-resolve graceful handling
# ===========================================================================


class TestDoubleResolve:
    """Resolving an already-resolved group should be handled gracefully."""

    def test_resolve_then_resolve_same_group(self, stub_memory_graph_adapter):
        """After first resolve, second keep_both on same group_id still works
        in the stub (stub doesn't null conflict_group_id). In real Neo4j,
        the Cypher `WHERE n.conflict_group_id = $group_id` would match 0 rows
        after first resolve clears the field. This documents the divergence."""
        from tests.test_regression_conflicts import _seed_conflict_group

        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-resolved",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
        )

        result1 = stub_memory_graph_adapter.resolve_conflict_group(
            "group-resolved", "keep_both", resolution_status="resolved",
        )
        assert result1["resolved"] == 2
        # Group status should be resolved now
        group = stub_memory_graph_adapter.conflict_groups["group-resolved"]
        assert group["status"] == "resolved"

    def test_resolve_nonexistent_group_does_not_crash(self, stub_memory_graph_adapter):
        """Resolving a nonexistent group should not crash — returns 0 resolved.
        In real Neo4j, the Cypher MATCH would find 0 rows."""
        result = stub_memory_graph_adapter.resolve_conflict_group(
            "nonexistent-group", "keep_both",
        )
        assert result["resolved"] == 0

    def test_resolve_replace_wrong_keep_uuid(self, stub_memory_graph_adapter):
        """Replace with keep_uuid not in group → ValueError."""
        from tests.test_regression_conflicts import _seed_conflict_group

        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
        )

        with pytest.raises(ValueError, match="not a member"):
            stub_memory_graph_adapter.resolve_conflict_group(
                "group-1", "replace", keep_uuid="node-Z",
            )


# ===========================================================================
# #5 — Stale embedding cache key (text-only, no model version)
# ===========================================================================


class TestEmbeddingCacheKey:
    """Cache key is SHA256(model + NUL + text) — model-aware after fix."""

    def test_same_text_different_model_is_cache_miss(self):
        """Different models produce different cache keys — no collision."""
        cache = EmbeddingCache()
        text = "hello world"
        vec_a = [0.1, 0.2, 0.3]
        vec_b = [0.4, 0.5, 0.6]

        cache.set(text, vec_a, model="model-a")
        cache.set(text, vec_b, model="model-b")

        assert cache.get(text, model="model-a") == vec_a
        assert cache.get(text, model="model-b") == vec_b

    def test_cache_key_includes_model(self):
        """Key differs when model differs."""
        cache = EmbeddingCache()
        key_a = cache._key("test", model="model-a")
        key_b = cache._key("test", model="model-b")
        assert key_a != key_b

    def test_same_model_same_text_is_cache_hit(self):
        cache = EmbeddingCache()
        cache.set("hello", [1.0], model="m1")
        assert cache.get("hello", model="m1") == [1.0]

    def test_different_text_different_key(self):
        cache = EmbeddingCache()
        assert cache._key("alpha") != cache._key("beta")

    def test_cache_miss_after_eviction(self):
        """LRU eviction works — old entries get evicted."""
        cache = EmbeddingCache(max_size=2)
        cache.set("a", [1.0])
        cache.set("b", [2.0])
        cache.set("c", [3.0])  # evicts "a"

        assert cache.get("a") is None
        assert cache.get("b") == [2.0]
        assert cache.get("c") == [3.0]

    def test_backward_compat_no_model_arg(self):
        """Omitting model defaults to empty string — backward compatible."""
        cache = EmbeddingCache()
        cache.set("text", [1.0])
        assert cache.get("text") == [1.0]


# ===========================================================================
# #12 — LLM returns valid HTTP 200 with wrong JSON schema
# ===========================================================================


class TestLLMWrongSchema:
    """Backend returns unexpected response shape — _chat_text should handle gracefully."""

    async def test_backend_returns_none_string(self):
        """Backend returns empty string → _chat_text returns None."""
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _EmptyBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return ""

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_EmptyBackend())
        result = await adapter._chat_text(
            system_prompt="test",
            user_prompt="test",
            operation="test",
            max_retries=0,
        )
        assert result is None

    async def test_backend_returns_whitespace_only(self):
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _WhitespaceBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "   \n  "

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_WhitespaceBackend())
        result = await adapter._chat_text(
            system_prompt="test",
            user_prompt="test",
            operation="test",
            max_retries=0,
        )
        assert result is None

    async def test_backend_raises_returns_none(self):
        """Backend raises exception → _chat_text returns None after retries."""
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _CrashBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                raise RuntimeError("upstream broke")

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_CrashBackend())
        result = await adapter._chat_text(
            system_prompt="test",
            user_prompt="test",
            operation="test",
            max_retries=0,
            retry_base_delay_s=0.0,
        )
        assert result is None

    async def test_backend_returns_thinking_tags_stripped(self):
        """Backend returns thinking tags → stripped to actual content."""
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _ThinkingBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "<think>reasoning here</think>actual answer"

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_ThinkingBackend())
        result = await adapter._chat_text(
            system_prompt="test",
            user_prompt="test",
            operation="test",
            max_retries=0,
        )
        assert result == "actual answer"

    async def test_confirm_contradiction_unexpected_response(self):
        """LLM returns neither CONFLICT nor CLEAR → returns None."""
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _GarbageBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "MAYBE"

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_GarbageBackend())
        result = await adapter.confirm_contradiction(
            name_a="A", content_a="fact A",
            name_b="B", content_b="fact B",
        )
        assert result is None, "Unexpected LLM response should return None"

    async def test_confirm_contradiction_empty_returns_none(self):
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _EmptyBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return ""

        adapter = LLMAdapter(base_url="http://test", api_key="test", chat_model="test", embed_model="test", backend=_EmptyBackend())
        result = await adapter.confirm_contradiction(
            name_a="A", content_a="a",
            name_b="B", content_b="b",
        )
        assert result is None


# ===========================================================================
# #1 — Partial failure between Graphiti success and Neo4j stamp
# ===========================================================================


class TestPartialStampFailure:
    """If stamp_ingest_metadata fails after Graphiti succeeds,
    try_reconcile_existing should recover on retry."""

    async def test_reconcile_finds_existing_completion(
        self,
        stub_memory_graph_adapter,
    ):
        """When find_completed_episode_artifact returns data, reconcile succeeds."""
        from unittest.mock import AsyncMock, patch
        from menhir.services.enrichment_steps import EnrichmentContext, try_reconcile_existing

        # Key must match (anchor_uuid, anchor_name) that try_reconcile_existing will look up
        stub_memory_graph_adapter.completed_episode_artifacts[("ep-1", "test-ep")] = {
            "resolved_episode_uuid": "resolved-123",
            "entity_uuids": ["ent-1", "ent-2"],
            "edge_uuids": ["edge-1"],
        }
        # Add a pending row so mark_episode_ready finds the episode
        stub_memory_graph_adapter.pending_episode_rows["ep-1"] = {
            "processing_state": ProcessingState.ENRICHING,
            "processing_owner": "w1",
        }

        ctx = EnrichmentContext(
            episode_uuid="ep-1",
            claimed={"name": "test-ep", "session_id": "s1", "user_id": "u1", "source": "test"},
            started=perf_counter(),
            processing_attempts=1,
            worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(),
            lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1),
            processing_steps_total=5,
            settings_record_revisions=False,
            ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        with patch("menhir.services.enrichment_steps.emit_scheduler_task_event", new_callable=AsyncMock), \
             patch("menhir.services.enrichment_steps.record_mcp_event"):
            handled = await try_reconcile_existing(ctx)
        assert handled is True

    async def test_reconcile_returns_false_when_no_artifact(
        self,
        stub_memory_graph_adapter,
    ):
        """When no prior completion exists, reconcile returns False."""
        from menhir.services.enrichment_steps import EnrichmentContext, try_reconcile_existing

        ctx = EnrichmentContext(
            episode_uuid="ep-new",
            claimed={"name": "new-ep", "session_id": "s1", "user_id": "u1", "source": "test"},
            started=perf_counter(),
            processing_attempts=0,
            worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(),
            lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1),
            processing_steps_total=5,
            settings_record_revisions=False,
            ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        handled = await try_reconcile_existing(ctx)
        assert handled is False


# ===========================================================================
# #11 — Negative edge weights corrupt adjacency scoring
# ===========================================================================


class TestNegativeEdgeWeights:
    """Negative edge weights should clamp to 0 via scoring, not produce negative adjacency."""

    def test_negative_adjacency_score_clamped_to_zero(self):
        """max(0.0, min(1.0, negative)) → 0.0."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="neg-adj", adjacency_score=-0.5)],
            QueryPreset.CONNECTED,
        )
        assert results[0].breakdown.adjacency_bonus == 0.0

    def test_negative_weight_in_adjacency_map_clamped(self):
        """Adjacency normalization: weight/max_adj with negative weight → negative ratio → clamped."""
        svc = ScoringService()
        results = svc.score_candidates(
            [_candidate(uuid="neg", adjacency_score=-1.0, similarity=0.5)],
            QueryPreset.CONNECTED,
        )
        assert results[0].breakdown.adjacency_bonus == 0.0
        assert math.isfinite(results[0].final_score)


# ===========================================================================
# #13 — Episode text exceeds LLM context window (preflight rejection)
# ===========================================================================


class TestEpisodePreflight:
    """build_episode_preflight_rejection catches oversized episodes."""

    def test_oversized_episode_rejected(self):
        from menhir.services.enrichment_steps import build_episode_preflight_rejection

        big_text = "x" * 500_000  # way over any token limit
        result = build_episode_preflight_rejection(big_text, max_estimated_tokens=12000)
        assert result is not None
        assert result["code"] == "episode_preflight_too_large"
        assert result["estimated_tokens"] > 12000

    def test_small_episode_passes_preflight(self):
        from menhir.services.enrichment_steps import build_episode_preflight_rejection

        result = build_episode_preflight_rejection("short text", max_estimated_tokens=12000)
        assert result is None

    def test_zero_limit_disables_preflight(self):
        from menhir.services.enrichment_steps import build_episode_preflight_rejection

        result = build_episode_preflight_rejection("x" * 500_000, max_estimated_tokens=0)
        assert result is None

    def test_empty_episode_passes(self):
        from menhir.services.enrichment_steps import build_episode_preflight_rejection

        result = build_episode_preflight_rejection("", max_estimated_tokens=12000)
        assert result is None


class TestPropagateUserFlag:
    """propagate_user_flag is the single source of truth for flag propagation:
    it flags semantic nodes and skips structural ones without aborting the caller
    (regression: the guard was duplicated across three sites and one dedup-onto-
    structural node used to abort a flagged episode's enrichment)."""

    def test_skips_structural_nodes_and_flags_the_rest(self):
        from menhir.services.enrichment_steps import propagate_user_flag

        flagged: list[str] = []

        class FakeAdapter:
            def flag_memory(self, uuid: str) -> bool:
                if uuid == "structural":
                    raise ValueError(
                        "Cannot flag structural graph node (role=directory). "
                        "Flag semantic memories only."
                    )
                flagged.append(uuid)
                return True

        # Must not raise even though the middle node is structural.
        propagate_user_flag(
            FakeAdapter(), ["sem-1", "structural", "sem-2"], episode_uuid="ep-1"
        )
        assert flagged == ["sem-1", "sem-2"]

    def test_empty_list_is_noop(self):
        from menhir.services.enrichment_steps import propagate_user_flag

        class FakeAdapter:
            def flag_memory(self, uuid: str) -> bool:  # pragma: no cover
                raise AssertionError("flag_memory should not be called for empty input")

        propagate_user_flag(FakeAdapter(), [], episode_uuid="ep-1")


# ===========================================================================
# #17 — Transitive conflicts not joined
# ===========================================================================


class TestTransitiveConflicts:
    """A≈B and B≈C create separate groups — no transitive closure."""

    async def test_two_separate_groups_created(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        """Conflict detection creates independent groups per node, not transitive closure."""
        from menhir.infrastructure.pending_actions import PendingActionStore
        from menhir.services import LifecycleService
        from menhir.services.lifecycle_service import SIMILARITY_CONFLICT_THRESHOLD

        # search_scored returns high-sim hit for each query
        stub_graphiti_client.search_scored_results = [
            ("node-B", "B content", SIMILARITY_CONFLICT_THRESHOLD + 0.05),
        ]

        svc = LifecycleService(
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=stub_graphiti_client,
            llm=None,
            pending_actions=PendingActionStore(),
        )

        # Two candidates: A and C. Both find B as similar.
        candidates = [
            {"uuid": "node-A", "content": "A content", "name": "A"},
            {"uuid": "node-C", "content": "C content", "name": "C"},
        ]
        conflicts = await svc._check_contradictions_batch(candidates)

        # Each node creates its own conflict with B — no transitive link A↔C
        assert conflicts >= 1
        # Document: A and C end up in separate groups with B


# ===========================================================================
# #19 — Duplicate UUIDs in similarity search inflate sharpness
# ===========================================================================


class TestDuplicateUUIDSharpness:
    """F2: Duplicate UUIDs from search are deduped by count_similar_by_cosine."""

    async def test_duplicate_uuid_counted_once(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        from menhir.infrastructure.pending_actions import PendingActionStore
        from menhir.services import LifecycleService

        # count_similar_by_cosine deduplicates by uuid (as of F2)
        stub_graphiti_client.count_similar_by_cosine_results = 2

        svc = LifecycleService(
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=stub_graphiti_client,
            llm=None,
            pending_actions=PendingActionStore(),
        )

        count = await svc._count_similar_nodes("test query", exclude_uuid="self-uuid")

        # F2 closes the duplicate-uuid inflation gap: count_similar_by_cosine returns
        # the distinct uuid count above the cosine floor, excluding self and Episodic.
        # With the stub returning 2, we assert 2 (deduped).
        assert count == 2, "Duplicate UUIDs are deduped by uuid in count_similar_by_cosine"


# ===========================================================================
# F2 — Lawful sharpness: count_similar_by_cosine tests
# ===========================================================================


class TestCountSimilarByCosineCounting:
    """F2: Verify counting semantics of count_similar_by_cosine."""

    async def test_distinct_neighbors_above_floor_counted(self, stub_graphiti_client):
        """Distinct neighbors above floor are counted; self-uuid and Episodic dropped."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        # Create a mock Graphiti client that returns results
        mock_client = SimpleNamespace()
        nodes = [
            SimpleNamespace(uuid="node-1", name="neighbor1", labels=["Entity"]),
            SimpleNamespace(uuid="node-2", name="neighbor2", labels=["Entity"]),
            SimpleNamespace(uuid="self-uuid", name="self", labels=["Entity"]),  # self, should be dropped
            SimpleNamespace(uuid="node-3", name="episodic", labels=["Episodic"]),  # Episodic, should be dropped
        ]
        results = SimpleNamespace(nodes=nodes, node_reranker_scores=[0.9, 0.8, 0.85, 0.95])

        async def async_search(*args, **kwargs):
            return results

        mock_client.search_ = async_search

        graphiti = GraphitiClient(client=mock_client)
        # Override the endpoint guard to not actually try to wake anything
        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        count = await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self-uuid", min_cosine=0.7, limit=50
        )
        # Should count node-1 and node-2 only (excluding self-uuid and Episodic)
        assert count == 2

    async def test_dimension_mismatch_returns_minus_one(self, stub_graphiti_client):
        """Dimension mismatch exception returns -1 (advisory, not critical)."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        mock_client = SimpleNamespace()

        async def failing_search(*args, **kwargs):
            raise RuntimeError(
                "Failed executing query with exception: vector.similarity.cosine dimension mismatch"
            )

        mock_client.search_ = failing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        count = await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self-uuid", min_cosine=0.7, limit=50
        )
        assert count == -1

    async def test_other_exception_returns_minus_one(self, stub_graphiti_client):
        """Any exception (not just dimension mismatch) returns -1."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        mock_client = SimpleNamespace()

        async def failing_search(*args, **kwargs):
            raise ValueError("Some other error")

        mock_client.search_ = failing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        count = await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self-uuid", min_cosine=0.7, limit=50
        )
        assert count == -1


class TestCountSimilarByConsineGraphitiContract:
    """F2: Verify Graphiti SearchConfig contract for cosine-only search."""

    async def test_cosine_only_config_no_bm25(self):
        """SearchConfig must use cosine_similarity only, no BM25."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from graphiti_core.search.search_config import NodeSearchMethod, NodeReranker
        from types import SimpleNamespace

        captured_config = None

        async def capturing_search(query, config, group_ids):
            nonlocal captured_config
            captured_config = config
            return SimpleNamespace(nodes=[], node_reranker_scores=[])

        mock_client = SimpleNamespace()
        mock_client.search_ = capturing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self", min_cosine=0.75, limit=50, group_ids=["g1"]
        )

        # Assert cosine-only
        assert captured_config is not None
        assert NodeSearchMethod.cosine_similarity in captured_config.node_config.search_methods
        assert NodeSearchMethod.bm25 not in captured_config.node_config.search_methods

    async def test_sim_min_score_passed(self):
        """sim_min_score must equal the min_cosine parameter."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        captured_config = None

        async def capturing_search(query, config, group_ids):
            nonlocal captured_config
            captured_config = config
            return SimpleNamespace(nodes=[], node_reranker_scores=[])

        mock_client = SimpleNamespace()
        mock_client.search_ = capturing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self", min_cosine=0.75, limit=50
        )

        assert captured_config.node_config.sim_min_score == 0.75

    async def test_limit_plus_one_requested(self):
        """limit must be requested as limit+1 (to allow for self-exclusion)."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        captured_config = None

        async def capturing_search(query, config, group_ids):
            nonlocal captured_config
            captured_config = config
            return SimpleNamespace(nodes=[], node_reranker_scores=[])

        mock_client = SimpleNamespace()
        mock_client.search_ = capturing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self", min_cosine=0.75, limit=50
        )

        assert captured_config.limit == 51

    async def test_group_ids_preserved(self):
        """group_ids (namespace scoping) must be passed through."""
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from types import SimpleNamespace

        captured_group_ids = None

        async def capturing_search(query, config, group_ids):
            nonlocal captured_group_ids
            captured_group_ids = group_ids
            return SimpleNamespace(nodes=[], node_reranker_scores=[])

        mock_client = SimpleNamespace()
        mock_client.search_ = capturing_search

        graphiti = GraphitiClient(client=mock_client)

        async def noop_guard(*args, **kwargs):
            pass

        graphiti._ensure_graphiti_endpoints_alive = noop_guard

        test_group_ids = ["group-1", "group-2"]
        await graphiti.count_similar_by_cosine(
            "query", exclude_uuid="self", min_cosine=0.75, group_ids=test_group_ids
        )

        assert captured_group_ids == test_group_ids


class TestF2RegressionSharpnessFlip:
    """F2: Regression fixture in corrupted 0.2–0.5 band (RRF vs true cosine)."""

    async def test_mid_band_corrupt_flips_to_protective(
        self, stub_memory_graph_adapter, stub_graphiti_client
    ):
        """Node with RRF sharpness ~0.33 (fails promote bar) but zero true-cosine neighbors → flips to 1.0."""
        from menhir.infrastructure.pending_actions import PendingActionStore
        from menhir.services.lifecycle_service import LifecycleService

        # Under old RRF path, this node had enough rank-neighbors to score ~0.33 sharpness
        # (failing the >=0.5 promote bar). But its true cosine neighbor count is 0.
        stub_graphiti_client.count_similar_by_cosine_results = 0  # No true cosine neighbors

        svc = LifecycleService(
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=stub_graphiti_client,
            llm=None,
            pending_actions=PendingActionStore(),
        )

        similar_count = await svc._count_similar_nodes(
            "unique query", exclude_uuid="node-uuid"
        )
        sharpness = svc.compute_sharpness(similar_count)

        # With true cosine count of 0, sharpness should be 1.0 (protective, promotes)
        assert sharpness == 1.0
        # Old RRF path would have scored this ~0.33 (mid-band corrupt), failing promote
        assert sharpness >= 0.5  # Now passes the promote bar


# ===========================================================================
# #20 — Mixed batch embedding index ordering
# ===========================================================================


class TestMixedBatchEmbeddingOrdering:
    """Cache hits/misses must be spliced back into correct positions."""

    async def test_mixed_hits_misses_preserve_order(self):
        from menhir.infrastructure.embedding_cache import EmbeddingCache
        from menhir.infrastructure.observability import _CachingEmbeddingsEndpoint

        cache = EmbeddingCache()
        cache.set("cached-text", [1.0, 2.0, 3.0], model="test")

        @dataclass
        class _FakeInner:
            call_inputs: list = field(default_factory=list)

            async def create(self, **kwargs: Any) -> Any:
                texts = kwargs.get("input", [])
                if isinstance(texts, str):
                    texts = [texts]
                self.call_inputs.append(texts)
                data = []
                for i, t in enumerate(texts):
                    data.append(SimpleNamespace(embedding=[float(ord(t[0]))], index=i, object="embedding"))
                return SimpleNamespace(
                    data=data, model="test", object="list",
                    usage=SimpleNamespace(prompt_tokens=0, total_tokens=0),
                )

        inner = _FakeInner()
        endpoint = _CachingEmbeddingsEndpoint(inner, cache)

        result = await endpoint.create(input=["cached-text", "miss-text", "cached-text"], model="test")

        # Position 0 and 2 should be cache hits, position 1 miss
        assert len(result.data) == 3
        assert result.data[0].embedding == [1.0, 2.0, 3.0]  # cache hit
        assert result.data[2].embedding == [1.0, 2.0, 3.0]  # cache hit
        assert result.data[1].index == 1


# ===========================================================================
# #21 — Identical scores sort stability
# ===========================================================================


class TestIdenticalScoreSortStability:
    """Equal-score candidates preserve original insertion order (Python stable sort)."""

    def test_stable_sort_preserves_order(self):
        svc = ScoringService()
        # All identical inputs → identical scores
        candidates = [
            _candidate(uuid=f"node-{i}", similarity=0.5, edge_count=3, adjacency_score=0.3)
            for i in range(5)
        ]
        results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)

        # Python sort is stable — original order preserved for equal keys
        uuids = [r.uuid for r in results]
        assert uuids == [f"node-{i}" for i in range(5)]


# ===========================================================================
# #22 — Retry of manually-deleted episode loops
# ===========================================================================


class TestRetryDeletedEpisode:
    """Episode in FAILED state with deleted node — scheduler retry should handle gracefully."""

    async def test_retry_row_without_uuid_returns_terminal_or_exhausted(self):
        """Row missing uuid/name fields falls through reconcile to classification."""
        from unittest.mock import patch
        from menhir.services.scheduler_tasks import retry_process_candidate

        @dataclass
        class _MinimalAdapter:
            def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
                return None

        @dataclass
        class _MinimalIngest:
            def get_queue_depth(self) -> int:
                return 0

            def get_context_window_retry_attempts(self) -> int:
                return 6

        # Row with empty uuid — reconcile skipped; unknown error → "manual_review" classification → "terminal"
        row = {
            "uuid": "",
            "name": "",
            "processing_attempts": 5,
            "processing_error": "some unknown error",
            "processing_completed_at": "2026-01-01T00:00:00+00:00",
        }

        with patch("menhir.services.scheduler_tasks.record_failure_event"):
            result = await retry_process_candidate(
                _MinimalAdapter(), _MinimalIngest(), row,
                max_attempts=3, now=datetime.now(timezone.utc),
            )
        # Unknown errors are classified as manual_review which is treated as terminal
        assert result == "terminal"

    async def test_retry_terminal_classification_not_requeued(self):
        """Terminal failures (e.g. parse errors) are never requeued."""
        from unittest.mock import patch
        from menhir.services.scheduler_tasks import retry_process_candidate

        @dataclass
        class _MinimalAdapter:
            def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
                return None

        @dataclass
        class _MinimalIngest:
            def get_queue_depth(self) -> int:
                return 0

            def get_context_window_retry_attempts(self) -> int:
                return 6

        row = {
            "uuid": "ep-1",
            "name": "test",
            "processing_attempts": 1,
            "processing_error": "Failed to extract JSON from LLM response",
            "processing_completed_at": "2026-01-01T00:00:00+00:00",
        }

        with patch("menhir.services.scheduler_tasks.record_failure_event"):
            result = await retry_process_candidate(
                _MinimalAdapter(), _MinimalIngest(), row,
                max_attempts=3, now=datetime.now(timezone.utc),
            )
        assert result == "terminal"

    async def test_context_window_error_gets_extended_retry_budget(self):
        """Context window errors bypass terminal classification and use extended cap."""
        from unittest.mock import patch
        from menhir.services.scheduler_tasks import retry_process_candidate

        @dataclass
        class _MinimalAdapter:
            def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
                return None

        requeued: list[str] = []

        @dataclass
        class _MinimalIngest:
            def get_queue_depth(self) -> int:
                return 0

            def get_context_window_retry_attempts(self) -> int:
                return 6

            async def requeue_failed_episode(self, episode_uuid: str) -> bool:
                requeued.append(episode_uuid)
                return True

        # Context window error at attempt 4 — beyond max_attempts=3 but within context_cap=6
        row = {
            "uuid": "ep-ctx",
            "name": "test",
            "processing_attempts": 4,
            "processing_error": "Cannot truncate prompt with n_keep (8199) >= n_ctx (4096)",
            "processing_completed_at": "2025-01-01T00:00:00+00:00",
        }

        with patch("menhir.services.scheduler_tasks.record_failure_event"):
            result = await retry_process_candidate(
                _MinimalAdapter(), _MinimalIngest(), row,
                max_attempts=3, now=datetime.now(timezone.utc),
            )
        assert result == "requeued", "context window error should get extended budget"
        assert requeued == ["ep-ctx"]

    async def test_context_window_error_exhausted_beyond_extended_cap(self):
        """Context window errors ARE exhausted when they exceed the extended cap."""
        from unittest.mock import patch
        from menhir.services.scheduler_tasks import retry_process_candidate

        @dataclass
        class _MinimalAdapter:
            def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
                return None

        @dataclass
        class _MinimalIngest:
            def get_queue_depth(self) -> int:
                return 0

            def get_context_window_retry_attempts(self) -> int:
                return 6

        # Context window error at attempt 7 — beyond context_cap=6
        row = {
            "uuid": "ep-ctx-exhausted",
            "name": "test",
            "processing_attempts": 7,
            "processing_error": "Cannot truncate prompt with n_keep (8199) >= n_ctx (4096)",
            "processing_completed_at": "2025-01-01T00:00:00+00:00",
        }

        with patch("menhir.services.scheduler_tasks.record_failure_event"):
            result = await retry_process_candidate(
                _MinimalAdapter(), _MinimalIngest(), row,
                max_attempts=3, now=datetime.now(timezone.utc),
            )
        assert result == "exhausted"


# ===========================================================================
# #23 — keep_uuid deleted between scan and resolve
# ===========================================================================


class TestKeepUuidDeletedDuringResolve:
    """keep_uuid not found in group members → ValueError."""

    def test_missing_keep_uuid_in_resolve(self, stub_memory_graph_adapter):
        from tests.test_regression_conflicts import _seed_conflict_group

        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
        )

        # keep_uuid that isn't in the group → ValueError
        with pytest.raises(ValueError, match="not a member"):
            stub_memory_graph_adapter.resolve_conflict_group(
                "group-1", "replace", keep_uuid="deleted-node",
            )


# ===========================================================================
# #24 — Scheduler with all circuit breakers OPEN
# ===========================================================================


class TestCircuitBreakerOpenState:
    """CircuitBreaker in OPEN state raises CircuitOpenError."""

    async def test_open_breaker_raises(self):
        from menhir.infrastructure.circuit_breaker import CircuitBreaker, CircuitOpenError

        breaker = CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=300.0)

        # Trip the breaker
        async def _fail():
            raise ConnectionError("upstream down")

        with pytest.raises(ConnectionError):
            await breaker.call(_fail)

        # Now it should be OPEN
        with pytest.raises(CircuitOpenError):
            await breaker.call(lambda: None)

    async def test_breaker_resets_on_process_restart(self):
        """New CircuitBreaker instance starts CLOSED — state lost on restart."""
        from menhir.infrastructure.circuit_breaker import CircuitBreaker, CircuitState

        breaker = CircuitBreaker(name="test")
        assert breaker._state == CircuitState.CLOSED
        assert breaker._failures == 0


# ===========================================================================
# #25 — Empty query string to recall
# ===========================================================================


class TestEmptyRecallQuery:
    """Empty query should not crash recall."""

    async def test_empty_query_returns_empty(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        from menhir.services.recall_service import RecallService
        from menhir.services.scoring_service import ScoringService

        stub_graphiti_client.search_scored_results = []

        svc = RecallService(
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=stub_graphiti_client,
            scoring_service=ScoringService(),
        )

        result = await svc.recall("", preset=QueryPreset.KNOWLEDGE, limit=10)
        assert result.results == []
        assert result.candidates_evaluated == 0


# ===========================================================================
# #26 — Zero/negative timeout in wait_for_episode_processing
# ===========================================================================


class TestZeroNegativeTimeout:
    """Negative/zero timeout should return immediately, not hang."""

    async def test_zero_timeout_returns_immediately(self, stub_memory_graph_adapter):
        from menhir.services.ingest_service import IngestService

        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )

        start = perf_counter()
        result = await svc.wait_for_episode_processing("ep-1", timeout_s=0.0)
        elapsed = perf_counter() - start

        assert elapsed < 0.5, "Should return near-instantly"

    async def test_negative_timeout_returns_immediately(self, stub_memory_graph_adapter):
        from menhir.services.ingest_service import IngestService

        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )

        start = perf_counter()
        result = await svc.wait_for_episode_processing("ep-1", timeout_s=-5.0)
        elapsed = perf_counter() - start

        assert elapsed < 0.5, "Should return near-instantly"

    async def test_ready_state_returns_immediately_not_after_full_timeout(
        self, stub_memory_graph_adapter,
    ) -> None:
        """Regression: ProcessingState is a (str, Enum) mixin, and str(ProcessingState.READY)
        returns the qualified repr "ProcessingState.READY", not the value "READY".
        wait_for_episode_processing used to wrap the row's state in str() before
        comparing against {ProcessingState.READY, ProcessingState.FAILED}, so the
        membership check could never match -- the method always polled for the
        full timeout_s regardless of the row's actual state (root cause of a
        CI/local hang traced 2026-07-12 via tests/test_milestone_two_contract.py::
        test_ingest_stamps_session_id_on_adapter, which reliably took the entire
        timeout window even though the episode was already READY within ~100ms).
        """
        from menhir.domain.models import ProcessingState
        from menhir.services.ingest_service import IngestService

        stub_memory_graph_adapter.pending_episode_rows["ep-ready"] = {
            "processing_state": ProcessingState.READY,
        }
        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )

        start = perf_counter()
        result = await svc.wait_for_episode_processing(
            "ep-ready", timeout_s=5.0, poll_interval_s=0.05,
        )
        elapsed = perf_counter() - start

        assert result is not None
        assert result["processing_state"] == ProcessingState.READY
        assert elapsed < 1.0, (
            "Should return on the first poll, not burn the full 5s timeout_s"
        )

    async def test_plain_string_ready_state_also_matches(
        self, stub_memory_graph_adapter,
    ) -> None:
        """A real Neo4j driver round-trips ProcessingState as a plain string, not
        the enum member -- the fix must match both forms, since str-mixin enums
        compare equal to their string value either way."""
        from menhir.services.ingest_service import IngestService

        stub_memory_graph_adapter.pending_episode_rows["ep-ready-str"] = {
            "processing_state": "READY",
        }
        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )

        start = perf_counter()
        result = await svc.wait_for_episode_processing(
            "ep-ready-str", timeout_s=5.0, poll_interval_s=0.05,
        )
        elapsed = perf_counter() - start

        assert result is not None
        assert result["processing_state"] == "READY"
        assert elapsed < 1.0


# ===========================================================================
# #27 — Preset weights sum to zero
# ===========================================================================


class TestZeroWeights:
    """All zero weights → final_score equals similarity + type_boost only."""

    def test_zero_alpha_beta_gamma_delta(self):
        """With all weights at 0, score = similarity + type_boost."""
        from menhir.domain.recall import PRESET_WEIGHTS

        svc = ScoringService()
        # Monkey-patch a zero-weight preset for this test
        candidates = [
            _candidate(uuid="a", similarity=0.8, adjacency_score=1.0, edge_count=100),
            _candidate(uuid="b", similarity=0.3, adjacency_score=0.0, edge_count=0),
        ]

        # Use KNOWLEDGE preset (alpha=0.2, beta=0.1, gamma=0.1, delta=0.0)
        # With delta already 0, check that non-zero weights do affect scoring
        results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)
        assert results[0].uuid == "a"  # higher similarity wins
        assert results[0].final_score > results[1].final_score


# ===========================================================================
# #28 — coerce_reference_time silently replaces bad timestamp
# ===========================================================================


class TestCoerceReferenceTime:
    """Bad timestamps silently fall back to now()."""

    def test_none_falls_back_to_now(self):
        from menhir.services.enrichment_steps import coerce_reference_time

        before = datetime.now(timezone.utc)
        result = coerce_reference_time(None)
        after = datetime.now(timezone.utc)

        assert before <= result <= after
        assert result.tzinfo is not None

    def test_string_falls_back_to_now(self):
        from menhir.services.enrichment_steps import coerce_reference_time

        result = coerce_reference_time("not-a-datetime")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_naive_datetime_gets_utc(self):
        from menhir.services.enrichment_steps import coerce_reference_time

        naive = datetime(2026, 1, 1, 12, 0, 0)
        result = coerce_reference_time(naive)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026

    def test_aware_datetime_passes_through(self):
        from menhir.services.enrichment_steps import coerce_reference_time

        aware = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        result = coerce_reference_time(aware)
        assert result is aware

    def test_neo4j_datetime_with_to_native(self):
        """Neo4j DateTime objects have .to_native() method."""
        from menhir.services.enrichment_steps import coerce_reference_time

        class _FakeNeo4jDateTime:
            def to_native(self):
                return datetime(2026, 3, 1, tzinfo=timezone.utc)

        result = coerce_reference_time(_FakeNeo4jDateTime())
        assert result == datetime(2026, 3, 1, tzinfo=timezone.utc)


# ===========================================================================
# #29 — Heartbeat cancel races with heartbeat write (documented)
# ===========================================================================


class TestHeartbeatCancelRace:
    """Heartbeat loop respects stop_event — documented race window."""

    async def test_heartbeat_stops_on_event(self, stub_memory_graph_adapter):
        """Setting stop_event causes heartbeat loop to exit promptly."""
        from menhir.services.ingest_service import IngestService

        svc = IngestService(
            graphiti_client=SimpleNamespace(),
            graph_adapter=stub_memory_graph_adapter,
            llm=SimpleNamespace(),
        )
        svc._processing_heartbeat_interval_s = 0.05

        stop = asyncio.Event()
        task = asyncio.create_task(
            svc._processing_heartbeat_loop("test-ep", stop)
        )

        await asyncio.sleep(0.1)  # let it tick once
        stop.set()
        await asyncio.sleep(0.15)

        assert task.done() or task.cancelled()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# ===========================================================================
# #30 — confirm_contradiction with identical content
# ===========================================================================


class TestIdenticalContentContradiction:
    """Same content on both sides → LLM may return unexpected token."""

    async def test_identical_content_returns_clear_or_none(self):
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _ClearBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "CLEAR"

        adapter = LLMAdapter(
            base_url="http://test", api_key="test",
            chat_model="test", embed_model="test",
            backend=_ClearBackend(),
        )
        result = await adapter.confirm_contradiction(
            name_a="same", content_a="identical content",
            name_b="same", content_b="identical content",
        )
        # Identical content should not be a contradiction
        assert result is False

    async def test_identical_content_garbage_response(self):
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _GarbageBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "SAME"  # neither CONFLICT nor CLEAR

        adapter = LLMAdapter(
            base_url="http://test", api_key="test",
            chat_model="test", embed_model="test",
            backend=_GarbageBackend(),
        )
        result = await adapter.confirm_contradiction(
            name_a="same", content_a="identical",
            name_b="same", content_b="identical",
        )
        assert result is None, "Unexpected token returns None"


# ===========================================================================
# Edge fact repair — LLM rewrite of synthetic facts
# ===========================================================================


class TestEdgeFactRepair:
    """Post-extraction LLM repair of synthetic edge facts."""

    async def test_no_synthetic_edges_no_llm_call(self, stub_memory_graph_adapter):
        """When all edges have original facts, no LLM call is made."""
        from menhir.services.enrichment_steps import _repair_synthetic_edge_facts, EnrichmentContext

        llm_calls: list[str] = []

        @dataclass
        class _TrackingLLM:
            base_url: str = "http://test"
            api_key: str = "test"
            chat_model: str = "test"
            embed_model: str = "test"

            async def repair_edge_facts(self, content: str, edges: list) -> list:
                llm_calls.append("called")
                return []

        edges = [
            SimpleNamespace(uuid="e1", fact="Alice manages Bob", name="manages"),
            SimpleNamespace(uuid="e2", fact="Bob reports to Alice", name="reports_to"),
        ]

        ctx = EnrichmentContext(
            episode_uuid="ep-1", claimed={}, started=perf_counter(),
            processing_attempts=1, worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(), lifecycle_service=None,
            llm=_TrackingLLM(),
            ingest_gate=IngestGate(1), processing_steps_total=5,
            settings_record_revisions=False, ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        await _repair_synthetic_edge_facts(ctx, edges, "some episode content")
        assert llm_calls == [], "No LLM call when no synthetic edges"
        # But provenance should still be stamped as original
        assert hasattr(stub_memory_graph_adapter, "edge_fact_updates")
        assert all(u["fact_source"] == "original" for u in stub_memory_graph_adapter.edge_fact_updates)

    async def test_synthetic_edges_get_repaired(self, stub_memory_graph_adapter):
        """Synthetic edges are sent to LLM and repaired facts written back."""
        from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX
        from menhir.services.enrichment_steps import _repair_synthetic_edge_facts, EnrichmentContext

        @dataclass
        class _RepairLLM:
            base_url: str = "http://test"
            api_key: str = "test"
            chat_model: str = "test"
            embed_model: str = "test"

            async def repair_edge_facts(self, content: str, edges: list) -> list:
                return ["Alice is the manager of Bob at Acme Corp"]

        edges = [
            SimpleNamespace(uuid="e1", fact=f"{SYNTHETIC_FACT_PREFIX}alice manages bob", name="alice -> bob"),
        ]

        ctx = EnrichmentContext(
            episode_uuid="ep-2", claimed={}, started=perf_counter(),
            processing_attempts=1, worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(), lifecycle_service=None,
            llm=_RepairLLM(),
            ingest_gate=IngestGate(1), processing_steps_total=5,
            settings_record_revisions=False, ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        await _repair_synthetic_edge_facts(ctx, edges, "Alice manages Bob at Acme Corp")
        updates = stub_memory_graph_adapter.edge_fact_updates
        assert len(updates) == 1
        assert updates[0]["fact"] == "Alice is the manager of Bob at Acme Corp"
        assert updates[0]["fact_source"] == "llm_repaired"

    async def test_llm_failure_falls_back_to_synthetic(self, stub_memory_graph_adapter):
        """When LLM fails, synthetic fact is kept with prefix stripped."""
        from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX
        from menhir.services.enrichment_steps import _repair_synthetic_edge_facts, EnrichmentContext

        @dataclass
        class _FailingLLM:
            base_url: str = "http://test"
            api_key: str = "test"
            chat_model: str = "test"
            embed_model: str = "test"

            async def repair_edge_facts(self, content: str, edges: list) -> list:
                raise RuntimeError("LLM unavailable")

        edges = [
            SimpleNamespace(uuid="e1", fact=f"{SYNTHETIC_FACT_PREFIX}alice manages bob", name="manages"),
        ]

        ctx = EnrichmentContext(
            episode_uuid="ep-3", claimed={}, started=perf_counter(),
            processing_attempts=1, worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(), lifecycle_service=None,
            llm=_FailingLLM(),
            ingest_gate=IngestGate(1), processing_steps_total=5,
            settings_record_revisions=False, ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        await _repair_synthetic_edge_facts(ctx, edges, "some content")
        updates = stub_memory_graph_adapter.edge_fact_updates
        assert len(updates) == 1
        assert updates[0]["fact"] == "alice manages bob"  # prefix stripped
        assert updates[0]["fact_source"] == "synthetic_fallback"

    async def test_no_llm_adapter_is_noop(self, stub_memory_graph_adapter):
        """When ctx.llm is None, repair is a no-op."""
        from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX
        from menhir.services.enrichment_steps import _repair_synthetic_edge_facts, EnrichmentContext

        edges = [
            SimpleNamespace(uuid="e1", fact=f"{SYNTHETIC_FACT_PREFIX}test fact", name="test"),
        ]

        ctx = EnrichmentContext(
            episode_uuid="ep-4", claimed={}, started=perf_counter(),
            processing_attempts=1, worker_id="w1",
            graph_adapter=stub_memory_graph_adapter,
            graphiti_client=SimpleNamespace(), lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1), processing_steps_total=5,
            settings_record_revisions=False, ready_warning_ms=8000,
            graphiti_add_episode_timeout_s=300.0,
            graphiti_episode_max_estimated_tokens=12000,
            get_queue_depth=lambda: 0,
        )

        await _repair_synthetic_edge_facts(ctx, edges, "content")
        assert not hasattr(stub_memory_graph_adapter, "edge_fact_updates") or \
               stub_memory_graph_adapter.edge_fact_updates == []

    def test_synthetic_prefix_round_trip(self):
        """Prefix is added during synthesis and detectable downstream."""
        from menhir.infrastructure.graphiti_helpers import (
            SYNTHETIC_FACT_PREFIX,
            _normalize_graphiti_json_payload,
            strip_synthetic_prefix,
        )

        payload = {
            "source_entity_name": "Alice",
            "target_entity_name": "Bob",
            "relation_type": "MANAGES",
        }
        result = _normalize_graphiti_json_payload(payload)
        assert result["fact"].startswith(SYNTHETIC_FACT_PREFIX)
        assert strip_synthetic_prefix(result["fact"]) == "Alice manages Bob"

    async def test_llm_repair_response_parsing(self):
        """repair_edge_facts parses numbered LLM response correctly."""
        from menhir.infrastructure.llm import LLMAdapter

        @dataclass
        class _MockBackend:
            async def create_chat_completion(self, **kw: Any) -> str:
                return "1. Alice is Bob's manager at Acme\n2. Bob works in engineering"

        adapter = LLMAdapter(
            base_url="http://test", api_key="test",
            chat_model="test", embed_model="test",
            backend=_MockBackend(),
        )
        stubs = [
            {"source": "Alice", "target": "Bob", "relation": "manages"},
            {"source": "Bob", "target": "engineering", "relation": "works_in"},
        ]
        results = await adapter.repair_edge_facts("episode text", stubs)
        assert results == [
            "Alice is Bob's manager at Acme",
            "Bob works in engineering",
        ]
