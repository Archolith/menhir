"""Candidate generation for recall: search lanes, facet fusion, pending and file-context pools."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.recall import QueryPreset, RecallResult, RetrievalScoreKind
from menhir.domain.retrieval_trace_models import FacetShadowTrace
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalTuningConfig
from menhir.services.hybrid_retrieval import FusionLane, hybrid_search, weighted_rrf_multi
from menhir.services.recall_policies import (
    FILE_LINKED_BASELINE_SIMILARITY,
    PENDING_ENTITY_SIMILARITY,
    _query_wants_history,
)
from menhir.services.scoring_service import GRAPHITI_RRF_DUAL_METHOD_MAX

logger = logging.getLogger(__name__)


@dataclass
class _CandidatePool:
    """Shared candidate-pool state produced by search and consumed by the recall pipeline."""

    candidate_uuids: list[str]
    similarity_map: dict[str, float]
    source_map: dict[str, CandidateSource]
    contributing_source_map: dict[str, frozenset[CandidateSource]]
    score_kind_map: dict[str, RetrievalScoreKind]
    rank_shadow: dict[str, dict[str, int]]
    content_cosine_map: dict[str, float]
    edge_hits: list[dict[str, Any]]
    search_error: str | None
    rank_shadow_warning: str | None
    facet_active_trace: FacetShadowTrace | None
    facet_active_warning: str | None


async def _generate_candidates(
    service: Any,
    query: str,
    preset: QueryPreset,
    candidate_k: int,
    limit: int,
    namespace: str | None,
    tuning: RetrievalTuningConfig,
    trace: bool,
    visible_pending_rows: list[dict[str, object]],
    pending_entity_uuids: list[str],
    file_context: str | None,
    file_context_project: str | None,
    _t_phases: dict[str, int],
) -> tuple[_CandidatePool | None, RecallResult | None]:
    """Run every candidate-generation lane; return the pool, or an early RecallResult."""
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
            return None, RecallResult(
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
            return None, RecallResult(
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
    return (
        _CandidatePool(
            candidate_uuids=candidate_uuids,
            similarity_map=similarity_map,
            source_map=source_map,
            contributing_source_map=contributing_source_map,
            score_kind_map=score_kind_map,
            rank_shadow=rank_shadow,
            content_cosine_map=content_cosine_map,
            edge_hits=edge_hits,
            search_error=search_error,
            rank_shadow_warning=rank_shadow_warning,
            facet_active_trace=facet_active_trace,
            facet_active_warning=facet_active_warning,
        ),
        None,
    )
