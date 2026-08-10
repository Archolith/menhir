"""Event-history domain — the pure contract + selector for grounded categorical occurrences.

Menhir already has ``Event -> Fold -> View`` and ``TimelineKind``; this module does NOT add another
View or store. It is Phase 1 of the approved event-history work: the immutable domain contract and a
deterministic, pure selector that later phases persist and project through the existing timeline
View. This slice only reasons over in-memory assertions.

## Identity (three levels, mirroring ``TypedAssertion``)

* ``source_key`` — BINDING-STABLE, SOURCE-GROUNDED locator: ``episode_uuid + span_start + span_end +
  claim_ordinal``. Contains NO extracted semantics, so a newer perceiver can correct the predicate /
  object on the same source span and still supersede via the same head. Deliberately excludes
  ``subject_uuid`` (invariant under merge rebinding) AND ``learned_at`` (ingest time is not source
  grounding). Reuses ``typed_assertion.build_source_key`` so the two source identities cannot drift.
* ``assertion_key`` — fully-interpreted identity: ``source_key + perceiver_version + namespace +
  predicate + object_key + domain + valid_at + time_basis``. Exact replay of the same interpreted
  occurrence yields the same key (this is what dedup keys on); a corrected world time on the same
  span is a NEW assertion, not a replay. ``subject_uuid``/``object_uuid`` are NOT part of it, so a
  merge re-binding of either UUID does not fork the interpreted assertion (mirrors scalar
  binding-stable identity).
* ``lane`` (``EventLane``) — the fold/selection lane: ``namespace + subject_uuid + predicate +
  domain`` (normalized). This is exactly what ``select_event_assertion`` filters to.

Authority is never derived from ``learned_at`` or input order; a valid_at tie between DISTINCT
candidates fails closed as ambiguous/advisory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import (
    EVIDENCE_TIERS,
    TIME_BASES,
    build_source_key,
)


def _norm(value: str | None) -> str:
    """Consistent normalization for textual identity components: blank-tolerant, trimmed, lowercased."""
    return (value or "").strip().lower()


#: Sentinel for an unparseable ``learned_at``: sorts AFTER any parseable time (UTC-aware so it is
#: comparable with ``parse_iso8601`` output), so a replay with no usable ingest stamp never beats a
#: parseable one when choosing a representative.
_LEARNED_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _rep_learned_key(e: "TypedEventAssertion") -> datetime:
    """Deterministic representative key for an EXACT replay (identical ``assertion_key``). Uses
    ``learned_at`` ONLY to pick WHICH identical-occurrence copy represents the group — this is
    representation of an already-proven replay, never authority ordering (authority is still
    decided strictly by ``valid_at``). Unparseable ingest times sort last."""
    dt = parse_iso8601(e.learned_at)
    return dt if dt is not None else _LEARNED_FAR_FUTURE


def _rep_secondary_key(e: "TypedEventAssertion") -> tuple[str, str, str, str, str]:
    """Input-order-independent TOTAL tie-break for replays whose ``learned_at`` is equal/unparseable.
    Because every member of an exact-replay group shares ``assertion_key`` (so predicate/object_key/
    domain/valid_at/time_basis all agree), the remaining distinguishing surface — turn evidence,
    stated span, subject/object display, and metadata — is compared on a STABLE tuple (metadata via
    ``json.dumps(sort_keys=True, default=str)``) so two replays with equal ingest time but differing
    display/metadata still order the same regardless of input order."""
    meta = json.dumps(e.metadata, sort_keys=True, default=str, ensure_ascii=False)
    return (
        _norm(e.turn_evidence_uuid),
        _norm(e.stated_span),
        _norm(e.subject_display),
        _norm(e.object_display),
        meta,
    )


@dataclass(frozen=True)
class EventLane:
    """The fold/selection lane a timeline is scoped to: exactly namespace + subject_uuid + predicate
    + optional domain (normalized). Immutable value object; ``key`` is the comparable form."""

    subject_uuid: str
    predicate: str
    namespace: str | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if not (self.subject_uuid or "").strip():
            raise ValueError("EventLane requires a non-blank subject_uuid")
        if not (self.predicate or "").strip():
            raise ValueError("EventLane requires a non-empty predicate")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            _norm(self.namespace),
            _norm(self.subject_uuid),
            _norm(self.predicate),
            _norm(self.domain),
        )


class EventSelectionIntent(str, Enum):
    """The selection intent for a timeline lane. Distinct from
    ``menhir.domain.temporal_intent.TemporalIntent`` (which detects recall-query lenses); the
    selection intent is about WHICH point on the lane's timeline is requested."""

    LATEST = "latest"             # greatest eligible valid_at, at/before optional as_of
    PREDECESSOR = "predecessor"   # greatest eligible valid_at strictly before a required anchor


