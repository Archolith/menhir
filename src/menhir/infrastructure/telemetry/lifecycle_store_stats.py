"""Telemetry retention pruning and per-operation/feature statistics."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _span_days


class TelemetryRetentionStatsMixin:
    #: Retention tiers for the sidecar (CF-171). Table -> (timestamp column, tier).
    #:
    #: Tiered by ROLE rather than uniformly, because these tables answer different questions.
    #: The high-volume group is per-operation observability: `lifecycle_events` alone writes
    #: ~30 rows per ingest, which is what produces the measured ~664 bytes/row growth. The
    #: diagnostic group is what someone reads when investigating a defect weeks later, and a
    #: 30-day window there would delete the history needed to correlate a recurring failure.
    #:
    #: `merge_audit` is DELIBERATELY ABSENT and must stay absent. It is not observability: its
    #: `snapshot_json` holds the absorbed node's content and is the ONLY surviving copy once a
    #: merge DETACH-DELETEs that node. Two recovery paths read it -- `legacy_unmerge_coordinator`
    #: to restore an absorbed node, and `merge_recoverability` to report whether a merge can be
    #: undone at all. The row outliving the node is the design, not an oversight. Time-pruning it
    #: would silently convert "recoverable" into "permanently lost", which is data destruction
    #: wearing the costume of disk hygiene. Erasure already suppresses these rows by subject,
    #: which is the correct deletion axis for them.
    _RETENTION_TIERS: dict[str, tuple[str, str]] = {
        # High-volume observability.
        "lifecycle_events": ("recorded_at", "observability"),
        "mcp_events": ("started_at", "observability"),
        "episode_task_events": ("recorded_at", "observability"),
        "lifecycle_actions": ("recorded_at", "observability"),
        # Diagnostic / investigative.
        "failure_events": ("recorded_at", "diagnostic"),
        "recall_receipts": ("created_at", "diagnostic"),
        "conflict_resolutions": ("resolved_at", "diagnostic"),
        "recall_lab_runs": ("recorded_at", "diagnostic"),
        "extraction_lab_runs": ("recorded_at", "diagnostic"),
    }

    def prune_telemetry_tables(
        self, *, observability_days: int, diagnostic_days: int
    ) -> dict[str, int]:
        """Delete rows past the retention window for each sidecar table (CF-171).

        Returns a per-table deleted count, omitting tables that do not exist in this database --
        four of the nine are created lazily or live in a separate store file, and a pruner that
        raised on a missing table would abort the whole sweep and leave the high-volume tables
        unpruned. A missing table is not an error; it is a table with nothing to prune.

        The comparison uses the `substr(replace(col, 'T', ' '), 1, 19)` idiom rather than a plain
        `<`. That is not decoration: CF-6 in this codebase was a timestamp-separator mismatch
        where ISO-8601 values with a 'T' compared wrong against `datetime('now', ...)`, silently
        widening every window. A pruner that got this wrong would silently delete nothing -- or,
        far worse, silently delete the wrong side of the boundary.
        """

        self._ensure_ready()
        deleted: dict[str, int] = {}
        windows = {
            "observability": max(1, int(observability_days)),
            "diagnostic": max(1, int(diagnostic_days)),
        }
        with self._connect() as conn:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table, (column, tier) in self._RETENTION_TIERS.items():
                if table not in existing:
                    continue
                cursor = conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE substr(replace({column}, 'T', ' '), 1, 19)
                          < datetime('now', '-' || ? || ' days')
                    """,
                    (windows[tier],),
                )
                if cursor.rowcount > 0:
                    deleted[table] = cursor.rowcount
            conn.commit()
        return deleted

    def prune_old_revisions(self, *, retention_days: int) -> int:
        """Delete memory_revisions rows older than retention_days. Returns rows deleted.

        `retention_days` is deliberately REQUIRED. It used to default to 14, duplicating the
        `revision_retention_days` setting rather than reading it, so any wiring that forgot to
        thread the value would have silently ignored MENHIR_REVISION_RETENTION_DAYS while
        appearing to honour it. There is one source of truth for the window and the caller
        must supply it.
        """

        self._ensure_ready()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM memory_revisions
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) < datetime('now', '-' || ? || ' days')
                """,
                (max(1, retention_days),),
            )
            conn.commit()
            return cursor.rowcount

    def fetch_lifecycle_actions_summary(
        self, *, since_hours: int = 24
    ) -> dict[str, Any]:
        """Aggregate lifecycle_actions for the stats report."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT action, COUNT(*) AS cnt,
                       AVG(duration_ms) AS avg_ms,
                       SUM(CASE WHEN llm_used = 1 THEN 1 ELSE 0 END) AS llm_count
                FROM lifecycle_actions
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                GROUP BY action
                """,
                (since_hours,),
            ).fetchall()
            revision_count = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM memory_revisions
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                """,
                (since_hours,),
            ).fetchone()
        result: dict[str, Any] = {}
        for row in rows:
            result[row["action"]] = {
                "count": int(row["cnt"]),
                "avg_ms": round(float(row["avg_ms"])) if row["avg_ms"] else None,
                "llm_count": int(row["llm_count"]),
            }
        result["revisions"] = int(revision_count["cnt"]) if revision_count else 0
        return result

    @staticmethod
    def _percentile(sorted_values: list[int], p: float) -> int | None:
        if not sorted_values:
            return None
        index = max(0, math.ceil(len(sorted_values) * p) - 1)
        return sorted_values[min(index, len(sorted_values) - 1)]

    def fetch_operation_stats(
        self,
        *,
        operation: str | None = None,
        since_hours: int = 24,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Per-operation: call count, success rate, p50/p95 latency_ms."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            agg_rows = conn.execute(
                """
                SELECT
                    operation,
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                    group_concat(
                        CASE WHEN duration_ms IS NOT NULL
                             THEN CAST(duration_ms AS TEXT) END,
                        ','
                    ) AS latencies_csv
                FROM mcp_events
                WHERE substr(replace(started_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                  AND (? IS NULL OR operation = ?)
                GROUP BY operation
                ORDER BY total_calls DESC
                LIMIT ?
                """,
                (since_hours, operation, operation, limit),
            ).fetchall()

            results: list[dict[str, Any]] = []
            for row in agg_rows:
                total = int(row["total_calls"])
                successes = int(row["successes"])
                rate = round(successes / total * 100, 1) if total > 0 else 0.0
                raw_csv: str | None = row["latencies_csv"]
                sorted_latencies = (
                    sorted(int(v) for v in raw_csv.split(",") if v) if raw_csv else []
                )
                results.append(
                    {
                        "operation": row["operation"],
                        "total_calls": total,
                        "successes": successes,
                        "success_rate_pct": rate,
                        "p50_ms": self._percentile(sorted_latencies, 0.5),
                        "p95_ms": self._percentile(sorted_latencies, 0.95),
                    }
                )
            return results

    def fetch_feature_stats(
        self,
        *,
        since_hours: int | None = None,
        kinds: tuple[str, ...] = ("tool", "resource", "resource_template"),
    ) -> list[dict[str, Any]]:
        """Per-(operation, kind) usage + effectiveness stats for the feature dashboard.

        Usage: total_calls, calls_per_day (derived from first/last seen span).
        Effectiveness: success_rate (calls that did not error), p50/p95 latency, and
        avg result size (mean serialized bytes returned by successful calls).

        ``since_hours=None`` includes all-time history; otherwise only events whose
        ``started_at`` is within the window are counted.

        Note: there is deliberately no "hit rate" here. ``result_size`` measures the
        whole serialized envelope, not item count, so it cannot reliably distinguish
        an empty result from a populated one across tools. Whether a recall actually
        *helped* is captured by the self-reported usefulness signal instead
        (see :meth:`fetch_usefulness_stats`).
        """

        self._ensure_ready()
        placeholders = ",".join("?" for _ in kinds)
        params: list[Any] = list(kinds)
        window_clause = ""
        if since_hours is not None:
            window_clause = "AND substr(replace(started_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')"
            params.append(since_hours)

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT
                    operation,
                    kind,
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                    AVG(CASE WHEN success = 1 THEN result_size END) AS avg_result_size,
                    AVG(input_size) AS avg_input_size,
                    MIN(started_at) AS first_seen,
                    MAX(started_at) AS last_seen,
                    group_concat(
                        CASE WHEN duration_ms IS NOT NULL
                             THEN CAST(duration_ms AS TEXT) END,
                        ','
                    ) AS latencies_csv
                FROM mcp_events
                WHERE kind IN ({placeholders})
                {window_clause}
                GROUP BY operation, kind
                ORDER BY total_calls DESC
                """,
                tuple(params),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            total = int(row["total_calls"])
            successes = int(row["successes"])
            raw_csv: str | None = row["latencies_csv"]
            latencies = sorted(int(v) for v in raw_csv.split(",") if v) if raw_csv else []
            span_days = _span_days(row["first_seen"], row["last_seen"])
            results.append(
                {
                    "operation": row["operation"],
                    "kind": row["kind"],
                    "total_calls": total,
                    "successes": successes,
                    "errors": total - successes,
                    "success_rate_pct": round(successes / total * 100, 1) if total else 0.0,
                    "calls_per_day": round(total / span_days, 2) if span_days else float(total),
                    "p50_ms": self._percentile(latencies, 0.5),
                    "p95_ms": self._percentile(latencies, 0.95),
                    "avg_result_size": int(row["avg_result_size"]) if row["avg_result_size"] is not None else None,
                    "avg_input_size": int(row["avg_input_size"]) if row["avg_input_size"] is not None else None,
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                }
            )
        return results
