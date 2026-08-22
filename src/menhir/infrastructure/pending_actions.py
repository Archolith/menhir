"""SQLite-backed work queue for deferred LLM operations on graph nodes.

The ``action`` column is a free-form string so new LLM-dependent operations
can reuse this table without schema changes.  Known actions:

- ``compress``          — decay compression (M4)
- ``rehydrate``         — context merge on COMPRESSED retrieval (M4 rehydration)

Planned / future actions:

- ``resolve_conflict``  — LLM-backed contradiction resolution
- ``refine_summary``    — periodic summary improvement
- ``extract_emotions``  — emotional quotient enrichment

To add a new action: upsert rows with the new action string, query with
``fetch_pending(action="your_action")``, and delete on success via
``complete(node_uuid)``.  Retry logic is handled by the ``attempts`` column
and the ``max_attempts`` filter in ``fetch_pending``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)




def _migrate_pending_actions_key(conn: sqlite3.Connection) -> bool:
    """Rebuild `pending_actions` if it still carries the single-column key (CF-243).

    Returns True when a rebuild happened. Idempotent: a table already on the composite key is
    left alone, so this is safe to run on every startup.

    Rows are carried across, collapsing any that collide on `(node_uuid, action)` -- there can be
    at most one such row per pair under the OLD key too, so nothing is actually merged; the
    INSERT ... SELECT is written defensively rather than because a collision is possible.
    """
    cols = conn.execute("PRAGMA table_info(pending_actions)").fetchall()
    if not cols:
        return False
    # cols rows are (cid, name, type, notnull, dflt, pk); pk is the 1-based position in the key.
    key = sorted((c[5], c[1]) for c in cols if c[5])
    if [name for _, name in key] == ["node_uuid", "action"]:
        return False

    conn.execute("ALTER TABLE pending_actions RENAME TO pending_actions_old")
    conn.execute(
        """
        CREATE TABLE pending_actions (
            node_uuid       TEXT NOT NULL,
            action          TEXT NOT NULL,
            context         TEXT,
            source_uuid     TEXT,
            attempts        INTEGER DEFAULT 0,
            failed_at       TEXT,
            failure_reason  TEXT,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (node_uuid, action)
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO pending_actions
            (node_uuid, action, context, source_uuid, attempts, failed_at, failure_reason, created_at)
        SELECT node_uuid, action, context, source_uuid, attempts, failed_at, failure_reason, created_at
        FROM pending_actions_old
        """
    )
    conn.execute("DROP TABLE pending_actions_old")
    logger.info("pending_actions migrated to the (node_uuid, action) key (CF-243)")
    return True


@dataclass
class PendingActionStore:
    """Manages the pending_actions table in the telemetry SQLite database.

    Schema initialization (``mkdir`` + ``CREATE TABLE``) is memoized per ``db_path`` at the class
    level, keyed on the resolved path, so cheap per-request constructions of this dataclass do not
    re-run DDL. The memo is keyed by path -- NOT a module-level singleton -- so stores built
    against the per-test redirected ``MENHIR_MCP_TELEMETRY_DB`` keep resolving to that test's
    isolated DB instead of binding once to the operator's real telemetry database, and stores on
    two different paths each initialize their own schema.
    """

    db_path: Path = field(default_factory=default_telemetry_db_path)

    #: Resolved ``db_path`` values whose schema has already been created this process. Keyed on
    #: the normalized path so two spellings of the same file (relative vs absolute, ``..``, etc.)
    #: initialize only once.
    _initialized_paths: ClassVar[set[Path]] = set()
    #: Class-level lock so first-run DDL actually serializes across instances sharing a path.
    _class_lock: ClassVar[threading.Lock] = threading.Lock()

    def _resolve_db_key(self) -> Path:
        """Return the normalized key for memoizing this store's initialization."""
        return self.db_path.expanduser().resolve()

    def _ensure_ready(self) -> None:
        key = self._resolve_db_key()
        if key in type(self)._initialized_paths:
            return
        with type(self)._class_lock:
            if key in type(self)._initialized_paths:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with connect_telemetry_db(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_actions (
                        node_uuid       TEXT NOT NULL,
                        action          TEXT NOT NULL,
                        context         TEXT,
                        source_uuid     TEXT,
                        attempts        INTEGER DEFAULT 0,
                        failed_at       TEXT,
                        failure_reason  TEXT,
                        created_at      TEXT NOT NULL,
                        PRIMARY KEY (node_uuid, action)
                    )
                    """
                )
                # CF-243 migration. The original key was `node_uuid` alone, so a node could hold
                # only ONE pending action: enqueuing a second kind overwrote the first (losing its
                # attempts/failed_at/failure_reason) and `complete()` deleted whichever had won.
                # Two kinds exist in production -- `compress` and `rehydrate` -- and a node can
                # legitimately be pending both.
                #
                # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so an
                # existing deployment keeps the old key unless it is rebuilt here. Detected by
                # reading the actual key rather than a version marker.
                _migrate_pending_actions_key(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pending_actions_action
                    ON pending_actions (action)
                    """
                )
                conn.commit()
            type(self)._initialized_paths.add(key)

    def upsert(
        self,
        node_uuid: str,
        action: str,
        *,
        context: str | None = None,
        source_uuid: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Insert or update a pending action. Increments attempts on conflict."""
        self._ensure_ready()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pending_actions
                    (node_uuid, action, context, source_uuid, attempts, failed_at, failure_reason, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(node_uuid, action) DO UPDATE SET
                    context = COALESCE(excluded.context, pending_actions.context),
                    source_uuid = COALESCE(excluded.source_uuid, pending_actions.source_uuid),
                    attempts = pending_actions.attempts + 1,
                    failed_at = excluded.failed_at,
                    failure_reason = COALESCE(excluded.failure_reason, pending_actions.failure_reason)
                """,
                (node_uuid, action, context, source_uuid, now if failure_reason else None, failure_reason, now),
            )
            conn.commit()

    def complete(self, node_uuid: str, action: str | None = None) -> bool:
        """Remove a pending action after successful processing.

        *action* identifies WHICH pending action completed (CF-243). Omitting it clears every
        pending action for the node, which is the pre-CF-243 behaviour and is why the parameter is
        optional rather than required: a caller that genuinely means "this node is done with
        everything" still has a way to say so.

        Every production caller passes it. A node can hold both a `compress` and a `rehydrate`,
        and completing one used to delete the other.
        """
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            if action is None:
                cursor = conn.execute(
                    "DELETE FROM pending_actions WHERE node_uuid = ?", (node_uuid,)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM pending_actions WHERE node_uuid = ? AND action = ?",
                    (node_uuid, action),
                )
            conn.commit()
            return cursor.rowcount > 0

    def fetch_pending(
        self,
        action: str | None = None,
        *,
        max_attempts: int = 10,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch pending actions, optionally filtered by action type."""
        self._ensure_ready()
        if action is not None:
            query = """
                SELECT node_uuid, action, context, source_uuid, attempts,
                       failed_at, failure_reason, created_at
                FROM pending_actions
                WHERE action = ? AND attempts < ?
                ORDER BY created_at ASC
                LIMIT ?
            """
            params: tuple[Any, ...] = (action, max_attempts, limit)
        else:
            query = """
                SELECT node_uuid, action, context, source_uuid, attempts,
                       failed_at, failure_reason, created_at
                FROM pending_actions
                WHERE attempts < ?
                ORDER BY created_at ASC
                LIMIT ?
            """
            params = (max_attempts, limit)

        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def count(self, action: str | None = None) -> int:
        """Count pending actions, optionally filtered by action type."""
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            if action is not None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM pending_actions WHERE action = ?",
                    (action,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()
            return row[0] if row else 0
