"""Tests for FacetCandidateSource (menhir.domain.facet_candidate_source, Phase 2b)."""

from __future__ import annotations

import pytest

from menhir.domain.facet_candidate_source import (
    FacetCandidateSource,
    FacetInputs,
)
from menhir.domain.facets import FacetedQuery, MemoryFacetSet


class _StubReader:
    """In-memory FacetGraphReader: one lookup, no graph."""

    def __init__(self, data: dict[str, FacetInputs]) -> None:
        self._data = data
        self.calls: list[list[str]] = []

    def fetch_facet_inputs(self, uuids: list[str]) -> dict[str, FacetInputs]:
        self.calls.append(list(uuids))
        return {u: self._data[u] for u in uuids if u in self._data}


def _pool() -> dict[str, FacetInputs]:
    return {
        "good": FacetInputs(
            content="fixed source_aware_floor", project="menhir",
            anchored_files=("recall_service.py",), anchored_symbols=("source_aware_floor",),
        ),
        "wrong": FacetInputs(
            content="fixed source_aware_floor", project="yawn",
            anchored_files=("recall_service.py",), anchored_symbols=("source_aware_floor",),
        ),
        "scope_only": FacetInputs(content="user likes dark mode", project="menhir"),
    }


def _query() -> FacetedQuery:
    return FacetedQuery(
        MemoryFacetSet(symbol={"source_aware_floor"}, file={"recall_service.py"}, project="menhir")
    )


@pytest.mark.unit
def test_generates_candidates_and_ranks_structural_convergence_first() -> None:
    src = FacetCandidateSource(_StubReader(_pool()))
    ranked = src.ranked_uuids(_query(), ["good", "wrong", "scope_only"])
    # all three share >=1 facet with the query (good/wrong via file+symbol, scope_only via project)
    assert set(ranked) == {"good", "wrong", "scope_only"}
    # structural convergence (good/wrong) outranks scope-only overlap
    assert ranked.index("good") < ranked.index("scope_only")
    assert ranked.index("wrong") < ranked.index("scope_only")


@pytest.mark.unit
def test_defers_scope_discipline_to_wardens() -> None:
    # with discipline deferred, the wrong-project candidate is NOT penalized here; the
    # ScopeWarden drops it downstream. FACET only generates + converges.
    exps = FacetCandidateSource(_StubReader(_pool())).contribute(_query(), ["good", "wrong"])
    assert all(exp.penalties == [] for exp in exps)


@pytest.mark.unit
def test_no_facet_overlap_yields_no_candidates() -> None:
    src = FacetCandidateSource(_StubReader(_pool()))
    q = FacetedQuery(MemoryFacetSet(symbol={"nonexistent_symbol"}))
    assert src.ranked_uuids(q, ["good", "wrong", "scope_only"]) == []


@pytest.mark.unit
def test_single_bounded_prefetch_over_the_pool() -> None:
    reader = _StubReader(_pool())
    FacetCandidateSource(reader).contribute(_query(), ["good", "wrong", "scope_only"])
    # exactly one graph round-trip over the whole pool
    assert reader.calls == [["good", "wrong", "scope_only"]]


@pytest.mark.unit
def test_regular_memory_overlap_without_any_anchors() -> None:
    # a regular (unanchored) memory contributes via interpretive/scope overlap alone
    data = {
        "reg": FacetInputs(content="we deprecated the old preset", project="menhir"),
    }
    src = FacetCandidateSource(_StubReader(data))
    q = FacetedQuery(MemoryFacetSet(operation={"deprecate"}, project="menhir"))
    assert src.ranked_uuids(q, ["reg"]) == ["reg"]
