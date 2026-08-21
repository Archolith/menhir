"""Event-history recall — the pure, deterministic classifier + selector that turns a natural-language
recall query into a latest/predecessor event CANDIDATE SELECTION (Phase 4).

Generic and offline-only: it wires up nothing (no settings/flag, runtime, endpoint, repository/View
write, telemetry, or LLM) and reasons only over in-memory ``TypedEventAssertion`` objects via the
pure domain ``select_event_assertion``. No benchmark ids, no fixture-specific wording. It produces a
deterministic candidate; evidence/foundation authority is a later layer and lives elsewhere.

Everything is fail-closed and conservative. A query classifies as a completed-acquisition
latest/predecessor route only on an exact conservative surface form; malformed/misspelled/unrelated
syntax abstains. Selection never uses ``learned_at`` or input order — only ``valid_at`` (world/source
time) decides, and ``selected`` is populated only on unique success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from menhir.domain.event_history import (
    EventLane,
    EventSelectionIntent,
    SelectionStatus,
    TypedEventAssertion,
    normalize_text as _norm,
    select_event_assertion,
)
from menhir.domain.temporal import parse_iso8601

#: The single canonical predicate for completed-acquisition semantics (mirrors perception).
CANONICAL_PREDICATE_ACQUIRED = "acquired"

#: Conservative surface verbs marking a completed acquisition, including gerund inflections. This is
#: a generic closed allowlist (never a prefix/synonym matcher), word-boundary matched.
_ACQUISITION_VERBS = (
    "buy", "bought", "buying", "purchase", "purchased", "purchasing",
    "get", "got", "getting", "acquire", "acquired", "acquiring",
)
_VERB_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in _ACQUISITION_VERBS) + r")\b", re.IGNORECASE)

#: Gerund inflections that can follow a predecessor cue ("before getting the blender") and are
#: stripped from the anchor so the anchor resolves to the object, not the event phrase.
_GERUND_INFLECTIONS = frozenset({"buying", "purchasing", "getting", "acquiring"})

#: Conservative recency cues. Word-boundary matched; ``most recent`` covers ``most recently``.
_RECENCY_RE = re.compile(r"\b(?:latest|newest|most\s+recent(?:ly)?)\b", re.IGNORECASE)

#: Predecessor cues, longest first so ``immediately before``/``right before`` win over ``before``.
_PREDECESSOR_RE = re.compile(
    r"\b(?:immediately\s+before|right\s+before|prior\s+to|before)\b", re.IGNORECASE)

#: Articles/possessive determiners stripped from the head of a normalized anchor.
_ANCHOR_DETERMINERS = re.compile(r"^(?:a|an|the|my)\s+", re.IGNORECASE)

#: Generic noun heads (and their plurals) that yield the closed generic/advisory route with NO scope
#: token — the query asks for "a thing/item/..." with no narrowing head.
_GENERIC_HEADS = frozenset({
    "thing", "item", "object", "gadget", "equipment", "device", "stuff", "tool",
    "product", "accessory", "supply", "gear",
    "things", "items", "objects", "gadgets", "devices", "tools", "products",
    "accessories", "supplies",
})


def _last_word(phrase: str) -> str | None:
    """The terminal (last) whitespace-delimited token of a noun phrase, punctuation stripped."""
    tokens = re.findall(r"\S+", phrase or "")
    if not tokens:
        return None
    return tokens[-1].strip(".,!?;:'\"")


def _strip_anchor_determiners(anchor: str) -> str:
    """Strip a leading gerund inflection ("before getting the blender" -> "the blender"), then any
    leading a/an/the/my and trailing query punctuation, then normalize whitespace. Deliberately
    generic, not task-specific."""
    text = _norm(anchor)
    while True:
        first = re.split(r"\s+", text, maxsplit=1)[0]
        if first in _GERUND_INFLECTIONS:
            rest = re.split(r"\s+", text, maxsplit=1)
            text = _norm(rest[1] if len(rest) == 2 else "")
            continue
        match = _ANCHOR_DETERMINERS.match(text)
        if not match:
            break
        text = _norm(text[match.end():])
    return text.rstrip(".,!?;:'\"")


def _has_whole_token(object_key: str, token: str) -> bool:
    """True when ``token`` appears in ``object_key`` as a whole word (no word chars adjacent)."""
    if not token:
        return False
    return re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", _norm(object_key)) is not None


# --------------------------------------------------------------------------- query classification


class EventQueryRouteKind(str, Enum):
    """Closed route a recall query classified into. Anything not matching a conservative form is
    ``ABSTAIN`` (never a guessed semantic default)."""

    LATEST = "latest"              # which/type/kind-of <NP> did ... <verb> ... <recency>
    PREDECESSOR = "predecessor"    # ... <verb> ... <predecessor-cue> <anchor>
    GENERIC_ADVISORY = "generic_advisory"  # generic head -> advisory, no scope token
    ABSTAIN = "abstain"            # malformed / misspelled / unrelated / unknown


@dataclass(frozen=True)
class EventQueryRoute:
    """Immutable classification of one recall query. ``scope_token`` is the terminal head of the
    noun phrase (None for the generic/advisory route); ``anchor_token`` is the normalized predecessor
    anchor (None otherwise). ``noun_phrase`` is the normalized noun phrase captured only for the
    conservative latest forms (including the generic/advisory route), None otherwise. ``predicate``
    is the canonical predicate the route targets."""

    kind: EventQueryRouteKind
    predicate: str = CANONICAL_PREDICATE_ACQUIRED
    scope_token: str | None = None
    anchor_token: str | None = None
    noun_phrase: str | None = None
    reason: str = ""


def _classify_predecessor(text: str) -> EventQueryRoute | None:
    """Classify a predecessor query, or None to fall through to the latest classifier. Requires the
    query to begin with ``what``/``which`` AND an acquisition verb PLUS a predecessor cue with a
    nonblank normalized anchor following it."""
    if not (text.startswith("what ") or text.startswith("which ")):
        return None
    if not _VERB_RE.search(text):
        return None
    cue = _PREDECESSOR_RE.search(text)
    if cue is None:
        return None
    anchor = _strip_anchor_determiners(text[cue.end():])
    if not anchor:
        return EventQueryRoute(
            EventQueryRouteKind.ABSTAIN, reason="predecessor cue with no nonblank anchor")
    return EventQueryRoute(
        EventQueryRouteKind.PREDECESSOR, anchor_token=anchor, reason="predecessor classified")


def _classify_latest(text: str) -> EventQueryRoute:
    """Classify a latest query from one of the two conservative forms:
    ``What type/kind of <NP> did ... <verb> ... <recency>`` or ``Which <NP> did ... <verb> ...
    <recency>``. The terminal head token of <NP> becomes the scope token unless it is generic.
    Acquisition verbs are searched ONLY in the clause after the literal `` did ``, never inside the
    noun phrase, so a verb inside the NP (e.g. a gerund in ``Which buying guide ...``) cannot satisfy
    the route."""
    np_rest: str | None = None
    for prefix in ("what type of", "what kind of"):
        if text.startswith(prefix):
            np_rest = text[len(prefix):]
            break
    if np_rest is None and text.startswith("which "):
        np_rest = text[len("which "):]
    if np_rest is None:
        return EventQueryRoute(EventQueryRouteKind.ABSTAIN, reason="not a conservative latest form")
    if " did " not in text:
        return EventQueryRoute(EventQueryRouteKind.ABSTAIN, reason="latest form requires ' did '")
    np, _, did_clause = np_rest.partition(" did ")
    np = np.strip()
    head = _last_word(np)
    if not head or not _VERB_RE.search(did_clause) or not _RECENCY_RE.search(text):
        return EventQueryRoute(EventQueryRouteKind.ABSTAIN, reason="missing verb/recency/did")
    if head in _GENERIC_HEADS:
        return EventQueryRoute(
            EventQueryRouteKind.GENERIC_ADVISORY, noun_phrase=np,
            reason=f"generic head {head!r}; advisory")
    return EventQueryRoute(
        EventQueryRouteKind.LATEST, scope_token=head, noun_phrase=np, reason="latest classified")


def classify_event_query(query: str | None) -> EventQueryRoute:
    """Classify a natural-language recall query into a closed, fail-closed ``EventQueryRoute``."""
    text = _norm(query)
    if not text:
        return EventQueryRoute(EventQueryRouteKind.ABSTAIN, reason="blank query")
    predecessor = _classify_predecessor(text)
    if predecessor is not None:
        return predecessor
    return _classify_latest(text)


# --------------------------------------------------------------------------- pure scoping/anchor


class ScopeStatus(str, Enum):
    """Closed outcome of whole-token scope application."""

    APPLIED = "applied"
    INSUFFICIENT_SUPPORT = "insufficient_support"


@dataclass(frozen=True)
class ScopeOutcome:
    """Immutable result of ``apply_scope``. On ``INSUFFICIENT_SUPPORT``, ``assertions`` are the
    UNCHANGED input and ``reason`` is the closed explanation; on ``APPLIED`` they are the filtered
    subset whose object_keys match the token."""

    status: ScopeStatus
    assertions: tuple[TypedEventAssertion, ...]
    matched_object_keys: tuple[str, ...] = ()
    reason: str = ""


def apply_scope(
    assertions: Iterable[TypedEventAssertion], *, scope_token: str,
) -> ScopeOutcome:
    """Pure whole-token scope helper. Scope applies ONLY when at least 2 DISTINCT ``object_key``s
    match ``scope_token`` as a whole token; under 2 it returns the unchanged assertions plus the
    closed ``insufficient_support`` reason (so a caller can fall back instead of guessing)."""
    assertions = tuple(assertions)
    token = _norm(scope_token)
    distinct: dict[str, TypedEventAssertion] = {}
    for a in assertions:
        if _has_whole_token(a.object_key, token):
            distinct.setdefault(_norm(a.object_key), a)
    if len(distinct) < 2:
        return ScopeOutcome(
            ScopeStatus.INSUFFICIENT_SUPPORT, assertions,
            reason=(
                f"scope requires at least 2 DISTINCT object_keys matching {token!r}; got "
                f"{len(distinct)}"
            ),
        )
    kept = tuple(a for a in assertions if _has_whole_token(a.object_key, token))
    return ScopeOutcome(
        ScopeStatus.APPLIED, kept, tuple(sorted(distinct)), reason="scope applied")


def resolve_exact_anchor(
    assertions: Iterable[TypedEventAssertion], *, anchor_token: str,
) -> TypedEventAssertion | None:
    """Pure exact-anchor helper. An anchor resolves ONLY when exactly one distinct ``assertion_key``
    matches ``anchor_token`` by normalized exact ``object_key`` equality; exact-replay duplicates
    (same ``assertion_key``) collapse to one. Zero or more than one distinct match ABSTAINS (None)."""
    token = _norm(anchor_token)
    distinct: dict[str, TypedEventAssertion] = {}
    for a in assertions:
        if _norm(a.object_key) == token:
            distinct.setdefault(a.assertion_key, a)
    if len(distinct) != 1:
        return None
    return next(iter(distinct.values()))


# --------------------------------------------------------------------------- pure selection


class EventRecallStatus(str, Enum):
    """Fail-closed outcome of an event recall selection."""

    UNIQUE = "unique"                  # one assertion uniquely selected on the winning valid_at
    NONE = "none"                      # no eligible candidate / no unique winner in any domain
    AMBIGUOUS = "ambiguous"            # distinct candidates tied at the winning valid_at
    INSUFFICIENT_SCOPE = "insufficient_scope"  # scope token matched <2 distinct object_keys
    NO_ANCHOR = "no_anchor"            # predecessor anchor absent or not uniquely resolved
    UNCLASSIFIED = "unclassified"      # query abstained / route not usable


class EventRecallGate(str, Enum):
    """Closed fail-closed gates for an event recall selection."""

    ROUTE = "route"            # route-level failure (abstain / unusable)
    SCOPE = "scope"            # scope token matched <2 distinct object_keys
    ANCHOR = "anchor"          # predecessor anchor absent or not uniquely resolved
    AMBIGUITY = "ambiguity"    # distinct candidates tied at the winning valid_at
    TIME = "time"              # no winning candidate with a parseable valid_at
    NO_CANDIDATE = "no_candidate"  # no eligible candidate / no unique winner in any domain
    PASS = "pass"              # unique winner selected


@dataclass(frozen=True)
class EventRecallSelection:
    """Immutable selection result. ``selected`` is populated ONLY on unique success; ``domain`` is
    the winning domain lane; ``advisory`` marks a generic-head route with no scope token."""

    status: EventRecallStatus
    selected: TypedEventAssertion | None = None
    route: EventQueryRoute | None = None
    domain: str | None = None
    advisory: bool = False
    gate: EventRecallGate | None = None
    reason: str = ""

    @property
    def has_unique_selection(self) -> bool:
        return self.status is EventRecallStatus.UNIQUE


def _select_lane_winners(
    assertions: list[TypedEventAssertion], *, route: EventQueryRoute,
    namespace: str | None, subject_uuid: str, as_of: str | None,
    anchor_time: str | None,
) -> tuple[list[tuple[str | None, TypedEventAssertion]], bool]:
    """Run ``select_event_assertion`` once per actual-domain lane over the exact-filtered assertions.

    Returns ``(unique_winners, any_ambiguous)``. An empty/invalid lane contributes no winner. ANY
    in-scope lane ambiguity at a potentially winning time is surfaced so the caller fails closed
    overall (a separate lane must never win over an ambiguous one). For a predecessor route,
    ``anchor_time`` (the resolved anchor's ``valid_at``) applies to EVERY lane, so the predecessor is
    found across domains, not just inside the anchor's own lane. ``learned_at``/input order never used."""
    groups: dict[str | None, list[TypedEventAssertion]] = {}
    for a in assertions:
        groups.setdefault(_norm(a.domain), []).append(a)
    if route.kind is EventQueryRouteKind.PREDECESSOR:
        intent = EventSelectionIntent.PREDECESSOR
    else:
        intent = EventSelectionIntent.LATEST
    winners: list[tuple[str | None, TypedEventAssertion]] = []
    any_ambiguous = False
    for domain, members in groups.items():
        lane = EventLane(
            subject_uuid=subject_uuid, predicate=route.predicate,
            namespace=namespace, domain=members[0].domain,
        )
        sel = select_event_assertion(
            members, lane=lane, intent=intent, as_of=as_of, anchor_time=anchor_time,
        )
        if sel.status is SelectionStatus.UNIQUE and sel.selected is not None:
            winners.append((domain, sel.selected))
        elif sel.status is SelectionStatus.AMBIGUOUS:
            any_ambiguous = True
    return winners, any_ambiguous


def _combine_domain_winners(
    winners: list[tuple[str | None, TypedEventAssertion]], *,
    route: EventQueryRoute, advisory: bool,
) -> EventRecallSelection:
    """Combine unique domain winners by ``valid_at`` only. Invalid time cannot win; distinct
    candidates tied at the winning valid_at are ambiguous; ``learned_at``/input order are never used."""
    if not winners:
        return EventRecallSelection(
            EventRecallStatus.NONE, route=route, advisory=advisory,
            gate=EventRecallGate.NO_CANDIDATE, reason="no unique winner in any domain lane",
        )
    parsed: list[tuple[str | None, TypedEventAssertion, object]] = []
    for domain, assertion in winners:
        dt = parse_iso8601(assertion.valid_at)
        if dt is not None:
            parsed.append((domain, assertion, dt))
    if not parsed:
        return EventRecallSelection(
            EventRecallStatus.NONE, route=route, advisory=advisory,
            gate=EventRecallGate.TIME, reason="no winning candidate has a parseable valid_at",
        )
    best = max(dt for _, _, dt in parsed)
    best_winners = [(domain, a) for domain, a, dt in parsed if dt == best]
    if len(best_winners) > 1:
        return EventRecallSelection(
            EventRecallStatus.AMBIGUOUS, route=route, advisory=advisory,
            gate=EventRecallGate.AMBIGUITY, reason=f"distinct candidates tied at winning valid_at {best}",
        )
    domain, selected = best_winners[0]
    return EventRecallSelection(
        EventRecallStatus.UNIQUE, selected=selected, route=route,
        domain=domain, advisory=advisory, gate=EventRecallGate.PASS, reason="unique winner selected",
    )


def select_event_recall(
    assertions: Iterable[TypedEventAssertion], *,
    route: EventQueryRoute, subject_uuid: str, namespace: str | None = None,
    as_of: str | None = None,
) -> EventRecallSelection:
    """Pure selector over in-memory assertions for one classified ``route``.

    Exactly filters ``namespace + subject_uuid + predicate (acquired)``, applies the route, groups by
    actual domain, calls the existing ``select_event_assertion`` per ``EventLane``, and combines the
    unique domain winners by ``valid_at`` only. Authority never uses ``learned_at``/input order, and
    ``    selected`` is populated only on unique success.
    """
    if route.kind is EventQueryRouteKind.ABSTAIN:
        return EventRecallSelection(
            EventRecallStatus.UNCLASSIFIED, route=route,
            gate=EventRecallGate.ROUTE, reason="query route abstained",
        )
    predicate = _norm(route.predicate) or CANONICAL_PREDICATE_ACQUIRED
    filtered = [
        a for a in assertions
        if _norm(a.namespace) == _norm(namespace)
        and _norm(a.subject_uuid) == _norm(subject_uuid)
        and _norm(a.predicate) == predicate
    ]
    advisory = route.kind is EventQueryRouteKind.GENERIC_ADVISORY

    if route.kind is EventQueryRouteKind.PREDECESSOR:
        if not route.anchor_token:
            return EventRecallSelection(
                EventRecallStatus.NO_ANCHOR, route=route,
                gate=EventRecallGate.ANCHOR, reason="predecessor route requires a non-blank anchor",
            )
        anchor = resolve_exact_anchor(filtered, anchor_token=route.anchor_token)
        if anchor is None:
            return EventRecallSelection(
                EventRecallStatus.NO_ANCHOR, route=route,
                gate=EventRecallGate.ANCHOR, reason="anchor not uniquely resolved by assertion_key",
            )
        winners = _select_lane_winners(
            filtered, route=route, namespace=namespace, subject_uuid=subject_uuid,
            as_of=None, anchor_time=anchor.valid_at,
        )
        if winners[1]:
            return EventRecallSelection(
                EventRecallStatus.AMBIGUOUS, route=route, advisory=advisory,
                gate=EventRecallGate.AMBIGUITY, reason="an in-scope domain lane is ambiguous",
            )
        return _combine_domain_winners(winners[0], route=route, advisory=advisory)

    if route.kind is EventQueryRouteKind.LATEST and route.scope_token:
        outcome = apply_scope(filtered, scope_token=route.scope_token)
        if outcome.status is ScopeStatus.INSUFFICIENT_SUPPORT:
            return EventRecallSelection(
                EventRecallStatus.INSUFFICIENT_SCOPE, route=route,
                gate=EventRecallGate.SCOPE, reason=outcome.reason,
            )
        working = outcome.assertions
    else:
        working = tuple(filtered)

    winners = _select_lane_winners(
        list(working), route=route, namespace=namespace, subject_uuid=subject_uuid,
        as_of=as_of, anchor_time=None,
    )
    if winners[1]:
        return EventRecallSelection(
            EventRecallStatus.AMBIGUOUS, route=route, advisory=advisory,
            gate=EventRecallGate.AMBIGUITY, reason="an in-scope domain lane is ambiguous",
        )
    return _combine_domain_winners(winners[0], route=route, advisory=advisory)
