"""Candidate acquisition, scoring, enrichment, and result assembly for recall."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.services.ingest_service import IngestService

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.truth.kinds import DIVERSITY_FAMILY as _FRONTIER_DIVERSITY_FAMILY
from menhir.domain.namespace import namespace_to_group_ids, stamped_namespace
from menhir.domain.self_identity import self_uuid_for_namespace
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

# Degradation is deliberate: a slow or unavailable graph must not fail recall. But a programming
# error raised by a call whose signature we control is a bug, not an outage -- a stale signature
# once surfaced as a wrong answer (swallowed into `assert 0 == 1`) instead of an error. Split them.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, KeyError)


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
    _render_scalar_history_content,
    _repo_path_for,
    _select_candidate_content,
    _staleness_evidence_for,
)

from menhir.services.event_history_authority import event_authority_for_query
from menhir.services.event_history_recall import classify_event_query

from menhir.services.recall_pipeline_observation import _run_scalar_lanes
from menhir.services.recall_pipeline_results import (
    _attach_frontier_pool_metadata,
    _apply_frontier_portions,
    _attach_stale_verifications,
    _build_candidate_data,
    _compose_recall_note,
    _empty_pool_result,
    _enrich_temporal_facts,
    _inject_standalone_fact_edges,
    _run_post_recall_updates,
    _score_candidates,
)
from menhir.services.recall_pipeline_scalar_authority import _projection_is_recall_eligible
from menhir.services.recall_pipeline_search import _generate_candidates
from menhir.services.recall_pipeline_trace import _build_retrieval_trace


async def run_recall(
    service: Any,
    query: str,
    *,
    preset: QueryPreset = QueryPreset.KNOWLEDGE,
    limit: int = 10,
    candidate_k: int = 50,
    context_node_ids: list[str] | None = None,
    include_session: bool = False,
    session_id: str | None = None,
    include_superseded: bool = False,
    wait_for_pending: bool = False,
    pending_wait_timeout_s: float = 3.0,
    file_context: str | None = None,
    file_context_project: str | None = None,
    namespace: str | None = None,
    include_invalidated: bool = False,
    tuning: RetrievalTuningConfig | None = None,
    trace: bool = False,
    update_access: bool = True,
) -> RecallResult:
    # update_access=False makes recall a pure read: no last_accessed touches, no
    # edge-weight reinforcement, no rehydration scheduling. For measurement probes
    # (view-entropy) that must not reinforce the very nodes they measure.
    _t_total = perf_counter()
    _t_phases: dict[str, int] = {}
    tuning = tuning or RetrievalTuningConfig()
    authority_layer: list[ScalarAuthorityVerdict] = []

    # --- Pending episode wait ---
    visible_pending_rows: list[dict[str, object]] = []
    pending_entity_uuids: list[str] = []
    if wait_for_pending:
        _t = perf_counter()
        try:
            visible_pending_rows, pending_entity_uuids = (
                await service._wait_for_pending_episodes(
                    query, limit, pending_wait_timeout_s, namespace=namespace
                )
            )
        except _PROGRAMMING_ERRORS:
            raise
        except Exception:
            logger.exception(
                "Pending-episode wait failed query=%r; continuing with normal recall",
                query[:60],
            )
        _t_phases["pending_wait"] = int((perf_counter() - _t) * 1000)

    # --- Phase 1: candidate generation (see recall_pipeline_search._generate_candidates) ---
    pool, early_result = await _generate_candidates(
        service, query, preset, candidate_k, limit, namespace, tuning, trace,
        visible_pending_rows, pending_entity_uuids, file_context, file_context_project,
        _t_phases,
    )
    if early_result is not None:
        return early_result
    search_error = pool.search_error
    candidate_uuids = pool.candidate_uuids
    similarity_map = pool.similarity_map
    source_map = pool.source_map
    contributing_source_map = pool.contributing_source_map
    score_kind_map = pool.score_kind_map
    rank_shadow = pool.rank_shadow
    content_cosine_map = pool.content_cosine_map

    # --- Metadata fetch + filter (before adjacency, so hidden nodes don't affect ranking) ---
    _t = perf_counter()
    metadata_rows = await asyncio.to_thread(service.graph_adapter.fetch_candidate_metadata, candidate_uuids)
    metadata_by_uuid: dict[str, dict[str, object]] = {}
    for row in metadata_rows:
        uuid = str(row.get("uuid") or "").strip()
        if not uuid:
            logger.error(
                "Recall skipped malformed metadata row with no uuid; keys=%s",
                sorted(str(key) for key in row),
            )
            continue
        metadata_by_uuid[uuid] = row

    candidate_inputs: list[dict[str, object]] = []
    for uuid in candidate_uuids:
        meta = metadata_by_uuid.get(uuid)
        if meta is None:
            continue
        # Structural graph nodes (project-scan Directory/File/Project entities) carry a
        # structure_role. They exist for query_structure, never semantic recall, but leak in
        # here via BM25 token collisions (e.g. "rules" -> the .continue/rules directory node).
        # Drop them before scoring, mirroring fetch_recent_memories' structural exclusion.
        if meta.get("structure_role") is not None:
            continue
        scope = str(meta.get("scope") or NodeScope.SESSION)
        freshness = str(meta.get("freshness") or FreshnessState.ACTIVE)
        # CANDIDATE is the human-review tier: never recalled until approved
        # (which promotes it to PERSISTENT). This is the load-bearing guarantee
        # of staged review - candidates must not influence ranking or results.
        if scope == NodeScope.CANDIDATE:
            continue
        if freshness == FreshnessState.GONE:
            continue
        if scope == NodeScope.SESSION:
            if not include_session:
                continue
            # When the caller identifies a session, SESSION scope is an ownership
            # boundary, not a broad opt-in to every fresh node in the namespace.
            # Missing owner stamps fail closed for an identified caller.
            if session_id is not None and str(meta.get("session_id") or "") != session_id:
                continue
        # Materialized Views have a fail-closed context contract. Historical/debug inspection uses
        # direct getters and operator listings; ``include_superseded`` must not turn ordinary recall
        # into an inspection API. Missing lifecycle stamps, OPERATOR audience, retirement, or a
        # receipt/MENTIONS mismatch therefore excludes the View unconditionally. Ordinary memories
        # have ``is_view`` unset/false and pass through untouched.
        is_view = bool(meta.get("is_view"))
        is_superseded_view = is_view and meta.get("view_current") is not True
        if is_view and (
            meta.get("view_class") != "FACT"
            or meta.get("view_audience") != "RECALL"
            or meta.get("view_current") is not True
            or bool(meta.get("retired"))
            or not bool(meta.get("view_provenance_live"))
        ):
            continue
        # scalar_history exclusion: when the feature flag is OFF, stored scalar_history
        # Entities must be excluded from generic recall — not merely omitted from the
        # dedicated lane. This makes rollback real and prevents an old View from leaking
        # through vector/entity retrieval.
        if meta.get("view_kind") == "scalar_history" and not service.scalar_history_enabled:
            continue
        # Defense-in-depth: if namespace is explicitly set, filter candidates by namespace.
        # The candidate's metadata namespace property (or "default" if missing/None)
        # must match the target namespace.
        if namespace is not None:
            candidate_namespace = meta.get("namespace") or stamped_namespace(None)
            target_namespace = stamped_namespace(namespace)
            if candidate_namespace != target_namespace:
                continue
        try:
            edge_count = int(meta.get("edge_count") or 0)
            similarity = float(similarity_map.get(uuid, 0.0))
            if not math.isfinite(similarity):
                raise ValueError("candidate similarity is not finite")
            candidate_inputs.append(
                {
                    "uuid": uuid,
                    "name": str(meta.get("name") or uuid),
                    "content": _select_candidate_content(meta, preset=preset),
                    "scope": scope,
                    "memory_type": str(meta.get("type") or "SEMANTIC"),
                    "similarity": similarity,
                    "last_accessed_days_ago": days_ago(meta.get("last_accessed")),
                    "edge_count": edge_count,
                    "freshness": freshness,
                    "has_conflict": bool(meta.get("conflict_group_id")),
                    "conflict_status": meta.get("conflict_status") or None,
                    "source": source_map.get(uuid, CandidateSource.VECTOR),
                    "contributing_sources": contributing_source_map.get(uuid, frozenset()),
                    "retrieval_score_kind": score_kind_map.get(
                        uuid, RetrievalScoreKind.GRAPHITI_RRF
                    ),
                    "bm25_rank": rank_shadow.get(uuid, {}).get("bm25_rank"),
                    "cosine_rank": rank_shadow.get(uuid, {}).get("cosine_rank"),
                    "content_rank": rank_shadow.get(uuid, {}).get("content_rank"),
                    "content_cosine": content_cosine_map.get(uuid),
                    "is_superseded_view": is_superseded_view,
                    "view_kind": meta.get("view_kind"),
                }
            )
        except Exception as exc:
            logger.error(
                "Recall skipped malformed candidate metadata uuid=%r keys=%s: %s: %s",
                uuid,
                sorted(str(key) for key in meta),
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
    _t_phases["metadata_fetch"] = int((perf_counter() - _t) * 1000)

    # Observation, scalar-history advisory, and view-authority lanes live in
    # recall_pipeline_observation; they run in the original order and share this state.
    _obs_slots_for_history, candidate_inputs = await _run_scalar_lanes(
        service, query, namespace, candidate_inputs, metadata_by_uuid, authority_layer, _t_phases,
    )

    early_result = _empty_pool_result(
        service, query, candidate_inputs, visible_pending_rows, preset, limit,
        search_error, authority_layer,
    )
    if early_result is not None:
        return early_result

    # --- Adjacency ---
    _t = perf_counter()
    eligible_uuids = [str(c["uuid"]) for c in candidate_inputs]
    adjacency_map, edge_index = await service._compute_adjacency(
        eligible_uuids, context_node_ids, namespace,
    )
    _t_phases["adjacency"] = int((perf_counter() - _t) * 1000)

    candidates = _build_candidate_data(candidate_inputs, adjacency_map)
    _inject_standalone_fact_edges(
        candidates, pool.edge_hits, metadata_by_uuid, namespace, tuning, query
    )

    # --- Frontier provenance: derive evidence_kinds + project for the oracle/warden path ---
    # Only when a frontier portion will read it (active gate/ranking or the shadow pass),
    # so the old path issues no extra query. Merged into metadata_by_uuid so both
    # _apply_frontier and the shadow see it.
    frontier_active = (
        tuning.enable_oracle_ranking
        or tuning.enable_warden_gate
        or tuning.enable_belief_gate
        or (trace and tuning.enable_assertion_shadow)
    )
    if frontier_active and candidate_inputs:
        _t = perf_counter()
        await _attach_frontier_pool_metadata(service, eligible_uuids, metadata_by_uuid, tuning)
        if tuning.enable_belief_gate:
            try:
                for _uuid in eligible_uuids:
                    _m = metadata_by_uuid.get(_uuid)
                    if _m is None:
                        continue
                    _ev = _staleness_evidence_for(
                        _m, provider=service._change_log_provider, repo_resolver=_repo_path_for)
                    if _ev:
                        _m["staleness_evidence"] = _ev
            except Exception:
                logger.exception("Belief-gate staleness pass failed; continuing without staleness")
        _t_phases["frontier_metadata"] = int((perf_counter() - _t) * 1000)

    scored, scoring_trace = await _score_candidates(
        service, candidates, preset, tuning, trace, _t_phases
    )

    scored, frontier_note = await _apply_frontier_portions(
        service, query, namespace, scored, metadata_by_uuid, tuning, file_context_project,
        _t_phases,
    )

    pending_fallback = service._pending_fallback_results(visible_pending_rows, preset, limit)
    top_results = pending_fallback + scored[:max(0, limit - len(pending_fallback))]

    note = _compose_recall_note(
        scored, pending_fallback, candidates, frontier_note, search_error,
        pool.facet_active_warning,
    )

    top_results = await _enrich_temporal_facts(
        service, top_results, include_invalidated, _t_phases
    )

    # --- Stale-anchor labeling (POST-RANK, label-only) ---
    # Enrich each result with stale metadata when the anchor file was changed
    # after the memory was anchored. Best-effort: failure leaves items unlabeled
    # (stale_anchor_info=None) and never breaks recall.
    _t = perf_counter()
    try:
        stale_rows = await asyncio.to_thread(
            service.graph_adapter.stale_anchored_memories,
            project=file_context_project,
            limit=200,
            namespace=namespace,
        )
        stale_by_uuid: dict[str, dict[str, Any]] = {
            str(r["memory_uuid"]): r
            for r in stale_rows
            if r.get("memory_uuid")
        }
        labeled: list[ScoredMemory] = []
        for sm in top_results:
            row = stale_by_uuid.get(sm.uuid)
            if row is not None:
                info: dict[str, Any] = {
                    "stale_anchor": True,
                    "stale_reason": "file_changed_after_anchor",
                    "dirty_at": row.get("dirty_at"),
                    "anchored_at": row.get("anchored_at"),
                    "path": row.get("path"),
                }
            else:
                info = {"stale_anchor": False}
            labeled.append(replace(sm, stale_anchor_info=info))
        top_results = labeled
    except Exception:
        logger.exception(
            "Stale-anchor labeling failed; continuing without stale labels"
        )
    _t_phases["stale_labeling"] = int((perf_counter() - _t) * 1000)

    top_results = await _attach_stale_verifications(service, top_results, _t_phases)

    nodes_touched = await _run_post_recall_updates(
        service, top_results, metadata_by_uuid, edge_index, update_access, _t_phases
    )

    _t_phases["total"] = int((perf_counter() - _t_total) * 1000)
    logger.info(
        "recall latency breakdown query=%r preset=%s candidates=%d results=%d phases=%s",
        query[:60],
        preset.value,
        len(candidates),
        len(top_results),
        _t_phases,
    )

    retrieval_trace = await _build_retrieval_trace(
        service, query, preset, tuning, namespace, candidate_inputs, metadata_by_uuid,
        top_results, file_context_project, scoring_trace, _t_phases,
        pool.facet_active_trace, pool.rank_shadow_warning,
    )

    return RecallResult(
        query=query,
        preset=preset.value,
        results=top_results,
        candidates_evaluated=len(candidates),
        nodes_touched=nodes_touched,
        note=note,
        search_error=search_error,
        trace=retrieval_trace,
        authority_layer=tuple(authority_layer) or None,
    )


async def apply_event_history_authority_layer(
    service: Any,
    result: RecallResult,
    query: str,
    namespace: str | None,
) -> RecallResult:
    """Layer an advisory/lead event-history authority verdict onto a recall result.

    Recognized conservative first-person event queries only; every other query, and any probe
    failure, returns *result* unchanged. Kept out of ``event_history_authority`` (the pure verdict
    layer) because this does repository I/O (``graph_adapter.event_assertions_for_subject_predicate``),
    which that module explicitly documents itself as never doing.
    """
    if not service.event_history_authority_enabled or namespace is None:
        return result
    try:
        stamped = stamped_namespace(namespace)
        subject_uuid = self_uuid_for_namespace(namespace)
        # Probe recognition WITHOUT repository I/O: the pure helper over empty assertions returns
        # None exactly when the query is not a recognized conservative first-person event route
        # (third-party / nested-attributed / unclassified / malformed). No repo call in that case.
        probe = event_authority_for_query(
            query, (), subject_uuid=subject_uuid, namespace=stamped,
            foundation_verified=False)
        if probe is None:
            return result
        route = classify_event_query(query)
        assertions = service.graph_adapter.event_assertions_for_subject_predicate(
            subject_uuid, route.predicate,
            namespace=stamped, include_superseded=False, materializable_only=True)
        verdict = event_authority_for_query(
            query, assertions, subject_uuid=subject_uuid, namespace=stamped,
            foundation_verified=True)
        if verdict is not None:
            return replace(result, event_authority_layer=(verdict,))
    except Exception:
        logger.exception(
            "event-history authority probe failed; returning original result")
    return result
