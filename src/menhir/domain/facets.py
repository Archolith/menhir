"""Facet-overlap candidate generation + meet-point reranking (R2, ported).

Pure-domain port of the graduated bench mechanism (``archolith_bench/facet/``): a
deterministic candidate *generator* by facet overlap, plus a convergence reranker.
No I/O, no graph, no similarity model — it operates on facet sets that a caller
derives elsewhere (from ``ANCHORED_TO``/``DEFINES`` for structural facets, from
metadata/text for interpretive/scope facets). See
``.agent/plans/r2-facet-production-integration.md``.

One mechanism, two fashions (only the facet *population* differs):
- **structural** (file/symbol/test) — precise code-convergence for anchored memories;
- **interpretive/scope** (operation/object/actor/evidence + repo/project/namespace +
  belief) — coarse topical grouping + scope/stale signal for *any* memory.

Integration note: the reranker's scope/stale *penalties* below reproduce the
bench-validated behavior for standalone testing, but in recall integration that
discipline is owned by the ``ScopeWarden``/``CurrentnessWarden`` chain — the wiring
layer defers to the wardens rather than double-penalizing. This module is the
candidate-generation + convergence engine; the wardens keep the discipline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

SET_FACETS: tuple[str, ...] = (
    "actor", "object", "operation", "file", "symbol", "test", "evidence_type",
)
SCALAR_FACETS: tuple[str, ...] = ("source_id", "repo", "project", "namespace", "belief_bucket")
TEMPORAL_FACETS: tuple[str, ...] = ("valid_time", "learned_time")
SCOPE_FACETS: tuple[str, ...] = ("repo", "project", "namespace")
STALE_BUCKETS: frozenset[str] = frozenset({"historical", "anergic", "blocked"})


@dataclass
class MemoryFacetSet:
    """Explicit facet labels for one memory or one query's constraints."""

    actor: set[str] = field(default_factory=set)
    object: set[str] = field(default_factory=set)
    operation: set[str] = field(default_factory=set)
    file: set[str] = field(default_factory=set)
    symbol: set[str] = field(default_factory=set)
    test: set[str] = field(default_factory=set)
    evidence_type: set[str] = field(default_factory=set)
    source_id: str | None = None
    repo: str | None = None
    project: str | None = None
    namespace: str | None = None
    belief_bucket: str | None = None
    valid_time: str | None = None
    learned_time: str | None = None

    def values(self, facet: str) -> set[str]:
        """Value set for any facet (scalars yield a 0/1-element set)."""
        raw = getattr(self, facet)
        if isinstance(raw, set):
            return raw
        return {raw} if raw else set()

    def discrete_pairs(self) -> set[tuple[str, str]]:
        """All (facet, value) pairs usable for overlap indexing (temporal excluded)."""
        pairs: set[tuple[str, str]] = set()
        for facet in SET_FACETS:
            for value in getattr(self, facet):
                pairs.add((facet, value))
        for facet in SCALAR_FACETS:
            value = getattr(self, facet)
            if value:
                pairs.add((facet, value))
        return pairs


@dataclass
class FacetedMemory:
    """A memory reduced to its facets for the overlap engine."""

    id: str
    facets: MemoryFacetSet = field(default_factory=MemoryFacetSet)
    superseded: bool = False

    @property
    def is_stale(self) -> bool:
        return self.superseded or (self.facets.belief_bucket in STALE_BUCKETS)


@dataclass
class FacetedQuery:
    """A query's facet constraints for the overlap engine."""

    facets: MemoryFacetSet = field(default_factory=MemoryFacetSet)
    required_facets: list[str] = field(default_factory=list)
    intent: str = "current"  # current | historical | any


class MemoryFacetIndex:
    """Inverted index from (facet, value) pairs to memory ids (candidate generator)."""

    def __init__(self) -> None:
        self._postings: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._id_set: set[str] = set()

    def add(self, memory: FacetedMemory) -> None:
        self._id_set.add(memory.id)
        for pair in memory.facets.discrete_pairs():
            self._postings[pair].add(memory.id)

    def build(self, memories: list[FacetedMemory]) -> "MemoryFacetIndex":
        for memory in memories:
            self.add(memory)
        return self

    def candidates(self, query_facets: MemoryFacetSet) -> list[tuple[str, int]]:
        """(memory_id, overlap_count) for memories sharing >=1 facet pair, overlap-then-id sorted."""
        overlap: dict[str, int] = defaultdict(int)
        for pair in query_facets.discrete_pairs():
            for memory_id in self._postings.get(pair, ()):
                overlap[memory_id] += 1
        return sorted(overlap.items(), key=lambda item: (-item[1], item[0]))

    def candidate_ids(self, query_facets: MemoryFacetSet) -> list[str]:
        return [memory_id for memory_id, _ in self.candidates(query_facets)]

    def __len__(self) -> int:
        return len(self._id_set)


