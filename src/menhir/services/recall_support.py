"""Reusable pre/post-recall operations shared by the recall coordinator."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, field, replace
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.services.ingest_service import IngestService

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.truth.kinds import DIVERSITY_FAMILY as _FRONTIER_DIVERSITY_FAMILY
from menhir.domain.namespace import namespace_to_group_ids, stamped_namespace
from menhir.domain.recall import (
    CandidateData,
    QueryPreset,
    RecallResult,
    RetrievalScoreKind,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
    ScoredMemory,
    TemporalFact,
)
from menhir.domain.retrieval_tuning import (
    SOURCE_PRIORS,
    CandidateSource,
    RetrievalTuningConfig,
)
from menhir.domain.retrieval_trace_models import (
    AssertionShadowRow,
    AssertionShadowTrace,
    FacetShadowRow,
    FacetShadowTrace,
    RelevanceBreakdown,
    RetrievalTrace,
    ScoringTrace,
    ViewReachability,
)
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.services.hybrid_retrieval import FusionLane, hybrid_search, weighted_rrf_multi

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry import record_mcp_event
from menhir.domain.utils import days_ago
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.services.scoring_service import (
    GRAPHITI_RRF_DUAL_METHOD_MAX,
    MIN_SIMILARITY_THRESHOLD,
    ScoringService,
)
from menhir.domain.git_staleness import BeliefCommitContext, derive_structural_staleness
from menhir.services.change_log_provider import CachedGitChangeLog, ChangeLogProvider
from menhir.infrastructure.paths import repo_root_for_project

logger = logging.getLogger(__name__)



from menhir.services.recall_policies import (
    FILE_LINKED_BASELINE_SIMILARITY,
    PENDING_ENTITY_SIMILARITY,
    _authority_contributors,
    _belief_markers_from_facts,
    _blend_oracle_order,
    _build_temporal_facts,
    _filter_to_current_beliefs,
    _frontier_trace_enabled,
    _oracle_similarity,
    _query_wants_history,
    _repo_path_for,
    _select_candidate_content,
    _staleness_evidence_for,
)


class RecallSupportMixin:
    async def shutdown(self) -> None:
        """Cancel and await all pending background rehydration tasks."""

        tasks = list(self._rehydration_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._rehydration_tasks.clear()

    def _track_rehydration_task(self, task: asyncio.Task[None]) -> None:
        """Retain and cleanup background rehydration tasks."""
        self._rehydration_tasks.add(task)
        task.add_done_callback(self._rehydration_tasks.discard)

    def _schedule_rehydration(self, node_uuid: str, *, source_node_uuid: str | None = None) -> None:
        """Start supervised fire-and-forget rehydration."""
        task = asyncio.create_task(
            self._fire_rehydration(node_uuid, source_node_uuid=source_node_uuid)
        )
        self._track_rehydration_task(task)

    async def _fire_rehydration(self, node_uuid: str, *, source_node_uuid: str | None = None) -> None:
        """Supervised background rehydration for a COMPRESSED node."""
        if self.lifecycle_service is None:
            return

        started = perf_counter()
        try:
            rehydrated = await self.lifecycle_service.rehydrate_node(
                node_uuid,
                new_context=None,
                source_node_uuid=source_node_uuid,
            )
            duration_ms = int((perf_counter() - started) * 1000)
            record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": source_node_uuid,
                    "trigger": "retrieval",
                },
                result={"rehydrated": rehydrated},
                duration_ms=duration_ms,
                success=rehydrated,
            )
        except Exception:  # background task: log and record, never propagate
            duration_ms = int((perf_counter() - started) * 1000)
            logger.exception("Rehydration failed for node=%s", node_uuid)
            record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": source_node_uuid,
                    "trigger": "retrieval",
                },
                result={"error": "rehydration_failed"},
                duration_ms=duration_ms,
                success=False,
            )

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
            partial(
                self.graph_adapter.fetch_relevant_pending_episodes,
                query,
                limit=3,
                namespace=namespace,
            ),
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
        resolved_linked_entity_uuids: list[str] = []
        for episode_uuid in resolved_episode_uuids:
            uuids = await asyncio.to_thread(
                self.graph_adapter.fetch_linked_entity_uuids_for_episode, episode_uuid,
            )
            resolved_linked_entity_uuids.extend(uuids)
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

        result_uuid_set = set(result_uuids)
        seen_edges: set[str] = set()
        for (a, b), edge_uuids in edge_index.items():
            if a in result_uuid_set and b in result_uuid_set:
                for edge_uuid in edge_uuids:
                    if edge_uuid not in seen_edges:
                        await asyncio.to_thread(self.graph_adapter.increment_edge_weight, edge_uuid)
                        seen_edges.add(edge_uuid)

        if self.lifecycle_service is not None:
            for result in top_results:
                if result.memory_type == "EPISODIC_PENDING":
                    continue
                meta = metadata_by_uuid.get(result.uuid)
                if meta and str(meta.get("freshness") or FreshnessState.ACTIVE) == FreshnessState.COMPRESSED:
                    self._schedule_rehydration(result.uuid)

        return nodes_touched

    async def _run_assertion_shadow(
        self,
        query: str,
        namespace: str | None,
        candidate_inputs: list[dict[str, object]],
        metadata_by_uuid: dict[str, Any],
        query_project: str | None = None,
        tuning: RetrievalTuningConfig | None = None,
    ) -> AssertionShadowTrace:
        """Observe-only oracle/warden pass over the recall candidate set.

        SHADOW means: this records what the AssertionPipeline *would* have admitted /
        flagged / refused and how the oracle combiner *would* have ranked the
        candidates — it never changes the recall results, never writes the graph, and
        never raises into the recall path (the caller wraps it). Its only effect is the
        :class:`AssertionShadowTrace` attached to the retrieval trace, so the frontier
        oracle stack can be measured against the shipped ScoringService ranking on the
        live graph before any active wiring.

        The candidate ``metadata`` is the prefetched ``fetch_candidate_metadata`` snapshot
        plus ``evidence_kinds`` and ``project`` derived by ``_attach_frontier_metadata``
        (SUPPORTED_BY :Evidence, structural ANCHORED_TO project, episode source). ``created_at``
        + ``evidence_kinds`` give Temporal/Evidence real anchors; the candidate ``project`` +
        the query's ``file_context_project`` give the ScopeOracle a real project axis. Still
        absent by data-model limit: ``repo``/``branch`` are not modeled anywhere;
        ``valid_at``/``expired_at`` are graphiti EDGE bitemporal; and ``artifact_type``/
        ``anchors`` live on gated L4 ``Artifact`` nodes. So Scope runs on project+namespace
        only and Intent runs partial — recorded in ``note``.
        """
        from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
        from menhir.domain.oracles import CandidateMemory, QueryContext
        from menhir.services.assertion_pipeline import AssertionPipeline

        cmems = []
        for c in candidate_inputs:
            md = dict(metadata_by_uuid.get(str(c["uuid"]), {}))
            md["similarity"] = _oracle_similarity(
                float(c.get("similarity") or 0.0),
                c.get("retrieval_score_kind"),
            )
            cmems.append(CandidateMemory(
                id=str(c["uuid"]),
                content=str(c.get("content") or c.get("name") or ""),
                metadata=md,
            ))
        qctx = QueryContext(text=query, namespace=namespace, project=query_project)
        pipeline = AssertionPipeline(
            LogSpaceOracleCombiner(),
            auto_intent=tuning.enable_intent_lens if tuning else False,
            contradiction_interrupt=tuning.enable_contradiction_interrupt if tuning else False,
            belief_gate=tuning.enable_belief_gate if tuning else False,
            evidence_anchor=tuning.enable_evidence_anchor if tuning else True,
        )
        resolved_lens = pipeline._resolve_intent(qctx).intent
        outcome = await pipeline.run(qctx, cmems)
        rows = [
            AssertionShadowRow(
                candidate_id=r.candidate_id,
                rank=r.rank,
                decision=r.decision.value,
                score=r.score,
                label=r.label,
            )
            for r in outcome.ranked
        ]
        return AssertionShadowTrace(
            intent=resolved_lens,
            admitted=len(outcome.admitted),
            flagged=len(outcome.flagged),
            refused=len(outcome.refused),
            rows=rows,
            note=(
                "observe-only: results unchanged. created_at feeds Temporal; evidence_kinds "
                "(SUPPORTED_BY/ANCHORED_TO/episode-source) feeds Evidence; project "
                "(ANCHORED_TO structure_project vs query file_context_project) + namespace feed "
                "Scope. Still absent: repo/branch (not modeled), valid_at/expired_at (edge "
                "bitemporal), artifact_type/anchors (gated L4) -- so Scope is project+namespace "
                "only and Intent runs partial."
            ),
        )

    async def _run_facet_pass(
        self,
        query: str,
        namespace: str | None,
        eligible_uuids: list[str],
        *,
        query_project: str | None = None,
        active: bool = False,
    ) -> FacetShadowTrace:
        """FACET candidate-generation pass over the recall pool.

        By default this is the observe-only shadow. With ``active=True`` the caller
        fuses the returned order into candidate ranking. One bounded bulk graph query
        derives facets from ANCHORED_TO/DEFINES + metadata; scope/stale discipline is
        deferred to the warden chain.
        """
        from menhir.domain.facet_candidate_source import FacetCandidateSource
        from menhir.domain.facet_derivation import derive_facets
        from menhir.domain.facets import FacetedQuery

        q_facets = derive_facets(content=query, project=query_project, namespace=namespace)
        query_pairs = sorted(f"{f}={v}" for f, v in q_facets.discrete_pairs())
        neo4j = getattr(self.graph_adapter, "neo4j", None)
        if neo4j is None:  # no graph reader (e.g. stub adapter) -> empty, never crash
            return FacetShadowTrace(
                pool=len(eligible_uuids), candidates=0, query_facets=query_pairs, rows=[],
                note=(
                    "active facet unavailable: no Neo4j reader; base ranking retained."
                    if active
                    else "observe-only: no Neo4j reader on the graph adapter; "
                    "facet shadow skipped."
                ),
            )

        from menhir.infrastructure.facet_graph_reader import Neo4jFacetGraphReader

        source = FacetCandidateSource(Neo4jFacetGraphReader(neo4j))
        explanations = await asyncio.to_thread(
            source.contribute, FacetedQuery(facets=q_facets), list(eligible_uuids)
        )
        rows = [
            FacetShadowRow(
                candidate_id=e.memory_id, rank=e.rank, score=e.score,
                convergence=e.convergence, matched_required=e.matched_required,
            )
            for e in explanations
        ]
        return FacetShadowTrace(
            pool=len(eligible_uuids),
            candidates=len(explanations),
            query_facets=query_pairs,
            rows=rows,
            note=(
                "active: FACET overlap + meet-point convergence fused into candidate ranking; "
                "scope/stale discipline deferred to the warden chain."
                if active
                else "observe-only: FACET candidate generation over the recall pool by facet "
                "overlap + meet-point convergence; scope/stale discipline deferred to the "
                "warden chain. Results unchanged."
            ),
        )

    async def _apply_frontier(
        self,
        query: str,
        namespace: str | None,
        scored: list[ScoredMemory],
        metadata_by_uuid: dict[str, Any],
        tuning: RetrievalTuningConfig,
        query_project: str | None = None,
    ) -> tuple[list[ScoredMemory], str | None]:
        """Apply the ACTIVE frontier portions to the post-floor survivors.

        Replaces the ScoringService order with the oracle combiner order
        (``enable_oracle_ranking``) and/or applies the warden gate
        (``enable_warden_gate``: drop REFUSED, label FLAGGED). The source-aware floor has
        already run inside ScoringService, so junk is gone either way — this only reorders
        and gates the survivors. ``enable_intent_lens`` selects the temporal lens fed to the
        oracle/warden path (no effect on its own).

        Unlike the observe-only shadow, this CHANGES results, so it must fail safe: any
        error degrades to the input (ScoringService) order — never breaks recall. Returns
        ``(results, note)``.
        """
        from dataclasses import replace

        from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
        from menhir.domain.oracles import CandidateMemory, QueryContext
        from menhir.domain.diversity import diversify
        from menhir.services.assertion_pipeline import AssertionPipeline

        def _family_for(uuid: str) -> str:
            kinds = metadata_by_uuid.get(uuid, {}).get("evidence_kinds") or ()
            for k in kinds:
                fam = _FRONTIER_DIVERSITY_FAMILY.get(str(k))
                if fam:
                    return fam
            return "semantic"

        try:
            cmems = []
            for s in scored:
                md = dict(metadata_by_uuid.get(s.uuid, {}))
                # Inject ScoringService's retrieval relevance on SemanticOracle's [0, 1]
                # contract. The source value is RRF, not cosine, for normal graph search.
                md["similarity"] = _oracle_similarity(
                    s.breakdown.semantic_similarity,
                    s.retrieval_score_kind,
                )
                cmems.append(CandidateMemory(id=s.uuid, content=str(s.content or s.name or ""), metadata=md))
            qctx = QueryContext(text=query, namespace=namespace, project=query_project)
            pipeline = AssertionPipeline(
                LogSpaceOracleCombiner(), auto_intent=tuning.enable_intent_lens,
                contradiction_interrupt=tuning.enable_contradiction_interrupt,
                belief_gate=tuning.enable_belief_gate,
                evidence_anchor=tuning.enable_evidence_anchor,
            )
            outcome = await pipeline.run(qctx, cmems)

            if _frontier_trace_enabled():
                import collections
                breakdown = collections.Counter(
                    f"{r.decision.value}:{r.reason}" for r in outcome.ranked
                )
                logger.warning(
                    "FRONTIER_TRACE ns=%s query=%r cands=%d admitted=%d flagged=%d refused=%d breakdown=%s",
                    namespace, query[:60], len(cmems), len(outcome.admitted),
                    len(outcome.flagged), len(outcome.refused), dict(breakdown),
                )

            result = scored
            if tuning.enable_oracle_ranking:
                rank_of = {r.candidate_id: r.rank for r in outcome.ranked}
                result = _blend_oracle_order(
                    result,
                    rank_of,
                    oracle_weight=tuning.oracle_rank_weight,
                )
            if tuning.enable_diversity_gate:
                result = diversify(result, family_of=lambda s: _family_for(s.uuid))
            if tuning.enable_warden_gate:
                refused = {r.candidate_id for r in outcome.refused}
                label_of = {r.candidate_id: r.label for r in outcome.flagged}
                result = [
                    replace(s, warden_label=label_of[s.uuid]) if s.uuid in label_of else s
                    for s in result
                    if s.uuid not in refused
                ]
            portions = [
                p for p, on in (
                    ("oracle_ranking", tuning.enable_oracle_ranking),
                    ("warden_gate", tuning.enable_warden_gate),
                    ("diversity_gate", tuning.enable_diversity_gate),
                    ("intent_lens", tuning.enable_intent_lens),
                    ("belief_gate", tuning.enable_belief_gate),
                ) if on
            ]
            note = ("frontier: " + ",".join(portions)) if portions else None
            # belief_gate only ADDS CurrentnessWarden to the chain; warden_gate is the master
            # switch that APPLIES the chain's verdicts (drop REFUSED / label FLAGGED). With
            # belief_gate on but warden_gate off, those verdicts are computed and discarded —
            # warn so the gate is not silently inert.
            if tuning.enable_belief_gate and not tuning.enable_warden_gate:
                warn = (
                    "belief_gate has no effect without warden_gate "
                    "(belief verdicts computed but not applied)"
                )
                logger.warning("recall frontier: %s query=%r", warn, query[:60])
                note = f"{note} | {warn}" if note else warn
            return result, note
        except Exception:  # active path: degrade to the old order, never break recall
            logger.exception(
                "Frontier apply failed query=%r -> degrading to ScoringService order", query[:60]
            )
            record_mcp_event(
                kind="background",
                operation="frontier_apply",
                payload={"query": query[:60]},
                result={"error": "frontier_apply_failed"},
                success=False,
            )
            return scored, None

    async def _attach_frontier_metadata(
        self,
        uuids: list[str],
        metadata_by_uuid: dict[str, Any],
    ) -> None:
        """Merge DERIVED ``evidence_kinds`` and ``project`` into candidate metadata.

        Entity nodes store neither field; both are derived from graph provenance:
          - ``evidence_kinds`` from SUPPORTED_BY :Evidence, a ``file`` anchor when the
            candidate is structurally ANCHORED_TO code, and MENTIONS-ing episode sources
            mapped via ``evidence_kind_for_source``.
          - ``project`` from the structural anchor's ``structure_project``, but ONLY when all
            anchors agree on a single project (a memory spanning projects is left unscoped so
            the ScopeOracle stays permissive rather than inventing a false conflict).
        Feeds both the warden gate and the shadow (they read these metadata keys). Best-effort:
        a fetch failure leaves both absent (warden treats as unanchored; scope stays unknown)
        and never breaks recall."""
        from menhir.domain.self_reinforcement import evidence_kind_for_source

        try:
            rows = await asyncio.to_thread(
                self.graph_adapter.fetch_candidate_provenance, uuids
            )
        except Exception:
            logger.exception("Provenance fetch failed; candidates treated as unanchored/unscoped")
            return
        for row in rows:
            try:
                uuid = str(row.get("uuid") or "").strip()
                if not uuid:
                    raise ValueError("provenance row has no uuid")
                kinds: set[str] = {
                    str(k) for k in (row.get("evidence_node_kinds") or []) if k
                }
                anchor_projects = {
                    str(p) for p in (row.get("anchor_projects") or []) if p
                }
                if anchor_projects:
                    kinds.add("file")  # anchored to real code == a file anchor
                for src in row.get("episode_sources") or []:
                    if src is not None:
                        kinds.add(evidence_kind_for_source(str(src)))
                meta = metadata_by_uuid.get(uuid)
                if meta is not None:
                    meta["evidence_kinds"] = tuple(sorted(kinds))
                    if len(anchor_projects) == 1:
                        meta["project"] = next(iter(anchor_projects))
                    anchor_paths = [
                        str(p) for p in (row.get("anchor_paths") or []) if p
                    ]
                    if anchor_paths:
                        meta["anchor_paths"] = tuple(anchor_paths)
                    if len(anchor_projects) == 1:
                        meta["anchor_project"] = next(iter(anchor_projects))
            except Exception as exc:
                logger.error(
                    "Recall skipped malformed provenance row uuid=%r keys=%s: %s: %s",
                    row.get("uuid"),
                    sorted(str(key) for key in row),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )

    def _plan_view_authority_suppression(
        self, query: str, namespace: str, candidate_inputs: list[dict[str, object]]
    ) -> frozenset[str]:
        """Provenance-linked current-state View suppression (Step 7 canary).

        Returns the candidate uuids a current scalar_state View is authorised to suppress: empty
        unless an EXPLICIT current-state query has provenance-linked stale predecessors that pass all
        six authority gates. The candidate uuids passed here are RECALL candidate `:Entity` semantic-
        memory nodes; `suppressible_provenance` bridges each to the typed-assertion log via its shared
        source episode `(:Episodic)-[:MENTIONS]->(:Entity)` and returns suppression in that SAME entity
        space (Step 7c fix: the join was formerly against `TypedAssertion.episode_uuid`, a disjoint
        uuid space, so it matched nothing and the gate never fired). Read-only; never mutates the graph."""
        from menhir.domain.scalar_view_authority import QueryIntent
        from menhir.domain.scalar_view_suppression import (
            authority_query_intent,
            plan_view_suppression,
        )
        from menhir.infrastructure.audit_trail import RECALL as _audit
        from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

        # Audit every decision point so a replay can tell WHY suppression did or did not fire:
        # non-current intent vs no suppressible provenance surfaced (a "nothing to suppress" quirk)
        # vs a gate that advisory'd on a surfaced row (a real gap). Behavior-neutral, toggle-gated.
        _audit.begin()
        uuids = [str(c["uuid"]) for c in candidate_inputs]
        intent = authority_query_intent(query)
        _audit.audit("intent", intent.value, namespace=namespace,
                     details={"query": query[:120], "candidates": len(uuids), "candidate_uuids": uuids})
        if intent is not QueryIntent.CURRENT_STATE:
            _audit.audit("skip", "not_current_state", namespace=namespace)
            return frozenset()
        neo4j = getattr(self.graph_adapter, "neo4j", None)
        if neo4j is None:
            _audit.audit("skip", "no_neo4j", namespace=namespace)
            return frozenset()
        rows = TypedAssertionRepository(neo4j).suppressible_provenance(
            namespace=namespace, candidate_uuids=uuids
        )
        _audit.audit("provenance", "fetched", namespace=namespace,
                     details={"row_count": len(rows),
                              "row_candidate_uuids": [str(r.get("candidate_uuid")) for r in rows]})
        if not rows:
            # No surfaced candidate is the source episode of a superseded predecessor for a current
            # View -> nothing suppressible was surfaced (the "quirk" branch, not a gate failure).
            _audit.audit("result", "nothing_suppressible", namespace=namespace,
                         details={"candidates": len(uuids)})
            return frozenset()
        plan = plan_view_suppression(query, rows, uuids)
        # Per-row gate verdict: an advisory here on a surfaced row is where a real gap would show.
        for d in plan.decisions:
            _audit.audit("gate", d.outcome, namespace=namespace,
                         details={"candidate_uuid": d.candidate_uuid, "attribute": d.slot_attribute,
                                  "gate": d.gate, "reason": d.reason})
        _audit.audit("result", "suppressed" if plan.any_suppressed else "no_suppression",
                     namespace=namespace,
                     details={"suppressed": sorted(plan.suppressed_uuids), "rows": len(rows)})
        if plan.any_suppressed:
            logger.info(
                "View authority suppressed %d candidate(s) for current-state query=%r: %s",
                len(plan.suppressed_uuids),
                query[:60],
                sorted(plan.suppressed_uuids),
            )
        return plan.suppressed_uuids
