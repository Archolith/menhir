"""Wardens — the assertion-boundary guard layer (peer of Oracle and Mutator).

The three object types in menhir's recall/knowledge stack:

    Oracle   observe -> a score/signal over a candidate (read-only)
    Warden   guard   -> an operational decision on whether a retrieved memory may enter
                        the agent's current-truth context (admit / flag / attenuate / refuse)
    Mutator  write   -> the persistence boundary

A Warden consumes a candidate + context (belief score, evidence, query intent, session
retrieval stats — the signals produced by Oracles and by the Chronostratum temporal/git
modules) and returns a WardenVerdict. It does NOT score (Oracle) and does NOT write
(Mutator). The concrete wardens wrap the policies already built as pure functions:

    CurrentnessWarden  <- belief.currentness_bucket (superseded -> refuse/flag, fed by
                          temporal.py / git_staleness.py supersession signals)
    ExhaustionWarden   <- exhaustion.exhaustion_decision (unproductive loop -> attenuate/refuse)

Multiple wardens compose through WardenChain (most-restrictive-wins), the way Oracles
compose through a combiner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from menhir.domain.belief import (
    BeliefEvidence,
    BeliefScore,
    QueryIntent,
    RecallBucket,
    currentness_bucket,
    is_superseded_belief,
    is_unsafe_belief,
)
from menhir.domain.exhaustion import ExhaustionDecision, RetrievalStats, exhaustion_decision
from menhir.domain.oracles import OraclePacket, OraclePolarity, OracleTarget
from menhir.domain.scope import MemoryScope, scope_conflict
from menhir.domain.self_reinforcement import (
    ReinforcementVerdict,
    SupportProfile,
    assess_reinforcement,
)
from menhir.domain.truth.labels import WardenLabel


class WardenDecision(str, Enum):
    """An operational decision at the assertion boundary, in increasing restrictiveness."""

    ADMIT = "admit"          # allow into current-truth context unchanged
    FLAG = "flag"            # admit, but labeled (historical / conflict / uncertain)
    ATTENUATE = "attenuate"  # admit with a damped score
    REFUSE = "refuse"        # keep out of current-truth context (suppressed / unsafe)


# Restrictiveness order for combining verdicts (later = more restrictive wins).
_RESTRICTIVENESS = {
    WardenDecision.ADMIT: 0,
    WardenDecision.FLAG: 1,
    WardenDecision.ATTENUATE: 2,
    WardenDecision.REFUSE: 3,
}


@dataclass(frozen=True)
class WardenVerdict:
    """A warden's decision on a candidate, with provenance."""

    decision: WardenDecision
    warden: str
    reason: str = ""
    label: WardenLabel | None = None  # set for FLAG — always a WardenLabel value
    factor: float = 1.0               # score multiplier for ATTENUATE (1.0 otherwise)


@dataclass(frozen=True)
class WardenContext:
    """Everything a warden might read about one candidate. Each warden uses what it needs."""

    candidate_id: str
    belief_score: BeliefScore | None = None
    evidence: tuple[BeliefEvidence, ...] = ()
    intent: QueryIntent = QueryIntent.CURRENT
    retrieval_stats: RetrievalStats | None = None
    query_scope: MemoryScope | None = None      # where the query is asked
    candidate_scope: MemoryScope | None = None  # where the candidate memory lives
    support_profile: SupportProfile | None = None  # provenance for the anti-spiral guard
    oracle_packet: OraclePacket | None = None   # the R7 combiner output for this candidate


@runtime_checkable
class Warden(Protocol):
    name: str

    def evaluate(self, ctx: WardenContext) -> WardenVerdict: ...


# bucket -> (decision, label) for the currentness warden.
_BUCKET_DECISION: dict[RecallBucket, tuple[WardenDecision, WardenLabel | None]] = {
    RecallBucket.SAFE_TO_ASSERT: (WardenDecision.ADMIT, None),
    RecallBucket.MENTION_WITH_UNCERTAINTY: (WardenDecision.FLAG, WardenLabel.UNCERTAIN),
    RecallBucket.CONFLICT_SET: (WardenDecision.FLAG, WardenLabel.CONFLICT),
    RecallBucket.HISTORICAL_ONLY: (WardenDecision.FLAG, WardenLabel.HISTORICAL),
    RecallBucket.ANERGIC_CURRENT: (WardenDecision.REFUSE, None),
    RecallBucket.BLOCKED: (WardenDecision.REFUSE, None),
    RecallBucket.DO_NOT_ASSERT: (WardenDecision.REFUSE, None),
}


