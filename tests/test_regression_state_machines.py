"""M7 Phase 2 — State machine regression scenarios.

Multi-step scenario tests that exercise full state machine paths through
the episode processing queue, memory lifecycle, and scope transitions.

Each scenario exercises at least 2 state transitions using the existing
StubMemoryGraphAdapter and StubGraphitiClient from conftest.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from menhir.domain import IngestStatus, new_session
from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.recall import CandidateData, QueryPreset, PRESET_WEIGHTS, ScoredMemory
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import IngestService, LifecycleService, RecallService, ScoringService
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.lifecycle_service import (
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SHARPNESS_PROMOTE_THRESHOLD,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Controllable fake LLM for consolidation/decay tests."""

    compress_return: str | None = "LLM compressed summary"
    merge_return: str | None = "LLM merged summary"
    merge_calls: list[tuple[str, str]] = field(default_factory=list)
    similar_count: int = 0

    async def compress_content(self, content: str) -> str | None:
        return self.compress_return

    async def merge_content(self, existing_content: str, new_context: str) -> str | None:
        self.merge_calls.append((existing_content, new_context))
        return self.merge_return

    async def check_contradiction(self, a: str, b: str) -> bool:
        return False


def _make_entity(
    uuid: str,
    *,
    flagged: bool = False,
    name: str = "",
    content: str = "",
    scope: str = "SESSION",
    freshness: str = "ACTIVE",
    sharpness: float = 0.0,
    edge_count: int = 0,
    rehydration_count: int = 0,
    has_conflict: bool = False,
    conflict_status: str | None = None,
) -> dict:
    return {
        "uuid": uuid,
        "name": name or f"entity-{uuid}",
        "content": content or f"content for {uuid}",
        "summary": None,
        "type": "SEMANTIC",
        "scope": scope,
        "freshness": freshness,
        "sharpness": sharpness,
        "user_flagged": flagged,
        "session_id": "session-1",
        "created_at": "2026-03-01T00:00:00Z",
        "last_accessed": "2026-03-01T00:00:00Z",
        "edge_count": edge_count,
        "rehydration_count": rehydration_count,
        "has_conflict": has_conflict,
        "conflict_status": conflict_status,
    }


# ===================================================================
# Episode Processing Lifecycle
# ===================================================================


class TestEpisodeHappyPath:
    """PENDING → claim → ENRICHING → graphiti → stamp → finalize → READY"""

    @pytest.mark.asyncio
    async def test_happy_path_ingest(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
        stub_llm_adapter,
    ):
        # Queue-backed happy path: PENDING -> claim -> extract -> stamp -> READY.
        # (Direct ingest now delegates to the worker, so the terminal state is asserted
        # at the worker layer where extraction + stamping happen.)
        adapter = stub_memory_graph_adapter
        service = IngestService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            llm=stub_llm_adapter,
        )
        episode_uuid = adapter.create_pending_episode(
            episode_uuid="regression-happy",
            name="episode-regression-session-1",
            content="user prefers dark mode",
            session_id="regression-session-1",
            user_id="test-user",
            source="test",
            source_confidence=0.9,
        )

        await service._process_pending_episode(episode_uuid)

        assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
        assert len(stub_graphiti_client.add_episode_calls) == 1
        assert len(adapter.stamp_calls) == 1


class TestRetryAfterFailure:
    """PENDING → claim → fail → FAILED → retry → PENDING → claim → succeed → READY"""

    @pytest.mark.asyncio
    async def test_retry_after_failure(self, stub_memory_graph_adapter, stub_graphiti_client):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-retry-001"

        # Step 1: Create pending episode
        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="retry test",
            content="test content",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.PENDING

        # Step 2: Claim (attempt 1)
        claimed = adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="w1", lease_seconds=60,
        )
        assert claimed is not None
        assert claimed["processing_state"] == ProcessingState.ENRICHING
        assert claimed["processing_attempts"] == 1

        # Step 3: Fail
        adapter.mark_episode_failed(ep_id, "timeout error", worker_id="w1")
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.FAILED

        # Step 4: Claim again (attempt 2 — FAILED episodes are claimable)
        claimed2 = adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="w2", lease_seconds=60,
        )
        assert claimed2 is not None
        assert claimed2["processing_state"] == ProcessingState.ENRICHING
        assert claimed2["processing_attempts"] == 2

        # Step 5: Succeed
        adapter.mark_episode_ready(
            ep_id,
            worker_id="w2",
            resolved_episode_uuid="resolved-001",
            nodes_touched=3,
            edges_touched=2,
        )
        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.READY
        assert row["processing_attempts"] == 2


