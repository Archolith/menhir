"""Assertion pipeline — the Oracle -> Warden handoff (observe -> decide).

Ties the retrieval stack together end to end:

    OracleExecutor (R4)  observe  -> per-candidate OracleResults
    OracleCombiner (R7)  rank     -> ranked ids + per-candidate OraclePacket
    WardenChain          decide   -> admit / flag / attenuate / refuse each ranked candidate

The combiner RANKS; the wardens DECIDE whether each ranked candidate may enter the agent's
current-truth context. The result is bucketed like the belief recall packet:

    admitted   -> current-truth context, in ranked order (ADMIT), score = combiner probability
                  (x attenuation factor)
    flagged    -> shown but labeled (historical / conflict / uncertain) — FLAG
    refused    -> kept out of current-truth context (superseded / wrong-scope / unsafe) — REFUSE

Wardens read one shared snapshot: ScopeWarden + EvidenceAnchorWarden read the candidate
metadata (scope, evidence_kinds), OracleAdmissionWarden reads the OraclePacket (currentness/
conflict). No double-application — each warden owns a disjoint axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from menhir.domain.belief import QueryIntent
from menhir.domain.belief_evidence import assemble_belief_evidence, score_candidate_belief
from menhir.domain.intent_affinity import task_intents_to_lens
from menhir.domain.oracles import CandidateMemory, QueryContext
from menhir.domain.query_intent import classify_intent
from menhir.domain.scope import MemoryScope
from menhir.domain.self_reinforcement import SupportProfile
from menhir.domain.truth.labels import WardenLabel
from menhir.domain.warden import (
    ContradictionWarden,
    CurrentnessWarden,
    EvidenceAnchorWarden,
    OracleAdmissionWarden,
    ScopeWarden,
    Warden,
    WardenChain,
    WardenContext,
    WardenDecision,
)
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.retrieval_oracles import default_oracles

# Map the oracle QueryContext intent string to the warden QueryIntent enum.
_INTENT = {"current": QueryIntent.CURRENT, "historical": QueryIntent.HISTORICAL, "any": QueryIntent.CURRENT}


@dataclass(frozen=True)
class AdmissionResult:
    """One ranked candidate's final admission decision."""

    candidate_id: str
    rank: int
    score: float                 # combiner probability x any warden attenuation
    decision: WardenDecision
    label: WardenLabel | None = None  # warden FLAG qualifier — always a WardenLabel value
    reason: str = ""


@dataclass
class AssertionOutcome:
    """The bucketed pipeline result (mirrors the belief recall packet)."""

    admitted: list[AdmissionResult] = field(default_factory=list)   # current-truth context
    flagged: list[AdmissionResult] = field(default_factory=list)    # shown, labeled
    refused: list[AdmissionResult] = field(default_factory=list)    # kept out

    @property
    def ranked(self) -> list[AdmissionResult]:
        """All results in the combiner's rank order (admitted+flagged+refused interleaved)."""
        return sorted([*self.admitted, *self.flagged, *self.refused], key=lambda r: r.rank)


def _candidate_scope(candidate: CandidateMemory) -> MemoryScope:
    m = candidate.metadata
    def s(k): return str(m[k]) if m.get(k) not in (None, "") else None
    return MemoryScope(repo=s("repo"), project=s("project"), branch=s("branch"), namespace=s("namespace"))


def _support_profile(candidate: CandidateMemory) -> SupportProfile:
    kinds = tuple(str(k) for k in candidate.metadata.get("evidence_kinds", ()))
    meta_depth = int(candidate.metadata.get("meta_depth", 0) or 0)
    return SupportProfile(support_kinds=kinds, meta_depth=meta_depth)


class AssertionPipeline:
    """Run observe (oracles) -> rank (combiner) -> decide (wardens) for a query."""

    def __init__(self, combiner, *, executor: OracleExecutor | None = None,
                 wardens: list[Warden] | None = None, auto_intent: bool = True,
                 contradiction_interrupt: bool = False,
                 belief_gate: bool = False, evidence_anchor: bool = True) -> None:
        self.executor = executor or OracleExecutor(default_oracles())
        self.combiner = combiner
        # The handoff chain: scope (metadata) + anti-spiral (evidence) + oracle-axis (packet).
        # evidence_anchor drops the anti-spiral Guard 5 for anecdotal corpora, where every
        # recalled fact is LLM-extracted from conversation (no external code/test/git anchor)
        # and Guard 5 would otherwise refuse the entire result set.
        default = [ScopeWarden()]
        if evidence_anchor:
            default.append(EvidenceAnchorWarden())
        default.append(OracleAdmissionWarden())
        if belief_gate:
            default.append(CurrentnessWarden())
        if contradiction_interrupt:
            default.append(ContradictionWarden())
        self.wardens = wardens or default
        self.chain = WardenChain(self.wardens)
        self.auto_intent = auto_intent
        self.belief_gate = belief_gate

    def _resolve_intent(self, query: QueryContext) -> QueryContext:
        """Derive the temporal lens from the query text when the caller did not pin one.

        Convention (plan Phase 2 #5): a default ``intent="current"`` means "use default
        behavior" -> classify the task intent and let it select the lens (avoid_repeat ->
        historical, verify_currentness -> any, else current). An explicitly non-default intent
        is honored as-is. The classifier only ever upgrades the default on an explicit history
        cue; a no-cue query classifies LOW and the lens stays "current" (no change)."""
        if not self.auto_intent or query.intent != "current":
            return query
        hits, _ = classify_intent(query.text)
        lens = task_intents_to_lens([h.intent for h in hits])
        return query if lens == "current" else replace(query, intent=lens)

    async def run(self, query: QueryContext, candidates: list[CandidateMemory]) -> AssertionOutcome:
        query = self._resolve_intent(query)
        grouped = await self.executor.evaluate(query, candidates)
        ranked_ids, packets = self.combiner.rank(query, grouped)
        by_id = {c.id: c for c in candidates}
        intent = _INTENT.get(query.intent, QueryIntent.CURRENT)

        outcome = AssertionOutcome()
        q_scope = MemoryScope(repo=query.repo, project=query.project, branch=query.branch, namespace=query.namespace)
        for rank, cid in enumerate(ranked_ids):
            candidate = by_id[cid]
            packet = packets[cid]
            belief_score = (
                score_candidate_belief(cid, candidate.content, candidate.metadata)
                if self.belief_gate else None
            )
            evidence = (
                assemble_belief_evidence(candidate.metadata) if self.belief_gate else ()
            )
            ctx = WardenContext(
                candidate_id=cid, intent=intent,
                belief_score=belief_score, evidence=evidence,
                query_scope=q_scope, candidate_scope=_candidate_scope(candidate),
                support_profile=_support_profile(candidate), oracle_packet=packet,
            )
            verdict = self.chain.evaluate(ctx)
            result = AdmissionResult(
                candidate_id=cid, rank=rank,
                score=round(packet.combined_probability * verdict.factor, 6),
                decision=verdict.decision, label=verdict.label, reason=verdict.reason,
            )
            if verdict.decision is WardenDecision.REFUSE:
                outcome.refused.append(result)
            elif verdict.decision is WardenDecision.FLAG:
                outcome.flagged.append(result)
            else:  # ADMIT or ATTENUATE both enter current-truth context (attenuated score)
                outcome.admitted.append(result)
        return outcome