@dataclass
class CurrentnessWarden:
    """Guards against asserting superseded beliefs as current truth (belief currentness)."""

    name: str = "currentness"

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        score = ctx.belief_score
        if score is None:
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no belief score")
        superseded = is_superseded_belief(list(ctx.evidence))
        unsafe = is_unsafe_belief(score, is_superseded=superseded)
        bucket = currentness_bucket(
            base=score.bucket, intent=ctx.intent, is_superseded=superseded, is_unsafe=unsafe
        )
        decision, label = _BUCKET_DECISION[bucket]
        return WardenVerdict(decision, self.name, reason=f"bucket={bucket.value}", label=label)


@dataclass
class ExhaustionWarden:
    """Guards against unproductive retrieval loops (session-local exhaustion)."""

    name: str = "exhaustion"
    attenuation: float = 0.4

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        stats = ctx.retrieval_stats
        if stats is None:
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no retrieval stats")
        decision = exhaustion_decision(stats)
        if decision is ExhaustionDecision.SUPPRESS:
            return WardenVerdict(WardenDecision.REFUSE, self.name, reason="retrieval loop")
        if decision is ExhaustionDecision.ATTENUATE:
            return WardenVerdict(WardenDecision.ATTENUATE, self.name, reason="repeating unproductively", factor=self.attenuation)
        return WardenVerdict(WardenDecision.ADMIT, self.name, reason="productive/within limit")


@dataclass
class ScopeWarden:
    """Guards against wrong-scope memory entering current context (SelfToleranceGate).

    Refuses a candidate whose repo/project/branch/namespace conflicts with the query's —
    an old-branch memory, another repo's memory, the same filename in a different project.
    Permissive on missing scope (absence is 'unknown', not non-self): no scope on either
    side -> ADMIT. This is the gap currentness does NOT cover (temporal vs scope)."""

    name: str = "scope"

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        if ctx.query_scope is None or ctx.candidate_scope is None:
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="scope unknown")
        conflict, dims = scope_conflict(ctx.query_scope, ctx.candidate_scope)
        if conflict:
            return WardenVerdict(WardenDecision.REFUSE, self.name, reason=f"wrong scope: {', '.join(dims)}")
        return WardenVerdict(WardenDecision.ADMIT, self.name, reason="same scope")


@dataclass
class EvidenceAnchorWarden:
    """Guards against self-reinforcement: a current-truth assertion needs a non-self anchor
    and a shallow support chain (R8 SelfReinforcementGuard, Guards 2/3/5).

    Refuses a synthetic-only / unanchored claim (retrieval & LLM-summaries are attention,
    not truth -- rule zero); flags a too-deep memory-call chain. Permissive when no support
    profile is supplied (absence is unknown, not non-self)."""

    name: str = "evidence_anchor"

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        profile = ctx.support_profile
        if profile is None:
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no support profile")
        verdict = assess_reinforcement(profile)
        if verdict is ReinforcementVerdict.NEEDS_EVIDENCE:
            return WardenVerdict(WardenDecision.REFUSE, self.name, reason="synthetic-only / no external anchor")
        if verdict is ReinforcementVerdict.OVER_DEPTH:
            return WardenVerdict(WardenDecision.FLAG, self.name, reason="meta-memory chain too deep", label=WardenLabel.SUSPICIOUS_CHAIN)
        return WardenVerdict(WardenDecision.ADMIT, self.name, reason="externally anchored")


