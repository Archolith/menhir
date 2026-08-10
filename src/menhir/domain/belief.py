"""Belief-state domain types for uncertain and superseded memories.

This module is intentionally small: it gives Chronostratum a transparent baseline for
scoring competing beliefs before introducing a real probabilistic-circuit backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import exp, log


class BeliefCandidateType(str, Enum):
    """Kinds of explanations or states menhir may reason about."""

    CAUSE = "cause"
    FIX = "fix"
    REGRESSION = "regression"
    SUPERSESSION = "supersession"
    DEPENDENCY_STATE = "dependency_state"


class BeliefHead(str, Enum):
    """Question being asked of the belief layer.

    Splitting heads keeps menhir from collapsing relevance, current truth,
    explanation support, and supersession into one vague confidence score.
    """

    RELEVANT = "relevant"
    CURRENT = "current"
    SUPPORTED = "supported"
    SUPERSEDED = "superseded"


class RecallBucket(str, Enum):
    """LLM-facing policy buckets for belief-aware recall.

    The first four are the Rung-0 baseline set (kept stable). The last three are the
    R3 currentness-policy refinements (belief-layer.md "Immediate target #1"): they
    split the too-coarse ``DO_NOT_ASSERT`` into *historically useful* vs *suppressed
    for current truth* vs *unsafe*. They are only ever produced by the intent-aware
    policy (``currentness_bucket`` / ``build_intent_aware_packet``); the Rung-0
    ``classify`` path never emits them, so existing behavior is unchanged.
    """

    SAFE_TO_ASSERT = "safe_to_assert"
    MENTION_WITH_UNCERTAINTY = "mention_with_uncertainty"
    CONFLICT_SET = "conflict_set"
    DO_NOT_ASSERT = "do_not_assert"
    # R3 currentness-policy buckets (additive):
    HISTORICAL_ONLY = "historical_only"      # former belief; timeline/drift value, not current truth
    ANERGIC_CURRENT = "anergic_current"      # relevant but suppressed from current-truth assertion
    BLOCKED = "blocked"                      # unsafe/unsupported/poisoned — keep out of answer context


class QueryIntent(str, Enum):
    """What the caller is asking of recall — drives the currentness policy.

    CURRENT  -> only safe current truth may be asserted; superseded beliefs are
                suppressed (ANERGIC_CURRENT) or shown as former belief (HISTORICAL_ONLY).
    HISTORICAL -> superseded beliefs are first-class (HISTORICAL_ONLY), not suppressed —
                the caller wants the belief-drift story.
    CONFLICT -> unresolved disagreement (CONFLICT_SET) is the point and is surfaced.
    """

    CURRENT = "current"
    HISTORICAL = "historical"
    CONFLICT = "conflict"


class EvidenceSignal(str, Enum):
    """Inspectable evidence features feeding a belief score."""

    EMBEDDING_MATCH = "embedding_match"
    ENTITY_MATCH = "entity_match"
    GRAPH_PATH = "graph_path"
    IS_VALID_AT_QUERY_TIME = "is_valid_at_query_time"
    IS_EXPIRED = "is_expired"
    MENTIONED_BY_USER = "mentioned_by_user"
    MENTIONED_BY_AGENT = "mentioned_by_agent"
    OBSERVED_ERROR = "observed_error"
    TEST_FAILED = "test_failed"
    TEST_PASSED = "test_passed"
    FILE_CHANGED = "file_changed"
    SYMBOL_CHANGED = "symbol_changed"
    DEPENDENCY_CHANGED = "dependency_changed"
    BEFORE_FAILURE = "before_failure"
    AFTER_FAILURE = "after_failure"
    LATER_CONTRADICTED = "later_contradicted"
    LATER_CONFIRMED = "later_confirmed"
    SOURCE_IS_USER = "source_is_user"
    SOURCE_IS_LOG = "source_is_log"
    SOURCE_IS_GIT = "source_is_git"
    SOURCE_IS_GRAPHITI = "source_is_graphiti"


class EvidencePolarity(str, Enum):
    """Whether a piece of evidence supports or contradicts a candidate belief."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


