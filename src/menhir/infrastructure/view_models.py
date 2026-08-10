"""View — the ONE supersedable, recallable node shape behind write-time consolidation.

Event -> Fold -> View. A View is materialized query-sufficient state: a stamped :Entity that
recall treats exactly like an ingested memory (the "stamp like ingest" invariant), carrying at
most one CURRENT version per key, older versions kept and linked by SUPERSEDES.

There is ONE View node shape and a growing set of *kinds*, distinguished ONLY by their value slot:
  - kind='counter'  (QuantState)  value is a scalar          -> "failed 4 times"     (view_value)
  - kind='timeline'               value is an ordered list   -> "what happened, when" (view_payload)

## SSOT: the repository owns what is SHARED; a ViewKind owns what is per-kind — both directions.

`ViewRepository._write_version` is the single writer of the shared machinery: recall stamps,
supersession (`view_current`/`SUPERSEDES`/`expired_at`, old kept), MENTIONS provenance, `view_key`
keying, `view_sig` idempotency. It never knows what a "counter" or a "timeline" IS.

A `ViewKind` is the single source of truth for one memory type — its value slot, retrieval surface,
signature, AND read projection, so "what a counter is" is defined in exactly one place instead of
smeared across a record_ method and a fetch_ method. Adding a memory type = **one new ViewKind
subclass** registered in `ViewRepository.KINDS`, with zero changes to the write core. That is the
code expression of the invariant: a new memory type = a new fold + a new value slot, never a new
node type.

INVARIANT — "if it should be recalled, it must be stamped like ingest stamps it":
This writer bypasses graphiti's add_episode, so every View version must reproduce ingest's full
stamping (namespace stamped, scope=PERSISTENT, name+name_embedding) or recall silently drops it.
Those stamps live in `_write_version` so every kind is stamped identically — the reason recall
needs no per-kind code (`recall_service` never references `view_kind`).

Back-compat: counter nodes still carry `is_quantstate:true` + the `qs_*` mirror props, and reads
fall back to `qs_key`/`qs_current`, so counters written by the pre-View writer keep superseding with
no migration. QuantStateRepository is a thin alias of this class.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import uuid4

from menhir.domain.temporal import parse_iso8601

try:  # neo4j is a hard runtime dep; guard the import so unit imports without the driver still load.
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except Exception:  # pragma: no cover - driver always present in the running service
    _Neo4jConstraintError = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Placeholder for the supporting-event count inside a kind's `summary_template`. The unchanged-value
#: provenance refresh (plan D2) computes the count INSIDE Cypher (the union must be atomic), so the
#: summary cannot be pre-rendered in Python — it is rendered as a template here and the count is
#: substituted server-side in the same statement that writes the union.
_COUNT_TOKEN = "__N__"

# The kind-agnostic recall stamps every View version must carry (mirrors a graphiti :Entity).
_SHARED_STAMPS = (
    "type: 'SEMANTIC', scope: 'PERSISTENT', freshness: 'ACTIVE', user_flagged: false, "
    "edge_count: 0, sharpness: 0.0"
)


class ViewClass(Enum):
    """Storage class for a View node (Metric plan Part A).

    FACT  -> :Entity, a recallable memory (counters, timelines, verifier registers).
    METRIC -> :Metric, operator-only instrumentation, excluded from semantic recall BY LABEL
              (recall matches :Entity), and never carrying a name_embedding or MENTIONS.

    The two classes share the entire versioning machinery (view_key/view_sig/view_current/
    SUPERSEDES); the only differences are the node label, the type stamp, and provenance.
    """

    FACT = "FACT"
    METRIC = "METRIC"


# Allowlisted enum -> Cypher label literal. Neo4j cannot bind a label as a query parameter, so
# these queries interpolate the label -- but ONLY ever from this closed map, NEVER from a caller
# string or free text. That is the plan's "closed label interface" (A1).
_CLASS_LABELS: dict[ViewClass, str] = {ViewClass.FACT: "Entity", ViewClass.METRIC: "Metric"}


def _label_for(view_class: ViewClass) -> str:
    """The allowlisted Cypher label literal for a ViewClass. Raises on anything unknown."""
    label = _CLASS_LABELS.get(view_class)
    if label is None:
        raise ValueError(f"unknown ViewClass {view_class!r}")
    return label


# =============================================================================================
# ViewKind — the SSOT for one memory type. Everything per-kind, write AND read, lives here.
# =============================================================================================

class ViewKind(ABC):
    """One memory type's complete definition: value slot, retrieval surface, idempotency
    signature, and read projection. The repository supplies the shared shape; a kind supplies
    only what makes it that kind. Payloads are plain dicts (kwargs the wrapper collected)."""

    #: the discriminator stored as `view_kind` and used in the `view_key`
    name: str

    #: LWW-register semantics (fold-algebra Law 1 / EXTREME(valid_at)): the current version is the
    #: one with the greatest world-time `valid_at`, so a temporally-OLDER event must not overwrite
    #: it. True for value registers (counter). False for set/list kinds (timeline), whose
    #: supersession is driven by the signature (the whole set changed), not by which arrived latest.
    lww_register: bool = False

    @abstractmethod
    def key_discriminator(self, payload: dict[str, Any]) -> str:
        """The per-subject key segment (`ns::subject::<this>`). Constant for single-slot kinds
        (timeline), value-bearing for keyed kinds (a counter's counter name)."""

    @abstractmethod
    def signature(self, payload: dict[str, Any]) -> str:
        """Idempotency key: equal signature => no-op (refresh provenance only); different =>
        supersede. Counter = the value; timeline = a hash of the ordered events."""

    @abstractmethod
    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        """(name, summary): the BM25/embedding recall surface + the human-readable body. This is
        what makes the View findable by a natural-language query."""

    def summary_template(self, subject: str, payload: dict[str, Any]) -> str | None:
        """The kind's summary with `_COUNT_TOKEN` where the supporting-event count goes, or None if
        the summary does not quote that count.

        Only used by the unchanged-value provenance refresh (plan D2): the union of episode UUIDs is
        computed inside Cypher, so the final count is not known in Python — the count is substituted
        into this template server-side. A kind that returns None keeps its summary as written."""
        return None

    @abstractmethod
    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The value slot (+ any compat mirror) merged onto the node. Must NOT set the shared
        view_* identity props — `_write_version` owns those."""

    #: RETURN columns for a current-version fetch. Defined ONCE per kind (was duplicated in the
    #: old per-kind fetch queries), consumed by `_fetch_current`.
    read_fields: str

    @abstractmethod
    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map a fetched row (shaped by `read_fields`) to the kind's public dict."""

    # -- shared defaults; a kind overrides only if it differs ---------------------------------
    def episode_uuids(self, payload: dict[str, Any]) -> list[str]:
        return [str(u) for u in (payload.get("episode_uuids") or [])]

    def valid_at(self, payload: dict[str, Any]) -> str | None:
        return payload.get("valid_at")


class CounterKind(ViewKind):
    """kind='counter' (QuantState): a scalar (subject, counter) -> value, supersedable by value."""

    name = "counter"
    lww_register = True  # a current-total register: newer valid_at wins (fold-algebra Law 1)
    read_fields = ("n.uuid AS uuid, n.view_subject AS subject, n.qs_counter AS counter, "
                   "n.view_value AS value, toString(n.valid_at) AS valid_at")

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        return str(payload["counter"])

    def signature(self, payload: dict[str, Any]) -> str:
        return _fmt(float(payload["value"]))

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        counter = str(payload["counter"]); value = float(payload["value"])
        n_eps = len(payload.get("episode_uuids") or [])
        name = _counter_retrieval_text(subject, counter, value)
        return name, _counter_summary(subject, payload, n_eps)

    def summary_template(self, subject: str, payload: dict[str, Any]) -> str | None:
        return _counter_summary(subject, payload, _COUNT_TOKEN)

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        counter = str(payload["counter"]); value = float(payload["value"])
        return {
            "view_value": value,
            # back-compat mirror: pre-View readers/queries keyed on is_quantstate/qs_* keep working
            "is_quantstate": True, "qs_key": key, "qs_subject": subject.strip(),
            "qs_counter": counter.strip(), "qs_value": value, "qs_current": True,
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"uuid": row.get("uuid"), "subject": row.get("subject"),
                "counter": row.get("counter"), "value": row.get("value"),
                "valid_at": row.get("valid_at")}


class TimelineKind(ViewKind):
    """kind='timeline': an ordered event list for a subject, supersedable by the set of events.
    Entries are normalized ONCE by the wrapper (`record_timeline`) before reaching these methods.

    Two modes share the SAME kind and node shape, distinguished only by the payload:
      - LEGACY subject-only mode (no `predicate`): value is an ordered list of `{when, what,
        episode_uuid}` — `key_discriminator` == 'timeline', ordered (when, what) signature, the
        subject-only surface/render, and `parse` returns exactly the existing public keys.
      - EVENT-LANE mode (`predicate` nonblank, optional `domain`): the value is an ordered list of
        the fixed query-sufficient event-entry schema. The lane discriminator is a collision-safe
        `timeline:event:` key, the signature covers predicate/domain and the full normalized
        entries, the surface/render uses occurrence/history language, and `parse` additionally
        projects `predicate`/`domain` only when a predicate is present.
    Event entries are normalized by the dedicated private normalizer (`_normalize_event_entries`);
    the legacy `_normalize_entries` is never reused for them."""

    name = "timeline"
    read_fields = ("n.uuid AS uuid, n.view_subject AS subject, n.view_value AS count, "
                   "n.view_payload AS payload, n.view_predicate AS predicate, "
                   "n.view_domain AS domain, toString(n.valid_at) AS valid_at")

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        if _event_mode(payload):
            return _event_lane_suffix(payload.get("predicate"), payload.get("domain"))
        return "timeline"

    def signature(self, payload: dict[str, Any]) -> str:
        if _event_mode(payload):
            return _event_sig(payload.get("predicate"), payload.get("domain"), payload["entries"])
        return _timeline_sig(payload["entries"])

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        entries = payload["entries"]
        if _event_mode(payload):
            pred = payload.get("predicate")
            dom = payload.get("domain")
            return _event_surface(subject, pred, dom, entries), \
                _render_event_timeline(subject, pred, dom, entries)
        return _timeline_surface(subject, entries), _render_timeline(subject, entries)

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload["entries"]
        props = {"view_value": float(len(entries)),
                 "view_payload": json.dumps(entries, ensure_ascii=False)}
        if _event_mode(payload):
            # Lane stamp: predicate always; domain only when present (None is dropped so legacy
            # subject-only write_props stays byte-identical).
            props["view_predicate"] = str(payload.get("predicate") or "").strip()
            dom = str(payload.get("domain") or "").strip()
            if dom:
                props["view_domain"] = dom
        return props

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {"uuid": row.get("uuid"), "subject": row.get("subject"),
               "count": row.get("count"), "valid_at": row.get("valid_at")}
        out["entries"] = json.loads(row.get("payload") or "[]")
        # Event rows project predicate/domain; legacy rows (no predicate) keep the exact shape.
        if row.get("predicate"):
            out["predicate"] = row.get("predicate")
            out["domain"] = row.get("domain")
        return out

    def episode_uuids(self, payload: dict[str, Any]) -> list[str]:
        return [str(e["episode_uuid"]) for e in payload["entries"] if e.get("episode_uuid")]

    def valid_at(self, payload: dict[str, Any]) -> str | None:
        entries = payload["entries"]
        return entries[-1]["when"] if entries else None


class AdmissionAuditKind(ViewKind):
    """kind='admission_audit': a record of an admission verdict on a user-tier claim.
    Payload: {requested_source, effective_source, granted, turn_evidence_uuid, reason}.
    Non-idempotent: every verdict creates a new row (always supersedes prior).
    """

    name = "admission_audit"
    lww_register = False  # Every audit entry is a separate event, not a value register.
    read_fields = (
        "n.uuid AS uuid, n.view_subject AS subject, n.view_value AS granted, "
        "n.requested_source AS requested_source, n.effective_source AS effective_source, "
        "n.reason AS reason, n.turn_evidence_uuid AS turn_evidence_uuid, "
        "toString(n.valid_at) AS valid_at"
    )

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        return "admission_audit"

    def signature(self, payload: dict[str, Any]) -> str:
        # Non-idempotent: every verdict is unique (timestamp-based), so hash all audit fields.
        import hashlib
        basis = "|".join(str(v) for v in [
            payload.get("requested_source", ""),
            payload.get("effective_source", ""),
            payload.get("granted", False),
            payload.get("turn_evidence_uuid", ""),
            payload.get("reason", ""),
        ])
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        granted = bool(payload.get("granted", False))
        requested = str(payload.get("requested_source", ""))
        effective = str(payload.get("effective_source", ""))
        reason = str(payload.get("reason", ""))
        status = "granted" if granted else "denied"
        name = f"Admission {status}: {subject} claimed {requested}"
        summary = (
            f"Admission verdict for {subject}: requested {requested} tier, "
            f"effective {effective} ({status}). Reason: {reason}"
        )
        return name, summary

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "view_value": float(bool(payload.get("granted", False))),
            "requested_source": str(payload.get("requested_source", "")),
            "effective_source": str(payload.get("effective_source", "")),
            "reason": str(payload.get("reason", "")),
            "turn_evidence_uuid": (
                str(payload["turn_evidence_uuid"]) if payload.get("turn_evidence_uuid") else None
            ),
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "uuid": row.get("uuid"),
            "subject": row.get("subject"),
            "granted": bool(row.get("granted")),
            "requested_source": row.get("requested_source"),
            "effective_source": row.get("effective_source"),
            "reason": row.get("reason"),
            "turn_evidence_uuid": row.get("turn_evidence_uuid"),
            "valid_at": row.get("valid_at"),
        }


