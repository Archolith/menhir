"""Scoring service — pure computation, no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass

from menhir.domain.memory_types import get_policy
from menhir.domain.recall import (
    CandidateData,
    PRESET_WEIGHTS,
    QueryPreset,
    ScoredMemory,
)
from menhir.domain.retrieval_tuning import FLOOR_EXEMPT_SOURCES
from menhir.domain.retrieval_trace_models import (
    CandidateTrace,
    RelevanceBreakdown,
    ScoringTrace,
)


_DEFAULT_RECENCY_LAMBDA = 0.1


def _record_floored(trace: ScoringTrace, floored: list[CandidateData]) -> None:
    """Append floored (dropped-before-scoring) candidates to an R0 trace.

    Floored candidates carry similarity + source + survived_floor=False only;
    score_parts/final_score/rank stay None because they were never scored.
    """
    for c in floored:
        trace.candidates.append(
            CandidateTrace(
                uuid=c.uuid,
                source=c.source,
                similarity=_safe_float(c.similarity),
                survived_floor=False,
                bm25_rank=c.bm25_rank,
                cosine_rank=c.cosine_rank,
                contributing_sources=c.contributing_sources,
                content_rank=c.content_rank,
                content_cosine=c.content_cosine,
            )
        )


def _safe_float(value: float, fallback: float = 0.0) -> float:
    """Coerce NaN/Inf to a safe fallback."""
    if math.isnan(value) or math.isinf(value):
        return fallback
    return value


# Candidates below this score are noise and should be filtered before scoring.
#
# NOTE (verified live 2026-06-28, see .agent/plans/deferred-verification.md): the
# `similarity` this gates is graphiti's RRF node_reranker_score (search_scored),
# NOT a cosine similarity. With graphiti's default rank_const=1 the score is
# sum_methods 1/(rank+1) -- a dual-method top hit = 2.0, single-method top = 1.0 --
# so a 0.15 floor behaves as a RANK CUT (~top 12-13 dual / ~top 6 single), not a
# cosine cutoff. The value is reasonable only by luck of rank_const=1; revisit the
# scale (rescale or re-document as a rank cut) before tuning hybrid_alpha.
#
# The floor is SOURCE-AWARE: it only gates candidates whose relevance is a
# vector-similarity score (CandidateSource.VECTOR). Candidates that were injected
# by provenance -- an exact lexical/BM25 hit, a linked pending episode, a
# structural file neighbor -- carry their relevance in their source, not in the
# RRF score, and must not be silently dropped by this gate. Those sources are
# listed in FLOOR_EXEMPT_SOURCES (see domain/retrieval_tuning.py).
#
# SCALE CONTRACT (plan 1a): this floor is a RANK CUT on graphiti's RRF scale, not
# a cosine threshold. graphiti's NodeReranker.rrf scores each hit as
# sum_methods 1/(rank + rank_const) with rank_const=1, so a dual-method (bm25 +
# cosine) top hit tops out at 1/(0+1) + 1/(0+1) = GRAPHITI_RRF_DUAL_METHOD_MAX.
# 0.15 on that scale admits roughly the top ~12-13 dual / ~6 single hits.
# test_scoring_service::test_graphiti_rrf_scale_contract pins the rank_const=1
# assumption against the graphiti package and fails loudly if upstream changes it.
# Semantic normalization of this lane to [0,1] is a ranking change, staged behind
# RetrievalTuningConfig.similarity_scale="normalized" (plan 1b); default "rrf" is
# today's behavior byte-for-byte.
MIN_SIMILARITY_THRESHOLD = 0.15

# The dual-method (bm25 + cosine) top-hit ceiling of graphiti's RRF reranker score
# under rank_const=1. This is the scale MIN_SIMILARITY_THRESHOLD and the [0,1]
# SOURCE_PRIORS are (accidentally) mixed across today; plan 1b's normalized mode
# divides search scores by this constant to put them on the [0,1] prior scale.
# Pinned by test_graphiti_rrf_scale_contract so an upstream RRF change is caught.
GRAPHITI_RRF_DUAL_METHOD_MAX = 2.0


@dataclass
class ScoringService:
    """Applies the relevance formula to candidate metadata and returns ranked results."""

    def score_candidates(
        self,
        candidates: list[CandidateData],
        preset: QueryPreset,
        *,
        min_similarity: float = MIN_SIMILARITY_THRESHOLD,
        trace: ScoringTrace | None = None,
    ) -> list[ScoredMemory]:
        if not candidates:
            return []

        # Filter out vector candidates with negligible similarity before scoring.
        # This prevents "best of bad options" results for off-topic queries.
        # Floor-exempt sources (BM25/pending/file-linked/...) survive regardless:
        # their relevance is provenance, not cosine similarity.
        def _passes_floor(candidate: CandidateData) -> bool:
            return (
                candidate.source in FLOOR_EXEMPT_SOURCES
                or not math.isfinite(candidate.similarity)  # NaN/Inf: let _safe_float coerce in scoring
                or candidate.similarity >= min_similarity
            )

        # R0 trace (opt-in): record floored candidates before they are dropped.
        # When trace is None this list is empty and the path is unchanged.
        floored = [c for c in candidates if trace is not None and not _passes_floor(c)]
        candidates = [c for c in candidates if _passes_floor(c)]
        if not candidates:
            if trace is not None:
                _record_floored(trace, floored)
            return []

        alpha, beta, gamma, delta = PRESET_WEIGHTS[preset]

        max_log_edge = max(
            (math.log(1 + max(0, c.edge_count)) for c in candidates),
            default=0.0,
        )
        max_log_edge = _safe_float(max_log_edge)

        scored: list[ScoredMemory] = []
        for c in candidates:
            policy = get_policy(c.memory_type)
            recency_lambda = policy.scoring_recency_lambda
            type_boost = policy.scoring_boost

            similarity = _safe_float(c.similarity)
            days_ago = _safe_float(c.last_accessed_days_ago)
            adj_raw = _safe_float(c.adjacency_score)

            recency = math.exp(-recency_lambda * days_ago)
            recency = max(0.0, min(1.0, recency))

            raw_prominence = math.log(1 + max(0, c.edge_count))
            raw_prominence = _safe_float(raw_prominence)
            prominence = raw_prominence / max_log_edge if max_log_edge > 0 else 0.0

            adjacency = max(0.0, min(1.0, adj_raw))

            # Conflict boost: 1.0 if this node is actively in an unresolved conflict,
            # 0.5 if conflict exists but resolved/auto-resolved, 0.0 otherwise.
            if c.has_conflict and c.conflict_status == "unresolved":
                conflict_signal = 1.0
            elif c.has_conflict:
                conflict_signal = 0.5
            else:
                conflict_signal = 0.0

            relevance = (
                similarity
                + alpha * adjacency
                + beta * recency
                + gamma * prominence
                + delta * conflict_signal
                + type_boost
            )

            breakdown = RelevanceBreakdown(
                semantic_similarity=similarity,
                adjacency_bonus=adjacency,
                recency_bonus=recency,
                prominence_bonus=prominence,
                conflict_bonus=conflict_signal,
                type_boost=type_boost,
                preset=preset.value,
                alpha=alpha,
                beta=beta,
                gamma=gamma,
                delta=delta,
            )

            scored.append(
                ScoredMemory(
                    uuid=c.uuid,
                    name=c.name,
                    content=c.content,
                    scope=c.scope,
                    memory_type=c.memory_type,
                    final_score=relevance,
                    breakdown=breakdown,
                    retrieval_score=(
                        _safe_float(c.retrieval_score)
                        if c.retrieval_score is not None
                        else similarity
                    ),
                    retrieval_score_kind=c.retrieval_score_kind,
                    is_superseded_view=c.is_superseded_view,
                    view_kind=c.view_kind,
                    is_scalar_authority=c.is_scalar_authority,
                )
            )

        scored.sort(key=lambda s: s.final_score, reverse=True)

        if trace is not None:
            survivor_by_uuid = {c.uuid: c for c in candidates}
            for rank, s in enumerate(scored):
                c = survivor_by_uuid[s.uuid]
                trace.candidates.append(
                    CandidateTrace(
                        uuid=s.uuid,
                        source=c.source,
                        similarity=_safe_float(c.similarity),
                        survived_floor=True,
                        score_parts=s.breakdown,
                        final_score=s.final_score,
                        rank=rank,
                        bm25_rank=c.bm25_rank,
                        cosine_rank=c.cosine_rank,
                        contributing_sources=c.contributing_sources,
                        content_rank=c.content_rank,
                        content_cosine=c.content_cosine,
                    )
                )
            _record_floored(trace, floored)

        return scored
