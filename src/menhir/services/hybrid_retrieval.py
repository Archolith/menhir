"""Hybrid candidate generation — attributed vector + BM25 with weighted RRF.

This is the attributed counterpart to ``graphiti_client.search_scored``. Where
``search_scored`` fuses BM25 + cosine inside graphiti's RRF reranker into one
opaque score (and discards which method found each node), this module runs the
two methods as *separate labeled passes* and blends them with a tunable
weighted reciprocal-rank fusion. The output carries each candidate's
:class:`CandidateSource`, which lets the source-aware floor keep exact lexical
hits that would otherwise fall under the vector-similarity floor.

Why fuse on rank, not raw score: BM25 scores and cosine scores live on different,
incomparable scales, so a linear ``alpha*cosine + (1-alpha)*bm25`` blend is
ill-defined. Reciprocal-rank fusion combines the two by *position*, so the
``hybrid_alpha`` weight is well-defined regardless of either method's score
distribution.

Open item (cannot be verified in this repo's sandbox, where graphiti is not
importable): the absolute scale of the normalized fused ``similarity`` relative
to ``MIN_SIMILARITY_THRESHOLD`` for VECTOR-only candidates. The source-aware
floor makes correctness independent of that scale for BM25/injected sources;
the vector-only floor interaction should be confirmed against a live graphiti /
archolith-bench trace before this path is promoted past experimental.

See ``docs/research/retrieval-tuning-stack.md`` (hybrid alpha) and the R1 rung in
``.agent/plans/menhir-research-execution-ladder.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from menhir.domain.retrieval_tuning import CandidateSource, RetrievalTuningConfig

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient
    from menhir.infrastructure.graphiti_client import GraphitiClient

# Production-parity reciprocal-rank constant. Graphiti's node RRF implementation
# defaults to rank_const=1 and Menhir's score contract is pinned to that value in
# test_scoring_service::test_graphiti_rrf_scale_contract. Using the more common
# k=60 changes ordering, not merely scale, so the benchmark control must use 1.
RRF_K = 1
RRF_COMMON_CEILING = 2.0

ADMISSION_PRODUCTION_FUSED = "production_fused"
ADMISSION_ATTRIBUTED = "attributed"

# Method-name (graphiti) -> attributed source.
_METHOD_SOURCE: dict[str, CandidateSource] = {
    "cosine_similarity": CandidateSource.VECTOR,
    "bm25": CandidateSource.BM25,
}


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
    scale = RRF_COMMON_CEILING / active_weight if active_weight > 0.0 else 0.0

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


def weighted_rrf(
    vector_hits: list[tuple[str, str]],
    bm25_hits: list[tuple[str, str]],
    *,
    alpha: float,
    k: int = RRF_K,
    limit: int | None = None,
    admission_policy: str = ADMISSION_ATTRIBUTED,
) -> list[HybridCandidate]:
    """Two-lane weighted RRF (vector weighted ``alpha``, BM25 weighted ``1 - alpha``).

    Thin wrapper over :func:`weighted_rrf_multi` so the two-lane control path and the
    N-lane path share ONE implementation -- three-lane fusion cannot silently diverge
    from the two-lane baseline that must reproduce production. ``alpha == 1.0`` is
    vector-only, ``0.0`` BM25-only, ``0.5`` symmetric. A BM25-pass candidate is admitted
    :attr:`CandidateSource.BM25` (floor-exempt) even if the vector pass also found it.
    """
    return weighted_rrf_multi(
        [
            FusionLane(CandidateSource.BM25, list(bm25_hits), weight=1.0 - alpha),
            FusionLane(CandidateSource.VECTOR, list(vector_hits), weight=alpha),
        ],
        k=k,
        limit=limit,
        admission_policy=admission_policy,
    )


async def hybrid_search(
    graphiti_client: GraphitiClient | UnavailableGraphitiClient,
    query: str,
    *,
    config: RetrievalTuningConfig,
    num_results: int = 50,
    group_ids: list[str] | None = None,
) -> list[HybridCandidate]:
    """Run attributed vector + BM25 passes and fuse them per ``config``.

    Returns up to ``num_results`` fused candidates, each tagged with its source.
    Method selection follows ``config.hybrid_alpha``: a pure end (alpha 0 or 1)
    skips the unused pass entirely to save a round-trip.
    """
    methods: list[str] = []
    if config.hybrid_alpha > 0.0:
        methods.append("cosine_similarity")
    if config.hybrid_alpha < 1.0:
        methods.append("bm25")
    if not methods:  # defensive: alpha is validated to [0,1], so this is unreachable
        methods = ["cosine_similarity", "bm25"]

    ranked = await graphiti_client.search_ranked_by_method(
        query, methods=methods, num_results=num_results, group_ids=group_ids
    )

    return weighted_rrf(
        ranked.get("cosine_similarity", []),
        ranked.get("bm25", []),
        alpha=config.hybrid_alpha,
        limit=num_results,
        admission_policy=config.fusion_admission_policy,
    )
