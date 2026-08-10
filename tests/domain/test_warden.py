"""Tests for the Warden layer (assertion-boundary guards; peer of Oracle/Mutator)."""

from __future__ import annotations

from menhir.domain.belief import (
    BeliefCandidate,
    BeliefCandidateType,
    BeliefEvidence,
    BeliefHead,
    BeliefScore,
    EvidenceSignal,
    QueryIntent,
    RecallBucket,
)
from menhir.domain.exhaustion import RetrievalStats
from menhir.domain.scope import MemoryScope
from menhir.domain.self_reinforcement import SupportProfile
from menhir.domain.oracles import OraclePacket, OraclePolarity, OracleResult, OracleTarget
from menhir.domain.warden import (
    ContradictionWarden,
    CurrentnessWarden,
    EvidenceAnchorWarden,
    ExhaustionWarden,
    ScopeWarden,
    WardenChain,
    WardenContext,
    WardenDecision,
)


def _score(bucket: RecallBucket) -> BeliefScore:
    return BeliefScore(
        candidate=BeliefCandidate(id="c", statement="s", candidate_type=BeliefCandidateType.FIX),
        head=BeliefHead.CURRENT, probability=0.9, support=1.0, contradiction=0.0,
        evidence_count=1, bucket=bucket,
    )


# --- CurrentnessWarden ------------------------------------------------------

def test_currentness_admits_current_belief() -> None:
    ctx = WardenContext("c", belief_score=_score(RecallBucket.SAFE_TO_ASSERT), evidence=())
    assert CurrentnessWarden().evaluate(ctx).decision is WardenDecision.ADMIT


def test_currentness_refuses_superseded_under_current_intent() -> None:
    # would-be-asserted belief + supersession signal -> ANERGIC -> REFUSE
    ctx = WardenContext(
        "c", belief_score=_score(RecallBucket.SAFE_TO_ASSERT),
        evidence=(BeliefEvidence(EvidenceSignal.IS_EXPIRED),), intent=QueryIntent.CURRENT,
    )
    assert CurrentnessWarden().evaluate(ctx).decision is WardenDecision.REFUSE


def test_currentness_flags_superseded_as_historical_under_historical_intent() -> None:
    ctx = WardenContext(
        "c", belief_score=_score(RecallBucket.SAFE_TO_ASSERT),
        evidence=(BeliefEvidence(EvidenceSignal.IS_EXPIRED),), intent=QueryIntent.HISTORICAL,
    )
    v = CurrentnessWarden().evaluate(ctx)
    assert v.decision is WardenDecision.FLAG and v.label == "historical"


def test_currentness_admits_without_score() -> None:
    assert CurrentnessWarden().evaluate(WardenContext("c")).decision is WardenDecision.ADMIT


# --- ExhaustionWarden -------------------------------------------------------

def test_exhaustion_admit_attenuate_refuse() -> None:
    w = ExhaustionWarden()
    assert w.evaluate(WardenContext("c", retrieval_stats=RetrievalStats(retrievals_since_progress=1))).decision is WardenDecision.ADMIT
    att = w.evaluate(WardenContext("c", retrieval_stats=RetrievalStats(retrievals_since_progress=2)))
    assert att.decision is WardenDecision.ATTENUATE and 0.0 < att.factor < 1.0
    assert w.evaluate(WardenContext("c", retrieval_stats=RetrievalStats(retrievals_since_progress=4))).decision is WardenDecision.REFUSE


# --- WardenChain (most restrictive wins) ------------------------------------

def test_chain_takes_most_restrictive() -> None:
    ctx = WardenContext(
        "c",
        belief_score=_score(RecallBucket.SAFE_TO_ASSERT),       # currentness -> ADMIT
        evidence=(),
        retrieval_stats=RetrievalStats(retrievals_since_progress=4),  # exhaustion -> REFUSE
    )
    v = WardenChain([CurrentnessWarden(), ExhaustionWarden()]).evaluate(ctx)
    assert v.decision is WardenDecision.REFUSE
    assert "currentness:admit" in v.reason and "exhaustion:refuse" in v.reason


def test_chain_multiplies_attenuation() -> None:
    ctx = WardenContext(
        "c",
        belief_score=_score(RecallBucket.SAFE_TO_ASSERT),
        retrieval_stats=RetrievalStats(retrievals_since_progress=2),  # ATTENUATE 0.4
    )
    v = WardenChain([CurrentnessWarden(), ExhaustionWarden(attenuation=0.5)]).evaluate(ctx)
    assert v.decision is WardenDecision.ATTENUATE
    assert v.factor == 0.5


def test_empty_chain_admits() -> None:
    assert WardenChain([]).evaluate(WardenContext("c")).decision is WardenDecision.ADMIT


