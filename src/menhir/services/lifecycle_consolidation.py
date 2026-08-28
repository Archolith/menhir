"""Lifecycle consolidation, promotion, contradiction, and orphan recovery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from menhir.domain.memory_types import get_policy
from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.utils import days_ago
from menhir.config import MemorySettings
from menhir.infrastructure.cypher import Cypher
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.llm import LLMAdapter

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient, UnavailableLLMAdapter
    from menhir.services.correlation_service import CorrelationService
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.infrastructure.telemetry import record_lifecycle_action, record_memory_revision, record_mcp_event
from menhir.infrastructure.telemetry.store import telemetry_store

logger = logging.getLogger(__name__)

from menhir.services.lifecycle_models import (
    CONSOLIDATION_BATCH_SIZE,
    DECAY_BATCH_SIZE,
    DEMOTE_TTL_DAYS,
    ORPHAN_MAX_AGE_HOURS,
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SHARPNESS_COSINE_FLOOR,
    SHARPNESS_PROMOTE_THRESHOLD,
    SIMILARITY_CONFLICT_THRESHOLD,
    ConsolidationResult,
    DecayResult,
    ProgressCallback,
    _DEFAULT_COMPRESS_DAYS,
    _DEFAULT_COMPRESS_EDGE_COUNT,
    _DEFAULT_GONE_DAYS,
    _DEFAULT_GONE_EDGE_COUNT,
    _DEFAULT_GONE_SHARPNESS,
)

class LifecycleConsolidationMixin:
    @staticmethod
    def compute_sharpness(similar_count: int) -> float:
        """v1 sharpness: uniqueness only (no emotions).

        0 similar = 1.0, 1 similar = 0.5, 4 similar = 0.2, etc.
        """
        return 1.0 / (1.0 + max(0, similar_count))

    async def consolidate_session(
        self,
        session_id: str | None = None,
        *,
        max_age_hours: float = 0,
        on_progress: ProgressCallback | None = None,
    ) -> ConsolidationResult:
        """Run SESSION -> PERSISTENT consolidation.

        Args:
            session_id: Consolidate nodes from this session only. None = all SESSION nodes.
            max_age_hours: If > 0, only consolidate nodes older than this (for orphan recovery).
            on_progress: Optional async callback invoked after each node is processed.
        """
        if self._consolidation_lock.locked():
            logger.warning("Consolidation already in progress, skipping")
            return ConsolidationResult(0, 0, 0, 0, 0)

        async with self._consolidation_lock:
            started = perf_counter()
            result = await self._run_consolidation(session_id, max_age_hours, on_progress)

        duration_ms = int((perf_counter() - started) * 1000)
        record_mcp_event(
            kind="background",
            operation="consolidation",
            payload={
                "session_id": session_id,
                "max_age_hours": max_age_hours,
            },
            result={
                "promoted": result.promoted,
                "deleted": result.deleted,
                "conflicts_detected": result.conflicts_detected,
                "skipped_pending": result.skipped_pending,
                "orphan_episodes_cleaned": result.orphan_episodes_cleaned,
            },
            duration_ms=duration_ms,
            success=True,
        )
        logger.info(
            "Consolidation complete: promoted=%d deleted=%d demoted=%d conflicts=%d skipped_pending=%d orphans=%d duration_ms=%d",
            result.promoted,
            result.deleted,
            result.demoted,
            result.conflicts_detected,
            result.skipped_pending,
            result.orphan_episodes_cleaned,
            duration_ms,
        )
        return result

    async def _expire_demoted_session_nodes(self, session_id: str | None) -> int:
        """Delete demoted SESSION nodes whose grace TTL has expired, through the journaled saga.

        Runs as a SESSION_TTL_DELETE operation (plan Phase 6): a complete snapshot of every target is
        committed as PREPARED before anything is destroyed, and the audit is taken from the mutation's
        RETURN rather than from the candidate list.

        That distinction is the fix. This used to record a lifecycle action for every CANDIDATE before
        running the delete -- but the delete re-filters on scope='SESSION', so a node promoted in the
        race window survived while the audit already claimed it was destroyed. It logged intent and
        called it a record. Now a survivor is reported as `skipped` and never audited as deleted.

        Callers must still run this AFTER promotion so a rescued node -- whose ttl_expires was cleared
        by promote_to_persistent -- is excluded up front.
        """
        result = await asyncio.to_thread(
            self._delete_coordinator().delete_expired_session_nodes, session_id=session_id
        )
        deleted = result.get("deleted") or []
        for node_uuid in deleted:
            record_lifecycle_action(
                action="delete",
                node_uuid=str(node_uuid),
                trigger="demote_ttl_expiry",
            )
        skipped = result.get("skipped") or []
        if skipped:
            logger.info(
                "TTL sweep spared %d node(s) that changed after selection (not deleted): %s",
                len(skipped), skipped,
            )
        orphaned = result.get("newly_unreferenced_evidence") or []
        if orphaned:
            # Reported, never deleted: Evidence ownership is a separate design decision, and
            # inferring deletion from degree zero is the exact mistake that destroyed nodes on
            # 2026-07-12.
            logger.info(
                "TTL sweep left %d Evidence node(s) unreferenced (reported, NOT deleted): %s",
                len(orphaned), orphaned,
            )
        return len(deleted)

    def _delete_coordinator(self):
        """The journaled delete saga, built lazily on the default telemetry sidecar.

        Tests are isolated by the autouse ``isolated_telemetry_db`` conftest fixture, which redirects
        ``MENHIR_MCP_TELEMETRY_DB`` -- the journal resolves its path from that env var at construction,
        so it lands on the per-test throwaway DB, never the operator's real sidecar.
        """
        if getattr(self, "_delete_coord", None) is None:
            from menhir.infrastructure.graph_operations import GraphOperationsJournal
            from menhir.services.delete_coordinator import DeleteCoordinator

            self._delete_coord = DeleteCoordinator(
                graph_adapter=self.graph_adapter, journal=GraphOperationsJournal()
            )
        return self._delete_coord

    async def _run_consolidation(
        self,
        session_id: str | None,
        max_age_hours: float,
        on_progress: ProgressCallback | None = None,
    ) -> ConsolidationResult:
        # Phase 1: Gather SESSION entities
        candidates = await asyncio.to_thread(
            self.graph_adapter.fetch_session_entities,
            session_id=session_id,
            max_age_hours=max_age_hours,
        )

        if not candidates:
            # No promotion candidates, but demoted nodes may still have expired — run the
            # TTL-expiry sweep (promotion cannot rescue anything here, so ordering is moot).
            deleted = await self._expire_demoted_session_nodes(session_id)
            orphans = await asyncio.to_thread(self.graph_adapter.cleanup_orphan_episodes, session_id)
            return ConsolidationResult(
                promoted=0,
                deleted=deleted,
                conflicts_detected=0,
                skipped_pending=0,
                orphan_episodes_cleaned=orphans,
                demoted=0,
            )

        total = len(candidates)

        # Phase 2: Compute promotion signals
        promote_uuids: list[str] = []
        demote_uuids: list[str] = []
        promote_candidates_for_conflict_check: list[dict[str, Any]] = []

        for idx, node in enumerate(candidates):
            uuid = str(node["uuid"])
            node_name = str(node.get("name") or uuid)
            flagged = bool(node.get("user_flagged", False))

            # Auto-promote flagged nodes
            if flagged:
                promote_uuids.append(uuid)
                if on_progress:
                    await on_progress(idx + 1, total, node_name)
                continue

            # Check persistent edge count
            persistent_edges = await asyncio.to_thread(self.graph_adapter.count_persistent_edges, uuid)
            if persistent_edges >= PERSISTENT_EDGE_PROMOTE_THRESHOLD:
                promote_candidates_for_conflict_check.append(node)
                if on_progress:
                    await on_progress(idx + 1, total, node_name)
                continue

            # Compute sharpness via vector similarity
            similar_count = await self._count_similar_nodes(
                str(node.get("name") or node.get("content") or ""),
                exclude_uuid=uuid,
                namespace=str(node.get("namespace") or "default"),
            )
            if similar_count < 0:
                logger.warning("Skipping node %s - similarity search unavailable", uuid)
                if on_progress:
                    await on_progress(idx + 1, total, node_name)
                continue
            sharpness = self.compute_sharpness(similar_count)
            await asyncio.to_thread(self.graph_adapter.update_sharpness, uuid, sharpness)

            if sharpness >= SHARPNESS_PROMOTE_THRESHOLD:
                promote_candidates_for_conflict_check.append(node)
            else:
                # F5 demote-with-TTL: low lawful-cosine sharpness, unflagged, below edge threshold.
                # Start (do not reset) a grace TTL; the node stays SESSION until it expires.
                demote_uuids.append(uuid)

            if on_progress:
                await on_progress(idx + 1, total, node_name)

        # Phase 3: Contradiction detection for promotion candidates
        conflicts_detected = 0
        for i in range(0, len(promote_candidates_for_conflict_check), CONSOLIDATION_BATCH_SIZE):
            batch = promote_candidates_for_conflict_check[i:i + CONSOLIDATION_BATCH_SIZE]
            batch_conflicts = await self._check_contradictions_batch(batch)
            conflicts_detected += batch_conflicts
            promote_uuids.extend(str(node["uuid"]) for node in batch)

        # Phase 4: Execute transitions and demote
        promoted = await asyncio.to_thread(self.graph_adapter.promote_to_persistent, promote_uuids)
        for uuid in promote_uuids:
            record_memory_revision(
                node_uuid=uuid,
                field="scope",
                old_value=NodeScope.SESSION,
                new_value=NodeScope.PERSISTENT,
                changed_by="consolidation",
            )
        demoted = await asyncio.to_thread(self.graph_adapter.set_demote_ttl, demote_uuids, DEMOTE_TTL_DAYS)
        for uuid in demote_uuids:
            record_lifecycle_action(
                action="demote",
                node_uuid=uuid,
                session_id=session_id,
                trigger="consolidation_demote",
            )

        # Phase 4b: TTL-expiry delete AFTER promotion — promotion wins. promote_to_persistent
        # cleared ttl_expires on rescued nodes, so they no longer match the expired query.
        deleted = await self._expire_demoted_session_nodes(session_id)

        # Count pending/enriching episodes (informational — they are not consolidated)
        skipped_pending = await asyncio.to_thread(self.graph_adapter.count_pending_episodes, session_id)
        if skipped_pending:
            logger.info("Skipped %d PENDING/ENRICHING episodes (not yet enriched)", skipped_pending)

        # Phase 5: Cleanup orphan episodes
        orphans = await asyncio.to_thread(self.graph_adapter.cleanup_orphan_episodes, session_id)

        return ConsolidationResult(
            promoted=promoted,
            deleted=deleted,
            conflicts_detected=conflicts_detected,
            skipped_pending=skipped_pending,
            orphan_episodes_cleaned=orphans,
            demoted=demoted,
        )

    async def _count_similar_nodes(
        self,
        query: str,
        *,
        exclude_uuid: str,
        namespace: str | None = None,
    ) -> int:
        """Count DISTINCT entity nodes whose true cosine similarity to query is > SHARPNESS_COSINE_FLOOR (namespace-scoped).

        Delegates to count_similar_by_cosine, which performs cosine-only search, excludes self
        and Episodic nodes, and deduplicates by uuid. Returns -1 if the similarity search is
        unavailable (advisory, not critical).
        """
        if not query.strip():
            return 0

        try:
            return await self.graphiti_client.count_similar_by_cosine(
                query,
                exclude_uuid=exclude_uuid,
                min_cosine=SHARPNESS_COSINE_FLOOR,
                group_ids=namespace_to_group_ids(namespace),
            )
        except Exception:  # degrade gracefully: sharpness is advisory, not critical
            logger.warning("Similarity search failed for sharpness computation", exc_info=True)
            return -1

    def _build_correlation_service(self) -> "CorrelationService":
        """Build the CorrelationService used for pair classification (SSOT-03).

        CorrelationService is the sole owner of routing, deterministic vetoes,
        and judge-gated merge decisions -- this method exists so
        _check_contradictions_batch consumes that classification instead of
        reimplementing it (which previously diverged: this class's own
        inline copy checked only co_mention/anchor_project and silently
        omitted the ineligible_node veto).

        Passes ``self.graph_adapter`` itself as the correlation-repo argument
        rather than reaching into its private ``._correlation`` attribute — the
        adapter now exposes public delegates for every method CorrelationService
        needs (create_related_to_edge, merge_entity, fetch_entity_merge_metadata,
        check_ineligible_node_veto, check_co_mention_veto, check_anchor_project_veto).
        A prior version of this code reached into ``._correlation`` directly,
        which a 2026-07-04 review flagged as fragile (the adapter's Neo4j
        attribute is `neo4j`, not `_neo4j`, and stubs never exercised that path).
        """
        from menhir.services.correlation_service import CorrelationService

        return CorrelationService(
            self.graph_adapter,
            self.graphiti_client,
            llm=self.llm,
        )

    async def _check_contradictions_batch(
        self,
        candidates: list[dict[str, Any]],
    ) -> int:
        """Check a batch of promotion candidates for contradictions with existing PERSISTENT nodes.

        Returns count of conflicts detected.

        Pair classification (routing, vetoes, judge-gated merge) is delegated to
        CorrelationService.classify_pair (SSOT-03) -- this method retains only
        lifecycle-specific bookkeeping: namespace-scoped search, the
        pending_llm_review conflict-queue write, telemetry suppression
        (cooldown) checks, and one-conflict-per-node capping.

        Correlation routing (Step 8): pairs with similarity 0.70–0.85 receive
        a RELATES_TO edge instead of entering the conflict queue.  Only pairs
        >= SIMILARITY_CONFLICT_THRESHOLD (0.85) are flagged as conflicts.

        Part 2: Judge-gated merge — near-duplicates (>= merge_threshold) route to the
        judge after deterministic vetoes. Merge only on unanimous yes; non-unanimous or
        judge-unavailable → conflict (fail-safe: never merge without confirmation).
        """
        correlation_service = self._build_correlation_service()
        conflicts = 0

        for node in candidates:
            uuid = str(node["uuid"])
            query = str(node.get("content") or node.get("name") or "")
            if not query.strip():
                continue

            group_ids = namespace_to_group_ids(str(node.get("namespace") or "default"))
            similar: list[tuple[str, str, float]] = []
            try:
                similar = await self.graphiti_client.search_scored(
                    query, num_results=5, group_ids=group_ids
                )
            except Exception:  # skip node: contradiction check is best-effort
                logger.warning("Contradiction search failed for node=%s", uuid, exc_info=True)
                continue

            for other_uuid, other_name, score in similar:
                if other_uuid == uuid:
                    continue

                final_action, _details = await correlation_service.classify_pair(
                    uuid, other_uuid, score,
                )

                if final_action in ("none", "related"):
                    # "related" already created its RELATES_TO edge inside
                    # classify_pair — don't break, keep checking remaining pairs.
                    continue

                if final_action == "merged":
                    logger.info(
                        "Correlation: MERGED %s absorbed into %s (sim=%.3f, judge-confirmed)",
                        uuid, other_uuid, score,
                    )
                    # Skip conflict flag and continue to next node
                    break

                # final_action == "conflict" (native conflict-range score, or a
                # merge proposal blocked by veto/judge falling through to conflict)

                # Skip pairs that were already reviewed and suppressed
                try:
                    if await asyncio.to_thread(
                        telemetry_store.is_pair_resolved,
                        uuid, other_uuid,
                        cooldown_days=self.settings.conflict_cooldown_days,
                    ):
                        continue
                except Exception:
                    logger.debug("Suppression check failed for %s <-> %s", uuid, other_uuid, exc_info=True)

                # SSOT-08: a claim conflicting with a PROMOTED node is not an
                # ordinary two-sided disagreement to be auto-adjudicated by the
                # symmetric LLM conflict-review voter (confirm_pending_conflicts
                # defaults to status='pending_llm_review') -- the PROMOTED side is
                # ground truth by definition. Route it straight to 'unresolved'
                # (an existing, already-meaningful conflict-pipeline status: LLM
                # confirmed genuine contradiction) so it surfaces for manual
                # operator review instead of being silently voted on.
                initial_status = "pending_llm_review"
                try:
                    pair_metadata = await asyncio.to_thread(
                        self.graph_adapter.fetch_entity_merge_metadata, [uuid, other_uuid],
                    )
                    if any(str(m.get("scope") or "") == "PROMOTED" for m in pair_metadata):
                        initial_status = "unresolved"
                except Exception:
                    logger.debug(
                        "PROMOTED-scope lookup failed for %s <-> %s; defaulting to pending_llm_review",
                        uuid, other_uuid, exc_info=True,
                    )

                # High similarity — flag as a conflict (status depends on whether
                # either side is PROMOTED, see above).
                new_group_id = str(uuid4())
                canonical_group_id, updated = await asyncio.to_thread(
                    self.graph_adapter.set_conflict,
                    uuid, other_uuid, new_group_id,
                    initial_status=initial_status,
                )
                if updated > 0:
                    conflicts += 1
                    logger.info(
                        "Conflict detected: %s <-> %s (similarity=%.3f, group=%s)",
                        uuid,
                        other_uuid,
                        score,
                        canonical_group_id,
                    )
                # Only flag one conflict per node to avoid explosion
                break

        return conflicts

    async def recover_orphans(
        self,
        max_age_hours: float = ORPHAN_MAX_AGE_HOURS,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> ConsolidationResult:
        """Consolidate stale SESSION nodes from crashed/abandoned sessions."""

        logger.info("Running orphan recovery for SESSION nodes older than %.1f hours", max_age_hours)
        return await self.consolidate_session(max_age_hours=max_age_hours, on_progress=on_progress)

    # --- Decay lifecycle (M4) ---