@dataclass
class OracleAdmissionWarden:
    """Decides admission from the R7 OraclePacket's currentness/historicality/conflict axis.

    This is the Oracle->Warden handoff: the oracle pipeline RANKS (the packet), this warden
    DECIDES whether the ranked candidate may enter current-truth context. It reads the
    per-result targets/polarities (the same temporal/conflict signals, now in oracle form) —
    NOT scope (ScopeWarden owns that) and NOT synthetic support (EvidenceAnchorWarden owns
    that), so the chain composes without double-applying.

      currentness CONTRADICT + current intent  -> REFUSE (superseded; don't assert as current)
      currentness CONTRADICT + historical intent -> ADMIT (that's what they asked for)
      historicality SUPPORT  + current intent  -> FLAG 'historical' (former belief)
      conflict CONTRADICT                       -> FLAG 'conflict'
      role_logits['blocked'] > 0                -> REFUSE (unsafe/contaminated)
      else                                      -> ADMIT
    """

    name: str = "oracle_admission"

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        packet = ctx.oracle_packet
        if packet is None:
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no oracle packet")
        current = ctx.intent is QueryIntent.CURRENT

        if packet.role_logits.get("blocked", 0.0) > 0.0:
            return WardenVerdict(WardenDecision.REFUSE, self.name, reason="blocked logit > 0")

        currentness_contradicted = any(
            r.target is OracleTarget.CURRENTNESS and r.polarity is OraclePolarity.CONTRADICT
            for r in packet.results
        )
        if currentness_contradicted and current:
            return WardenVerdict(WardenDecision.REFUSE, self.name, reason="superseded under current intent")

        conflict = any(
            r.target is OracleTarget.CONFLICT and r.polarity is OraclePolarity.CONTRADICT
            for r in packet.results
        )
        if conflict:
            return WardenVerdict(WardenDecision.FLAG, self.name, reason="oracle conflict", label=WardenLabel.CONFLICT)

        historical_support = any(
            r.target is OracleTarget.HISTORICALITY and r.polarity is OraclePolarity.SUPPORT
            for r in packet.results
        )
        if historical_support and current:
            return WardenVerdict(WardenDecision.FLAG, self.name, reason="historical under current intent", label=WardenLabel.HISTORICAL)

        return WardenVerdict(WardenDecision.ADMIT, self.name, reason="oracle-clean")


@dataclass
class ContradictionWarden:
    """Guard 7 (ContradictionInterrupt): a contradiction interrupts CURRENT assertion.

    Contradiction (oracle CONFLICT/CONTRADICT, or belief CONFLICT_SET bucket) means the
    candidate must not enter current-truth context until resolved -> REFUSE under current
    intent; historical intent still gets it, labeled 'conflict' (history is preserved). This
    is stricter than OracleAdmissionWarden's FLAG; most-restrictive-wins makes the REFUSE win
    when both fire. Assertion-side only -- the Guard-1 heat-freeze is deferred (mutator side).
    """

    name: str = "contradiction"

    def _contradicted(self, ctx: WardenContext) -> bool:
        """Check if a contradiction exists in belief score or oracle results."""
        if ctx.belief_score is not None and ctx.belief_score.bucket is RecallBucket.CONFLICT_SET:
            return True
        packet = ctx.oracle_packet
        if packet is not None:
            return any(
                r.target is OracleTarget.CONFLICT and r.polarity is OraclePolarity.CONTRADICT
                for r in packet.results
            )
        return False

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        if not self._contradicted(ctx):
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no contradiction")
        if ctx.intent is QueryIntent.CURRENT:
            return WardenVerdict(
                WardenDecision.REFUSE,
                self.name,
                reason="contradiction interrupts current assertion",
            )
        return WardenVerdict(
            WardenDecision.FLAG,
            self.name,
            reason="contradiction under historical intent",
            label=WardenLabel.CONFLICT,
        )


@dataclass
class WardenChain:
    """Run several wardens over a candidate; the most restrictive verdict wins.

    Mirrors the oracle combiner but for decisions, not scores: REFUSE beats ATTENUATE
    beats FLAG beats ADMIT. ATTENUATE factors multiply across wardens. The contributing
    verdicts are kept for explainability."""

    wardens: list[Warden] = field(default_factory=list)

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        verdicts = [w.evaluate(ctx) for w in self.wardens]
        if not verdicts:
            return WardenVerdict(WardenDecision.ADMIT, "chain", reason="no wardens")
        winner = max(verdicts, key=lambda v: _RESTRICTIVENESS[v.decision])
        factor = 1.0
        for v in verdicts:
            factor *= v.factor
        reason = "; ".join(f"{v.warden}:{v.decision.value}" for v in verdicts)
        return WardenVerdict(
            decision=winner.decision, warden="chain", reason=reason,
            label=winner.label, factor=round(factor, 6),
        )