# --- ScopeWarden ------------------------------------------------------------

def test_scope_refuses_wrong_repo() -> None:
    ctx = WardenContext(
        "c", query_scope=MemoryScope(repo="menhir"), candidate_scope=MemoryScope(repo="yawn.dashboard"),
    )
    v = ScopeWarden().evaluate(ctx)
    assert v.decision is WardenDecision.REFUSE and "repo" in v.reason


def test_scope_admits_same_and_unknown() -> None:
    same = WardenContext("c", query_scope=MemoryScope(repo="menhir"), candidate_scope=MemoryScope(repo="menhir"))
    assert ScopeWarden().evaluate(same).decision is WardenDecision.ADMIT
    unknown = WardenContext("c")  # no scope on either side
    assert ScopeWarden().evaluate(unknown).decision is WardenDecision.ADMIT


def test_evidence_anchor_warden_refuses_synthetic_only() -> None:
    """Rule zero: a claim backed only by LLM/retrieval self-support is refused as current truth."""
    ctx = WardenContext("c", support_profile=SupportProfile(support_kinds=("agent_inference", "llm_summary")))
    assert EvidenceAnchorWarden().evaluate(ctx).decision is WardenDecision.REFUSE


def test_evidence_anchor_warden_admits_externally_anchored() -> None:
    ctx = WardenContext("c", support_profile=SupportProfile(support_kinds=("git", "agent_inference"), meta_depth=1))
    assert EvidenceAnchorWarden().evaluate(ctx).decision is WardenDecision.ADMIT


def test_evidence_anchor_warden_flags_deep_meta_chain() -> None:
    ctx = WardenContext("c", support_profile=SupportProfile(support_kinds=("git",), meta_depth=5))
    v = EvidenceAnchorWarden().evaluate(ctx)
    assert v.decision is WardenDecision.FLAG and v.label == "suspicious_chain"


def test_evidence_anchor_warden_admits_without_profile() -> None:
    assert EvidenceAnchorWarden().evaluate(WardenContext("c")).decision is WardenDecision.ADMIT


def test_scope_warden_closes_the_gap_currentness_misses() -> None:
    """The r3_rename_wrong_scope finding: a current (non-superseded) belief in the WRONG repo
    is ADMITted by currentness but REFUSEd by scope. The chain catches it."""
    ctx = WardenContext(
        "c",
        belief_score=_score(RecallBucket.SAFE_TO_ASSERT),   # currentness -> ADMIT (not superseded)
        evidence=(),
        query_scope=MemoryScope(repo="menhir"),
        candidate_scope=MemoryScope(repo="yawn.dashboard"),  # scope -> REFUSE
    )
    assert CurrentnessWarden().evaluate(ctx).decision is WardenDecision.ADMIT
    chain = WardenChain([CurrentnessWarden(), ScopeWarden()]).evaluate(ctx)
    assert chain.decision is WardenDecision.REFUSE


# --- ContradictionWarden (Guard 7) -----------------------------------------------


def _packet_with_conflict() -> OraclePacket:
    """Helper: oracle packet with a CONFLICT/CONTRADICT result."""
    r = OracleResult(
        oracle="conflict_oracle",
        probability=0.9,
        polarity=OraclePolarity.CONTRADICT,
        target=OracleTarget.CONFLICT,
    )
    return OraclePacket(
        candidate_id="m1",
        results=[r],
        role_logits={},
        combined_probability=0.5,
        uncertainty=0.0,
    )


def test_contradiction_refuses_under_current_intent() -> None:
    """Contradiction interrupts CURRENT assertion -> REFUSE."""
    w = ContradictionWarden()
    v = w.evaluate(
        WardenContext(
            candidate_id="m1",
            intent=QueryIntent.CURRENT,
            oracle_packet=_packet_with_conflict(),
        )
    )
    assert v.decision is WardenDecision.REFUSE


def test_contradiction_flags_under_historical_intent() -> None:
    """Contradiction under HISTORICAL intent -> FLAG 'conflict'."""
    w = ContradictionWarden()
    v = w.evaluate(
        WardenContext(
            candidate_id="m1",
            intent=QueryIntent.HISTORICAL,
            oracle_packet=_packet_with_conflict(),
        )
    )
    assert v.decision is WardenDecision.FLAG
    assert v.label == "conflict"


def test_contradiction_admits_when_no_contradiction() -> None:
    """No contradiction -> ADMIT."""
    w = ContradictionWarden()
    v = w.evaluate(WardenContext(candidate_id="m1", intent=QueryIntent.CURRENT))
    assert v.decision is WardenDecision.ADMIT