class SelectionStatus(str, Enum):
    """Fail-closed outcome of a selection. ``EventSelection.has_unique_selection`` is True only when
    ``status == UNIQUE`` (a deterministic temporal winner — NOT yet authoritative)."""

    UNIQUE = "unique"         # one assertion selected
    NONE = "none"             # lane empty / no eligible candidate / no resolvable anchor
    AMBIGUOUS = "ambiguous"   # distinct candidates tied at the winning valid_at (advisory, no winner)
    INVALID = "invalid"       # bad request: missing lane/anchor, or unparseable/misapplied as_of/anchor


#: authority-decision-style gates for an EventSelection (see ``gate``).
GATE_PASS = "pass"
GATE_NO_CANDIDATE = "no_candidate"
GATE_AMBIGUITY = "ambiguity"
GATE_ANCHOR = "anchor"
GATE_TIME = "time"
GATE_INVALID = "invalid"


@dataclass(frozen=True)
class EventSelection:
    """Immutable selection result. ``gate`` is an explanatory string (``pass`` / ``no_candidate`` /
    ``ambiguity`` / ``anchor`` / ``time`` / ``invalid``); ``selected`` is populated only when a
    unique temporal winner exists (``has_unique_selection``)."""

    status: SelectionStatus
    gate: str
    reason: str
    selected: "TypedEventAssertion | None" = None

    @property
    def has_unique_selection(self) -> bool:
        """True when exactly one assertion was uniquely selected on the winning valid_at. This is NOT
        authority: evidence/foundation gates are a later phase, and ``time_basis`` may be
        ``learned_fallback``. This pure selector only reports the deterministic temporal winner."""
        return self.status is SelectionStatus.UNIQUE


