"""Durable provider-reported LLM token usage telemetry."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

#: ``Class[:status[:code]]`` as built by openai_rate_limit.provider_error_code.
_ERROR_CODE_SHAPE = re.compile(r"[A-Za-z0-9_.-]{1,64}(?::[0-9]{1,3}(?::[A-Za-z0-9_.:-]{1,64})?)?")


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
            error TEXT,
            error_code TEXT
        )
        """
    )
    # A sidecar created before error_code existed keeps its old column set under IF NOT EXISTS.
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(llm_usage_events)")}
    if "error_code" not in columns:
        conn.execute("ALTER TABLE llm_usage_events ADD COLUMN error_code TEXT")
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
        error_code: str | None = None,
    ) -> bool:
        # error_code is inventoried as non-content; a direct caller must not be able to put free
        # text there, so anything outside the identifier shape is dropped.
        if error_code is not None and not _ERROR_CODE_SHAPE.fullmatch(error_code):
            error_code = None
        self._ensure_ready()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO llm_usage_events (
                    call_id, recorded_at, run_id, episode_uuid, operation, kind,
                    model, endpoint, status, duration_ms, input_tokens, output_tokens,
                    total_tokens, cached_input_tokens, reasoning_output_tokens,
                    provider_usage_json, error, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    error_code,
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
            failed_where = f"{where} AND status = 'failed'" if where else "WHERE status = 'failed'"
            failures = conn.execute(
                f"""
                SELECT COALESCE(error_code, 'unrecorded') AS error_code, COUNT(*) AS calls
                FROM llm_usage_events
                {failed_where}
                GROUP BY COALESCE(error_code, 'unrecorded')
                ORDER BY calls DESC
                """,
                params,
            ).fetchall()
        summary = dict(totals) if totals is not None else {}
        summary["run_id"] = run_id
        summary["by_model"] = [dict(row) for row in rows]
        summary["failed_by_error_code"] = {row["error_code"]: row["calls"] for row in failures}
        return summary
