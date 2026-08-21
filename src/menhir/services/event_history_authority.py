"""Event-history authority — the pure, offline advisory verdict layer for first-person event recall.

Generic and offline-only: it wires up nothing (no repository, settings/flag, runtime, endpoint,
telemetry, or LLM) and reasons only over in-memory ``TypedEventAssertion`` objects via the existing
pure classifier + selector in ``menhir.services.event_history_recall``. No benchmark ids, no
fixture-specific wording.

A lead is claimed ONLY for a conservative explicit first-person ``did I`` query (case/whitespace
tolerant) that yields a unique, NON-generic selection whose foundation is verified and whose exact
quote/evidence identity is nonblank. A separate explicit ``before I bought/purchased/got/acquired``
guard may return an anchor advisory for broader temporal questions, but it never selects or leads an
answer. Other non-first-person queries and route abstentions return ``None``. Everything else —
generic routes, unverified foundation, ambiguous/empty selection — returns a structured advisory
``EventAuthorityVerdict``. ``learned_at`` is never used: authority rests on ``valid_at`` plus
grounding identity.
"""

from __future__ import annotations

import re
from typing import Iterable

from menhir.domain.event_history import TypedEventAssertion, normalize_text as _norm
from menhir.domain.recall import EventAuthorityVerdict
from menhir.services.event_history_recall import (
    CANONICAL_PREDICATE_ACQUIRED,
    EventQueryRoute,
    EventQueryRouteKind,
    EventRecallStatus,
    classify_event_query,
    resolve_exact_anchor,
    select_event_recall,
)

#: A guard for explicit completed-acquisition anchors in temporal questions that are intentionally
#: broader than the narrow latest/predecessor answer selector. It does not classify an answer route
#: or select an event; it only prevents ordinary memories from masquerading as an answer when the
#: query's stated anchor is absent or ambiguous. Clause punctuation bounds the object phrase.
_EXPLICIT_ANCHOR_GUARD_RE = re.compile(
    r"(?:^|[.!?]\s+)before\s+i\s+(?:bought|purchased|got|acquired)\s+"
    r"(?P<anchor>[^,;.!?]+)",
    re.IGNORECASE,
)
_ANCHOR_GUARD_DETERMINER_RE = re.compile(r"^(?:a|an|the|my)\s+", re.IGNORECASE)


def _nonblank(value: str | None) -> bool:
    return bool((value or "").strip())


def _is_first_person(query: str | None) -> bool:
    """True only when the classified surface's first `` did `` clause boundary is immediately
    followed by the subject ``i`` (first person). Nested/quoted/attributed ``did I`` — e.g. a
    quote after a third-person main clause — is rejected because the boundary is not first person."""
    text = _norm(query)
    boundary = text.find(" did ")
    if boundary < 0:
        return False
    after = text[boundary + len(" did "):]
    return re.match(r"\bi\b", after) is not None


def _explicit_anchor_guard_token(query: str | None) -> str | None:
    """Return a normalized explicit acquisition anchor for a broader temporal guard query.

    The surface must literally state that the first-person speaker completed an acquisition before
    some object (``before I bought/purchased/got/acquired X``). Future/hypothetical verbs and
    third-party clauses do not match. This helper never infers an answer route.
    """
    match = _EXPLICIT_ANCHOR_GUARD_RE.search(_norm(query))
    if match is None:
        return None
    anchor = _norm(match.group("anchor"))
    anchor = _ANCHOR_GUARD_DETERMINER_RE.sub("", anchor, count=1)
    return anchor.strip() or None


def _unresolved_explicit_anchor_verdict(
    query: str | None,
    assertions: Iterable[TypedEventAssertion],
    *,
    subject_uuid: str,
    namespace: str | None,
) -> EventAuthorityVerdict | None:
    """Fail closed for an explicit acquisition anchor outside the answer-selection grammar.

    A uniquely grounded anchor means the guard has nothing to say and ordinary recall remains
    unchanged. Zero or multiple distinct assertion identities produce an advisory with no selected
    object. Exact object-key equality, namespace, subject, and predicate are required.
    """
    anchor = _explicit_anchor_guard_token(query)
    if anchor is None:
        return None
    eligible = tuple(
        assertion
        for assertion in assertions
        if _norm(assertion.namespace) == _norm(namespace)
        and _norm(assertion.subject_uuid) == _norm(subject_uuid)
        and _norm(assertion.predicate) == CANONICAL_PREDICATE_ACQUIRED
    )
    if resolve_exact_anchor(eligible, anchor_token=anchor) is not None:
        return None
    return EventAuthorityVerdict(
        predicate=CANONICAL_PREDICATE_ACQUIRED,
        object_key=None,
        object_display=None,
        valid_at=None,
        stated_span=None,
        assertion_key=None,
        episode_uuid=None,
        turn_evidence_uuid=None,
        domain=None,
        time_basis=None,
        status="advisory",
        gate="anchor",
        reason=f"explicit temporal anchor {anchor!r} was not uniquely resolved",
        subject_uuid=subject_uuid,
        has_foundation=False,
        kind="anchor_guard",
    )