@dataclass(frozen=True)
class TypedEventAssertion:
    """One immutable, grounded categorical occurrence (Phase-1 domain contract).

    ``valid_at`` is world/source time, ``learned_at`` is ingest time. ``span_start``/``span_end``
    are source char offsets (both known or both ``-1``); ``claim_ordinal`` disambiguates same-lane
    claims in one episode when offsets are absent. See the module docstring for identity levels.
    """

    subject_uuid: str            # resolved entity identity (post-correlation)
    subject_display: str         # human-readable subject (display surface, never the key)
    predicate: str               # canonical predicate (owned, located_at, measured, ...)
    object_key: str              # resolved object identity/key
    object_display: str          # human-readable object (display surface, never the key)
    valid_at: str                # world/source time (ISO); parseable via parse_iso8601
    learned_at: str              # ingest time (ISO); never part of event identity, and never used for
                                 # temporal authority ordering. Permitted ONLY to choose a deterministic
                                 # representative among an already-proven exact replay.
    stated_span: str             # exact source span (readable grounding)
    episode_uuid: str            # provenance / replay ledger key
    object_uuid: str | None = None  # optional resolved object entity uuid
    domain: str | None = None    # optional domain scoping (fold lane component)
    namespace: str | None = None
    turn_evidence_uuid: str | None = None
    span_start: int = -1         # source char offset (>=0 when known; with span_end locates the claim)
    span_end: int = -1           # source char offset (>= span_start when known)
    claim_ordinal: int = 0       # disambiguates same-lane claims in one episode when offsets absent
    time_basis: str = "explicit" # provenance for valid_at (one of TIME_BASES)
    evidence_tier: str = "agent" # one of EVIDENCE_TIERS
    perceiver_version: str = "v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Fail-closed identity + grounding: an assertion missing any required field cannot anchor a
        # categorical occurrence, so it is a hard error rather than a silently weak record. Every
        # check tolerates a runtime None (treated as blank) and raises ValueError, never AttributeError.
        if not (self.subject_uuid or "").strip():
            raise ValueError("TypedEventAssertion requires a non-blank subject_uuid (resolved identity)")
        if not (self.subject_display or "").strip():
            raise ValueError("TypedEventAssertion requires a non-blank subject_display")
        if not (self.predicate or "").strip():
            raise ValueError("TypedEventAssertion requires a non-empty predicate (canonical)")
        if not (self.object_key or "").strip():
            raise ValueError("TypedEventAssertion requires a non-empty object_key")
        if not (self.object_display or "").strip():
            raise ValueError("TypedEventAssertion requires a non-blank object_display")
        if not (self.valid_at or "").strip():
            raise ValueError("TypedEventAssertion requires a non-blank valid_at (world/source time)")
        if not (self.learned_at or "").strip():
            raise ValueError("TypedEventAssertion requires a non-blank learned_at (ingest time)")
        if not (self.stated_span or "").strip():
            raise ValueError("TypedEventAssertion requires a non-empty stated_span (source grounding)")
        if not (self.episode_uuid or "").strip():
            raise ValueError("TypedEventAssertion requires an episode_uuid (provenance)")
        if self.time_basis not in TIME_BASES:
            raise ValueError(f"time_basis {self.time_basis!r} not in {sorted(TIME_BASES)}")
        if self.evidence_tier not in EVIDENCE_TIERS:
            raise ValueError(f"evidence_tier {self.evidence_tier!r} not in {EVIDENCE_TIERS}")
        if not (isinstance(self.span_start, int) and isinstance(self.span_end, int)):
            raise ValueError("span_start/span_end must be ints")
        both_known = self.span_start >= 0 and self.span_end >= 0
        both_absent = self.span_start == -1 and self.span_end == -1
        if not (both_known or both_absent):
            raise ValueError("span_start/span_end must both be known (>=0) or both -1")
        if both_known and self.span_end < self.span_start:
            raise ValueError("span_end must be >= span_start")
        if self.claim_ordinal < 0:
            raise ValueError("claim_ordinal must be >= 0")

    @property
    def source_key(self) -> str:
        """BINDING-STABLE source locator: episode + span + ordinal, no subject_uuid, no learned_at.
        Reuses ``build_source_key`` so this cannot drift from the TypedAssertion definition."""
        return build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)

    @property
    def assertion_key(self) -> str:
        """Fully-interpreted identity: source_key + perceiver_version + namespace + predicate +
        object_key + domain + valid_at + time_basis. Exact replay of the same interpreted occurrence
        -> the same key; that is what ``select_event_assertion`` dedups on. Includes
        ``valid_at``/``time_basis`` so a corrected source/world time on the same span is a NEW
        fully-interpreted assertion, not collapsed as a replay. Excludes ``learned_at`` AND
        ``subject_uuid``/``object_uuid``: UUID rebinding (merge) does NOT fork the fully interpreted
        source assertion — the canonical ``object_key`` (and source_key) carry the interpretation, and
        ``object_uuid`` is a resolution detail, not identity (mirrors scalar binding-stable identity)."""
        h = hashlib.sha256()
        parts = (
            self.source_key,
            str(self.perceiver_version),
            _norm(self.namespace),
            _norm(self.predicate),
            _norm(self.object_key),
            _norm(self.domain),
            _norm(self.valid_at),
            _norm(self.time_basis),
        )
        for part in parts:
            h.update(str(part).encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    @property
    def lane_key(self) -> tuple[str, str, str, str]:
        """The comparable form of this assertion's lane: (namespace, subject_uuid, predicate, domain),
        normalized — matches ``EventLane.key`` for the same components."""
        return (
            _norm(self.namespace),
            _norm(self.subject_uuid),
            _norm(self.predicate),
            _norm(self.domain),
        )


def select_event_assertion(
    events: Iterable[TypedEventAssertion],
    *,
    lane: EventLane,
    intent: EventSelectionIntent,
    as_of: str | None = None,
    anchor_time: str | None = None,
    anchor_assertion_key: str | None = None,
) -> EventSelection:
    """Pure, deterministic selector over one lane. Ordering is by ``valid_at`` only (via
    ``parse_iso8601``); ``learned_at`` and input order never break a tie.

    ``lane`` scopes selection strictly to exactly that one ``EventLane``. For ``LATEST``, ``as_of``
    is an optional upper bound (at/before); without it the greatest valid_at wins. For
    ``PREDECESSOR``, an anchor is required — either ``anchor_assertion_key`` (resolved to its
    valid_at within ``events``) or ``anchor_time`` — and the result is the greatest valid_at strictly
    before it.

    Exact replay duplicates (same ``assertion_key``) are deduplicated deterministically. Distinct
    candidates tied at the winning valid_at fail closed as ``AMBIGUOUS``; malformed/missing
    ``valid_at`` and events outside the lane can never win.

    Selector parameters fail closed when mutually exclusive or misapplied: ``anchor_assertion_key``
    and ``anchor_time`` are mutually exclusive (both -> ``INVALID``/``GATE_ANCHOR``); any anchor
    parameter on ``LATEST`` is ``INVALID``/``GATE_ANCHOR``; ``as_of`` on ``PREDECESSOR`` is
    ``INVALID``/``GATE_ANCHOR``.
    """
    req_key = lane.key
    has_anchor_key = anchor_assertion_key is not None
    has_anchor_time = anchor_time is not None
    if has_anchor_key and has_anchor_time:
        return EventSelection(
            SelectionStatus.INVALID, GATE_ANCHOR,
            "anchor_assertion_key and anchor_time are mutually exclusive", None)
    if intent is EventSelectionIntent.LATEST and (has_anchor_key or has_anchor_time):
        return EventSelection(
            SelectionStatus.INVALID, GATE_ANCHOR,
            "anchor parameters are not valid for LATEST", None)
    if intent is EventSelectionIntent.PREDECESSOR and as_of is not None:
        return EventSelection(
            SelectionStatus.INVALID, GATE_ANCHOR,
            "as_of is not valid for PREDECESSOR (use anchor_time / anchor_assertion_key)", None)

    # 1) Strict lane filter.
    lane_events = [e for e in events if e.lane_key == req_key]

    # 2) Deterministic exact-replay dedup. Identical ``assertion_key`` == the same proven occurrence
    #    re-persisted; within that group we keep ONE representative chosen by earliest parseable
    #    ``learned_at`` (then input-order-independent secondaries). This is representation only — it
    #    never manufactures a winner and never collapses DISTINCT candidates (different assertion_key)
    #    that legitimately tie on valid_at. See ``_rep_learned_key`` for the boundary.
    groups: dict[str, list[TypedEventAssertion]] = {}
    for e in lane_events:
        groups.setdefault(e.assertion_key, []).append(e)
    deduped: list[TypedEventAssertion] = [
        min(members, key=lambda m: (_rep_learned_key(m), _rep_secondary_key(m)))
        for members in groups.values()
    ]

    # 3) Parse valid_at; malformed/missing time can never win.
    parsed: list[tuple[TypedEventAssertion, Any]] = []
    for e in deduped:
        dt = parse_iso8601(e.valid_at)
        if dt is not None:
            parsed.append((e, dt))

    if intent is EventSelectionIntent.LATEST:
        if as_of is not None:
            pivot = parse_iso8601(as_of)
            if pivot is None:
                return EventSelection(
                    SelectionStatus.INVALID, GATE_TIME, f"as_of is not parseable: {as_of!r}", None)
            eligible = [(e, dt) for e, dt in parsed if dt <= pivot]
        else:
            eligible = parsed
        if not eligible:
            return EventSelection(
                SelectionStatus.NONE, GATE_NO_CANDIDATE,
                "no eligible assertion in lane at/before as_of", None)
        winning = max(dt for _, dt in eligible)
        winners = [e for e, dt in eligible if dt == winning]
        if len(winners) > 1:
            return EventSelection(
                SelectionStatus.AMBIGUOUS, GATE_AMBIGUITY,
                f"distinct candidates tied at winning valid_at {winning.isoformat()}", None)
        return EventSelection(SelectionStatus.UNIQUE, GATE_PASS, "latest selected", winners[0])

    if intent is EventSelectionIntent.PREDECESSOR:
        if anchor_assertion_key is not None:
            pivot = None
            for e, dt in parsed:
                if e.assertion_key == anchor_assertion_key:
                    pivot = dt
                    break
            if pivot is None:
                return EventSelection(
                    SelectionStatus.INVALID, GATE_ANCHOR,
                    "anchor_assertion_key not found among candidates in lane", None)
        elif anchor_time is not None:
            pivot = parse_iso8601(anchor_time)
            if pivot is None:
                return EventSelection(
                    SelectionStatus.INVALID, GATE_TIME,
                    f"anchor_time is not parseable: {anchor_time!r}", None)
        else:
            return EventSelection(
                SelectionStatus.INVALID, GATE_ANCHOR,
                "PREDECESSOR requires an anchor (anchor_assertion_key or anchor_time)", None)

        eligible = [(e, dt) for e, dt in parsed if dt < pivot]
        if not eligible:
            return EventSelection(
                SelectionStatus.NONE, GATE_NO_CANDIDATE,
                "no assertion strictly before the anchor in lane", None)
        winning = max(dt for _, dt in eligible)
        winners = [e for e, dt in eligible if dt == winning]
        if len(winners) > 1:
            return EventSelection(
                SelectionStatus.AMBIGUOUS, GATE_AMBIGUITY,
                f"distinct candidates tied at predecessor valid_at {winning.isoformat()}", None)
        return EventSelection(SelectionStatus.UNIQUE, GATE_PASS, "predecessor selected", winners[0])

    return EventSelection(
        SelectionStatus.INVALID, GATE_INVALID, f"unsupported intent: {intent!r}", None)