def _scalar_norm(value: Any) -> str:
    """Stable normalized string for a typed scalar value — the register content AND the signature
    basis. Handles the heterogeneous typed ValueKinds: numbers, ranges ``[lo, hi]``, booleans, and
    string states (clock_time ``07:30``, weekday ``saturday``, status ``finished``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _fmt(float(value))
    if isinstance(value, (list, tuple)):
        parts = [
            _fmt(float(x)) if isinstance(x, (int, float)) and not isinstance(x, bool)
            else str(x).strip()
            for x in value
        ]
        # Degenerate range [N, N] -> the point N. MUST mirror domain.typed_assertion.normalize_scalar
        # exactly (see its comment): this function is the View/signature side of the same value
        # identity, so a collapse applied on only one side would make the assertion and the View
        # disagree about the same value. A genuine range (lo != hi) is left intact.
        if len(parts) == 2 and parts[0] == parts[1]:
            return parts[0]
        return "-".join(parts)
    return str(value).strip()


def _duration_seconds_endpoint_display(value: Any) -> str | None:
    """Render one canonical duration-in-seconds value without changing its stored identity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        total = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not total.is_finite():
        return None

    sign = "-" if total < 0 else ""
    magnitude = abs(total)
    whole_seconds = int(magnitude)
    fractional_seconds = magnitude - whole_seconds
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    seconds_value = Decimal(seconds) + fractional_seconds
    seconds_text = format(seconds_value, "f")
    if "." in seconds_text:
        seconds_text = seconds_text.rstrip("0").rstrip(".")
    if seconds_value < 10:
        seconds_text = f"0{seconds_text}"
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds_text}"
    return f"{sign}{minutes}:{seconds_text}"


