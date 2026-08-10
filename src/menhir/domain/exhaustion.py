"""Retrieval exhaustion policy — session-local attenuation for agent loops (R3 rung E).

belief-layer.md "Productive recency and exhaustion": an agent can reinforce its own
failure loop —

    memory retrieved -> last_accessed touched -> recency score improves
    -> memory retrieved again -> agent repeats the same failed reasoning

The fix is NOT to remove recency. It is to distinguish *productive* recency from *loop*
recency. The load-bearing signal is therefore ``retrievals_since_progress`` (how many
times this memory has been pulled with no progress event since), NOT the raw retrieval
count — a memory that keeps helping is retrieved often and must not be penalized.

This is the transparent baseline (no probabilistic backend): a pure, inspectable decision
keyed on session counters + a progress signal + exemptions. It is separate from the belief
currentness policy (belief.py): currentness gates *superseded* truth; exhaustion gates
*unproductive repetition* within a session. A memory can be perfectly current yet looping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExhaustionDecision(str, Enum):
    """What to do with a candidate given its session retrieval history."""

    ALLOW = "allow"          # full score; not looping (or exempt)
    ATTENUATE = "attenuate"  # relevant but repeating unproductively — damp its score
    SUPPRESS = "suppress"    # stuck in a loop — keep it out of this turn's context


# Memory classes that must NEVER be exhaustion-penalized, however often retrieved
# (belief-layer.md exemptions). Repeated retrieval of these is expected, not a loop.
class ExemptReason(str, Enum):
    CURRENT_TASK_GOAL = "current_task_goal"
    ACTIVE_ERROR_LOG = "active_error_log"
    USER_INSTRUCTION = "user_instruction"
    SOURCE_OF_TRUTH = "source_of_truth"
    FOUNDATIONAL = "foundational"


@dataclass(frozen=True)
class RetrievalStats:
    """Session-local retrieval history for one candidate memory.

    ``retrievals_since_progress`` resets to 0 whenever a progress event fires (test
    passed, failure changed, user confirmed, patch accepted, conflict resolved, ...),
    so it measures *unproductive* repetition specifically.
    """

    session_retrieval_count: int = 0
    retrievals_since_progress: int = 0
    exempt_reason: ExemptReason | None = None

    @property
    def is_exempt(self) -> bool:
        return self.exempt_reason is not None


# Defaults: a memory may be pulled twice with no progress before it is damped, and is
# suppressed once it has been pulled this many times since the last progress. Tunable;
# the bench decides the values, these are a transparent starting point.
_DEFAULT_ATTENUATE_AT = 2
_DEFAULT_SUPPRESS_AT = 4
_ATTENUATION_FACTOR = 0.4  # multiplicative score damp applied to an ATTENUATE decision


def exhaustion_decision(
    stats: RetrievalStats,
    *,
    attenuate_at: int = _DEFAULT_ATTENUATE_AT,
    suppress_at: int = _DEFAULT_SUPPRESS_AT,
) -> ExhaustionDecision:
    """Decide whether a candidate is in an unproductive retrieval loop.

    Exempt memories always ALLOW. Otherwise the decision is keyed ONLY on
    ``retrievals_since_progress`` — raw retrieval count never penalizes by itself
    (a memory that keeps producing progress resets its counter and stays ALLOW)."""
    if stats.is_exempt:
        return ExhaustionDecision.ALLOW
    if stats.retrievals_since_progress >= suppress_at:
        return ExhaustionDecision.SUPPRESS
    if stats.retrievals_since_progress >= attenuate_at:
        return ExhaustionDecision.ATTENUATE
    return ExhaustionDecision.ALLOW


def exhaustion_factor(
    stats: RetrievalStats,
    *,
    attenuate_at: int = _DEFAULT_ATTENUATE_AT,
    suppress_at: int = _DEFAULT_SUPPRESS_AT,
    attenuation: float = _ATTENUATION_FACTOR,
) -> float:
    """Multiplicative score factor in [0, 1] implementing the decision.

    ALLOW -> 1.0 (no change), ATTENUATE -> ``attenuation``, SUPPRESS -> 0.0. A score
    multiplied by this factor keeps productive/exempt memories intact and damps or drops
    looping ones, without touching the underlying relevance computation."""
    decision = exhaustion_decision(stats, attenuate_at=attenuate_at, suppress_at=suppress_at)
    if decision is ExhaustionDecision.SUPPRESS:
        return 0.0
    if decision is ExhaustionDecision.ATTENUATE:
        return max(0.0, min(1.0, attenuation))
    return 1.0
