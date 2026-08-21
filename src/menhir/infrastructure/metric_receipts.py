"""Immutable receipts explaining every Metric fold (Part C of the Metric plan).

A Metric counter is a durable fold over many transient telemetry rows. A bare
``source='failure-telemetry'`` is not a reproducible receipt, and the raw rows must stay
prunable. So each write appends an immutable ``metric_receipts`` row, keyed by the same
``op_id`` as the ``graph_operations`` journal entry.

Retention model: CHAINED ACCUMULATORS (owner-confirmed 2026-07-13). The first receipt for a
Metric records the full contributing row-ID set and aggregate through its cutoff; each later
receipt references ``previous_receipt_op_id`` and records only the new row IDs in
``(previous_cutoff, cutoff]``. The aggregate and ``input_digest`` chain from the prior
committed receipt, so raw telemetry rows can be pruned later without making the count or its
lineage unverifiable. A ``query_version`` change starts a new explicitly migrated lineage; it
never silently reinterprets pruned history.

Rows are append-only: a same-value refresh writes a NEW receipt and only updates
``metric_last_receipt_op_id`` on the Metric node. See the workspace-root artifact
.agent/plans/menhir-metric-provenance-redesign.md (Part C).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path

logger = logging.getLogger(__name__)

RECEIPT_KINDS = frozenset({"TELEMETRY_FOLD", "RUN_TALLY", "LEGACY_MIGRATION"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetricReceiptError(RuntimeError):
    """Raised when a receipt would violate the append-only / kind contract."""


@dataclass
class MetricReceiptStore:
    """The ``metric_receipts`` table: append-only fold provenance keyed by op_id."""

    db_path: Path = field(default_factory=default_telemetry_db_path)
    _initialized: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

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
                    CREATE TABLE IF NOT EXISTS metric_receipts (
                        op_id                   TEXT PRIMARY KEY,
                        receipt_kind            TEXT NOT NULL,
                        metric_uuid             TEXT NOT NULL,
                        view_key                TEXT NOT NULL,
                        source_table            TEXT,
                        grouping_json           TEXT,
                        query_version           TEXT,
                        previous_receipt_op_id  TEXT,
                        cutoff_id               INTEGER,
                        source_row_count        INTEGER,
                        source_row_ids_json     TEXT,
                        input_digest            TEXT,
                        aggregate_value         REAL,
                        run_id                  TEXT,
                        created_at              TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metric_receipts_key ON metric_receipts (view_key)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metric_receipts_uuid ON metric_receipts (metric_uuid)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metric_receipts_prev "
                    "ON metric_receipts (previous_receipt_op_id)"
                )
                conn.commit()
            self._initialized = True

    def append(
        self,
        *,
        op_id: str,
        receipt_kind: str,
        metric_uuid: str,
        view_key: str,
        aggregate_value: float,
        source_table: str | None = None,
        grouping_json: str | None = None,
        query_version: str | None = None,
        previous_receipt_op_id: str | None = None,
        cutoff_id: int | None = None,
        source_row_count: int | None = None,
        source_row_ids_json: str | None = None,
        input_digest: str | None = None,
        run_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Append an immutable receipt. Raises if op_id already has one (append-only).

        Pass ``conn`` to enlist this insert in a caller-owned SQLite transaction so the
        receipt and its graph_operations row commit atomically (plan E2). When ``conn`` is
        given this method does NOT commit -- the caller commits (or rolls back) both.
        """
        if receipt_kind not in RECEIPT_KINDS:
            raise MetricReceiptError(f"unknown receipt_kind {receipt_kind!r}")
        self._ensure_ready()
        now = _utc_now_iso()
        owns = conn is None
        connection = connect_telemetry_db(self.db_path) if owns else conn
        try:
            connection.execute(
                """
                INSERT INTO metric_receipts (
                    op_id, receipt_kind, metric_uuid, view_key, source_table,
                    grouping_json, query_version, previous_receipt_op_id, cutoff_id,
                    source_row_count, source_row_ids_json, input_digest,
                    aggregate_value, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op_id, receipt_kind, metric_uuid, view_key, source_table,
                    grouping_json, query_version, previous_receipt_op_id, cutoff_id,
                    source_row_count, source_row_ids_json, input_digest,
                    aggregate_value, run_id, now,
                ),
            )
            if owns:
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise MetricReceiptError(
                f"receipt for op_id {op_id!r} already exists; receipts are append-only"
            ) from exc
        finally:
            if owns:
                connection.close()

    def get(self, op_id: str) -> dict[str, Any] | None:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM metric_receipts WHERE op_id = ?", (op_id,)
            ).fetchone()
            return dict(row) if row else None

    def latest_for_key(
        self,
        view_key: str,
        *,
        committed_only: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """The head of the accumulator chain for a Metric key.

        ``committed_only`` (default) restricts the head to receipts whose graph operation actually
        COMMITTED. This matters: a receipt is written at PREPARE, but its graph mutation can still
        fail or drift, leaving the op PREPARED/NEEDS_REVIEW. Chaining the next fold onto such an
        orphan would anchor the accumulator to an aggregate the graph never reached -- silently
        double-counting or corrupting the lineage. The write fence makes that hard to hit, but the
        invariant should hold BY CONSTRUCTION, not by fence discipline.

        rowid (SQLite insertion order) breaks created_at ties deterministically, so two receipts
        written in the same microsecond still order by write order.

        Pass ``conn`` to read the head inside a caller-owned transaction (CF-244). The chain check
        in ``MetricWriteCoordinator._saga_write`` has to re-read the head and insert the next
        receipt atomically; opening a second connection here would read OUTSIDE that transaction
        and could not see -- or could deadlock against -- the writer's own lock.
        """
        self._ensure_ready()
        if conn is not None:
            return self._latest_for_key_on(conn, view_key, committed_only=committed_only)
        with connect_telemetry_db(self.db_path) as owned:
            return self._latest_for_key_on(owned, view_key, committed_only=committed_only)

    @staticmethod
    def _latest_for_key_on(
        conn: sqlite3.Connection, view_key: str, *, committed_only: bool
    ) -> dict[str, Any] | None:
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            if committed_only:
                row = conn.execute(
                    "SELECT r.* FROM metric_receipts r "
                    "JOIN graph_operations o ON o.op_id = r.op_id "
                    "WHERE r.view_key = ? AND o.state = 'COMMITTED' "
                    "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1",
                    (view_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM metric_receipts WHERE view_key = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (view_key,),
                ).fetchone()
            return dict(row) if row else None
        finally:
            # A caller-owned connection is borrowed, not ours to reconfigure.
            conn.row_factory = previous_factory

    def chain_for_key(self, view_key: str) -> list[dict[str, Any]]:
        """All receipts for a Metric key, oldest first (the full accumulator lineage).

        rowid tiebreak keeps the chain deterministic when created_at ties.
        """
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM metric_receipts WHERE view_key = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (view_key,),
            ).fetchall()
            return [dict(r) for r in rows]
