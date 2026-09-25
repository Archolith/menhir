"""Candidate assembly and post-ranking enrichment stages for the recall pipeline."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import replace
from time import perf_counter
from typing import Any

from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.recall import (
    CandidateData,
    QueryPreset,
    RecallResult,
    RetrievalScoreKind,
    ScalarAuthorityVerdict,
    ScoredMemory,
)
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalTuningConfig
from menhir.domain.retrieval_trace_models import ScoringTrace
from menhir.services.recall_policies import (
    _belief_markers_from_facts,
    _build_temporal_facts,
    _filter_to_current_beliefs,
)
from menhir.services.scoring_service import GRAPHITI_RRF_DUAL_METHOD_MAX, MIN_SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)


def _empty_pool_result(
    service: Any,
    query: str,
    candidate_inputs: list[dict[str, object]],
    visible_pending_rows: list[dict[str, object]],
    preset: QueryPreset,
    limit: int,
    search_error: str | None,
    authority_layer: list[ScalarAuthorityVerdict],
) -> RecallResult | None:
    """Return the pending-fallback / confirmed-empty result, or None when candidates exist."""
    if candidate_inputs:
        return None
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


def _build_candidate_data(
    candidate_inputs: list[dict[str, object]],
    adjacency_map: dict[str, float],
) -> list[CandidateData]:
    """Materialize validated CandidateData rows from the filtered candidate inputs."""
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
    return candidates


def _inject_standalone_fact_edges(
    candidates: list[CandidateData],
    edge_hits: list[dict[str, Any]],
    metadata_by_uuid: dict[str, dict[str, object]],
    namespace: str | None,
    tuning: RetrievalTuningConfig,
    query: str,
) -> None:
    """Inject terse fact edges as floor-exempt answers (standalone mode only)."""
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


async def _attach_frontier_pool_metadata(
    service: Any,
    eligible_uuids: list[str],
    metadata_by_uuid: dict[str, dict[str, object]],
    tuning: RetrievalTuningConfig,
) -> None:
    """Attach frontier provenance and belief-gate temporal-fact markers to the metadata."""
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


async def _score_candidates(
    service: Any,
    candidates: list[CandidateData],
    preset: QueryPreset,
    tuning: RetrievalTuningConfig,
    trace: bool,
    _t_phases: dict[str, int],
) -> tuple[list[ScoredMemory], ScoringTrace | None]:
    """Score and rank candidates; floor scales with the normalized-similarity mode."""
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
    return scored, scoring_trace


async def _apply_frontier_portions(
    service: Any,
    query: str,
    namespace: str | None,
    scored: list[ScoredMemory],
    metadata_by_uuid: dict[str, dict[str, object]],
    tuning: RetrievalTuningConfig,
    file_context_project: str | None,
    _t_phases: dict[str, int],
) -> tuple[list[ScoredMemory], str | None]:
    """Apply active frontier combiner/warden portions to the scored survivors."""
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
    return scored, frontier_note


def _compose_recall_note(
    scored: list[ScoredMemory],
    pending_fallback: list[ScoredMemory],
    candidates: list[CandidateData],
    frontier_note: str | None,
    search_error: str | None,
    facet_active_warning: str | None,
) -> str | None:
    """Compose the recall note from floor, frontier, search-error, and facet signals."""
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
    return note


async def _enrich_temporal_facts(
    service: Any,
    top_results: list[ScoredMemory],
    include_invalidated: bool,
    _t_phases: dict[str, int],
) -> list[ScoredMemory]:
    """Enrich final results with temporal facts (POST-RANK); degraded mode keeps empty facts."""
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
    return top_results


async def _attach_stale_verifications(
    service: Any,
    top_results: list[ScoredMemory],
    _t_phases: dict[str, int],
) -> list[ScoredMemory]:
    """Attach latest post-dirty verification receipts to stale anchor info (POST-LABEL)."""
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
    return top_results


async def _run_post_recall_updates(
    service: Any,
    top_results: list[ScoredMemory],
    metadata_by_uuid: dict[str, dict[str, object]],
    edge_index: dict[str, object],
    update_access: bool,
    _t_phases: dict[str, int],
) -> int:
    """Best-effort post-recall access updates; returns the number of nodes touched."""
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
    return nodes_touched