class TestExhaustedRetries:
    """Fail 3 times → attempts exhausted → no further claims possible."""

    def test_exhausted_retries(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-exhaust-001"
        max_attempts = 3

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="exhaust test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )

        # Cycle through 3 claim-fail rounds
        for i in range(max_attempts):
            claimed = adapter.claim_pending_episode(
                ep_id, max_attempts=max_attempts, worker_id=f"w{i}", lease_seconds=60,
            )
            assert claimed is not None, f"Should be claimable on attempt {i + 1}"
            adapter.mark_episode_failed(ep_id, f"error-{i}", worker_id=f"w{i}")

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.FAILED
        assert row["processing_attempts"] == 3

        # Step 4: Should NOT be claimable
        blocked = adapter.claim_pending_episode(
            ep_id, max_attempts=max_attempts, worker_id="w99", lease_seconds=60,
        )
        assert blocked is None


class TestStaleLease:
    """ENRICHING with expired lease → reset to PENDING (attempts < max)."""

    def test_stale_lease_recovery(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-stale-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="stale test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        # Claim it
        adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="w1", lease_seconds=60,
        )
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.ENRICHING

        # Simulate stale lease reset (worker died)
        reset_count = adapter.reset_stale_enriching_episodes(max_attempts=3)
        assert reset_count == 1

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.PENDING
        assert "lease_expired_reset" in str(row.get("processing_error") or "")

    def test_stale_lease_exhausted(self, stub_memory_graph_adapter):
        """ENRICHING at max attempts → reset to FAILED (not PENDING)."""
        adapter = stub_memory_graph_adapter
        ep_id = "ep-stale-exhausted-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="stale exhausted",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        # Burn through attempts
        for i in range(3):
            adapter.claim_pending_episode(
                ep_id, max_attempts=3, worker_id=f"w{i}", lease_seconds=60,
            )
            if i < 2:
                adapter.mark_episode_failed(ep_id, f"error-{i}", worker_id=f"w{i}")

        # Now at attempt 3, still ENRICHING
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.ENRICHING
        assert adapter.pending_episode_rows[ep_id]["processing_attempts"] == 3

        reset_count = adapter.reset_stale_enriching_episodes(max_attempts=3)
        assert reset_count == 1

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.FAILED
        assert "exhausted" in str(row.get("processing_error") or "")


class TestOrphanedEnriching:
    """ENRICHING + no processing_owner → reset to PENDING."""

    def test_orphaned_enriching_reset(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-orphan-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="orphan test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        # Force into ENRICHING with no owner (simulating orphaned state)
        adapter.pending_episode_rows[ep_id]["processing_state"] = ProcessingState.ENRICHING
        adapter.pending_episode_rows[ep_id]["processing_owner"] = None
        adapter.pending_episode_rows[ep_id]["processing_attempts"] = 1

        reset_count = adapter.reset_orphaned_enriching_episodes(max_attempts=3)
        assert reset_count == 1

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.PENDING
        assert "orphaned" in str(row.get("processing_error") or "")


class TestWorkerShutdown:
    """ENRICHING + matching owner → release to PENDING on shutdown."""

    def test_worker_shutdown_release(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-shutdown-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="shutdown test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="worker-A", lease_seconds=60,
        )
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.ENRICHING
        assert adapter.pending_episode_rows[ep_id]["processing_owner"] == "worker-A"

        released = adapter.release_worker_episode_leases("worker-A", max_attempts=3)
        assert released == 1

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.PENDING
        assert "worker_shutdown" in str(row.get("processing_error") or "")


class TestCircuitBreakerRequeue:
    """Circuit open → mark_episode_pending without incrementing attempts."""

    def test_circuit_breaker_requeue(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-circuit-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="circuit test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        # Claim it (increments attempts to 1)
        adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="w1", lease_seconds=60,
        )
        assert adapter.pending_episode_rows[ep_id]["processing_attempts"] == 1

        # mark_episode_pending resets to PENDING without incrementing attempts
        adapter.mark_episode_pending(ep_id, worker_id="w1")

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.PENDING
        # Attempts should still be 1 — mark_episode_pending does NOT increment
        assert row["processing_attempts"] == 1


class TestBudgetCapRequeue:
    """Budget exceeded → mark_episode_pending with retry_after_s."""

    def test_budget_cap_requeue(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-budget-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="budget test",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        adapter.claim_pending_episode(
            ep_id, max_attempts=3, worker_id="w1", lease_seconds=60,
        )
        assert adapter.pending_episode_rows[ep_id]["processing_state"] == ProcessingState.ENRICHING

        # Requeue with retry_after
        adapter.mark_episode_pending(ep_id, retry_after_s=30.0, worker_id="w1")

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.PENDING
        assert row["retry_after_s"] == 30.0
        assert row["processing_attempts"] == 1  # unchanged


class TestContextWindowRetryPath:
    """Context window errors exhaust at max_attempts via scheduler path.

    claim_pending_episode has Cypher allowing extended context_retry_attempts=6,
    but the scheduler retry sweep hard-stops at processing_attempts >= max_attempts.
    This test validates the actual runtime behavior.
    """

    def test_context_window_exhausted_at_max_attempts(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-ctx-001"
        max_attempts = 3

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="context window test",
            content="huge episode text",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )

        # Use an actual context window error marker from the codebase
        ctx_error = "exceeds the available context window n_ctx=8192"

        # 3 rounds of claim → fail with context window error
        for i in range(max_attempts):
            claimed = adapter.claim_pending_episode(
                ep_id,
                max_attempts=max_attempts,
                context_retry_attempts=6,
                worker_id=f"w{i}",
                lease_seconds=60,
            )
            assert claimed is not None, f"Should be claimable at attempt {i + 1}"
            adapter.mark_episode_failed(ep_id, ctx_error, worker_id=f"w{i}")

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.FAILED
        assert row["processing_attempts"] == 3

        # Verify the scheduler classification would be "terminal" for context window errors
        classification = classify_enrichment_failure(row["processing_error"])
        assert classification == "terminal"  # context window → terminal classification

        # But the CLAIM Cypher has extended logic — it CAN still claim FAILED
        # context_window episodes beyond max_attempts if attempts < context_cap.
        # This proves the claim-side extended budget exists even though
        # the scheduler retry sweep wouldn't invoke it.
        claimed_extended = adapter.claim_pending_episode(
            ep_id,
            max_attempts=max_attempts,
            context_retry_attempts=6,
            worker_id="w-extended",
            lease_seconds=60,
        )
        assert claimed_extended is not None, (
            "claim_pending_episode should allow FAILED context_window episodes "
            "beyond max_attempts when attempts < context_retry_attempts"
        )
        assert claimed_extended["processing_attempts"] == 4


class TestFailExhaustedPending:
    """Dead PENDING rows are cleaned up at startup."""

    def test_fail_exhausted_pending(self, stub_memory_graph_adapter):
        adapter = stub_memory_graph_adapter
        ep_id = "ep-dead-pending-001"

        adapter.create_pending_episode(
            episode_uuid=ep_id,
            name="dead pending",
            content="test",
            session_id="s1",
            user_id="u1",
            source="test",
            source_confidence=0.9,
        )
        # Manually set attempts beyond max while still PENDING (corruption scenario)
        adapter.pending_episode_rows[ep_id]["processing_attempts"] = 5

        failed = adapter.fail_exhausted_pending_episodes(max_attempts=3)
        assert failed == 1

        row = adapter.pending_episode_rows[ep_id]
        assert row["processing_state"] == ProcessingState.FAILED
        assert "exhausted" in str(row.get("processing_error") or "")


# ===================================================================
# Memory Scope Transitions
# ===================================================================


class TestSessionToPersistent:
    """SESSION entities with enough persistent edges → promoted."""

    @pytest.mark.asyncio
    async def test_session_to_persistent(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [_make_entity("a")]
        adapter.persistent_edge_counts = {"a": PERSISTENT_EDGE_PROMOTE_THRESHOLD}
        stub_graphiti_client.search_scored_results = []

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.consolidate_session("session-1")

        assert "a" in adapter.promoted_nodes
        assert result.promoted == 1
        assert result.deleted == 0

    @pytest.mark.asyncio
    async def test_persistent_edge_below_threshold_needs_sharpness(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        """Nodes below edge threshold fall through to sharpness check."""
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [_make_entity("a")]
        adapter.persistent_edge_counts = {"a": PERSISTENT_EDGE_PROMOTE_THRESHOLD - 1}
        # No similar nodes → sharpness=1.0 (unique) → promotes
        stub_graphiti_client.search_scored_results = []

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.consolidate_session("session-1")

        assert "a" in adapter.promoted_nodes
        assert result.promoted == 1


class TestSessionFlaggedPromote:
    """Flagged entities bypass sharpness/edge checks."""

    @pytest.mark.asyncio
    async def test_flagged_always_promoted(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [
            _make_entity("a", flagged=True),
        ]
        adapter.persistent_edge_counts = {"a": 0}  # no edges
        stub_graphiti_client.search_scored_results = []

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.consolidate_session("session-1")

        assert "a" in adapter.promoted_nodes
        assert result.promoted == 1


class TestSessionLowValueDelete:
    """Low-sharpness SESSION entities are demoted with TTL (F5: grace window before deletion)."""

    @pytest.mark.asyncio
    async def test_low_value_demoted_with_ttl(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        """Low-value nodes are retained THIS PASS but demoted with a grace TTL."""
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [_make_entity("a")]
        adapter.persistent_edge_counts = {"a": 0}
        # F2: Many similar neighbors via true cosine → low sharpness → below SHARPNESS_PROMOTE_THRESHOLD (0.5)
        stub_graphiti_client.count_similar_by_cosine_results = 4

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        # Verify the sharpness from 4 similar nodes is below threshold
        sharpness = LifecycleService.compute_sharpness(4)
        assert sharpness < SHARPNESS_PROMOTE_THRESHOLD

        result = await svc.consolidate_session("session-1")

        # F5: sharpness-based deletion now re-enabled via grace TTL. The node is
        # neither promoted nor deleted THIS PASS, but demoted with a 14-day TTL.
        # If nothing promotes it within 14 days, it is deleted as unkept.
        assert "a" not in adapter.deleted_session_nodes
        assert result.deleted == 0
        assert result.promoted == 0
        # F5: the node is demoted
        assert result.demoted == 1
        assert "a" in adapter.demoted_uuids
        # Verify set_demote_ttl was called
        assert len(adapter.set_demote_ttl_calls) == 1
        assert adapter.set_demote_ttl_calls[0]["node_uuids"] == ["a"]

    @pytest.mark.asyncio
    async def test_low_value_expired_ttl_deleted(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        """Demoted node whose TTL has expired is deleted with audit record."""
        adapter = stub_memory_graph_adapter
        # No nodes to promote or demote this pass
        adapter.session_entities = []
        # But there's an expired demoted node to delete
        adapter.ttl_expired_uuids = [
            {"uuid": "expired-a", "name": "expired node", "session_id": "session-1"}
        ]

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.consolidate_session("session-1")

        # The expired node is deleted
        assert "expired-a" in adapter.deleted_session_nodes
        assert result.deleted == 1
        assert result.demoted == 0


class TestPersistentNoDowngrade:
    """Conservative stamp must not downgrade PERSISTENT → SESSION."""

    @pytest.mark.asyncio
    async def test_persistent_stamp_preserved(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
        stub_llm_adapter,
    ):
        # The stub's stamp_ingest_metadata always returns the same PolicyStampResult.
        # The real adapter uses COALESCE to never downgrade scope.
        # We verify that ingest through the service layer calls stamp with the right params,
        # which is the contract that conservative stamping depends on.
        adapter = stub_memory_graph_adapter
        service = IngestService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            llm=stub_llm_adapter,
        )
        episode_uuid = adapter.create_pending_episode(
            episode_uuid="persistent-stamp",
            name="episode-session-1-persistent-stamp",
            content="test content",
            session_id="session-1",
            user_id="test-user",
            source="test",
            source_confidence=0.5,
        )

        await service._process_pending_episode(episode_uuid)

        assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
        assert len(adapter.stamp_calls) == 1
        stamp = adapter.stamp_calls[0]
        # Service always passes the session's source, session_id, user_id.
        # The real adapter applies COALESCE, but the contract is that
        # these values are always passed so the Cypher can do the right thing.
        assert stamp["session_id"] == "session-1"
        assert stamp["user_id"] == "test-user"
        assert stamp["source"] == "test"


# ===================================================================
# Freshness Lifecycle
# ===================================================================


class TestActiveToCompressed:
    """ACTIVE node past threshold with low edges → COMPRESSED."""

    @pytest.mark.asyncio
    async def test_decay_compresses(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        old_date = datetime.now(timezone.utc) - timedelta(days=45)
        adapter.decay_candidates_by_freshness = {
            FreshnessState.ACTIVE: [
                {
                    "uuid": "node-a",
                    "content": "long content that should be compressed to shorter text " * 10,
                    "original_content": "long content that should be compressed to shorter text " * 10,
                    "scope": "PERSISTENT",
                    "freshness": "ACTIVE",
                    "edge_count": 1,
                    "sharpness": 0.2,
                    "user_flagged": False,
                    "rehydration_count": 0,
                    "last_accessed": old_date,
                },
            ],
        }
        # F2: Many similar neighbors via true cosine so sharpness is recalculated below DECAY_COMPRESS_SHARPNESS (0.3)
        stub_graphiti_client.count_similar_by_cosine_results = 4

        llm = _FakeLLM(compress_return="short compressed text")
        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
            llm=llm,
        )
        result = await svc.apply_decay()

        assert result.compressed == 1
        assert len(adapter.compressed_nodes) == 1
        assert adapter.compressed_nodes[0]["uuid"] == "node-a"
        assert adapter.compressed_nodes[0]["summary"] == "short compressed text"


class TestCompressedToGone:
    """COMPRESSED → GONE is disarmed (2026-07-03 hotfix): eligible nodes park at COMPRESSED."""

    @pytest.mark.asyncio
    async def test_decay_does_not_delete_while_gone_disarmed(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        very_old_date = datetime.now(timezone.utc) - timedelta(days=100)
        adapter.decay_candidates_by_freshness = {
            FreshnessState.COMPRESSED: [
                {
                    "uuid": "node-b",
                    "content": "old compressed content",
                    "original_content": "original",
                    "scope": "PERSISTENT",
                    "freshness": "COMPRESSED",
                    "edge_count": 1,
                    "sharpness": 0.05,
                    "user_flagged": False,
                    "last_accessed": very_old_date,
                },
            ],
        }
        adapter.bridge_delete_results = {
            "node-b": {"edges_bridged": 2, "deleted": 1},
        }

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.apply_decay()

        # HOTFIX 2026-07-03: GONE transitions disarmed (sharpness is an RRF rank artifact) —
        # the eligible node is NOT deleted; it parks at COMPRESSED until sharpness is lawful.
        assert result.deleted == 0
        assert "node-b" not in adapter.deleted_decay_nodes


class TestRehydrationOnRecall:
    """COMPRESSED node matched by recall → rehydrate → ACTIVE."""

    @pytest.mark.asyncio
    async def test_rehydration(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
        stub_llm_adapter,
    ):
        adapter = stub_memory_graph_adapter
        node_uuid = "node-rehy"

        # Set up a COMPRESSED candidate that recall will find
        adapter.candidate_metadata = [
            _make_entity(
                node_uuid,
                freshness="COMPRESSED",
                scope="PERSISTENT",
                rehydration_count=0,
                content="compressed summary",
            ),
        ]
        adapter.node_freshness[node_uuid] = "COMPRESSED"

        stub_graphiti_client.search_scored_results = [
            (node_uuid, "compressed summary", 0.9),
        ]

        ingest_svc = IngestService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            llm=stub_llm_adapter,
        )
        scoring_svc = ScoringService()
        lifecycle_svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
            llm=_FakeLLM(merge_return="rehydrated content"),
        )
        recall_svc = RecallService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            scoring_service=scoring_svc,
            ingest_service=ingest_svc,
            lifecycle_service=lifecycle_svc,
        )

        result = await recall_svc.recall("test query")

        assert len(result.results) > 0
        # Rehydration is scheduled as a background asyncio task — let it run
        await asyncio.sleep(0)
        # Drain any pending background rehydration tasks
        for task in list(recall_svc._rehydration_tasks):
            await task

        assert len(adapter.rehydrated_nodes) == 1
        assert adapter.rehydrated_nodes[0]["uuid"] == node_uuid
        assert adapter.node_freshness[node_uuid] == FreshnessState.ACTIVE


class TestRehydrationBrake:
    """Node with rehydration_count >= 3 → stays COMPRESSED, no rehydration."""

    @pytest.mark.asyncio
    async def test_rehydration_brake(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
        stub_llm_adapter,
    ):
        adapter = stub_memory_graph_adapter
        node_uuid = "node-brake"

        adapter.candidate_metadata = [
            _make_entity(
                node_uuid,
                freshness="COMPRESSED",
                scope="PERSISTENT",
                rehydration_count=3,  # at the exempt threshold
                content="compressed summary",
            ),
        ]
        adapter.node_freshness[node_uuid] = "COMPRESSED"

        stub_graphiti_client.search_scored_results = [
            (node_uuid, "compressed summary", 0.9),
        ]

        ingest_svc = IngestService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            llm=stub_llm_adapter,
        )
        scoring_svc = ScoringService()
        lifecycle_svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
            llm=_FakeLLM(),
        )
        recall_svc = RecallService(
            graphiti_client=stub_graphiti_client,
            graph_adapter=adapter,
            scoring_service=scoring_svc,
            ingest_service=ingest_svc,
            lifecycle_service=lifecycle_svc,
        )

        result = await recall_svc.recall("test query")

        assert len(result.results) > 0
        # Node should NOT have been rehydrated
        assert len(adapter.rehydrated_nodes) == 0
        assert adapter.node_freshness[node_uuid] == FreshnessState.COMPRESSED


# ===================================================================
# SESSION-Node Orphan Recovery
# ===================================================================


class TestRecoverOrphansPromotesValuable:
    """Old SESSION entities with persistent edges → promoted via recover_orphans."""

    @pytest.mark.asyncio
    async def test_recover_orphans_promotes(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [
            _make_entity("orphan-a", scope=NodeScope.SESSION),
        ]
        adapter.persistent_edge_counts = {"orphan-a": PERSISTENT_EDGE_PROMOTE_THRESHOLD}
        stub_graphiti_client.search_scored_results = []

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.recover_orphans(max_age_hours=4.0)

        assert "orphan-a" in adapter.promoted_nodes
        assert result.promoted >= 1


class TestRecoverOrphansDeletesLowValue:
    """Old low-sharpness SESSION orphans are RETAINED (2026-07-03 hotfix: deletion disarmed)."""

    @pytest.mark.asyncio
    async def test_recover_orphans_retains_low_value(
        self, stub_memory_graph_adapter, stub_graphiti_client,
    ):
        adapter = stub_memory_graph_adapter
        adapter.session_entities = [
            _make_entity("orphan-b", scope=NodeScope.SESSION),
        ]
        adapter.persistent_edge_counts = {"orphan-b": 0}
        # Many similar → low sharpness → delete
        stub_graphiti_client.search_scored_results = [
            ("sim-1", "similar", 0.95),
            ("sim-2", "similar", 0.93),
            ("sim-3", "similar", 0.91),
            ("sim-4", "similar", 0.90),
        ]

        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=stub_graphiti_client,
        )
        result = await svc.recover_orphans(max_age_hours=4.0)

        # HOTFIX 2026-07-03: sharpness-based deletion disarmed — orphan recovery no longer
        # deletes low-sharpness session nodes; they stay SESSION.
        assert "orphan-b" not in adapter.deleted_session_nodes
        assert result.deleted == 0
