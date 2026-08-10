"""Tests for the Oracle -> Warden handoff (assertion pipeline, end to end)."""

from __future__ import annotations

import pytest

from menhir.domain.belief import QueryIntent
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
from menhir.domain.oracles import (
    CandidateMemory,
    OraclePacket,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
)
from menhir.domain.warden import CurrentnessWarden, OracleAdmissionWarden, WardenContext, WardenDecision
from menhir.services.assertion_pipeline import AssertionPipeline
from menhir.services.retrieval_oracles import default_oracles


# --- OracleAdmissionWarden (the bridge) ------------------------------------

def _packet(cid: str, *results: OracleResult, blocked: float = 0.0) -> OraclePacket:
    return OraclePacket(candidate_id=cid, results=list(results), role_logits={"blocked": blocked},
                        combined_probability=0.5, uncertainty=0.0)


def _res(target, polarity) -> OracleResult:
    return OracleResult(oracle="o", probability=1.0, polarity=polarity, target=target)


def test_admission_refuses_superseded_under_current_intent() -> None:
    pkt = _packet("c", _res(OracleTarget.CURRENTNESS, OraclePolarity.CONTRADICT))
    ctx = WardenContext("c", intent=QueryIntent.CURRENT, oracle_packet=pkt)
    assert OracleAdmissionWarden().evaluate(ctx).decision is WardenDecision.REFUSE


def test_admission_admits_superseded_under_historical_intent() -> None:
    pkt = _packet("c", _res(OracleTarget.CURRENTNESS, OraclePolarity.CONTRADICT))
    ctx = WardenContext("c", intent=QueryIntent.HISTORICAL, oracle_packet=pkt)
    assert OracleAdmissionWarden().evaluate(ctx).decision is WardenDecision.ADMIT


def test_admission_flags_historical_under_current_intent() -> None:
    pkt = _packet("c", _res(OracleTarget.HISTORICALITY, OraclePolarity.SUPPORT))
    v = OracleAdmissionWarden().evaluate(WardenContext("c", intent=QueryIntent.CURRENT, oracle_packet=pkt))
    assert v.decision is WardenDecision.FLAG and v.label == "historical"


def test_admission_refuses_on_blocked_logit() -> None:
    pkt = _packet("c", blocked=1.2)
    assert OracleAdmissionWarden().evaluate(WardenContext("c", oracle_packet=pkt)).decision is WardenDecision.REFUSE


def test_admission_admits_clean_packet() -> None:
    pkt = _packet("c", _res(OracleTarget.RELEVANCE, OraclePolarity.SUPPORT))
    assert OracleAdmissionWarden().evaluate(WardenContext("c", oracle_packet=pkt)).decision is WardenDecision.ADMIT


def test_admission_admits_without_packet() -> None:
    assert OracleAdmissionWarden().evaluate(WardenContext("c")).decision is WardenDecision.ADMIT


