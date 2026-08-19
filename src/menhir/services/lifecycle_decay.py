"""Lifecycle decay, compression, deletion, and rehydration operations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

# Optional async callback: (processed, total, current_node_name) -> None
ProgressCallback = Callable[[int, int, str], Awaitable[None]]

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

#: After this many consecutive compression failures the sweep stops calling the LLM. Each call
#: costs up to 242 s of backoff before returning None, and consecutive failures are almost
#: certainly one shared outage rather than N independent ones.
_MAX_CONSECUTIVE_LLM_FAILURES = 3


class LifecycleDecayMixin:
    @staticmethod
    def should_compress(node: dict[str, Any]) -> bool:
        """Determine if an ACTIVE node should transition to COMPRESSED.

        Delegates to the node's MemoryTypePolicy for type-specific thresholds.
        """
        policy = get_policy(str(node.get("type") or "SEMANTIC"))
        return policy.should_compress(node)

    @staticmethod
    def should_delete(node: dict[str, Any]) -> bool:
        """Determine if a COMPRESSED node should transition to GONE.

        HOTFIX 2026-07-03 — GONE transitions DISABLED. The sharpness gate (<0.1) is an RRF rank
        artifact satisfied by ~59% of persistent nodes, and compression does not reset
        last_accessed — so a long-idle node can be compressed on one nightly sweep and
        irreversibly deleted on the next. 115 deletions already occurred under the broken gate.
        Deletion stays off until sharpness is computed on a lawful scale; MemoryTypePolicy
        thresholds are untouched (this wrapper is the decay sweep's single choke point).
        See .agent/reviews/menhir-lifecycle-scale-probe-2026-07-03.md.
        """
        return False

    async def apply_decay(self) -> DecayResult:
        """Run the full decay lifecycle job.

        Phases: sync edge counts, recalculate sharpness, compress, delete with bridging.
        Isolation never authorizes deletion -- the degree-zero orphan-cleanup path was
        removed 2026-07-13 (see the workspace-root artifact
        .agent/plans/menhir-orphan-cleanup-removal.md).
        """
        if self._decay_lock.locked():
            logger.warning("Decay already in progress, skipping")
            return DecayResult(0, 0, 0, 0, 0, 0)

        async with self._decay_lock:
            started = perf_counter()
            result = await self._run_decay()
        duration_ms = int((perf_counter() - started) * 1000)
        record_mcp_event(
            kind="background",
            operation="decay",
            payload={},
            result={
                "edge_counts_synced": result.edge_counts_synced,
                "sharpness_recalculated": result.sharpness_recalculated,
                "compressed": result.compressed,
                "deleted": result.deleted,
                "edges_bridged": result.edges_bridged,
                "orphan_subgraphs_cleaned": result.orphan_subgraphs_cleaned,
            },
            duration_ms=duration_ms,
            success=True,
        )
        logger.info(
            "Decay complete: synced=%d sharpness=%d compressed=%d deleted=%d bridged=%d orphans=%d duration_ms=%d",
            result.edge_counts_synced,
            result.sharpness_recalculated,
            result.compressed,
            result.deleted,
            result.edges_bridged,
            result.orphan_subgraphs_cleaned,
            duration_ms,
        )
        return result

    @staticmethod
    def _compress_content(content: str | None) -> str:
        """Fallback compression used until the LLM path is wired in."""
        text = (content or "").strip()
        if len(text) <= 200:
            return text
        truncated = text[:200]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated.rstrip() + "..."

    async def rehydrate_node(
        self,
        node_uuid: str,
        new_context: str | None = None,
        *,
        source_node_uuid: str | None = None,
        source_episode_uuid: str | None = None,
    ) -> bool:
        """Rehydrate a COMPRESSED node back to ACTIVE.

        If contextful rehydration fails, queue a deferred SQLite action instead
        of silently flipping the node.
        """
        metadata = await asyncio.to_thread(self.graph_adapter.fetch_candidate_metadata, [node_uuid])
        if not metadata:
            return False

        node = metadata[0]
        freshness = str(node.get("freshness") or FreshnessState.ACTIVE)
        if freshness != FreshnessState.COMPRESSED:
            await asyncio.to_thread(self.pending_actions.complete, node_uuid)
            return False

        source_uuid = source_episode_uuid or source_node_uuid
        context_text = str(new_context or "").strip()
        if not context_text:
            updated = await asyncio.to_thread(self.graph_adapter.complete_rehydration, node_uuid)
            if updated:
                await asyncio.to_thread(self.pending_actions.complete, node_uuid)
                record_lifecycle_action(
                    action="rehydrate",
                    node_uuid=node_uuid,
                    trigger="manual",
                    before_freshness=FreshnessState.COMPRESSED,
                    after_freshness=FreshnessState.ACTIVE,
                    llm_used=False,
                )
            return updated

        if self.llm is None:
            await asyncio.to_thread(
                self.pending_actions.upsert,
                node_uuid,
                "rehydrate",
                context=context_text,
                source_uuid=source_uuid,
                failure_reason="no_llm",
            )
            return False

        # Archive-first recovery: attempt to retrieve the original (pre-compression)
        # content from the revision sidecar before falling back to node content.
        # This prevents photocopy-loss from repeated compress/rehydrate cycles.
        archived_content = await asyncio.to_thread(telemetry_store.get_original_content, node_uuid)
        existing_content = archived_content or str(
            node.get("content") or node.get("summary") or node.get("name") or ""
        ).strip()

        # Log which source was used for debugging/audit
        if archived_content:
            logger.debug(
                "Rehydration: using archived original content for node=%s (archive_len=%d)",
                node_uuid,
                len(archived_content),
            )

        if not existing_content:
            await asyncio.to_thread(
                self.pending_actions.upsert,
                node_uuid,
                "rehydrate",
                context=context_text,
                source_uuid=source_uuid,
                failure_reason="missing_existing_content",
            )
            return False

        merged = await self.llm.merge_content(existing_content, context_text)
        if merged is None:
            await asyncio.to_thread(
                self.pending_actions.upsert,
                node_uuid,
                "rehydrate",
                context=context_text,
                source_uuid=source_uuid,
                failure_reason="llm_failed",
            )
            return False

        updated = await asyncio.to_thread(self.graph_adapter.complete_rehydration, node_uuid, merged)
        if updated:
            await asyncio.to_thread(self.pending_actions.complete, node_uuid)
            record_lifecycle_action(
                action="rehydrate",
                node_uuid=node_uuid,
                trigger="manual",
                before_freshness=FreshnessState.COMPRESSED,
                after_freshness=FreshnessState.ACTIVE,
                llm_used=True,
            )
            record_memory_revision(
                node_uuid=node_uuid,
                field="content",
                old_value=existing_content,
                new_value=merged,
                changed_by="consolidation",
            )
        else:
            await asyncio.to_thread(
                self.pending_actions.upsert,
                node_uuid,
                "rehydrate",
                context=context_text,
                source_uuid=source_uuid,
                failure_reason="complete_rehydration_failed",
            )
        return updated

    async def _run_decay(self) -> DecayResult:
        """Internal decay implementation."""
        edge_counts_synced = await asyncio.to_thread(self.graph_adapter.sync_edge_counts)
        sharpness_recalculated = 0
        compressed = 0
        deleted = 0
        edges_bridged = 0

        active_candidates = await asyncio.to_thread(
            self.graph_adapter.fetch_decay_candidates,
            FreshnessState.ACTIVE,
            min_days_since_accessed=_DEFAULT_COMPRESS_DAYS,
            max_edge_count=_DEFAULT_COMPRESS_EDGE_COUNT,
        )

        consecutive_llm_failures = 0
        for candidate in active_candidates:
            uuid = str(candidate.get("uuid") or "")
            query = str(candidate.get("content") or candidate.get("summary") or candidate.get("name") or "")
            similar_count = await self._count_similar_nodes(
                query, exclude_uuid=uuid, namespace=str(candidate.get("namespace") or "default")
            )
            if similar_count < 0:
                logger.warning("Skipping decay sharpness update for %s - similarity search unavailable", uuid)
                continue
            sharpness = self.compute_sharpness(similar_count)
            await asyncio.to_thread(self.graph_adapter.update_sharpness, uuid, sharpness)
            sharpness_recalculated += 1

            candidate["sharpness"] = sharpness
            candidate["last_accessed_days_ago"] = days_ago(candidate.get("last_accessed"))
            if self.should_compress(candidate):
                raw = candidate.get("content") or candidate.get("summary") or ""
                if not raw.strip():
                    # Nothing to compress. Name-only entity nodes (~52% of the
                    # entity layer) carry no content/summary body — their value is
                    # in their edges, and the memory content lives on the episodes
                    # that reference them. Compression only sheds body text, so it
                    # is a no-op here. Skip without calling the LLM and without
                    # recording a pending action: compress_content("") hits the
                    # empty-prompt guard and returns None, which was being
                    # mislabeled failure_reason="llm_failed" and re-selected every
                    # sweep (a persistent, misleading fake-LLM-failure signal).
                    continue
                if self.llm is None:
                    await asyncio.to_thread(self.pending_actions.upsert, uuid, "compress", failure_reason="no_llm")
                    continue
                if consecutive_llm_failures >= _MAX_CONSECUTIVE_LLM_FAILURES:
                    await asyncio.to_thread(self.pending_actions.upsert, uuid, "compress", failure_reason="llm_breaker_open")
                    continue
                summary = await self.llm.compress_content(raw)
                if not summary or not summary.strip():
                    consecutive_llm_failures += 1
                    await asyncio.to_thread(self.pending_actions.upsert, uuid, "compress", failure_reason="llm_failed")
                    continue
                consecutive_llm_failures = 0
                if await asyncio.to_thread(self.graph_adapter.compress_node, uuid, summary):
                    await asyncio.to_thread(self.pending_actions.complete, uuid)
                    compressed += 1
                    record_lifecycle_action(
                        action="compress",
                        node_uuid=uuid,
                        trigger="decay_sweep",
                        before_freshness=FreshnessState.ACTIVE,
                        after_freshness=FreshnessState.COMPRESSED,
                        llm_used=True,
                    )
                    record_memory_revision(
                        node_uuid=uuid,
                        field="content",
                        old_value=raw,
                        new_value=summary,
                        changed_by="decay",
                    )

        delete_candidates = await asyncio.to_thread(
            self.graph_adapter.fetch_decay_candidates,
            FreshnessState.COMPRESSED,
            min_days_since_accessed=_DEFAULT_GONE_DAYS,
            max_edge_count=_DEFAULT_GONE_EDGE_COUNT,
            max_sharpness=_DEFAULT_GONE_SHARPNESS,
        )
        for candidate in delete_candidates:
            candidate["last_accessed_days_ago"] = days_ago(candidate.get("last_accessed"))
            if self.should_delete(candidate):
                del_uuid = str(candidate.get("uuid") or "")
                outcome = await asyncio.to_thread(self.graph_adapter.bridge_and_delete, del_uuid)
                deleted += int(outcome.get("deleted", 0))
                edges_bridged += int(outcome.get("edges_bridged", 0))
                if outcome.get("deleted"):
                    record_lifecycle_action(
                        action="gone",
                        node_uuid=del_uuid,
                        trigger="decay_sweep",
                        before_freshness=FreshnessState.COMPRESSED,
                        after_freshness="gone",
                        llm_used=False,
                    )

        # Degree-zero orphan cleanup was REMOVED (2026-07-13). It inferred physical-deletion
        # eligibility from node isolation alone -- bypassing freshness, memory type, age,
        # sharpness, and the deletion gate -- and destroyed ~24 unrecoverable production
        # memories. A normal decay sweep must never delete a node for being isolated; an
        # isolated node (e.g. a sole neighbour left after bridge_and_delete) is benign.
        # orphan_subgraphs_cleaned is retained as a hard-pinned 0 for output/telemetry/replay
        # compatibility (removing the field is a separate follow-up). Physical deletion, if
        # ever built, goes through an explicit terminal-state reaper -- see
        # .agent/plans/menhir-terminal-reaper.md and the merge/decay lifecycle review (P0).
        orphan_subgraphs_cleaned = 0
        return DecayResult(
            edge_counts_synced=edge_counts_synced,
            sharpness_recalculated=sharpness_recalculated,
            compressed=compressed,
            deleted=deleted,
            edges_bridged=edges_bridged,
            orphan_subgraphs_cleaned=orphan_subgraphs_cleaned,
        )
