"""Opt-in recall query log: query text and ordered result ids, linked to the recall receipt token."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _utc_now_iso

logger = logging.getLogger(__name__)


def initialize_recall_query_log_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_query_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT,
            operation TEXT,
            created_at TEXT,
            client_id TEXT,
            session_id TEXT,
            namespace TEXT,
            preset TEXT,
            result_limit INTEGER,
            query TEXT,
            query_sha256 TEXT,
            result_ids_json TEXT,
            result_scores_json TEXT,
            source_memory_ids_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_query_log_created ON recall_query_log (created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_query_log_token ON recall_query_log (token)"
    )


class TelemetryRecallQueryLogMixin:
    def record_recall_query(
        self,
        *,
        query: str,
        token: str | None = None,
        operation: str = "",
        client_id: str = "",
        session_id: str = "",
        namespace: str | None = None,
        preset: str | None = None,
        limit: int | None = None,
        result_ids: list[Any] | None = None,
        result_scores: list[Any] | None = None,
        source_memory_ids: list[Any] | None = None,
        store_query_text: bool = True,
        created_at: str | None = None,
    ) -> None:
        """Log one recall's query and ordered results for later evaluation-set building.

        Best-effort: a failed write is logged and swallowed so it can never break the recall
        it describes. ``store_query_text=False`` stores only the SHA-256 of the query.
        """
        try:
            self._ensure_ready()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO recall_query_log
                        (token, operation, created_at, client_id, session_id, namespace,
                         preset, result_limit, query, query_sha256, result_ids_json,
                         result_scores_json, source_memory_ids_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token,
                        operation,
                        created_at or _utc_now_iso(),
                        client_id or "",
                        session_id or "",
                        namespace,
                        preset,
                        limit,
                        query if store_query_text else None,
                        hashlib.sha256(query.encode("utf-8")).hexdigest(),
                        json.dumps(list(result_ids or []), default=str),
                        json.dumps(list(result_scores or []), default=str),
                        json.dumps(list(source_memory_ids or []), default=str),
                    ),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 - query logging must never break recall
            logger.debug("record_recall_query skipped", exc_info=True)
