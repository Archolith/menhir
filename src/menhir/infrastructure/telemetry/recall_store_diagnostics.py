"""Enrichment, lifecycle, and conflict-resolution diagnostics."""

from __future__ import annotations

import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _utc_now_iso


class TelemetryDiagnosticsMixin:
    def fetch_enrichment_rate(self, *, since_hours: int = 24) -> dict[str, Any]:
        """Ingestion volume, enrichment success rate, avg/p95 enrichment latency."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                    AVG(CASE WHEN success = 1 THEN duration_ms END) AS avg_duration_ms,
                    group_concat(
                        CASE WHEN success = 1 AND duration_ms IS NOT NULL
                             THEN CAST(duration_ms AS TEXT) END,
                        ','
                    ) AS success_latencies_csv
                FROM mcp_events
                WHERE substr(replace(started_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                  AND operation = 'episode_enrichment'
                """,
                (since_hours,),
            ).fetchone()
        total = int(row["total"]) if row else 0
        successes = int(row["successes"] or 0) if row else 0
        avg_ms = row["avg_duration_ms"] if row else None
        raw_csv: str | None = row["success_latencies_csv"] if row else None
        sorted_latencies = (
            sorted(int(v) for v in raw_csv.split(",") if v) if raw_csv else []
        )
        return {
            "total": total,
            "successes": successes,
            "success_rate_pct": round(successes / total * 100, 1) if total > 0 else 0.0,
            "avg_duration_ms": round(float(avg_ms)) if avg_ms is not None else None,
            "p95_duration_ms": self._percentile(sorted_latencies, 0.95),
        }

    def fetch_lifecycle_summary(self, *, since_hours: int = 24) -> dict[str, Any]:
        """Compression events, rehydration events, conflict events — counts + latest."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT event, COUNT(*) AS cnt, MAX(recorded_at) AS latest
                FROM lifecycle_events
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                GROUP BY event
                ORDER BY cnt DESC
                """,
                (since_hours,),
            ).fetchall()
        return {
            row["event"]: {"count": int(row["cnt"]), "latest": row["latest"]}
            for row in rows
        }

    def record_conflict_resolution(
        self,
        *,
        uuid_a: str,
        uuid_b: str,
        status: str,
        group_id: str,
        action: str,
        reviewed_by: str,
    ) -> None:
        """Record a conflict resolution for a specific reviewed pair.

        UUIDs are sorted so lookup is order-independent.
        Uses INSERT OR REPLACE — re-resolving the same pair overwrites the row.
        """
        pair = tuple(sorted([uuid_a, uuid_b]))
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conflict_resolutions
                    (resolved_at, uuid_a, uuid_b, status, group_id, action, reviewed_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    pair[0],
                    pair[1],
                    status,
                    group_id,
                    action,
                    reviewed_by,
                ),
            )
            conn.commit()

    def is_pair_resolved(
        self, uuid_a: str, uuid_b: str, *, cooldown_days: int = 0
    ) -> bool:
        """Check if a pair has a suppression row in conflict_resolutions.

        Args:
            uuid_a: First node UUID.
            uuid_b: Second node UUID (order doesn't matter).
            cooldown_days: If > 0, only suppress if resolved within this many days.
                           If 0 (default), suppress permanently.
        """
        pair = tuple(sorted([uuid_a, uuid_b]))
        self._ensure_ready()
        with self._connect() as conn:
            if cooldown_days > 0:
                row = conn.execute(
                    """
                    SELECT 1 FROM conflict_resolutions
                    WHERE uuid_a = ? AND uuid_b = ?
                      AND substr(replace(resolved_at, 'T', ' '), 1, 19) > datetime('now', ?)
                    """,
                    (pair[0], pair[1], f"-{cooldown_days} days"),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM conflict_resolutions WHERE uuid_a = ? AND uuid_b = ?",
                    (pair[0], pair[1]),
                ).fetchone()
        return row is not None
