"""Cheap retrieval oracles (R6) — the menhir port, consuming the domain producers.

Each oracle reads ONE class of evidence over a prefetched candidate snapshot and returns an
OracleResult (evidence, never final truth). All are stateless, pure, read-only — so the
OracleExecutor (R4) reduces them deterministically.

The menhir difference from the bench prototype: the temporal and scope oracles CONSUME the
existing domain producers rather than re-deriving the rules — TemporalOracle uses
`domain/temporal.py` (the one supersession/validity producer) and ScopeOracle uses
`domain/scope.py`. This is the ladder's "one producer, many consumers" rule: the oracle that
*ranks* uses the same classification the Warden that *decides* uses, so they cannot drift.

The cheap set: Semantic (lexical stand-in, real-embedder seam), Structure (file/symbol/test
overlap), Scope (domain/scope), Temporal (domain/temporal), Evidence (provenance strength).
"""

from __future__ import annotations

import os
from typing import Protocol

from menhir.domain.artifact_role import derive_content_role
from menhir.domain.intent_affinity import affinity_to_weight, resolve_affinity
from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
    RetrievalOracle,
)
from menhir.domain.query_intent import IntentConfidence, TaskIntent, classify_intent
from menhir.domain.scope import MemoryScope, scope_conflict
from menhir.domain.temporal import FactTemporal, TemporalRole, temporal_role
from menhir.domain.self_reinforcement import ANCHOR_KINDS, SELF_SOURCE_KINDS


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(t) > 1}


