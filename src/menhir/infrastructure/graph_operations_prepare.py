"""PREPARE side of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): schema creation and the legacy
participant-lock backfill (``_ensure_ready``), the PREPARED insert with its recovery-lease gate
and per-participant fencing (``prepare``), and unique-index conflict classification.
``GraphOperationsJournal`` composes this mixin; every method runs against the facade's
``self.db_path``.
"""

from __future__ import annotations

import sqlite3
import time
import uuid as uuidlib

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations_model import (
    OPERATION_KINDS,
    RECONCILIATION_LEASE_NAME,
    GraphOperationError,
    SagaWritesPausedError,
    _PARTICIPANT_KINDS,
    _participants_from_request_json,
)
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.clock import utc_now_iso as _utc_now_iso


class _GraphOperationsPrepareMixin:
    """Schema initialization and the PREPARED write path of the saga journal."""

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
