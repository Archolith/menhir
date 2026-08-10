"""R3 currentness-policy tests — the intent-aware belief bucketing.

Proves the load-bearing rule: a superseded-but-relevant belief is suppressed from
current truth under CURRENT intent (ANERGIC_CURRENT) but stays first-class under
HISTORICAL intent (HISTORICAL_ONLY) — the CE-willow poisoning fix. The Rung-0
BeliefScorer behavior is unchanged (covered by test_belief.py)."""

from __future__ import annotations

from menhir.domain.belief import (
    BeliefCandidate,
    BeliefCandidateType,
    BeliefEvidence,
    BeliefScore,
    BeliefHead,
    EvidencePolarity,
    EvidenceSignal,
    QueryIntent,
    RecallBucket,
    build_intent_aware_packet,
    currentness_bucket,
    is_superseded_belief,
    is_unsafe_belief,
)


def _score(bucket: RecallBucket, *, prob: float = 0.9, support: float = 1.0, contra: float = 0.0) -> BeliefScore:
    cand = BeliefCandidate(id="c", statement="s", candidate_type=BeliefCandidateType.FIX)
    return BeliefScore(
        candidate=cand, head=BeliefHead.CURRENT, probability=prob,
        support=support, contradiction=contra, evidence_count=1, bucket=bucket,
    )


# --- supersession + unsafe detection ---------------------------------------

def test_is_superseded_belief_detects_expiry_and_contradiction() -> None:
    assert is_superseded_belief([BeliefEvidence(EvidenceSignal.IS_EXPIRED)]) is True
    assert is_superseded_belief(
        [BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS)]
    ) is True
    assert is_superseded_belief([BeliefEvidence(EvidenceSignal.TEST_PASSED)]) is False
    assert is_superseded_belief([]) is False


def test_is_unsafe_belief_only_for_unsupported_noise() -> None:
    # DO_NOT_ASSERT with no supersession -> noise -> unsafe
    assert is_unsafe_belief(_score(RecallBucket.DO_NOT_ASSERT), is_superseded=False) is True
    # DO_NOT_ASSERT but superseded -> history, NOT unsafe
    assert is_unsafe_belief(_score(RecallBucket.DO_NOT_ASSERT), is_superseded=True) is False
    # assertable belief is never unsafe
    assert is_unsafe_belief(_score(RecallBucket.SAFE_TO_ASSERT), is_superseded=False) is False


# --- the currentness policy (core) -----------------------------------------

def test_current_intent_suppresses_superseded_assertion_to_anergic() -> None:
    # would-be-asserted superseded belief -> ANERGIC_CURRENT under CURRENT intent
    assert currentness_bucket(
        base=RecallBucket.SAFE_TO_ASSERT, intent=QueryIntent.CURRENT,
        is_superseded=True, is_unsafe=False,
    ) is RecallBucket.ANERGIC_CURRENT
    assert currentness_bucket(
        base=RecallBucket.MENTION_WITH_UNCERTAINTY, intent=QueryIntent.CURRENT,
        is_superseded=True, is_unsafe=False,
    ) is RecallBucket.ANERGIC_CURRENT


def test_historical_intent_keeps_superseded_as_first_class_history() -> None:
    assert currentness_bucket(
        base=RecallBucket.SAFE_TO_ASSERT, intent=QueryIntent.HISTORICAL,
        is_superseded=True, is_unsafe=False,
    ) is RecallBucket.HISTORICAL_ONLY


def test_current_belief_keeps_baseline_bucket() -> None:
    # not superseded -> unchanged regardless of intent
    for intent in QueryIntent:
        assert currentness_bucket(
            base=RecallBucket.SAFE_TO_ASSERT, intent=intent,
            is_superseded=False, is_unsafe=False,
        ) is RecallBucket.SAFE_TO_ASSERT


def test_unsafe_is_blocked_regardless_of_intent() -> None:
    for intent in QueryIntent:
        assert currentness_bucket(
            base=RecallBucket.DO_NOT_ASSERT, intent=intent,
            is_superseded=False, is_unsafe=True,
        ) is RecallBucket.BLOCKED


def test_conflict_intent_preserves_conflict_set() -> None:
    assert currentness_bucket(
        base=RecallBucket.CONFLICT_SET, intent=QueryIntent.CONFLICT,
        is_superseded=True, is_unsafe=False,
    ) is RecallBucket.CONFLICT_SET
    # a superseded non-conflict belief under conflict intent -> history
    assert currentness_bucket(
        base=RecallBucket.SAFE_TO_ASSERT, intent=QueryIntent.CONFLICT,
        is_superseded=True, is_unsafe=False,
    ) is RecallBucket.HISTORICAL_ONLY


# --- end-to-end packet: the CE-willow story --------------------------------

def test_intent_aware_packet_routes_ce_willow_belief_drift() -> None:
    current_truth = _score(RecallBucket.SAFE_TO_ASSERT, prob=0.9)            # load-order cause (current)
    stale_assertion = _score(RecallBucket.SAFE_TO_ASSERT, prob=0.85)         # "patch fully fixed" (superseded)
    noise = _score(RecallBucket.DO_NOT_ASSERT, prob=0.1, support=0.0)        # unsupported noise

    scored = [
        (current_truth, [BeliefEvidence(EvidenceSignal.LATER_CONFIRMED)]),
        (stale_assertion, [BeliefEvidence(EvidenceSignal.LATER_CONTRADICTED, EvidencePolarity.CONTRADICTS)]),
        (noise, [BeliefEvidence(EvidenceSignal.EMBEDDING_MATCH)]),
    ]

    cur = build_intent_aware_packet(scored, intent=QueryIntent.CURRENT)
    assert current_truth in cur.safe_to_assert          # current truth still assertable
    assert stale_assertion in cur.anergic_current       # superseded suppressed from current truth
    assert noise in cur.blocked                          # noise blocked
    assert stale_assertion not in cur.safe_to_assert     # the poisoning fix

    hist = build_intent_aware_packet(scored, intent=QueryIntent.HISTORICAL)
    assert stale_assertion in hist.historical_only       # former belief preserved for the drift story
    assert stale_assertion not in hist.anergic_current
