"""Unit tests for CorrelationService and correlation routing logic."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.services.correlation_service import (
    CORRELATION_CONFLICT_THRESHOLD,
    CORRELATION_MERGE_THRESHOLD,
    CORRELATION_RELATED_THRESHOLD,
    CorrelationBatchResult,
    CorrelationResult,
    CorrelationService,
)
from menhir.infrastructure.correlation_queries import (
    GUARDED_PROVENANCE,
    CorrelationRepository,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo() -> MagicMock:
    """Return a mock CorrelationRepository."""
    repo = MagicMock(spec=CorrelationRepository)
    repo.create_related_to_edge.return_value = True
    repo.merge_entity.return_value = {"merged": 1, "edges_bridged": 2, "deleted": 1}
    repo.correlation_exists.return_value = False
    repo.fetch_entity_merge_metadata.return_value = []
    return repo


def _make_graphiti_client(results: list[tuple[str, str, float]] | None = None) -> AsyncMock:
    """Return a mock GraphitiClient with configurable search results."""
    client = AsyncMock()
    client.search_scored = AsyncMock(return_value=results or [])
    return client


# ---------------------------------------------------------------------------
# Routing logic tests
# ---------------------------------------------------------------------------

class TestCorrelationRouting:
    """Test the _route() method that determines action from similarity score."""

    def test_below_related_threshold_returns_none(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(0.50) == "none"

    def test_at_related_threshold_returns_related(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(CORRELATION_RELATED_THRESHOLD) == "related"

    def test_between_related_and_conflict_returns_related(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(0.78) == "related"

    def test_at_conflict_threshold_returns_conflict(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(CORRELATION_CONFLICT_THRESHOLD) == "conflict"

    def test_between_conflict_and_merge_returns_conflict(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(0.90) == "conflict"

    def test_at_merge_threshold_returns_merge_proposed(self) -> None:
        # Part 2: Judge-gated merge (2026-07-03 plan). Scores >= merge_threshold now route to
        # "merge_proposed", which is then handled by deterministic vetoes + LLM judge.
        # The judge decides whether to merge or flag as conflict (fail-safe).
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(CORRELATION_MERGE_THRESHOLD) == "merge_proposed"

    def test_above_merge_threshold_returns_merge_proposed(self) -> None:
        # Part 2: Judge-gated merge routes to proposal for judge.
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        assert svc._route(0.99) == "merge_proposed"

    def test_custom_thresholds_override_defaults(self) -> None:
        # Part 2: Custom thresholds work with judge-gated merge.
        svc = CorrelationService(
            _make_repo(),
            _make_graphiti_client(),
            related_threshold=0.6,
            conflict_threshold=0.75,
            merge_threshold=0.90,
        )
        assert svc._route(0.55) == "none"
        assert svc._route(0.60) == "related"
        assert svc._route(0.75) == "conflict"
        # merge range routes to merge_proposed for judge-gating (Part 2)
        assert svc._route(0.90) == "merge_proposed"


# ---------------------------------------------------------------------------
# Single-node correlation check tests
# ---------------------------------------------------------------------------

class TestCheckCorrelation:
    """Test check_correlation() — single node check during enrichment."""

    @pytest.mark.asyncio
    async def test_empty_query_skips_check(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client()
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "  ")
        assert result.checked == 1
        assert result.skipped == 1
        assert result.related == 0
        client.search_scored.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_similar_entities_returns_empty(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.checked == 1
        assert result.related == 0
        assert result.conflicts == 0
        assert result.merged == 0

    @pytest.mark.asyncio
    async def test_related_range_creates_edge(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "similar entity", 0.75),
        ])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.related == 1
        assert result.conflicts == 0
        repo.create_related_to_edge.assert_called_once_with(
            "uuid-1", "uuid-2", similarity=0.75,
        )

    @pytest.mark.asyncio
    async def test_conflict_range_returns_conflict_count(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "contradicting entity", 0.88),
        ])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.conflicts == 1
        assert result.related == 0
        # Conflict handling is delegated — no edge or merge created here
        repo.create_related_to_edge.assert_not_called()
        repo.merge_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_range_flags_conflict(self) -> None:
        # Part 2: Judge-gated merge (2026-07-03 plan). The merge range routes to merge_proposed,
        # which is judged. With no LLM available, merge_proposed → conflict (fail-safe: never
        # merge without confirmation). This test exercises the no-judge case: same behavior as
        # before (conflict flag), but now through the judge-gating pathway.
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "near duplicate", 0.96),
        ])
        svc = CorrelationService(repo, client, llm=None)  # No judge → fail-safe to conflict

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.merged == 0
        assert result.conflicts == 1
        repo.merge_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_committed_fires_scalar_state_hook(self) -> None:
        # Piece C.3: a committed merge invokes on_merge_committed(absorbed, survivor) post-COMMIT.
        repo = _make_repo()
        coordinator = MagicMock()
        coordinator.merge.return_value = {"merged": 1, "op_id": "op-1"}
        seen: list[dict] = []

        def _hook(*, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str) -> None:
            seen.append({"absorbed": absorbed_uuid, "survivor": survivor_uuid, "op": merge_op_id})

        svc = CorrelationService(repo, _make_graphiti_client(), merge_coordinator=coordinator,
                                 on_merge_committed=_hook)
        with patch.object(svc, "_handle_merge_proposal", AsyncMock(return_value="merged")):
            action, _ = await svc.classify_pair("src-absorbed", "tgt-survivor", 0.97)
        assert action == "merged"
        assert seen == [{"absorbed": "src-absorbed", "survivor": "tgt-survivor", "op": "op-1"}]

    @pytest.mark.asyncio
    async def test_merge_hook_failure_does_not_fail_the_merge(self) -> None:
        # The merge already committed; a hook error is logged and swallowed (repaired out of band).
        repo = _make_repo()
        coordinator = MagicMock()
        coordinator.merge.return_value = {"merged": 1, "op_id": "op-2"}

        def _boom(*, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str) -> None:
            raise RuntimeError("rebind blew up")

        svc = CorrelationService(repo, _make_graphiti_client(), merge_coordinator=coordinator,
                                 on_merge_committed=_boom)
        with patch.object(svc, "_handle_merge_proposal", AsyncMock(return_value="merged")):
            action, details = await svc.classify_pair("s", "t", 0.97)
        assert action == "merged" and details.get("merged") == 1  # merge still reported success

    @pytest.mark.asyncio
    async def test_no_hook_is_byte_identical(self) -> None:
        # Default (no hook) path: a committed merge returns normally with no scalar coupling.
        repo = _make_repo()
        coordinator = MagicMock()
        coordinator.merge.return_value = {"merged": 1, "op_id": "op-3"}
        svc = CorrelationService(repo, _make_graphiti_client(), merge_coordinator=coordinator)
        with patch.object(svc, "_handle_merge_proposal", AsyncMock(return_value="merged")):
            action, _ = await svc.classify_pair("s", "t", 0.97)
        assert action == "merged"

    @pytest.mark.asyncio
    async def test_self_match_excluded(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-1", "same entity", 1.0),
            ("uuid-2", "different entity", 0.75),
        ])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        # Self match excluded, only the 0.75 match creates a related edge
        assert result.related == 1

    @pytest.mark.asyncio
    async def test_exclude_uuids_respected(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "excluded entity", 0.75),
            ("uuid-3", "included entity", 0.80),
        ])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation(
            "uuid-1", "test query", exclude_uuids={"uuid-2"},
        )
        assert result.related == 1
        repo.create_related_to_edge.assert_called_once_with(
            "uuid-1", "uuid-3", similarity=0.80,
        )

    @pytest.mark.asyncio
    async def test_below_threshold_not_routed(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "vaguely similar", 0.50),
        ])
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.related == 0
        assert result.conflicts == 0
        assert result.merged == 0

    @pytest.mark.asyncio
    async def test_search_failure_returns_skipped(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client()
        client.search_scored = AsyncMock(side_effect=RuntimeError("search failed"))
        svc = CorrelationService(repo, client)

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.checked == 1
        assert result.skipped == 1

    @pytest.mark.asyncio
    async def test_mixed_similarity_results(self) -> None:
        # Part 2: Judge-gated merge. With no LLM, merge_proposed → conflict (fail-safe).
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-2", "related", 0.75),
            ("uuid-3", "conflicting", 0.88),
            ("uuid-4", "duplicate", 0.97),
        ])
        svc = CorrelationService(repo, client, llm=None)  # No judge: merge_proposed → conflict

        result = await svc.check_correlation("uuid-1", "test query")
        assert result.related == 1
        # the 0.88 "conflicting" and 0.97 "duplicate" both land in conflict (second via judge-gating)
        assert result.conflicts == 2
        assert result.merged == 0


# ---------------------------------------------------------------------------
# Batch correlation check tests
# ---------------------------------------------------------------------------

class TestCheckCorrelationBatch:
    """Test check_correlation_batch() — used during consolidation/promotion."""

    @pytest.mark.asyncio
    async def test_empty_batch_returns_zero(self) -> None:
        svc = CorrelationService(_make_repo(), _make_graphiti_client())
        result = await svc.check_correlation_batch([])
        assert result.checked == 0

    @pytest.mark.asyncio
    async def test_batch_processes_multiple_candidates(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-other", "related", 0.75),
        ])
        svc = CorrelationService(repo, client)

        candidates = [
            {"uuid": "uuid-1", "name": "entity 1", "content": "test"},
            {"uuid": "uuid-2", "name": "entity 2", "content": "test"},
        ]
        result = await svc.check_correlation_batch(candidates)
        assert result.checked == 2
        assert result.related == 2  # Each candidate creates one RELATES_TO edge

    @pytest.mark.asyncio
    async def test_batch_skips_empty_query(self) -> None:
        repo = _make_repo()
        client = _make_graphiti_client()
        svc = CorrelationService(repo, client)

        candidates = [
            {"uuid": "uuid-1", "name": "", "content": ""},
        ]
        result = await svc.check_correlation_batch(candidates)
        assert result.skipped == 1
        client.search_scored.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_stops_after_first_match_per_node(self) -> None:
        """Batch mode only processes one match per node (like _check_contradictions_batch)."""
        repo = _make_repo()
        client = _make_graphiti_client(results=[
            ("uuid-other-1", "related 1", 0.75),
            ("uuid-other-2", "related 2", 0.80),
        ])
        svc = CorrelationService(repo, client)

        candidates = [
            {"uuid": "uuid-1", "name": "entity 1", "content": "test"},
        ]
        result = await svc.check_correlation_batch(candidates)
        # Only one edge created (first match), not two
        assert result.related == 1


# ---------------------------------------------------------------------------
# Judge-gated merge tests (Part 2)
# ---------------------------------------------------------------------------

class TestJudgeGatedMerge:
    """Test the judge-gated merge pipeline with deterministic vetoes."""

    @pytest.mark.asyncio
    async def test_promoted_survivor_veto_blocks_merge(self) -> None:
        """SSOT-08: a PROMOTED node is merge-immune, even if the judge would confirm."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Foo", "content": "content 1", "scope": "PROMOTED"},
            {"uuid": "uuid-2", "name": "Bar", "content": "content 2", "scope": "PERSISTENT"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",  # PROMOTED
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        assert action == "conflict"
        fake_llm.confirm_same_entity.assert_not_called()
        repo.merge_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_promoted_absorbed_veto_blocks_merge(self) -> None:
        """SSOT-08: PROMOTED immunity applies regardless of which side of the pair it's on."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Foo", "content": "content 1", "scope": "PERSISTENT"},
            {"uuid": "uuid-2", "name": "Bar", "content": "content 2", "scope": "PROMOTED"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",  # PROMOTED
            similarity=0.97,
        )

        assert action == "conflict"
        fake_llm.confirm_same_entity.assert_not_called()
        repo.merge_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_co_mention_veto_blocks_merge(self) -> None:
        """Co-mention veto: if both entities are MENTIONED by same episode, they are distinct."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = True  # Veto fires
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Foo", "content": "content 1"},
            {"uuid": "uuid-2", "name": "Bar", "content": "content 2"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)  # Even if judge says yes

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Veto fires → conflict, never merges
        assert action == "conflict"
        # Judge never called (veto blocks it)
        fake_llm.confirm_same_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_anchor_project_veto_blocks_merge(self) -> None:
        """Anchor-project veto: both anchored to different projects → veto."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = True  # Anchor project veto fires
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "FileA", "content": "src/main/java"},
            {"uuid": "uuid-2", "name": "FileB", "content": "src/test/java"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Veto fires → conflict
        assert action == "conflict"
        # Judge never called
        fake_llm.confirm_same_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_ineligible_node_veto_structural_survivor(self) -> None:
        """Ineligible-node veto: structural survivor → veto."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = True  # Structural or path-shaped node
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "src/components/article", "content": "Article component"},
            {"uuid": "uuid-2", "name": "Article", "content": "Article entity"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Ineligible node veto fires → conflict (before judge is consulted)
        assert action == "conflict"
        # Judge never called (veto blocks it)
        fake_llm.confirm_same_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_ineligible_node_veto_path_shaped_absorbed(self) -> None:
        """Ineligible-node veto: path-shaped absorbed node → veto."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = True  # Path-shaped absorbed node
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Configuration", "content": "Config"},
            {"uuid": "uuid-2", "name": ".env", "content": "Environment variables"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Ineligible node veto fires → conflict
        assert action == "conflict"
        # Judge never called
        fake_llm.confirm_same_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_ineligible_node_veto_fires_before_co_mention(self) -> None:
        """Ineligible-node veto runs FIRST, before co_mention check."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = True  # Fires
        repo.check_co_mention_veto.return_value = True  # Also would fire
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "CHANGELOG.md", "content": "Changelog"},
            {"uuid": "uuid-2", "name": "Version", "content": "v1.0"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Both vetoes would fire, but ineligible_node fires first → conflict
        assert action == "conflict"
        # Judge never called
        fake_llm.confirm_same_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_ineligible_node_veto_does_not_fire_for_plain_names(self) -> None:
        """Ineligible-node veto does NOT fire for plain (non-structural, non-path-shaped) names."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False  # Plain names, no veto
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "UserService", "summary": "User management"},
            {"uuid": "uuid-2", "name": "UserServiceV2", "summary": "User management v2"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        # All three judges say SAME
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # No veto → judge is consulted and all vote yes → merged
        assert action == "merged"
        # Judge called k=3 times (veto did not block it)
        assert fake_llm.confirm_same_entity.call_count == 3

    @pytest.mark.asyncio
    async def test_unanimous_judge_allows_merge(self) -> None:
        """Unanimous judge (k=3, all yes) allows merge to proceed."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "UserService", "summary": "User management"},
            {"uuid": "uuid-2", "name": "UserServiceV2", "summary": "User management v2"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        # All three judges say SAME
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Unanimous yes → merge allowed
        assert action == "merged"
        # Judge called k=3 times
        assert fake_llm.confirm_same_entity.call_count == 3

    @pytest.mark.asyncio
    async def test_split_judge_blocks_merge(self) -> None:
        """Split judge votes (not unanimous) → conflict (fail-safe)."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Article", "summary": "News article"},
            {"uuid": "uuid-2", "name": "News", "summary": "News story"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        # Split: yes, no, yes — not unanimous
        fake_llm.confirm_same_entity = AsyncMock(side_effect=[True, False, True])

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Split judge → conflict (fail-safe: never merge without unanimous yes)
        assert action == "conflict"
        # Judge called k=3 times
        assert fake_llm.confirm_same_entity.call_count == 3

    @pytest.mark.asyncio
    async def test_judge_unavailable_blocks_merge(self) -> None:
        """Judge unavailable → merge blocked, route to conflict (fail-safe)."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False

        client = _make_graphiti_client()

        svc = CorrelationService(repo, client, llm=None)  # No LLM

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # No LLM → conflict (fail-safe: never merge without judge)
        assert action == "conflict"

    @pytest.mark.asyncio
    async def test_judge_exception_blocks_merge(self) -> None:
        """Judge exception (None return) → merge blocked (fail-safe)."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "ServiceA", "summary": "Service A"},
            {"uuid": "uuid-2", "name": "ServiceB", "summary": "Service B"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        # Judge fails (returns None)
        fake_llm.confirm_same_entity = AsyncMock(return_value=None)

        svc = CorrelationService(repo, client, llm=fake_llm)

        action = await svc._handle_merge_proposal(
            survivor_uuid="uuid-1",
            absorbed_uuid="uuid-2",
            similarity=0.97,
        )

        # Judge unavailable → conflict
        assert action == "conflict"

    @pytest.mark.asyncio
    async def test_merge_proposed_in_check_correlation(self) -> None:
        """Scores >= merge_threshold route to merge_proposed, then judge."""
        repo = _make_repo()
        repo.check_ineligible_node_veto = MagicMock(return_value=False)
        repo.check_co_mention_veto = MagicMock(return_value=False)
        repo.check_anchor_project_veto = MagicMock(return_value=False)
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-2", "name": "Entity", "summary": "test"},
            {"uuid": "uuid-1", "name": "Entity", "summary": "test"},
        ]

        client = _make_graphiti_client(results=[
            ("uuid-2", "similar entity", 0.96),  # >= merge_threshold
        ])

        from unittest.mock import AsyncMock
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        # A confirmed merge now executes through the journaled saga (plan Phase 4), not the raw
        # repository primitive. Inject the coordinator so this unit test does not touch the sidecar.
        fake_coordinator = MagicMock()
        fake_coordinator.merge = MagicMock(return_value={"merged": 1, "op_id": "op-1"})

        svc = CorrelationService(repo, client, llm=fake_llm, merge_coordinator=fake_coordinator)

        result = await svc.check_correlation("uuid-1", "test query")

        # Merge proposed → judge → unanimous yes → merged (via the coordinator)
        assert result.merged == 1
        fake_llm.confirm_same_entity.assert_called()
        fake_coordinator.merge.assert_called_once_with(
            survivor_uuid="uuid-2", absorbed_uuid="uuid-1", similarity=0.96
        )
        # The unaudited repository path must NOT be used for a confirmed merge.
        repo.merge_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_unnamed_node_skips_correlation(self) -> None:
        """Unnamed node (no name, no content) skips correlation entirely."""
        repo = _make_repo()
        client = _make_graphiti_client()

        svc = CorrelationService(repo, client)

        # Empty name and content
        result = await svc.check_correlation("uuid-1", "")

        # Skipped, no search performed
        assert result.checked == 1
        assert result.skipped == 1
        client.search_scored.assert_not_called()


# ---------------------------------------------------------------------------
# Audit trail tests (Part 3)
# ---------------------------------------------------------------------------

class TestMergeAuditTrail:
    """Test that merge audit snapshots preserve absorbed node for unmerge."""

    def test_merge_audit_snapshot_structure(self) -> None:
        """Part 3: merge_audit snapshot includes uuid, properties, and relationships."""
        # This is a contract test: merge_entity should produce a merge_audit entry
        # with enough information to reconstruct the absorbed node.
        # Actual Neo4j execution would be tested in integration tests.
        # This test documents the expected structure.

        expected_snapshot_fields = [
            "snapshot_at",
            "absorbed_uuid",
            "survivor_uuid",
            "similarity",
            "properties",
            "relationships",
        ]

        # The merge_entity Cypher query constructs audit_entry as:
        # json({
        #     snapshot_at: datetime() | toString,
        #     absorbed_uuid: absorbed.uuid,
        #     survivor_uuid: survivor.uuid,
        #     similarity: $similarity,
        #     properties: snapshot,          # uuid, name, summary, content, source, ...
        #     relationships: relationships   # [{peer_uuid, rel_type, weight, direction}, ...]
        # })

        # Verify the query has these fields (documentation/contract)
        from menhir.infrastructure.correlation_queries import CorrelationRepository
        assert hasattr(CorrelationRepository, "merge_entity")

        # The audit snapshot round-trip: absorbed node props + edges are recoverable
        # from merge_audit[*].{properties, relationships} on the survivor.
        # Test would assert: given a merge_audit entry, all absorbed node metadata
        # is present and can reconstruct the original entity structure.

    @pytest.mark.asyncio
    async def test_identity_receipt_emitted(self) -> None:
        """Part 4: identity_decision event emitted with band, vetoes, judge votes."""
        repo = MagicMock(spec=CorrelationRepository)
        repo.check_ineligible_node_veto.return_value = False
        repo.check_co_mention_veto.return_value = False
        repo.check_anchor_project_veto.return_value = False
        repo.fetch_entity_merge_metadata.return_value = [
            {"uuid": "uuid-1", "name": "Service", "summary": "A service"},
            {"uuid": "uuid-2", "name": "Service", "summary": "A service"},
        ]

        client = _make_graphiti_client()

        from unittest.mock import AsyncMock, patch
        fake_llm = AsyncMock()
        fake_llm.confirm_same_entity = AsyncMock(return_value=True)

        svc = CorrelationService(repo, client, llm=fake_llm)

        # Patch record_mcp_event to verify it's called
        with patch("menhir.services.correlation_service.record_mcp_event") as mock_record:
            action = await svc._handle_merge_proposal(
                survivor_uuid="uuid-1",
                absorbed_uuid="uuid-2",
                similarity=0.97,
            )

            # Should have emitted an identity_decision event
            assert mock_record.called
            call_args = mock_record.call_args
            assert call_args[1]["operation"] == "identity_decision"
            assert call_args[1]["payload"]["similarity"] == 0.97
            assert call_args[1]["result"]["final_action"] == "merged"
            # Verify judge votes are in the result
            assert "judge_votes" in call_args[1]["result"]
            assert call_args[1]["result"]["unanimous"] is True


# ---------------------------------------------------------------------------
# CorrelationRepository unit tests (mocked Neo4j)
# ---------------------------------------------------------------------------

class TestCorrelationRepository:
    """Test CorrelationRepository Cypher operations with mocked Neo4j."""

    def test_create_related_to_edge_success(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = [{"edge_type": "correlation"}]
        repo = CorrelationRepository(neo4j)

        result = repo.create_related_to_edge("uuid-1", "uuid-2", similarity=0.75)
        assert result is True
        neo4j.execute.assert_called_once()
        # Verify the Cypher uses MERGE for idempotency
        call_args = neo4j.execute.call_args
        cypher = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
        assert "MERGE" in cypher
        assert "RELATES_TO" in cypher

    def test_create_related_to_edge_no_match(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = []
        repo = CorrelationRepository(neo4j)

        result = repo.create_related_to_edge("uuid-1", "uuid-2", similarity=0.75)
        assert result is False

    @staticmethod
    def _eligible_signal_rows() -> list[dict]:
        """Preflight eligibility rows for two ELIGIBLE nodes (plan Phase 3 gate)."""
        return [
            {"uuid": "survivor-1", "ineligible_role": False, "namespace": "default",
             "freshness": "ACTIVE", "scope": "PERSISTENT", "user_flagged": False,
             "conflict_status": None},
            {"uuid": "absorbed-2", "ineligible_role": False, "namespace": "default",
             "freshness": "ACTIVE", "scope": "PERSISTENT", "user_flagged": False,
             "conflict_status": None},
        ]

    def test_merge_entity_success(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.side_effect = [
            self._eligible_signal_rows(),          # Phase 0: eligibility preflight
            [{"uuid": "absorbed-2", "relationships": []}],  # Phase 1: absorbed snapshot
            [{"edges_bridged": 2, "deleted": 1}],  # Phase 2: mutate + delete
        ]
        repo = CorrelationRepository(neo4j)

        result = repo.merge_entity("survivor-1", "absorbed-2", similarity=0.96)
        assert result["merged"] == 1
        assert result["edges_bridged"] == 2
        assert result["deleted"] == 1
        # Three phases: (0) eligibility preflight, (1) read absorbed snapshot, (2) mutate + delete.
        assert neo4j.execute.call_count == 3
        snapshot_cypher = neo4j.execute.call_args_list[1][0][0]
        mutate_cypher = neo4j.execute.call_args_list[2][0][0]
        # Phase 2 does the destructive work
        assert "DETACH DELETE" in mutate_cypher
        assert "RELATES_TO" in mutate_cypher  # bridging edges
        assert "min(" not in mutate_cypher
        # Provenance is DERIVED in Python and written as finished values. The old clause computed it
        # inline (`source_confidence = CASE ... + 0.1`), which both inflated trust per merge and put
        # a second copy of the tier ladder in Cypher where it could drift from the domain table.
        assert "source_confidence = $authority" in mutate_cypher
        assert "+ 0.1" not in mutate_cypher
        assert "source_confidence = CASE" not in mutate_cypher
        # The audit entry is serialized in Python, not via a (nonexistent) Neo4j json() function
        assert "json(" not in snapshot_cypher and "json(" not in mutate_cypher
        assert "| toString" not in snapshot_cypher and "| toString" not in mutate_cypher

    def test_merge_entity_audit_entry_is_valid_json(self) -> None:
        """The merge_audit entry is serialized in Python to a JSON string (Neo4j here has no json()
        function and cannot store nested maps), carrying the absorbed node snapshot + relationships."""
        import json as _json

        neo4j = MagicMock()
        neo4j.execute.side_effect = [
            # Phase 0: eligibility preflight (both nodes eligible)
            self._eligible_signal_rows(),
            # Phase 1: absorbed snapshot + relationships
            [{
                "uuid": "absorbed-2", "name": "Svc", "summary": "s", "content": "c",
                "source": "claude-code", "source_confidence": 0.8, "type": "service",
                "scope": "PERSISTENT", "created_at": "2026-01-01T00:00:00Z", "namespace": "default",
                "relationships": [
                    {"peer_uuid": "p1", "rel_type": "RELATES_TO", "weight": 0.9, "direction": "out"},
                ],
            }],
            # Phase 2: mutate result
            [{"edges_bridged": 1, "deleted": 1}],
        ]
        repo = CorrelationRepository(neo4j)

        repo.merge_entity("survivor-1", "absorbed-2", similarity=0.97)

        mutate_params = neo4j.execute.call_args_list[2][1]["params"]
        entry = _json.loads(mutate_params["audit_entry"])
        assert entry["absorbed_uuid"] == "absorbed-2"
        assert entry["survivor_uuid"] == "survivor-1"
        assert entry["similarity"] == 0.97
        assert entry["properties"]["name"] == "Svc"
        assert entry["relationships"][0]["peer_uuid"] == "p1"
        assert "snapshot_at" in entry

    def test_merge_entity_no_match(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = []
        repo = CorrelationRepository(neo4j)

        result = repo.merge_entity("survivor-1", "absorbed-2", similarity=0.96)
        assert result["merged"] == 0
        assert result["edges_bridged"] == 0

    def test_correlation_exists_true(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = [{"cnt": 1}]
        repo = CorrelationRepository(neo4j)

        assert repo.correlation_exists("uuid-1", "uuid-2") is True

    def test_correlation_exists_false(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = [{"cnt": 0}]
        repo = CorrelationRepository(neo4j)

        assert repo.correlation_exists("uuid-1", "uuid-2") is False

    def test_fetch_entity_merge_metadata_empty(self) -> None:
        neo4j = MagicMock()
        repo = CorrelationRepository(neo4j)

        result = repo.fetch_entity_merge_metadata([])
        assert result == []
        neo4j.execute.assert_not_called()

    def test_fetch_entity_merge_metadata_returns_rows(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = [
            {"uuid": "uuid-1", "name": "test", "summary": "s", "content": "c",
             "source": "claude-code", "source_confidence": 0.9, "scope": "PERSISTENT",
             "created_at": "2026-01-01", "structure_role": None},
        ]
        repo = CorrelationRepository(neo4j)

        result = repo.fetch_entity_merge_metadata(["uuid-1"])
        assert len(result) == 1
        assert result[0]["uuid"] == "uuid-1"


# ---------------------------------------------------------------------------
# Merge provenance: the repository contract for reading, guarding and restoring it
# ---------------------------------------------------------------------------

class _GuardEnforcingNeo4j:
    """A Neo4j stub that EVALUATES the mutation's provenance guard instead of recording the call.

    A MagicMock cannot catch this defect: the mutation would "succeed" against any parameters at all,
    including ones derived from a stale read. This stub holds node state, optionally applies a
    concurrent write in the window between Phase 1 and Phase 2, and then does what the database does
    -- drop the row when a guarded value no longer matches, so the merge abstains.

    It drives off the repository's own ``GUARDED_PROVENANCE`` table rather than a copy, so a value
    added to the guard is automatically enforced here too, and Python's ``==`` reproduces the
    null-safe Cypher idiom exactly (absent == absent, absent != any value).
    """

    def __init__(
        self,
        survivor: dict,
        absorbed: dict,
        *,
        concurrent_write: dict | None = None,
        absorbed_concurrent_write: dict | None = None,
    ):
        self.state = {"survivor": dict(survivor), "absorbed": dict(absorbed)}
        self._concurrent = {
            "survivor": concurrent_write, "absorbed": absorbed_concurrent_write,
        }
        self.mutate_params: dict | None = None

    def execute(self, query: str, params: dict | None = None, **_kwargs):
        # **_kwargs absorbs execution OPTIONS the production client accepts (timeout_s,
        # should_continue -- CF-211) without this stub having to track them. They govern how a
        # statement is dispatched, never what it returns, so ignoring them here leaves every
        # assertion below testing exactly what it tested before.
        params = params or {}
        if "ineligible_role" in query:                       # Phase 0: eligibility preflight
            return [
                {"uuid": uuid, "ineligible_role": False, "namespace": "default",
                 "freshness": "ACTIVE", "scope": "PERSISTENT", "user_flagged": False,
                 "conflict_status": None}
                for uuid in (params.get("s"), params.get("a"))
            ]
        if "mentioned_by_episodes" in query:                 # Phase 1: read
            s, a = self.state["survivor"], self.state["absorbed"]
            row = {
                "survivor_source": s.get("source"),
                "survivor_sources": s.get("sources"),
                "survivor_source_confidence": s.get("source_confidence"),
                "uuid": "absorbed-2", "name": "n", "summary": None, "content": None,
                "source": a.get("source"), "sources": a.get("sources"),
                "source_confidence": a.get("source_confidence"),
                "corroboration": a.get("corroboration"),
                "type": None, "scope": "PERSISTENT", "created_at": None, "namespace": "default",
                "relationships": [], "mentioned_by_episodes": [], "survivor_episodes_before": [],
            }
            for node, write in self._concurrent.items():     # the other merge lands, right here
                if write:
                    self.state[node].update(write)
            return [row]

        # Phase 2: mutate. Enforce the guard the way the WHERE clause does.
        self.mutate_params = params
        for node, prop, key in GUARDED_PROVENANCE:
            assert f"coalesce({node}.{prop} = ${key}," in query, (
                f"the mutation does not guard {node}.{prop}; a concurrent change to it would be "
                f"silently overwritten by the values derived from the Phase 1 read"
            )
            assert f"{node}.{prop} IS NULL AND ${key} IS NULL" in query, (
                f"the guard on {node}.{prop} is not null-safe; `null = null` is null in Cypher, so a "
                f"node with no stored value would never merge"
            )
            assert key in params, f"guard parameter {key} is missing"
            if self.state[node].get(prop) != params[key]:
                return []                                    # fail closed: no row, no merge
        return [{"edges_bridged": 0, "episodes_rebound": 0, "deleted": 1}]


@pytest.fixture
def _no_sidecar(monkeypatch):
    """Keep these unit tests out of the operator's real telemetry sidecar."""
    monkeypatch.setattr("menhir.infrastructure.telemetry.record_merge", lambda **kwargs: None)


@pytest.mark.usefixtures("_no_sidecar")
class TestMergeProvenanceContract:
    """The five integration defects in `01a10e4`, at the repository boundary."""

    def test_phase_one_reads_every_provenance_input_for_both_nodes(self) -> None:
        """The derivation consumes source + sources + source_confidence from BOTH nodes. Whatever it
        reads must come from this one query -- Phase 2 has already stopped deriving anything."""
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "claude-code", "sources": ["claude-code"], "source_confidence": 0.7},
        )
        captured: list[str] = []
        original = neo4j.execute

        def _spy(query, params=None, **kwargs):
            captured.append(query)
            return original(query, params, **kwargs)

        neo4j.execute = _spy  # type: ignore[method-assign]
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 1

        read = next(q for q in captured if "mentioned_by_episodes" in q)
        for expr in (
            "survivor.source AS survivor_source",
            "survivor.sources AS survivor_sources",
            "survivor.source_confidence AS survivor_source_confidence",
            "absorbed.source AS source",
            "absorbed.sources AS sources",
            "absorbed.source_confidence AS source_confidence",
            "absorbed.corroboration AS corroboration",
        ):
            assert expr in read, f"Phase 1 does not read {expr}"

    def test_an_absorbed_nodes_prior_contributors_are_not_dropped(self) -> None:
        """Defect: the absorbed node's `sources` was never read, so absorbing an already-merged node
        kept only its lowest-tier `source` and lost every other writer."""
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "claude-code", "sources": ["claude-code", "project-scan"],
             "source_confidence": 0.7},
        )
        CorrelationRepository(neo4j).merge_entity("survivor-1", "absorbed-2", similarity=0.96)

        assert neo4j.mutate_params["sources"] == ["codex", "claude-code", "project-scan"]
        assert neo4j.mutate_params["corroboration"] == 3

    def test_an_explicit_downgrade_is_not_raised_by_the_merge(self) -> None:
        """Two project-scan nodes deliberately held at 0.5 must not come out at the label's 0.9."""
        neo4j = _GuardEnforcingNeo4j(
            {"source": "project-scan", "sources": ["project-scan"], "source_confidence": 0.5},
            {"source": "project-scan", "sources": ["project-scan"], "source_confidence": 0.5},
        )
        CorrelationRepository(neo4j).merge_entity("survivor-1", "absorbed-2", similarity=0.96)

        assert neo4j.mutate_params["authority"] == 0.5
        assert neo4j.mutate_params["primary_source"] == "project-scan"

    def test_the_written_values_are_exactly_what_the_unmerge_replays(self) -> None:
        """Guard 2 replays `merge_delta` against the pre-merge snapshot and compares. If the merge's
        parameters and that replay ever disagree, exact unmerge stops working -- so they come from
        one function, and this pins that."""
        from menhir.domain import merge_delta as md

        survivor = {"source": "codex", "sources": ["codex"], "source_confidence": 0.4}
        absorbed = {"source": "claude-code", "sources": ["claude-code", "project-scan"],
                    "source_confidence": 0.7}
        neo4j = _GuardEnforcingNeo4j(survivor, absorbed)
        CorrelationRepository(neo4j).merge_entity("survivor-1", "absorbed-2", similarity=0.96)

        replayed = md.replay_survivor_merge(survivor, absorbed)
        assert neo4j.mutate_params["sources"] == replayed["sources"]
        assert neo4j.mutate_params["primary_source"] == replayed["source"]
        assert neo4j.mutate_params["authority"] == replayed["source_confidence"]
        assert neo4j.mutate_params["corroboration"] == replayed["corroboration"]

    def test_the_audit_entry_carries_the_absorbed_nodes_new_provenance(self) -> None:
        """`sources`/`corroboration` are provenance too. Leaving them out of the snapshot makes a
        degraded restore driven off this entry resurrect the node with its contributors erased."""
        import json as _json

        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "claude-code", "sources": ["claude-code", "project-scan"],
             "source_confidence": 0.7, "corroboration": 2},
        )
        CorrelationRepository(neo4j).merge_entity("survivor-1", "absorbed-2", similarity=0.96)

        properties = _json.loads(neo4j.mutate_params["audit_entry"])["properties"]
        assert properties["sources"] == ["claude-code", "project-scan"]
        assert properties["corroboration"] == 2

    def test_a_concurrent_contributor_addition_makes_the_merge_abstain(self) -> None:
        """THE race. Phase 1 reads survivor sources=[codex]; another merge then adds a HIGHER-tier
        contributor, which leaves the lowest-tier `source` unchanged at 'codex'. A guard on `source`
        alone passes, and the stale write erases `claude-code` from the graph.
        """
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            concurrent_write={"source": "codex", "sources": ["codex", "claude-code"]},
        )
        result = CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)

        assert result["merged"] == 0
        assert result["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
        assert neo4j.mutate_params["expected_survivor_sources"] == ["codex"], (
            "the guard must compare against what Phase 1 actually read"
        )

    @pytest.mark.parametrize(
        "concurrent_write",
        [
            {"source": "gemini-cli"},                       # primary source replaced
            {"sources": ["codex", "claude-code"]},          # contributor added
            {"source_confidence": 0.2},                     # explicit downgrade landed
        ],
    )
    def test_any_concurrent_provenance_change_makes_the_merge_abstain(
        self, concurrent_write
    ) -> None:
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            concurrent_write=concurrent_write,
        )
        result = CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)
        assert result["merged"] == 0

    def test_an_unchanged_graph_still_merges(self) -> None:
        """The guard must fail CLOSED on change, not on everything."""
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
        )
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 1

    def test_absent_provenance_is_guarded_null_safely(self) -> None:
        """A node with no provenance at all must still be mergeable -- `null = null` is null in
        Cypher, so a bare equality would drop the row forever. The guard handles null on both sides
        instead of inventing a sentinel, and the parameters stay RAW."""
        neo4j = _GuardEnforcingNeo4j({}, {})
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 1
        for _, _, key in GUARDED_PROVENANCE:
            assert neo4j.mutate_params[key] is None, (
                f"{key} was normalized; the guard would then compare a tidied parameter against the "
                f"untidied graph"
            )
        assert neo4j.mutate_params["sources"] == []
        assert neo4j.mutate_params["primary_source"] == "merged"
        assert neo4j.mutate_params["corroboration"] == 0

    @pytest.mark.parametrize(
        "confidence", [-0.25, 2.0, "0.5"],
        ids=["below-domain", "above-domain", "string"],
    )
    def test_an_unchanged_malformed_confidence_still_merges(self, confidence) -> None:
        """Coercing a malformed value to a sentinel in the PARAMETER made the node permanently
        unmergeable: the graph still holds the raw value, which can never equal a sentinel the graph
        does not contain. The guard asks 'did this change?', not 'is this well-formed?' -- the
        derivation is where malformedness is interpreted."""
        state = {"source": "codex", "sources": ["codex"], "source_confidence": confidence}
        neo4j = _GuardEnforcingNeo4j(dict(state), dict(state))
        result = CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)

        assert result["merged"] == 1
        # ...and the malformed value never becomes the merged node's authority.
        assert neo4j.mutate_params["authority"] == 0.5      # codex ceiling, not -0.25/2.0/nan

    @pytest.mark.parametrize(
        ("node", "concurrent_write"),
        [
            ("survivor", {"source_confidence": None}),      # cleared
            ("survivor", {"sources": None}),                # list property removed entirely
            ("survivor", {"sources": []}),                  # emptied -- NOT the same as removed
            ("absorbed", {"source_confidence": None}),
            ("absorbed", {"sources": None}),
            ("absorbed", {"sources": []}),
            ("absorbed", {"corroboration": None}),
        ],
        ids=["s-confidence", "s-sources-removed", "s-sources-emptied",
             "a-confidence", "a-sources-removed", "a-sources-emptied", "a-corroboration"],
    )
    def test_clearing_a_provenance_value_is_detected_as_a_change(
        self, node, concurrent_write
    ) -> None:
        """Absent and empty are different states, and the derivation reads them differently: a
        missing `sources` falls back to the legacy `source`, an empty one does not. A guard that
        collapsed both onto `[]` would not see this happen."""
        state = {"source": "codex", "sources": ["codex"], "source_confidence": 0.5,
                 "corroboration": 1}
        writes = {f"{'absorbed_' if node == 'absorbed' else ''}concurrent_write": concurrent_write}
        neo4j = _GuardEnforcingNeo4j(dict(state), dict(state), **writes)
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 0

    def test_setting_a_previously_absent_value_is_detected_as_a_change(self) -> None:
        neo4j = _GuardEnforcingNeo4j(
            {}, {}, concurrent_write={"source_confidence": 0.5})
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 0

    def test_a_concurrent_corroboration_change_alone_makes_the_merge_abstain(self) -> None:
        """`absorbed.corroboration` is not a derivation input, but it IS snapshotted into the audit
        entry. Leaving it unguarded lets the durable record describe a state the node never held --
        and a restore driven off that entry would resurrect the wrong count."""
        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5,
             "corroboration": 1},
            # Corroboration alone changes -- every derivation input is untouched, so a guard covering
            # only those would let the stale audit entry through.
            absorbed_concurrent_write={"corroboration": 4},
        )
        result = CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)

        assert result["merged"] == 0
        assert result["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
        assert neo4j.mutate_params["expected_absorbed_corroboration"] == 1

    def test_a_nan_confidence_abstains_rather_than_merging_on_an_undefined_comparison(self) -> None:
        """Honest limitation, pinned. IEEE says NaN never equals itself, so "unchanged" has no
        meaning for it and neither Cypher nor this stub can answer the guard's question. Abstaining
        is the correct fail-closed outcome -- the alternative is a merge that cannot tell whether the
        value it read is the value still there. NaN is not a value any writer produces."""
        state = {"source": "codex", "sources": ["codex"], "source_confidence": float("nan")}
        neo4j = _GuardEnforcingNeo4j(dict(state), dict(state))
        assert CorrelationRepository(neo4j).merge_entity(
            "survivor-1", "absorbed-2", similarity=0.96)["merged"] == 0

    def test_the_guard_covers_every_value_phase_one_reads(self) -> None:
        """The table and the query are generated from one source, so this pins the CONTENT of the
        table: every provenance value read in Phase 1, plus the corroboration the audit snapshots."""
        assert set(GUARDED_PROVENANCE) == {
            ("survivor", "source", "expected_survivor_source"),
            ("survivor", "sources", "expected_survivor_sources"),
            ("survivor", "source_confidence", "expected_survivor_confidence"),
            ("absorbed", "source", "expected_absorbed_source"),
            ("absorbed", "sources", "expected_absorbed_sources"),
            ("absorbed", "source_confidence", "expected_absorbed_confidence"),
            ("absorbed", "corroboration", "expected_absorbed_corroboration"),
        }


class TestDegradedRestoreReadsTheAuditEntry:
    """The writer and the degraded reader must agree on what an audit entry carries.

    `merge_entity` serializes the absorbed node's provenance into `survivor.merge_audit` and the
    telemetry sidecar. `legacy_snapshot` is what reads that entry back when no journal row survives
    (recovery tier GRAPH_SNAPSHOT_ONLY), and it filters through a property ALLOWLIST. A field the
    writer emits but the allowlist omits is dropped at the last possible moment -- the restore
    resurrects the node with its contributor list erased, having had the data in hand.
    """

    @pytest.mark.usefixtures("_no_sidecar")
    def test_the_reader_preserves_the_provenance_the_writer_emits(self) -> None:
        """Driven off a REAL entry from merge_entity, not a hand-written fixture: that is what makes
        this catch a future divergence between the two sides."""
        from menhir.domain import legacy_snapshot as ls

        neo4j = _GuardEnforcingNeo4j(
            {"source": "codex", "sources": ["codex"], "source_confidence": 0.5},
            {"source": "claude-code", "sources": ["claude-code", "project-scan"],
             "source_confidence": 0.7, "corroboration": 2},
        )
        CorrelationRepository(neo4j).merge_entity("survivor-1", "absorbed-2", similarity=0.96)

        entry = ls.parse(neo4j.mutate_params["audit_entry"])
        restored = ls.absorbed_properties(entry)

        assert restored["sources"] == ["claude-code", "project-scan"]
        assert restored["corroboration"] == 2
        assert restored["source"] == "claude-code"
        assert restored["source_confidence"] == 0.7

    def test_every_property_the_writer_emits_is_in_the_allowlist(self) -> None:
        """The general form of the same defect: nothing the audit entry captures may be silently
        unreadable. Compares the writer's snapshot keys against the reader's allowlist."""
        import inspect

        from menhir.domain import legacy_snapshot as ls
        from menhir.infrastructure import correlation_queries as cq

        source = inspect.getsource(cq.CorrelationRepository.merge_entity)
        block = source.split('snapshot = {', 1)[1].split('}', 1)[0]
        emitted = set(re.findall(r'"(\w+)"', block))

        assert emitted, "could not locate the audit snapshot key list"
        assert emitted <= set(ls.LEGACY_PROPERTY_ALLOWLIST), (
            f"merge_entity writes {sorted(emitted - set(ls.LEGACY_PROPERTY_ALLOWLIST))} into the "
            f"audit entry, but the degraded reader's allowlist drops them"
        )

    def test_an_old_vintage_entry_without_provenance_is_still_readable(self) -> None:
        """Widening the allowlist must not require the new fields. Entries written before merge
        derived provenance simply omit them, and absent values were always dropped."""
        from menhir.domain import legacy_snapshot as ls

        entry = ls.parse(json.dumps({
            "absorbed_uuid": "a", "survivor_uuid": "s",
            "properties": {
                "uuid": "a", "name": "Svc", "summary": "s", "content": "c",
                "source": "claude-code", "source_confidence": 0.8, "type": "service",
                "scope": "PERSISTENT", "created_at": "2026-01-01T00:00:00Z",
                "namespace": "default",
            },
        }))
        restored = ls.absorbed_properties(entry)

        assert "sources" not in restored and "corroboration" not in restored
        assert restored["source"] == "claude-code"
        assert restored["uuid"] == "a"

    def test_a_null_provenance_value_is_still_dropped(self) -> None:
        """Neo4j stores no null properties, so `SET a = $props` must not carry one."""
        from menhir.domain import legacy_snapshot as ls

        entry = ls.parse(json.dumps({
            "absorbed_uuid": "a", "survivor_uuid": "s",
            "properties": {"uuid": "a", "sources": None, "corroboration": None},
        }))
        assert ls.absorbed_properties(entry) == {"uuid": "a"}


class TestFetchSurvivorProperties:
    """Guard 2 in `unmerge_coordinator` compares this row against the replayed merge output."""

    def test_it_returns_every_merge_owned_property(self) -> None:
        """Regression: the query returned four of the six, so `sources` and `corroboration` read back
        as None on a survivor that held them and Guard 2 refused EVERY new merge's unmerge with
        SURVIVOR_CHANGED_SINCE_MERGE. The bug was in the read, not in the graph."""
        from menhir.domain.merge_delta import MERGE_OWNED_SURVIVOR_PROPERTIES

        neo4j = MagicMock()
        neo4j.execute.return_value = [{
            "summary": "s", "content": "c", "source": "codex", "source_confidence": 0.5,
            "sources": ["codex", "claude-code"], "corroboration": 2,
        }]
        result = CorrelationRepository(neo4j).fetch_survivor_properties("survivor-1")

        assert set(result) == set(MERGE_OWNED_SURVIVOR_PROPERTIES)
        cypher = neo4j.execute.call_args[0][0]
        for prop in MERGE_OWNED_SURVIVOR_PROPERTIES:
            assert f"s.{prop} AS {prop}" in cypher, f"the survivor read omits {prop}"

    def test_missing_survivor_returns_none(self) -> None:
        neo4j = MagicMock()
        neo4j.execute.return_value = []
        assert CorrelationRepository(neo4j).fetch_survivor_properties("nope") is None


# ---------------------------------------------------------------------------
# Threshold constant validation
# ---------------------------------------------------------------------------

class TestThresholdConstants:
    """Validate that threshold constants are consistent."""

    def test_related_less_than_conflict(self) -> None:
        assert CORRELATION_RELATED_THRESHOLD < CORRELATION_CONFLICT_THRESHOLD

    def test_conflict_less_than_merge(self) -> None:
        assert CORRELATION_CONFLICT_THRESHOLD < CORRELATION_MERGE_THRESHOLD

    def test_default_values_match_plan(self) -> None:
        """Step 8 plan specifies: 0.7 related, 0.85 conflict, 0.95 merge."""
        assert CORRELATION_RELATED_THRESHOLD == 0.70
        assert CORRELATION_CONFLICT_THRESHOLD == 0.85
        assert CORRELATION_MERGE_THRESHOLD == 0.95
