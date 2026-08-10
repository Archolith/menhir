"""Unit tests for LifecycleService consolidation logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dataclasses import dataclass, field
from pathlib import Path

from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.config import MemorySettings
from menhir.services.lifecycle_service import (
    ConsolidationResult,
    DecayResult,
    LifecycleService,
    SHARPNESS_PROMOTE_THRESHOLD,
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SIMILARITY_CONFLICT_THRESHOLD,
)


@dataclass
class _FakeLLM:
    """Controllable fake LLM for decay tests."""
    compress_return: str | None = "LLM summary"
    merge_return: str | None = "Merged summary"
    merge_calls: list[tuple[str, str]] = field(default_factory=list)
    confirm_same_entity_return: bool | None = True
    confirm_same_entity_calls: int = field(default=0, init=False)

    async def compress_content(self, content: str) -> str | None:
        return self.compress_return

    async def merge_content(self, existing_content: str, new_context: str) -> str | None:
        self.merge_calls.append((existing_content, new_context))
        return self.merge_return

    async def confirm_same_entity(
        self, *, name_a: str, content_a: str, name_b: str, content_b: str,
    ) -> bool | None:
        self.confirm_same_entity_calls += 1
        return self.confirm_same_entity_return


@dataclass
class _FakePendingActions:
    upsert_calls: list[dict[str, object]] = field(default_factory=list)
    complete_calls: list[str] = field(default_factory=list)

    def upsert(
        self,
        node_uuid: str,
        action: str,
        *,
        context: str | None = None,
        source_uuid: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        self.upsert_calls.append(
            {
                "node_uuid": node_uuid,
                "action": action,
                "context": context,
                "source_uuid": source_uuid,
                "failure_reason": failure_reason,
            }
        )

    def complete(self, node_uuid: str) -> bool:
        self.complete_calls.append(node_uuid)
        return True


pytestmark = pytest.mark.unit


def _make_entity(uuid: str, *, flagged: bool = False, name: str = "", content: str = "") -> dict:
    return {
        "uuid": uuid,
        "name": name or f"entity-{uuid}",
        "content": content or f"content for {uuid}",
        "summary": None,
        "type": "SEMANTIC",
        "sharpness": 0.0,
        "user_flagged": flagged,
        "session_id": "session-1",
        "created_at": "2026-03-01T00:00:00Z",
    }


# --- Sharpness computation ---

def test_sharpness_unique_node():
    assert LifecycleService.compute_sharpness(0) == 1.0


def test_sharpness_one_similar():
    assert LifecycleService.compute_sharpness(1) == 0.5


def test_sharpness_four_similar():
    assert LifecycleService.compute_sharpness(4) == pytest.approx(0.2)


def test_sharpness_negative_clamped():
    assert LifecycleService.compute_sharpness(-5) == 1.0


# --- Promotion logic ---

@pytest.mark.asyncio
async def test_flagged_nodes_always_promoted(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [
        _make_entity("a", flagged=True),
        _make_entity("b", flagged=False),
    ]
    # b has no edges, search returns nothing -> sharpness=1.0 (unique) -> promotes
    stub_graphiti_client.search_scored_results = []

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session("session-1")

    assert "a" in stub_memory_graph_adapter.promoted_nodes
    assert "b" in stub_memory_graph_adapter.promoted_nodes
    assert result.promoted == 2
    assert result.deleted == 0


@pytest.mark.asyncio
async def test_persistent_edge_threshold_promotes(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a")]
    stub_memory_graph_adapter.persistent_edge_counts = {"a": PERSISTENT_EDGE_PROMOTE_THRESHOLD}
    stub_graphiti_client.search_scored_results = []

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert "a" in stub_memory_graph_adapter.promoted_nodes
    assert result.promoted == 1


@pytest.mark.asyncio
async def test_low_sharpness_skips_deletion(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a")]
    # F2: Return many similar neighbors -> low sharpness (via count_similar_by_cosine)
    stub_graphiti_client.count_similar_by_cosine_results = 5

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    # HOTFIX 2026-07-03: sharpness-based deletion disabled (sharpness is an RRF rank artifact) —
    # the node is neither promoted nor deleted; it stays SESSION (bounded, repairable noise).
    assert "a" not in stub_memory_graph_adapter.deleted_session_nodes
    assert result.deleted == 0
    assert result.promoted == 0
    # Sharpness is still computed and cached: 1/(1+5) ≈ 0.167 (via true-cosine count, not RRF)
    assert stub_memory_graph_adapter.sharpness_updates["a"] == pytest.approx(1.0 / 6.0)


@pytest.mark.asyncio
async def test_unique_node_promoted_via_sharpness(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a")]
    # F2: No similar neighbors -> sharpness=1.0 (via count_similar_by_cosine)
    stub_graphiti_client.count_similar_by_cosine_results = 0

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert "a" in stub_memory_graph_adapter.promoted_nodes
    assert result.promoted == 1
    assert stub_memory_graph_adapter.sharpness_updates["a"] == 1.0


@pytest.mark.asyncio
async def test_similarity_search_failure_skips_node(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a")]

    # F2: Make count_similar_by_cosine (sharpness path) fail, simulating graphiti unavailable
    stub_graphiti_client.count_similar_by_cosine_exception = RuntimeError("graphiti unavailable")

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.promoted == 0
    assert result.deleted == 0
    assert "a" not in stub_memory_graph_adapter.promoted_nodes
    assert "a" not in stub_memory_graph_adapter.deleted_session_nodes
    assert "a" not in stub_memory_graph_adapter.sharpness_updates


@pytest.mark.asyncio
async def test_empty_session_no_ops(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = []

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.promoted == 0
    assert result.deleted == 0


# --- Contradiction detection ---

@pytest.mark.asyncio
async def test_high_similarity_flags_conflict(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    # Score 0.88 falls in the conflict range (0.85–0.95)
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.88),
    ]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.conflicts_detected == 1
    assert len(stub_memory_graph_adapter.conflict_calls) == 1
    call = stub_memory_graph_adapter.conflict_calls[0]
    assert call["uuid_a"] == "new-node"
    assert call["uuid_b"] == "existing-node"
    # Ordinary conflicts still default to pending_llm_review (auto-reviewable).
    assert call["initial_status"] == "pending_llm_review"
    # Node still gets promoted despite conflict
    assert "new-node" in stub_memory_graph_adapter.promoted_nodes


@pytest.mark.asyncio
async def test_conflict_against_promoted_node_routes_to_unresolved_not_pending(
    stub_memory_graph_adapter, stub_graphiti_client,
) -> None:
    """SSOT-08 contradiction-queue routing.

    A claim conflicting with a PROMOTED (verified ground truth) node must not
    become an ordinary pending_llm_review pair -- that status is
    auto-adjudicated by confirm_pending_conflicts' symmetric LLM voter, which
    has no notion that one side is ground truth. Route it to 'unresolved'
    instead (already a meaningful conflict-pipeline status: LLM-confirmed
    genuine contradiction), which surfaces for manual review without being
    silently auto-voted on.
    """
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    stub_memory_graph_adapter.entity_scopes["existing-node"] = "PROMOTED"
    # Score 0.88 falls in the conflict range (0.85-0.95) -- a plain conflict,
    # not a merge proposal, to isolate the routing decision from the
    # merge-immunity veto (SSOT-08 stage 2).
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.88),
    ]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.conflicts_detected == 1
    assert len(stub_memory_graph_adapter.conflict_calls) == 1
    assert stub_memory_graph_adapter.conflict_calls[0]["initial_status"] == "unresolved"


@pytest.mark.asyncio
async def test_very_high_similarity_flags_conflict(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    # Score 0.96 falls in the (former) merge range (≥0.95)
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.96),
    ]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    # HOTFIX 2026-07-03: auto-merge disabled — the pair falls through to the conflict flag
    # (abstention-shaped; merge is irreversible and the score is a rank artifact).
    assert result.conflicts_detected == 1
    assert len(stub_memory_graph_adapter.conflict_calls) == 1
    assert len(stub_memory_graph_adapter.merge_entity_calls) == 0


@pytest.mark.asyncio
async def test_ineligible_node_veto_blocks_merge_via_lifecycle_path(
    stub_memory_graph_adapter, stub_graphiti_client,
) -> None:
    """SSOT-03 veto-parity regression.

    LifecycleService._check_contradictions_batch used to reimplement the
    merge-veto workflow inline and checked only co_mention/anchor_project,
    silently omitting the ineligible_node veto that CorrelationService's
    canonical _handle_merge_proposal always enforces. A structural/path-shaped
    node pair with an available, merge-confirming LLM judge would previously
    have been merge-eligible through the lifecycle path even though the
    canonical correlation path would refuse it. Now that lifecycle delegates
    to CorrelationService.classify_pair, the veto must block the merge here too.
    """
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    stub_memory_graph_adapter.ineligible_node_veto = True  # structural/path-shaped node
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.99),  # well within merge range
    ]
    fake_llm = _FakeLLM(confirm_same_entity_return=True)  # judge would confirm if asked

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
    )
    result = await svc.consolidate_session()

    # Veto fires before the judge is ever consulted -> falls through to conflict,
    # never merges, regardless of the judge's (would-be) unanimous yes.
    assert result.conflicts_detected == 1
    assert len(stub_memory_graph_adapter.merge_entity_calls) == 0
    assert fake_llm.confirm_same_entity_calls == 0


@pytest.mark.asyncio
async def test_promoted_node_veto_blocks_merge_via_lifecycle_path(
    stub_memory_graph_adapter, stub_graphiti_client,
) -> None:
    """SSOT-08 merge-immunity regression, exercised through the lifecycle path.

    A PROMOTED node must never be absorbed into (or absorb) another identity
    during correlation, even at a near-duplicate similarity score with a
    merge-confirming judge available.
    """
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    stub_memory_graph_adapter.entity_scopes["existing-node"] = "PROMOTED"
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.99),
    ]
    fake_llm = _FakeLLM(confirm_same_entity_return=True)

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
    )
    result = await svc.consolidate_session()

    assert result.conflicts_detected == 1
    assert len(stub_memory_graph_adapter.merge_entity_calls) == 0
    assert fake_llm.confirm_same_entity_calls == 0


@pytest.mark.asyncio
async def test_medium_similarity_creates_related_edge(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("new-node")]
    # Score 0.78 falls in the related range (0.70–0.85)
    stub_graphiti_client.search_scored_results = [
        ("existing-node", "existing", 0.78),
    ]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    # Related range: no conflict, no merge, but a RELATES_TO edge is created
    assert result.conflicts_detected == 0
    assert len(stub_memory_graph_adapter.conflict_calls) == 0
    assert len(stub_memory_graph_adapter.merge_entity_calls) == 0
    assert len(stub_memory_graph_adapter.related_edge_calls) == 1
    rel_call = stub_memory_graph_adapter.related_edge_calls[0]
    assert rel_call["source_uuid"] == "new-node"
    assert rel_call["target_uuid"] == "existing-node"


@pytest.mark.asyncio
async def test_low_similarity_no_conflict(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a")]
    stub_graphiti_client.search_scored_results = [
        ("other", "other", 0.5), # below threshold
    ]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.conflicts_detected == 0
    assert len(stub_memory_graph_adapter.conflict_calls) == 0


# --- Orphan recovery ---

@pytest.mark.asyncio
async def test_orphan_recovery_delegates_to_consolidation(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("old")]
    stub_graphiti_client.search_scored_results = []

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.recover_orphans(max_age_hours=4.0)

    assert isinstance(result, ConsolidationResult)
    assert result.promoted == 1


# --- Idempotency ---

@pytest.mark.asyncio
async def test_consolidation_is_idempotent(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a", flagged=True)]

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    r1 = await svc.consolidate_session()

    # Second run: session_entities would be empty in real Neo4j since
    # they were promoted. Simulate by clearing the list.
    stub_memory_graph_adapter.session_entities = []
    stub_memory_graph_adapter.promoted_nodes.clear()
    r2 = await svc.consolidate_session()

    assert r1.promoted == 1
    assert r2.promoted == 0


# --- Orphan episode cleanup ---

@pytest.mark.asyncio
async def test_orphan_episodes_cleaned(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = []
    stub_memory_graph_adapter.orphan_episodes_cleaned = 3

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.orphan_episodes_cleaned == 3


# --- Skipped pending count ---

@pytest.mark.asyncio
async def test_skipped_pending_reports_pending_episodes(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a", flagged=True)]
    stub_memory_graph_adapter.pending_episodes_count = 4

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.skipped_pending == 4
    assert result.promoted == 1


@pytest.mark.asyncio
async def test_skipped_pending_zero_when_none_pending(stub_memory_graph_adapter, stub_graphiti_client):
    stub_memory_graph_adapter.session_entities = [_make_entity("a", flagged=True)]
    stub_memory_graph_adapter.pending_episodes_count = 0

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )
    result = await svc.consolidate_session()

    assert result.skipped_pending == 0


# --- Constants ---

def test_thresholds_match_design_doc():
    assert SHARPNESS_PROMOTE_THRESHOLD == 0.5
    assert PERSISTENT_EDGE_PROMOTE_THRESHOLD == 3
    assert SIMILARITY_CONFLICT_THRESHOLD == 0.85


# --- Decay orchestration ---

@pytest.mark.asyncio
async def test_apply_decay_runs_full_core_pass(stub_memory_graph_adapter, stub_graphiti_client, tmp_path):
    old_active = datetime.now(timezone.utc) - timedelta(days=45)
    old_compressed = datetime.now(timezone.utc) - timedelta(days=120)
    stub_memory_graph_adapter.edge_counts_synced = 6
    stub_memory_graph_adapter.bridge_delete_results = {
        "compressed-1": {"edges_bridged": 2, "deleted": 1}
    }
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {
                "uuid": "active-1",
                "name": "Active Candidate",
                "content": "x" * 240,
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.0,
                "edge_count": 2,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": old_active,
                "created_at": old_active,
            },
        ],
        "COMPRESSED": [
            {
                "uuid": "compressed-1",
                "name": "Compressed Candidate",
                "content": "compressed summary",
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.05,
                "edge_count": 2,
                "freshness": "COMPRESSED",
                "user_flagged": False,
                "last_accessed": old_compressed,
                "created_at": old_compressed,
            },
        ],
    }
    # F2: Set count_similar_by_cosine_results (4 similar neighbors via true cosine)
    stub_graphiti_client.count_similar_by_cosine_results = 4

    fake_llm = _FakeLLM(compress_return="LLM compressed summary")
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "test.db"),
        settings=MemorySettings(),
    )
    result = await svc.apply_decay()

    # HOTFIX 2026-07-03: GONE disarmed — the eligible compressed candidate is NOT deleted.
    # 2026-07-13: orphan cleanup removed — orphan_subgraphs_cleaned is always 0.
    assert result == DecayResult(
        edge_counts_synced=6,
        sharpness_recalculated=1,
        compressed=1,
        deleted=0,
        edges_bridged=0,
        orphan_subgraphs_cleaned=0,
    )
    assert stub_memory_graph_adapter.sync_edge_counts_calls == 1
    assert stub_memory_graph_adapter.sharpness_updates["active-1"] == pytest.approx(0.2)
    assert stub_memory_graph_adapter.compressed_nodes[0]["uuid"] == "active-1"
    assert stub_memory_graph_adapter.compressed_nodes[0]["summary"] == "LLM compressed summary"
    assert stub_memory_graph_adapter.deleted_decay_nodes == []


@pytest.mark.asyncio
async def test_apply_decay_preserves_nodes_deferred_for_compression(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    tmp_path,
):
    old_active = datetime.now(timezone.utc) - timedelta(days=45)
    stub_memory_graph_adapter.edge_counts_synced = 1
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {
                "uuid": "active-deferred",
                "name": "Deferred Candidate",
                "content": "x" * 240,
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.0,
                "edge_count": 0,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": old_active,
                "created_at": old_active,
            },
        ],
        "COMPRESSED": [],
    }
    # F2: Set count_similar_by_cosine_results (4 similar neighbors via true cosine)
    stub_graphiti_client.count_similar_by_cosine_results = 4
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=None,
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "test.db"),
        settings=MemorySettings(),
    )

    result = await svc.apply_decay()

    # A node deferred for compression (no LLM) is recorded as a pending action for retry.
    # It is never deleted -- orphan cleanup was removed, so isolation cannot delete it.
    assert result.compressed == 0
    pending = svc.pending_actions.fetch_pending("compress")
    assert len(pending) == 1
    assert pending[0]["node_uuid"] == "active-deferred"


@pytest.mark.asyncio
async def test_apply_decay_skips_when_already_running(stub_memory_graph_adapter, stub_graphiti_client, tmp_path):
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        pending_actions=PendingActionStore(db_path=tmp_path / "test.db"),
    )
    await svc._decay_lock.acquire()

    result = await svc.apply_decay()

    assert result == DecayResult(0, 0, 0, 0, 0, 0)
    assert stub_memory_graph_adapter.sync_edge_counts_calls == 0


@pytest.mark.asyncio
async def test_apply_decay_handles_empty_candidate_sets(stub_memory_graph_adapter, stub_graphiti_client, tmp_path):
    stub_memory_graph_adapter.edge_counts_synced = 2
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        pending_actions=PendingActionStore(db_path=tmp_path / "test.db"),
    )

    result = await svc.apply_decay()

    assert result == DecayResult(
        edge_counts_synced=2,
        sharpness_recalculated=0,
        compressed=0,
        deleted=0,
        edges_bridged=0,
        orphan_subgraphs_cleaned=0,
    )


@pytest.mark.asyncio
async def test_apply_decay_skips_sharpness_update_when_similarity_search_fails(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    tmp_path,
):
    old_active = datetime.now(timezone.utc) - timedelta(days=45)
    stub_memory_graph_adapter.edge_counts_synced = 1
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {
                "uuid": "active-1",
                "name": "Active Candidate",
                "content": "x" * 240,
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.25,
                "edge_count": 2,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": old_active,
                "created_at": old_active,
            },
        ],
        "COMPRESSED": [],
    }

    # F2: Make count_similar_by_cosine (sharpness path) fail, simulating graphiti unavailable
    stub_graphiti_client.count_similar_by_cosine_exception = RuntimeError("graphiti unavailable")

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=_FakeLLM(compress_return="should not run"),
        pending_actions=PendingActionStore(db_path=tmp_path / "test.db"),
    )

    result = await svc.apply_decay()

    assert result == DecayResult(
        edge_counts_synced=1,
        sharpness_recalculated=0,
        compressed=0,
        deleted=0,
        edges_bridged=0,
        orphan_subgraphs_cleaned=0,
    )
    assert "active-1" not in stub_memory_graph_adapter.sharpness_updates
    assert stub_memory_graph_adapter.compressed_nodes == []


@pytest.mark.asyncio
async def test_rehydrate_node_merges_content_and_clears_pending(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    tmp_path,
):
    store = PendingActionStore(db_path=tmp_path / "test.db")
    store.upsert("compressed-1", "rehydrate", context="fresh detail", source_uuid="episode-1")
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "compressed-1",
            "name": "Compressed Node",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "compressed summary",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc) - timedelta(days=100),
            "edge_count": 1,
            "sharpness": 0.05,
            "freshness": "COMPRESSED",
            "user_flagged": False,
        }
    ]
    stub_memory_graph_adapter.node_freshness["compressed-1"] = "COMPRESSED"
    fake_llm = _FakeLLM(merge_return="merged memory")
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
        pending_actions=store,
    )

    result = await svc.rehydrate_node(
        "compressed-1",
        new_context="fresh detail",
        source_episode_uuid="episode-1",
    )

    assert result is True
    assert fake_llm.merge_calls == [("compressed summary", "fresh detail")]
    assert stub_memory_graph_adapter.rehydrated_nodes == [
        {"uuid": "compressed-1", "content": "merged memory"}
    ]
    assert store.fetch_pending("rehydrate") == []


@pytest.mark.asyncio
async def test_rehydrate_node_flips_active_without_new_context(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    tmp_path,
):
    store = PendingActionStore(db_path=tmp_path / "test.db")
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "compressed-1",
            "name": "Compressed Node",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "compressed summary",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc) - timedelta(days=100),
            "edge_count": 1,
            "sharpness": 0.05,
            "freshness": "COMPRESSED",
            "user_flagged": False,
        }
    ]
    stub_memory_graph_adapter.node_freshness["compressed-1"] = "COMPRESSED"
    fake_llm = _FakeLLM()
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
        pending_actions=store,
    )

    result = await svc.rehydrate_node("compressed-1")

    assert result is True
    assert fake_llm.merge_calls == []
    assert stub_memory_graph_adapter.rehydrated_nodes == [
        {"uuid": "compressed-1", "content": None}
    ]
    assert store.count() == 0


@pytest.mark.asyncio
async def test_rehydrate_node_queues_pending_when_llm_fails(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    tmp_path,
):
    store = PendingActionStore(db_path=tmp_path / "test.db")
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "compressed-1",
            "name": "Compressed Node",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "compressed summary",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc) - timedelta(days=100),
            "edge_count": 1,
            "sharpness": 0.05,
            "freshness": "COMPRESSED",
            "user_flagged": False,
        }
    ]
    stub_memory_graph_adapter.node_freshness["compressed-1"] = "COMPRESSED"
    fake_llm = _FakeLLM(merge_return=None)
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
        pending_actions=store,
    )

    result = await svc.rehydrate_node(
        "compressed-1",
        new_context="fresh detail",
        source_episode_uuid="episode-1",
    )

    assert result is False
    assert stub_memory_graph_adapter.rehydrated_nodes == []
    pending = store.fetch_pending("rehydrate")
    assert len(pending) == 1
    assert pending[0]["node_uuid"] == "compressed-1"
    assert pending[0]["action"] == "rehydrate"
    assert pending[0]["context"] == "fresh detail"
    assert pending[0]["source_uuid"] == "episode-1"
    assert pending[0]["failure_reason"] == "llm_failed"


@pytest.mark.asyncio
async def test_rehydrate_node_marks_pending_failure_when_graph_update_fails(
    stub_memory_graph_adapter,
    stub_graphiti_client,
):
    fake_pending_actions = _FakePendingActions()
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "compressed-1",
            "name": "Compressed Node",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "compressed summary",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc) - timedelta(days=100),
            "edge_count": 1,
            "sharpness": 0.05,
            "freshness": "COMPRESSED",
            "user_flagged": False,
        }
    ]
    stub_memory_graph_adapter.node_freshness["compressed-1"] = "COMPRESSED"

    def _fail_complete_rehydration(node_uuid: str, updated_content: str | None = None) -> bool:
        stub_memory_graph_adapter.rehydrated_nodes.append({"uuid": node_uuid, "content": updated_content})
        return False

    stub_memory_graph_adapter.complete_rehydration = _fail_complete_rehydration  # type: ignore[method-assign]

    fake_llm = _FakeLLM(merge_return="merged memory")
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=fake_llm,
        pending_actions=fake_pending_actions,  # type: ignore[arg-type]
    )

    result = await svc.rehydrate_node(
        "compressed-1",
        new_context="fresh detail",
        source_episode_uuid="episode-1",
    )

    assert result is False
    assert fake_pending_actions.complete_calls == []
    assert fake_pending_actions.upsert_calls == [
        {
            "node_uuid": "compressed-1",
            "action": "rehydrate",
            "context": "fresh detail",
            "source_uuid": "episode-1",
            "failure_reason": "complete_rehydration_failed",
        }
    ]


# --- Decay policy-input contract (2026-07-13 hotfix) ---------------------------
# The decay projection was widened to carry `type`, `rehydration_count`, and
# `target_date_passed`. These tests assert those fields reach the policy call
# through the full `_run_decay` path -- a projection-only test cannot prove the
# service loop does not drop them before `should_compress`.

@pytest.mark.asyncio
async def test_apply_decay_passes_complete_policy_input_to_should_compress(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
):
    old_active = datetime.now(timezone.utc) - timedelta(days=45)
    stub_memory_graph_adapter.edge_counts_synced = 1
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {
                "uuid": "active-1",
                "type": "PROCEDURAL",
                "name": "Active Candidate",
                "content": "x" * 240,
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.0,
                "edge_count": 2,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": old_active,
                "created_at": old_active,
                "rehydration_count": 0,
                "target_date_passed": False,
            },
        ],
    }
    stub_graphiti_client.count_similar_by_cosine_results = 4

    captured: list[dict] = []
    original = LifecycleService.should_compress

    def _spy(node):
        captured.append(dict(node))
        return original(node)

    monkeypatch.setattr(LifecycleService, "should_compress", staticmethod(_spy))

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=_FakeLLM(compress_return="LLM compressed summary"),
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "test.db"),
        settings=MemorySettings(),
    )
    await svc.apply_decay()

    assert captured, "should_compress was never called"
    node = captured[0]
    # Every field MemoryTypePolicy reads must be present at the policy boundary,
    # including the ones _run_decay enriches (last_accessed_days_ago) and the ones
    # the widened projection supplies (type, rehydration_count, target_date_passed).
    for key in (
        "type",
        "rehydration_count",
        "target_date_passed",
        "last_accessed_days_ago",
        "sharpness",
        "edge_count",
        "scope",
        "freshness",
        "user_flagged",
    ):
        assert key in node, f"policy input {key!r} missing at should_compress boundary"
    assert node["type"] == "PROCEDURAL"


@pytest.mark.asyncio
async def test_apply_decay_exempts_rehydrated_node_from_compression(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
):
    # A node old and low-signal enough to compress, but rehydrated at/above the
    # exempt count. Before the hotfix, rehydration_count never reached the policy
    # (it defaulted to 0), so this node WOULD have been compressed. With the field
    # flowing through, the REHYDRATION_EXEMPT_COUNT gate protects it.
    from menhir.domain.memory_types import REHYDRATION_EXEMPT_COUNT

    old_active = datetime.now(timezone.utc) - timedelta(days=45)
    stub_memory_graph_adapter.edge_counts_synced = 1
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {
                "uuid": "rehydrated-1",
                "type": "SEMANTIC",
                "name": "Rehydrated Candidate",
                "content": "x" * 240,
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.0,
                "edge_count": 2,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": old_active,
                "created_at": old_active,
                "rehydration_count": REHYDRATION_EXEMPT_COUNT,
                "target_date_passed": False,
            },
        ],
    }
    # 4 similar neighbors -> sharpness 0.2 < 0.3, so absent the exemption this
    # candidate satisfies every compression threshold.
    stub_graphiti_client.count_similar_by_cosine_results = 4

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=_FakeLLM(compress_return="LLM compressed summary"),
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "test.db"),
        settings=MemorySettings(),
    )
    result = await svc.apply_decay()

    assert result.sharpness_recalculated == 1  # the pass reached the decision
    assert result.compressed == 0
    assert stub_memory_graph_adapter.compressed_nodes == []
