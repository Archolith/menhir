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
from menhir.domain.typed_assertion import VALUE_KINDS as DOMAIN_VALUE_KINDS, normalize_scalar

try:  # neo4j is a hard runtime dep; guard the import so unit imports without the driver still load.
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except Exception:  # pragma: no cover - driver always present in the running service
    _Neo4jConstraintError = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Facade module: the ViewKind contract, the concrete counter/timeline/admission-audit kinds,
# and the scalar value/display helpers live in the `view_models_*` sibling modules; everything
# is re-exported here so every existing import site of `view_models` keeps working unchanged.
from menhir.infrastructure.view_models_base import (
    _CLASS_LABELS,
    _COUNT_TOKEN,
    _SHARED_STAMPS,
    _checked_template,
    _is_older,
    _label_for,
    _log_missing_episodes,
    _normalize_episode_uuids,
    _now,
    _parse_dt,
    ViewAudience,
    ViewClass,
    ViewKind,
)
from menhir.infrastructure.view_models_kinds import (
    AdmissionAuditKind,
    CounterKind,
    TimelineKind,
    _EVENT_ENTRY_ALLOWLIST,
    _EVENT_ENTRY_REQUIRED,
    _counter_retrieval_text,
    _counter_summary,
    _day,
    _event_lane_suffix,
    _event_mode,
    _event_sig,
    _event_surface,
    _fmt,
    _normalize_entries,
    _normalize_event_entries,
    _render_event_timeline,
    _render_timeline,
    _timeline_sig,
    _timeline_surface,
)
from menhir.infrastructure.view_models_scalar import (
    _duration_seconds_endpoint_display,
    _scalar_display,
    _scalar_norm,
)


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

    #: the typed ValueKinds a scalar_state slot may carry (fail-closed allowlist). Single-sourced
    #: from the domain's VALUE_KINDS — domain owns the allowlist; this class only consumes it.
    VALUE_KINDS = DOMAIN_VALUE_KINDS

    def audience(self, payload: dict[str, Any]) -> ViewAudience:
        return ViewAudience.RECALL

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
        elif isinstance(value, (int, float, Decimal)):
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

    def audience(self, payload: dict[str, Any]) -> ViewAudience:
        """History is operator-only unless the writer explicitly opts this payload into recall."""
        return (
            ViewAudience.RECALL
            if payload.get("recallable") is True
            else ViewAudience.OPERATOR
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
