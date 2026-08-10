"""M7 Phase 3 — Conflict lifecycle regression scenarios.

End-to-end scenarios for the conflict detection → LLM review → resolution
pipeline, exercising the full conflict lifecycle through service-layer methods.

Each scenario uses the existing StubMemoryGraphAdapter and StubGraphitiClient
from conftest.py with the newly added conflict group tracking methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.recall import CandidateData, QueryPreset, PRESET_WEIGHTS
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import LifecycleService, ScoringService
from menhir.services.lifecycle_service import SIMILARITY_CONFLICT_THRESHOLD

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Controllable fake LLM for conflict confirmation tests."""

    confirm_result: bool | None = True
    confirm_calls: list[dict[str, str]] = field(default_factory=list)
    compress_return: str | None = "compressed"
    merge_return: str | None = "merged"

    async def confirm_contradiction(
        self,
        *,
        name_a: str,
        content_a: str,
        name_b: str,
        content_b: str,
    ) -> bool | None:
        self.confirm_calls.append({
            "name_a": name_a,
            "content_a": content_a,
            "name_b": name_b,
            "content_b": content_b,
        })
        return self.confirm_result

    async def compress_content(self, content: str) -> str | None:
        return self.compress_return

    async def merge_content(self, existing: str, new: str) -> str | None:
        return self.merge_return


def _make_lifecycle_service(stub_adapter, stub_graphiti, llm=None):
    """Build a LifecycleService wired to stub adapters."""
    return LifecycleService(
        graph_adapter=stub_adapter,
        graphiti_client=stub_graphiti,
        llm=llm,
        pending_actions=PendingActionStore(),
    )


def _seed_conflict_group(
    stub_adapter,
    group_id: str,
    members: list[dict[str, str]],
    *,
    status: str = "pending_llm_review",
    created_at: str | datetime | None = None,
):
    """Seed a conflict group directly into the stub's conflict_groups state."""
    if created_at is None:
        created_at = "2026-03-09T00:00:00+00:00"
    stub_adapter.conflict_groups[group_id] = {
        "group_id": group_id,
        "created_at": created_at,
        "status": status,
        "members": [
            {
                "uuid": m["uuid"],
                "name": m.get("name", m["uuid"]),
                "content": m.get("content", ""),
                "status": status,
                "scope": m.get("scope", "PERSISTENT"),
                "node_created_at": m.get("node_created_at", "2026-03-01T00:00:00+00:00"),
            }
            for m in members
        ],
    }


# ===========================================================================
# Detection and Review
# ===========================================================================


class TestSimilarityScanCreatesPending:
    """Two similar PERSISTENT nodes → consolidation detects conflict → pending_llm_review."""

    async def test_consolidation_detects_high_similarity_conflict(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        # Arrange: SESSION entity with content, search_scored returns high-similarity match
        stub_memory_graph_adapter.session_entities = [
            {
                "uuid": "node-A",
                "name": "user prefers dark mode",
                "content": "The user always uses dark mode in editors",
                "user_flagged": False,
            },
        ]
        # Edge count below threshold → falls through to sharpness check
        stub_memory_graph_adapter.persistent_edge_counts["node-A"] = 0
        # search_scored returns a high-similarity hit (above 0.85 threshold)
        # First call: sharpness computation (similarity check)
        # Second call: contradiction check (_check_contradictions_batch)
        stub_graphiti_client.search_scored_results = [
            ("existing-persistent-1", "user prefers light mode", SIMILARITY_CONFLICT_THRESHOLD + 0.05),
        ]

        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client)

        # Act
        result = await svc.consolidate_session("test-session")

        # Assert: conflict detected
        assert result.conflicts_detected >= 1
        assert len(stub_memory_graph_adapter.conflict_calls) >= 1
        call = stub_memory_graph_adapter.conflict_calls[-1]
        assert call["initial_status"] == "pending_llm_review"
        assert call["uuid_a"] == "node-A"
        assert call["uuid_b"] == "existing-persistent-1"