def _evidence_identity_ok(selected: TypedEventAssertion) -> bool:
    """True when the selected assertion carries an exact nonblank quote (stated_span) AND a nonblank
    evidence identity (episode_uuid or turn_evidence_uuid). Grounding/evidence, never ``learned_at``."""
    return _nonblank(selected.stated_span) and (
        _nonblank(selected.episode_uuid) or _nonblank(selected.turn_evidence_uuid)
    )


def _verdict(
    *, route: EventQueryRoute, subject_uuid: str, has_foundation: bool,
    status: str, gate: str, reason: str,
    selected: TypedEventAssertion | None,
) -> EventAuthorityVerdict:
    """Build an immutable verdict; selected-grounding fields are None when a gate failed."""
    return EventAuthorityVerdict(
        predicate=route.predicate,
        object_key=selected.object_key if selected else None,
        object_display=selected.object_display if selected else None,
        valid_at=selected.valid_at if selected else None,
        stated_span=selected.stated_span if selected else None,
        assertion_key=selected.assertion_key if selected else None,
        episode_uuid=selected.episode_uuid if selected else None,
        turn_evidence_uuid=selected.turn_evidence_uuid if selected else None,
        domain=selected.domain if selected else None,
        time_basis=selected.time_basis if selected else None,
        status=status,
        gate=gate,
        reason=reason,
        subject_uuid=subject_uuid,
        has_foundation=has_foundation,
        kind=route.kind.value,
    )


def event_authority_for_query(
    query: str | None,
    assertions: Iterable[TypedEventAssertion],
    *,
    subject_uuid: str,
    namespace: str | None = None,
    foundation_verified: bool,
) -> EventAuthorityVerdict | None:
    """Return a structured advisory/lead verdict for a first-person event recall query, or None for
    abstention. A broader explicit completed-acquisition anchor may produce an anchor-only advisory;
    it never produces a selected object or lead.

    Delegates to the existing ``classify_event_query`` + ``select_event_recall``. A lead is produced
    only for a unique, non-generic selection with ``foundation_verified=True`` and an exact nonblank
    quote/evidence identity; generic routes and unverified foundations remain advisory.
    """
    route = classify_event_query(query)
    if route.kind is EventQueryRouteKind.ABSTAIN:
        return _unresolved_explicit_anchor_verdict(
            query, assertions, subject_uuid=subject_uuid, namespace=namespace)
    if not _is_first_person(query):
        return None

    selection = select_event_recall(
        assertions, route=route, subject_uuid=subject_uuid, namespace=namespace)

    if selection.status is not EventRecallStatus.UNIQUE:
        gate = selection.gate.value if selection.gate is not None else "no_candidate"
        return _verdict(
            route=route, subject_uuid=subject_uuid, has_foundation=False,
            status="advisory", gate=gate, reason=selection.reason, selected=None,
        )

    assert selection.selected is not None
    selected = selection.selected

    if selection.advisory:
        return _verdict(
            route=route, subject_uuid=subject_uuid, has_foundation=foundation_verified,
            status="advisory", gate="route",
            reason="generic route stays advisory (no scope token)", selected=selected,
        )
    if not foundation_verified:
        return _verdict(
            route=route, subject_uuid=subject_uuid, has_foundation=False,
            status="advisory", gate="foundation",
            reason="foundation not verified; advisory", selected=selected,
        )
    if not _evidence_identity_ok(selected):
        return _verdict(
            route=route, subject_uuid=subject_uuid, has_foundation=True,
            status="advisory", gate="evidence",
            reason="quote/evidence identity not exact and nonblank; advisory", selected=selected,
        )
    return _verdict(
        route=route, subject_uuid=subject_uuid, has_foundation=True,
        status="leads", gate="pass",
        reason="unique non-generic selection with verified foundation", selected=selected,
    )