# --- end-to-end pipeline ----------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_admits_current_refuses_superseded() -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    q = QueryContext(text="recall floor behavior", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="current", content="recall floor is a rank cut",
                        metadata={"repo": "menhir", "valid_at": "2026-06-01", "created_at": "2026-06-01",
                                  "evidence_kinds": ("git",)}),
        CandidateMemory(id="stale", content="recall floor is cosine",
                        metadata={"repo": "menhir", "created_at": "2026-01-01", "expired_at": "2026-02-01",
                                  "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    admitted_ids = [r.candidate_id for r in out.admitted]
    refused_ids = [r.candidate_id for r in out.refused]
    assert "current" in admitted_ids        # current truth admitted
    assert "stale" in refused_ids           # superseded refused from current-truth context


@pytest.mark.asyncio
async def test_pipeline_refuses_wrong_scope() -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    q = QueryContext(text="hybrid alpha", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="in_scope", content="hybrid alpha blends vector and bm25",
                        metadata={"repo": "menhir", "evidence_kinds": ("git",)}),
        CandidateMemory(id="other_repo", content="hybrid alpha slider in the dashboard",
                        metadata={"repo": "yawn.dashboard", "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    assert "other_repo" in [r.candidate_id for r in out.refused]   # wrong-scope refused
    assert "in_scope" in [r.candidate_id for r in out.admitted]


@pytest.mark.asyncio
async def test_pipeline_historical_intent_admits_former_belief() -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    q = QueryContext(text="what did we believe about the floor", intent="historical", repo="menhir")
    cands = [
        CandidateMemory(id="former", content="recall floor is cosine",
                        metadata={"repo": "menhir", "created_at": "2026-01-01", "expired_at": "2026-02-01",
                                  "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    # under historical intent the superseded belief is not refused
    assert "former" not in [r.candidate_id for r in out.refused]


@pytest.mark.asyncio
async def test_pipeline_auto_intent_flows_end_to_end_through_default_config() -> None:
    # The production default path: default executor (incl IntentOracle) + auto_intent.
    # An avoid_repeat cue on a DEFAULT-current query must auto-derive the historical lens so
    # the superseded "former attempt" is admissible, WITHOUT the caller pinning intent.
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    q = QueryContext(text="have we already tried the floor", repo="menhir")  # intent defaulted
    cands = [
        CandidateMemory(id="former", content="recall floor is cosine",
                        metadata={"repo": "menhir", "artifact_type": "experiment",
                                  "created_at": "2026-01-01", "expired_at": "2026-02-01",
                                  "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    assert "former" not in [r.candidate_id for r in out.refused]  # auto-derived lens flowed through


def test_pipeline_auto_derives_lens_from_task_intent() -> None:
    # "have we already tried" (avoid_repeat) on a default-"current" query -> historical lens;
    # an explicit lens is left untouched; a no-cue query stays current.
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    assert pipe._resolve_intent(QueryContext(text="have we already tried the floor")).intent == "historical"
    assert pipe._resolve_intent(QueryContext(text="is the floor still accurate")).intent == "any"
    assert pipe._resolve_intent(QueryContext(text="recall floor behavior")).intent == "current"
    assert pipe._resolve_intent(QueryContext(text="x", intent="historical")).intent == "historical"


def test_pipeline_auto_intent_can_be_disabled() -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner(), auto_intent=False)
    assert pipe._resolve_intent(QueryContext(text="have we already tried the floor")).intent == "current"


@pytest.mark.asyncio
async def test_pipeline_buckets_cover_all_candidates_in_rank_order() -> None:
    pipe = AssertionPipeline(LogSpaceOracleCombiner())
    q = QueryContext(text="x", intent="current", repo="menhir")
    cands = [CandidateMemory(id=f"m{i}", content="x", metadata={"repo": "menhir", "evidence_kinds": ("git",)}) for i in range(3)]
    out = await pipe.run(q, cands)
    ranked = out.ranked
    assert len(ranked) == 3
    assert [r.rank for r in ranked] == [0, 1, 2]   # contiguous, sorted


@pytest.mark.asyncio
async def test_contradiction_warden_refuses_contradicted_under_current_with_flag_on() -> None:
    """With contradiction_interrupt=True, a contradicted candidate lands in refused under current intent."""
    # Import OracleTarget and OraclePolarity here
    from menhir.domain.oracles import OracleTarget, OraclePolarity
    from menhir.services.oracle_executor import OracleExecutor

    # Use a mock executor that returns a conflicted oracle result
    class MockExecutor(OracleExecutor):
        async def evaluate(self, query, candidates):
            # Return one result per candidate with a CONFLICT/CONTRADICT
            return {
                "contradicted": [
                    _res(OracleTarget.CONFLICT, OraclePolarity.CONTRADICT)
                ]
            }

    pipe = AssertionPipeline(
        LogSpaceOracleCombiner(),
        executor=MockExecutor(default_oracles()),
        contradiction_interrupt=True
    )
    q = QueryContext(text="recall floor", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="contradicted", content="recall floor detail",
                        metadata={"repo": "menhir", "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    refused_ids = [r.candidate_id for r in out.refused]
    assert "contradicted" in refused_ids  # contradiction interrupted


@pytest.mark.asyncio
async def test_contradiction_warden_default_off_does_not_refuse() -> None:
    """With contradiction_interrupt=False (default), contradicted candidate is flagged, not refused."""
    from menhir.domain.oracles import OracleTarget, OraclePolarity
    from menhir.services.oracle_executor import OracleExecutor

    class MockExecutor(OracleExecutor):
        async def evaluate(self, query, candidates):
            # Return one result per candidate with a CONFLICT/CONTRADICT
            return {
                "contradicted": [
                    _res(OracleTarget.CONFLICT, OraclePolarity.CONTRADICT)
                ]
            }

    pipe = AssertionPipeline(
        LogSpaceOracleCombiner(),
        executor=MockExecutor(default_oracles()),
        contradiction_interrupt=False  # default-off
    )
    q = QueryContext(text="recall floor", intent="current", repo="menhir")
    cands = [
        CandidateMemory(id="contradicted", content="recall floor detail",
                        metadata={"repo": "menhir", "evidence_kinds": ("git",)}),
    ]
    out = await pipe.run(q, cands)
    # With default-off, ContradictionWarden is not in the chain
    # OracleAdmissionWarden will FLAG it (conflict), but not REFUSE it
    refused_ids = [r.candidate_id for r in out.refused]
    flagged_ids = [r.candidate_id for r in out.flagged]
    # Should be flagged, not refused
    assert "contradicted" in flagged_ids
    assert "contradicted" not in refused_ids


# --- belief_gate tests --------------------------------------------------------


def test_belief_gate_off_excludes_currentness_warden() -> None:
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=False)
    assert not any(isinstance(w, CurrentnessWarden) for w in p.wardens)


def test_belief_gate_on_includes_currentness_warden() -> None:
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=True)
    assert any(isinstance(w, CurrentnessWarden) for w in p.wardens)


@pytest.mark.asyncio
async def test_belief_gate_on_refuses_superseded_under_current_intent() -> None:
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=True, auto_intent=False)
    q = QueryContext(text="what is true now", intent="current")
    cands = [CandidateMemory(id="u1", content="the patch fixed it",
                             metadata={"similarity": 0.9, "belief_superseded": True})]
    outcome = await p.run(q, cands)
    # a superseded belief must not land in admitted-as-current
    assert all(r.candidate_id != "u1" for r in outcome.admitted)
