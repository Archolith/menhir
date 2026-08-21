"""Cross-database saga journal for SQLite-snapshot + Neo4j-mutate operations.

SQLite and Neo4j cannot share a transaction, so every operation that snapshots to the
sidecar and then mutates the graph runs as a recoverable saga:

    PREPARED  -> commit a journal row (op_id, request, snapshot) in SQLite
    MUTATE    -> idempotent, preconditioned Neo4j write keyed by op_id
    COMMITTED -> mark the journal row committed only after the after-state verifies
    RECONCILE -> replay any row still PREPARED after a crash; drift -> NEEDS_REVIEW

The missing durable-before-delete record is exactly why ~24 nodes destroyed by the
degree-zero orphan cleanup on 2026-07-12 were unrecoverable. This journal makes every
Metric write, migration, and reversal replayable and reversible. See the workspace-root
artifact .agent/plans/menhir-metric-provenance-redesign.md (Part E).

Invariants enforced here (Part E1):
  - operation identity and request_json are immutable once PREPARED is committed;
  - at most one PREPARED routine METRIC_WRITE may exist per target_key (fencing);
  - a NEEDS_REVIEW row never transitions except by an explicit operator command.

Per-participant fencing (invariant 14): entity-pair and delete operations additionally take one
lock per participant UUID in ``graph_operation_locks``, so two unresolved operations can never
share a node even when their pair keys differ (a merge of A+B vs a merge of B+C, or a delete of B
vs a merge of A+B). The lock releases only on a terminal state or an audited NEEDS_REVIEW clearance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid as uuidlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)

# Closed enums (Part E1). Kept as plain frozensets so callers pass validated strings.
#
# ENTITY_MERGE (merge/delete lifecycle remediation, Phase 4) reuses this journal rather than adding
# a parallel audit store. Its target_key is the normalized PAIR key, so the existing unresolved-key
# fence (ux_graph_ops_unresolved_key) automatically quarantines a drifted pair: while a merge is
# PREPARED or NEEDS_REVIEW, a competing merge of the same pair cannot be prepared.
#
# METRIC_WRITE is preserved verbatim (invariant 13): renaming it would strand PREPARED rows written
# by an earlier build and silently stop their replay.
OPERATION_KINDS = frozenset(
    {
        "METRIC_WRITE", "METRIC_MIGRATE", "METRIC_REVERSE",
        "ENTITY_MERGE", "ENTITY_UNMERGE", "LEGACY_ENTITY_UNMERGE",
        "ENTITY_DELETE", "SESSION_TTL_DELETE",
        "EXPLICIT_ERASURE",
    }
)
# FAILED is terminal and means "no graph mutation occurred" (plan section 1's state list). It is
# NOT a quarantine: an operation that abstained at the mutation gate (e.g. a node legitimately became
# COMPRESSED between PREPARE and MUTATE) left the graph untouched, so there is nothing for an operator
# to adjudicate. Because the unresolved-key index fences only PREPARED and NEEDS_REVIEW, marking such
# an op FAILED RELEASES the pair -- whereas NEEDS_REVIEW would fence it forever over a benign
# abstention, and leaving it PREPARED would make reconciliation retry a pair that will never become
# eligible again.
OPERATION_STATES = frozenset(
    {"PREPARED", "COMMITTED", "NEEDS_REVIEW", "REVERSED", "FAILED"}
)

# Per-participant fencing (invariant 14). The pair-key fence blocks a competing operation on the
# SAME pair, but two unresolved operations that share ONE node via different pairs (a merge of A+B
# and an unrelated merge of B+C, or a delete of B while A+B is unresolved) are not mutually fenced by
# target_key alone. graph_operation_locks holds one row per participant UUID of an UNRESOLVED op, so
# at most one unresolved operation may hold any given participant. Metric kinds fence on target_key
# (metric identity, not entity pairs) and take no participant locks.
#
# Entity-pair kinds lock {survivor, absorbed}; delete kinds lock each target uuid.
_PARTICIPANT_PAIR_KINDS = frozenset(
    {"ENTITY_MERGE", "ENTITY_UNMERGE", "LEGACY_ENTITY_UNMERGE"}
)
# EXPLICIT_ERASURE fences its participants for the same reason a delete does: while an
# erasure is unresolved, a merge on one of its subjects could copy the content being erased
# into a survivor's recovery snapshot. A NAMESPACE erasure carries no "targets" (membership
# lives in the erasure_subjects inventory, not in request_json, so a large namespace stays
# bounded), so it takes no participant lock and fences on its target_key instead.
_PARTICIPANT_DELETE_KINDS = frozenset(
    {"ENTITY_DELETE", "SESSION_TTL_DELETE", "EXPLICIT_ERASURE"}
)
_PARTICIPANT_KINDS = _PARTICIPANT_PAIR_KINDS | _PARTICIPANT_DELETE_KINDS

# A lock is released only when its op reaches a terminal state. NEEDS_REVIEW keeps the fence
# (quarantine still fences); PREPARED keeps it (the op is in flight).
_LOCK_RELEASE_STATES = frozenset({"COMMITTED", "REVERSED", "FAILED"})




def _participant_uuids(operation_kind: str, request: dict[str, Any]) -> list[str]:
    """Participant UUIDs an operation must fence, derived from its request payload.

    Entity-pair kinds fence {survivor, absorbed}; delete kinds fence each target. Everything else
    (Metric writes/migrations/reversals) takes no participant lock. Used identically at PREPARE and
    at backfill so the two paths can never disagree about what a row locks.
    """
    if operation_kind in _PARTICIPANT_PAIR_KINDS:
        return [
            str(u)
            for u in (request.get("survivor_uuid"), request.get("absorbed_uuid"))
            if u
        ]
    if operation_kind in _PARTICIPANT_DELETE_KINDS:
        seen: dict[str, None] = {}
        for t in request.get("targets") or []:
            if t:
                seen.setdefault(str(t), None)
        return list(seen)
    return []


def _participants_from_request_json(operation_kind: str, request_json: str | None) -> list[str]:
    """Best-effort participant extraction from a stored request_json string.

    A parse failure degrades to no participant locks (the pair-key fence still holds), matching the
    documented backward-compat posture: the participant fence is advisory until a row is locked.
    """
    if operation_kind not in _PARTICIPANT_KINDS or not request_json:
        return []
    try:
        request = json.loads(request_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(request, dict):
        return []
    return _participant_uuids(operation_kind, request)


#: Lease name that pauses all saga PREPARE while recovery owns the backlog (CF-20c).
#:
#: The lease lives in `scheduler_leases`, in the SAME SQLite database as this journal. That shared
#: database is what makes the gate correct rather than advisory: a writer's BEGIN IMMEDIATE and the
#: recovery lease's BEGIN IMMEDIATE contend for the same write lock, so they serialise and neither
#: can slip between the other's check and write.
RECONCILIATION_LEASE_NAME = "saga-reconciliation"


class GraphOperationError(RuntimeError):
    """Raised when a saga invariant would be violated (immutability, fencing, state)."""


class SagaWritesPausedError(GraphOperationError):
    """A new saga cannot PREPARE because recovery currently owns the backlog.

    Distinct from the fencing errors so a caller can tell "this specific target is busy" (retry
    later, or adjudicate) from "this process is not accepting new sagas at all" (wait for recovery
    to finish). Subclasses GraphOperationError so existing handlers keep working unchanged.
    """


@dataclass
class GraphOperationsJournal:
    """The ``graph_operations`` table: the durable record of every graph mutation.

    Rows are the source of intent; the graph is the source of truth for "done". A row's
    identity (op_id) and request_json are frozen after PREPARED so a replay can never
    silently change what was intended.
    """

    db_path: Path = field(default_factory=default_telemetry_db_path)
    _initialized: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ------------------------------------------------------------------ schema
    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with connect_telemetry_db(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_operations (
                        op_id                 TEXT PRIMARY KEY,
                        batch_id              TEXT,
                        operation_kind        TEXT NOT NULL,
                        target_uuid           TEXT,
                        target_key            TEXT,
                        request_json          TEXT NOT NULL,
                        before_snapshot_json  TEXT,
                        expected_after_sha256 TEXT,
                        state                 TEXT NOT NULL,
                        attempt_count         INTEGER NOT NULL DEFAULT 0,
                        last_error            TEXT,
                        created_at            TEXT NOT NULL,
                        updated_at            TEXT NOT NULL,
                        committed_at          TEXT,
                        reverses_op_id        TEXT,
                        owner_token           TEXT,
                        owner_heartbeat_at    TEXT,
                        owner_lease_expires_at TEXT,
                        owner_death_attested_by TEXT,
                        owner_death_attested_at TEXT,
                        owner_death_attested_for_token TEXT
                    )
                    """
                )
                # Additive ownership migration (CF-20b). CREATE TABLE IF NOT EXISTS does nothing to
                # an ALREADY EXISTING table, so a sidecar created before this fence keeps its old
                # column set and every ownership read would raise. Add the columns explicitly,
                # following the PRAGMA-then-ALTER idiom used by the telemetry store.
                #
                # Nullable with no default, deliberately: a pre-existing PREPARED row genuinely has
                # no owner, and backfilling a synthetic claim would fabricate exactly the liveness
                # evidence recovery is supposed to reason about. Ownerless reads as OWNER_UNKNOWN.
                operation_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(graph_operations)")
                }
                for column in (
                    "owner_token",
                    "owner_heartbeat_at",
                    "owner_lease_expires_at",
                    "owner_death_attested_by",
                    "owner_death_attested_at",
                    "owner_death_attested_for_token",
                ):
                    if column not in operation_columns:
                        # Column names are literals from the tuple above, never caller input.
                        conn.execute(
                            f"ALTER TABLE graph_operations ADD COLUMN {column} TEXT"
                        )
                # Batch operations are unique per (kind, batch, target) so a migration
                # cannot enqueue the same node twice.
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_graph_ops_batch_target
                    ON graph_operations (operation_kind, batch_id, target_uuid)
                    WHERE batch_id IS NOT NULL
                    """
                )
                # Fencing: at most one UNRESOLVED write per target_key, so two writers cannot both
                # create a competing current version.
                #
                # NEEDS_REVIEW fences too, not just PREPARED: a drifted operation may have left the
                # node half-applied, and letting a fresh write proceed against it would mutate the
                # very state the operator still has to adjudicate. The fence releases only when the
                # op reaches a terminal state (COMMITTED / REVERSED) or an operator clears it.
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_graph_ops_unresolved_key
                    ON graph_operations (operation_kind, target_key)
                    WHERE state IN ('PREPARED', 'NEEDS_REVIEW') AND target_key IS NOT NULL
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_ops_state ON graph_operations (state)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_ops_batch ON graph_operations (batch_id)"
                )
                # Per-participant fence (invariant 14). Each row locks one participant UUID for the
                # lifetime of an UNRESOLVED op; entity_uuid is PRIMARY KEY so at most one op can hold
                # a given participant. Lock rows are inserted at PREPARE (same transaction as the
                # journal row) and deleted when the op reaches a terminal state, so the table holds
                # ONLY live locks -- the uniqueness of entity_uuid is the whole fence.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_operation_locks (
                        entity_uuid     TEXT PRIMARY KEY,
                        op_id           TEXT NOT NULL,
                        operation_kind  TEXT NOT NULL,
                        created_at      TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_operation_locks_op "
                    "ON graph_operation_locks (op_id)"
                )
                self._backfill_participant_locks(conn)
                conn.commit()
            self._initialized = True

    def _backfill_participant_locks(self, conn: sqlite3.Connection) -> None:
        """Materialize locks for unresolved rows written before this fence existed.

        Runs inside ``_ensure_ready``'s transaction. Idempotent (INSERT OR IGNORE against the
        entity_uuid primary key), so it is safe to run every process start; it also re-locks any
        unresolved op whose lock rows a crash left behind. Until a row is backfilled the participant
        fence is advisory -- the pair-key fence still holds, so there is no regression.
        """
        now = _utc_now_iso()
        placeholders = ", ".join("?" for _ in _PARTICIPANT_KINDS)
        kinds = tuple(sorted(_PARTICIPANT_KINDS))
        rows = conn.execute(
            f"SELECT op_id, operation_kind, request_json FROM graph_operations "
            f"WHERE state IN ('PREPARED', 'NEEDS_REVIEW') "
            f"AND operation_kind IN ({placeholders})",
            kinds,
        ).fetchall()
        for op_id, operation_kind, request_json in rows:
            for entity_uuid in _participants_from_request_json(operation_kind, request_json):
                conn.execute(
                    "INSERT OR IGNORE INTO graph_operation_locks "
                    "(entity_uuid, op_id, operation_kind, created_at) VALUES (?, ?, ?, ?)",
                    (entity_uuid, op_id, operation_kind, now),
                )

    # ------------------------------------------------------------------ PREPARED
    def prepare(
        self,
        *,
        operation_kind: str,
        request_json: str,
        target_uuid: str | None = None,
        target_key: str | None = None,
        batch_id: str | None = None,
        before_snapshot_json: str | None = None,
        expected_after_sha256: str | None = None,
        reverses_op_id: str | None = None,
        op_id: str | None = None,
        conn: sqlite3.Connection | None = None,
        owner_token: str | None = None,
    ) -> str:
        """Insert a PREPARED operation and return its op_id.

        ``expected_after_sha256`` is the postcondition fingerprint frozen at PREPARE
        (plan E1); it is immutable afterwards -- ``mark_committed`` never rewrites it.

        Pass ``conn`` to enlist this insert in a caller-owned SQLite transaction so the
        operation row and its Metric receipt commit atomically (plan E2). When ``conn`` is
        given, this method does NOT commit -- the caller commits (or rolls back) both.

        Raises GraphOperationError if operation_kind is unknown, if a competing PREPARED
        routine write already fences this target_key, or if the batch/target pair is already
        enqueued.
        """
        if operation_kind not in OPERATION_KINDS:
            raise GraphOperationError(f"unknown operation_kind {operation_kind!r}")
        self._ensure_ready()
        op_id = op_id or uuidlib.uuid4().hex
        now = _utc_now_iso()
        # Per-participant fence (invariant 14): one lock row per participant, inserted in the SAME
        # transaction as the journal row so the fence and the intent commit atomically.
        participants = _participants_from_request_json(operation_kind, request_json)
        # Live-owner claim (CF-20b): stamped in the SAME insert as the intent, so a PREPARED row
        # is never briefly ownerless. A row that appears with no claim is therefore a legacy row,
        # which is what lets OWNER_UNKNOWN mean something specific. Overridable so a test can
        # impersonate another process, and so a future reconciler can claim an abandoned row.
        claim_token = owner_token or oo.process_owner_token()
        # The durable expiry must be derived from THIS operation's kind, not the single-statement
        # default. A writer's local heartbeat computes its headroom from the kind, so a shorter
        # durable stamp would let the writer believe it had more claim than the row actually
        # carries -- and it would believe that in the DANGEROUS direction, passing its own
        # pre-dispatch headroom check while the real claim was close to lapsing.
        claim_seconds = oo.lease_seconds_for_kind(operation_kind)
        owns = conn is None
        connection = connect_telemetry_db(self.db_path) if owns else conn
        try:
            # BEGIN IMMEDIATE takes the write lock BEFORE the gate check, so the check and the
            # insert are one atomic step against the recovery lease (CF-20c). Python's sqlite3
            # otherwise starts a DEFERRED transaction on first DML, which would leave the gate check
            # racing the lease acquisition: a deferred reader can see no lease, recovery can then
            # acquire it and commit, and this insert still lands.
            #
            # This also covers the metric saga, which hands in its own connection but has not yet
            # issued any DML when it calls us -- so the IMMEDIATE opened here becomes the
            # transaction that its journal row AND its receipt both commit in. A connection already
            # inside a transaction is left alone: it owns its own boundary.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            self._assert_saga_writes_allowed(connection)
            connection.execute(
                """
                INSERT INTO graph_operations (
                    op_id, batch_id, operation_kind, target_uuid, target_key,
                    request_json, before_snapshot_json, expected_after_sha256,
                    state, attempt_count, last_error, created_at, updated_at,
                    committed_at, reverses_op_id,
                    owner_token, owner_heartbeat_at, owner_lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', 0, NULL, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    op_id, batch_id, operation_kind, target_uuid, target_key,
                    request_json, before_snapshot_json, expected_after_sha256,
                    now, now, reverses_op_id,
                    claim_token, now, oo.lease_expiry_iso(seconds=claim_seconds),
                ),
            )
            for entity_uuid in participants:
                connection.execute(
                    "INSERT INTO graph_operation_locks "
                    "(entity_uuid, op_id, operation_kind, created_at) VALUES (?, ?, ?, ?)",
                    (entity_uuid, op_id, operation_kind, now),
                )
            if owns:
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise self._classify_conflict(
                connection, operation_kind, target_key, target_uuid, batch_id,
                op_id, participants, exc,
            ) from exc
        finally:
            if owns:
                connection.close()
        return op_id

    def _assert_saga_writes_allowed(self, conn: sqlite3.Connection) -> None:
        """Refuse a new PREPARE while recovery holds the reconciliation lease (CF-20c).

        MUST be called with a write lock already held (BEGIN IMMEDIATE), or this is a
        check-then-insert race: a deferred reader could see no lease, recovery could then acquire it
        and commit, and this insert would still land -- producing exactly the PREPARED row that
        recovery has already decided it owns the complete set of.

        Reads the lease table directly rather than through SchedulerLeaseStore because this module is
        infrastructure and that store is a service; importing upward would invert the layering for a
        single SELECT. The coupling is the table name and its epoch-seconds expiry column.

        A MISSING table means no gate has ever been created -- the normal state on a fresh database,
        and positive evidence of absence, so it fails open. Every other read failure means the gate
        schema exists but cannot be read, which is NOT evidence of absence, so it fails closed for
        the same reason a present-but-unparseable expiry does.
        """
        try:
            row = conn.execute(
                "SELECT owner_id, lease_expires_at FROM scheduler_leases WHERE lease_name = ?",
                (RECONCILIATION_LEASE_NAME,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # Fail OPEN only for a table that does not exist -- proof no gate was ever created, and
            # the normal state of a fresh database. Any other operational error means the gate
            # schema is present but unreadable, which is not evidence of absence, so it fails
            # CLOSED like a present-but-unparseable row.
            if "no such table" in str(exc).lower():
                return
            raise SagaWritesPausedError(
                f"cannot read the reconciliation gate ({exc}); refusing to PREPARE while its "
                "state is unknown"
            ) from exc
        except sqlite3.Error as exc:
            raise SagaWritesPausedError(
                f"cannot read the reconciliation gate ({exc}); refusing to PREPARE while its "
                "state is unknown"
            ) from exc
        if row is None:
            return
        try:
            expires_at = float(row[1])
        except (TypeError, ValueError):
            # A lease row with an unreadable expiry cannot be proven expired. Fail CLOSED here --
            # unlike a missing table, a PRESENT row is positive evidence that recovery is running.
            raise SagaWritesPausedError(
                "saga writes are paused: the reconciliation lease is held by "
                f"{row[0]!r} with an unreadable expiry"
            ) from None
        if expires_at > time.time():
            raise SagaWritesPausedError(
                "saga writes are paused while recovery reconciles the PREPARED backlog "
                f"(lease {RECONCILIATION_LEASE_NAME!r} held by {row[0]!r}); retry once the "
                "instance reports write-ready"
            )

    def _classify_conflict(
        self,
        conn: sqlite3.Connection,
        operation_kind: str,
        target_key: str | None,
        target_uuid: str | None,
        batch_id: str | None,
        op_id: str,
        participants: list[str],
        exc: sqlite3.IntegrityError,
    ) -> GraphOperationError:
        """Turn a unique-constraint violation into a specific error by inspecting state.

        Version-independent: rather than parse SQLite's (unstable) error text, query which
        of the unique indexes actually conflicts. A failed INSERT does not abort the surrounding
        transaction, so these reads see the committed conflicting rows.
        """
        try:
            # Exclude the current op's own row: when a participant-lock insert fails, this op's
            # journal row is already present in the (uncommitted) transaction, so an unqualified
            # target_key lookup would match itself and misreport a pair-key conflict.
            blocking = conn.execute(
                "SELECT state FROM graph_operations "
                "WHERE operation_kind = ? AND target_key = ? AND op_id != ? "
                "AND state IN ('PREPARED', 'NEEDS_REVIEW') LIMIT 1",
                (operation_kind, target_key, op_id),
            ).fetchone() if target_key is not None else None
            if blocking:
                return GraphOperationError(
                    f"an unresolved ({blocking[0]}) {operation_kind} already fences target_key "
                    f"{target_key!r}; reconcile it (or clear the review) before writing a "
                    "competing version"
                )
            for entity_uuid in participants:
                lock = conn.execute(
                    "SELECT op_id, operation_kind FROM graph_operation_locks "
                    "WHERE entity_uuid = ? AND op_id != ? LIMIT 1",
                    (entity_uuid, op_id),
                ).fetchone()
                if lock:
                    return GraphOperationError(
                        f"participant {entity_uuid!r} is already fenced by an unresolved "
                        f"{lock[1]} (op {lock[0]}); reconcile it (or clear the review) before "
                        "preparing an operation that shares this node"
                    )
            if batch_id is not None and target_uuid is not None and conn.execute(
                "SELECT 1 FROM graph_operations "
                "WHERE operation_kind = ? AND batch_id = ? AND target_uuid = ? AND op_id != ? "
                "LIMIT 1",
                (operation_kind, batch_id, target_uuid, op_id),
            ).fetchone():
                return GraphOperationError(
                    f"{operation_kind} for target {target_uuid!r} already enqueued in batch "
                    f"{batch_id!r}"
                )
        except sqlite3.Error:
            pass
        return GraphOperationError(str(exc))

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

    # ------------------------------------------------------------------ reads
    def get(self, op_id: str) -> dict[str, Any] | None:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            return dict(row) if row else None

    def renew_owner_heartbeat(
        self,
        op_id: str,
        *,
        seconds: int = oo.DEFAULT_LEASE_SECONDS,
        owner_token: str | None = None,
    ) -> bool:
        """Extend this process's claim on a PREPARED operation. Returns whether it still holds it.

        A single conditional UPDATE, so the check and the extension cannot interleave: it renews
        only if the row is still PREPARED AND still carries this process's token. The boolean is
        the point -- long-running saga code must be able to discover that it LOST its claim (the
        row was quarantined, committed, or taken over) and stop before its next side effect,
        rather than carrying on believing it owns work someone else may now be replaying.
        """
        self._ensure_ready()
        now = _utc_now_iso()
        token = owner_token or oo.process_owner_token()
        with connect_telemetry_db(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_heartbeat_at = ?, owner_lease_expires_at = ?, "
                # A renewal is the owner proving it is alive, which DISPROVES any attestation of its
                # death. Leaving one in place would let it reactivate the moment this fresh lease
                # later expired.
                "    owner_death_attested_by = NULL, owner_death_attested_at = NULL, "
                "    owner_death_attested_for_token = NULL "
                "WHERE op_id = ? AND state = 'PREPARED' AND owner_token = ?",
                (now, oo.lease_expiry_iso(seconds=seconds), op_id, token),
            )
            conn.commit()
            return cursor.rowcount == 1

    def claim_abandoned_operation(
        self,
        op_id: str,
        *,
        owner_token: str | None = None,
        seconds: int | None = None,
    ) -> bool:
        """Take ownership of an ABANDONED PREPARED row before replaying it (CF-20c).

        ``seconds`` defaults to the TTL derived from the row's OWN operation_kind, read inside the
        same transaction as the claim.

        Returns True only if this call is the one that took it. The whole point is that the
        transfer happens BEFORE any graph side effect: without it, two reconcilers can both read a
        row as abandoned, both begin mutating, and only discover the conflict at the journal
        transition -- by which time both have already touched the graph.

        One conditional UPDATE under BEGIN IMMEDIATE, so the liveness test and the transfer cannot
        interleave. The WHERE clause is the guard, and each conjunct is load-bearing:

        * ``state = 'PREPARED'`` -- a row that reached a terminal state is finished, not recoverable.
        * ``owner_token IS NOT NULL`` -- an ownerless row is OWNER_UNKNOWN, never ABANDONED. During
          a mixed-version rollout an older binary with no ownership support may still be executing
          it, so claiming it is exactly the double-apply this design exists to prevent.
        * ``owner_lease_expires_at <= now`` -- a live heartbeat is a hard veto. Comparison is done
          in SQL against the stored ISO string so the read and the write are one statement; ISO-8601
          UTC strings from ``_utc_now_iso`` sort lexicographically in timestamp order, which is what
          makes that valid.

        Those conjuncts are NECESSARY but not SUFFICIENT. Expiry alone is a staleness signal, never
        a death certificate, so the same :func:`classify_ownership` rule the observer applies is
        re-evaluated inside this transaction and must return ABANDONED before the UPDATE runs.

        A row this process already owns is NOT claimable through here: it is not abandoned, and a
        reconciler that finds its own live claim should be verifying its own state machine rather
        than re-taking a lease it never lost.
        """
        self._ensure_ready()
        token = owner_token or oo.process_owner_token()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Derive the new expiry from the ROW's own kind, inside the same transaction that reads
            # it. A claimant that stamped the single-statement default onto a two-statement
            # operation would hand itself a claim shorter than the work it is about to do.
            kind_row = conn.execute(
                "SELECT operation_kind FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            claim_seconds = (
                seconds if seconds is not None
                else oo.lease_seconds_for_kind(kind_row[0] if kind_row else "")
            )
            # Enforce the SAME rule classification applies, inside the same transaction. Claiming
            # on expiry alone would route straight past the death-evidence requirement and
            # reintroduce exactly the unproven premise it exists to remove.
            row = conn.execute(
                "SELECT owner_token, owner_lease_expires_at, owner_death_attested_by, state, "
                "owner_death_attested_for_token "
                "FROM graph_operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if row is None or row[3] != "PREPARED":
                conn.commit()
                return False
            candidate = {
                "owner_token": row[0],
                "owner_lease_expires_at": row[1],
                "owner_death_attested_by": row[2],
                "owner_death_attested_for_token": row[4],
            }
            if oo.classify_ownership(candidate) != oo.ABANDONED:
                conn.commit()
                return False

            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_token = ?, owner_heartbeat_at = ?, owner_lease_expires_at = ?, "
                "    updated_at = ?, "
                # The attestation dies with the owner it named. Carrying it across a transfer would
                # make it evidence about the CLAIMANT: once this new lease later went stale, the
                # stale attestation would declare a possibly-live reconciler ABANDONED and invite a
                # third process to replay underneath it -- the exact false-death path Option 3
                # exists to remove.
                "    owner_death_attested_by = NULL, owner_death_attested_at = NULL, "
                "    owner_death_attested_for_token = NULL "
                "WHERE op_id = ? "
                "  AND state = 'PREPARED' "
                "  AND owner_token IS NOT NULL "
                "  AND owner_token != ? "
                "  AND owner_lease_expires_at IS NOT NULL "
                "  AND owner_lease_expires_at <= ?",
                (token, now, oo.lease_expiry_iso(seconds=claim_seconds), now, op_id, token, now),
            )
            conn.commit()
            return cursor.rowcount == 1

    def attest_owner_death(self, op_id: str, *, attested_by: str) -> bool:
        """Record independent operator evidence that a PREPARED row's writer is gone.

        The sanctioned path for the case automation cannot decide: an owner on another host, or one
        whose PID has been recycled, where this process can never prove death by inspection. It is
        deliberately a human act with a name attached -- the alternative is inferring death from a
        clock, which is the premise this design removed.

        An attestation is a safety OVERRIDE for evidence automation cannot gather -- it is not a
        faster clock, and each WHERE conjunct keeps it from becoming one:

        * ``state = 'PREPARED'`` -- a terminal operation has no writer to attest about.
        * ``owner_token IS NOT NULL`` -- an ownerless row names no writer, so there is nobody to
          attest the death OF. Those rows stay OWNER_UNKNOWN and need direct repair.
        * ``owner_lease_expires_at IS NOT NULL AND <= now`` -- the claim must ALREADY be stale.
          Attesting against a live heartbeat would let a mistaken operator authorise a replay
          underneath a writer that renewed its lease seconds ago. :func:`classify_ownership`
          independently refuses to read an attestation on a fresh lease, so this is the durable
          half of a check enforced on both the write and the read side.

        Both the name and the instant are stored, because an override that cannot be audited after
        the fact is indistinguishable from the clock-based inference this replaced.
        """
        if not str(attested_by or "").strip():
            raise GraphOperationError("attested_by is required: attestation must name a person")
        self._ensure_ready()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_death_attested_by = ?, owner_death_attested_at = ?, updated_at = ?, "
                # Bind the attestation to the EXACT token it is about. An operator attests that one
                # named process is dead, never that "whoever holds this row" is dead.
                "    owner_death_attested_for_token = owner_token "
                "WHERE op_id = ? "
                "  AND state = 'PREPARED' "
                "  AND owner_token IS NOT NULL "
                "  AND owner_lease_expires_at IS NOT NULL "
                "  AND owner_lease_expires_at <= ?",
                (str(attested_by).strip(), now, now, op_id, now),
            )
            conn.commit()
            return cursor.rowcount == 1

    def iter_by_state(
        self, state: str, *, batch_size: int = 500
    ) -> Iterator[dict[str, Any]]:
        """Yield EVERY row in ``state``, oldest first, with no horizon that can hide rows.

        ``list_by_state(limit=500)`` cannot be made exhaustive by calling it repeatedly: it always
        returns the same oldest page, so any row that never leaves the state makes the caller loop
        on it forever while newer rows are never seen. That is the deterministic starvation CF-20
        has to remove before recovery can be trusted, and it is why this is a cursor, not a bigger
        limit.

        The keyset is ``(created_at, op_id)``, and the op_id half is load-bearing. ``created_at`` is
        NOT unique -- operations prepared in the same instant share it -- so ordering by it alone is
        not a total order, and a page boundary falling inside a group of ties would silently skip or
        repeat rows. op_id is the PRIMARY KEY, so it breaks every tie.

        Each page is a separate connection and the scan holds no snapshot: rows that leave ``state``
        mid-scan simply stop appearing, and rows added with a later ``created_at`` will be picked up.
        For an observation pass that is correct. A pass that must see a FIXED backlog has to close
        the PREPARE gate first (CF-20c) -- the cursor guarantees progress, not isolation.
        """
        if state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown state {state!r}")
        self._ensure_ready()
        page_size = max(1, int(batch_size))
        cursor_created_at: str | None = None
        cursor_op_id: str | None = None
        while True:
            with connect_telemetry_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if cursor_created_at is None:
                    rows = conn.execute(
                        "SELECT * FROM graph_operations WHERE state = ? "
                        "ORDER BY created_at ASC, op_id ASC LIMIT ?",
                        (state, page_size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM graph_operations WHERE state = ? "
                        "AND (created_at > ? OR (created_at = ? AND op_id > ?)) "
                        "ORDER BY created_at ASC, op_id ASC LIMIT ?",
                        (state, cursor_created_at, cursor_created_at, cursor_op_id, page_size),
                    ).fetchall()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            cursor_created_at = rows[-1]["created_at"]
            cursor_op_id = rows[-1]["op_id"]
            if len(rows) < page_size:
                return

    def list_by_state(self, state: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown state {state!r}")
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM graph_operations WHERE state = ? ORDER BY created_at ASC LIMIT ?",
                (state, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM graph_operations WHERE batch_id = ? ORDER BY created_at ASC",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]


    def list_committed_merges(self, *, page_size: int = 5000) -> list[dict[str, Any]]:
        """Read-only merge-lineage source for the ScalarStateView C.4.4 repair passes: ALL COMMITTED
        ENTITY_MERGE ops as ``[{op_id, absorbed_uuid, survivor_uuid}]``, oldest first (chain order).
        Filters ``state='COMMITTED' AND operation_kind='ENTITY_MERGE'`` IN SQL and PAGES through the
        whole history (`page_size` is a batch size, NOT a total cap), so a later merge or a downstream
        chain crossing an arbitrary cutoff is never permanently hidden, and malformed rows cannot
        consume a limit ahead of valid lineage. A row missing a survivor/absorbed pair is skipped.
        Does not touch the write/fence path."""
        return self._committed_pairs("ENTITY_MERGE", page_size=page_size)

    def list_committed_unmerges(self, *, page_size: int = 5000) -> list[dict[str, Any]]:
        """Read-only lineage source for ALL committed ENTITY_UNMERGE ops as
        ``[{op_id, merge_op_id, absorbed_uuid, survivor_uuid}]`` (the shape
        `repair_incomplete_reconciliations` consumes). `merge_op_id` comes from the row's
        ``reverses_op_id``. Filtered in SQL and PAGED through the whole history; rows missing the pair
        or the reversed op are skipped. Read-only."""
        return self._committed_pairs("ENTITY_UNMERGE", page_size=page_size, with_reverses=True)

    def _committed_pairs(
        self, operation_kind: str, *, page_size: int, with_reverses: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        out: list[dict[str, Any]] = []
        offset = 0
        batch = max(1, int(page_size))
        # Page to EXHAUSTION — there is no total cutoff. A ceiling here would permanently hide the
        # rows beyond it (every call restarts at offset 0), so a beyond-ceiling receiptless op would be
        # invisible forever and a beyond-ceiling orphan would be marked unresolved on every run.
        while True:
            with connect_telemetry_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT op_id, request_json, reverses_op_id FROM graph_operations "
                    "WHERE state = 'COMMITTED' AND operation_kind = ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
                    (operation_kind, batch, offset),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                try:
                    request = json.loads(row["request_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue                    # malformed row skipped; it did NOT consume valid budget
                absorbed = request.get("absorbed_uuid")
                survivor = request.get("survivor_uuid")
                if not (absorbed and survivor):
                    continue
                item = {"op_id": str(row["op_id"]),
                        "absorbed_uuid": str(absorbed), "survivor_uuid": str(survivor)}
                if with_reverses:
                    merge_op_id = row["reverses_op_id"] or request.get("merge_op_id")
                    if not merge_op_id:
                        continue      # an unmerge with no forward-merge lineage cannot be reconciled
                    item["merge_op_id"] = str(merge_op_id)
                out.append(item)
            if len(rows) < batch:
                break                           # last page
            offset += batch
        return out
