"""Provenance-linked View suppression planner (Step 7b) -- pure, offline.

Given the recall query's temporal intent and the DETERMINISTIC provenance rows linking a current
scalar_state View to the source episode of the superseded predecessor it replaced, decide which
recall candidates a current View is authorised to suppress. Every decision goes through the pure
six-gate :func:`menhir.domain.scalar_view_authority.decide_view_authority`; nothing here relaxes it.

The suppression source is provenance-linked ONLY (owner decision): a candidate may be suppressed
solely when it is the source episode of a superseded predecessor assertion whose current sibling
carries a current View for the same slot. No fuzzy text/slot matching -- a candidate with no such
provenance edge is never touched.

Intent mapping is conservative: authority applies ONLY to a query with an EXPLICIT current cue.
A bare query with no temporal cue is AMBIGUOUS -> advisory (never suppresses).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from menhir.domain.scalar_view_authority import (
    AuthorityQuery,
    OverlappingFact,
    QueryIntent,
    ViewCandidate,
    decide_view_authority,
)
from menhir.domain.temporal import TemporalQuery
from menhir.domain.temporal_intent import classify_temporal_intent

__all__ = ["authority_query_intent", "SuppressionDecision", "SuppressionPlan", "plan_view_suppression"]


def authority_query_intent(query_text: str) -> QueryIntent:
    """Map a query to the authority intent via the deterministic temporal lens (conservative).

    CURRENT_STATE only when an EXPLICIT current cue matched (now/currently/today/...); a history
    cue -> PREVIOUS_VALUE; an as-of world-time query -> COMPARISON; a bare default -> AMBIGUOUS.
    Every non-current intent keeps the View advisory (gate 1 refuses suppression)."""
    ti = classify_temporal_intent(query_text or "")
    if ti.query is TemporalQuery.AS_KNOWN_AT:
        return QueryIntent.PREVIOUS_VALUE
    if ti.query is TemporalQuery.AS_OF_WORLD:
        return QueryIntent.COMPARISON
    if ti.query is TemporalQuery.CURRENT_BELIEF and ti.cue is not None:
        return QueryIntent.CURRENT_STATE
    return QueryIntent.AMBIGUOUS


@dataclass(frozen=True)
class SuppressionDecision:
    candidate_uuid: str
    slot_attribute: str
    outcome: str   # "suppress" | "advisory"
    gate: str
    reason: str


@dataclass(frozen=True)
class SuppressionPlan:
    intent: QueryIntent
    suppressed_uuids: frozenset[str]
    decisions: tuple[SuppressionDecision, ...] = field(default_factory=tuple)

    @property
    def any_suppressed(self) -> bool:
        return bool(self.suppressed_uuids)


def _view_from_row(row: Mapping[str, Any], subject_uuid: str) -> ViewCandidate:
    return ViewCandidate(
        subject_uuid=subject_uuid,
        attribute=str(row.get("attribute") or ""),
        scope=str(row.get("scope") or ""),
        value_kind=str(row.get("value_kind") or ""),
        unit=str(row.get("unit") or ""),
        value=row.get("view_value"),
        evidence_tier=row.get("evidence_tier"),
        view_current=bool(row.get("view_current")),
        is_sole_current=bool(row.get("is_sole_current")),
        valid_at=row.get("view_valid_at"),
    )


def _fact_from_row(row: Mapping[str, Any], subject_uuid: str) -> OverlappingFact:
    return OverlappingFact(
        subject_uuid=subject_uuid,
        attribute=str(row.get("attribute") or ""),
        scope=str(row.get("scope") or ""),
        value=row.get("stale_value"),
        valid_at=row.get("stale_valid_at"),
    )


def plan_view_suppression(
    query_text: str,
    provenance_rows: Iterable[Mapping[str, Any]],
    candidate_uuids: Iterable[str],
) -> SuppressionPlan:
    """Decide which candidate uuids a current View may suppress. Pure; never raises.

    ``provenance_rows`` each describe one superseded predecessor: {candidate_uuid (its source
    episode), subject_uuid, attribute, scope, value_kind, unit, view_value, view_valid_at,
    evidence_tier, view_current, is_sole_current, stale_value, stale_valid_at}. Only rows whose
    ``candidate_uuid`` is actually present in ``candidate_uuids`` are considered.
    """
    intent = authority_query_intent(query_text)
    present = {str(u) for u in candidate_uuids}
    query = AuthorityQuery(intent=intent, subject_uuid=None)  # subject filled per-row below

    suppressed: set[str] = set()
    decisions: list[SuppressionDecision] = []
    for row in provenance_rows:
        cand = str(row.get("candidate_uuid") or "")
        if not cand or cand not in present:
            continue
        subject_uuid = str(row.get("subject_uuid") or "")
        q = AuthorityQuery(intent=intent, subject_uuid=subject_uuid)
        view = _view_from_row(row, subject_uuid)
        fact = _fact_from_row(row, subject_uuid)
        d = decide_view_authority(q, view, fact)
        decisions.append(SuppressionDecision(
            candidate_uuid=cand, slot_attribute=view.attribute,
            outcome=d.outcome.value, gate=d.gate, reason=d.reason,
        ))
        if d.suppress:
            suppressed.add(cand)

    return SuppressionPlan(
        intent=intent, suppressed_uuids=frozenset(suppressed), decisions=tuple(decisions),
    )
