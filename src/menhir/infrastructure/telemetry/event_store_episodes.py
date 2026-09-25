"""Episode task event persistence for the telemetry event store."""

from __future__ import annotations

import sqlite3
from typing import Any


class _EventStoreEpisodesMixin:
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
