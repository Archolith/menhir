"""Scheduled physical compaction of the telemetry sidecar (erasure rebuild, P3).

A purge blanks content under ``secure_delete``, but bytes written earlier through a connection
that was not secure (or sitting in the WAL) can survive in free pages. ``VACUUM`` rewrites the
file, and ``wal_checkpoint(TRUNCATE)`` folds and empties the WAL. This job runs both, requires
both to have finished, and only then stamps ``graph_operations.sidecar_compacted_at`` on every
op committed before the job started. A busy result changes nothing and is retried next run.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from menhir.clock import utc_now_iso as _utc_now_iso
from menhir.infrastructure.telemetry.helpers import (
    add_column_if_missing,
    connect_telemetry_db,
    default_telemetry_db_path,
)

logger = logging.getLogger(__name__)

COMPACTED = "compacted"
RETRY = "retry"


def _ensure_compacted_column(conn: sqlite3.Connection) -> bool:
    """Add ``sidecar_compacted_at`` to graph_operations; False when the journal table is absent."""
    existing = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_operations)")}
    if not existing:
        return False
    add_column_if_missing(conn, "graph_operations", "sidecar_compacted_at", "TEXT", existing)
    conn.commit()
    return True


def run_sidecar_compaction(*, db_path: Path | str | None = None) -> dict[str, Any]:
    """VACUUM, then wal_checkpoint(TRUNCATE); stamp ``sidecar_compacted_at`` only if both succeed.

    Returns ``{"status": "compacted", "started_at", "ops_stamped"}`` or
    ``{"status": "retry", "reason", "started_at"}``; a retry result has changed no op row.
    ``started_at`` is taken BEFORE the VACUUM from the clock the journal uses for
    ``committed_at``, so an op committed after it is never stamped by this run.
    """
    path = Path(db_path) if db_path is not None else default_telemetry_db_path()
    started_at = _utc_now_iso()
    conn = connect_telemetry_db(path)
    try:
        conn.isolation_level = None  # autocommit: VACUUM cannot run inside a transaction
        has_journal = _ensure_compacted_column(conn)
        try:
            conn.execute("VACUUM")
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            logger.warning("sidecar compaction busy, will retry: %s", exc)
            return {"status": RETRY, "reason": f"busy: {exc}", "started_at": started_at}
        if row is None or int(row[0]) != 0:
            logger.warning("sidecar compaction: wal_checkpoint(TRUNCATE) busy, will retry")
            return {"status": RETRY, "reason": "wal_checkpoint busy", "started_at": started_at}
        stamped = 0
        if has_journal:
            cursor = conn.execute(
                "UPDATE graph_operations SET sidecar_compacted_at = ? "
                "WHERE committed_at IS NOT NULL AND committed_at < ? "
                "AND sidecar_compacted_at IS NULL",
                (_utc_now_iso(), started_at),
            )
            stamped = cursor.rowcount
        return {"status": COMPACTED, "started_at": started_at, "ops_stamped": stamped}
    finally:
        conn.close()


__all__ = ["COMPACTED", "RETRY", "run_sidecar_compaction"]
