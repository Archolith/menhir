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
from menhir.services import auto_mutation_policy as amp

logger = logging.getLogger(__name__)

from menhir.services.lifecycle_models import (
    CONSOLIDATION_BATCH_SIZE,
    DECAY_BATCH_SIZE,
    DEMOTE_TTL_DAYS,
    ORPHAN_MAX_AGE_HOURS,
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SHARPNESS_COSINE_FLOOR,
    SHARPNESS_PROMOTE_THRESHOLD,
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
                "merge_errors": result.merge_errors,
            },
            duration_ms=duration_ms,
            success=True,
        )
        logger.info(
            "Consolidation complete: promoted=%d deleted=%d demoted=%d conflicts=%d skipped_pending=%d orphans=%d merge_errors=%d duration_ms=%d",
            result.promoted,
            result.deleted,
            result.demoted,
            result.conflicts_detected,
            result.skipped_pending,
            result.orphan_episodes_cleaned,
            result.merge_errors,
            duration_ms,
        )
        return result

    async def _expire_demoted_session_nodes(
        self, session_id: str | None, exclude: frozenset[str] = frozenset()
    ) -> int:
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
            self._delete_coordinator().delete_expired_session_nodes,
            session_id=session_id, exclude=exclude,
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

    async def _cleanup_orphan_episodes(self, session_id: str | None) -> int:
        """Orphan-episode sweep behind MENHIR_AUTO_DELETE_MODE: off -> 0, propose -> record, on -> delete."""
        mode = amp.auto_delete_mode()
        if mode == "on":
            return await asyncio.to_thread(self.graph_adapter.cleanup_orphan_episodes, session_id)
        if mode == "propose":
            try:
                uuids = await asyncio.to_thread(
                    self.graph_adapter.fetch_orphan_episode_uuids, session_id
                )
            except Exception:  # noqa: BLE001 -- a proposal read must never abort consolidation
                logger.warning("could not read orphan episodes to propose", exc_info=True)
                return 0
            await asyncio.to_thread(amp.record_proposals, amp.DELETE_PROPOSAL, [
                (u, None, amp.proposal_context(amp.AUTO_DELETE_PROPOSED, "orphan_episode_cleanup"))
                for u in uuids
            ])
            logger.info("auto delete: proposed %d orphan episode(s), none deleted", len(uuids))
        return 0

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
            orphans = await self._cleanup_orphan_episodes(session_id)
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
        merge_error_log: list[str] = []
        # absorbed promotion candidate -> the survivor that now holds its content.
        absorbed_into: dict[str, str] = {}
        for i in range(0, len(promote_candidates_for_conflict_check), CONSOLIDATION_BATCH_SIZE):
            batch = promote_candidates_for_conflict_check[i:i + CONSOLIDATION_BATCH_SIZE]
            batch_conflicts = await self._check_contradictions_batch(
                batch, merge_error_log, absorbed_into=absorbed_into,
            )
            conflicts_detected += batch_conflicts
            # A candidate whose check raised is not promoted unchecked; the next pass retries it.
            failed = set(merge_error_log)
            promote_uuids.extend(str(node["uuid"]) for node in batch if str(node["uuid"]) not in failed)
        # A promotion candidate absorbed by a merge is gone, so promoting its uuid matches nothing.
        # Its content now lives in the survivor: promote that instead, or a SESSION survivor (whose
        # TTL the merge only cleared) could be demoted and TTL-deleted later, taking it along.
        # promote_to_persistent skips a survivor that is already PERSISTENT or protected.
        for survivor_uuid in absorbed_into.values():
            if survivor_uuid not in promote_uuids:
                promote_uuids.append(survivor_uuid)

        # Phase 4: Execute transitions and demote
        # Audit rows follow what the mutation changed (#919), not what was requested: a requested
        # uuid may have been absorbed by a merge above, left SESSION, or be lifecycle-protected.
        promoted_uuids = await asyncio.to_thread(self.graph_adapter.promote_to_persistent, promote_uuids)
        for uuid in promoted_uuids:
            record_memory_revision(
                node_uuid=uuid,
                field="scope",
                old_value=NodeScope.SESSION,
                new_value=NodeScope.PERSISTENT,
                changed_by="consolidation",
            )
        # Only newly demoted nodes: one already in its grace window keeps its TTL and gets no new row.
        demoted_uuids = await asyncio.to_thread(
            self.graph_adapter.set_demote_ttl, demote_uuids, DEMOTE_TTL_DAYS
        )
        for uuid in demoted_uuids:
            record_lifecycle_action(
                action="demote",
                node_uuid=uuid,
                session_id=session_id,
                trigger="consolidation_demote",
            )

        # Phase 4b: TTL-expiry delete AFTER promotion — promotion wins. promote_to_persistent
        # cleared ttl_expires on rescued nodes, so they no longer match the expired query.
        # A candidate whose contradiction check raised was neither promoted nor checked; it must not
        # be TTL-deleted in the same pass either (#938). The next pass decides it.
        deleted = await self._expire_demoted_session_nodes(
            session_id, exclude=frozenset(merge_error_log)
        )

        # Count pending/enriching episodes (informational — they are not consolidated)
        skipped_pending = await asyncio.to_thread(self.graph_adapter.count_pending_episodes, session_id)
        if skipped_pending:
            logger.info("Skipped %d PENDING/ENRICHING episodes (not yet enriched)", skipped_pending)

        # Phase 5: Cleanup orphan episodes
        orphans = await self._cleanup_orphan_episodes(session_id)

        return ConsolidationResult(
            promoted=len(promoted_uuids),
            deleted=deleted,
            conflicts_detected=conflicts_detected,
            skipped_pending=skipped_pending,
            orphan_episodes_cleaned=orphans,
            demoted=len(demoted_uuids),
            merge_errors=len(merge_error_log),
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

        return CorrelationService(self.graph_adapter, self.graphiti_client, llm=self.llm)

    async def record_conflict_pair(
        self, uuid: str, other_uuid: str,
    ) -> tuple[str, int] | None:
        """Queue one classified conflict pair, shared by consolidation and enrichment (#655).

        Returns ``None`` when the pair was reviewed and suppressed within the cooldown,
        else ``set_conflict``'s ``(group_id, updated)``. Callers have already searched
        within the node's namespace, so the pair is same-silo.
        """
        # Skip pairs that were already reviewed and suppressed
        try:
            if await asyncio.to_thread(
                telemetry_store.is_pair_resolved,
                uuid, other_uuid,
                cooldown_days=self.settings.conflict_cooldown_days,
            ):
                return None
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
        return canonical_group_id, updated

    async def _check_contradictions_batch(
        self,
        candidates: list[dict[str, Any]],
        errors: list[str] | None = None,
        *,
        absorbed_into: dict[str, str] | None = None,
    ) -> int:
        """Check a batch of promotion candidates for contradictions with existing PERSISTENT nodes.

        Returns count of conflicts detected. When ``absorbed_into`` is given, each candidate a
        merge absorbed is mapped to the survivor uuid the merge result reports.

        Pair classification (routing, vetoes, judge-gated merge) is delegated to
        CorrelationService.classify_pair (SSOT-03), and the per-node loop to the shared
        ``correlation_service_checks.scan_node`` (#871) -- this method retains only
        lifecycle-specific bookkeeping: the node's namespace (``default`` when unset), the
        pending_llm_review conflict-queue write, telemetry suppression
        (cooldown) checks, and one-conflict-per-node capping.

        Correlation routing (Step 8): scores of 0.70–0.85 receive a RELATES_TO
        edge instead of entering the conflict queue.  Only scores
        >= CORRELATION_CONFLICT_THRESHOLD (0.85) are flagged as conflicts.  These
        are RRF rank scores, not cosines; see CorrelationService.classify_pair.

        Part 2: Judge-gated merge — near-duplicates (>= merge_threshold) route to the
        judge after deterministic vetoes. Merge only on unanimous yes; non-unanimous or
        judge-unavailable → conflict (fail-safe: never merge without confirmation). A confirmed
        duplicate is still not merged unless MENHIR_AUTO_MERGE_MODE is "on"; classify_pair then
        returns ``none`` with a reason and the node's scan ends without a conflict.
        """
        from menhir.services.correlation_service_checks import scan_node

        correlation_service = self._build_correlation_service()
        conflicts = 0

        async def _queue_conflict(cr: Any) -> bool:
            # final_action == "conflict" (native conflict-range score, or a merge proposal blocked
            # by veto/judge falling through to conflict). One conflict per node; a suppressed pair
            # keeps the scan going.
            nonlocal conflicts
            recorded = await self.record_conflict_pair(cr.source_uuid, cr.target_uuid)
            if recorded is None:
                return False
            canonical_group_id, updated = recorded
            if updated > 0:
                conflicts += 1
                logger.info(
                    "Conflict detected: %s <-> %s (similarity=%.3f, group=%s)",
                    cr.source_uuid, cr.target_uuid, cr.similarity, canonical_group_id,
                )
            return True

        for node in candidates:
            uuid = str(node["uuid"])
            query = str(node.get("content") or node.get("name") or "")
            if not query.strip():
                continue

            group_ids = namespace_to_group_ids(str(node.get("namespace") or "default"))
            # The shared scan (#871): a merge ends it, a drop ends it (going on would reach hits
            # this loop never classified; test_drop_ends_the_node_scan), a queued conflict ends it.
            # A failed search or classify_pair is recorded in `errors` and never promoted
            # unchecked (#938, #1008); without `errors` a classify_pair failure raises (contain_failures).
            scan = await scan_node(
                correlation_service.classify_pair, self.graphiti_client, uuid, query,
                group_ids=group_ids, num_results=5, stop_on_drop=True,
                on_conflict=_queue_conflict, errors=errors,
                contain_failures=errors is not None,
            )
            if absorbed_into is not None:
                for cr in scan.results or []:
                    if cr.action == "merged" and cr.target_uuid:
                        absorbed_into[uuid] = str(cr.target_uuid)

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

    async def preview_orphan_recovery(self, max_age_hours: float = ORPHAN_MAX_AGE_HOURS) -> dict[str, Any]:
        """Preview all phases of global orphan recovery without changing graph state.

        The age argument gates consolidation only. TTL expiry and empty-episode cleanup
        retain their independent eligibility rules; a promotion may rescue an expired node.
        Phase counts are therefore not a prediction of exact mutation counts.
        """
        candidates, expired, empty_episodes = await asyncio.gather(
            asyncio.to_thread(self.graph_adapter.fetch_session_entities, None, max_age_hours),
            asyncio.to_thread(self.graph_adapter.fetch_ttl_expired_session_uuids, None),
            asyncio.to_thread(self.graph_adapter.cleanup_orphan_episodes, None, dry_run=True),
        )
        return {
            "dry_run": True,
            "scope": "global",
            "max_age_hours": max_age_hours,
            "session_nodes_found": len(candidates),
            "ttl_expired_nodes_found": len(expired),
            "empty_orphan_episodes_found": empty_episodes,
            "age_rule": "max_age_hours applies to consolidation; TTL expiry and empty-episode cleanup use their own rules",
        }

    # --- Decay lifecycle (M4) ---
