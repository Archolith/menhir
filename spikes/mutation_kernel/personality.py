"""Personality reference extension for the Menhir mutation spike.

The extension deliberately does not know about files, commits, tests, Graphiti, Neo4j, or MCP.
It demonstrates three kinds of derived state:

* traits: continuous tendencies (assertiveness, warmth, skepticism, ...);
* values/beliefs: continuous positions accumulated from experience/reflection;
* behavior policies: which reaction has produced the best outcomes in a situation.

All durable history remains in Incident + Assertion records.  Views are disposable and rebuildable.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

from .kernel import (
    Abstention,
    Assertion,
    EvidenceRef,
    View,
    current_assertions,
    effective_authority,
    make_assertion,
)


TRAIT_ASSERTION = "personality.trait"
VALUE_ASSERTION = "personality.value"
POLICY_ASSERTION = "personality.behavior_policy"

TRAIT_VIEW = "personality.trait_view"
VALUE_VIEW = "personality.value_view"
POLICY_VIEW = "personality.behavior_policy_view"


@dataclass(frozen=True)
class Incident:
    """One preserved experience and the reaction/outcome later personality folds may learn from."""

    incident_id: str
    subject_id: str
    scope: str
    occurred_at: str
    situation: str
    reaction: str
    outcome: str
    outcome_score: float
    reflection: str
    counterparty_id: str | None = None

    def __post_init__(self) -> None:
        if not -1.0 <= self.outcome_score <= 1.0:
            raise ValueError("Incident.outcome_score must be between -1 and 1")
        for name in ("incident_id", "subject_id", "scope", "occurred_at", "situation", "reaction"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"Incident.{name} must be non-empty")

    @property
    def evidence(self) -> EvidenceRef:
        return EvidenceRef(
            source_id=self.incident_id,
            source_kind="personality.incident",
            note=f"{self.situation}: {self.reaction} -> {self.outcome}",
        )


@dataclass(frozen=True)
class ContinuousSignal:
    """One extension-owned observation on a [-1, 1] axis."""

    score: float
    weight: float = 1.0
    explanation: str = ""

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError("ContinuousSignal.score must be between -1 and 1")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("ContinuousSignal.weight must be finite and > 0")


@dataclass(frozen=True)
class PolicySignal:
    """Observed outcome for choosing one reaction in a class of situations."""

    action: str
    outcome: float
    weight: float = 1.0
    explanation: str = ""

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("PolicySignal.action must be non-empty")
        if not -1.0 <= self.outcome <= 1.0:
            raise ValueError("PolicySignal.outcome must be between -1 and 1")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("PolicySignal.weight must be finite and > 0")


@dataclass(frozen=True)
class PolicyDecision:
    """The current preferred action plus transparent scores for all observed alternatives."""

    action: str
    action_scores: tuple[tuple[str, float], ...]
    evidence_counts: tuple[tuple[str, int], ...]


def trait_assertion(
    incident: Incident,
    *,
    trait: str,
    score: float,
    weight: float = 1.0,
    confidence: float = 1.0,
    authority: str = "agent",
    supersedes: Sequence[str] = (),
    interpreter_version: str = "personality-v1",
) -> Assertion:
    """Interpret an incident as evidence for a personality trait."""

    signal = ContinuousSignal(
        score=score,
        weight=weight,
        explanation=incident.reflection,
    )
    return make_assertion(
        source=incident.evidence,
        assertion_type=TRAIT_ASSERTION,
        subject_id=incident.subject_id,
        scope=incident.scope,
        key=trait,
        value=signal,
        valid_at=incident.occurred_at,
        authority=authority,
        confidence=confidence,
        supersedes=supersedes,
        interpreter_version=interpreter_version,
        metadata=(
            ("situation", incident.situation),
            ("reaction", incident.reaction),
        ),
    )


def value_assertion(
    incident: Incident,
    *,
    value_name: str,
    score: float,
    weight: float = 1.0,
    confidence: float = 1.0,
    authority: str = "agent",
    supersedes: Sequence[str] = (),
    interpreter_version: str = "personality-v1",
) -> Assertion:
    """Interpret an incident/reflection as evidence for an evolving value or belief."""

    signal = ContinuousSignal(
        score=score,
        weight=weight,
        explanation=incident.reflection,
    )
    return make_assertion(
        source=incident.evidence,
        assertion_type=VALUE_ASSERTION,
        subject_id=incident.subject_id,
        scope=incident.scope,
        key=value_name,
        value=signal,
        valid_at=incident.occurred_at,
        authority=authority,
        confidence=confidence,
        supersedes=supersedes,
        interpreter_version=interpreter_version,
        metadata=(
            ("situation", incident.situation),
            ("reaction", incident.reaction),
        ),
    )


def policy_assertion(
    incident: Incident,
    *,
    situation_class: str | None = None,
    action: str | None = None,
    confidence: float = 1.0,
    authority: str = "agent",
    supersedes: Sequence[str] = (),
    interpreter_version: str = "personality-v1",
) -> Assertion:
    """Record how a chosen reaction performed in one situation.

    The fold later compares this action against other reactions observed for the same situation
    class and scope.  No incident is deleted when the preferred action changes.
    """

    signal = PolicySignal(
        action=action or incident.reaction,
        outcome=incident.outcome_score,
        explanation=incident.reflection,
    )
    return make_assertion(
        source=incident.evidence,
        assertion_type=POLICY_ASSERTION,
        subject_id=incident.subject_id,
        scope=incident.scope,
        key=situation_class or incident.situation,
        value=signal,
        valid_at=incident.occurred_at,
        authority=authority,
        confidence=confidence,
        supersedes=supersedes,
        interpreter_version=interpreter_version,
        metadata=(("outcome", incident.outcome),),
    )


def _slot(
    assertions: Iterable[Assertion],
    *,
    assertion_type: str,
) -> list[Assertion]:
    rows = [row for row in current_assertions(assertions) if row.assertion_type == assertion_type]
    if not rows:
        return []
    expected = (rows[0].subject_id, rows[0].scope, rows[0].key)
    if any((row.subject_id, row.scope, row.key) != expected for row in rows):
        raise ValueError("fold input must contain exactly one subject/scope/key slot")
    return rows


def _continuous_fold(
    assertions: Iterable[Assertion],
    *,
    assertion_type: str,
    view_type: str,
    contested_threshold: float = 0.10,
) -> View | Abstention:
    rows = _slot(assertions, assertion_type=assertion_type)
    if not rows:
        return Abstention(view_type, "", "", "", "no_evidence")

    typed: list[tuple[Assertion, ContinuousSignal]] = []
    for row in rows:
        if not isinstance(row.value, ContinuousSignal):
            raise TypeError(f"{assertion_type} requires ContinuousSignal values")
        typed.append((row, row.value))

    weights = [signal.weight * row.confidence for row, signal in typed]
    mass = sum(weights)
    if mass <= 0:
        return Abstention(
            view_type,
            rows[0].subject_id,
            rows[0].scope,
            rows[0].key,
            "no_effective_evidence",
            tuple(row.assertion_id for row in rows),
        )

    score = sum(signal.score * weight for (_, signal), weight in zip(typed, weights)) / mass
    opposing = any(signal.score < 0 for _, signal in typed) and any(
        signal.score > 0 for _, signal in typed
    )
    if opposing and abs(score) < contested_threshold:
        return Abstention(
            view_type,
            rows[0].subject_id,
            rows[0].scope,
            rows[0].key,
            "contested",
            tuple(row.assertion_id for row in rows),
        )

    # Agreement shrinks as observations diverge from the folded center. Evidence mass increases
    # confidence asymptotically; neither creates new evidence or modifies stored assertions.
    dispersion = sum(
        abs(signal.score - score) * weight for (_, signal), weight in zip(typed, weights)
    ) / mass
    agreement = max(0.0, 1.0 - dispersion / 2.0)
    maturity = 1.0 - math.exp(-mass / 2.0)
    confidence = max(0.0, min(1.0, agreement * maturity))

    contributors = tuple(row.assertion_id for row in rows)
    counter = tuple(
        row.assertion_id
        for row, signal in typed
        if (score > 0 and signal.score < 0) or (score < 0 and signal.score > 0)
    )
    rationale = tuple(
        signal.explanation
        for _, signal in typed
        if signal.explanation
    )
    return View(
        view_type=view_type,
        subject_id=rows[0].subject_id,
        scope=rows[0].scope,
        key=rows[0].key,
        value=round(score, 6),
        confidence=round(confidence, 6),
        contributor_ids=contributors,
        counterevidence_ids=counter,
        effective_authority=effective_authority(rows),
        rationale=rationale,
    )


def fold_trait(assertions: Iterable[Assertion]) -> View | Abstention:
    return _continuous_fold(
        assertions,
        assertion_type=TRAIT_ASSERTION,
        view_type=TRAIT_VIEW,
    )


def fold_value(assertions: Iterable[Assertion]) -> View | Abstention:
    return _continuous_fold(
        assertions,
        assertion_type=VALUE_ASSERTION,
        view_type=VALUE_VIEW,
    )


def fold_policy(
    assertions: Iterable[Assertion],
    *,
    decision_margin: float = 0.15,
) -> View | Abstention:
    """Choose the reaction with the strongest accumulated positive outcome evidence."""

    rows = _slot(assertions, assertion_type=POLICY_ASSERTION)
    if not rows:
        return Abstention(POLICY_VIEW, "", "", "", "no_evidence")

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    contributors_by_action: dict[str, list[str]] = {}

    for row in rows:
        if not isinstance(row.value, PolicySignal):
            raise TypeError("personality.behavior_policy requires PolicySignal values")
        signal = row.value
        weighted = signal.outcome * signal.weight * row.confidence
        totals[signal.action] = totals.get(signal.action, 0.0) + weighted
        counts[signal.action] = counts.get(signal.action, 0) + 1
        contributors_by_action.setdefault(signal.action, []).append(row.assertion_id)

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    best_action, best_score = ranked[0]
    if best_score <= 0:
        return Abstention(
            POLICY_VIEW,
            rows[0].subject_id,
            rows[0].scope,
            rows[0].key,
            "no_positive_strategy",
            tuple(row.assertion_id for row in rows),
        )

    if len(ranked) > 1 and (best_score - ranked[1][1]) < decision_margin:
        return Abstention(
            POLICY_VIEW,
            rows[0].subject_id,
            rows[0].scope,
            rows[0].key,
            "contested",
            tuple(row.assertion_id for row in rows),
        )

    positive_mass = sum(max(0.0, value) for _, value in ranked)
    confidence = best_score / positive_mass if positive_mass > 0 else 0.0
    maturity = 1.0 - math.exp(-len(rows) / 2.0)
    confidence = max(0.0, min(1.0, confidence * maturity))

    decision = PolicyDecision(
        action=best_action,
        action_scores=tuple((name, round(score, 6)) for name, score in ranked),
        evidence_counts=tuple(sorted(counts.items())),
    )
    counter = tuple(
        row.assertion_id
        for row in rows
        if isinstance(row.value, PolicySignal) and row.value.action != best_action
    )
    return View(
        view_type=POLICY_VIEW,
        subject_id=rows[0].subject_id,
        scope=rows[0].scope,
        key=rows[0].key,
        value=decision,
        confidence=round(confidence, 6),
        contributor_ids=tuple(row.assertion_id for row in rows),
        counterevidence_ids=counter,
        effective_authority=effective_authority(rows),
        rationale=tuple(
            row.value.explanation
            for row in rows
            if isinstance(row.value, PolicySignal)
            and row.value.action == best_action
            and row.value.explanation
        ),
    )


def choose_scoped_policy(
    views: Iterable[View],
    *,
    person_id: str | None = None,
    group_ids: Sequence[str] = (),
) -> View | None:
    """Resolve behavior from most-specific learned scope to global.

    Person-specific policy outranks group policy, which outranks global policy.  If several group
    policies apply, confidence breaks the tie, then scope string for deterministic replay.

    This is mechanism only.  A real extension must separately define which group scopes are lawful
    to learn from and what admission evidence is required.
    """

    candidates = [view for view in views if view.view_type == POLICY_VIEW]
    by_scope: dict[str, list[View]] = {}
    for view in candidates:
        by_scope.setdefault(view.scope, []).append(view)

    scopes: list[str] = []
    if person_id:
        scopes.append(f"person:{person_id}")
    scopes.extend(f"group:{group_id}" for group_id in group_ids)
    scopes.append("global")

    for scope in scopes:
        matches = by_scope.get(scope, [])
        if matches:
            return sorted(matches, key=lambda view: (-view.confidence, view.key, view.scope))[0]
    return None
