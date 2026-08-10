"""Candidate acquisition, scoring, enrichment, and result assembly for recall."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

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
    _render_scalar_history_content,
    _repo_path_for,
    _select_candidate_content,
    _staleness_evidence_for,
)

from menhir.services.event_history_authority import event_authority_for_query
from menhir.services.event_history_recall import classify_event_query

async def run_recall(
    service: Any,
    query: str,
    *,
    preset: QueryPreset = QueryPreset.KNOWLEDGE,
    limit: int = 10,
    candidate_k: int = 50,
    context_node_ids: list[str] | None = None,
    include_session: bool = False,
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
                    query, limit, pending_wait_timeout_s
                )
            )
        except Exception:
            logger.exception(
                "Pending-episode wait failed query=%r; continuing with normal recall",
                query[:60],
            )
        _t_phases["pending_wait"] = int((perf_counter() - _t) * 1000)

    # --- Phase 1: candidate generation (vector, or attributed hybrid) ---
    # source_map records each candidate's origin so the scoring floor can be
    # source-aware. Default path: a single fused vector+BM25 search whose hits
    # are all attributed VECTOR. Hybrid path (tuning.enable_bm25): vector and
    # BM25 run as separate labeled passes, fused by hybrid_alpha.
    _t = perf_counter()
    group_ids = namespace_to_group_ids(namespace)
    source_map: dict[str, CandidateSource] = {}
    contributing_source_map: dict[str, frozenset[CandidateSource]] = {}
    score_kind_map: dict[str, RetrievalScoreKind] = {}
    rank_shadow: dict[str, dict[str, int]] = {}
    content_cosine_map: dict[str, float] = {}
    rank_shadow_warning: str | None = None
    search_error: str | None = None
    facet_active_trace: FacetShadowTrace | None = None
    facet_active_warning: str | None = None
    try:
        if tuning.enable_content_vector:
            methods: list[str] = []
            if not tuning.content_vector_replace_name:
                methods.append("cosine_similarity")
            if tuning.enable_bm25:
                methods.append("bm25")
            ranked = await service.graphiti_client.search_ranked_by_method(
                query,
                methods=methods,
                num_results=candidate_k,
                group_ids=group_ids,
            ) if methods else {}
            lanes: list[FusionLane] = []
            if "bm25" in methods:
                lanes.append(FusionLane(
                    CandidateSource.BM25,
                    ranked.get("bm25", []),
                    weight=1.0 - tuning.hybrid_alpha,
                ))
            if "cosine_similarity" in methods:
                lanes.append(FusionLane(
                    CandidateSource.VECTOR,
                    ranked.get("cosine_similarity", []),
                    weight=tuning.hybrid_alpha if tuning.enable_bm25 else 1.0,
                ))
            for method, rows in ranked.items():
                rank_key = "bm25_rank" if method == "bm25" else "cosine_rank"
                for rank, (uuid, _name) in enumerate(rows, start=1):
                    rank_shadow.setdefault(uuid, {})[rank_key] = rank
            try:
                query_vector = await service.graphiti_client.embed_query(query)
                content_rows = service.graph_adapter.search_content_embeddings(
                    query_vector,
                    limit=tuning.content_vector_k,
                    group_ids=group_ids,
                )
            except Exception as exc:
                logger.error(
                    "Content-vector lane unavailable; using remaining lanes: %s: %s",
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
                content_rows = []
            content_hits: list[tuple[str, str]] = []
            for rank, row in enumerate(content_rows, start=1):
                try:
                    uuid = str(row.get("uuid") or "").strip()
                    cosine = float(row["cosine"])
                    if not uuid or not math.isfinite(cosine):
                        raise ValueError("content-vector row has invalid uuid or cosine")
                    content_hits.append((uuid, str(row.get("name") or uuid)))
                    rank_shadow.setdefault(uuid, {})["content_rank"] = rank
                    content_cosine_map[uuid] = cosine
                except Exception as exc:
                    logger.error(
                        "Recall skipped malformed content-vector row uuid=%r: %s: %s",
                        row.get("uuid"),
                        exc.__class__.__name__,
                        exc,
                        exc_info=True,
                    )
            lanes.append(FusionLane(
                CandidateSource.CONTENT_VECTOR,
                content_hits,
                weight=tuning.content_vector_weight,
            ))
            hybrid = weighted_rrf_multi(
                lanes,
                limit=candidate_k,
                admission_policy=tuning.fusion_admission_policy,
            )
            search_results = [(c.uuid, c.name, c.similarity) for c in hybrid]
            source_map = {c.uuid: c.source for c in hybrid}
            contributing_source_map = {
                c.uuid: c.contributing_sources for c in hybrid
            }
            score_kind_map = {
                c.uuid: RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED for c in hybrid
            }
        elif tuning.enable_bm25:
            hybrid = await hybrid_search(
                service.graphiti_client, query, config=tuning,
                num_results=candidate_k, group_ids=group_ids,
            )
            search_results = [(c.uuid, c.name, c.similarity) for c in hybrid]
            source_map = {c.uuid: c.source for c in hybrid}
            contributing_source_map = {
                c.uuid: c.contributing_sources for c in hybrid
            }
            score_kind_map = {
                c.uuid: RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED for c in hybrid
            }
        else:
            search_results = await service.graphiti_client.search_scored(
                query, num_results=candidate_k, group_ids=group_ids
            )
            source_map = {uuid: CandidateSource.VECTOR for uuid, _, _ in search_results}
            contributing_source_map = {
                uuid: frozenset({CandidateSource.VECTOR})
                for uuid, _, _ in search_results
            }
            score_kind_map = {
                uuid: RetrievalScoreKind.GRAPHITI_RRF for uuid, _, _ in search_results
            }
            if trace:
                try:
                    ranked = await service.graphiti_client.search_ranked_by_method(
                        query,
                        methods=["bm25", "cosine_similarity"],
                        num_results=candidate_k,
                        group_ids=group_ids,
                    )
                    for method, rows in ranked.items():
                        rank_key = "bm25_rank" if method == "bm25" else "cosine_rank"
                        for rank, (uuid, _name) in enumerate(rows, start=1):
                            rank_shadow.setdefault(uuid, {})[rank_key] = rank
                except Exception as exc:
                    rank_shadow_warning = (
                        f"rank_method_shadow_unavailable:{exc.__class__.__name__}"
                    )
                    logger.error(
                        "Rank-method shadow unavailable: %s: %s",
                        exc.__class__.__name__,
                        exc,
                        exc_info=True,
                    )
    except Exception as exc:
        search_error = f"graphiti_search_unavailable:{exc.__class__.__name__}"
        logger.error(
            "Graphiti search unavailable (degraded mode); query=%r preset=%s namespace=%r "
            "falling back to empty results: %s: %s",
            query,
            preset.value,
            namespace,
            exc.__class__.__name__,
            exc,
            exc_info=True,
        )
        search_results = []
        source_map = {}
        contributing_source_map = {}
        score_kind_map = {}

    # Experimental active FACET lane. FACET can only rank candidates already
    # retrieved into the bounded pool; it never expands graph scope. Fuse its
    # convergence order with the existing best-first order so downstream scoring
    # and oracle ranking receive a real facet-influenced similarity signal.
    if tuning.enable_facet_candidates and search_results:
        try:
            facet_active_trace = await service._run_facet_pass(
                query,
                namespace,
                [uuid for uuid, _name, _score in search_results],
                query_project=file_context_project,
                active=True,
            )
            facet_name_map = {uuid: name for uuid, name, _score in search_results}
            facet_hits = [
                (row.candidate_id, facet_name_map[row.candidate_id])
                for row in facet_active_trace.rows
                if row.candidate_id in facet_name_map
            ]
            for rank, (uuid, _name) in enumerate(facet_hits, start=1):
                rank_shadow.setdefault(uuid, {})["facet_rank"] = rank
            if facet_hits and tuning.facet_weight > 0.0:
                previous_sources = dict(source_map)
                previous_contributors = dict(contributing_source_map)
                fused = weighted_rrf_multi(
                    [
                        FusionLane(
                            CandidateSource.VECTOR,
                            [(uuid, name) for uuid, name, _score in search_results],
                        ),
                        FusionLane(
                            CandidateSource.FACET,
                            facet_hits,
                            weight=tuning.facet_weight,
                        ),
                    ],
                    limit=candidate_k,
                    admission_policy="production_fused",
                )
                search_results = [(row.uuid, row.name, row.similarity) for row in fused]
                # FACET changes ranking but does not grant a new floor exemption;
                # retain the original admission source and add FACET as provenance.
                source_map = {
                    row.uuid: previous_sources.get(row.uuid, row.source)
                    for row in fused
                }
                contributing_source_map = {
                    row.uuid: frozenset(
                        set(previous_contributors.get(row.uuid, frozenset()))
                        | set(row.contributing_sources)
                    )
                    for row in fused
                }
                score_kind_map = {
                    row.uuid: RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED
                    for row in fused
                }
                facet_active_warning = "facet: active_rank_fusion"
            elif not facet_hits:
                facet_active_warning = "active facet found no overlaps; base ranking retained."
            else:
                facet_active_warning = "active facet weight is zero; base ranking retained."
        except Exception as exc:
            facet_active_warning = f"active facet unavailable:{exc.__class__.__name__}"
            logger.error(
                "Active FACET lane unavailable; retaining base ranking: %s: %s",
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
    _t_phases["vector_search"] = int((perf_counter() - _t) * 1000)

    # Plan 1b (staged; default "rrf" == byte-identical): normalize graphiti's
    # RRF reranker score to [0, 1] so the VECTOR `similarity` lane shares one
    # scale with the [0, 1] SOURCE_PRIORS, restoring PENDING=1.0's intended
    # top-pin. This rescales one additive lane and CHANGES ranking, so it is
    # off by default and measured on the oracle slice before any default flip.
    # Applied here -- to genuine search scores only, before the pending
    # fallback and provenance priors -- so those [0, 1] priors are untouched.
    #
    # SCOPE: only the search_scored (fused RRF, ~[0, 2]) path needs this. The
    # attributed-hybrid/content paths already use weighted_rrf's pinned common
    # ceiling, so dividing again would corrupt Arm-A parity. Guard both paths.
    if (
        tuning.similarity_scale == "normalized"
        and not tuning.enable_bm25
        and not tuning.enable_content_vector
        and search_results
    ):
        search_results = [
            (uuid, name, min(1.0, max(0.0, score / GRAPHITI_RRF_DUAL_METHOD_MAX)))
            for uuid, name, score in search_results
        ]
        for uuid, _name, _score in search_results:
            score_kind_map[uuid] = RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED

    if not search_results:
        if visible_pending_rows:
            return RecallResult(
                query=query,
                preset=preset.value,
                results=service._pending_fallback_results(visible_pending_rows, preset, limit),
                candidates_evaluated=0,
                nodes_touched=0,
                note=(
                    "Recall search backend failed; pending results may be incomplete."
                    if search_error
                    else None
                ),
                search_error=search_error,
            )
        if not pending_entity_uuids:
            return RecallResult(
                query=query,
                preset=preset.value,
                results=[],
                candidates_evaluated=0,
                nodes_touched=0,
                note=(
                    "Recall search backend failed; this is not a confirmed zero-match result."
                    if search_error
                    else None
                ),
                search_error=search_error,
            )
        search_results = [(uuid, uuid, PENDING_ENTITY_SIMILARITY) for uuid in pending_entity_uuids]

    candidate_uuids = list(dict.fromkeys(pending_entity_uuids + [uuid for uuid, _, _ in search_results]))
    similarity_map = {uuid: score for uuid, _, score in search_results}
    for uuid in pending_entity_uuids:
        similarity_map[uuid] = max(similarity_map.get(uuid, 0.0), PENDING_ENTITY_SIMILARITY)
        # Pending entities are injected by provenance; their PENDING source
        # overrides any search attribution and exempts them from the floor.
        source_map[uuid] = CandidateSource.PENDING
        contributing_source_map[uuid] = frozenset({CandidateSource.PENDING})
        score_kind_map[uuid] = RetrievalScoreKind.SOURCE_PRIOR

    # --- File context: inject file-linked semantic candidates ---
    if file_context:
        _t = perf_counter()
        try:
            file_linked_uuids = await service._resolve_file_context(file_context, file_context_project)
        except Exception:
            logger.exception(
                "File context resolution failed path=%r project=%r; continuing without linked candidates",
                file_context,
                file_context_project,
            )
            file_linked_uuids = []
        _t_phases["file_context"] = int((perf_counter() - _t) * 1000)
        if file_linked_uuids:
            logger.debug(
                "File context resolved %d semantic UUIDs from path=%r project=%s",
                len(file_linked_uuids), file_context, file_context_project,
            )
            candidate_uuids = list(dict.fromkeys(candidate_uuids + file_linked_uuids))
            for uuid in file_linked_uuids:
                if uuid not in similarity_map:
                    similarity_map[uuid] = FILE_LINKED_BASELINE_SIMILARITY
                    # Only attribute FILE_LINKED when this uuid is a pure
                    # file-context injection (not already a search/pending
                    # hit), matching the similarity-prior condition above.
                    source_map[uuid] = CandidateSource.FILE_LINKED
                    contributing_source_map[uuid] = frozenset({CandidateSource.FILE_LINKED})
                    score_kind_map[uuid] = RetrievalScoreKind.SOURCE_PRIOR

    # --- Fact-edge retrieval (opt-in): pull RELATES_TO fact edges once ---
    # Node search returns entity *names*; a "what happened / what did I" query's answer is
    # a dated *fact* on an edge. We run the edge search here (before metadata fetch) so the
    # "pointer" mode can hydrate the edge's endpoint NODES through the normal node path.
    edge_hits: list[dict[str, Any]] = []
    # Pointer hydration is lens-gated (episodic/history queries only); standalone (the
    # rejected comparison arm) still runs unconditionally. Skip the edge round-trip when
    # pointer mode is on but the query isn't history-wanting.
    _pointer_active = (
        tuning.enable_fact_edges
        and tuning.fact_edge_mode == "pointer"
        and _query_wants_history(query)
    )
    _run_edge_search = tuning.enable_fact_edges and (
        tuning.fact_edge_mode == "standalone" or _pointer_active
    )
    if _run_edge_search:
        _t = perf_counter()
        try:
            edge_hits = await service.graphiti_client.search_edges_scored(
                query, num_results=tuning.fact_edge_k, group_ids=group_ids
            )
        except Exception as exc:
            logger.error(
                "Fact-edge search unavailable (continuing node-only): %s: %s",
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
            edge_hits = []
        _t_phases["fact_edge_search"] = int((perf_counter() - _t) * 1000)

        # POINTER mode: the edge is a signpost, not the answer. Hydrate its endpoint entity
        # NODES (rich summaries + surrounding context) into the candidate pool with an
        # edge-derived similarity prior, so they flow through the normal node metadata /
        # scope / adjacency path. This fixes the standalone mode's context collapse (rung
        # A′): we keep the node's full content AND let the edge boost the right node's rank.
        if _pointer_active:
            pointer_added = 0
            for hit in edge_hits:
                try:
                    escore = float(hit["score"])
                    if not math.isfinite(escore):
                        raise ValueError("fact-edge score is not finite")
                    for key in ("source_node_uuid", "target_node_uuid"):
                        nuuid = str(hit.get(key) or "").strip()
                        if not nuuid:
                            continue
                        if nuuid not in similarity_map:
                            candidate_uuids.append(nuuid)
                            pointer_added += 1
                        # Edge-derived prior; take the strongest edge that points here.
                        similarity_map[nuuid] = max(
                            similarity_map.get(nuuid, 0.0), escore
                        )
                        source_map.setdefault(nuuid, CandidateSource.FACT_EDGE)
                        score_kind_map.setdefault(
                            nuuid, RetrievalScoreKind.FACT_EDGE_RRF
                        )
                except Exception as exc:
                    logger.error(
                        "Recall skipped malformed fact-edge pointer uuid=%r: %s: %s",
                        hit.get("uuid"),
                        exc.__class__.__name__,
                        exc,
                        exc_info=True,
                    )
            candidate_uuids = list(dict.fromkeys(candidate_uuids))
            logger.debug(
                "fact-edge pointer hydration query=%r edges=%d added_nodes=%d",
                query[:60], len(edge_hits), pointer_added,
            )

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
        if scope == NodeScope.SESSION and not include_session:
            continue
        # View(kind) supersession: a superseded View version (view_current is false) is stale
        # state kept only for history/provenance. It must not compete with the current version
        # in default recall. Only View nodes carry view_current; every normal memory has it
        # unset (None) and passes through untouched. include_superseded surfaces them for
        # historical/provenance/debug recall, where they are flagged is_superseded_view.
        is_superseded_view = meta.get("view_current") is False
        if is_superseded_view and not include_superseded:
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

    # Pre-initialize: populated by the observation lane if it runs; read by the scalar_history
    # advisory lane below regardless of whether authority injection is enabled.
    _obs_slots_for_history: set[tuple[str, str, str, str, str]] = set()

    # --- Observation candidates (Phase 4a.2, flag-gated): inject :TypedAssertion observations ---
    # The recall pipeline is otherwise :Entity-only (fetch_candidate_metadata matches (n:Entity)),
    # so a typed-scalar observation ("I own 20 rare coins") is never a candidate. Search the
    # observation lane whenever either scalar feature needs query-dependent slots. Only
    # scalar_view_authority_enabled adds observations to candidate_inputs and performs the current-
    # value authority work below; history-only mode uses the same hits as a neutral discovery seam.
    # Injected HERE (before the empty-candidate early-return below) so observations can surface even
    # when NO :Entity candidate matched -- the exact "the fact lives only on the assertion log" case.
    # Only MATERIALIZABLE observations surface (the lane query filters superseded/binding_pending).
    # Flag-OFF -> no search, byte-identical recall. Failures never break recall.
    if (service.scalar_view_authority_enabled or service.scalar_history_enabled) and namespace is not None:
        _t = perf_counter()
        # Phase 4b instrumentation: the additive authority path emits its own recall_audit events
        # (the legacy suppression path's events cover only suppression). One `authority_annotation`
        # event per injected slot records whether it LEADS or stays ADVISORY and the basis
        # (tier vs user-FOUNDS) -- so a MEASURE run can compute the wrongful-authority rate (a
        # `leads` with no user foundation) and confirm the lane fired. No-op when the toggle is off.
        from menhir.infrastructure.audit_trail import RECALL as _authority_audit
        if service.scalar_view_authority_enabled:
            _authority_audit.begin()
        try:
            obs_vec = await service.graphiti_client.embed_query(query)
            obs_hits = await asyncio.to_thread(
                service.graph_adapter.search_assertion_embeddings,
                obs_vec, limit=10, namespaces=[stamped_namespace(namespace)],
            )
            existing_uuids = (
                {str(c["uuid"]) for c in candidate_inputs}
                if service.scalar_view_authority_enabled else set()
            )
            obs_added = 0
            obs_slots: set[tuple[str, str, str, str, str]] = set()
            # G17 (4a.3): subject_uuid -> subject_display of each surfaced observation, used to
            # resolve the QUERY's subject INDEPENDENTLY of the injection's provenance row (a named
            # third party the query mentions is rescued by matching its display in the query text).
            obs_subject_displays: dict[str, str] = {}
            for hit in obs_hits:
                try:
                    aid = str(hit.get("assertion_id") or "").strip()
                    span = str(hit.get("stated_span") or "").strip()
                    cos = hit.get("cosine")
                    if not aid or not span or cos is None or not math.isfinite(float(cos)):
                        continue
                    _subj = str(hit.get("subject_uuid") or "")
                    _attr = str(hit.get("attribute") or "")
                    _scope = str(hit.get("scope") or "")
                    _value_kind = str(hit.get("value_kind") or "")
                    _unit = str(hit.get("unit") or "")
                    if _subj and _attr and _value_kind:
                        obs_slots.add((_subj, _attr, _scope, _value_kind, _unit))
                    # History-only mode deliberately collects the matched slot but does not inject
                    # the raw observation or run scalar-state authority/suppression work.
                    if not service.scalar_view_authority_enabled:
                        continue
                    if aid in existing_uuids:
                        continue
                    existing_uuids.add(aid)
                    candidate_inputs.append({
                        "uuid": aid, "name": span, "content": span,
                        "scope": str(NodeScope.PERSISTENT), "memory_type": "OBSERVATION",
                        "similarity": float(cos), "last_accessed_days_ago": 0.0, "edge_count": 0,
                        "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                        "conflict_status": None, "source": CandidateSource.OBSERVATION,
                        "contributing_sources": frozenset({CandidateSource.OBSERVATION}),
                        "retrieval_score_kind": RetrievalScoreKind.GRAPHITI_RRF,
                        "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                        "content_cosine": None, "is_superseded_view": False, "view_kind": None,
                    })
                    # Seed metadata so any oracle/provenance reader (and Phase 4a.4's slot-keyed
                    # View-authority lookup) can resolve the observation's slot + subject.
                    metadata_by_uuid[aid] = {
                        "name": span, "content": span, "namespace": hit.get("namespace"),
                        "valid_at": hit.get("valid_at"),
                        "ss_attribute": hit.get("attribute"), "ss_scope": hit.get("scope"),
                        "ss_value_kind": hit.get("value_kind"), "ss_unit": hit.get("unit"),
                        "subject_uuid": hit.get("subject_uuid"),
                    }
                    if _subj:
                        obs_subject_displays[_subj] = str(hit.get("subject_display") or "")
                    obs_added += 1
                except Exception as exc:
                    logger.error(
                        "Recall skipped malformed observation hit id=%r: %s: %s",
                        hit.get("assertion_id"), exc.__class__.__name__, exc, exc_info=True,
                    )
            logger.debug("observation injection query=%r added=%d (k=10)", query[:60], obs_added)
            _obs_slots_for_history = obs_slots  # share with scalar_history lane below

            # Phase 4a.4: for each SURFACED slot, DETERMINISTICALLY inject the current scalar_state
            # View by slot-keyed lookup (NOT embedding rank), so the authoritative CURRENT value
            # surfaces even when its View did not win ranking -- the G5 stale-value fix (the "how
            # many coins now" query returns 37, not the stale 20 the observation embedding matched).
            # A slot with no current View (abstained/expired -> current unknown by design) injects
            # nothing. Floor-exempt SCALAR_AUTHORITY source so the cosine floor never drops it.
            #
            # Phase 4b FOUNDATION GATE (G10/7.G): an injected View is always visible (additive), but
            # it is marked the CURRENT AUTHORITY (leads the answer) ONLY when its effective tier
            # rests on a SOURCE FOUNDATION (not `agent`-only probabilistic extraction, which
            # memory-governance.md forbids from self-authorizing). The effective tier is read from
            # the FOLD SSOT (current_authority) at as_of=now (G16 -- not the stamped snapshot; G19 --
            # not folding future in), cached per subject. Absent a foundation the View is injected as
            # ADVISORY (is_scalar_authority=False): the current value is still surfaced, just never
            # falsely presented as verified authority.
            from datetime import datetime, timezone
            from menhir.domain.scalar_view_authority import FOUNDATION_TIERS, QueryIntent
            from menhir.domain.scalar_view_suppression import authority_query_intent
            _now = datetime.now(timezone.utc)
            _authority_by_subject: dict[str, dict[tuple[str, str, str, str], str]] = {}
            # G13: an EXPIRED slot has NO current View by design; for a current-state (or bare) query
            # the recall must surface the EXPIRY VERDICT (last-known + date + current-UNKNOWN) so the
            # old observations never read as current. Historical/as-of intents (PREVIOUS_VALUE/
            # COMPARISON) are slice-B territory and are NOT expiry-verdicted here.
            _intent = authority_query_intent(query)
            # G13 slice B: an explicit "as of <date>" (COMPARISON) query leads with the deterministic
            # AS-OF FOLD at that date (absolutes + deltas folded up to <t>), NOT the current View and
            # NOT a raw observation ordering. History cues without a date (PREVIOUS_VALUE) carry no
            # resolvable timestamp, so they fall through to ordinary ranking (slice A already keeps the
            # expiry verdict off a history query). Only an explicit date is folded here.
            from menhir.domain.temporal_intent import classify_temporal_intent
            _as_of_str = classify_temporal_intent(query).as_of
            _as_of_dt = None
            if _intent == QueryIntent.COMPARISON and _as_of_str:
                try:
                    _as_of_dt = datetime.strptime(_as_of_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    _as_of_dt = None
            _asof_states_by_subject: dict[str, dict[tuple[str, str, str, str], Any]] = {}

            def _asof_state_for(subject_uuid: str, slot: tuple[str, str, str, str]):
                if subject_uuid not in _asof_states_by_subject:
                    try:
                        svc = service.graph_adapter.scalar_state_service()
                        res = svc.fold_entity(
                            subject_uuid, namespace=stamped_namespace(namespace), as_of=_as_of_dt)
                        _asof_states_by_subject[subject_uuid] = {
                            (s.attribute, s.scope, s.value_kind, s.unit): s for s in res.states}
                    except Exception:
                        logger.exception("as-of fold failed subject=%r", subject_uuid)
                        _asof_states_by_subject[subject_uuid] = {}
                return _asof_states_by_subject[subject_uuid].get(slot)

            _expiries_by_subject: dict[str, dict[tuple[str, str, str, str], Any]] = {}

            def _expiry_for(subject_uuid: str, slot: tuple[str, str, str, str]):
                if subject_uuid not in _expiries_by_subject:
                    try:
                        svc = service.graph_adapter.scalar_state_service()
                        _expiries_by_subject[subject_uuid] = svc.current_expiries(
                            subject_uuid, namespace=stamped_namespace(namespace), as_of=_now)
                    except Exception:
                        logger.exception(
                            "expiry read failed subject=%r; no verdict", subject_uuid)
                        _expiries_by_subject[subject_uuid] = {}
                return _expiries_by_subject[subject_uuid].get(slot)

            # G17 (4a.3): resolve the QUERY's subject INDEPENDENTLY of the injection provenance row,
            # so a View about subject B is never injected as authority for a query about subject A
            # (the cross-subject leak; Gate 2 was tautological because query/View/fact subjects all
            # came from one row). Two deterministic, I/O-free signals: (1) a first-person query
            # resolves the namespace's canonical self subject -- whose uuid is DETERMINISTIC,
            # uuid5("menhir-self:<ns>") (episode_lifecycle.ensure_self_entity), so no DB read; (2) a
            # named third party the query MENTIONS is rescued by matching a surfaced observation's
            # subject_display as a whole word in the query ("my dad's cars" -> the `dad` View is
            # allowed). When the query subject cannot be resolved (no first-person, no named match)
            # the set is empty and the gate is OPEN (today's behavior -- do not over-restrict).
            import re as _re
            import uuid as _uuid
            from menhir.services.typed_scalar_perception import SELF_TOKENS
            _qlow = query.lower()
            _first_person = bool(_re.search(r"\b(i|me|my|mine|myself|we|us|our)\b", _qlow))
            query_subjects: set[str] = set()
            if _first_person:
                # the canonical self subject: deterministic uuid5, matches what perception bound
                # self-subject assertions to (episode_lifecycle.ensure_self_entity) -- no DB read.
                query_subjects.add(str(_uuid.uuid5(
                    _uuid.NAMESPACE_URL, f"menhir-self:{stamped_namespace(namespace)}")))
            for _s_uuid, _s_disp in obs_subject_displays.items():
                _disp = (_s_disp or "").strip().lower()
                if not _disp:
                    continue
                if _first_person and _disp in SELF_TOKENS:
                    query_subjects.add(_s_uuid)                     # a self observation (display)
                elif _re.search(rf"\b{_re.escape(_disp)}\b", _qlow):
                    query_subjects.add(_s_uuid)                     # a named third party in the query

            def _effective_tier(subject_uuid: str, slot: tuple[str, str, str, str]) -> str | None:
                if subject_uuid not in _authority_by_subject:
                    try:
                        svc = service.graph_adapter.scalar_state_service()
                        _authority_by_subject[subject_uuid] = svc.current_authority(
                            subject_uuid, namespace=stamped_namespace(namespace), as_of=_now)
                    except Exception:
                        logger.exception(
                            "foundation-gate authority read failed subject=%r; advisory", subject_uuid)
                        _authority_by_subject[subject_uuid] = {}
                return _authority_by_subject[subject_uuid].get(slot)

            async def _assertion_contributor_payload(
                assertion_ids: list[str], relations: dict[str, str]
            ) -> dict[str, Any]:
                try:
                    return await asyncio.to_thread(
                        service.graph_adapter.fetch_assertion_contributors,
                        assertion_ids=assertion_ids, relations=relations, limit=8)
                except Exception:
                    logger.exception("structured assertion provenance unavailable; returning head only")
                    return {"contributors": [], "total": len(assertion_ids), "next_offset": None}

            async def _view_contributor_payload(view_uuid: str) -> dict[str, Any]:
                try:
                    return await asyncio.to_thread(
                        service.graph_adapter.fetch_scalar_authority_contributors,
                        view_uuid=view_uuid, limit=8, offset=0,
                        namespace=stamped_namespace(namespace))
                except Exception:
                    logger.exception("structured View provenance unavailable; returning head only")
                    return {"contributors": [], "total": 0, "next_offset": None}

            auth_added = 0
            _authority_slots = obs_slots if service.scalar_view_authority_enabled else set()
            for (subj, attr, scp, vk, un) in _authority_slots:
                if not subj or not attr or not vk:
                    continue
                # G17 subject safety: skip the authority injection for a subject the query is NOT
                # about (when the query subject could be resolved). The observation candidate itself
                # already surfaced additively; only the View-as-authority is subject-gated.
                if query_subjects and subj not in query_subjects:
                    logger.debug(
                        "scalar-authority injection skipped cross-subject subj=%r (query subjects=%r)",
                        subj, query_subjects)
                    continue
                if _intent == QueryIntent.COMPARISON and _as_of_dt is None:
                    # The as-of date matched _AS_OF_RE (so intent is COMPARISON) but did not resolve
                    # to a real calendar date -- e.g. "as of 2026-02-30". Injecting nothing is the
                    # only safe answer: falling through would hand back the CURRENT View, which is
                    # never the answer to "as of <date>", and the expiry verdict below is
                    # current-state-only so it would not correct it either.
                    continue
                if _intent == QueryIntent.COMPARISON and _as_of_dt is not None:
                    # G13 slice B: lead with the AS-OF folded value at <date>; the current View is not
                    # the answer to "as of <date>". Founds-gated like the current authority.
                    st = _asof_state_for(subj, (attr, scp, vk, un))
                    if st is not None:
                        auuid = (f"scalar-asof:{stamped_namespace(namespace)}:{_as_of_str}:{subj}:"
                                 f"{attr}:{scp}:{vk}:{un}")
                        if auuid not in existing_uuids:
                            existing_uuids.add(auuid)
                            a_founded = False
                            try:
                                a_founded = await asyncio.to_thread(
                                    service.graph_adapter.assertions_have_user_foundation,
                                    assertion_ids=list(st.contributor_ids),
                                    namespace=stamped_namespace(namespace))
                            except Exception:
                                logger.exception(
                                    "as-of foundation read failed slot=%r; advisory", (subj, attr))
                            aname = (f"{st.subject_display or 'user'}'s {attr} as of {_as_of_str} = "
                                     f"{st.value}.")
                            candidate_inputs.append({
                                "uuid": auuid, "name": aname, "content": aname,
                                "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
                                "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
                                "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                                "conflict_status": None,
                                "source": CandidateSource.SCALAR_AUTHORITY,
                                "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
                                "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                                "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                                "content_cosine": None, "is_superseded_view": False,
                                "view_kind": "scalar_state",
                                "is_scalar_authority": a_founded,
                            })
                            metadata_by_uuid[auuid] = {
                                "name": aname, "content": aname,
                                "namespace": stamped_namespace(namespace),
                                "view_kind": "scalar_state", "view_current": True,
                                "scalar_asof": _as_of_str, "as_of_value": st.value,
                                "valid_at": st.valid_at, "has_foundation": a_founded,
                            }
                            relations = {st.anchor_id: "CURRENT_ANCHOR"}
                            relations.update({i: "CONTRIBUTED_TO"
                                              for i in st.contributed_delta_ids})
                            relations.update({i: "SUPERSEDED_ANCHOR"
                                              for i in st.superseded_anchor_ids})
                            contributor_payload = await _assertion_contributor_payload(
                                [
                                    st.anchor_id, *st.contributed_delta_ids,
                                    *st.superseded_anchor_ids,
                                ],
                                relations,
                            )
                            authority_layer.append(ScalarAuthorityVerdict(
                                kind="as_of", status="leads" if a_founded else "advisory",
                                subject_uuid=subj, attribute=attr, scope=scp,
                                value_kind=vk, unit=un, value=st.value,
                                valid_at=st.valid_at, view_uuid=None,
                                has_foundation=a_founded,
                                contributors=_authority_contributors(contributor_payload),
                                contributors_total=int(contributor_payload.get("total", 0)),
                                contributors_truncated=(
                                    contributor_payload.get("next_offset") is not None),
                                next_offset=contributor_payload.get("next_offset"),
                            ))
                            _authority_audit.audit(
                                "authority_annotation", "leads" if a_founded else "advisory",
                                namespace=stamped_namespace(namespace), subject_uuid=subj,
                                slot=[attr, scp, vk, un],
                                details={"kind": "as_of", "as_of": _as_of_str,
                                         "value": str(st.value), "user_foundation": a_founded})
                            auth_added += 1
                    continue
                view = await asyncio.to_thread(
                    service.graph_adapter.fetch_current_scalar_view_for_slot,
                    subject_uuid=subj, attribute=attr, scope=scp, value_kind=vk, unit=un,
                    namespace=stamped_namespace(namespace),
                )
                if not view:
                    # G13: no current View. If the slot EXPIRED (value ended, no replacement) and the
                    # query is current-state / bare, inject the EXPIRY VERDICT so the old observations
                    # are never read as current. It LEADS only on a user foundation (the "used to own"
                    # was user-declared, via the expiry contributors' FOUNDS); else advisory.
                    if _intent in (QueryIntent.CURRENT_STATE, QueryIntent.AMBIGUOUS):
                        exp = _expiry_for(subj, (attr, scp, vk, un))
                        if exp is not None:
                            euuid = (f"scalar-expiry:{stamped_namespace(namespace)}:{subj}:{attr}:"
                                     f"{scp}:{vk}:{un}")
                            if euuid not in existing_uuids:
                                existing_uuids.add(euuid)
                                e_founded = False
                                try:
                                    e_founded = await asyncio.to_thread(
                                        service.graph_adapter.assertions_have_user_foundation,
                                        assertion_ids=list(exp.contributor_ids),
                                        namespace=stamped_namespace(namespace))
                                except Exception:
                                    logger.exception(
                                        "expiry foundation read failed slot=%r; advisory",
                                        (subj, attr))
                                ename = (
                                    f"{exp.subject_display or 'user'}'s {attr}: EXPIRED. last known "
                                    f"{exp.expired_value} as of {exp.valid_at}; current {attr} UNKNOWN.")
                                candidate_inputs.append({
                                    "uuid": euuid, "name": ename, "content": ename,
                                    "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
                                    "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
                                    "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                                    "conflict_status": None,
                                    "source": CandidateSource.SCALAR_AUTHORITY,
                                    "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
                                    "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                                    "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                                    "content_cosine": None, "is_superseded_view": False,
                                    "view_kind": "scalar_state",
                                    "is_scalar_authority": e_founded,
                                })
                                metadata_by_uuid[euuid] = {
                                    "name": ename, "content": ename,
                                    "namespace": stamped_namespace(namespace),
                                    "view_kind": "scalar_state", "view_current": True,
                                    "scalar_expiry": True, "expired_value": exp.expired_value,
                                    "valid_at": exp.valid_at, "has_foundation": e_founded,
                                }
                                contributor_payload = await _assertion_contributor_payload(
                                    list(exp.contributor_ids),
                                    {i: "EXPIRY_INPUT" for i in exp.contributor_ids},
                                )
                                authority_layer.append(ScalarAuthorityVerdict(
                                    kind="expired",
                                    status="leads" if e_founded else "advisory",
                                    subject_uuid=subj, attribute=attr, scope=scp,
                                    value_kind=vk, unit=un, value=exp.expired_value,
                                    valid_at=exp.valid_at, view_uuid=None,
                                    has_foundation=e_founded,
                                    contributors=_authority_contributors(contributor_payload),
                                    contributors_total=int(contributor_payload.get("total", 0)),
                                    contributors_truncated=(
                                        contributor_payload.get("next_offset") is not None),
                                    next_offset=contributor_payload.get("next_offset"),
                                ))
                                _authority_audit.audit(
                                    "authority_annotation", "leads" if e_founded else "advisory",
                                    namespace=stamped_namespace(namespace), subject_uuid=subj,
                                    slot=[attr, scp, vk, un],
                                    details={"kind": "expiry", "expired_value": str(exp.expired_value),
                                             "valid_at": exp.valid_at, "user_foundation": e_founded})
                                auth_added += 1
                    continue
                vuuid = str(view.get("uuid") or "").strip()
                if not vuuid:
                    continue
                view_already_ranked = vuuid in existing_uuids
                if not view_already_ranked:
                    existing_uuids.add(vuuid)
                vname = str(view.get("name") or f"{attr}: {view.get('value')}")
                tier = await asyncio.to_thread(_effective_tier, subj, (attr, scp, vk, un))
                # G14 slice 3 (10.G basis gate): the View leads on a SOURCE FOUNDATION, from EITHER
                # a trusted non-perception write path (effective tier not `agent`) OR -- the bridge
                # payoff -- the head's CURRENT_ANCHOR tracing to a declarant='user' :TurnEvidence
                # admission (FOUNDS edge). So an `agent`-tier extraction of an ADMITTED user statement
                # now leads in a Turn-capturing box, while `agent` extraction with no user admission
                # (Episodic fixtures) stays advisory. Additive: never weakens the 4b tier basis.
                tier_foundation = bool(tier) and tier in FOUNDATION_TIERS
                user_foundation = False
                if not tier_foundation:
                    try:
                        user_foundation = await asyncio.to_thread(
                            service.graph_adapter.scalar_view_has_user_foundation,
                            view_uuid=vuuid, namespace=stamped_namespace(namespace))
                    except Exception:
                        logger.exception(
                            "foundation-gate FOUNDS read failed view=%r; advisory", vuuid)
                has_foundation = tier_foundation or user_foundation
                authority_candidate = {
                    "uuid": vuuid, "name": vname, "content": vname,
                    "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
                    "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
                    "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                    "conflict_status": None, "source": CandidateSource.SCALAR_AUTHORITY,
                    "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
                    "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                    "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                    "content_cosine": None, "is_superseded_view": False,
                    "view_kind": "scalar_state",
                    # Phase 4b: lead ONLY on a foundation; else injected but advisory.
                    "is_scalar_authority": has_foundation,
                }
                if view_already_ranked:
                    # A View can win ordinary vector search before the deterministic slot lookup.
                    # Do not let UUID dedup skip its authority annotation (the wake_time e2e case):
                    # upgrade that same candidate in place and still emit the structured verdict.
                    for existing in candidate_inputs:
                        if str(existing.get("uuid") or "") == vuuid:
                            existing.update(authority_candidate)
                            break
                else:
                    candidate_inputs.append(authority_candidate)
                metadata_by_uuid[vuuid] = {
                    "name": vname, "content": vname, "namespace": view.get("namespace"),
                    "view_kind": "scalar_state", "view_current": True,
                    "ss_value": view.get("value"), "valid_at": view.get("valid_at"),
                    "effective_tier": tier, "has_foundation": has_foundation,
                    "tier_foundation": tier_foundation, "user_foundation": user_foundation,
                }
                contributor_payload = await _view_contributor_payload(vuuid)
                authority_layer.append(ScalarAuthorityVerdict(
                    kind="current", status="leads" if has_foundation else "advisory",
                    subject_uuid=subj, attribute=attr, scope=scp, value_kind=vk, unit=un,
                    value=view.get("value"), valid_at=view.get("valid_at"), view_uuid=vuuid,
                    has_foundation=has_foundation,
                    contributors=_authority_contributors(contributor_payload),
                    contributors_total=int(contributor_payload.get("total", 0)),
                    contributors_truncated=(contributor_payload.get("next_offset") is not None),
                    next_offset=contributor_payload.get("next_offset"),
                ))
                _authority_audit.audit(
                    "authority_annotation", "leads" if has_foundation else "advisory",
                    namespace=stamped_namespace(namespace), subject_uuid=subj,
                    slot=[attr, scp, vk, un],
                    details={"kind": "current", "value": str(view.get("value")),
                             "effective_tier": tier, "tier_foundation": tier_foundation,
                             "user_foundation": user_foundation})
                auth_added += 1
            logger.debug("scalar-authority injection query=%r added=%d", query[:60], auth_added)
            # Lane summary: how many observations surfaced and how many authority annotations fired,
            # under which intent -- the top-line the MEASURE run reads to confirm the lane ran.
            if service.scalar_view_authority_enabled:
                _authority_audit.audit(
                    "observation_lane", "surfaced", namespace=stamped_namespace(namespace),
                    details={"observations_added": obs_added,
                             "authority_annotations": auth_added,
                             "intent": _intent.value, "query": query[:120]})
        except Exception:
            logger.exception(
                "observation injection failed query=%r; leaving recall unchanged", query[:60]
            )
        _t_phases["observation_lane"] = int((perf_counter() - _t) * 1000)

    # --- Scalar history advisory lane (flag-gated) ---
    # When scalar_history_enabled, surface scalar_history Views as advisory context for
    # slots discovered by the observation lane above. This is the Slice 3 recall contract:
    # - activate for history/change/comparison questions;
    # - activate as bounded support when a current-state query has no anchored scalar state;
    # - remain below an authoritative scalar_state head when both exist;
    # - label delta-only slots "advisory history — not an absolute current total";
    # - include contributor IDs for provenance inspection.
    # Must NOT: pass the current-anchor foundation check, enter the scalar authority layer,
    # suppress raw memories, convert latest delta to absolute, add delta entries,
    # use recorded/ingest time as the displayed event time.
    # Failures never break recall. Flag-OFF -> the generic exclusion above already prevents
    # scalar_history Views from leaking through vector/entity retrieval.
    #
    if service.scalar_history_enabled and namespace is not None and _obs_slots_for_history:
        _t = perf_counter()
        try:
            from menhir.domain.scalar_view_authority import QueryIntent
            from menhir.domain.scalar_view_suppression import authority_query_intent
            _sh_intent = authority_query_intent(query)
            _sh_wants_history = (
                _query_wants_history(query)
                or _sh_intent in (QueryIntent.COMPARISON, QueryIntent.PREVIOUS_VALUE)
            )
            _sh_existing = {str(c["uuid"]) for c in candidate_inputs}
            _sh_added = 0
            for (subj, attr, scp, vk, un) in _obs_slots_for_history:
                if not subj or not attr or not vk:
                    continue
                # Read gate: history/comparison → always; current-state → only when
                # no scalar_state View exists (bounded support for unanchored slots).
                if not _sh_wants_history:
                    _state_view = await asyncio.to_thread(
                        service.graph_adapter.fetch_current_scalar_view_for_slot,
                        subject_uuid=subj, attribute=attr, scope=scp,
                        value_kind=vk, unit=un,
                        namespace=stamped_namespace(namespace),
                    )
                    if _state_view:
                        continue  # scalar_state leads; history stays off
                hv = await asyncio.to_thread(
                    service.graph_adapter.fetch_scalar_history,
                    subject_uuid=subj, attribute=attr, scope=scp,
                    value_kind=vk, unit=un,
                    namespace=stamped_namespace(namespace),
                )
                if not hv:
                    continue
                huuid = str(hv.get("uuid") or "").strip()
                if not huuid or huuid in _sh_existing:
                    continue
                _sh_existing.add(huuid)
                hcontent = _render_scalar_history_content(hv)
                hname = (
                    f"advisory history: {attr} ({scp})"
                    if scp else f"advisory history: {attr}"
                )
                candidate_inputs.append({
                    "uuid": huuid, "name": hname, "content": hcontent,
                    "scope": str(NodeScope.PERSISTENT),
                    "memory_type": "SCALAR_HISTORY",
                    # Below authority (1.0) but above generic floor — advisory rank.
                    "similarity": 0.85,
                    "last_accessed_days_ago": 0.0, "edge_count": 0,
                    "freshness": str(FreshnessState.ACTIVE),
                    "has_conflict": False, "conflict_status": None,
                    "source": CandidateSource.OBSERVATION,
                    "contributing_sources": frozenset({CandidateSource.OBSERVATION}),
                    "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                    "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                    "content_cosine": None, "is_superseded_view": False,
                    "view_kind": "scalar_history",
                    "is_scalar_authority": False,  # NEVER authority
                })
                metadata_by_uuid[huuid] = {
                    "name": hname, "content": hcontent,
                    "namespace": stamped_namespace(namespace),
                    "view_kind": "scalar_history", "view_current": True,
                    "entry_count": int(hv.get("entry_count") or 0),
                    "payload_entry_count": int(
                        hv.get("payload_entry_count")
                        if hv.get("payload_entry_count") is not None
                        else len(hv.get("entries") or [])
                    ),
                    "omitted_entry_count": int(hv.get("omitted_entry_count") or 0),
                }
                authority_layer.append(ScalarAuthorityVerdict(
                    kind="history", status="advisory",
                    subject_uuid=subj, attribute=attr, scope=scp,
                    value_kind=vk, unit=un,
                    value=None, valid_at=hv.get("last_valid_at"),
                    view_uuid=huuid, has_foundation=False,
                    contributors=tuple(
                        ScalarAuthorityContributor(
                            assertion_id=str(e.get("assertion_id") or ""),
                            relation="HISTORY_ENTRY",
                            operation=str(e.get("operation") or ""),
                            value=e.get("value"),
                            stated_span=str(e.get("stated_span") or ""),
                            valid_at=str(e.get("valid_at") or ""),
                            evidence_tier=str(e.get("evidence_tier") or ""),
                            episode_uuid=str(e.get("episode_uuid") or ""),
                        )
                        for e in (hv.get("entries") or [])
                    ),
                    contributors_total=int(hv.get("entry_count") or 0),
                    contributors_truncated=bool(int(hv.get("omitted_entry_count") or 0) > 0),
                    next_offset=(
                        int(hv.get("payload_entry_count") or len(hv.get("entries") or []))
                        if int(hv.get("omitted_entry_count") or 0) > 0 else None
                    ),
                ))
                _sh_added += 1
            logger.debug(
                "scalar-history advisory lane query=%r added=%d", query[:60], _sh_added)
        except Exception:
            logger.exception(
                "scalar-history advisory lane failed query=%r; leaving recall unchanged",
                query[:60],
            )
        _t_phases["scalar_history_lane"] = int((perf_counter() - _t) * 1000)

    # --- Step 7 canary: current-state View authority suppression (flag-gated) ---
    # When enabled, a current scalar_state View may suppress an older PROVENANCE-LINKED graph
    # fact for an explicit current-state query, but only if all six authority gates pass. Failures
    # never break recall (logged, recall proceeds unchanged). OFF by default -> no behavior change.
    if service.scalar_view_authority_enabled and namespace is not None and candidate_inputs:
        _t = perf_counter()
        try:
            suppressed = service._plan_view_authority_suppression(query, namespace, candidate_inputs)
            if suppressed:
                candidate_inputs = [
                    c for c in candidate_inputs if str(c["uuid"]) not in suppressed
                ]
        except Exception:
            logger.exception(
                "View-authority suppression failed query=%r; leaving recall unchanged", query[:60]
            )
        _t_phases["view_authority"] = int((perf_counter() - _t) * 1000)

    if not candidate_inputs:
        if visible_pending_rows:
            return RecallResult(
                query=query,
                preset=preset.value,
                results=service._pending_fallback_results(visible_pending_rows, preset, limit),
                candidates_evaluated=0,
                nodes_touched=0,
                note=(
                    "Recall search backend failed; pending results may be incomplete."
                    if search_error
                    else None
                ),
                search_error=search_error,
                authority_layer=tuple(authority_layer) or None,
            )
        return RecallResult(
            query=query,
            preset=preset.value,
            results=[],
            candidates_evaluated=0,
            nodes_touched=0,
            note=(
                "Recall search backend failed; this is not a confirmed zero-match result."
                if search_error
                else None
            ),
            search_error=search_error,
            authority_layer=tuple(authority_layer) or None,
        )

    # --- Adjacency ---
    _t = perf_counter()
    eligible_uuids = [str(c["uuid"]) for c in candidate_inputs]
    adjacency_map, edge_index = await service._compute_adjacency(
        eligible_uuids, context_node_ids, namespace,
    )
    _t_phases["adjacency"] = int((perf_counter() - _t) * 1000)

    candidates: list[CandidateData] = []
    for c in candidate_inputs:
        try:
            candidates.append(CandidateData(
                uuid=str(c["uuid"]),
                name=str(c["name"]),
                content=c["content"],
                scope=str(c["scope"]),
                memory_type=str(c["memory_type"]),
                similarity=float(c["similarity"]),
                last_accessed_days_ago=float(c["last_accessed_days_ago"]),
                edge_count=int(c["edge_count"]),
                adjacency_score=adjacency_map.get(str(c["uuid"]), 0.0),
                freshness=str(c["freshness"]),
                has_conflict=bool(c.get("has_conflict")),
                conflict_status=c.get("conflict_status") or None,
                source=c.get("source") or CandidateSource.VECTOR,  # type: ignore[arg-type]
                is_superseded_view=bool(c.get("is_superseded_view")),
                view_kind=c.get("view_kind") or None,
                retrieval_score=float(c["similarity"]),
                retrieval_score_kind=c.get("retrieval_score_kind")
                or RetrievalScoreKind.GRAPHITI_RRF,  # type: ignore[arg-type]
                bm25_rank=c.get("bm25_rank"),  # type: ignore[arg-type]
                cosine_rank=c.get("cosine_rank"),  # type: ignore[arg-type]
                contributing_sources=c.get("contributing_sources") or frozenset(),  # type: ignore[arg-type]
                content_rank=c.get("content_rank"),  # type: ignore[arg-type]
                content_cosine=c.get("content_cosine"),  # type: ignore[arg-type]
                is_scalar_authority=bool(c.get("is_scalar_authority")),
            ))
        except Exception as exc:
            logger.error(
                "Recall skipped invalid candidate uuid=%r: %s: %s",
                c.get("uuid"),
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )

    # --- Fact-edge STANDALONE candidates (opt-in): inject terse fact edges as answers ---
    # edge-as-answer: append the top EntityEdge.fact strings as CandidateSource.FACT_EDGE
    # candidates, built directly as CandidateData (bypassing the node-keyed metadata/scope/
    # adjacency path), floor-exempt, with oracle metadata seeded from the edge. NOTE:
    # measured net-NEGATIVE at N=30 (rung A′) because terse facts crowd out richer nodes and
    # collapse context — the "pointer" mode (hydrating endpoint nodes, above) is preferred.
    # Reuses edge_hits already fetched above; only runs in standalone mode.
    if tuning.enable_fact_edges and tuning.fact_edge_mode == "standalone":
        existing_uuids = {c.uuid for c in candidates}
        edges_added = 0
        for hit in edge_hits:
            try:
                edge_uuid = str(hit.get("uuid") or "").strip()
                score = float(hit["score"])
                fact = str(hit.get("fact") or "").strip()
                if not edge_uuid or not fact or not math.isfinite(score):
                    raise ValueError("fact-edge row has invalid uuid, fact, or score")
                if edge_uuid in existing_uuids:
                    continue
                existing_uuids.add(edge_uuid)
                candidates.append(
                    CandidateData(
                        uuid=edge_uuid,
                        name=fact,
                        content=fact,
                        scope=str(NodeScope.SESSION),
                        memory_type="SEMANTIC",
                        similarity=score,
                        last_accessed_days_ago=0.0,
                        edge_count=0,
                        adjacency_score=0.0,
                        freshness=str(FreshnessState.ACTIVE),
                        has_conflict=False,
                        conflict_status=None,
                        source=CandidateSource.FACT_EDGE,
                        retrieval_score=score,
                        retrieval_score_kind=RetrievalScoreKind.FACT_EDGE_RRF,
                    )
                )
                # Seed oracle metadata so the frontier reranker scores edges on strength.
                metadata_by_uuid[edge_uuid] = {
                    "name": fact,
                    "content": fact,
                    "namespace": namespace,
                    "evidence_kinds": ("timestamp",),
                    "created_at": hit.get("created_at"),
                    "valid_at": hit.get("valid_at"),
                    "invalid_at": hit.get("invalid_at"),
                    "expired_at": hit.get("expired_at"),
                }
                edges_added += 1
            except Exception as exc:
                logger.error(
                    "Recall skipped malformed standalone fact edge uuid=%r: %s: %s",
                    hit.get("uuid"),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
        logger.debug(
            "fact-edge standalone injection query=%r added=%d edges (k=%d)",
            query[:60], edges_added, tuning.fact_edge_k,
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
        await service._attach_frontier_metadata(eligible_uuids, metadata_by_uuid)
        if tuning.enable_belief_gate:
            try:
                fact_rows = await asyncio.to_thread(
                    service.graph_adapter.fetch_temporal_facts, eligible_uuids
                )
                for uuid, marks in _belief_markers_from_facts(fact_rows).items():
                    metadata_by_uuid.setdefault(uuid, {}).update(marks)
            except Exception:
                logger.exception(
                    "Belief-gate temporal fact fetch failed for %d uuids; "
                    "candidates treated as untimed", len(eligible_uuids),
                )
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

    # --- Phase 2: score + rank ---
    _t = perf_counter()
    scoring_trace = ScoringTrace() if trace else None
    # Under normalized mode (plan 1b) the search-score lane was divided by the
    # RRF max, so the floor scales by the same factor -> identical membership.
    _min_similarity = (
        MIN_SIMILARITY_THRESHOLD / GRAPHITI_RRF_DUAL_METHOD_MAX
        if tuning.similarity_scale == "normalized"
        else MIN_SIMILARITY_THRESHOLD
    )
    scored = service.scoring_service.score_candidates(
        candidates, preset, min_similarity=_min_similarity, trace=scoring_trace
    )
    _t_phases["scoring"] = int((perf_counter() - _t) * 1000)

    # --- Frontier portions (active): reorder by combiner / gate by wardens ---
    # Applied to the survivors BEFORE the top-k slice so it shapes which make the cut.
    # OFF by default -> this block is skipped and the path is byte-for-byte the old one.
    frontier_note: str | None = None
    if scored and (tuning.enable_oracle_ranking or tuning.enable_warden_gate or tuning.enable_belief_gate):
        _t = perf_counter()
        scored, frontier_note = await service._apply_frontier(
            query, namespace, scored, metadata_by_uuid, tuning,
            query_project=file_context_project,
        )
        _t_phases["frontier"] = int((perf_counter() - _t) * 1000)

    pending_fallback = service._pending_fallback_results(visible_pending_rows, preset, limit)
    top_results = pending_fallback + scored[:max(0, limit - len(pending_fallback))]

    # Signal when all candidates were below the similarity floor
    note = None
    if not scored and not pending_fallback and len(candidates) > 0:
        note = "No memories matched with sufficient relevance."
    elif not scored and pending_fallback and len(candidates) > 0:
        note = "Only pending (unprocessed) memories found; no enriched memories matched."
    if frontier_note:
        note = f"{note} | {frontier_note}" if note else frontier_note
    if search_error:
        warning = "Recall search backend failed; fallback results may be incomplete."
        note = f"{note} | {warning}" if note else warning
    if facet_active_warning:
        note = f"{note} | {facet_active_warning}" if note else facet_active_warning

    # --- Temporal facts enrichment (POST-RANK) ---
    _t = perf_counter()
    try:
        # Collect uuids of final results, excluding EPISODIC_PENDING
        result_uuids_for_facts = [
            r.uuid for r in top_results if r.memory_type != "EPISODIC_PENDING"
        ]
        if result_uuids_for_facts:
            fact_rows = await asyncio.to_thread(
                service.graph_adapter.fetch_temporal_facts, result_uuids_for_facts
            )
            if not include_invalidated:
                fact_rows = _filter_to_current_beliefs(fact_rows)
            facts_by_uuid = _build_temporal_facts(fact_rows)
            enriched_results = []
            for sm in top_results:
                if sm.memory_type == "EPISODIC_PENDING":
                    enriched_results.append(sm)
                else:
                    facts = facts_by_uuid.get(sm.uuid, ())
                    enriched_results.append(replace(sm, temporal_facts=facts))
            top_results = enriched_results
    except Exception:  # degraded mode: log and continue with empty temporal_facts
        logger.exception("Temporal facts enrichment failed; continuing with empty facts")
    _t_phases["temporal_enrichment"] = int((perf_counter() - _t) * 1000)

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

    # --- Stale-anchor verification enrichment (POST-LABEL) ---
    # For stale items, fetch the latest post-dirty verification receipt
    # and attach it to stale_anchor_info. Best-effort: failure or absence
    # leaves stale output unchanged and never breaks recall.
    _t = perf_counter()
    try:
        stale_anchor_specs: list[dict[str, Any]] = []
        for sm in top_results:
            info = sm.stale_anchor_info
            if info is not None and info.get("stale_anchor") is True:
                stale_anchor_specs.append({
                    "memory_uuid": sm.uuid,
                    "path": str(info.get("path") or ""),
                    "dirty_at": str(info.get("dirty_at") or ""),
                })
        if stale_anchor_specs:
            verifications = await asyncio.to_thread(
                service.graph_adapter.latest_stale_anchor_verifications,
                stale_anchors=stale_anchor_specs,
            )
            if verifications:
                enriched: list[ScoredMemory] = []
                for sm in top_results:
                    info = sm.stale_anchor_info
                    if info is not None and info.get("stale_anchor") is True:
                        key = (sm.uuid, str(info.get("path") or ""))
                        ver = verifications.get(key)
                        if ver is not None:
                            info = dict(info)
                            info["stale_verification"] = {
                                "outcome": ver.get("outcome"),
                                "verified_at": ver.get("verified_at"),
                                "verified_by": ver.get("verified_by"),
                                "basis": ver.get("basis"),
                            }
                            enriched.append(replace(sm, stale_anchor_info=info))
                        else:
                            enriched.append(sm)
                    else:
                        enriched.append(sm)
                top_results = enriched
    except Exception:
        logger.exception(
            "Stale-anchor verification enrichment failed; continuing without verifications"
        )
    _t_phases["verification_enrichment"] = int((perf_counter() - _t) * 1000)

    # --- Post-recall updates ---
    _t = perf_counter()
    nodes_touched = 0
    if update_access:
        try:
            nodes_touched = await service._post_recall_updates(
                top_results, metadata_by_uuid, edge_index
            )
        except Exception:
            logger.exception(
                "Post-recall access updates failed for %d results; returning results unchanged",
                len(top_results),
            )
    _t_phases["post_updates"] = int((perf_counter() - _t) * 1000)

    _t_phases["total"] = int((perf_counter() - _t_total) * 1000)
    logger.info(
        "recall latency breakdown query=%r preset=%s candidates=%d results=%d phases=%s",
        query[:60],
        preset.value,
        len(candidates),
        len(top_results),
        _t_phases,
    )

    retrieval_trace = None
    if scoring_trace is not None:
        # Shadow oracle/warden pass (observe-only): runs only when a trace is
        # requested and the flag is on, never changes `top_results`, and must never
        # break recall — any failure is logged and recorded, not propagated.
        assertion_shadow = None
        if tuning.enable_assertion_shadow and candidate_inputs:
            _t = perf_counter()
            try:
                assertion_shadow = await service._run_assertion_shadow(
                    query, namespace, candidate_inputs, metadata_by_uuid,
                    query_project=file_context_project,
                    tuning=tuning,
                )
                record_mcp_event(
                    kind="background",
                    operation="assertion_shadow",
                    payload={"query": query[:60], "candidates": len(candidate_inputs)},
                    result={
                        "admitted": assertion_shadow.admitted,
                        "flagged": assertion_shadow.flagged,
                        "refused": assertion_shadow.refused,
                        "intent": assertion_shadow.intent,
                    },
                    duration_ms=int((perf_counter() - _t) * 1000),
                    success=True,
                )
            except Exception:  # observe-only: never break recall
                logger.exception("Assertion shadow pass failed query=%r", query[:60])
                record_mcp_event(
                    kind="background",
                    operation="assertion_shadow",
                    payload={"query": query[:60], "candidates": len(candidate_inputs)},
                    result={"error": "assertion_shadow_failed"},
                    duration_ms=int((perf_counter() - _t) * 1000),
                    success=False,
                )
        # FACET candidate-generation shadow (observe-only, default-off): what would
        # CandidateSource.FACET contribute over this pool? Never changes top_results;
        # any failure is logged + recorded, not propagated.
        facet_shadow = None
        if tuning.enable_facet_shadow and candidate_inputs:
            _t = perf_counter()
            try:
                facet_shadow = await service._run_facet_pass(
                    query, namespace,
                    [str(c["uuid"]) for c in candidate_inputs],
                    query_project=file_context_project,
                )
                record_mcp_event(
                    kind="background",
                    operation="facet_shadow",
                    payload={"query": query[:60], "pool": facet_shadow.pool},
                    result={"candidates": facet_shadow.candidates},
                    duration_ms=int((perf_counter() - _t) * 1000),
                    success=True,
                )
            except Exception:  # observe-only: never break recall
                logger.exception("Facet shadow pass failed query=%r", query[:60])
                record_mcp_event(
                    kind="background",
                    operation="facet_shadow",
                    payload={"query": query[:60], "candidates": len(candidate_inputs)},
                    result={"error": "facet_shadow_failed"},
                    duration_ms=int((perf_counter() - _t) * 1000),
                    success=False,
                )
        # D0 view-reachability (trace-only, deterministic): where did the first
        # current View land in the shipped results? A View is query-sufficient
        # state, so its rank IS the delivered retrieval entropy for its query
        # class — no labels, no LLM. view_kind/view_current come from the
        # metadata already fetched; a superseded version never counts.
        from menhir.services.view_entropy import estimate_footprint_tokens

        view_reachability = None
        _walk_tokens = 0
        for _rank, _sm in enumerate(top_results, start=1):
            _walk_tokens += estimate_footprint_tokens(_sm.content or _sm.name)
            _meta = metadata_by_uuid.get(_sm.uuid) or {}
            if _meta.get("view_kind") and _meta.get("view_current") is not False:
                view_reachability = ViewReachability(
                    uuid=_sm.uuid,
                    view_kind=str(_meta["view_kind"]),
                    rank=_rank,
                    tokens_to_view=_walk_tokens,
                )
                # Emit telemetry for view-reachability outcomes (best-effort)
                try:
                    record_mcp_event(
                        kind="background",
                        operation="view_reachability",
                        payload={
                            "query": query[:60],
                            "namespace": namespace,
                            "view_kind": str(_meta["view_kind"]),
                            "rank": _rank,
                            "tokens_to_view": _walk_tokens,
                            "result_count": len(top_results),
                        },
                        success=True,
                    )
                except Exception:  # telemetry: never break retrieval
                    logger.exception("view_reachability event recording failed")
                break
        # Emit view_absent event when no current View surfaced
        if view_reachability is None:
            try:
                record_mcp_event(
                    kind="background",
                    operation="view_reachability",
                    payload={
                        "query": query[:60],
                        "namespace": namespace,
                        "result_count": len(top_results),
                    },
                    result={"view_absent": True},
                    success=True,
                )
            except Exception:  # telemetry: never break retrieval
                logger.exception("view_absent event recording failed")
        retrieval_trace = RetrievalTrace(
            query=query,
            preset=preset.value,
            total_ms=_t_phases["total"],
            phases=dict(_t_phases),
            candidates=list(scoring_trace.candidates),
            assertion_shadow=assertion_shadow,
            view_reachability=view_reachability,
            facet_shadow=facet_shadow,
            facet_active=facet_active_trace,
            rank_shadow_warning=rank_shadow_warning,
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
        subject_uuid = str(uuid5(NAMESPACE_URL, f"menhir-self:{stamped}"))
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
