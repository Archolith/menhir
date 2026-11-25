"""One-pass oracle combiner (R7) — the menhir port of the killer baseline.

``LogSpaceOracleCombiner`` (ladder F) reduces per-candidate OracleResults (from the R4
executor over the R6 oracles) into a ranked list + an OraclePacket using role-specific
log-space logits (relevant/current/historical/conflict/blocked). Contradiction is negative
log-evidence D = lambda * q^gamma (not subtraction from a final score); duplicate evidence
is down-weighted by source-family independence (1/sqrt(n)); per-family positive
contribution is capped; missing evidence raises uncertainty rather than suppressing.

The math follows docs/research/oracle-amplified-retrieval.md. F is build-first; iterative
amplification (R11) must beat F to ship and is not here. Its output feeds the Wardens — this
is the Oracle->Warden handoff: the combiner RANKS, the wardens then DECIDE admission.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping

from menhir.domain.oracles import (
    OraclePacket,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
    Role,
)
from menhir.domain.temporal_lens import TemporalLens

# Support weights by source family (calibration placeholders, from the research doc).
FAMILY_ALPHA: dict[str, float] = {
    "semantic": 0.8, "structure": 1.0, "temporal": 1.0, "scope": 0.6,
    # Provenance strength is an admission/trust concern owned by EvidenceAnchorWarden,
    # not query relevance. Keep an explicit zero so an opt-in diagnostic EvidenceOracle
    # cannot accidentally outrank an exact semantic hit.
    "evidence": 0.0,
    # Starting value, to be tuned (#572): without it intent defaulted to 1.0, above semantic.
    "intent": 0.4,
}
# Contradiction penalty strength by the contradiction's target.
TARGET_LAMBDA: dict[OracleTarget, float] = {
    OracleTarget.CURRENTNESS: 2.5, OracleTarget.SCOPE: 2.0, OracleTarget.CONFLICT: 3.0,
}
DEFAULT_LAMBDA = 2.0
GAMMA = 1.5
MAX_FAMILY_CONTRIBUTION = 3.0
HISTORICAL_BOOST = 0.5
SCOPE_SUPPORT_FRACTION = 0.25


def _independence(results: list[OracleResult]) -> dict[str, float]:
    """1/sqrt(n) per source family so duplicate evidence cannot fake certainty."""
    counts: dict[str, int] = defaultdict(int)
    for r in results:
        counts[r.source_family] += 1
    return {fam: 1.0 / math.sqrt(n) for fam, n in counts.items()}


def _softmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    top = max(scores)
    exps = [math.exp(s - top) for s in scores]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


def _rank_and_fill(raw: dict[str, float], packets: dict[str, OraclePacket]) -> list[str]:
    """Sort by score desc, preserving incoming rank on ties, and fill probabilities."""
    incoming_rank = {cid: rank for rank, cid in enumerate(raw)}
    ranked = sorted(raw, key=lambda cid: (-raw[cid], incoming_rank[cid]))
    probs = _softmax([raw[cid] for cid in ranked])
    for cid, p in zip(ranked, probs):
        packets[cid].combined_probability = p
    return ranked


class LogSpaceOracleCombiner:
    """Ladder F: role-specific log-space logits with contradiction as neg. log-evidence."""

    name = "logspace"

    def __init__(self, role_weights: Mapping[str, float] | None = None) -> None:
        # None (or a missing role) weighs 1.0, which reproduces the unweighted score exactly.
        self.role_weights: dict[str, float] = dict(role_weights or {})

    def rank(
        self, query: QueryContext, grouped: dict[str, list[OracleResult]]
    ) -> tuple[list[str], dict[str, OraclePacket]]:
        packets: dict[str, OraclePacket] = {}
        raw: dict[str, float] = {}
        for cid, results in grouped.items():
            packet = self._combine_one(cid, results)
            packets[cid] = packet
            terms = self._ranking_terms(query, packet.role_logits, self.role_weights)
            raw[cid] = self._ranking_score(query, packet.role_logits, self.role_weights)
            packet.contributions = dict(terms)
            packet.ranking_score = raw[cid]
        return _rank_and_fill(raw, packets), packets

    def _combine_one(self, cid: str, results: list[OracleResult]) -> OraclePacket:
        z = {"relevant": 0.0, "current": 0.0, "historical": 0.0, "conflict": 0.0, "blocked": 0.0}
        independence = _independence(results)
        family_used: dict[str, float] = defaultdict(float)
        rationale: list[str] = []
        missing = 0

        def add_support(role: str, family: str, magnitude: float) -> float:
            remaining = max(0.0, MAX_FAMILY_CONTRIBUTION - family_used[family])
            applied = min(magnitude, remaining)
            z[role] += applied
            family_used[family] += applied
            return applied

        for r in results:
            ind = independence.get(r.source_family, 1.0)
            alpha = FAMILY_ALPHA.get(r.source_family, 1.0)
            magnitude = alpha * r.probability * r.confidence * r.directness * r.scope_match * ind
            q = r.probability * r.confidence * r.directness * r.scope_match * ind
            penalty = TARGET_LAMBDA.get(r.target, DEFAULT_LAMBDA) * (q ** GAMMA)

            if r.polarity == OraclePolarity.MISSING:
                missing += 1
                continue
            if r.polarity == OraclePolarity.NEUTRAL:
                continue

            if r.log_odds is not None:
                # A calibrated relevance score (the cross-encoder logit) keeps its own uncapped
                # role: the family cap would flatten it, and wardens never read this role.
                if r.polarity == OraclePolarity.SUPPORT and math.isfinite(r.log_odds):
                    z[Role.RERANK.value] = z.get(Role.RERANK.value, 0.0) + r.log_odds
                    rationale.append(f"rerank {r.oracle} {r.log_odds:.2f}")
                continue

            if r.target == OracleTarget.RELEVANCE:
                if r.polarity == OraclePolarity.SUPPORT:
                    applied = add_support("relevant", r.source_family, magnitude)
                    rationale.append(f"+relevant {r.oracle} {applied:.2f}")
                else:
                    z["relevant"] -= penalty
                    rationale.append(f"-relevant {r.oracle} {penalty:.2f}")
            elif r.target == OracleTarget.CURRENTNESS:
                if r.polarity == OraclePolarity.SUPPORT:
                    applied = add_support("current", r.source_family, magnitude)
                    rationale.append(f"+current {r.oracle} {applied:.2f}")
                else:
                    z["current"] -= penalty
                    z["historical"] += HISTORICAL_BOOST * q
                    rationale.append(f"-current {r.oracle} {penalty:.2f} (+hist)")
            elif r.target == OracleTarget.HISTORICALITY:
                if r.polarity == OraclePolarity.SUPPORT:
                    applied = add_support("historical", r.source_family, magnitude)
                    rationale.append(f"+historical {r.oracle} {applied:.2f}")
            elif r.target == OracleTarget.SCOPE:
                if r.polarity == OraclePolarity.SUPPORT:
                    applied = add_support("relevant", r.source_family, SCOPE_SUPPORT_FRACTION * magnitude)
                    rationale.append(f"+relevant(scope) {r.oracle} {applied:.2f}")
                else:  # wrong-scope contamination suppresses relevance and blocks
                    z["blocked"] += penalty
                    z["relevant"] -= penalty
                    rationale.append(f"-relevant/+blocked {r.oracle} {penalty:.2f}")
            elif r.target == OracleTarget.CONFLICT:
                # Kept pending #566/#579/#659 (no default oracle emits OracleTarget.CONFLICT yet).
                if r.polarity == OraclePolarity.CONTRADICT:
                    z["conflict"] += penalty
                    z["current"] -= penalty
                    rationale.append(f"+conflict {r.oracle} {penalty:.2f}")

        return OraclePacket(
            candidate_id=cid, results=results, role_logits=z,
            combined_probability=0.0, uncertainty=missing / (len(results) or 1),
            rationale=rationale,
        )

    @staticmethod
    def _ranking_terms(
        query: QueryContext, z: Mapping[str, float], weights: Mapping[str, float] | None = None,
    ) -> list[tuple[str, float]]:
        """Signed, weighted per-role contributions the intent selects, in summation order.

        Intent picks which role eligibility ranks the candidate. The rerank role is added only
        when the candidate carries a rerank logit, so packets without one score as before.
        """
        w = weights or {}
        if query.intent == TemporalLens.HISTORICAL:
            signed = (("relevant", 1.0), ("historical", 1.0), ("blocked", -1.0))
        elif query.intent == TemporalLens.ANY:
            signed = (("relevant", 1.0), ("blocked", -1.0))
        else:
            signed = (("relevant", 1.0), ("current", 1.0), ("blocked", -1.0), ("conflict", -1.0))
        if Role.RERANK.value in z:
            signed = (*signed, (Role.RERANK.value, 1.0))
        return [(role, sign * (w.get(role, 1.0) * z[role])) for role, sign in signed]

    @staticmethod
    def _ranking_score(
        query: QueryContext, z: Mapping[str, float], weights: Mapping[str, float] | None = None,
    ) -> float:
        """Sum of ``_ranking_terms``; with unit weights this equals the unweighted role sum."""
        terms = LogSpaceOracleCombiner._ranking_terms(query, z, weights)
        score = terms[0][1]
        for _, value in terms[1:]:
            score += value
        return score