class TestLLMConfirmsContradiction:
    """pending_llm_review group → LLM confirms → status=unresolved."""

    async def test_llm_confirms_sets_unresolved(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        # Arrange: seed a pending_llm_review group with two members
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "dark mode pref", "content": "user likes dark mode"},
                {"uuid": "node-B", "name": "light mode pref", "content": "user likes light mode"},
            ],
            status="pending_llm_review",
        )
        llm = _FakeLLM(confirm_result=True)
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client, llm=llm)

        # Act
        result = await svc.confirm_pending_conflicts()

        # Assert
        assert result["confirmed"] == 1
        assert result["cleared"] == 0
        assert result["errors"] == 0
        assert llm.confirm_calls, "LLM should have been called"
        group = stub_memory_graph_adapter.conflict_groups["group-1"]
        assert group["status"] == "unresolved"


class TestLLMClearsFalsePositive:
    """pending_llm_review group → LLM says no contradiction → false_positive."""

    async def test_llm_clears_as_false_positive(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "Python 3.12", "content": "project uses Python 3.12"},
                {"uuid": "node-B", "name": "Python version", "content": "Python 3.12 required"},
            ],
            status="pending_llm_review",
        )
        llm = _FakeLLM(confirm_result=False)
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client, llm=llm)

        # Act
        result = await svc.confirm_pending_conflicts()

        # Assert
        assert result["confirmed"] == 0
        assert result["cleared"] == 1
        assert len(stub_memory_graph_adapter.resolve_conflict_calls) == 1
        resolve_call = stub_memory_graph_adapter.resolve_conflict_calls[0]
        assert resolve_call["action"] == "keep_both"
        assert resolve_call["resolution_status"] == "false_positive"


class TestLLMFailureLeavePending:
    """LLM returns None → group stays in pending_llm_review for retry."""

    async def test_llm_none_leaves_pending(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "fact-A", "content": "A"},
                {"uuid": "node-B", "name": "fact-B", "content": "B"},
            ],
            status="pending_llm_review",
        )
        llm = _FakeLLM(confirm_result=None)  # LLM failed
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client, llm=llm)

        result = await svc.confirm_pending_conflicts()

        assert result["errors"] == 1
        assert result["confirmed"] == 0
        assert result["cleared"] == 0
        # Group status unchanged — still pending_llm_review
        group = stub_memory_graph_adapter.conflict_groups["group-1"]
        assert group["status"] == "pending_llm_review"


class TestSingleMemberGroupCleared:
    """Spurious single-member group → cleared without LLM call."""

    async def test_single_member_auto_cleared(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-spurious",
            [{"uuid": "lonely-node", "name": "only member", "content": "alone"}],
            status="pending_llm_review",
        )
        llm = _FakeLLM(confirm_result=True)
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client, llm=llm)

        result = await svc.confirm_pending_conflicts()

        assert result["cleared"] == 1
        assert result["confirmed"] == 0
        assert not llm.confirm_calls, "LLM should NOT be called for single-member groups"


class TestRequeueForReview:
    """unresolved group → requeue → pending_llm_review."""

    def test_requeue_reverts_status(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
        )

        count = stub_memory_graph_adapter.requeue_conflicts_for_llm_review(from_status="unresolved")

        assert count == 1
        group = stub_memory_graph_adapter.conflict_groups["group-1"]
        assert group["status"] == "pending_llm_review"
        for member in group["members"]:
            assert member["status"] == "pending_llm_review"


# ===========================================================================
# Resolution Actions
# ===========================================================================


class TestResolveKeepBoth:
    """unresolved group → resolve(action=keep_both) → resolved."""

    def test_keep_both_resolves_group(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "fact A"},
                {"uuid": "node-B", "name": "fact B"},
            ],
            status="unresolved",
        )

        result = stub_memory_graph_adapter.resolve_conflict_group(
            "group-1", "keep_both", resolution_status="resolved"
        )

        assert result["action"] == "keep_both"
        assert result["resolved"] == 2
        assert result["removed_uuids"] == []
        assert result["bridged_edges"] == 0
        group = stub_memory_graph_adapter.conflict_groups["group-1"]
        assert group["status"] == "resolved"


class TestResolveReplace:
    """unresolved group → resolve(action=replace, keep_uuid=A) → B removed."""

    def test_replace_removes_non_keep(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "canonical fact", "content": "correct"},
                {"uuid": "node-B", "name": "outdated fact", "content": "wrong"},
            ],
            status="unresolved",
        )

        result = stub_memory_graph_adapter.resolve_conflict_group(
            "group-1",
            "replace",
            keep_uuid="node-A",
            resolution_status="resolved",
        )

        assert result["action"] == "replace"
        assert result["removed_uuid"] == "node-B"
        assert result["removed_uuids"] == ["node-B"]
        assert result["bridged_edges"] >= 1
        assert result["resolved"] == 1  # node-A survives


