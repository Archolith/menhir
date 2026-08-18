"""Durable erasure-subject inventory store.

An explicit erasure must persist WHICH subjects it is erasing BEFORE it destroys graph
discoverability. Once the graph namespace is deleted it can no longer be queried for the
UUIDs that still need sidecar cleanup, so a crash mid-erasure would strand content forever.
This store is that durable inventory: a normalized membership table keyed by erasure
operation id, so a large namespace erase stays bounded and resumable.

The table stores identifiers and status ONLY. It must never hold memory text, payload
previews, or snapshots -- the whole point is that the erasure record does not itself retain
the erased content. Reading this table after an erasure must reveal nothing about what was
erased beyond which subject keys were targeted.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.telemetry import default_telemetry_db_path

# Closed enum of subject kinds a read-suppression check can ask about. Kept as a plain
# frozenset so callers pass validated strings.
SUBJECT_TYPES = frozenset({"NODE_UUID", "NAMESPACE", "EPISODE_UUID", "SESSION_ID"})


class ErasureSubjectError(Exception):
    """Raised when a subject_type is not one of the recognised SUBJECT_TYPES."""


def suppressed_node_uuids(db_path, candidates) -> frozenset[str]:
    """Of ``candidates``, which node uuids a live (unpurged) erasure covers.

    One query for the whole set, so a page of rows costs a single lookup instead of one per
    row. Used by sidecar readers that return content keyed by node uuid: between a committed
    erasure intent and a finished purge the rows still hold the text, and that window is
    exactly when the supposedly erased content would otherwise be served.

    Fails CLOSED -- an error suppresses every candidate. Serving content this exists to
    withhold is worse than a read returning nothing.
    """
    import logging
    import sqlite3 as _sqlite3

    wanted = [str(c) for c in candidates if c]
    if not wanted:
        return frozenset()
    try:
        with _sqlite3.connect(db_path) as conn:
            # No table means no erasure has ever been recorded against this database, so
            # nothing is suppressed. That is positive evidence, not a failure -- unlike a query
            # error, which cannot distinguish "no erasures" from "cannot tell". Treating the
            # missing table as fail-closed suppressed EVERY content read on any deployment
            # where erasure had never run.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='erasure_subjects'"
            ).fetchone()
            if exists is None:
                return frozenset()
            placeholders = ",".join("?" for _ in wanted)
            rows = conn.execute(
                "SELECT DISTINCT subject_value FROM erasure_subjects "
                f"WHERE subject_type = 'NODE_UUID' AND purged_at IS NULL "
                f"AND subject_value IN ({placeholders})",
                wanted,
            ).fetchall()
        return frozenset(str(r[0]) for r in rows)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "erasure suppression lookup failed; suppressing all %d candidates",
            len(wanted),
            exc_info=True,
        )
        return frozenset(wanted)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ErasureSubjectStore:
    """The ``erasure_subjects`` table: the durable receipt of an in-flight erasure.

    Rows record only an erasure operation id, a subject identifier, and its purge status --
    never any of the erased content. The ``purged_at`` stamp turns a read-suppression
    predicate ("is this subject being erased right now") and a resumed-erasure worklist
    ("which subjects still need cleanup") into the same query.
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
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS erasure_subjects (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        op_id         TEXT NOT NULL,
                        subject_type  TEXT NOT NULL,
                        subject_value TEXT NOT NULL,
                        recorded_at   TEXT NOT NULL,
                        purged_at     TEXT,
                        UNIQUE(op_id, subject_type, subject_value)
                    )
                    """
                )
                # Bounded, resumable cleanup: a resumed erasure finds its remaining work by
                # (op_id, purged_at IS NULL), so keying on the operation keeps one namespace
                # erase's rows together even when it spans many subject values.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_erasure_subjects_op_purged "
                    "ON erasure_subjects (op_id, purged_at)"
                )
                # The read-suppression check asks "is this subject being erased right now",
                # which is a lookup by subject, not by op.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_erasure_subjects_subject "
                    "ON erasure_subjects (subject_type, subject_value)"
                )
                conn.commit()
            self._initialized = True

    # ------------------------------------------------------------------ writes
    def record_subjects(
        self,
        op_id: str,
        subjects: Iterable[tuple[str, str]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Insert (subject_type, subject_value) pairs for ``op_id``; return rows inserted.

        Validates every subject_type against SUBJECT_TYPES, raising ErasureSubjectError on an
        unknown one, and skips empty/blank subject_value entries. Uses INSERT OR IGNORE so
        re-running PREPARE after a crash is idempotent.

        Pass ``conn`` to enlist this insert in a caller-owned SQLite transaction. When ``conn``
        is given, this method does NOT commit -- the caller commits (or rolls back). When it is
        None, a connection is opened and committed here.
        """
        self._ensure_ready()
        now = _utc_now_iso()
        rows: list[tuple[str, str, str, str]] = []
        for subject_type, subject_value in subjects:
            if subject_type not in SUBJECT_TYPES:
                raise ErasureSubjectError(f"unknown subject_type {subject_type!r}")
            if not str(subject_value or "").strip():
                continue
            rows.append((op_id, subject_type, subject_value, now))
        if not rows:
            return 0
        owns = conn is None
        connection = sqlite3.connect(self.db_path) if owns else conn
        try:
            cursor = connection.executemany(
                "INSERT OR IGNORE INTO erasure_subjects "
                "(op_id, subject_type, subject_value, recorded_at) VALUES (?, ?, ?, ?)",
                rows,
            )
            if owns:
                connection.commit()
            return cursor.rowcount
        finally:
            if owns:
                connection.close()

    def mark_purged(
        self,
        op_id: str,
        *,
        subject_type: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Stamp ``purged_at`` on the op's unpurged rows; return rows affected.

        Only stamps rows where ``purged_at IS NULL`` so a resumed run does not rewrite earlier
        timestamps. Same ``conn`` semantics as :meth:`record_subjects`.
        """
        self._ensure_ready()
        now = _utc_now_iso()
        owns = conn is None
        connection = sqlite3.connect(self.db_path) if owns else conn
        try:
            sql = "UPDATE erasure_subjects SET purged_at = ? WHERE op_id = ? AND purged_at IS NULL"
            params: list[Any] = [now, op_id]
            if subject_type is not None:
                if subject_type not in SUBJECT_TYPES:
                    raise ErasureSubjectError(f"unknown subject_type {subject_type!r}")
                sql += " AND subject_type = ?"
                params.append(subject_type)
            cursor = connection.execute(sql, params)
            if owns:
                connection.commit()
            return cursor.rowcount
        finally:
            if owns:
                connection.close()

    # ------------------------------------------------------------------ reads
    def fetch_subjects(
        self,
        op_id: str,
        *,
        subject_type: str | None = None,
        unpurged_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return rows for ``op_id`` as dicts.

        ``unpurged_only=True`` filters to ``purged_at IS NULL`` -- that is how a resumed
        erasure finds the remaining work.
        """
        self._ensure_ready()
        sql = "SELECT * FROM erasure_subjects WHERE op_id = ?"
        params: list[Any] = [op_id]
        if subject_type is not None:
            if subject_type not in SUBJECT_TYPES:
                raise ErasureSubjectError(f"unknown subject_type {subject_type!r}")
            sql += " AND subject_type = ?"
            params.append(subject_type)
        if unpurged_only:
            sql += " AND purged_at IS NULL"
        sql += " ORDER BY id ASC"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def has_live_erasure(self, *, subject_type: str, subject_value: str) -> bool:
        """True when any row matches that subject with ``purged_at IS NULL``.

        This is the read-suppression predicate: an erasure that has PREPARED but not finished
        purging must already suppress reads of its subjects.
        """
        if subject_type not in SUBJECT_TYPES:
            raise ErasureSubjectError(f"unknown subject_type {subject_type!r}")
        self._ensure_ready()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM erasure_subjects "
                "WHERE subject_type = ? AND subject_value = ? AND purged_at IS NULL LIMIT 1",
                (subject_type, subject_value),
            ).fetchone()
            return row is not None

    def count_unpurged(self, op_id: str) -> int:
        """Count the op's rows still awaiting purge."""
        self._ensure_ready()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM erasure_subjects WHERE op_id = ? AND purged_at IS NULL",
                (op_id,),
            ).fetchone()
            return int(row[0])
