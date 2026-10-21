"""State-machine transitions of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): the explicit PREPARED ->
COMMITTED / NEEDS_REVIEW / REVERSED / FAILED moves, the operator-only escape from
NEEDS_REVIEW, attempt recording, and the shared ``_transition`` guard that retires the
live-owner claim and releases participant locks. ``GraphOperationsJournal`` composes this
mixin; every method runs against the facade's ``self.db_path``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from menhir.infrastructure.graph_operations_model import (
    OPERATION_STATES,
    GraphOperationError,
    _LOCK_RELEASE_STATES,
)
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.clock import utc_now_iso as _utc_now_iso


class _GraphOperationsTransitionsMixin:
    """Guarded state transitions for journal rows."""

    # ------------------------------------------------------------------ transitions
    def mark_committed(self, op_id: str) -> None:
        """Move PREPARED -> COMMITTED. Only a PREPARED row may commit.

        Does NOT touch expected_after_sha256: the postcondition fingerprint is frozen at
        PREPARE (plan E1) and commit only verifies-then-transitions (plan E4).
        """
        self._transition(
            op_id,
            to_state="COMMITTED",
            allowed_from={"PREPARED"},
            set_committed_at=True,
        )

    def mark_needs_review(self, op_id: str, *, observed_error: str | None = None) -> None:
        """Flag drift: the graph did not match the expected before/after state."""
        self._transition(
            op_id,
            to_state="NEEDS_REVIEW",
            allowed_from={"PREPARED", "COMMITTED"},
            last_error=observed_error,
        )

    def mark_reversed(self, op_id: str) -> None:
        """Mark a forward operation REVERSED after its reverse op committed."""
        self._transition(op_id, to_state="REVERSED", allowed_from={"COMMITTED"})

    def mark_failed(self, op_id: str, *, reason: str | None = None) -> None:
        """Terminal FAILED: the operation abstained and made NO graph mutation.

        Only a PREPARED row may fail: once a mutation has been verified and COMMITTED there is
        nothing to fail. Unlike NEEDS_REVIEW this does not fence the target -- the graph is untouched,
        so a later, legitimately-eligible attempt at the same target must be allowed to proceed.
        """
        self._transition(
            op_id, to_state="FAILED", allowed_from={"PREPARED"}, last_error=reason
        )

    def clear_needs_review(self, op_id: str, *, to_state: str) -> None:
        """Operator-only escape from NEEDS_REVIEW (Part E1: no automatic transition).

        Callers must be an explicit operator command, never a background job.
        """
        if to_state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown target state {to_state!r}")
        self._transition(op_id, to_state=to_state, allowed_from={"NEEDS_REVIEW"})

    def record_attempt(self, op_id: str, *, error: str | None = None) -> None:
        """Increment attempt_count and record the last error, without changing state."""
        self._ensure_ready()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE graph_operations
                SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ?
                WHERE op_id = ?
                """,
                (error, now, op_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise GraphOperationError(f"no operation {op_id!r}")

    def _transition(
        self,
        op_id: str,
        *,
        to_state: str,
        allowed_from: set[str],
        set_committed_at: bool = False,
        last_error: str | None = None,
    ) -> None:
        # Immutable-after-PREPARED fields (op_id, request_json, expected_after_sha256) are
        # never in the SET list here -- a transition only moves state and audit stamps.
        self._ensure_ready()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if row is None:
                raise GraphOperationError(f"no operation {op_id!r}")
            current = row["state"]
            if current == to_state:
                return  # idempotent
            if current not in allowed_from:
                raise GraphOperationError(
                    f"illegal transition {current} -> {to_state} for {op_id!r}"
                )
            sets = ["state = ?", "updated_at = ?"]
            params: list[Any] = [to_state, now]
            if set_committed_at:
                sets.append("committed_at = ?")
                params.append(now)
            if last_error is not None:
                sets.append("last_error = ?")
                params.append(last_error)
            # Retire the live-owner claim the moment the row leaves PREPARED (CF-20b). PREPARED is
            # the only state in which "someone is executing this right now" can be true, so any
            # transition out of it ends the claim.
            #
            # This deliberately differs from the participant fence below, which NEEDS_REVIEW keeps.
            # The two answer different questions: the fence protects the node from competing
            # writes while an operator adjudicates, whereas the owner claim only says whether a
            # writer is still mid-flight. Leaving a fresh-looking heartbeat on a quarantined row
            # would make it read as LIVE_OWNER indefinitely.
            if current == "PREPARED":
                sets.extend(
                    [
                        "owner_token = NULL",
                        "owner_heartbeat_at = NULL",
                        "owner_lease_expires_at = NULL",
                    ]
                )
            params.append(op_id)
            conn.execute(
                f"UPDATE graph_operations SET {', '.join(sets)} WHERE op_id = ?", params
            )
            # Release the participant fence only on a terminal state (invariant 14). NEEDS_REVIEW
            # keeps the locks so a quarantined op still fences its participants; an operator clearing
            # NEEDS_REVIEW to a terminal state releases them here through the same path.
            if to_state in _LOCK_RELEASE_STATES:
                conn.execute(
                    "DELETE FROM graph_operation_locks WHERE op_id = ?", (op_id,)
                )
            conn.commit()
