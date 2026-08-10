"""FacetCandidateSource — facet-overlap contribution over a recall pool (R2, Phase 2b).

Composes the pure pieces (facet derivation + overlap index + meet-point rerank) behind a
**graph-reader seam**. Given a candidate pool (uuids) + a query, it does **one bounded
prefetch** to derive each candidate's facets, indexes them, generates facet-overlap
candidates from the pool, and meet-point reranks — with the scope/stale **discipline
deferred to the warden chain** (``defer_discipline_to_wardens=True``), because menhir's
``ScopeWarden``/``CurrentnessWarden`` already own that. FACET's net-new value here is
**candidate generation + convergence** (surfacing overlap a similarity ranker misses).

No recall wiring in this module (Phase 3 shadow / Phase 4 active). The real
``FacetGraphReader`` runs a single Cypher over the pool's ``ANCHORED_TO``/``DEFINES`` +
node metadata (Phase 2b-real, infrastructure); here it is a protocol, tested with a stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .facet_derivation import derive_facets
from .facets import (
    FacetedMemory,
    FacetedQuery,
    MeetPointExplanation,
    MeetPointReranker,
    MeetPointWeights,
    MemoryFacetIndex,
)


@dataclass
class FacetInputs:
    """Everything ``derive_facets`` needs for one memory (from the bounded graph prefetch)."""

    content: str = ""
    namespace: str | None = None
    project: str | None = None
    repo: str | None = None
    belief_bucket: str | None = None
    valid_time: str | None = None
    learned_time: str | None = None
    source_id: str | None = None
    anchored_files: tuple[str, ...] = ()
    anchored_symbols: tuple[str, ...] = ()
    superseded: bool = False

    def to_memory(self, uuid: str) -> FacetedMemory:
        return FacetedMemory(
            id=uuid,
            facets=derive_facets(
                content=self.content, namespace=self.namespace, project=self.project,
                repo=self.repo, belief_bucket=self.belief_bucket, valid_time=self.valid_time,
                learned_time=self.learned_time, source_id=self.source_id,
                anchored_files=self.anchored_files, anchored_symbols=self.anchored_symbols,
            ),
            superseded=self.superseded,
        )


@runtime_checkable
class FacetGraphReader(Protocol):
    """One bounded bulk prefetch: candidate uuids -> their facet inputs (one round-trip)."""

    def fetch_facet_inputs(self, uuids: list[str]) -> dict[str, FacetInputs]:
        ...


class FacetCandidateSource:
    """Facet-overlap candidate contribution over a recall candidate pool."""

    def __init__(self, reader: FacetGraphReader, *, weights: MeetPointWeights | None = None) -> None:
        self._reader = reader
        # Discipline (scope/stale) is the warden chain's job; FACET generates + converges.
        self._reranker = MeetPointReranker(weights, defer_discipline_to_wardens=True)

    def contribute(self, query: FacetedQuery, pool_uuids: list[str]) -> list[MeetPointExplanation]:
        """Derive facets for the pool (one prefetch), generate facet-overlap candidates from it,
        and meet-point rerank (convergence only). Best-first; empty if nothing overlaps."""
        inputs = self._reader.fetch_facet_inputs(pool_uuids)
        memories = {uuid: fi.to_memory(uuid) for uuid, fi in inputs.items()}
        index = MemoryFacetIndex().build(list(memories.values()))
        candidate_ids = index.candidate_ids(query.facets)
        return self._reranker.rerank(query, candidate_ids, memories)

    def ranked_uuids(self, query: FacetedQuery, pool_uuids: list[str]) -> list[str]:
        return [exp.memory_id for exp in self.contribute(query, pool_uuids)]
