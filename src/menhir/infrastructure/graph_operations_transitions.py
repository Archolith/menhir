"""State-machine transitions of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): the explicit PREPARED ->
COMMITTED / NEEDS_REVIEW / REVERSED / FAILED moves, the operator-only escape from
NEEDS_REVIEW, attempt recording, the escalation of a commit refused after graph work, and the
shared ``_transition`` guard that retires the live-owner claim and releases participant locks.
``GraphOperationsJournal`` composes this mixin; every method runs against the facade's
``self.db_path``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from menhir.infrastructure.graph_operations_model import (
    OPERATION_STATES,
    GraphOperationError,
    _LOCK_RELEASE_STATES,
    _participants_from_request_json,
)
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.clock import utc_now_iso as _utc_now_iso

_logger = logging.getLogger(__name__)

#: Why a compare-and-set refused (FR-7). Enum-shaped so it survives telemetry redaction.
REFUSAL_COMMITTED_BY_OTHER = "committed_by_other_owner"
REFUSAL_OWNER_MISMATCH = "owner_mismatch"
REFUSAL_ILLEGAL_TRANSITION = "illegal_transition"

#: Outcomes of :meth:`_GraphOperationsTransitionsMixin.escalate_after_refused_commit` (#404).
ESCALATED = "escalated"
REFUSED_FENCE_TAKEN = "refused_fence_taken"
REFUSED_NOT_CLAIMANT = "refused_not_claimant"
REFUSED_STATE = "refused_state"

#: Prefix of the resolved_by_token an operator clear stamps (FR-3): never a writer's token.
OPERATOR_TOKEN_PREFIX = "operator:"


class StaleTransitionError(GraphOperationError):
    """A compare-and-set transition matched no row: the caller's expected state or claim is stale.

    The writer lost the race (a reconciler moved the row, or its claim was taken over). It must
    stop; it must not retry or release anything. Subclasses ``GraphOperationError`` so existing
    handlers of an illegal transition keep working. ``refusal`` says which of the three cases it
    was (FR-7).
    """

    def __init__(self, message: str, *, refusal: str = REFUSAL_OWNER_MISMATCH) -> None:
        super().__init__(message)
        self.refusal = refusal


def _require_token(owner_token: object, *, method: str) -> str:
    """Fail closed on a missing claim (#406 F-6): None used to mean "skip the owner check"."""
    if not isinstance(owner_token, str) or not owner_token.strip():
        raise GraphOperationError(
            f"{method} requires the caller's owner_token; an omitted or empty token would skip "
            "the ownership compare-and-set (CF-271, #406 F-6)"
        )
    return owner_token


def _record_journal_event(
    *, failure_stage: str, classification: str, error_type: str, error: str, details: dict
) -> None:
    """Best-effort durable record; the caller's log line is the fallback."""
    try:
        from menhir.infrastructure.telemetry.recorders import record_failure_event

        record_failure_event(
            operation="graph_operation_commit",
            failure_stage=failure_stage,
            classification=classification,
            retryable=False,
            error_type=error_type,
            error=error,
            details=details,
        )
    except Exception:  # noqa: BLE001 -- diagnostics must never mask the caller's outcome
        _logger.warning(
            "could not persist the %s event for %s", failure_stage, details.get("op_id"),
            exc_info=True,
        )


class _GraphOperationsTransitionsMixin:
    """Guarded state transitions for journal rows."""

    # ------------------------------------------------------------------ transitions
    def mark_committed(
        self, op_id: str, *, owner_token: str, after_graph_work: bool = True
    ) -> None:
        """Move PREPARED -> COMMITTED. Only a PREPARED row may commit.

        Does NOT touch expected_after_sha256: the postcondition fingerprint is frozen at
        PREPARE (plan E1) and commit only verifies-then-transitions (plan E4).

        ``after_graph_work`` says whether THIS caller's own statement mutated the graph (True) or it
        only observed an already-applied state (False, #404 F-8). It is stored on the row so a stale
        writer can tell "the committer did graph work too" from "the committer only observed mine".
        A refused commit after graph work leaves a durable record (CF-271 F2); one without graph
        work is only logged, because no graph state is in question.
        """
        token = _require_token(owner_token, method="mark_committed")
        try:
            self._transition(
                op_id,
                to_state="COMMITTED",
                allowed_from={"PREPARED"},
                set_committed_at=True,
                owner_token=token,
                commit_graph_work=after_graph_work,
            )
        except StaleTransitionError as exc:
            if after_graph_work:
                self._record_commit_lost(op_id, exc)
            else:
                _logger.warning(
                    "commit refused for %s with no graph work by this caller (%s): %s",
                    op_id, exc.refusal, exc,
                )
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
            "refusal": exc.refusal,
        }
        if exc.refusal == REFUSAL_COMMITTED_BY_OTHER:
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
            "op_id=%s kind=%s refusal=%s",
            situation, op_id, details["operation_kind"], exc.refusal,
        )
        _record_journal_event(
            failure_stage="commit_refused_after_graph_work",
            classification="manual_review",
            error_type=type(exc).__name__,
            error=str(exc),
            details=details,
        )

    def mark_needs_review(
        self,
        op_id: str,
        *,
        owner_token: str,
        observed_error: str | None = None,
    ) -> None:
        """Flag drift: the graph did not match the expected before/after state.

        Only a PREPARED row may be quarantined: a COMMITTED row was verified, and a stale writer
        must not downgrade it (CF-271). A refused commit after graph work reaches NEEDS_REVIEW
        through :meth:`escalate_after_refused_commit` instead.
        """
        self._transition(
            op_id,
            to_state="NEEDS_REVIEW",
            allowed_from={"PREPARED"},
            last_error=observed_error,
            owner_token=_require_token(owner_token, method="mark_needs_review"),
        )

    def mark_reversed(self, op_id: str, *, owner_token: str) -> None:
        """Mark a forward operation REVERSED after its reverse op committed.

        ``owner_token`` is the reverse op's claim, stamped as ``resolved_by_token`` so the row
        names who reversed it (FR-3). A COMMITTED row has no live owner, so it is not compared.
        """
        self._transition(
            op_id,
            to_state="REVERSED",
            allowed_from={"COMMITTED"},
            owner_token=_require_token(owner_token, method="mark_reversed"),
        )

    def mark_failed(
        self, op_id: str, *, owner_token: str, reason: str | None = None
    ) -> None:
        """Terminal FAILED: the operation made NO graph mutation.

        Callers must hold proof of that, not an assumption: an abstention re-read at the
        before-state, a server-reported rollback re-read at the before-state (#427), or an
        operator classification at the before-state.

        Only a PREPARED row may fail: once a mutation has been verified and COMMITTED there is
        nothing to fail. Unlike NEEDS_REVIEW this does not fence the target -- the graph is untouched,
        so a later, legitimately-eligible attempt at the same target must be allowed to proceed.
        """
        self._transition(
            op_id,
            to_state="FAILED",
            allowed_from={"PREPARED"},
            last_error=reason,
            owner_token=_require_token(owner_token, method="mark_failed"),
        )

    def clear_needs_review(self, op_id: str, *, to_state: str, operator: str) -> None:
        """Operator-only escape from NEEDS_REVIEW (Part E1: no automatic transition).

        Callers must be an explicit operator command, never a background job. The operator is
        named, and stamped as ``resolved_by_token = 'operator:<name>'`` so the row never keeps the
        token of the writer that quarantined it (FR-3, FR-4).

        PREPARED is refused as a target (#406 F-7): the claim was retired when the row left
        PREPARED, so re-opening it would create an ownerless PREPARED row that any token could
        terminate and that recovery could never claim.
        """
        if to_state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown target state {to_state!r}")
        if to_state == "NEEDS_REVIEW":
            return  # already the only legal source state; nothing to clear
        if to_state == "PREPARED":
            raise GraphOperationError(
                "clear_needs_review refuses PREPARED: a re-opened row would have no owner, so any "
                "writer could terminate it and recovery could never claim it (#406 F-7)"
            )
        name = str(operator or "").strip()
        if not name:
            raise GraphOperationError("clear_needs_review requires the operator's name")
        if to_state == "FAILED" and (self.get(op_id) or {}).get("operation_kind") == "EXPLICIT_ERASURE":
            # FAILED asserts "no mutation" and releases the fence, but a quarantined erasure has
            # already purged part of its subject: it must be completed, never abandoned.
            raise GraphOperationError(
                "clear_needs_review refuses FAILED for an erasure: complete it by live replay"
            )
        self._transition(
            op_id,
            to_state=to_state,
            allowed_from={"NEEDS_REVIEW"},
            owner_token=f"{OPERATOR_TOKEN_PREFIX}{name}",
        )

    def record_attempt(
        self, op_id: str, *, owner_token: str, error: str | None = None
    ) -> bool:
        """Increment attempt_count and record the last error, without changing state.

        Only a PREPARED row whose claim is that token (or an ownerless legacy row) is updated.
        Returns False when the row exists but is no longer attemptable -- a terminal row's
        ``last_error`` is adjudication evidence and a stale writer must not overwrite it. Raises
        only when the operation does not exist.
        """
        token = _require_token(owner_token, method="record_attempt")
        self._ensure_ready()
        now = _utc_now_iso()
        sql = (
            "UPDATE graph_operations "
            "SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ? "
            "WHERE op_id = ? AND state = 'PREPARED' "
            "  AND (owner_token IS NULL OR owner_token = ?)"
        )
        with connect_telemetry_db(self.db_path) as conn:
            cur = conn.execute(sql, (error, now, op_id, token))
            conn.commit()
            if cur.rowcount == 1:
                return True
            exists = conn.execute(
                "SELECT 1 FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if exists is None:
                raise GraphOperationError(f"no operation {op_id!r}")
            return False

    # ------------------------------------------------------------------ escalation (#404)
    def escalate_after_refused_commit(
        self, op_id: str, *, owner_token: str, observed_error: str
    ) -> str:
        """Quarantine a FAILED or COMMITTED row whose claim a writer's late graph work contradicts.

        The one path out of FAILED/COMMITTED into NEEDS_REVIEW (#404). Used when a writer's commit
        was refused after its own graph work landed: FAILED ("no mutation") is then false, and
        COMMITTED by an owner that also did graph work may hide a double apply.

        One ``BEGIN IMMEDIATE`` that fails closed on each condition:

        * the caller must have held a claim on THIS op (``graph_operation_claims``), so a stale or
          confused writer cannot reopen an unrelated row;
        * the row must be FAILED or COMMITTED;
        * the participant fence is re-taken -- FAILED/COMMITTED released it -- and the escalation
          is refused if another op now holds a participant, or if a newer unresolved op holds the
          same ``target_key`` (``ux_graph_ops_unresolved_key``).

        Returns one of ``ESCALATED``, ``REFUSED_FENCE_TAKEN``, ``REFUSED_NOT_CLAIMANT``,
        ``REFUSED_STATE``. Every outcome is logged and durably recorded.
        """
        token = _require_token(owner_token, method="escalate_after_refused_commit")
        self._ensure_ready()
        now = _utc_now_iso()
        outcome = REFUSED_STATE
        kind: str | None = None
        state: str | None = None
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state, operation_kind, request_json FROM graph_operations "
                    "WHERE op_id = ?",
                    (op_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise GraphOperationError(f"no operation {op_id!r}")
                state, kind, request_json = row[0], row[1], row[2]
                claimed = conn.execute(
                    "SELECT 1 FROM graph_operation_claims WHERE op_id = ? AND claim_token = ?",
                    (op_id, token),
                ).fetchone()
                if claimed is None:
                    outcome = REFUSED_NOT_CLAIMANT
                elif state not in ("FAILED", "COMMITTED"):
                    outcome = REFUSED_STATE
                else:
                    outcome = self._refence_and_quarantine(
                        conn, op_id, kind, request_json, state, token, observed_error, now
                    )
                if outcome == ESCALATED:
                    conn.commit()
                else:
                    conn.rollback()
            except GraphOperationError:
                raise
            except Exception:
                conn.rollback()
                raise
        details = {"op_id": op_id, "operation_kind": kind, "journal_state": state,
                   "escalation": outcome}
        log = _logger.error if outcome == ESCALATED else _logger.critical
        log(
            "Refused commit after graph work: escalation %s for op_id=%s kind=%s from %s (#404)",
            outcome, op_id, kind, state,
        )
        _record_journal_event(
            failure_stage="commit_refused_escalation",
            classification="manual_review",
            error_type="StaleTransitionError",
            error=observed_error,
            details=details,
        )
        return outcome

    @staticmethod
    def _refence_and_quarantine(
        conn: sqlite3.Connection,
        op_id: str,
        kind: str,
        request_json: str,
        from_state: str,
        token: str,
        observed_error: str,
        now: str,
    ) -> str:
        """Inside the caller's transaction: re-take every participant lock, then quarantine."""
        for entity_uuid in _participants_from_request_json(kind, request_json):
            holder = conn.execute(
                "SELECT op_id FROM graph_operation_locks WHERE entity_uuid = ?", (entity_uuid,)
            ).fetchone()
            if holder is not None and holder[0] != op_id:
                return REFUSED_FENCE_TAKEN
            if holder is None:
                conn.execute(
                    "INSERT INTO graph_operation_locks "
                    "(entity_uuid, op_id, operation_kind, created_at) VALUES (?, ?, ?, ?)",
                    (entity_uuid, op_id, kind, now),
                )
        try:
            cursor = conn.execute(
                "UPDATE graph_operations SET state = 'NEEDS_REVIEW', last_error = ?, "
                "updated_at = ?, resolved_by_token = ? WHERE op_id = ? AND state = ?",
                (observed_error, now, token, op_id, from_state),
            )
        except sqlite3.IntegrityError:
            # ux_graph_ops_unresolved_key: a newer PREPARED/NEEDS_REVIEW op already fences this
            # target_key, so quarantining this one would collide with live work.
            return REFUSED_FENCE_TAKEN
        return ESCALATED if cursor.rowcount == 1 else REFUSED_STATE

    # ------------------------------------------------------------------ internals
    def _record_stale_evidence(
        self, op_id: str, to_state: str, owner_token: str, evidence: str | None
    ) -> None:
        """F-9: a stale writer's same-state move is refused silently no more."""
        _logger.warning(
            "stale writer %s also moved op %s to %s; its evidence is kept here, not in the row: %s",
            owner_token, op_id, to_state, evidence,
        )
        _record_journal_event(
            failure_stage="stale_writer_evidence",
            classification="manual_review",
            error_type="StaleTransitionError",
            error=evidence or "",
            details={"op_id": op_id, "journal_state": to_state,
                     "refusal": REFUSAL_OWNER_MISMATCH},
        )

    def _transition(
        self,
        op_id: str,
        *,
        to_state: str,
        allowed_from: set[str],
        owner_token: str,
        set_committed_at: bool = False,
        last_error: str | None = None,
        commit_graph_work: bool | None = None,
    ) -> None:
        """Compare-and-set transition (CF-271).

        The UPDATE itself matches on ``op_id`` AND ``state IN allowed_from`` AND, while the row is
        PREPARED, the caller's claim token (an ownerless legacy row has no second owner to protect
        and stays exempt). Every caller passes a token; there is no tokenless path (#406 F-6). So a
        stale writer cannot overwrite a state a reconciler already moved. Zero matched rows changes
        nothing and releases nothing; it raises :class:`StaleTransitionError`. Lock release runs
        only in the same transaction as a successful update.
        """
        # Immutable-after-PREPARED fields (op_id, request_json, expected_after_sha256) are
        # never in the SET list here -- a transition only moves state and audit stamps.
        token = _require_token(owner_token, method="_transition")
        self._ensure_ready()
        now = _utc_now_iso()
        from_states = sorted(allowed_from)
        stale_evidence = False
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            sets = ["state = ?", "updated_at = ?", "resolved_by_token = ?"]
            params: list[Any] = [to_state, now, token]
            if set_committed_at:
                sets.append("committed_at = ?")
                params.append(now)
            if last_error is not None:
                sets.append("last_error = ?")
                params.append(last_error)
            if commit_graph_work is not None:
                sets.append("commit_graph_work = ?")
                params.append(1 if commit_graph_work else 0)
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
            where = (
                f"op_id = ? AND state IN ({', '.join('?' for _ in from_states)}) "
                "AND (state != 'PREPARED' OR owner_token IS NULL OR owner_token = ?)"
            )
            params.append(op_id)
            params.extend(from_states)
            params.append(token)
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
                    different_owner = resolved_by is not None and resolved_by != token
                    # CF-271 review F-2: a commit another owner made is not this writer's success.
                    # test_cf271_stale_commit_after_takeover exercises it. NULL (an older binary's
                    # tokenless move) stays idempotent: there is no evidence of a second owner.
                    if to_state == "COMMITTED" and different_owner:
                        raise StaleTransitionError(
                            f"{op_id!r} is already COMMITTED by another owner; this writer's "
                            "commit is refused",
                            refusal=REFUSAL_COMMITTED_BY_OTHER,
                        )
                    stale_evidence = different_owner and to_state in ("NEEDS_REVIEW", "FAILED")
                else:
                    if current in allowed_from:
                        raise StaleTransitionError(
                            f"transition {current} -> {to_state} for {op_id!r} refused: "
                            "owner token does not match the row's claim",
                            refusal=REFUSAL_OWNER_MISMATCH,
                        )
                    raise StaleTransitionError(
                        f"illegal transition {current} -> {to_state} for {op_id!r}",
                        refusal=REFUSAL_ILLEGAL_TRANSITION,
                    )
            else:
                # Release the participant fence only on a terminal state (invariant 14).
                # NEEDS_REVIEW keeps the locks so a quarantined op still fences its participants;
                # an operator clearing NEEDS_REVIEW to a terminal state releases them here through
                # the same path.
                if to_state in _LOCK_RELEASE_STATES:
                    conn.execute(
                        "DELETE FROM graph_operation_locks WHERE op_id = ?", (op_id,)
                    )
                conn.commit()
        if stale_evidence:
            # Idempotent, but a DIFFERENT owner made the move: keep this writer's evidence (F-9).
            self._record_stale_evidence(op_id, to_state, token, last_error)
