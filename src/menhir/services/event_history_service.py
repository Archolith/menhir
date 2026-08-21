"""Event History Phase 2B.2b: the deterministic rebuild that materializes one event-lane timeline.

This service drives the durable :TypedEventAssertion log (``source``) into the disposable event-lane
timeline View (``sink``) for exactly one ``EventLane``. It is deliberately LLM-free and makes no
authority verdict: it only folds the lane's materializable, non-superseded assertions into the fixed
event-entry schema and writes/edges them to the View, then reconciles the lane's stale Views.

## Contract invariants

* **One lane, fail-closed.** ``EventLane`` validation fails closed; the read is exactly one lane with
  ``include_superseded=False`` / ``materializable_only=True``.
* **No ``learned_at``.** World time (``valid_at``) is the only temporal authority; ``learned_at``
  never orders entries and never acts as a fallback for an unparseable ``valid_at``. An assertion
  with an unparseable ``valid_at`` is counted in ``excluded_invalid_time`` and left in the durable
  audit only — it never reaches the disposable View.
* **Deterministic, total order.** Retained entries are ordered by world time then ``assertion_key``,
  independent of source/input order. Exact replays (same ``assertion_key``) are deduplicated to one
  TOTAL deterministic representative — the minimum over a stable, ``learned_at``-free tie-break —
  never a "latest winner".
* **Write + edge proof.** A retained lane writes the View, then redraws its ``EVENT_HISTORY_ENTRY``
  edges; ``complete`` is True only when the returned View ``uuid`` is nonblank AND ``drawn ==
  expected == len(entries)``.
* **Reconcile exactly this lane.** Reconciliation reads the lane's current Views, compares normalized
  predicate/domain, and retires only stale Views for THIS lane. Sibling lanes are never touched.
* **Fail-closed retirement.** On a missing uuid, edge mismatch, or any exception, the rebuild returns
  ``complete=False`` with a concise ``reason`` and performs NO speculative retirement — existing Views
  are left untouched for retry. Empty/all-invalid is the deliberate no-desired-state case and may
  retire this lane's Views (writing no empty View).
"""

from __future__ import annotations

import json
from datetime import timezone
from typing import Any, Iterable, Protocol

from menhir.domain.event_history import (
    EventLane,
    TypedEventAssertion,
    # IDENTITY normalization, not query-text normalization (CF-72). This module compares
    # values against `EventLane.key`, which is built with this exact function; using the
    # whitespace-collapsing `normalize_text` here would make a stored predicate stop
    # matching the canonical lane it was written from, so a View that should be retired
    # would survive. See the note in menhir.domain.event_history.
    normalize_identity_component as _norm,
)
from menhir.domain.temporal import parse_iso8601


def _canonical_utc_z(when: str) -> str:
    """Canonicalize a parseable world-time string to UTC ISO with a trailing 'Z'. Callers must only
    pass a value that already parsed; this strips offsets so ordering is offset-independent."""
    dt = parse_iso8601(when)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rep_key(a: TypedEventAssertion) -> tuple[str, str, str, str, str]:
    """TOTAL, input-order-independent tie-break for an exact-replay representative. Never uses
    ``learned_at``: it compares only the stable display/grounding surface of an already-proven
    replay (turn evidence, stated span, subject/object display, and canonicalized metadata), so two
    replays with equal identity still order identically regardless of ingest time or input order."""
    meta = json.dumps(a.metadata, sort_keys=True, default=str, ensure_ascii=False)
    return (
        _norm(a.turn_evidence_uuid),
        _norm(a.stated_span),
        _norm(a.subject_display),
        _norm(a.object_display),
        meta,
    )


class EventAssertionSource(Protocol):
    """The durable assertion-log reader the rebuild consumes. Matches
    ``TypedEventAssertionRepository.assertions_for_lane`` for a single lane."""

    def assertions_for_lane(
        self,
        lane: EventLane,
        *,
        include_superseded: bool = False,
        materializable_only: bool = True,
    ) -> list[TypedEventAssertion]:
        ...


