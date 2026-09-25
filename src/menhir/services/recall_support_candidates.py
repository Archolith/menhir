"""Candidate-set preparation and post-recall bookkeeping for the recall mixin stack."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter
from typing import Any

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.recall import QueryPreset, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.infrastructure.telemetry import record_mcp_event

logger = logging.getLogger(__name__)


class RecallSupportCandidateMixin:
    async def _resolve_file_context(
        self,
        file_path: str,
        project: str | None = None,
    ) -> list[str]:
        """Resolve file path -> structural neighbors -> cross-linked semantic UUIDs.

        Returns semantic entity UUIDs that are ANCHORED_TO structural entities
        in the file's neighborhood (the file itself + its imports/importers/tests).
        """
        from menhir.infrastructure.structural_anchoring import normalize_to_repo_relative

        # Get known roots for path normalization
        projects = await asyncio.to_thread(self.graph_adapter.list_structure_projects)
        known_roots = [p.get("root_path", "") for p in projects if p.get("root_path")]
        normalized = normalize_to_repo_relative(file_path, known_roots)

        # Resolve project if not provided
        if not project:
            project_names = [p["name"] for p in projects if p.get("name")]
            if not project_names:
                return []
            bulk_result = await asyncio.to_thread(
                self.graph_adapter._structure.resolve_structural_neighbors_bulk,
                project_names, normalized,
            )
            if bulk_result:
                project, structural_neighbors = bulk_result
            else:
                return []
        else:
            structural_neighbors = await asyncio.to_thread(
                self.graph_adapter._structure.resolve_structural_neighbors,
                project, normalized,
            )

        if not structural_neighbors:
            return []

        return await asyncio.to_thread(
            self.graph_adapter.find_cross_linked_semantic_entities, structural_neighbors,
        )

    @staticmethod
    def _pending_fallback_results(
        pending_rows: list[dict[str, object]],
        preset: QueryPreset,
        limit: int,
    ) -> list[ScoredMemory]:
        results: list[ScoredMemory] = []
        for row in pending_rows[:limit]:
            state = str(row.get("processing_state") or ProcessingState.PENDING)
            content = row.get("content")
            breakdown = RelevanceBreakdown(
                semantic_similarity=0.0,
                adjacency_bonus=0.0,
                recency_bonus=0.0,
                prominence_bonus=0.0,
                conflict_bonus=0.0,
                type_boost=0.0,
                preset=preset.value,
                alpha=0.0,
                beta=0.0,
                gamma=0.0,
                delta=0.0,
            )
            results.append(
                ScoredMemory(
                    uuid=str(row.get("uuid") or ""),
                    name=str(row.get("name") or f"Pending memory ({state})"),
                    content=str(content) if content else None,
                    scope=str(row.get("scope") or NodeScope.SESSION),
                    memory_type="EPISODIC_PENDING",
                    final_score=0.0,
                    breakdown=breakdown,
                )
            )
        return results

    async def _wait_for_pending_episodes(
        self,
        query: str,
        limit: int,
        timeout_s: float,
        *,
        namespace: str | None = None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Wait for in-flight episodes to finish; return (visible_pending_rows, entity_uuids).

        ``visible_pending_rows`` are episodes still PENDING/ENRICHING after the wait
        (shown as fallback results). ``entity_uuids`` are entities linked from episodes
        that became READY during the wait (injected into the candidate set).
        """
        if self.ingest_service is None:
            return [], []

        pending_candidates = await asyncio.to_thread(
            self.graph_adapter.fetch_relevant_pending_episodes,
            query,
            limit=3,
            namespace=namespace,
        )
        if not pending_candidates:
            return [], []

        wait_started = perf_counter()
        timed_out = False
        updated_rows_by_uuid: dict[str, dict[str, object]] = {}
        for row in pending_candidates:
            remaining = timeout_s - (perf_counter() - wait_started)
            if remaining <= 0:
                timed_out = True
                break
            state = str(row.get("processing_state") or "")
            if state not in {ProcessingState.PENDING, ProcessingState.ENRICHING}:
                continue
            updated = await self.ingest_service.wait_for_episode_processing(
                str(row.get("uuid") or ""),
                timeout_s=remaining,
            )
            if updated is None:
                timed_out = True
                break
            updated_rows_by_uuid[str(row.get("uuid") or "")] = dict(updated)
            if str(updated.get("processing_state") or "") in {ProcessingState.PENDING, ProcessingState.ENRICHING}:
                timed_out = True
                break

        wait_duration_ms = int((perf_counter() - wait_started) * 1000)
        record_mcp_event(
            kind="background",
            operation="recall_pending_wait",
            payload={
                "query": query,
                "pending_count": len(pending_candidates),
                "timed_out": timed_out,
            },
            result={"wait_duration_ms": wait_duration_ms},
            duration_ms=wait_duration_ms,
            success=not timed_out,
        )

        refreshed_pending_rows: list[dict[str, object]] = []
        for row in pending_candidates:
            row_uuid = str(row.get("uuid") or "")
            refreshed = updated_rows_by_uuid.get(row_uuid)
            if refreshed is None:
                refreshed = await asyncio.to_thread(self.graph_adapter.fetch_episode_processing, row_uuid)
            if refreshed is not None:
                refreshed_pending_rows.append(refreshed)

        visible_pending_rows = [
            row for row in refreshed_pending_rows
            if str(row.get("processing_state") or "") in {ProcessingState.PENDING, ProcessingState.ENRICHING}
        ]
        ready_rows = [
            row for row in refreshed_pending_rows
            if str(row.get("processing_state") or "") == ProcessingState.READY
        ]
        resolved_episode_uuids = [
            str(row.get("resolved_episode_uuid") or "")
            for row in ready_rows
            if row.get("resolved_episode_uuid")
        ]
        inline_linked_entity_uuids = [
            str(uuid)
            for row in ready_rows
            for uuid in row.get("linked_entity_uuids", []) or []
            if uuid
        ]
        # CF-75: one round trip for every resolved episode, not one per episode. The re-ordering
        # below is not decorative -- `dict.fromkeys` downstream dedupes by FIRST occurrence, so the
        # entity order has to stay the serial loop's order or recall's candidate order shifts.
        linked_by_episode = await asyncio.to_thread(
            self.graph_adapter.fetch_linked_entity_uuids_for_episodes, resolved_episode_uuids,
        )
        resolved_linked_entity_uuids: list[str] = []
        for episode_uuid in resolved_episode_uuids:
            resolved_linked_entity_uuids.extend(linked_by_episode.get(episode_uuid, []))
        entity_uuids = list(dict.fromkeys(inline_linked_entity_uuids + resolved_linked_entity_uuids))
        return visible_pending_rows, entity_uuids

    async def _compute_adjacency(
        self,
        eligible_uuids: list[str],
        context_node_ids: list[str] | None,
        namespace: str | None = None,
    ) -> tuple[dict[str, float], dict[tuple[str, str], list[str]]]:
        """Fetch and normalize adjacency scores; return (adjacency_map, edge_index).

        ``adjacency_map`` maps uuid → normalized [0, 1] adjacency weight.
        ``edge_index`` maps (min_uuid, max_uuid) → list of edge uuids for the pair.

        ``namespace``, when set, constrains the adjacency traversal to same-namespace
        edges only (SSOT-04) -- otherwise context/structural edges from a foreign
        namespace could influence ranking even though the initial candidate fetch was
        already namespace-scoped.
        """
        eligible_uuid_set = set(eligible_uuids)
        try:
            adjacency_rows = await asyncio.to_thread(
                self.graph_adapter.fetch_adjacency_pairs,
                eligible_uuids,
                context_node_ids,
                namespace,
            )
        except Exception:
            logger.exception(
                "Recall adjacency fetch failed for %d candidates; continuing without adjacency",
                len(eligible_uuids),
            )
            return {}, {}
        adjacency_map: dict[str, float] = {}
        edge_index: dict[tuple[str, str], list[str]] = {}
        for row in adjacency_rows:
            try:
                source = str(row["source"] or "").strip()
                target = str(row["target"] or "").strip()
                if not source or not target:
                    raise ValueError("adjacency row is missing source or target")
                weight = float(row.get("weight") or 1.0)
                if not math.isfinite(weight):
                    raise ValueError("adjacency weight is not finite")
                edge_uuid = row.get("edge_uuid")
                if source in eligible_uuid_set:
                    adjacency_map[source] = adjacency_map.get(source, 0.0) + weight
                if target in eligible_uuid_set:
                    adjacency_map[target] = adjacency_map.get(target, 0.0) + weight
                if edge_uuid:
                    pair = (min(source, target), max(source, target))
                    edge_index.setdefault(pair, []).append(str(edge_uuid))
            except Exception as exc:
                logger.error(
                    "Recall skipped malformed adjacency row source=%r target=%r edge_uuid=%r: %s: %s",
                    row.get("source"),
                    row.get("target"),
                    row.get("edge_uuid"),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )

        max_adj = max(adjacency_map.values(), default=0.0)
        if max_adj > 0:
            adjacency_map = {k: v / max_adj for k, v in adjacency_map.items()}

        return adjacency_map, edge_index

    async def _post_recall_updates(
        self,
        top_results: list[ScoredMemory],
        metadata_by_uuid: dict[str, Any],
        edge_index: dict[tuple[str, str], list[str]],
    ) -> int:
        """Touch accessed nodes, increment traversed edge weights, schedule rehydration.

        Returns the count of nodes whose last_accessed was updated.
        """
        result_uuids = [r.uuid for r in top_results if r.memory_type != "EPISODIC_PENDING"]
        nodes_touched = await asyncio.to_thread(self.graph_adapter.touch_retrieved_nodes, result_uuids)

        # CF-75: one ratchet query for every traversed edge, not one per edge. Each call was an
        # all-relationship scan, so the serial loop cost N scans of the whole relationship store --
        # 3,150,400 dbHits for 50 edges on a 21k-edge graph, growing with the graph as well as the
        # result set. Dedupe stays here (it decides which edges are traversed at all); the ratchet
        # itself is one round trip.
        result_uuid_set = set(result_uuids)
        traversed_edge_uuids: list[str] = []
        seen_edges: set[str] = set()
        for (a, b), edge_uuids in edge_index.items():
            if a in result_uuid_set and b in result_uuid_set:
                for edge_uuid in edge_uuids:
                    if edge_uuid not in seen_edges:
                        traversed_edge_uuids.append(edge_uuid)
                        seen_edges.add(edge_uuid)
        if traversed_edge_uuids:
            await asyncio.to_thread(
                self.graph_adapter.increment_edge_weights, traversed_edge_uuids,
            )

        if self.lifecycle_service is not None:
            for result in top_results:
                if result.memory_type == "EPISODIC_PENDING":
                    continue
                meta = metadata_by_uuid.get(result.uuid)
                if meta and str(meta.get("freshness") or FreshnessState.ACTIVE) == FreshnessState.COMPRESSED:
                    self._schedule_rehydration(result.uuid)

        return nodes_touched