DEFAULT_SIGNAL_WEIGHTS: dict[EvidenceSignal, float] = {
    EvidenceSignal.EMBEDDING_MATCH: 0.45,
    EvidenceSignal.ENTITY_MATCH: 0.50,
    EvidenceSignal.GRAPH_PATH: 0.45,
    EvidenceSignal.IS_VALID_AT_QUERY_TIME: 0.90,
    EvidenceSignal.IS_EXPIRED: 1.20,
    EvidenceSignal.MENTIONED_BY_AGENT: 0.35,
    EvidenceSignal.MENTIONED_BY_USER: 0.70,
    EvidenceSignal.OBSERVED_ERROR: 0.85,
    EvidenceSignal.TEST_FAILED: 0.80,
    EvidenceSignal.TEST_PASSED: 0.80,
    EvidenceSignal.FILE_CHANGED: 0.55,
    EvidenceSignal.SYMBOL_CHANGED: 0.70,
    EvidenceSignal.DEPENDENCY_CHANGED: 0.75,
    EvidenceSignal.BEFORE_FAILURE: 0.40,
    EvidenceSignal.AFTER_FAILURE: 0.40,
    EvidenceSignal.LATER_CONTRADICTED: 1.10,
    EvidenceSignal.LATER_CONFIRMED: 1.10,
    EvidenceSignal.SOURCE_IS_USER: 0.45,
    EvidenceSignal.SOURCE_IS_LOG: 0.60,
    EvidenceSignal.SOURCE_IS_GIT: 0.65,
    EvidenceSignal.SOURCE_IS_GRAPHITI: 0.50,
}


@dataclass(frozen=True)
class BeliefCandidate:
    """A possible explanation, fix, regression, or state to score."""

    id: str
    statement: str
    candidate_type: BeliefCandidateType
    touched_entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class BeliefEvidence:
    """A single evidence item used to score a candidate belief."""

    signal: EvidenceSignal
    polarity: EvidencePolarity = EvidencePolarity.SUPPORTS
    strength: float = 1.0
    note: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if self.strength < 0:
            raise ValueError("BeliefEvidence.strength must be >= 0")


@dataclass(frozen=True)
class BeliefScore:
    """Transparent score for a candidate belief under current evidence."""

    candidate: BeliefCandidate
    head: BeliefHead
    probability: float
    support: float
    contradiction: float
    evidence_count: int
    bucket: RecallBucket
    rationale: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BeliefRecallPacket:
    """LLM-facing recall packet grouped by assertion policy.

    The first four groups are the Rung-0 baseline. The last three are populated only by
    the intent-aware builder (``build_intent_aware_packet``); the Rung-0
    ``build_recall_packet`` leaves them empty, so existing callers are unaffected.
    """

    safe_to_assert: tuple[BeliefScore, ...] = ()
    mention_with_uncertainty: tuple[BeliefScore, ...] = ()
    conflict_set: tuple[BeliefScore, ...] = ()
    do_not_assert: tuple[BeliefScore, ...] = ()
    # R3 currentness-policy groups (additive):
    historical_only: tuple[BeliefScore, ...] = ()
    anergic_current: tuple[BeliefScore, ...] = ()
    blocked: tuple[BeliefScore, ...] = ()


