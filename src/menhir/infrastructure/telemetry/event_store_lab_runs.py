"""Recall and Extraction Lab run persistence for the telemetry event store."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _json_default, _utc_now_iso

logger = logging.getLogger(__name__)


class _EventStoreLabRunsMixin:
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
