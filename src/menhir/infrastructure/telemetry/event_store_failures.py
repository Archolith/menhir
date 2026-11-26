"""Failure-event persistence for the telemetry event store."""

from __future__ import annotations

import sqlite3
from typing import Any


_MAX_FETCH_LIMIT = 500


def _clamp_fetch_limit(limit: int) -> int:
    return max(0, min(_MAX_FETCH_LIMIT, limit))


class _EventStoreFailuresMixin:
    def record_failure(
        self,
        *,
        recorded_at: str,
        operation: str,
        episode_uuid: str | None,
        failure_stage: str | None,
        classification: str | None,
        retryable: bool | None,
        processing_attempt: int | None,
        queue_depth: int | None,
        worker_id: str | None,
        error_type: str | None,
        error: str,
        details_json: str | None,
    ) -> bool:
        """Persist one structured failure record.

        Returns True when a new row is written and False when a duplicate is suppressed.
        """

        self._ensure_ready()
        with self._connect() as conn:
            if episode_uuid and processing_attempt is not None:
                # Serialize the duplicate check and insert across SQLite connections.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM failure_events
                    WHERE operation = ?
                      AND episode_uuid = ?
                      AND coalesce(failure_stage, '') = coalesce(?, '')
                      AND coalesce(classification, '') = coalesce(?, '')
                      AND coalesce(processing_attempt, -1) = ?
                      AND error = ?
                    LIMIT 1
                    """,
                    (
                        operation,
                        episode_uuid,
                        failure_stage,
                        classification,
                        processing_attempt,
                        error,
                    ),
                ).fetchone()
                if existing is not None:
                    return False
            conn.execute(
                """
                INSERT INTO failure_events (
                    recorded_at,
                    operation,
                    episode_uuid,
                    failure_stage,
                    classification,
                    retryable,
                    processing_attempt,
                    queue_depth,
                    worker_id,
                    error_type,
                    error,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    operation,
                    episode_uuid,
                    failure_stage,
                    classification,
                    None if retryable is None else (1 if retryable else 0),
                    processing_attempt,
                    queue_depth,
                    worker_id,
                    error_type,
                    error,
                    details_json,
                ),
            )
            conn.commit()
        return True

    def fetch_recent_failures(
        self, limit: int = 50, *, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch up to 500 of the most recent structured failure records."""

        self._ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if episode_uuid:
            clauses.append("episode_uuid = ?")
            params.append(episode_uuid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, recorded_at, operation, episode_uuid, failure_stage,
                       classification, retryable, processing_attempt, queue_depth,
                       worker_id, error_type, error, details_json
                FROM failure_events
                {where}
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (*params, _clamp_fetch_limit(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