@dataclass
class MeetPointWeights:
    """Meet-point score weights (bench-graduated seams; penalties >> bonuses by design)."""

    required_facet: float = 2.0
    file_symbol_test: float = 1.5
    evidence_source: float = 1.0
    time_compatible: float = 1.0
    stale_penalty: float = 4.0
    wrong_scope_penalty: float = 5.0


_DEFAULT_REQUIRED: tuple[str, ...] = ("repo", "project", "namespace", "operation", "object", "symbol")
_CONVERGENCE_FACETS: tuple[str, ...] = ("file", "symbol", "test")


@dataclass
class MeetPointExplanation:
    """Per-candidate trace of how the meet-point score was assembled."""

    memory_id: str
    score: float = 0.0
    matched_required: list[str] = field(default_factory=list)
    convergence: dict[str, list[str]] = field(default_factory=dict)
    evidence_overlap: list[str] = field(default_factory=list)
    time_compatible: bool = False
    penalties: list[str] = field(default_factory=list)
    rank: int = 0

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id, "score": round(self.score, 4), "rank": self.rank,
            "matched_required": self.matched_required, "convergence": self.convergence,
            "evidence_overlap": self.evidence_overlap, "time_compatible": self.time_compatible,
            "penalties": self.penalties,
        }


class MeetPointReranker:
    """Score facet-overlap candidates by convergence; emit explanation traces.

    ``defer_discipline_to_wardens=True`` drops the scope/stale penalties (recall
    integration owns that via the warden chain); the default keeps them for
    standalone use + parity with the graduated bench behavior.
    """

    def __init__(
        self,
        weights: MeetPointWeights | None = None,
        *,
        defer_discipline_to_wardens: bool = False,
    ) -> None:
        self.weights = weights or MeetPointWeights()
        self.defer_discipline_to_wardens = defer_discipline_to_wardens

    def score(self, query: FacetedQuery, memory: FacetedMemory) -> MeetPointExplanation:
        weights = self.weights
        qf, mf = query.facets, memory.facets
        exp = MeetPointExplanation(memory_id=memory.id)
        score = 0.0

        required = query.required_facets or [f for f in _DEFAULT_REQUIRED if qf.values(f)]
        for facet in required:
            if qf.values(facet) & mf.values(facet):
                score += weights.required_facet
                exp.matched_required.append(facet)

        for facet in _CONVERGENCE_FACETS:
            shared = sorted(qf.values(facet) & mf.values(facet))
            if shared:
                exp.convergence[facet] = shared
                score += weights.file_symbol_test * len(shared)

        evidence_shared = sorted(qf.values("evidence_type") & mf.values("evidence_type"))
        exp.evidence_overlap.extend(evidence_shared)
        if qf.source_id and qf.source_id == mf.source_id:
            exp.evidence_overlap.append(f"source_id={mf.source_id}")
        if exp.evidence_overlap:
            score += weights.evidence_source * len(exp.evidence_overlap)

        if _time_compatible(qf, mf):
            exp.time_compatible = True
            score += weights.time_compatible

        if not self.defer_discipline_to_wardens:
            if query.intent == "current" and memory.is_stale:
                score -= weights.stale_penalty
                bucket = mf.belief_bucket if mf.belief_bucket in STALE_BUCKETS else "superseded"
                exp.penalties.append(f"stale:{bucket}")
            for facet in SCOPE_FACETS:
                q_val, m_val = getattr(qf, facet), getattr(mf, facet)
                if q_val and m_val and q_val != m_val:
                    score -= weights.wrong_scope_penalty
                    exp.penalties.append(f"wrong_scope:{facet}({m_val}!={q_val})")

        exp.score = score
        return exp

    def rerank(
        self, query: FacetedQuery, candidate_ids: list[str],
        memories_by_id: dict[str, FacetedMemory],
    ) -> list[MeetPointExplanation]:
        explanations = [
            self.score(query, memories_by_id[cid])
            for cid in candidate_ids if cid in memories_by_id
        ]
        explanations.sort(key=lambda e: (-e.score, e.memory_id))
        for rank, exp in enumerate(explanations, start=1):
            exp.rank = rank
        return explanations

    def ranked_ids(
        self, query: FacetedQuery, candidate_ids: list[str],
        memories_by_id: dict[str, FacetedMemory],
    ) -> list[str]:
        return [exp.memory_id for exp in self.rerank(query, candidate_ids, memories_by_id)]


def _time_compatible(qf: MemoryFacetSet, mf: MemoryFacetSet) -> bool:
    """A candidate's fact must hold at/before the query's as-of time (lexical ISO compare)."""
    if not qf.valid_time or not mf.valid_time:
        return False
    return mf.valid_time <= qf.valid_time