def _scalar_display(payload: dict[str, Any]) -> str:
    """Return the retrieval-facing display while preserving the canonical scalar value.

    Duration perception deliberately canonicalizes elapsed ``M:SS``/``H:MM:SS`` values to seconds
    so folds, votes, deltas, and signatures stay numeric. The View is the presentation boundary, so
    it restores an elapsed-time display unless a caller supplied a more specific display.
    """
    explicit = payload.get("display")
    if explicit is not None and str(explicit).strip():
        return str(explicit)

    value = payload["value"]
    value_kind = str(payload.get("value_kind") or "").strip().lower()
    unit = str(payload.get("unit") or "").strip().lower()
    if value_kind == "duration" and unit == "seconds":
        if isinstance(value, (list, tuple)):
            displays = [_duration_seconds_endpoint_display(endpoint) for endpoint in value]
            if len(displays) == 2 and all(display is not None for display in displays):
                return displays[0] if displays[0] == displays[1] else f"{displays[0]}–{displays[1]}"
        else:
            display = _duration_seconds_endpoint_display(value)
            if display is not None:
                return display
    return _scalar_norm(value)


class ScalarStateKind(ViewKind):
    """kind='scalar_state': an entity-linked typed scalar register — the CURRENT value of an
    (entity, attribute, scope) for one of the typed ValueKinds (boolean, status, count, duration,
    frequency, money, measurement, clock_time, weekday).

    Written ONLY by the authoritative rebuild (ScalarStateService), which replaces the projection
    with the deterministic fold of the full current event log. So although ``lww_register`` is True
    (a value register), rebuild writes bypass the incremental LWW guard: a correction that moves the
    current value backward in ``valid_at`` must install, because rebuild is a full replacement, not
    a late-arriving incremental event. The signature therefore keys on value AND valid_at, so any
    projection change re-versions rather than refreshing a stale surface.

    Identity is the resolved entity UUID via the repository's ``subject_uuid`` keying, NOT the
    subject text (the ScalarStateView decision). The per-entity slot discriminates on a canonical
    HASH of {attribute, scope, value_kind, unit} so distinct series (MCU vs all films, owned vs
    sold) never supersede each other, and a value/scope containing ':' cannot collide keys. The
    readable slot components are kept as ``ss_*`` props for inspection.

    Payload: {attribute, scope, value_kind, unit, value (kind-typed), display?, valid_at?}.
    See `.agent/plans/menhir-scalar-state-view-{design,implementation}-plan.md`."""

    name = "scalar_state"
    lww_register = True  # register semantics; rebuild writes bypass it (authoritative replacement)
    read_fields = (
        "n.uuid AS uuid, n.view_subject AS subject, n.view_subject_uuid AS subject_uuid, "
        "n.ss_attribute AS attribute, n.ss_scope AS scope, n.ss_kind AS value_kind, "
        "n.ss_unit AS unit, n.ss_value AS value, n.ss_display AS display, "
        "toString(n.valid_at) AS valid_at"
    )

    #: the typed ValueKinds a scalar_state slot may carry (fail-closed allowlist).
    VALUE_KINDS = frozenset({
        "boolean", "status", "count", "duration", "frequency",
        "money", "measurement", "clock_time", "weekday",
    })

    @classmethod
    def _slot(cls, payload: dict[str, Any]) -> dict[str, str]:
        # Fail-closed identity: an empty attribute or an unknown value_kind would collapse
        # unrelated values into one entity slot — the exact over-merge the View exists to
        # eliminate — so reject them. Blank scope and unit stay legal (a valid unscoped/unitless
        # attribute series).
        attribute = str(payload.get("attribute", "")).strip().lower()
        value_kind = str(payload.get("value_kind", "")).strip().lower()
        if not attribute:
            raise ValueError("scalar_state slot requires a non-empty attribute")
        if value_kind not in cls.VALUE_KINDS:
            raise ValueError(
                f"scalar_state value_kind {value_kind!r} not in {sorted(cls.VALUE_KINDS)}"
            )
        return {
            "attribute": attribute,
            "scope": str(payload.get("scope", "")).strip().lower(),
            "value_kind": value_kind,
            "unit": str(payload.get("unit", "") or "").strip().lower(),
        }

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        # Canonical serialized hash of the slot (NOT raw colon concat): collision-safe on ':' in a
        # value/scope. Readable components are stored as ss_* props (write_props).
        canon = json.dumps(
            self._slot(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return "ss_" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]

    def signature(self, payload: dict[str, Any]) -> str:
        # Projection signature: value AND valid_at. Rebuild is authoritative (it replaces the
        # projection from the full log, not an incremental event), so a same-VALUE fold whose anchor
        # moved to a different valid_at (e.g. the July anchor was corrected away and an August anchor
        # now governs the same value) must produce a NEW version with the fresh surface/time — not a
        # provenance-only refresh of the stale one. Display/scope/subject are refreshed on the
        # unchanged-signature path (authoritative full-projection refresh in record()).
        return f"{_scalar_norm(payload['value'])}|{str(payload.get('valid_at') or '')}"

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        attribute = str(payload.get("attribute", "")).strip()
        scope = str(payload.get("scope", "")).strip()
        display = _scalar_display(payload)
        human = attribute.replace("_", " ") or "value"
        scope_txt = f" ({scope})" if scope else ""
        subj = subject.strip()
        valid_at = str(payload.get("valid_at") or "")
        name = f"{subj}'s {human}{scope_txt}: {display}. current {human}{scope_txt} = {display}."
        summary = f"{subj} — {human}{scope_txt} = {display} (current as of {valid_at[:10]})"
        return name, summary

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = payload["value"]
        vstr = _scalar_norm(value)
        slot = self._slot(payload)
        # numeric compat mirror for view_value; the register content proper is ss_value/ss_display.
        if isinstance(value, bool):
            numeric = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            numeric = float(value)
        else:
            numeric = 0.0
        return {
            "view_value": numeric,
            "ss_value": vstr,
            "ss_display": _scalar_display(payload),
            "ss_attribute": slot["attribute"],
            "ss_scope": slot["scope"],
            "ss_kind": slot["value_kind"],
            "ss_unit": slot["unit"],
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "uuid": row.get("uuid"), "subject": row.get("subject"),
            "subject_uuid": row.get("subject_uuid"),
            "attribute": row.get("attribute"), "scope": row.get("scope"),
            "value_kind": row.get("value_kind"), "unit": row.get("unit"),
            "value": row.get("value"), "display": row.get("display"),
            "valid_at": row.get("valid_at"),
        }


class ScalarHistoryKind(ViewKind):
    """kind='scalar_history': a slot-keyed, ordered, advisory history of typed scalar assertions.

    The companion to `scalar_state`: while scalar_state carries the CURRENT folded value (or
    abstains when no absolute anchor exists), scalar_history preserves the chronological sequence
    of all materializable assertions for a slot — including delta-only slots that scalar_state
    correctly refuses to ground.

    Identity mirrors scalar_state: `subject_uuid`-anchored, with the same canonical slot hash
    of {attribute, scope, value_kind, unit}. The discriminator prefix is `sh_` (vs `ss_`).

    **Advisory only.** A scalar_history View:
      - never enters the scalar authority lane;
      - never suppresses raw evidence;
      - never computes an absolute total from unanchored deltas.

    Payload: bounded JSON entries array (latest N assertions with typed values and provenance),
    plus first-class identity/count/signature/time-bound properties for inspection.

    See `.agent/plans/menhir-scalar-history-projection-plan.md`.
    """

    name = "scalar_history"
    lww_register = False  # set semantics: signature-driven supersession, not LWW

    read_fields = (
        "n.uuid AS uuid, n.view_subject AS subject, n.view_subject_uuid AS subject_uuid, "
        "n.ss_attribute AS attribute, n.ss_scope AS scope, n.ss_kind AS value_kind, "
        "n.ss_unit AS unit, n.sh_entry_count AS entry_count, "
        "n.sh_payload_entry_count AS payload_entry_count, "
        "n.sh_omitted_entry_count AS omitted_entry_count, "
        "n.sh_signature AS history_signature, n.sh_op_counts AS operation_counts, "
        "n.sh_first_valid_at AS first_valid_at, n.sh_last_valid_at AS last_valid_at, "
        "n.view_payload AS payload, toString(n.valid_at) AS valid_at"
    )

    @classmethod
    def _slot(cls, payload: dict[str, Any]) -> dict[str, str]:
        """Canonical slot identity — same validation as ScalarStateKind."""
        attribute = str(payload.get("attribute", "")).strip().lower()
        value_kind = str(payload.get("value_kind", "")).strip().lower()
        if not attribute:
            raise ValueError("scalar_history slot requires a non-empty attribute")
        if value_kind not in ScalarStateKind.VALUE_KINDS:
            raise ValueError(
                f"scalar_history value_kind {value_kind!r} not in {sorted(ScalarStateKind.VALUE_KINDS)}"
            )
        return {
            "attribute": attribute,
            "scope": str(payload.get("scope", "")).strip().lower(),
            "value_kind": value_kind,
            "unit": str(payload.get("unit", "") or "").strip().lower(),
        }

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        """Same canonical hash as scalar_state, but prefixed `sh_` so the two kinds never collide."""
        canon = json.dumps(
            self._slot(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return "sh_" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]

    def signature(self, payload: dict[str, Any]) -> str:
        """Projection signature: the history_signature computed by the pure builder over the
        ordered assertion identities. A change to any assertion (new, corrected, superseded)
        produces a new signature -> supersede."""
        return str(payload.get("history_signature") or "")

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        attribute = str(payload.get("attribute", "")).strip()
        scope = str(payload.get("scope", "")).strip()
        entry_count = int(payload.get("entry_count") or payload.get("history_entry_count") or 0)
        op_counts = payload.get("operation_counts") or {}
        first_at = str(payload.get("first_valid_at") or "")[:10]
        last_at = str(payload.get("last_valid_at") or "")[:10]

        human = attribute.replace("_", " ") or "value"
        scope_txt = f" ({scope})" if scope else ""
        subj = subject.strip()

        ops_txt = ", ".join(f"{v} {k}" for k, v in sorted(op_counts.items()) if v)

        name = (
            f"{subj}'s {human}{scope_txt} history: "
            f"{entry_count} assertion(s), {first_at} to {last_at}. "
            f"advisory scalar history — not an absolute current total."
        )
        summary = (
            f"{subj} — {human}{scope_txt} history: "
            f"{entry_count} assertion(s) [{ops_txt}] from {first_at} to {last_at}"
        )
        return name, summary

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        slot = self._slot(payload)
        entries = payload.get("entries") or []
        normalized_entries = [normalize_history_entry(e, index=i) for i, e in enumerate(entries)]
        entry_count = int(
            payload.get("entry_count")
            if payload.get("entry_count") is not None
            else payload.get("history_entry_count")
            if payload.get("history_entry_count") is not None
            else len(normalized_entries)
        )
        payload_entry_count = int(
            payload.get("payload_entry_count")
            if payload.get("payload_entry_count") is not None
            else len(normalized_entries)
        )
        omitted_entry_count = int(
            payload.get("omitted_entry_count")
            if payload.get("omitted_entry_count") is not None
            else max(0, entry_count - payload_entry_count)
        )
        if payload_entry_count != len(normalized_entries):
            raise ValueError(
                "scalar_history payload_entry_count must equal the embedded entry count"
            )
        if entry_count < payload_entry_count or omitted_entry_count != entry_count - payload_entry_count:
            raise ValueError(
                "scalar_history entry_count, payload_entry_count, and omitted_entry_count "
                "must describe one complete contributor set"
            )
        op_counts = payload.get("operation_counts") or {}

        # Serialize entries for the bounded recall payload.
        entries_json = json.dumps(
            normalized_entries,
            ensure_ascii=False,
        ) if normalized_entries else "[]"

        return {
            "view_value": float(entry_count),
            "view_payload": entries_json,
            "ss_attribute": slot["attribute"],
            "ss_scope": slot["scope"],
            "ss_kind": slot["value_kind"],
            "ss_unit": slot["unit"],
            "sh_entry_count": entry_count,
            "sh_payload_entry_count": payload_entry_count,
            "sh_omitted_entry_count": omitted_entry_count,
            "sh_signature": str(payload.get("history_signature") or ""),
            "sh_op_counts": json.dumps(op_counts, ensure_ascii=False),
            "sh_first_valid_at": str(payload.get("first_valid_at") or ""),
            "sh_last_valid_at": str(payload.get("last_valid_at") or ""),
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        op_counts_raw = row.get("operation_counts")
        if isinstance(op_counts_raw, str):
            try:
                op_counts_raw = json.loads(op_counts_raw)
            except (json.JSONDecodeError, TypeError):
                op_counts_raw = {}
        payload_raw = row.get("payload")
        entries = []
        if isinstance(payload_raw, str):
            try:
                entries = json.loads(payload_raw)
            except (json.JSONDecodeError, TypeError):
                entries = []
        payload_entry_count = (
            len(entries)
            if row.get("payload_entry_count") is None
            else int(row.get("payload_entry_count") or 0)
        )
        omitted_entry_count = (
            max(0, int(row.get("entry_count") or 0) - payload_entry_count)
            if row.get("omitted_entry_count") is None
            else int(row.get("omitted_entry_count") or 0)
        )
        return {
            "uuid": row.get("uuid"),
            "subject": row.get("subject"),
            "subject_uuid": row.get("subject_uuid"),
            "attribute": row.get("attribute"),
            "scope": row.get("scope"),
            "value_kind": row.get("value_kind"),
            "unit": row.get("unit"),
            "entry_count": row.get("entry_count"),
            "payload_entry_count": payload_entry_count,
            "omitted_entry_count": omitted_entry_count,
            "payload_truncated": omitted_entry_count > 0,
            "history_signature": row.get("history_signature"),
            "operation_counts": op_counts_raw or {},
            "first_valid_at": row.get("first_valid_at"),
            "last_valid_at": row.get("last_valid_at"),
            "entries": entries,
            "valid_at": row.get("valid_at"),
        }

    def episode_uuids(self, payload: dict[str, Any]) -> list[str]:
        return [str(u) for u in (payload.get("episode_uuids") or [])]

    def valid_at(self, payload: dict[str, Any]) -> str | None:
        return payload.get("last_valid_at") or payload.get("valid_at")


def normalize_history_entry(entry: Any, *, index: int | None = None) -> dict[str, Any]:
    """Normalize a HistoryEntry or arbitrary Mapping before any View/Cypher write.

    This is intentionally the one boundary shared by the JSON payload writer and the
    destructive HISTORY_ENTRY redraw.  In particular, validation happens before the redraw
    query can delete existing edges.
    """
    if isinstance(entry, Mapping):
        get = entry.get
    else:
        get = lambda name, default=None: getattr(entry, name, default)

    label = f"scalar_history entry {index}" if index is not None else "scalar_history entry"
    assertion_id = str(get("assertion_id", "") or "").strip()
    if not assertion_id:
        raise ValueError(f"{label} has a blank assertion_id")
    valid_at = str(get("valid_at", "") or "").strip()
    if not valid_at:
        raise ValueError(f"{label} has a blank valid_at")

    return {
        "assertion_id": assertion_id,
        "operation": str(get("operation", "") or "").strip(),
        "value": get("value"),
        "value_json": get("value_json"),
        "valid_at": valid_at,
        "episode_uuid": str(get("episode_uuid", "") or "").strip(),
        "turn_id": str(get("turn_id", "") or "").strip(),
        "evidence_tier": str(get("evidence_tier", "") or "").strip(),
        "stated_span": str(get("stated_span", "") or ""),
    }


def _history_entry_to_dict(entry: Any) -> dict[str, Any]:
    """Backward-compatible alias for the shared HistoryEntry normalizer."""
    return normalize_history_entry(entry)


# =============================================================================================
# ViewRepository — owns the SHARED shape; dispatches per-kind work to a ViewKind.
# =============================================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_episode_uuids(episode_uuids: Any) -> list[str]:
    """The durable provenance list's canonical form (plan D1): sorted, deduplicated, no blanks.
    Sorted+dedup is what makes `supporting_event_count` an exact, order-independent length and lets
    the Cypher-side union be compared against the Python-side one."""
    return sorted({str(u).strip() for u in (episode_uuids or []) if str(u or "").strip()})


def _checked_template(kind: ViewKind, subject: str, payload: dict[str, Any], summary: str,
                      n_eps: int) -> str | None:
    """The kind's summary template, but ONLY if it provably reconstructs the summary just rendered.

    The template mechanism assumes `_COUNT_TOKEN` appears exactly where the count goes and nowhere
    else. A subject or counter whose text literally contains the token would break that assumption
    and let Cypher rewrite part of the fact's own summary text. So the invariant is checked here
    rather than trusted: substitute the real count back in and require it to reproduce the rendered
    summary byte-for-byte. If it does not, the template is discarded -- the provenance refresh then
    leaves `summary`/`content` alone (a slightly stale count) instead of corrupting them."""
    tpl = kind.summary_template(subject, payload)
    if tpl is None:
        return None
    if tpl.replace(_COUNT_TOKEN, str(n_eps)) != summary:
        logger.warning(
            "view summary template does not reconstruct the summary for %r/%r; "
            "skipping count refresh (does the text contain %r?)",
            subject, kind.name, _COUNT_TOKEN,
        )
        return None
    return tpl


def _log_missing_episodes(node_uuid: str, stored: list[str], missing: list[str]) -> None:
    """One structured line per write when provenance points at episodes that no longer exist
    (plan D3). Missing UUIDs are KEPT and keep counting -- the fact really was supported by them --
    so this is the only place that gap becomes visible. Sample is bounded."""
    if not missing:
        return
    logger.info(
        "view provenance: %d/%d supporting episodes missing for view %s (sample=%s)",
        len(missing), len(stored), node_uuid, missing[:5],
    )


#: Tolerant Neo4j-timestamp parse. Single definition in `domain.temporal` -- do not re-inline it:
#: unparseable here makes `_is_older` fail open, silently disabling the LWW guard below.
_parse_dt = parse_iso8601


def _is_older(candidate: Any, current: Any) -> bool:
    """True iff `candidate` valid_at is strictly earlier than `current` valid_at. Both parsed
    tolerantly; a naive timestamp is treated as UTC. Unparseable on either side -> False (do not
    skip — fail open to the pre-guard behavior)."""
    a, b = _parse_dt(candidate), _parse_dt(current)
    if a is None or b is None:
        return False
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
        b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
    return a < b


def _fmt(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else str(v)


def _day(when: str) -> str:
    return str(when)[:10]


def _counter_summary(subject: str, payload: dict[str, Any], n_events: Any) -> str:
    """The counter's human-readable body. `n_events` is either the real supporting-event count or
    `_COUNT_TOKEN` (rendering a template Cypher fills in) — one formatter, so the summary written on
    the CREATE path and the one rewritten by the provenance refresh can never drift apart."""
    counter = str(payload["counter"]); value = float(payload["value"])
    valid_at = str(payload.get("valid_at") or "")
    return (f"{subject.strip()} — {counter.strip()} = {_fmt(value)} "
            f"(current as of {valid_at[:10]}; {n_events} supporting event(s))")


def _counter_retrieval_text(subject: str, counter: str, value: float) -> str:
    """Counter BM25/embedding surface. LEADS with a natural, answer-readable statement, THEN keeps
    the 'how many / how much' keywords for retrieval. The old lead — 'how many times {subject}
    {counter}: {value}' — read as telemetry: an answer A/B showed it ranked #1 yet the answer model
    could not use it ('how many times user playlists: 20' does not read as 'the user has 20
    playlists'), so a correct, top-ranked View still produced 'I don't know'. Leading with
    '{subject}'s {counter}: {value}' fixes readability without losing the query match."""
    human = counter.strip().replace("_", " ")
    v = _fmt(value)
    subj = subject.strip()
    return (f"{subj}'s {human}: {v}. {human} = {v} "
            f"(how many / how much {human}: {v}; {human} count is {v}).")


def _timeline_surface(subject: str, entries: list[dict[str, Any]]) -> str:
    """Timeline BM25/embedding surface — lexicalises the chronology so 'when did X …' and
    'what happened with X over time' queries match. Leads with subject + span, then events."""
    subj = subject.strip()
    head = f"timeline of {subj}: {len(entries)} event(s)"
    if entries:
        span = f" from {_day(entries[0]['when'])} to {_day(entries[-1]['when'])}"
        body = "; ".join(f"{_day(e['when'])} {str(e.get('what', '')).strip()}" for e in entries)
        return f"{head}{span} — {body}"
    return head


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce + sort entries ascending by `when`. Keeps only when/what/episode_uuid; drops
    entries without a `when`. Deterministic order is what makes the signature stable."""
    out: list[dict[str, Any]] = []
    for e in entries or []:
        when = e.get("when")
        if not when:
            continue
        out.append({"when": str(when), "what": str(e.get("what", "")).strip(),
                    "episode_uuid": (str(e["episode_uuid"]) if e.get("episode_uuid") else None)})
    out.sort(key=lambda x: (x["when"], x["what"]))
    return out


def _timeline_sig(entries: list[dict[str, Any]]) -> str:
    """Idempotency signature: stable hash of the ordered (when, what) pairs. Adding/removing/
    editing an event changes it -> supersede; re-running on the same events -> no-op."""
    basis = "|".join(f"{e['when']}~{e['what']}" for e in entries)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# Event-lane mode: predicate/domain scoped timeline entries. Pure + dependency-light. The legacy
# subject-only helpers above are left untouched; this slice adds its own normalizer and surface.
# ---------------------------------------------------------------------------------------------

#: The fixed, query-sufficient schema for one event-lane entry. Everything else is stripped.
_EVENT_ENTRY_ALLOWLIST = (
    "assertion_key", "source_key", "when", "what", "object_key", "object_uuid",
    "quote", "episode_uuid", "turn_evidence_uuid", "time_basis", "evidence_tier",
)

#: Fields that must be present and nonblank for an event entry to be query-sufficient.
_EVENT_ENTRY_REQUIRED = (
    "assertion_key", "source_key", "when", "what", "object_key", "quote", "episode_uuid",
)


def _event_mode(payload: dict[str, Any]) -> bool:
    """Whether the timeline payload is in event-lane mode. Event mode is explicit via a nonblank
    `predicate`; `domain` is optional. Domain WITHOUT predicate must fail closed (a lane is
    predicate-scoped, so a domain with no predicate would silently over-merge lanes)."""
    predicate = (payload.get("predicate") or "").strip()
    domain = (payload.get("domain") or "").strip()
    if not predicate and domain:
        raise ValueError(
            "event timeline mode requires a non-blank predicate when domain is present"
        )
    return bool(predicate)


def _event_lane_suffix(predicate: Any, domain: Any) -> str:
    """Deterministic, collision-safe discriminator for one predicate/domain event lane.

    Encodes the normalized (lowercased, trimmed) predicate and optional domain after a
    `timeline:event:` prefix using a length-prefixed, percent-encoded scheme. Percent-encoding
    removes every delimiter-ambiguous byte (colons, '%', '/', ...) from the segments, and the
    leading decimal length disambiguates the segment boundary even in principle — so raw colon
    concatenation collisions (e.g. predicate 'a' + domain 'b:c' vs predicate 'a:b' + domain 'c')
    cannot occur."""
    pred = (predicate or "").strip().lower()
    if not pred:
        raise ValueError("event timeline lane requires a non-blank predicate")
    dom = (domain or "").strip().lower()

    def _seg(text: str) -> str:
        enc = urllib.parse.quote(text, safe="")
        return f"{len(enc)}:{enc}"

    suffix = f"timeline:event:{_seg(pred)}"
    if dom:
        suffix += f":{_seg(dom)}"
    return suffix


def _event_sig(predicate: Any, domain: Any, entries: list[dict[str, Any]]) -> str:
    """Event-lane idempotency signature: covers predicate/domain AND the full normalized entry
    payload. Any change to a quote, object, provenance, source time, or the lane itself yields a
    new projection version; the legacy subject-only `_timeline_sig` is unchanged for legacy rows."""
    pred = (predicate or "").strip().lower()
    dom = (domain or "").strip().lower()
    basis = json.dumps(
        {"predicate": pred, "domain": dom, "entries": entries},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _normalize_event_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedicated event-lane normalizer (never the legacy `_normalize_entries`).

    For each entry: validates the required nonblank fields, parses `when` through
    `menhir.domain.temporal.parse_iso8601` (invalid/unparseable world time -> the entry is excluded
    from this disposable View; ingest time is never used), canonicalizes accepted times to UTC ISO
    with 'Z', and keeps ONLY the fixed allowlist. Exact replays (same `assertion_key`) are
    deduplicated deterministically and the result is sorted ascending by parsed world time, then
    `assertion_key` — independent of input order."""
    def _get(name: str, default: Any = None) -> Any:
        return getattr(e, name, default)

    out: list[dict[str, Any]] = []
    for e in entries or []:
        if isinstance(e, Mapping):
            get = e.get
        else:
            get = _get
        missing = [f for f in _EVENT_ENTRY_REQUIRED
                   if not str(get(f, "") or "").strip()]
        if missing:
            raise ValueError(
                f"event timeline entry missing required field(s): {', '.join(missing)}"
            )
        when_dt = parse_iso8601(get("when"))
        if when_dt is None:
            continue  # invalid/unparseable world time -> excluded (auditable later, not here)
        norm: dict[str, Any] = {
            "when": when_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for f in _EVENT_ENTRY_ALLOWLIST:
            if f == "when":
                continue
            v = get(f)
            if f in ("object_uuid", "turn_evidence_uuid"):
                norm[f] = str(v) if v else None
            elif isinstance(v, str):
                norm[f] = v.strip()
            else:
                norm[f] = v
        out.append(norm)
    # Representative selection must be TOTAL and input-order independent. Exact replays share an
    # assertion_key (and therefore, by construction, `when`), but can still differ in quote/
    # metadata — so sorting on (when, assertion_key) alone is a partial order whose tie is resolved
    # by Python's stable sort = INPUT order. Break the tie with the canonical JSON of the FULL
    # normalized entry, then keep the first (minimum) representative per assertion_key.
    out.sort(key=lambda x: (
        x["when"], x["assertion_key"],
        json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    ))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in out:
        if entry["assertion_key"] in seen:
            continue
        seen.add(entry["assertion_key"])
        deduped.append(entry)
    # Final ordering is (world time, assertion_key) regardless of the representative tie-break.
    deduped.sort(key=lambda x: (x["when"], x["assertion_key"]))
    return deduped


def _event_surface(subject: str, predicate: Any, domain: Any,
                   entries: list[dict[str, Any]]) -> str:
    """Event-lane BM25/embedding surface. Uses occurrence/history language (never current-state or
    ownership/supersession) and makes the predicate, optional domain, objects, source dates, and
    quotes readable so 'when did X <predicate>' queries match."""
    subj = subject.strip()
    pred = (predicate or "").strip()
    dom = (domain or "").strip()
    dom_txt = f" in domain {dom}" if dom else ""
    head = f"history of {subj}: {pred}{dom_txt} ({len(entries)} recorded occurrence(s))"
    if not entries:
        return head
    parts: list[str] = []
    for e in entries:
        obj = str(e.get("object_key", "")).strip()
        when = _day(e["when"])
        if obj:
            parts.append(f"{when}: {e['what']} ({obj})")
        else:
            parts.append(f"{when}: {e['what']}")
    return f"{head} — " + "; ".join(parts)


def _render_event_timeline(subject: str, predicate: Any, domain: Any,
                           entries: list[dict[str, Any]]) -> str:
    """Event-lane human-readable body. Occurs/history wording only; exposes predicate, optional
    domain, objects, source dates, and quotes without claiming current ownership/supersession."""
    subj = subject.strip()
    pred = (predicate or "").strip()
    dom = (domain or "").strip()
    dom_txt = f", domain: {dom}" if dom else ""
    lines = [f"Occurrence history — {subj}: {pred}{dom_txt} ({len(entries)} occurrence(s)):"]
    for e in entries:
        when = _day(e["when"])
        obj = str(e.get("object_key", "")).strip()
        src = str(e.get("source_key", "")).strip()
        q = str(e.get("quote", "")).strip()
        if len(q) > 60:
            q = q[:57] + "..."
        bits = []
        if obj:
            bits.append(f"object {obj}")
        if src:
            bits.append(f"source {src}")
        if q:
            bits.append(f"\"{q}\"")
        line = f"  {when}: {e['what']}"
        lines.append(line + (f" — {', '.join(bits)}" if bits else ""))
    return "\n".join(lines)


def _render_timeline(subject: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"Timeline — {subject.strip()} ({len(entries)} event(s)):"]
    lines += [f"  {_day(e['when'])}: {e['what']}" for e in entries]
    return "\n".join(lines)
