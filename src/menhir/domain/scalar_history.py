"""Pure scalar-history projection — slot-keyed, ordered, lossless-enough advisory history.

Given the CURRENT materializable assertions for one entity and namespace, produces one
`ScalarHistoryProjection` per canonical slot containing at least one valid assertion.
Each projection is an ordered, chronological sequence of the slot's assertions —
delta, absolute, correction, or expiry — with typed values, provenance IDs, and
source/world timestamps intact.

Pure and offline: no Neo4j, no LLM. The same function backs both the live projection
(at ingest) and replay (from the durable log), so parity is by construction.

**Advisory only.** A scalar_history View:
  - preserves the ordered typed history for recall and dashboard inspection;
  - never enters the scalar authority lane;
  - never suppresses raw evidence;
  - never computes an absolute total from unanchored deltas;
  - never treats the latest delta as an absolute current value.

The existing `scalar_state` View remains the authoritative current-state projection
and continues to abstain when a slot has no absolute anchor.

See `.agent/plans/menhir-scalar-history-projection-plan.md`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import normalize_scalar

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_parse_dt = parse_iso8601


@dataclass(frozen=True)
class HistoryEntry:
    """One ordered entry in a scalar history: a single TypedAssertion with its provenance."""

    assertion_id: str
    operation: str              # absolute | delta | correction | expire
    value: str                  # normalized string representation
    value_json: Any             # typed value (int, float, bool, str)
    valid_at: str               # source/world time (ISO-8601)
    episode_uuid: str           # source episode
    turn_id: str                # TurnEvidence turn_id (may be empty if not yet wired)
    evidence_tier: str          # user | agent | inferred
    stated_span: str            # original phrasing from source


@dataclass(frozen=True)
class ScalarHistoryProjection:
    """One slot's ordered assertion history, ready to be persisted as a scalar_history View."""

    subject_uuid: str
    subject_display: str
    attribute: str
    scope: str
    value_kind: str
    unit: str

    entries: list[HistoryEntry]         # bounded payload for recall (last max_entries)
    all_entries: list[HistoryEntry]      # full ordered set for provenance edges

    # Deterministic metadata derived from the FULL entry set (not bounded).
    history_signature: str          # hash of ordered assertion identities + semantic fields
    operation_counts: dict[str, int]   # e.g. {"delta": 2, "absolute": 1}
    first_valid_at: str
    last_valid_at: str
    episode_uuids: list[str]        # deduped, order-stable

    @property
    def slot_key(self) -> tuple[str, str, str, str, str]:
        return (self.subject_uuid, self.attribute, self.scope, self.value_kind, self.unit)

    @property
    def entry_count(self) -> int:
        """Complete current contributor count, not the bounded recall payload length."""
        return len(self.all_entries)

    @property
    def payload_entry_count(self) -> int:
        """Number of entries embedded in the bounded recall payload."""
        return len(self.entries)

    @property
    def omitted_entry_count(self) -> int:
        """Number of complete contributors omitted from the bounded payload."""
        return max(0, self.entry_count - self.payload_entry_count)

    @property
    def total_entry_count(self) -> int:
        """Full history length (for provenance and operation_counts consistency)."""
        return len(self.all_entries)


@dataclass(frozen=True)
class HistoryAbstention:
    """A slot for which history cannot be materialized."""
    slot_key: tuple[str, str, str, str, str]
    reason: str
    subject_uuid: str
    malformed_assertion_ids: tuple[str, ...] = ()


# Abstention reason codes (stable, for telemetry).
NO_VALID_ENTRIES = "no_valid_entries"
MALFORMED_VALID_AT = "malformed_valid_at"


@dataclass
class HistoryResult:
    """The output of `build_history`: projections + abstentions."""
    projections: list[ScalarHistoryProjection] = field(default_factory=list)
    abstentions: list[HistoryAbstention] = field(default_factory=list)


def _slot_of(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("subject_uuid", "")).strip(),
        str(row.get("attribute", "")).strip().lower(),
        str(row.get("scope", "")).strip().lower(),
        str(row.get("value_kind", "")).strip().lower(),
        str(row.get("unit", "") or "").strip().lower(),
    )