class TestResolveDiscardNew:
    """unresolved group → resolve(action=discard_new, keep_uuid=older) → newer removed."""

    def test_discard_new_removes_newer(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {
                    "uuid": "old-node",
                    "name": "original",
                    "content": "original fact",
                    "node_created_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "uuid": "new-node",
                    "name": "duplicate",
                    "content": "duplicate fact",
                    "node_created_at": "2026-03-01T00:00:00+00:00",
                },
            ],
            status="unresolved",
        )

        result = stub_memory_graph_adapter.resolve_conflict_group(
            "group-1",
            "discard_new",
            keep_uuid="old-node",
            resolution_status="resolved",
        )

        assert result["action"] == "discard_new"
        assert "new-node" in result["removed_uuids"]
        assert result["resolved"] == 1  # old-node survives


class TestPromotedNodeProtection:
    """PROMOTED node in remove set → default rejects, explicit flag allows."""

    def test_default_rejects_promoted_removal(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-promoted",
            [
                {"uuid": "keep-node", "name": "keeper", "scope": "PERSISTENT"},
                {"uuid": "promoted-node", "name": "promoted", "scope": "PROMOTED"},
            ],
            status="unresolved",
        )

        with pytest.raises(ValueError, match="Refusing to GONE PROMOTED"):
            stub_memory_graph_adapter.resolve_conflict_group(
                "group-promoted",
                "replace",
                keep_uuid="keep-node",
            )

    def test_explicit_flag_allows_promoted_removal(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-promoted",
            [
                {"uuid": "keep-node", "name": "keeper", "scope": "PERSISTENT"},
                {"uuid": "promoted-node", "name": "promoted", "scope": "PROMOTED"},
            ],
            status="unresolved",
        )

        result = stub_memory_graph_adapter.resolve_conflict_group(
            "group-promoted",
            "replace",
            keep_uuid="keep-node",
            allow_promoted_removal=True,
        )

        assert result["removed_uuids"] == ["promoted-node"]


# ===========================================================================
# Auto-Resolve and Scoring
# ===========================================================================


class TestAutoResolveAfterTimeout:
    """unresolved group older than 14 days → auto_resolve_stale_conflicts → auto-resolved."""

    async def test_stale_unresolved_auto_resolves(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        old_date = datetime.now(timezone.utc) - timedelta(days=20)
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "old-group",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
            created_at=old_date,
        )
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client)

        resolved = svc.auto_resolve_stale_conflicts(max_age_days=14)

        assert resolved == 1
        assert len(stub_memory_graph_adapter.resolve_conflict_calls) == 1
        call = stub_memory_graph_adapter.resolve_conflict_calls[0]
        assert call["action"] == "keep_both"
        assert call["resolution_status"] == "auto-resolved"

    async def test_recent_unresolved_not_auto_resolved(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        recent_date = datetime.now(timezone.utc) - timedelta(days=5)
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "recent-group",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="unresolved",
            created_at=recent_date,
        )
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client)

        resolved = svc.auto_resolve_stale_conflicts(max_age_days=14)

        assert resolved == 0


