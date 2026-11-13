"""Operator logic for stranded saga rows (#427 fix B). Plain dicts out, no Typer.

Every function takes the journal, the kind->handler mapping and the reconciliation gate as
parameters so tests can inject fakes. Writes happen only in ``attest``, ``resolve(apply=True)`` and
``clear_review``; ``list_operations``, ``show_operation`` and a dry-run ``resolve`` write nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations_model import GraphOperationError
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError
from menhir.infrastructure.telemetry.helpers import connect_telemetry_db
from menhir.services.saga_reconcile_outcomes import (
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    WOULD_RESTORE,
)

ERASURE_KIND = "EXPLICIT_ERASURE"
_DELETE_KINDS = frozenset({"ENTITY_DELETE", "SESSION_TTL_DELETE"})
_LIST_STATES = ("PREPARED", "NEEDS_REVIEW")


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refused": True, "reason": reason, **extra}


def _ownership(row: Mapping[str, Any]) -> str:
    return oo.classify_ownership(dict(row))


def _claim_history(journal: Any, op_id: str) -> list[dict[str, Any]]:
    with connect_telemetry_db(journal.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT claim_token, claim_kind, claimed_at FROM graph_operation_claims "
            "WHERE op_id = ? ORDER BY rowid",
            (op_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_operations(journal: Any, *, state: str | None = None) -> dict[str, Any]:
    states = (state,) if state else _LIST_STATES
    out: list[dict[str, Any]] = []
    for st in states:
        for row in journal.list_by_state(st):
            out.append(
                {
                    "op_id": row.get("op_id"),
                    "kind": row.get("operation_kind"),
                    "state": row.get("state"),
                    "ownership": _ownership(row),
                    "attempt_count": row.get("attempt_count"),
                    "last_error": row.get("last_error"),
                }
            )
    return {"ok": True, "operations": out}


def show_operation(journal: Any, handlers: Mapping[str, Any], op_id: str) -> dict[str, Any]:
    row = journal.get(op_id)
    if row is None:
        return _refuse(f"no such operation {op_id!r}")
    result: dict[str, Any] = {
        "ok": True,
        "row": row,
        "claims": _claim_history(journal, op_id),
        "ownership": _ownership(row),
        "verdict": None,
    }
    handler = handlers.get(row.get("operation_kind"))
    if row.get("state") == "PREPARED" and hasattr(handler, "classify_prepared_row"):
        outcome, diag = handler.classify_prepared_row(row)
        result["verdict"] = {"outcome": outcome, "diag": diag}
    return result


def attest(journal: Any, op_id: str, *, owner_token: str, by: str) -> dict[str, Any]:
    if not (by or "").strip():
        return _refuse("--by is required")
    if not (owner_token or "").strip():
        return _refuse("--owner-token is required")
    recorded = bool(
        journal.attest_owner_death(op_id, attested_by=by, expected_owner_token=owner_token)
    )
    if not recorded:
        return _refuse(
            "attestation not recorded (row not PREPARED, lease still live, or owner token "
            "differs from the one given)",
            op_id=op_id,
            recorded=False,
        )
    return {"ok": True, "op_id": op_id, "recorded": True}


def _is_before_state(kind: str, row: Mapping[str, Any], outcome: str, diag: Mapping[str, Any]) -> bool:
    if outcome in (WOULD_REPLAY, WOULD_RESTORE):
        return True
    if kind in _DELETE_KINDS and outcome == WOULD_NEEDS_REVIEW:
        # The delete handler reports "all targets still present" as NEEDS_REVIEW with every
        # target listed in ``survivors``. That is the before-state; a PARTIAL survivor list is not.
        try:
            targets = {str(t) for t in json.loads(row["request_json"]).get("targets") or []}
        except (TypeError, ValueError, KeyError, AttributeError):
            return False
        survivors = {str(s) for s in diag.get("survivors") or []}
        return bool(targets) and survivors == targets
    return False


def resolve(
    journal: Any,
    handlers: Mapping[str, Any],
    gate: Any,
    op_id: str,
    *,
    by: str,
    apply: bool = False,
) -> dict[str, Any]:
    if not (by or "").strip():
        return _refuse("--by is required")
    row = journal.get(op_id)
    if row is None:
        return _refuse(f"no such operation {op_id!r}")
    if row.get("state") != "PREPARED":
        return _refuse(f"row is {row.get('state')}, not PREPARED; use clear-review for NEEDS_REVIEW")
    token = row.get("owner_token")
    if not isinstance(token, str) or not token.strip():
        return _refuse("ownerless row: needs direct repair")
    kind = str(row.get("operation_kind"))
    if kind == ERASURE_KIND:
        return _refuse(
            "a partial erasure must be completed, not abandoned: use live replay "
            "(MENHIR_SAGA_RECONCILE_STARTUP_MODE=live) instead of resolve",
            kind=kind,
        )
    handler = handlers.get(kind)
    if not hasattr(handler, "classify_prepared_row"):
        return _refuse(f"no handler with classify_prepared_row for kind {kind!r}", kind=kind)

    ownership = _ownership(row)
    if ownership != oo.ABANDONED:
        return _refuse(
            f"ownership is {ownership}, not ABANDONED: attest the writer's death first "
            "(only true if that process is gone; restart the server for a row written by the "
            "running one), then resolve",
            ownership=ownership,
        )

    outcome, diag = handler.classify_prepared_row(row)
    planned = _planned_close(kind, row, by, outcome, diag)
    report: dict[str, Any] = {
        "ok": True,
        "op_id": op_id,
        "kind": kind,
        "outcome": outcome,
        "planned": planned["action"],
        "applied": False,
    }
    if not apply:
        return report

    if not gate.acquire():
        return _refuse("reconciliation gate is held by another process", op_id=op_id)
    try:
        claim = oo.new_claim_token()
        if not journal.claim_abandoned_operation(op_id, owner_token=claim):
            return _refuse("claim failed (row no longer abandoned or already claimed)", op_id=op_id)
        try:
            outcome, diag = handler.classify_prepared_row(journal.get(op_id) or row)
            planned = _planned_close(kind, row, by, outcome, diag)
            action = planned["action"]
            if action == "COMMITTED":
                journal.mark_committed(op_id, owner_token=claim, after_graph_work=False)
            elif action == "FAILED":
                journal.mark_failed(op_id, owner_token=claim, reason=planned["reason"])
            else:
                journal.mark_needs_review(
                    op_id, owner_token=claim, observed_error=planned["reason"]
                )
        except Exception as exc:  # noqa: BLE001 -- the claim must not strand the row
            # The row is now claimed by this short-lived process, and the claim cleared the
            # attestation: left PREPARED it would read as a live owner and block a live boot.
            error = f"operator {by}: resolve failed after the claim: {type(exc).__name__}: {exc}"
            try:
                journal.mark_needs_review(op_id, owner_token=claim, observed_error=error)
                quarantined = True
            except Exception:  # noqa: BLE001
                quarantined = False
            return _refuse(error, op_id=op_id, claim_token=claim, quarantined=quarantined)
        report.update(outcome=outcome, planned=action, applied=True, claim_token=claim)
        return report
    finally:
        gate.release()


def _planned_close(
    kind: str, row: Mapping[str, Any], by: str, outcome: str, diag: Mapping[str, Any]
) -> dict[str, str]:
    if outcome == WOULD_MARK_ALREADY_APPLIED:
        return {"action": "COMMITTED", "reason": ""}
    if _is_before_state(kind, row, outcome, diag):
        return {"action": "FAILED", "reason": f"operator {by} closed: graph at before-state"}
    return {
        "action": "NEEDS_REVIEW",
        "reason": f"operator {by}: {diag.get('observed_error') or outcome}",
    }


def clear_review(journal: Any, op_id: str, *, to_state: str, by: str) -> dict[str, Any]:
    if not (by or "").strip():
        return _refuse("--by is required")
    try:
        journal.clear_needs_review(op_id, to_state=to_state, operator=by)
    except (GraphOperationError, StaleTransitionError) as exc:
        return _refuse(str(exc), op_id=op_id, to_state=to_state)
    return {"ok": True, "op_id": op_id, "to_state": to_state, "operator": by}
