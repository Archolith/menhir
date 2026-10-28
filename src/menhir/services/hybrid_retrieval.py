"""N-lane weighted reciprocal-rank fusion for attributed candidate lanes.

Provides :func:`weighted_rrf_multi`, which fuses labeled, weighted, rank-ordered
:class:`FusionLane` passes (the facet lane uses it) into :class:`HybridCandidate`
rows carrying each candidate's :class:`CandidateSource`. Fusing on rank, not raw
score, lets lanes on incomparable scales combine without a scale contract. The
BM25 hybrid candidate-generation path was removed 2026-10-27 (archived on
archive/bucket1-features-2026-10-27); the BM25 source and admission precedence
remain as trace vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.retrieval_tuning import CandidateSource
from menhir.services.scoring_service import GRAPHITI_RRF_DUAL_METHOD_MAX

# Production-parity reciprocal-rank constant. Graphiti's node RRF implementation
# defaults to rank_const=1 and Menhir's score contract is pinned to that value in
# test_scoring_service::test_graphiti_rrf_scale_contract. Using the more common
# k=60 changes ordering, not merely scale, so the benchmark control must use 1.
RRF_K = 1

ADMISSION_PRODUCTION_FUSED = "production_fused"
ADMISSION_ATTRIBUTED = "attributed"


@dataclass(frozen=True)
class HybridCandidate:
    """One fused candidate with its attributed source and a normalized score.

    ``source`` is the ADMISSION source -- a single value that drives ranking (floor
    exemption, priors), assigned by a fixed precedence so it matches production's
    one-source-per-candidate policy. ``contributing_sources`` is the FULL set of
    lanes that produced the candidate -- provenance only, never read by scoring.
    Keeping the two apart is load-bearing: if merely *contributing* a floor-exempt
    lane (e.g. BM25) granted exemption, a control arm would diverge from production.
    """

    uuid: str
    name: str
    similarity: float  # normalized to the pinned production ceiling [0, 2]
    source: CandidateSource
    contributing_sources: frozenset[CandidateSource] = frozenset()


@dataclass(frozen=True)
class FusionLane:
    """One labeled, weighted, rank-ordered pass for N-lane fusion.

    ``hits`` is a best-first list of ``(uuid, name)``. ``weight`` scales this lane's
    reciprocal-rank contribution. ``source`` tags every candidate this lane produces.
    """

    source: CandidateSource
    hits: list[tuple[str, str]]
    weight: float = 1.0


# Admission precedence when a candidate is produced by several lanes. Floor-exempt
# sources come first, so an exact lexical (BM25) hit is never demoted to a floored
# source; among the semantic lanes VECTOR precedes CONTENT_VECTOR, so a name-vector
# hit keeps VECTOR admission even when content-vector also found it -- preserving
# today's behavior when the content lane is off.
_ADMISSION_PRECEDENCE: tuple[CandidateSource, ...] = (
    CandidateSource.BM25,
    CandidateSource.FILE_LINKED,
    CandidateSource.PENDING,
    CandidateSource.FACET,
    CandidateSource.STRUCTURE,
    CandidateSource.FACT_EDGE,
    CandidateSource.VECTOR,
    CandidateSource.CONTENT_VECTOR,
)


def _admission_source(
    sources: set[CandidateSource], *, admission_policy: str
) -> CandidateSource:
    if admission_policy == ADMISSION_PRODUCTION_FUSED:
        # Today's opaque graphiti BM25+name-vector path labels every fused hit VECTOR.
        # Keep that rank-affecting contract for benchmark Arms A/B/C; the complete
        # lane set remains available in contributing_sources for provenance.
        return CandidateSource.VECTOR
    if admission_policy != ADMISSION_ATTRIBUTED:
        raise ValueError(f"unsupported admission_policy: {admission_policy!r}")
    for candidate in _ADMISSION_PRECEDENCE:
        if candidate in sources:
            return candidate
    return next(iter(sources))  # defensive: an unlisted source


def weighted_rrf_multi(
    lanes: list[FusionLane],
    *,
    k: int = RRF_K,
    limit: int | None = None,
    admission_policy: str = ADMISSION_PRODUCTION_FUSED,
) -> list[HybridCandidate]:
    """Fuse N labeled, weighted, rank-ordered passes by weighted reciprocal-rank fusion.

    Fuses on RANK, not raw score, so lanes on incomparable scales (BM25 vs cosine vs
    content-cosine) combine without a scale contract: each lane contributes
    ``weight * 1/(k + rank)``, summed per candidate, then divided by the sum of active
    weights and multiplied by the pinned production dual-lane ceiling (2.0). This
    fixed, data-independent normalization makes an equal-weight two-lane fusion
    numerically identical to Graphiti production RRF, while a third lane cannot raise
    the ceiling merely by existing.

    Each candidate records the full set of contributing lane sources and a single
    rank-affecting admission source. The default ``production_fused`` policy labels
    every fused hit VECTOR, matching today's opaque graphiti path; ``attributed``
    retains the existing source-aware hybrid behavior. Ordering is fused-score
    descending with stable first-appearance tie-breaks (lanes processed in argument
    order). Zero-weight lanes still contribute membership (a candidate found only by a
    zero-weight lane is present with fused score 0.0), preserving the alpha-endpoint
    behavior of the two-lane form.
    """
    fused: dict[str, float] = {}
    names: dict[str, str] = {}
    contributing: dict[str, set[CandidateSource]] = {}
    order: list[str] = []  # deterministic first-appearance order for stable ties

    for lane in lanes:
        for rank, (uuid, name) in enumerate(lane.hits):
            if uuid not in fused:
                order.append(uuid)
                names[uuid] = name
                fused[uuid] = 0.0
                contributing[uuid] = set()
            fused[uuid] += lane.weight * (1.0 / (k + rank))
            contributing[uuid].add(lane.source)

    if not fused:
        return []

    active_weight = sum(max(0.0, lane.weight) for lane in lanes)
    scale = GRAPHITI_RRF_DUAL_METHOD_MAX / active_weight if active_weight > 0.0 else 0.0

    ranked_uuids = sorted(order, key=lambda u: fused[u], reverse=True)
    if limit is not None:
        ranked_uuids = ranked_uuids[:limit]

    return [
        HybridCandidate(
            uuid=uuid,
            name=names[uuid],
            similarity=fused[uuid] * scale,
            source=_admission_source(
                contributing[uuid], admission_policy=admission_policy
            ),
            contributing_sources=frozenset(contributing[uuid]),
        )
        for uuid in ranked_uuids
    ]

