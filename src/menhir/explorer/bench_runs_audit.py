"""Telemetry audit reader and live-graph post-processing for the bench-run explorer.

Ported from ``scalar_viewer.py`` without the archolith_bench import; extracted from
``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .bench_runs_io import _safe_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telemetry audit reader (ported from scalar_viewer.py, no archolith_bench import)
# ---------------------------------------------------------------------------


def _normalized_slot(values: list[Any] | tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(value or "").strip().lower() for value in values)


def _assertion_slot(assertion: dict[str, Any]) -> tuple[str, ...]:
    return _normalized_slot([
        assertion.get("subject_uuid"),
        assertion.get("attribute"),
        assertion.get("scope"),
        assertion.get("value_kind"),
        assertion.get("unit"),
    ])


def _audit_slot(event: dict[str, Any]) -> tuple[str, ...] | None:
    details = event.get("details") or {}
    raw_slot = details.get("slot")
    if not isinstance(raw_slot, (list, tuple)):
        return None
    if len(raw_slot) == 5:
        return _normalized_slot(raw_slot)
    if len(raw_slot) == 4 and details.get("subject_uuid"):
        return _normalized_slot([details["subject_uuid"], *raw_slot])
    return None


def _read_audit(
    telemetry_db: Path,
    namespace: str,
    assertions: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    if not telemetry_db.exists():
        return None, [], None
    uri = telemetry_db.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            rows = conn.execute(
                """
                SELECT id, recorded_at, event, status, episode_uuid, details_json
                FROM lifecycle_events
                WHERE phase = 'consolidation_audit'
                  AND json_extract(details_json, '$.namespace') = ?
                ORDER BY id
                """,
                (namespace,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Could not read telemetry audit: %s", exc)
        return None, [], "Telemetry audit could not be read."

    assertion_ids = {str(a.get("id") or "") for a in assertions}
    source_keys = {str(a.get("source_key") or "") for a in assertions}
    parsed: list[dict[str, Any]] = []
    relevant_passes: set[str] = set()
    pass_last_id: dict[str, int] = {}
    for row_id, at, event, state, pass_id, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            details = {"_raw": details_json}
        pid = str(pass_id or "")
        parsed.append({
            "id": int(row_id),
            "recorded_at": at,
            "event": event,
            "state": state,
            "pass_id": pid,
            "details": details,
        })
        pass_last_id[pid] = int(row_id)
        if (
            str(details.get("assertion_id") or "") in assertion_ids
            or str(details.get("source_key") or "") in source_keys
        ):
            relevant_passes.add(pid)

    if not relevant_passes:
        return None, [], (
            "Audit events exist for this namespace, but none match the assertions in this graph. "
            "The graph and telemetry may come from different benchmark attempts."
        )
    pass_id = max(relevant_passes, key=lambda pid: pass_last_id.get(pid, -1))
    events = [
        {
            "recorded_at": row["recorded_at"],
            "event": row["event"],
            "state": row["state"],
            "details": row["details"],
        }
        for row in parsed
        if row["pass_id"] == pass_id
    ]
    return pass_id, events, None


def annotate_assertion_fold_outcomes(
    assertions: list[dict[str, Any]],
    views: list[dict[str, Any]],
    history_views: list[dict[str, Any]],
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Explain each assertion's state and history projection outcomes."""
    current_state_ids: set[str] = set()
    historical_state_ids: set[str] = set()
    for view in views:
        target = current_state_ids if view.get("current") else historical_state_ids
        target.update(str(value) for value in view.get("contributor_ids") or [] if value)

    current_history_ids: set[str] = set()
    historical_history_ids: set[str] = set()
    for view in history_views:
        target = current_history_ids if view.get("current") else historical_history_ids
        target.update(str(value) for value in view.get("contributor_ids") or [] if value)
        target.update(
            str(entry.get("assertion_id"))
            for entry in view.get("entries") or []
            if entry.get("assertion_id")
        )

    state_events: dict[tuple[str, ...], dict[str, str]] = {}
    history_events: dict[tuple[str, ...], dict[str, str]] = {}
    for event in audit:
        slot = _audit_slot(event)
        event_name = str(event.get("event") or "")
        event_state = str(event.get("state") or "")
        details = event.get("details") or {}
        if slot and event_name == "fold" and event_state in {"abstain", "expiry"}:
            state_events[slot] = {
                "status": "abstained" if event_state == "abstain" else "expired",
                "reason": str(details.get("reason") or event_state),
            }
        elif slot and event_name == "view_write" and event_state == "stale_skipped":
            state_events[slot] = {
                "status": "write_failed",
                "reason": "stale scalar_state write was skipped",
            }
        elif slot and event_name == "history_fold" and event_state == "abstain":
            history_events[slot] = {
                "status": "abstained",
                "reason": str(details.get("reason") or event_state),
            }
        elif event_name == "history_rebuild" and event_state == "incomplete":
            for failure in details.get("failed_slots") or []:
                failed_slot = failure.get("slot_key")
                if isinstance(failed_slot, (list, tuple)) and len(failed_slot) == 5:
                    history_events[_normalized_slot(failed_slot)] = {
                        "status": "write_failed",
                        "reason": str(failure.get("error") or "scalar_history write failed"),
                    }

    annotated: list[dict[str, Any]] = []
    for assertion in assertions:
        assertion_id = str(assertion.get("id") or "")
        slot = _assertion_slot(assertion)
        pending = bool(assertion.get("binding_pending"))

        if pending:
            state = {"status": "not_folded", "reason": "subject binding is pending"}
            history = {"status": "not_folded", "reason": "subject binding is pending"}
        else:
            state = state_events.get(slot)
            if assertion_id in current_state_ids:
                state = {"status": "current", "reason": "contributes to the current scalar_state view"}
            elif state is None and assertion_id in historical_state_ids:
                state = {"status": "historical", "reason": "contributed to a superseded scalar_state view"}
            elif state is None and assertion.get("superseded"):
                state = {"status": "superseded", "reason": "assertion is superseded"}
            elif state is None:
                state = {
                    "status": "not_materialized",
                    "reason": (
                        "recorded in scalar_history but not used by the current scalar_state fold"
                        if assertion_id in current_history_ids
                        else "no scalar_state fold outcome was recorded"
                    ),
                }

            history = history_events.get(slot)
            if assertion_id in current_history_ids:
                history = {"status": "recorded", "reason": "recorded in the current scalar_history view"}
            elif history is None and assertion_id in historical_history_ids:
                history = {"status": "historical", "reason": "recorded in a superseded scalar_history view"}
            elif history is None:
                history = {
                    "status": "not_materialized",
                    "reason": "no scalar_history fold outcome was recorded",
                }

        annotated.append({
            **assertion,
            "fold_outcome": {
                "state": state,
                "history": history,
            },
        })
    return annotated