class BeliefScorer:
    """Baseline belief scorer using weighted evidence and a log-odds update.

    This is not intended to be the final probabilistic-circuit implementation. It is a
    swappable, deterministic baseline that gives us stable domain objects and tests.
    """

    def __init__(self, signal_weights: dict[EvidenceSignal, float] | None = None) -> None:
        self._signal_weights = signal_weights or DEFAULT_SIGNAL_WEIGHTS

    def score(
        self,
        candidate: BeliefCandidate,
        evidence: list[BeliefEvidence] | tuple[BeliefEvidence, ...],
        *,
        head: BeliefHead = BeliefHead.SUPPORTED,
        prior: float = 0.5,
    ) -> BeliefScore:
        """Score one candidate belief under the supplied evidence."""

        if not 0 < prior < 1:
            raise ValueError("prior must be between 0 and 1")

        log_odds = log(prior / (1 - prior))
        support = 0.0
        contradiction = 0.0
        rationale: list[str] = []

        for item in evidence:
            weight = self._signal_weights.get(item.signal, 0.0) * item.strength
            if item.polarity == EvidencePolarity.SUPPORTS:
                support += weight
                log_odds += weight
                direction = "+"
            else:
                contradiction += weight
                log_odds -= weight
                direction = "-"

            if item.note:
                rationale.append(f"{direction}{weight:.2f} {item.signal.value}: {item.note}")
            else:
                rationale.append(f"{direction}{weight:.2f} {item.signal.value}")

        probability = 1 / (1 + exp(-log_odds))
        bucket = self.classify(
            head=head,
            probability=probability,
            support=support,
            contradiction=contradiction,
        )
        return BeliefScore(
            candidate=candidate,
            head=head,
            probability=probability,
            support=support,
            contradiction=contradiction,
            evidence_count=len(evidence),
            bucket=bucket,
            rationale=tuple(rationale),
        )

    def rank(
        self,
        candidates: list[BeliefCandidate] | tuple[BeliefCandidate, ...],
        evidence_by_candidate: dict[str, list[BeliefEvidence] | tuple[BeliefEvidence, ...]],
        *,
        head: BeliefHead = BeliefHead.SUPPORTED,
        prior: float = 0.5,
    ) -> list[BeliefScore]:
        """Score and rank candidates from strongest to weakest belief."""

        scores = [
            self.score(candidate, evidence_by_candidate.get(candidate.id, ()), head=head, prior=prior)
            for candidate in candidates
        ]
        return sorted(scores, key=lambda item: item.probability, reverse=True)

    def build_recall_packet(
        self,
        scores: list[BeliefScore] | tuple[BeliefScore, ...],
    ) -> BeliefRecallPacket:
        """Group scored beliefs into LLM-facing assertion policy buckets."""

        safe_to_assert: list[BeliefScore] = []
        mention_with_uncertainty: list[BeliefScore] = []
        conflict_set: list[BeliefScore] = []
        do_not_assert: list[BeliefScore] = []

        for score in sorted(scores, key=lambda item: item.probability, reverse=True):
            match score.bucket:
                case RecallBucket.SAFE_TO_ASSERT:
                    safe_to_assert.append(score)
                case RecallBucket.MENTION_WITH_UNCERTAINTY:
                    mention_with_uncertainty.append(score)
                case RecallBucket.CONFLICT_SET:
                    conflict_set.append(score)
                case RecallBucket.DO_NOT_ASSERT:
                    do_not_assert.append(score)

        return BeliefRecallPacket(
            safe_to_assert=tuple(safe_to_assert),
            mention_with_uncertainty=tuple(mention_with_uncertainty),
            conflict_set=tuple(conflict_set),
            do_not_assert=tuple(do_not_assert),
        )

    @staticmethod
    def classify(
        *,
        head: BeliefHead,
        probability: float,
        support: float,
        contradiction: float,
    ) -> RecallBucket:
        """Map a probability/evidence profile to an assertion policy bucket."""

        if head == BeliefHead.SUPERSEDED and probability >= 0.70:
            return RecallBucket.DO_NOT_ASSERT
        if probability < 0.35:
            return RecallBucket.DO_NOT_ASSERT
        if support > 0 and contradiction > 0 and contradiction >= support * 0.45:
            return RecallBucket.CONFLICT_SET
        if probability >= 0.80:
            return RecallBucket.SAFE_TO_ASSERT
        if probability >= 0.55:
            return RecallBucket.MENTION_WITH_UNCERTAINTY
        return RecallBucket.DO_NOT_ASSERT


# ---------------------------------------------------------------------------
# R3 currentness policy (belief-layer.md Rung 1 / "Immediate target #2")
#
# A transparent, intent-aware layer on TOP of the Rung-0 BeliefScorer. It does not
# touch the scorer; it re-buckets already-scored beliefs by query intent so a
# superseded-but-relevant belief is not asserted as current truth (the CE-willow
# poisoning failure), while staying available as historical/conflict context.
# Bench-gated: this graduates only once archolith-bench shows it cuts stale-current
# assertions vs the baseline (belief-layer.md promotion condition).
# ---------------------------------------------------------------------------

