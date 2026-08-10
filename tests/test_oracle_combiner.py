"""Tests for the one-pass oracle combiners (R7)."""

from __future__ import annotations

import pytest

from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
)
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner, WeightedOracleCombiner
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.retrieval_oracles import default_oracles


def _r(oracle, target, polarity, *, prob=1.0, fam="semantic") -> OracleResult:
    return OracleResult(oracle=oracle, probability=prob, polarity=polarity, target=target, source_family=fam)


def _rel(oracle="semantic", fam="semantic", prob=1.0) -> OracleResult:
    return _r(oracle, OracleTarget.RELEVANCE, OraclePolarity.SUPPORT, prob=prob, fam=fam)


# --- WeightedOracleCombiner (E) --------------------------------------------

def test_weighted_ranks_more_relevant_first() -> None:
    grouped = {
        "hi": [_rel(prob=1.0)],
        "lo": [_rel(prob=0.2)],
    }
    ranked, packets = WeightedOracleCombiner().rank(QueryContext(text="t"), grouped)
    assert ranked == ["hi", "lo"]
    assert packets["hi"].combined_probability > packets["lo"].combined_probability


# --- LogSpaceOracleCombiner (F) --------------------------------------------

def test_logspace_suppresses_superseded_under_current_intent() -> None:
    """A relevant but currentness-contradicted candidate ranks below a clean relevant one."""
    grouped = {
        "current": [_rel(fam="semantic"), _r("temporal", OracleTarget.CURRENTNESS, OraclePolarity.SUPPORT, fam="temporal")],
        "stale": [_rel(fam="semantic"), _r("temporal", OracleTarget.CURRENTNESS, OraclePolarity.CONTRADICT, fam="temporal")],
    }
    ranked, _ = LogSpaceOracleCombiner().rank(QueryContext(text="t", intent="current"), grouped)
    assert ranked == ["current", "stale"]


def test_logspace_surfaces_historical_under_historical_intent() -> None:
    """Under historical intent the superseded candidate (historicality support) ranks up."""
    grouped = {
        "current_only": [_rel(fam="semantic")],
        "historical": [_rel(fam="semantic"), _r("temporal", OracleTarget.HISTORICALITY, OraclePolarity.SUPPORT, fam="temporal")],
    }
    ranked, _ = LogSpaceOracleCombiner().rank(QueryContext(text="t", intent="historical"), grouped)
    assert ranked[0] == "historical"


def test_logspace_blocks_wrong_scope() -> None:
    grouped = {
        "in_scope": [_rel(fam="semantic")],
        "wrong_scope": [_rel(fam="semantic"), _r("scope", OracleTarget.SCOPE, OraclePolarity.CONTRADICT, fam="scope")],
    }
    ranked, packets = LogSpaceOracleCombiner().rank(QueryContext(text="t", intent="current"), grouped)
    assert ranked == ["in_scope", "wrong_scope"]
    assert packets["wrong_scope"].role_logits["blocked"] > 0


def test_logspace_independence_downweights_duplicate_family() -> None:
    """Two same-family supports count for less than two independent-family supports."""
    same = {"c": [_rel(oracle="a", fam="semantic"), _rel(oracle="b", fam="semantic")]}
    indep = {"c": [_rel(oracle="a", fam="semantic"), _rel(oracle="b", fam="structure")]}
    _, p_same = LogSpaceOracleCombiner().rank(QueryContext(text="t"), same)
    _, p_indep = LogSpaceOracleCombiner().rank(QueryContext(text="t"), indep)
    assert p_indep["c"].role_logits["relevant"] > p_same["c"].role_logits["relevant"]


def test_logspace_missing_raises_uncertainty() -> None:
    grouped = {"c": [_rel(), _r("evidence", OracleTarget.RELEVANCE, OraclePolarity.MISSING, fam="evidence")]}
    _, packets = LogSpaceOracleCombiner().rank(QueryContext(text="t"), grouped)
    assert packets["c"].uncertainty > 0


def test_logspace_deterministic() -> None:
    grouped = {"b": [_rel(prob=0.5)], "a": [_rel(prob=0.5)]}
    r1, _ = LogSpaceOracleCombiner().rank(QueryContext(text="t"), grouped)
    r2, _ = LogSpaceOracleCombiner().rank(QueryContext(text="t"), grouped)
    assert r1 == r2 == ["b", "a"]   # equal score -> incoming rank, stable


# --- end-to-end: executor (R4) -> oracles (R6) -> combiner (R7) -------------

@pytest.mark.asyncio
async def test_full_pipeline_ranks_current_over_superseded() -> None:
    ex = OracleExecutor(default_oracles())
    q = QueryContext(text="recall floor behavior", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="current", content="recall floor is a rank cut",
                        metadata={"repo": "menhir", "valid_at": "2026-06-01", "created_at": "2026-06-01"}),
        CandidateMemory(id="stale", content="recall floor is cosine",
                        metadata={"repo": "menhir", "created_at": "2026-01-01", "expired_at": "2026-02-01"}),
    ]
    grouped = await ex.evaluate(q, cands)
    ranked, packets = LogSpaceOracleCombiner().rank(q, grouped)
    assert ranked[0] == "current"
    # the stale candidate accrued historical mass from the currentness contradiction
    assert packets["stale"].role_logits["historical"] > 0