# ---------------------------------------------------------------------------
# Memory inventory builder
# ---------------------------------------------------------------------------


def _build_memory_inventory(
    assertions: list[dict[str, Any]],
    views: list[dict[str, Any]],
    history_views: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    event_views: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    operations_by_assertion = {
        str(assertion.get("id") or ""): str(assertion.get("operation") or "")
        for assertion in assertions
    }
    inventory: list[dict[str, Any]] = []
    for view_kind, rows in (
        ("scalar_state", views),
        ("scalar_history", history_views),
    ):
        for view in rows:
            op_counts = view.get("op_counts") or {}
            if view_kind == "scalar_history" and isinstance(op_counts, dict):
                operations = set(op_counts.keys())
            else:
                operations = {
                    operations_by_assertion.get(str(assertion_id), "")
                    for assertion_id in view.get("contributor_ids") or []
                }
            operations.discard("")
            if operations == {"absolute"}:
                derivation = "absolute"
            elif operations == {"delta"}:
                derivation = "delta"
            elif operations:
                derivation = "mixed"
            else:
                derivation = "unknown"
            inventory.append({
                "id": str(view.get("id") or ""),
                "memory_type": "view",
                "view_kind": view_kind,
                "derivation": derivation,
                "operations": sorted(operations),
                "current": bool(view.get("current")),
                "subject": str(view.get("subject") or ""),
                "attribute": str(view.get("attribute") or ""),
                "scope": str(view.get("scope") or ""),
                "value": str(view.get("display") or view.get("value") or ""),
                "content": str(view.get("summary") or ""),
                "valid_at": view.get("valid_at"),
            })
    for view in event_views or []:
        inventory.append({
            "id": str(view.get("id") or ""),
            "memory_type": "view",
            "view_kind": "event_history",
            "derivation": "event",
            "operations": ["occurrence"],
            "current": bool(view.get("current")),
            "subject": str(view.get("subject") or ""),
            "attribute": str(view.get("predicate") or ""),
            "scope": str(view.get("domain") or ""),
            "value": f"{_safe_int(view.get('entry_count'))} occurrence(s)",
            "content": "",
            "valid_at": view.get("valid_at"),
        })
    for index, fact in enumerate(facts):
        inventory.append({
            "id": f"content:{index}",
            "memory_type": "content",
            "view_kind": None,
            "derivation": None,
            "operations": [],
            "current": None,
            "subject": str(fact.get("subject") or ""),
            "relation": str(fact.get("relation") or ""),
            "object": str(fact.get("object") or ""),
            "content": str(fact.get("fact") or ""),
            "episode_ids": list(fact.get("episode_ids") or []),
            "valid_at": fact.get("valid_at"),
            "learned_at": fact.get("learned_at"),
        })
    return inventory