class EventTimelineSink(Protocol):
    """The disposable event-lane timeline View writer the rebuild drives. Matches
    ``ViewRepository``'s event-lane mixin methods."""

    def record_event_timeline(
        self, *, subject: str, subject_uuid: str, predicate: str,
        entries: list[dict[str, Any]], domain: str | None = None,
        namespace: str | None = None, source: str = "event-history",
        source_confidence: float = 0.6, name_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        ...

    def list_event_timeline_views(
        self, *, subject_uuid: str, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def retire_event_timeline(self, *, view_key: str) -> bool:
        ...

    def draw_event_timeline_entries(
        self, *, view_uuid: str, entries: list[dict[str, Any]],
    ) -> dict[str, int]:
        ...


class EventHistoryService:
    """Deterministic rebuild of one event-lane timeline View from the durable event assertion log.

    ``source`` provides ``assertions_for_lane``; ``sink`` provides the event-lane timeline View
    methods. The service holds no LLM and makes no authority verdict.
    """

    def __init__(self, source: EventAssertionSource, sink: EventTimelineSink) -> None:
        self.source = source
        self.sink = sink

    # ------------------------------------------------------------------ public API

    def rebuild_lane(self, lane: EventLane, *, source: str = "event-history") -> dict[str, Any]:
        """Rebuild the event-lane timeline View for exactly ``lane`` and return a stable outcome dict.

        On a retained lane this writes the View, redraws its edges, and (only when complete) retires
        this lane's stale Views. An empty/all-invalid lane retires this lane's Views and writes no
        empty View. Any failure returns ``complete=False`` with a concise ``reason`` and performs no
        retirement.
        """
        base: dict[str, Any] = {
            "subject_uuid": getattr(lane, "subject_uuid", None),
            "namespace": getattr(lane, "namespace", None),
            "predicate": getattr(lane, "predicate", None),
            "domain": getattr(lane, "domain", None),
            "complete": False,
            "written": 0,
            "retired": 0,
            "retired_views": [],
            "entry_count": 0,
            "excluded_invalid_time": 0,
            "drawn": None,
            "expected": None,
            "view_key": None,
            "reason": "",
        }
        try:
            canonical = self._canonical_lane(lane)
            assertions = self.source.assertions_for_lane(
                canonical, include_superseded=False, materializable_only=True,
            )
            entries, excluded, subject_display = self._build_entries(assertions)

            base["subject_uuid"] = canonical.subject_uuid
            base["namespace"] = canonical.namespace
            base["predicate"] = canonical.predicate
            base["domain"] = canonical.domain
            base["entry_count"] = len(entries)
            base["excluded_invalid_time"] = excluded

            if not entries:
                return self._retire_empty(canonical, base)

            res = self.sink.record_event_timeline(
                subject=subject_display, subject_uuid=canonical.subject_uuid,
                predicate=canonical.predicate, domain=canonical.domain,
                namespace=canonical.namespace, entries=entries, source=source,
            )
            view_uuid = str(res.get("uuid") or "").strip()
            view_key = str(res.get("view_key") or "").strip()
            base["view_key"] = view_key or None
            if not view_uuid:
                base["reason"] = "record_event_timeline returned no view uuid"
                return base
            if not view_key:
                base["reason"] = "record_event_timeline returned no view_key"
                return base
            base["written"] = 1

            drawn = self.sink.draw_event_timeline_entries(view_uuid=view_uuid, entries=entries)
            base["drawn"] = int(drawn.get("drawn") or 0)
            base["expected"] = int(drawn.get("expected") or 0)
            if not (base["drawn"] == base["expected"] == len(entries)):
                base["reason"] = (
                    f"edge draw mismatch: drawn={base['drawn']} expected={base['expected']} "
                    f"entry_count={len(entries)}"
                )
                return base

            # Edge proof complete. Reconciliation (list + retire) decides `complete`; it is NOT set
            # True here, so an exception inside list/retire is caught by the outer except and returns
            # complete=False rather than a stale "complete" with a failure reason.
            return self._reconcile(canonical, view_key, base)
        except Exception as exc:  # noqa: BLE001 - fail closed with a concise reason
            base["reason"] = f"rebuild_lane failed: {type(exc).__name__}: {exc}"
            return base

    def rebuild_lanes(
        self, lanes: Iterable[EventLane], *, source: str = "event-history",
    ) -> dict[str, Any]:
        """Rebuild many lanes: normalize/dedup lane keys, run in deterministic lane-key order, and
        aggregate. ``complete`` is True only when every lane result is complete."""
        deduped: list[EventLane] = []
        seen: set[tuple[str, str, str, str]] = set()
        for lane in lanes:
            key = lane.key
            if key in seen:
                continue
            seen.add(key)
            deduped.append(lane)
        deduped.sort(key=lambda candidate_lane: candidate_lane.key)
        results = [self.rebuild_lane(candidate_lane, source=source) for candidate_lane in deduped]
        return {
            "complete": bool(results) and all(r.get("complete") for r in results),
            "lane_count": len(results),
            "results": results,
        }

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _canonical_lane(lane: EventLane) -> EventLane:
        """Reconstruct the canonical lane from the normalized identity components (``lane.key``) so
        casing/whitespace in namespace/domain can never fork the View key from the durable lane. The
        subject DISPLAY stays contributor-derived; lane identity is the normalized key."""
        namespace, subject_uuid, predicate, domain = lane.key
        return EventLane(
            subject_uuid=subject_uuid, predicate=predicate,
            namespace=namespace, domain=domain,
        )

    def _build_entries(
        self, assertions: Iterable[TypedEventAssertion],
    ) -> tuple[list[dict[str, Any]], int, str]:
        """Deterministically fold the lane's assertions into the fixed event-entry schema.

        Returns ``(entries, excluded_invalid_time, subject_display)``. Assertions with an unparseable
        ``valid_at`` are counted as excluded and never converted. Exact replays (same
        ``assertion_key``) are deduplicated to one total representative; the result is ordered by
        world time then ``assertion_key``. ``subject_display`` is chosen deterministically as the
        minimum ``(casefolded, original)`` display across the retained representatives — total and
        input-order-independent, not coupled to presentation order.
        """
        valid: list[TypedEventAssertion] = []
        excluded = 0
        for a in assertions:
            if parse_iso8601(a.valid_at) is None:
                excluded += 1
                continue
            valid.append(a)

        groups: dict[str, list[TypedEventAssertion]] = {}
        for a in valid:
            groups.setdefault(a.assertion_key, []).append(a)
        reps = [min(members, key=_rep_key) for members in groups.values()]
        reps.sort(key=lambda a: (
            parse_iso8601(a.valid_at).isoformat(), a.assertion_key, _rep_key(a),
        ))

        subject_display = ""
        if reps:
            subject_display = min(
                ((d.strip().casefold(), d.strip()) for d in (a.subject_display for a in reps)),
            )[1]
        entries = [self._to_entry(a) for a in reps]
        entries.sort(key=lambda e: (e["when"], e["assertion_key"]))
        return entries, excluded, subject_display

    @staticmethod
    def _to_entry(a: TypedEventAssertion) -> dict[str, Any]:
        """Map one materializable assertion to the fixed event-entry schema. ``when`` is the
        canonical UTC 'Z' world time; ``what`` is occurrence wording from predicate + object_display;
        ``quote`` is the exact stated span. No current-ownership/supersession wording appears."""
        return {
            "assertion_key": a.assertion_key,
            "source_key": a.source_key,
            "when": _canonical_utc_z(a.valid_at),
            "what": f"{a.predicate} {a.object_display}".strip(),
            "object_key": a.object_key,
            "object_uuid": a.object_uuid,
            "quote": a.stated_span,
            "episode_uuid": a.episode_uuid,
            "turn_evidence_uuid": a.turn_evidence_uuid,
            "time_basis": a.time_basis,
            "evidence_tier": a.evidence_tier,
        }

    def _exact_lane_views(self, canonical: EventLane) -> list[dict[str, Any]]:
        """The current Views for this exact lane (matching normalized predicate/domain). Sibling
        lanes (different predicate/domain) are excluded so they are never retired."""
        views = self.sink.list_event_timeline_views(
            subject_uuid=canonical.subject_uuid, namespace=canonical.namespace,
        )
        exact: list[dict[str, Any]] = []
        for v in views:
            if _norm(v.get("predicate")) == canonical.predicate and \
               _norm(v.get("domain")) == canonical.domain:
                exact.append(v)
        return exact

    def _retire_empty(
        self, canonical: EventLane, base: dict[str, Any],
    ) -> dict[str, Any]:
        """No-desired-state case: retire this lane's current Views and write no empty View. This is a
        deliberate outcome, but it reports ``complete=True`` only if every retirement actually
        happened; a failed retire fails closed with the unreconciled key."""
        retired, unreconciled = self._retire_views(self._exact_lane_views(canonical))
        base["retired"] = len(retired)
        base["retired_views"] = retired
        if unreconciled:
            base["complete"] = False
            base["reason"] = "empty lane; failed to retire stale view(s): " + ", ".join(unreconciled)
            return base
        base["complete"] = True
        base["reason"] = "no retained entries; retired exact-lane views and wrote no empty view"
        return base

    def _reconcile(
        self, canonical: EventLane, desired_view_key: str, base: dict[str, Any],
    ) -> dict[str, Any]:
        """Retire this exact lane's current Views whose view_key differs from the just-written one.
        The desired View is kept; sibling lanes are never touched. ``complete`` is True only when
        every stale View actually retired; a failed retire fails closed with the unreconciled key."""
        stale = [v for v in self._exact_lane_views(canonical)
                 if str(v.get("view_key") or "") != desired_view_key]
        retired, unreconciled = self._retire_views(stale)
        base["retired"] = len(retired)
        base["retired_views"] = retired
        if unreconciled:
            base["complete"] = False
            base["reason"] = "reconcile failed to retire stale view(s): " + ", ".join(unreconciled)
            return base
        base["complete"] = True
        base["reason"] = "rebuilt lane complete"
        return base

    def _retire_views(self, views: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Retire each given View by view_key. Returns ``(retired, unreconciled)`` view_keys."""
        retired: list[str] = []
        unreconciled: list[str] = []
        for v in views:
            view_key = str(v.get("view_key") or "").strip()
            if view_key and self.sink.retire_event_timeline(view_key=view_key):
                retired.append(view_key)
            else:
                unreconciled.append(view_key)
        return retired, unreconciled
