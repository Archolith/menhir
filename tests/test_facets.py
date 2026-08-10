"""Tests for the ported R2 facet mechanism (menhir.domain.facets, Phase 1)."""

from __future__ import annotations

import pytest

from menhir.domain.facets import (
    FacetedMemory,
    FacetedQuery,
    MeetPointReranker,
    MemoryFacetIndex,
    MemoryFacetSet,
)


@pytest.mark.unit
def test_index_candidates_by_structural_overlap() -> None:
    m1 = FacetedMemory("m1", MemoryFacetSet(file={"recall_service.py"}, symbol={"recall"}))
    m2 = FacetedMemory("m2", MemoryFacetSet(file={"scoring_service.py"}))
    idx = MemoryFacetIndex().build([m1, m2])
    assert idx.candidate_ids(MemoryFacetSet(file={"recall_service.py"})) == ["m1"]


@pytest.mark.unit
def test_index_candidates_regular_memory_interpretive_scope_overlap() -> None:
    # The "regular memory" fashion: overlap on operation/object/scope, no structural facets.
    m1 = FacetedMemory("m1", MemoryFacetSet(operation={"fix"}, object={"floor"}, project="menhir"))
    m2 = FacetedMemory("m2", MemoryFacetSet(operation={"add"}, project="yawn"))
    idx = MemoryFacetIndex().build([m1, m2])
    cands = idx.candidates(MemoryFacetSet(operation={"fix"}, project="menhir"))
    assert cands[0] == ("m1", 2)          # operation + project overlap
    assert "m2" not in dict(cands)        # no shared pair


@pytest.mark.unit
def test_reranker_convergence_and_required_overlap() -> None:
    mem = FacetedMemory("m1", MemoryFacetSet(file={"a.py"}, symbol={"foo"}, project="menhir"))
    q = FacetedQuery(MemoryFacetSet(file={"a.py"}, symbol={"foo"}, project="menhir"))
    exp = MeetPointReranker().score(q, mem)
    assert "file" in exp.convergence and "symbol" in exp.convergence
    assert "project" in exp.matched_required
    assert exp.score > 0


@pytest.mark.unit
def test_reranker_wrong_scope_penalty_ranks_correct_scope_first() -> None:
    good = FacetedMemory("good", MemoryFacetSet(symbol={"Foo"}, project="menhir"))
    wrong = FacetedMemory("wrong", MemoryFacetSet(symbol={"Foo"}, project="yawn"))
    q = FacetedQuery(MemoryFacetSet(symbol={"Foo"}, project="menhir"))
    rr = MeetPointReranker()
    assert rr.ranked_ids(q, ["good", "wrong"], {"good": good, "wrong": wrong}) == ["good", "wrong"]
    assert any(p.startswith("wrong_scope") for p in rr.score(q, wrong).penalties)


@pytest.mark.unit
def test_reranker_stale_penalty_under_current_intent() -> None:
    stale = FacetedMemory("s", MemoryFacetSet(symbol={"Foo"}, belief_bucket="historical"))
    q = FacetedQuery(MemoryFacetSet(symbol={"Foo"}), intent="current")
    assert any(p.startswith("stale") for p in MeetPointReranker().score(q, stale).penalties)


@pytest.mark.unit
def test_defer_discipline_to_wardens_drops_scope_and_stale_penalties() -> None:
    wrong = FacetedMemory(
        "w", MemoryFacetSet(symbol={"Foo"}, project="yawn", belief_bucket="historical")
    )
    q = FacetedQuery(MemoryFacetSet(symbol={"Foo"}, project="menhir"), intent="current")
    exp = MeetPointReranker(defer_discipline_to_wardens=True).score(q, wrong)
    assert exp.penalties == []   # scope/stale discipline left to the warden chain
