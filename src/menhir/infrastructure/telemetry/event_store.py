"""MCP event, lab-run, failure, and episode-task telemetry operations."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from menhir.infrastructure.telemetry.helpers import _json_default, _span_days, _utc_now_iso

logger = logging.getLogger(__name__)


class TelemetryEventStoreMixin:
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

    def record_recall_lab_run(
        self,
        *,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> int | None:
        """Persist one privacy-filtered Recall Lab result and return its run id."""

        self._ensure_ready()
        judgment = result_payload.get("judgment") or {}
        arm_summaries = [
            {
                "id": arm.get("id"),
                "label": arm.get("label"),
                "ok": arm.get("ok"),
                "degraded": arm.get("degraded", False),
                "hits": len(arm.get("results") or []),
                "candidates_evaluated": arm.get("candidates_evaluated", 0),
            }
            for arm in result_payload.get("arms") or []
        ]
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO recall_lab_runs (
                        recorded_at, query, preset, namespace, judge_enabled,
                        judge_ok, judge_model, winner_id, tied_ids_json,
                        arms_json, request_json, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        str(result_payload.get("query") or request_payload.get("query") or ""),
                        str(result_payload.get("preset") or request_payload.get("preset") or ""),
                        result_payload.get("namespace"),
                        1 if request_payload.get("judge") else 0,
                        (1 if judgment.get("ok") else 0) if judgment else None,
                        judgment.get("model"),
                        judgment.get("winner_id"),
                        json.dumps(judgment.get("tied_ids") or [], default=_json_default),
                        json.dumps(arm_summaries, default=_json_default),
                        json.dumps(request_payload, default=_json_default),
                        json.dumps(result_payload, default=_json_default),
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)
        except sqlite3.Error:
            logger.warning("Failed to save Recall Lab run", exc_info=True)
            return None

    def fetch_recall_lab_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent Recall Lab run summaries, newest first."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, recorded_at, query, preset, namespace, judge_enabled,
                       judge_ok, judge_model, winner_id, tied_ids_json, arms_json
                FROM recall_lab_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["judge_enabled"] = bool(item["judge_enabled"])
            item["judge_ok"] = None if item["judge_ok"] is None else bool(item["judge_ok"])
            item["tied_ids"] = json.loads(item.pop("tied_ids_json"))
            item["arms"] = json.loads(item.pop("arms_json"))
            results.append(item)
        return results

    def fetch_recall_lab_run(self, run_id: int) -> dict[str, Any] | None:
        """Return the complete stored request and result for one Recall Lab run."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, recorded_at, request_json, result_json
                FROM recall_lab_runs
                WHERE id = ?
                """,
                (int(run_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "recorded_at": row["recorded_at"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }

    def record_extraction_lab_run(
        self,
        *,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
        namespace: str | None = None,
    ) -> int | None:
        """Persist one Extraction Lab run and return its run id.

        Mirrors record_recall_lab_run's shape; arms_json carries a per-arm summary of
        gold scores + error state instead of retrieval-hit counts.

        ``source_namespace`` is the production fixture's real Graphiti group id. Use it as
        durable erasure lineage when the caller does not explicitly supply ``namespace``.
        Synthetic fixtures have no tenant owner, so they are stamped with an explicit lab scope
        rather than NULL; NULL is reserved for genuine pre-lineage historical residue.
        """

        self._ensure_ready()
        arm_summaries = [
            {
                "id": arm.get("id"),
                "label": arm.get("label"),
                "ok": arm.get("ok"),
                "tuning": arm.get("tuning"),
                "gold_scores": arm.get("gold_scores"),
                "error": arm.get("error"),
            }
            for arm in result_payload.get("arms") or []
        ]
        effective_namespace = (
            str(namespace or "").strip()
            or str(request_payload.get("source_namespace") or "").strip()
            or "__extraction_lab_unscoped__"
        )
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO extraction_lab_runs (
                        recorded_at, current_message, arms_json, request_json, result_json,
                        namespace
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        str(result_payload.get("current_message")
                            or request_payload.get("current_message") or ""),
                        json.dumps(arm_summaries, default=_json_default),
                        json.dumps(request_payload, default=_json_default),
                        json.dumps(result_payload, default=_json_default),
                        effective_namespace,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)
        except sqlite3.Error:
            logger.warning("Failed to save Extraction Lab run", exc_info=True)
            return None

    def fetch_extraction_lab_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent Extraction Lab run summaries, newest first."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, recorded_at, current_message, arms_json
                FROM extraction_lab_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["arms"] = json.loads(item.pop("arms_json"))
            results.append(item)
        return results

    def fetch_extraction_lab_run(self, run_id: int) -> dict[str, Any] | None:
        """Return the complete stored request and result for one Extraction Lab run."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, recorded_at, request_json, result_json
                FROM extraction_lab_runs
                WHERE id = ?
                """,
                (int(run_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "recorded_at": row["recorded_at"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }

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
        """Fetch the most recent structured failure records."""

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
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_episode_task_event(
        self,
        *,
        recorded_at: str,
        episode_uuid: str,
        parent_task: str | None,
        child_task: str | None,
        phase: str,
        kind: str | None,
        model: str | None,
        endpoint: str | None,
        scheduler_task: str | None,
        details_json: str | None,
    ) -> None:
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO episode_task_events (
                    recorded_at,
                    episode_uuid,
                    parent_task,
                    child_task,
                    phase,
                    kind,
                    model,
                    endpoint,
                    scheduler_task,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    episode_uuid,
                    parent_task,
                    child_task,
                    phase,
                    kind,
                    model,
                    endpoint,
                    scheduler_task,
                    details_json,
                ),
            )
            conn.commit()

    def fetch_episode_task_events(
        self, *, episode_uuid: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, recorded_at, episode_uuid, parent_task, child_task, phase,
                       kind, model, endpoint, scheduler_task, details_json
                FROM episode_task_events
                WHERE episode_uuid = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (episode_uuid, max(1, min(limit, 100))),
            ).fetchall()
            return [dict(row) for row in rows]

    def fetch_episode_task_events_map(
        self,
        *,
        episode_uuids: list[str],
        limit_per_episode: int = 6,
    ) -> dict[str, list[dict[str, Any]]]:
        cleaned = [str(uuid).strip() for uuid in episode_uuids if str(uuid).strip()]
        if not cleaned:
            return {}
        self._ensure_ready()
        placeholders = ",".join("?" for _ in cleaned)
        query = f"""
            SELECT id, recorded_at, episode_uuid, parent_task, child_task, phase,
                   kind, model, endpoint, scheduler_task, details_json
            FROM (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY episode_uuid
                           ORDER BY recorded_at DESC, id DESC
                       ) AS rn
                FROM episode_task_events
                WHERE episode_uuid IN ({placeholders})
            )
            WHERE rn <= ?
            ORDER BY episode_uuid, recorded_at DESC, id DESC
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                query, (*cleaned, max(1, min(limit_per_episode, 20)))
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            payload = dict(row)
            grouped.setdefault(str(payload["episode_uuid"]), []).append(payload)
        return grouped