# Evidence signals that mark a candidate belief as no-longer-current (superseded),
# independent of how it scored. Transparent baseline; the bench may refine it.
SUPERSESSION_SIGNALS: frozenset[EvidenceSignal] = frozenset(
    {EvidenceSignal.IS_EXPIRED, EvidenceSignal.LATER_CONTRADICTED}
)


def is_superseded_belief(
    evidence: list[BeliefEvidence] | tuple[BeliefEvidence, ...],
) -> bool:
    """True if any evidence marks this belief as expired or later-contradicted.

    Polarity-agnostic by design: whether the supersession signal was scored as
    support (on a SUPERSEDED head) or contradiction (on a CURRENT head), its
    presence means the belief is no longer current."""
    return any(item.signal in SUPERSESSION_SIGNALS for item in evidence)


def currentness_bucket(
    *,
    base: RecallBucket,
    intent: QueryIntent,
    is_superseded: bool,
    is_unsafe: bool,
) -> RecallBucket:
    """Map a Rung-0 bucket + query intent + belief flags to a currentness bucket.

    The load-bearing rule: under CURRENT intent a superseded belief that WOULD have
    been asserted is moved to ANERGIC_CURRENT (suppressed from current truth); under
    HISTORICAL intent it is HISTORICAL_ONLY (first-class former belief). Unsafe/noise
    is BLOCKED regardless of intent. Current (non-superseded) beliefs keep their
    baseline bucket."""
    if is_unsafe:
        return RecallBucket.BLOCKED
    if not is_superseded:
        return base
    if intent is QueryIntent.HISTORICAL:
        return RecallBucket.HISTORICAL_ONLY
    if intent is QueryIntent.CONFLICT:
        return base if base is RecallBucket.CONFLICT_SET else RecallBucket.HISTORICAL_ONLY
    # CURRENT intent: keep superseded beliefs out of current truth.
    if base in (RecallBucket.SAFE_TO_ASSERT, RecallBucket.MENTION_WITH_UNCERTAINTY):
        return RecallBucket.ANERGIC_CURRENT
    return RecallBucket.HISTORICAL_ONLY


def is_unsafe_belief(score: BeliefScore, *, is_superseded: bool) -> bool:
    """True for genuinely unsupported noise (block it), distinct from former belief.

    A DO_NOT_ASSERT belief with NO supersession evidence is not historically useful
    and not assertable -> BLOCKED. A superseded DO_NOT_ASSERT is history, not noise."""
    return (not is_superseded) and score.bucket is RecallBucket.DO_NOT_ASSERT


def build_intent_aware_packet(
    scored_with_evidence: list[tuple[BeliefScore, list[BeliefEvidence] | tuple[BeliefEvidence, ...]]],
    *,
    intent: QueryIntent,
) -> BeliefRecallPacket:
    """Re-bucket scored beliefs under the currentness policy for a query intent.

    Each input pairs a BeliefScore with the evidence it was scored on (so supersession
    can be derived transparently). Returns the 7-group packet; the original four groups
    carry current beliefs, the three new groups carry the policy outcomes."""
    groups: dict[RecallBucket, list[BeliefScore]] = {bucket: [] for bucket in RecallBucket}
    for score, evidence in scored_with_evidence:
        superseded = is_superseded_belief(evidence)
        unsafe = is_unsafe_belief(score, is_superseded=superseded)
        effective = currentness_bucket(
            base=score.bucket, intent=intent, is_superseded=superseded, is_unsafe=unsafe
        )
        groups[effective].append(score)

    def _ordered(bucket: RecallBucket) -> tuple[BeliefScore, ...]:
        return tuple(sorted(groups[bucket], key=lambda s: s.probability, reverse=True))

    return BeliefRecallPacket(
        safe_to_assert=_ordered(RecallBucket.SAFE_TO_ASSERT),
        mention_with_uncertainty=_ordered(RecallBucket.MENTION_WITH_UNCERTAINTY),
        conflict_set=_ordered(RecallBucket.CONFLICT_SET),
        do_not_assert=_ordered(RecallBucket.DO_NOT_ASSERT),
        historical_only=_ordered(RecallBucket.HISTORICAL_ONLY),
        anergic_current=_ordered(RecallBucket.ANERGIC_CURRENT),
        blocked=_ordered(RecallBucket.BLOCKED),
    )