def _history_signature(entries: list[HistoryEntry]) -> str:
    """Deterministic signature from the ordered assertion identity and semantic fields.

    Keyed on: assertion_id, operation, normalized value, valid_at. A change to any of
    these (a new assertion, a correction, a supersession) produces a new signature.
    """
    parts: list[str] = []
    for e in entries:
        parts.append(f"{e.assertion_id}|{e.operation}|{e.value}|{e.valid_at}")
    basis = "\n".join(parts)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _make_entry(row: dict[str, Any]) -> HistoryEntry | None:
    """Build a HistoryEntry from an assertion row. Returns None if valid_at is missing/malformed
    (fail-closed: the plan requires missing valid_at to produce an abstention/repair receipt
    rather than silently substituting ingest time)."""
    valid_at_raw = row.get("valid_at")
    parsed = _parse_dt(valid_at_raw)
    if parsed is None:
        return None  # fail-closed: no valid_at -> cannot order canonically

    return HistoryEntry(
        assertion_id=str(row.get("assertion_id") or ""),
        operation=str(row.get("operation") or ""),
        value=normalize_scalar(row.get("value")),
        value_json=row.get("value"),
        valid_at=str(valid_at_raw or ""),
        episode_uuid=str(row.get("episode_uuid") or ""),
        turn_id=str(row.get("turn_id") or ""),
        evidence_tier=str(row.get("evidence_tier") or "agent"),
        stated_span=str(row.get("stated_span") or ""),
    )


def build_history(
    rows: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
    max_entries: int = 16,
) -> HistoryResult:
    """Build ordered scalar history projections from materializable assertion rows.

    Groups by canonical slot, orders by (valid_at, assertion_id), and produces one
    `ScalarHistoryProjection` per slot that has at least one valid entry.

    `as_of` filters: assertions with valid_at > as_of are excluded (same semantics as
    the scalar_state fold). `None` = no time filter (pure set projection).

    `max_entries` bounds the entries list on the projection for recall payload sizing.
    The complete history is always available via the provenance edges; this bounds only
    the View's embedded payload.

    Pure: no Neo4j, no LLM.
    """
    # as-of activation filter (same semantics as scalar_state_fold.fold_assertions).
    if as_of is not None:
        # Malformed source times remain members of their slot so they cannot be silently
        # filtered out by the activation lens.  A slot with one malformed current member must
        # abstain as a whole rather than materializing a partial ordered history.
        rows = [
            r for r in rows
            if _parse_dt(r.get("valid_at")) is None
            or (_parse_dt(r.get("valid_at")) <= as_of)
        ]

    # Group by canonical slot.
    slots: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        slots.setdefault(_slot_of(row), []).append(row)

    result = HistoryResult()

    for slot_key in sorted(slots):  # sorted -> deterministic output order
        subject_uuid, attribute, scope, value_kind, unit = slot_key
        members = slots[slot_key]
        subject_uuid = str(members[0].get("subject_uuid", subject_uuid))

        # Build entries, retaining the identity of every malformed source-time row so the
        # caller can create a durable repair receipt instead of presenting a partial history.
        valid_entries: list[HistoryEntry] = []
        malformed_assertion_ids: list[str] = []
        for m in members:
            entry = _make_entry(m)
            if entry is not None:
                valid_entries.append(entry)
            else:
                malformed_assertion_ids.append(str(m.get("assertion_id") or ""))

        if malformed_assertion_ids:
            result.abstentions.append(
                HistoryAbstention(
                    slot_key,
                    MALFORMED_VALID_AT,
                    subject_uuid,
                    tuple(malformed_assertion_ids),
                )
            )
            continue

        if not valid_entries:
            result.abstentions.append(
                HistoryAbstention(slot_key, NO_VALID_ENTRIES, subject_uuid))
            continue

        # Order by (valid_at, assertion_id) — the plan's canonical ordering.
        valid_entries.sort(key=lambda e: (
            _parse_dt(e.valid_at) or _EPOCH,
            e.assertion_id,
        ))

        # Operation counts.
        op_counts: dict[str, int] = {}
        for e in valid_entries:
            op_counts[e.operation] = op_counts.get(e.operation, 0) + 1

        # Episode UUIDs — deduped, order-stable by first appearance.
        episode_uuids: list[str] = []
        for e in valid_entries:
            if e.episode_uuid and e.episode_uuid not in episode_uuids:
                episode_uuids.append(e.episode_uuid)

        # First/last valid_at.
        first_valid_at = valid_entries[0].valid_at
        last_valid_at = valid_entries[-1].valid_at

        # Signature over the full ordered set (not bounded).
        sig = _history_signature(valid_entries)

        # Bound the entries for the recall payload (keep the latest entries).
        bounded_limit = max(0, int(max_entries))
        bounded = (
            valid_entries[-bounded_limit:]
            if bounded_limit and len(valid_entries) > bounded_limit
            else valid_entries if bounded_limit else []
        )

        # Display comes from the first member's subject_display (deterministic: sorted slot).
        display = str(members[0].get("subject_display", "") or subject_uuid)

        result.projections.append(ScalarHistoryProjection(
            subject_uuid=subject_uuid,
            subject_display=display,
            attribute=attribute,
            scope=scope,
            value_kind=value_kind,
            unit=unit,
            entries=bounded,
            all_entries=valid_entries,
            history_signature=sig,
            operation_counts=op_counts,
            first_valid_at=first_valid_at,
            last_valid_at=last_valid_at,
            episode_uuids=episode_uuids,
        ))

    return result
