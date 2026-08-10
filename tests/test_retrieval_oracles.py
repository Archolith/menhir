"""Tests for the cheap retrieval oracles (R6) — consuming the domain producers."""

from __future__ import annotations

import pytest

from menhir.domain.oracles import CandidateMemory, OraclePolarity, OracleTarget, QueryContext
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.retrieval_oracles import (
    EvidenceOracle,
    ScopeOracle,
    SemanticOracle,
    StructureOracle,
    TemporalOracle,
    default_oracles,
)


def _cand(**meta) -> CandidateMemory:
    content = meta.pop("content", "")
    return CandidateMemory(id="m", content=content, metadata=meta)


# --- SemanticOracle ---------------------------------------------------------

def test_semantic_lexical_overlap() -> None:
    r = SemanticOracle().evaluate(QueryContext(text="recall floor cosine"), _cand(content="the recall floor is cosine"))
    assert r.probability > 0 and r.target is OracleTarget.RELEVANCE


def test_semantic_prefers_precomputed_similarity() -> None:
    # A precomputed similarity in metadata (the recall path's real cosine) overrides the
    # lexical stub, so the combiner ranks on embeddings even when text overlap disagrees.
    cand = _cand(content="totally unrelated words", similarity=0.91)
    r = SemanticOracle().evaluate(QueryContext(text="recall floor cosine"), cand)
    assert r.probability == pytest.approx(0.91)
    assert "precomputed" in r.note


def test_semantic_precomputed_similarity_is_clamped() -> None:
    r = SemanticOracle().evaluate(QueryContext(text="x"), _cand(content="y", similarity=1.7))
    assert r.probability == 1.0


# --- StructureOracle --------------------------------------------------------

def test_structure_overlap_on_symbol() -> None:
    q = QueryContext(text="x", symbols=("Foo.bar",))
    r = StructureOracle().evaluate(q, _cand(symbols=("Foo.bar",)))
    assert r.probability == 1.0 and r.polarity is OraclePolarity.SUPPORT


# --- ScopeOracle (consumes domain/scope) ------------------------------------

def test_scope_oracle_contradicts_wrong_repo() -> None:
    q = QueryContext(text="x", repo="menhir")
    r = ScopeOracle().evaluate(q, _cand(repo="yawn.dashboard"))
    assert r.polarity is OraclePolarity.CONTRADICT and r.target is OracleTarget.SCOPE


def test_scope_oracle_supports_match_and_missing_when_unknown() -> None:
    q = QueryContext(text="x", repo="menhir")
    assert ScopeOracle().evaluate(q, _cand(repo="menhir")).polarity is OraclePolarity.SUPPORT
    assert ScopeOracle().evaluate(q, _cand()).polarity is OraclePolarity.MISSING


# --- TemporalOracle (consumes domain/temporal) ------------------------------

def test_temporal_superseded_contradicts_under_current_intent() -> None:
    q = QueryContext(text="x", intent="current")
    r = TemporalOracle().evaluate(q, _cand(created_at="2026-01-01", expired_at="2026-02-01"))
    assert r.polarity is OraclePolarity.CONTRADICT and r.target is OracleTarget.CURRENTNESS


def test_temporal_superseded_supports_under_historical_intent() -> None:
    q = QueryContext(text="x", intent="historical")
    r = TemporalOracle().evaluate(q, _cand(created_at="2026-01-01", expired_at="2026-02-01"))
    assert r.polarity is OraclePolarity.SUPPORT and r.target is OracleTarget.HISTORICALITY


def test_temporal_superseded_flag_without_expired_at() -> None:
    q = QueryContext(text="x", intent="current")
    r = TemporalOracle().evaluate(q, _cand(created_at="2026-01-01", superseded=True))
    assert r.polarity is OraclePolarity.CONTRADICT


def test_temporal_anachronism_when_learned_after_as_of() -> None:
    q = QueryContext(text="x", as_of_time="2026-02-01")
    r = TemporalOracle().evaluate(q, _cand(valid_at="2026-01-01", created_at="2026-05-01"))
    assert r.polarity is OraclePolarity.CONTRADICT and "anachronism" in r.note


def test_temporal_unknown_is_missing_not_fabricated() -> None:
    r = TemporalOracle().evaluate(QueryContext(text="x"), _cand())
    assert r.polarity is OraclePolarity.MISSING


# --- EvidenceOracle (consumes self_reinforcement vocab) ---------------------

def test_evidence_strong_vs_self_vs_missing() -> None:
    q = QueryContext(text="x")
    assert EvidenceOracle().evaluate(q, _cand(evidence_kinds=("git",))).probability == 1.0
    weak = EvidenceOracle().evaluate(q, _cand(evidence_kinds=("agent_inference",)))
    assert weak.directness < 0.5
    assert EvidenceOracle().evaluate(q, _cand()).polarity is OraclePolarity.MISSING


# --- integration with the R4 executor ---------------------------------------

@pytest.mark.asyncio
async def test_default_oracles_run_through_executor_deterministically() -> None:
    ex = OracleExecutor(default_oracles())
    q = QueryContext(text="recall floor", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="cur", content="recall floor is a rank cut", metadata={"repo": "menhir", "created_at": "2026-01-01"}),
        CandidateMemory(id="old", content="recall floor cosine", metadata={"repo": "menhir", "created_at": "2026-01-01", "expired_at": "2026-02-01"}),
    ]
    out = await ex.evaluate(q, cands)
    assert list(out) == ["cur", "old"]
    # every candidate gets one result per oracle, in stable priority order
    # (intent graduated into the default set after the real-embedder bench gate)
    assert [r.oracle for r in out["cur"]] == ["semantic", "structure", "scope", "temporal", "intent"]
    # the superseded candidate's temporal oracle contradicts currentness
    temporal_old = next(r for r in out["old"] if r.oracle == "temporal")
    assert temporal_old.polarity is OraclePolarity.CONTRADICT


def test_default_oracles_leave_evidence_to_warden_admission() -> None:
    assert "evidence" not in {oracle.name for oracle in default_oracles()}