class TestConflictScoringSignal:
    """CONFLICT preset boosts unresolved conflict nodes via delta=0.4."""

    def test_conflict_preset_gives_higher_score(self):
        base = CandidateData(
            uuid="conflict-node",
            name="conflicting fact",
            content="some content",
            scope="PERSISTENT",
            memory_type="SEMANTIC",
            similarity=0.8,
            last_accessed_days_ago=1.0,
            edge_count=2,
            adjacency_score=0.3,
            freshness="ACTIVE",
            has_conflict=True,
            conflict_status="unresolved",
        )
        no_conflict = CandidateData(
            uuid="clean-node",
            name="clean fact",
            content="other content",
            scope="PERSISTENT",
            memory_type="SEMANTIC",
            similarity=0.8,
            last_accessed_days_ago=1.0,
            edge_count=2,
            adjacency_score=0.3,
            freshness="ACTIVE",
            has_conflict=False,
            conflict_status=None,
        )
        scorer = ScoringService()

        # Score with CONFLICT preset (delta=0.4)
        conflict_results = scorer.score_candidates([base, no_conflict], QueryPreset.CONFLICT)
        conflict_scored = {s.uuid: s for s in conflict_results}

        # Score with KNOWLEDGE preset (delta=0.0)
        knowledge_results = scorer.score_candidates([base, no_conflict], QueryPreset.KNOWLEDGE)
        knowledge_scored = {s.uuid: s for s in knowledge_results}

        # CONFLICT preset should give higher score to unresolved conflict node
        conflict_delta = (
            conflict_scored["conflict-node"].final_score
            - conflict_scored["clean-node"].final_score
        )
        knowledge_delta = (
            knowledge_scored["conflict-node"].final_score
            - knowledge_scored["clean-node"].final_score
        )

        # With CONFLICT preset (delta=0.4), the unresolved node gets +0.4*1.0 = +0.4 bonus
        assert conflict_delta > knowledge_delta
        assert conflict_delta == pytest.approx(
            PRESET_WEIGHTS[QueryPreset.CONFLICT][3] * 1.0, abs=0.01
        )
        # KNOWLEDGE preset delta=0.0 → no conflict bonus
        assert knowledge_delta == pytest.approx(0.0, abs=0.01)

    def test_resolved_conflict_gets_partial_boost(self):
        """has_conflict=True + resolved status → 0.5 signal (not 1.0)."""
        resolved = CandidateData(
            uuid="resolved-node",
            name="resolved",
            content="content",
            scope="PERSISTENT",
            memory_type="SEMANTIC",
            similarity=0.8,
            last_accessed_days_ago=1.0,
            edge_count=2,
            adjacency_score=0.3,
            freshness="ACTIVE",
            has_conflict=True,
            conflict_status="resolved",
        )
        scorer = ScoringService()
        results = scorer.score_candidates([resolved], QueryPreset.CONFLICT)

        breakdown = results[0].breakdown
        assert breakdown.conflict_bonus == 0.5


class TestMultiNodeConflictCluster:
    """3-node cluster → resolve(replace, keep_uuid=B) → A and C removed."""

    def test_three_node_cluster_resolution(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "cluster-group",
            [
                {"uuid": "node-A", "name": "variant A", "content": "fact variant A"},
                {"uuid": "node-B", "name": "canonical", "content": "canonical fact"},
                {"uuid": "node-C", "name": "variant C", "content": "fact variant C"},
            ],
            status="unresolved",
        )

        result = stub_memory_graph_adapter.resolve_conflict_group(
            "cluster-group",
            "replace",
            keep_uuid="node-B",
            resolution_status="resolved",
        )

        assert result["action"] == "replace"
        assert sorted(result["removed_uuids"]) == ["node-A", "node-C"]
        assert result["resolved"] == 1  # only node-B survives
        assert result["bridged_edges"] == 2  # one bridge per removed node


# ===========================================================================
# No-LLM fallback
# ===========================================================================


class TestConfirmPendingNoLLM:
    """confirm_pending_conflicts with no LLM returns skipped_no_llm."""

    async def test_no_llm_skips(
        self,
        stub_memory_graph_adapter,
        stub_graphiti_client,
    ):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [
                {"uuid": "node-A", "name": "A"},
                {"uuid": "node-B", "name": "B"},
            ],
            status="pending_llm_review",
        )
        svc = _make_lifecycle_service(stub_memory_graph_adapter, stub_graphiti_client, llm=None)

        result = await svc.confirm_pending_conflicts()

        assert result["skipped_no_llm"] == 1
        assert result["confirmed"] == 0
        assert result["cleared"] == 0


# ===========================================================================
# Edge cases
# ===========================================================================


class TestResolveInvalidAction:
    """Invalid action raises ValueError."""

    def test_invalid_action_raises(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [{"uuid": "A"}, {"uuid": "B"}],
            status="unresolved",
        )

        with pytest.raises(ValueError, match="Invalid action"):
            stub_memory_graph_adapter.resolve_conflict_group("group-1", "merge_all")


class TestResolveKeepUuidNotInGroup:
    """keep_uuid not in group raises ValueError."""

    def test_missing_keep_uuid_raises(self, stub_memory_graph_adapter):
        _seed_conflict_group(
            stub_memory_graph_adapter,
            "group-1",
            [{"uuid": "A"}, {"uuid": "B"}],
            status="unresolved",
        )

        with pytest.raises(ValueError, match="not a member"):
            stub_memory_graph_adapter.resolve_conflict_group(
                "group-1", "replace", keep_uuid="not-a-member"
            )