def _overlap_coefficient(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


class SemanticScorer(Protocol):
    """Query<->candidate text similarity in [0, 1]. The real-embedder seam."""

    name: str

    def similarity(self, query_text: str, candidate_text: str) -> float: ...


class LexicalSemanticScorer:
    """Deterministic lexical-overlap stand-in. Swap a real embedder before quoting numbers."""

    name = "lexical_stub"

    def similarity(self, query_text: str, candidate_text: str) -> float:
        return _overlap_coefficient(_tokens(query_text), _tokens(candidate_text))


class SemanticOracle:
    name = "semantic"
    source_family = "semantic"

    def __init__(self, scorer: SemanticScorer | None = None) -> None:
        self.scorer = scorer or LexicalSemanticScorer()

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        # Prefer a precomputed real similarity (e.g. the recall path's cosine from vector
        # search) when the caller injects it into metadata; otherwise fall back to the
        # text scorer (lexical stub or an injected embedder). This lets the combiner rank
        # on the SAME embedding signal ScoringService uses instead of lexical overlap.
        precomputed = candidate.metadata.get("similarity")
        if precomputed is not None:
            sim = max(0.0, min(1.0, float(precomputed)))
            note = f"precomputed_similarity={sim:.2f}"
        else:
            sim = self.scorer.similarity(query.text, candidate.content)
            note = f"lexical_overlap={sim:.2f}"
        return OracleResult(
            oracle=self.name, probability=sim, confidence=1.0,
            polarity=OraclePolarity.SUPPORT if sim > 0 else OraclePolarity.NEUTRAL,
            target=OracleTarget.RELEVANCE, source_family=self.source_family,
            note=note,
        )


class StructureOracle:
    """File/symbol/test overlap — structural relevance independent of wording."""

    name = "structure"
    source_family = "structure"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        q = set(query.files) | set(query.symbols) | set(query.tests)
        c = (
            set(candidate.metadata.get("files", ()))
            | set(candidate.metadata.get("symbols", ()))
            | set(candidate.metadata.get("tests", ()))
        )
        score = _overlap_coefficient(q, c) if q and c else 0.0
        return OracleResult(
            oracle=self.name, probability=score, confidence=1.0,
            polarity=OraclePolarity.SUPPORT if score > 0 else OraclePolarity.MISSING,
            target=OracleTarget.RELEVANCE, source_family=self.source_family,
            note=f"structure_overlap={score:.2f}",
        )


class ScopeOracle:
    """Repo/branch/project/namespace agreement — consumes domain/scope.scope_conflict."""

    name = "scope"
    source_family = "scope"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        q_scope = MemoryScope(repo=query.repo, project=query.project, branch=query.branch, namespace=query.namespace)
        m = candidate.metadata
        c_scope = MemoryScope(
            repo=_s(m.get("repo")), project=_s(m.get("project")),
            branch=_s(m.get("branch")), namespace=_s(m.get("namespace")),
        )
        conflict, dims = scope_conflict(q_scope, c_scope)
        if conflict:
            return OracleResult(
                oracle=self.name, probability=1.0, confidence=1.0,
                polarity=OraclePolarity.CONTRADICT, target=OracleTarget.SCOPE,
                source_family=self.source_family, note=f"scope_conflict={','.join(dims)}",
            )
        # no conflict: SUPPORT if any scope dimension was shared, else MISSING.
        shared = any(getattr(q_scope, d) and getattr(c_scope, d) for d in ("repo", "project", "branch", "namespace"))
        return OracleResult(
            oracle=self.name, probability=1.0 if shared else 0.0,
            confidence=1.0 if shared else 0.5,
            polarity=OraclePolarity.SUPPORT if shared else OraclePolarity.MISSING,
            target=OracleTarget.SCOPE, source_family=self.source_family,
            note="scope_match" if shared else "no_shared_scope",
        )


class TemporalOracle:
    """Currentness/historicality — consumes domain/temporal.temporal_role (the one producer).

    Builds a FactTemporal from the candidate's bitemporal metadata and routes the role to a
    target by intent. Directness is graded: an explicit timestamp role is direct (1.0); a
    belief-bucket-only inference is softer (0.6)."""

    name = "temporal"
    source_family = "temporal"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        m = candidate.metadata
        # A `superseded` flag with no explicit expired_at is treated as belief-expired
        # (temporal_role keys on expired_at IS NOT NULL -> HISTORICAL).
        expired_at = _s(m.get("expired_at"))
        if expired_at is None and m.get("superseded"):
            expired_at = "superseded"
        fact = FactTemporal(
            valid_at=_s(m.get("valid_at")), invalid_at=_s(m.get("invalid_at")),
            created_at=_s(m.get("created_at")), expired_at=expired_at,
        )
        role = temporal_role(fact, as_of=query.as_of_time)
        directness = 1.0 if (fact.valid_at or fact.invalid_at or fact.expired_at or fact.created_at) else 0.5
        intent = query.intent
        meta = self.source_family

        if role is TemporalRole.NOT_YET_KNOWN:
            return OracleResult(oracle=self.name, probability=1.0, confidence=1.0,
                                polarity=OraclePolarity.CONTRADICT, target=OracleTarget.CURRENTNESS,
                                directness=directness, source_family=meta, note="learned_after_as_of (anachronism)")
        if role is TemporalRole.NOT_YET_VALID:
            return OracleResult(oracle=self.name, probability=0.9, confidence=0.9,
                                polarity=OraclePolarity.CONTRADICT, target=OracleTarget.CURRENTNESS,
                                directness=directness, source_family=meta, note="not_yet_valid_at_as_of")
        if role in (TemporalRole.HISTORICAL, TemporalRole.EXPIRED_WORLD):
            if intent == "historical":
                return OracleResult(oracle=self.name, probability=1.0, confidence=0.9,
                                    polarity=OraclePolarity.SUPPORT, target=OracleTarget.HISTORICALITY,
                                    directness=directness, source_family=meta, note="superseded_answers_historical")
            return OracleResult(oracle=self.name, probability=1.0, confidence=0.9,
                                polarity=OraclePolarity.CONTRADICT, target=OracleTarget.CURRENTNESS,
                                directness=directness, source_family=meta, note="superseded_under_current_intent")
        if role is TemporalRole.CURRENT:
            if intent == "historical":
                return OracleResult(oracle=self.name, probability=0.3, confidence=0.6,
                                    polarity=OraclePolarity.NEUTRAL, target=OracleTarget.HISTORICALITY,
                                    directness=directness, source_family=meta, note="current_under_historical_intent")
            return OracleResult(oracle=self.name, probability=0.8, confidence=0.8,
                                polarity=OraclePolarity.SUPPORT, target=OracleTarget.CURRENTNESS,
                                directness=directness, source_family=meta, note="valid_at_as_of")
        return OracleResult(oracle=self.name, probability=0.0, confidence=0.4,
                            polarity=OraclePolarity.MISSING, target=OracleTarget.CURRENTNESS,
                            directness=directness, source_family=meta, note="no_temporal_anchor")


class EvidenceOracle:
    """Diagnostic provenance strength — consumes the self_reinforcement vocabulary.

    A non-self anchor (git/test/user/log/...) is strong; agent-only (agent_inference/
    llm_summary) is weak. This oracle is intentionally excluded from ``default_oracles``:
    provenance governs admission/trust through EvidenceAnchorWarden and must not act as
    query relevance. Missing evidence raises uncertainty (MISSING), never contradiction."""

    name = "evidence"
    source_family = "evidence"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        kinds = set(candidate.metadata.get("evidence_kinds", ()))
        if not kinds:
            return OracleResult(oracle=self.name, probability=0.0, confidence=0.3,
                                polarity=OraclePolarity.MISSING, target=OracleTarget.RELEVANCE,
                                directness=0.3, source_family=self.source_family, note="no_evidence")
        strong = bool(kinds & ANCHOR_KINDS)
        only_self = kinds <= SELF_SOURCE_KINDS
        directness = 1.0 if strong else (0.4 if only_self else 0.7)
        confidence = 1.0 if strong else (0.5 if only_self else 0.7)
        return OracleResult(
            oracle=self.name, probability=1.0 if strong else 0.5, confidence=confidence,
            polarity=OraclePolarity.SUPPORT, target=OracleTarget.RELEVANCE,
            directness=directness, source_family=self.source_family,
            note=f"evidence={','.join(sorted(kinds))}",
        )


class IntentOracle:
    """Task-intent-aware relevance (a pure-relevance oracle, NOT a Warden).

    Reads the query's task intent (`query_intent.classify_intent`) and the candidate's
    content role (`artifact_role.derive_content_role`) and emits a RELEVANCE result whose
    probability is the intent->role affinity (`intent_affinity`). It NEVER contradicts: a
    wrong-role-for-task hit is less *helpful*, not *unsafe* — so it lifts the relevance band
    of task-appropriate artifacts and lets the other oracles order within it. That is why
    intent is an oracle and gets no paired warden (only invariant dimensions —
    scope/currentness/anchoring — do).

    It joins the combiner as one more capped source family ("intent"); no combiner change.
    Lifecycle status stays owned by the temporal producer — the classifier only selects the
    temporal lens (see `intent_affinity.task_intents_to_lens`, wired at the recall entry
    point), never recomputing supersession here.

    Graduated into `default_oracles()` after the archolith-bench gate passed under a real
    embedder (text-embedding-3-small) AND a local model AND the lexical stub — identically,
    because the controlled corpus carries role in metadata, so intent's contribution is
    embedder-invariant (bench `d3811a2`)."""

    name = "intent"
    source_family = "intent"

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult:
        hits, confidence = classify_intent(query.text)
        if confidence is IntentConfidence.LOW:
            # Unclassifiable -> stay neutral, never distort baseline (missing != falsity).
            return OracleResult(
                oracle=self.name, probability=0.25, confidence=0.5,
                polarity=OraclePolarity.NEUTRAL, target=OracleTarget.RELEVANCE,
                source_family=self.source_family, note="low-confidence intent -> neutral",
            )
        intents: list[TaskIntent] = [h.intent for h in hits]
        roles = derive_content_role(candidate.metadata)
        aff, win_intent, win_role = resolve_affinity(intents, roles)
        weight = affinity_to_weight(aff)
        polarity = OraclePolarity.SUPPORT if weight > 0 else OraclePolarity.NEUTRAL
        return OracleResult(
            oracle=self.name, probability=weight, confidence=1.0,
            polarity=polarity, target=OracleTarget.RELEVANCE,
            source_family=self.source_family,
            note=f"{win_intent.value}:{win_role.value}={aff.value} w={weight:.2f}",
        )


def _s(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def default_oracles(semantic_scorer: SemanticScorer | None = None) -> list[RetrievalOracle]:
    """The cheap oracle set in deterministic priority order.

    Includes IntentOracle (graduated under a real embedder, bench `d3811a2`). It is a
    pure-RELEVANCE family, capped + independence-discounted by the combiner like any other,
    and returns NEUTRAL on low-confidence queries — so it never distorts a query that carries
    no task intent."""
    oracles: list[RetrievalOracle] = [
        SemanticOracle(semantic_scorer), StructureOracle(), ScopeOracle(),
        TemporalOracle(), IntentOracle(),
    ]
    # Experiment knob: MENHIR_FRONTIER_ORACLE_SUBSET=semantic,temporal restricts the active set
    # to isolate a single oracle's marginal effect (the ablation showed the full stack is
    # net-neutral; this lets us A/B one oracle without the others). Empty = all (default).
    subset = os.getenv("MENHIR_FRONTIER_ORACLE_SUBSET", "").strip()
    if subset:
        want = {s.strip().lower() for s in subset.split(",") if s.strip()}
        filtered = [o for o in oracles if getattr(o, "name", "") in want]
        if filtered:  # never return empty (would leave the combiner with no signal)
            oracles = filtered
    return oracles
