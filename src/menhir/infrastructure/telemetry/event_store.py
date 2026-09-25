"""MCP event, lab-run, failure, and episode-task telemetry operations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.event_store_episodes import _EventStoreEpisodesMixin
from menhir.infrastructure.telemetry.event_store_failures import _EventStoreFailuresMixin
from menhir.infrastructure.telemetry.event_store_lab_runs import _EventStoreLabRunsMixin

logger = logging.getLogger(__name__)


# Cohesive method families live in the event_store_* sibling modules beside this facade.
class TelemetryEventStoreMixin(
    _EventStoreLabRunsMixin,
    _EventStoreFailuresMixin,
    _EventStoreEpisodesMixin,
):
    def record(
        self,
        *,
        kind: str,
        operation: str,
        started_at: str,
        completed_at: str,
        duration_ms: int,
        success: bool,
        error: str | None,
        input_size: int | None,
        result_size: int | None,
        payload_preview: str | None,
        namespace: str | None = None,
        node_uuid: str | None = None,
        client_name: str | None = None,
        client_id: str | None = None,
        session_id: str | None = None,
        tier: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Persist a single MCP event.

        CF-29: the identity columns (`client_name`, `client_id`, `session_id`, `tier`) and `stage`
        are resolved by the caller from the request context, not read here -- this layer has no
        request scope. All five default to None so background/internal writers that have no caller
        are unaffected.
        """

        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_events (
                    started_at,
                    completed_at,
                    duration_ms,
                    operation,
                    kind,
                    success,
                    error,
                    input_size,
                    result_size,
                    payload_preview,
                    namespace,
                    node_uuid,
                    client_name,
                    client_id,
                    session_id,
                    tier,
                    stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    completed_at,
                    duration_ms,
                    operation,
                    kind,
                    1 if success else 0,
                    error,
                    input_size,
                    result_size,
                    payload_preview,
                    namespace,
                    node_uuid,
                    client_name,
                    client_id,
                    session_id,
                    tier,
                    stage,
                ),
            )
            conn.commit()

    def fetch_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch the most recent MCP events for the explorer display."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, started_at, completed_at, duration_ms, operation, kind,
                       success, error, input_size, result_size, payload_preview
                FROM mcp_events
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
