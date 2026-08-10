"""Durable provider-reported LLM token usage telemetry."""

from __future__ import annotations

import sqlite3
from typing import Any


def initialize_llm_usage_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_usage_events (
            call_id TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            run_id TEXT,
            episode_uuid TEXT,
            operation TEXT,
            kind TEXT NOT NULL,
            model TEXT,
            endpoint TEXT,
            status TEXT NOT NULL,
            duration_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cached_input_tokens INTEGER,
            reasoning_output_tokens INTEGER,
            provider_usage_json TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_usage_run_recorded
        ON llm_usage_events (run_id, recorded_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_usage_episode_recorded
        ON llm_usage_events (episode_uuid, recorded_at)
        """
    )


class TelemetryLLMUsageStoreMixin:
    """Persistence and aggregation methods composed into ``McpTelemetryStore``."""

    def record_llm_usage_event(
        self,
        *,
        call_id: str,
        recorded_at: str,
        run_id: str | None,
        episode_uuid: str | None,
        operation: str | None,
        kind: str,
        model: str | None,
        endpoint: str | None,
        status: str,
        duration_ms: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cached_input_tokens: int | None,
        reasoning_output_tokens: int | None,
        provider_usage_json: str | None,
        error: str | None,
    ) -> bool:
        self._ensure_ready()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO llm_usage_events (
                    call_id, recorded_at, run_id, episode_uuid, operation, kind,
                    model, endpoint, status, duration_ms, input_tokens, output_tokens,
                    total_tokens, cached_input_tokens, reasoning_output_tokens,
                    provider_usage_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    recorded_at,
                    run_id,
                    episode_uuid,
                    operation,
                    kind,
                    model,
                    endpoint,
                    status,
                    duration_ms,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cached_input_tokens,
                    reasoning_output_tokens,
                    provider_usage_json,
                    error,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def fetch_llm_usage_summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        self._ensure_ready()
        where = "WHERE run_id = ?" if run_id is not None else ""
        params: tuple[Any, ...] = (run_id,) if run_id is not None else ()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS calls,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_calls,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_calls,
                       SUM(CASE WHEN status = 'completed' AND total_tokens IS NULL THEN 1 ELSE 0 END)
                           AS missing_usage_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(reasoning_output_tokens), 0) AS reasoning_output_tokens
                FROM llm_usage_events
                {where}
                """,
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT kind, model, endpoint, COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(reasoning_output_tokens), 0) AS reasoning_output_tokens
                FROM llm_usage_events
                {where}
                GROUP BY kind, model, endpoint
                ORDER BY total_tokens DESC, calls DESC
                """,
                params,
            ).fetchall()
        summary = dict(totals) if totals is not None else {}
        summary["run_id"] = run_id
        summary["by_model"] = [dict(row) for row in rows]
        return summary
