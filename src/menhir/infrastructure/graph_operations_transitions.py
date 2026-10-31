"""State-machine transitions of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): the explicit PREPARED ->
COMMITTED / NEEDS_REVIEW / REVERSED / FAILED moves, the operator-only escape from
NEEDS_REVIEW, attempt recording, and the shared ``_transition`` guard that retires the
live-owner claim and releases participant locks. ``GraphOperationsJournal`` composes this
mixin; every method runs against the facade's ``self.db_path``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from menhir.infrastructure.graph_operations_model import (
    OPERATION_STATES,
    GraphOperationError,
    _LOCK_RELEASE_STATES,
)
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.clock import utc_now_iso as _utc_now_iso

_logger = logging.getLogger(__name__)


class StaleTransitionError(GraphOperationError):
    """A compare-and-set transition matched no row: the caller's expected state or claim is stale.

    The writer lost the race (a reconciler moved the row, or its claim was taken over). It must
    stop; it must not retry or release anything. Subclasses ``GraphOperationError`` so existing
    handlers of an illegal transition keep working.
    """


class _GraphOperationsTransitionsMixin:
    """Guarded state transitions for journal rows."""

    # ------------------------------------------------------------------ transitions
    def mark_committed(self, op_id: str, *, owner_token: str | None = None) -> None:
        """Move PREPARED -> COMMITTED. Only a PREPARED row may commit.

        Does NOT touch expected_after_sha256: the postcondition fingerprint is frozen at
        PREPARE (plan E1) and commit only verifies-then-transitions (plan E4).

        Every caller commits only after its own graph work, so a refused commit means that work
        may be in the graph while the journal says otherwise. CF-271 F2: leave a durable record.
        """
        try:
            self._transition(
                op_id,
                to_state="COMMITTED",
                allowed_from={"PREPARED"},
                set_committed_at=True,
                owner_token=owner_token,
            )
        except StaleTransitionError as exc:
            self._record_commit_lost(op_id, exc)
            raise

    def _record_commit_lost(self, op_id: str, exc: StaleTransitionError) -> None:
        """Best effort: never mask the StaleTransitionError the caller must see."""
        row: dict[str, Any] = {}
        try:
            row = dict(self.get(op_id) or {})  # type: ignore[attr-defined]
        except Exception:  # diagnostics only
            _logger.debug("could not read %s for the commit-refused record", op_id, exc_info=True)
        details = {
            "op_id": op_id,
            "operation_kind": row.get("operation_kind"),
            "journal_state": row.get("state"),
        }
        if details["journal_state"] == "COMMITTED":
            situation = (
                "another owner already committed this operation, so this writer's graph work may "
                "have been applied a second time"
            )
        else:
            situation = (
                f"the graph may hold this operation's after-state while the journal says "
                f"{details['journal_state']}"
            )
        _logger.error(
            "Graph operation commit refused after the caller's graph work; %s (CF-271 F2): "
            "op_id=%s kind=%s",
            situation, op_id, details["operation_kind"],
        )
        try:
            from menhir.infrastructure.telemetry.recorders import record_failure_event

            record_failure_event(
                operation="graph_operation_commit",
                failure_stage="commit_refused_after_graph_work",
                classification="manual_review",
                retryable=False,
                error_type=type(exc).__name__,
                error=str(exc),
                details=details,
            )
        except Exception:  # the log line above is the fallback record
            _logger.warning("could not persist the commit-refused event for %s", op_id, exc_info=True)

    def mark_needs_review(
        self,
        op_id: str,
        *,
        observed_error: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        """Flag drift: the graph did not match the expected before/after state.

        Only a PREPARED row may be quarantined: a COMMITTED row was verified, and a stale writer
        must not downgrade it (CF-271).
        """
        self._transition(
            op_id,
            to_state="NEEDS_REVIEW",
            allowed_from={"PREPARED"},
            last_error=observed_error,
            owner_token=owner_token,
        )

    def mark_reversed(self, op_id: str) -> None:
        """Mark a forward operation REVERSED after its reverse op committed."""
        self._transition(op_id, to_state="REVERSED", allowed_from={"COMMITTED"})

    def mark_failed(
        self, op_id: str, *, reason: str | None = None, owner_token: str | None = None
    ) -> None:
        """Terminal FAILED: the operation abstained and made NO graph mutation.

        Only a PREPARED row may fail: once a mutation has been verified and COMMITTED there is
        nothing to fail. Unlike NEEDS_REVIEW this does not fence the target -- the graph is untouched,
        so a later, legitimately-eligible attempt at the same target must be allowed to proceed.
        """
        self._transition(
            op_id,
            to_state="FAILED",
            allowed_from={"PREPARED"},
            last_error=reason,
            owner_token=owner_token,
        )

    def clear_needs_review(self, op_id: str, *, to_state: str) -> None:
        """Operator-only escape from NEEDS_REVIEW (Part E1: no automatic transition).

        Callers must be an explicit operator command, never a background job.
        """
        if to_state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown target state {to_state!r}")
        if to_state == "NEEDS_REVIEW":
            return  # already the only legal source state; nothing to clear
        self._transition(op_id, to_state=to_state, allowed_from={"NEEDS_REVIEW"})

    def record_attempt(
        self, op_id: str, *, error: str | None = None, owner_token: str | None = None
    ) -> bool:
        """Increment attempt_count and record the last error, without changing state.

        Only a PREPARED row is updated (and, when ``owner_token`` is given, only one whose claim is
        that token or ownerless). Returns False when the row exists but is no longer attemptable --
        a terminal row's ``last_error`` is adjudication evidence and a stale writer must not
        overwrite it. Raises only when the operation does not exist.
        """
        self._ensure_ready()
        now = _utc_now_iso()
        sql = (
            "UPDATE graph_operations "
            "SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ? "
            "WHERE op_id = ? AND state = 'PREPARED'"
        )
        params: list[Any] = [error, now, op_id]
        if owner_token is not None:
            sql += " AND (owner_token IS NULL OR owner_token = ?)"
            params.append(owner_token)
        with connect_telemetry_db(self.db_path) as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            if cur.rowcount == 1:
                return True
            exists = conn.execute(
                "SELECT 1 FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if exists is None:
                raise GraphOperationError(f"no operation {op_id!r}")
            return False

    def _transition(
        self,
        op_id: str,
        *,
        to_state: str,
        allowed_from: set[str],
        set_committed_at: bool = False,
        last_error: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        """Compare-and-set transition (CF-271).

        The UPDATE itself matches on ``op_id`` AND ``state IN allowed_from`` (and, when the caller
        holds a claim, the owner token while the row is PREPARED; an ownerless legacy row has no second
        owner to protect and stays exempt). Coordinators pass the token; ``_transition`` never
        defaults it. So a stale writer cannot
        overwrite a state a reconciler already moved. Zero matched rows changes nothing and
        releases nothing; it raises :class:`StaleTransitionError`. Lock release runs only in the
        same transaction as a successful update.
        """
        # Immutable-after-PREPARED fields (op_id, request_json, expected_after_sha256) are
        # never in the SET list here -- a transition only moves state and audit stamps.
        self._ensure_ready()
        now = _utc_now_iso()
        from_states = sorted(allowed_from)
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            sets = ["state = ?", "updated_at = ?"]
            params: list[Any] = [to_state, now]
            if set_committed_at:
                sets.append("committed_at = ?")
                params.append(now)
            if last_error is not None:
                sets.append("last_error = ?")
                params.append(last_error)
            if owner_token is not None:
                sets.append("resolved_by_token = ?")
                params.append(owner_token)
            # Retire the live-owner claim the moment the row leaves PREPARED (CF-20b). PREPARED is
            # the only state in which "someone is executing this right now" can be true, so any
            # transition out of it ends the claim.
            #
            # This deliberately differs from the participant fence below, which NEEDS_REVIEW keeps.
            # The two answer different questions: the fence protects the node from competing
            # writes while an operator adjudicates, whereas the owner claim only says whether a
            # writer is still mid-flight. Leaving a fresh-looking heartbeat on a quarantined row
            # would make it read as LIVE_OWNER indefinitely.
            # SET expressions read the pre-update row, so the CASE keys off the state we matched.
            sets.extend(
                [
                    "owner_token = CASE WHEN state = 'PREPARED' THEN NULL ELSE owner_token END",
                    "owner_heartbeat_at = CASE WHEN state = 'PREPARED' THEN NULL "
                    "ELSE owner_heartbeat_at END",
                    "owner_lease_expires_at = CASE WHEN state = 'PREPARED' THEN NULL "
                    "ELSE owner_lease_expires_at END",
                ]
            )
            where = f"op_id = ? AND state IN ({', '.join('?' for _ in from_states)})"
            params.append(op_id)
            params.extend(from_states)
            if owner_token is not None:
                where += " AND (state != 'PREPARED' OR owner_token IS NULL OR owner_token = ?)"
                params.append(owner_token)
            cursor = conn.execute(
                f"UPDATE graph_operations SET {', '.join(sets)} WHERE {where}", params
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT state, resolved_by_token FROM graph_operations WHERE op_id = ?",
                    (op_id,),
                ).fetchone()
                conn.rollback()
                if row is None:
                    raise GraphOperationError(f"no operation {op_id!r}")
                current = row["state"]
                resolved_by = row["resolved_by_token"]
                if current == to_state:
                    # CF-271 review F-2: a commit another owner made is not this writer's success.
                    # test_cf271_stale_commit_after_takeover exercises it. NULL (legacy or
                    # tokenless move) stays idempotent: there is no evidence of a second owner.
                    if (
                        to_state == "COMMITTED"
                        and owner_token is not None
                        and resolved_by is not None
                        and resolved_by != owner_token
                    ):
                        raise StaleTransitionError(
                            f"{op_id!r} is already COMMITTED by another owner; this writer's "
                            "commit is refused"
                        )
                    return  # idempotent
                if current in allowed_from:
                    raise StaleTransitionError(
                        f"transition {current} -> {to_state} for {op_id!r} refused: "
                        "owner token does not match the row's claim"
                    )
                raise StaleTransitionError(
                    f"illegal transition {current} -> {to_state} for {op_id!r}"
                )
            # Release the participant fence only on a terminal state (invariant 14). NEEDS_REVIEW
            # keeps the locks so a quarantined op still fences its participants; an operator clearing
            # NEEDS_REVIEW to a terminal state releases them here through the same path.
            if to_state in _LOCK_RELEASE_STATES:
                conn.execute(
                    "DELETE FROM graph_operation_locks WHERE op_id = ?", (op_id,)
                )
            conn.commit()
