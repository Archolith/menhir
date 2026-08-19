"""Lifecycle, merge, revision, and operation-stat telemetry operations."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from menhir.infrastructure.telemetry.helpers import _json_default, _span_days, _utc_now_iso

logger = logging.getLogger(__name__)

#: Ceiling for an archived field value in `memory_revisions`. This is the ONLY surviving copy of
#: a node body once decay compresses it, so the old 2,000-character cap silently discarded the
#: tail of anything longer on the success path (CF-101). Sized to hold a whole memory body
#: rather than a preview; the truncation marker remains for the pathological case.
_MAX_REVISION_VALUE_LEN = 100_000


class MergeAuditUnavailable(RuntimeError):
    """The merge-audit read failed, so its result is unknown rather than empty (CF-205).

    Exists so a caller cannot accidentally treat a failed read as "no snapshot recorded". Any
    handler catching this must report the merge's recoverability as UNKNOWN; reporting it as
    unrecoverable is the exact wrong answer, because it discards recovery material that may
    well be sitting in the sidecar.
    """


class TelemetryLifecycleStoreMixin:
    def record_lifecycle_event(
        self,
        *,
        recorded_at: str,
        component: str,
        event: str,
        state: str,
        episode_uuid: str | None,
        details_json: str | None,
    ) -> None:
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lifecycle_events (
                    recorded_at,
                    phase,
                    event,
                    status,
                    episode_uuid,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    component,
                    event,
                    state,
                    episode_uuid,
                    details_json,
                ),
            )
            conn.commit()

    def fetch_recent_lifecycle_events(
        self,
        *,
        limit: int = 50,
        component: str | None = None,
        episode_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if component:
            clauses.append("phase = ?")
            params.append(component)
        if episode_uuid:
            clauses.append("episode_uuid = ?")
            params.append(episode_uuid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id,
                       recorded_at,
                       phase AS component,
                       event,
                       status AS state,
                       episode_uuid,
                       details_json
                FROM lifecycle_events
                {where}
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (*params, max(1, min(limit, 200))),
            ).fetchall()
            return [dict(row) for row in rows]

    # "demote"/"delete" and their triggers back the F5 demote-with-TTL lifecycle
    # (LifecycleService: consolidation demote + TTL-expiry delete). They were added
    # to the caller but missing here, so every demote/delete telemetry write raised
    # ValueError and was silently dropped (the audit "chain of custody" for deletes).
    _VALID_ACTIONS = {"compress", "rehydrate", "gone", "demote", "delete"}
    _VALID_TRIGGERS = {
        "scheduler",
        "manual",
        "decay_sweep",
        "consolidation_demote",
        "demote_ttl_expiry",
    }
    _VALID_FIELDS = {"content", "summary", "scope", "freshness"}
    _VALID_CHANGED_BY = {"ingest", "consolidation", "conflict_resolution", "decay"}

    def record_lifecycle_action(
        self,
        *,
        action: str,
        node_uuid: str,
        session_id: str | None = None,
        trigger: str,
        before_freshness: str | None = None,
        after_freshness: str | None = None,
        llm_used: bool = False,
        duration_ms: int | None = None,
        notes: str | None = None,
    ) -> None:
        """Persist a compress/rehydrate/gone/demote/delete lifecycle action."""

        if action not in self._VALID_ACTIONS:
            raise ValueError(
                f"Invalid lifecycle action {action!r}; expected one of {self._VALID_ACTIONS}"
            )
        if trigger not in self._VALID_TRIGGERS:
            raise ValueError(
                f"Invalid trigger {trigger!r}; expected one of {self._VALID_TRIGGERS}"
            )
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO lifecycle_actions (
                        recorded_at, action, node_uuid, session_id, trigger,
                        before_freshness, after_freshness, llm_used, duration_ms, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        action,
                        node_uuid,
                        session_id,
                        trigger,
                        before_freshness,
                        after_freshness,
                        1 if llm_used else 0,
                        duration_ms,
                        notes,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record lifecycle action action=%s node=%s",
                action,
                node_uuid,
                exc_info=True,
            )

    def record_merge(
        self,
        *,
        survivor_uuid: str,
        absorbed_uuid: str,
        similarity: float | None,
        snapshot_json: str,
        survivor_namespace: str | None = None,
        absorbed_namespace: str | None = None,
    ) -> None:
        """Persist a merge to the durable sidecar audit.

        ``snapshot_json`` is the same audit entry written to ``survivor.merge_audit``
        in the graph (absorbed node's properties, relationships, and MENTIONS
        provenance). Unlike the graph property, this row survives deletion of the
        survivor, so an absorbed node stays recoverable even after decay or orphan
        cleanup removes the node that absorbed it.
        """
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO merge_audit (
                        recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json,
                        survivor_namespace, absorbed_namespace
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        survivor_uuid,
                        absorbed_uuid,
                        float(similarity) if similarity is not None else None,
                        snapshot_json,
                        survivor_namespace,
                        absorbed_namespace,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record merge audit survivor=%s absorbed=%s",
                survivor_uuid,
                absorbed_uuid,
                exc_info=True,
            )

    def fetch_merge_audit(
        self,
        *,
        survivor_uuid: str | None = None,
        absorbed_uuid: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read merge history from the durable sidecar.

        Filter by survivor (what did this node absorb?) or by absorbed uuid (what
        happened to this node?). With neither, returns the most recent merges.
        """
        self._ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if survivor_uuid:
            clauses.append("survivor_uuid = ?")
            params.append(survivor_uuid)
        if absorbed_uuid:
            clauses.append("absorbed_uuid = ?")
            params.append(absorbed_uuid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""
                    SELECT recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json
                    FROM merge_audit
                    {where}
                    ORDER BY recorded_at DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
                out = [dict(r) for r in rows]
        except sqlite3.Error as exc:
            # CF-205: this used to log and return []. An empty list is the caller's evidence that
            # NO snapshot exists, and both consumers turn that into a confident verdict --
            # ``legacy_unmerge_coordinator`` reports "this merge is NOT recoverable", and
            # ``merge_recoverability`` silently omits the merge from its degraded lane. A read
            # that failed is not evidence of absence, and here the wrong answer is the one that
            # discards recovery material. Raising is loud and correct; the caller may catch it
            # and report "unknown", but it can no longer mistake it for "none".
            #
            # Made reachable by the CF-1 logger fix: before that, this handler raised NameError
            # on the unbound logger, so the swallow never actually completed.
            logger.warning("Failed to fetch merge audit", exc_info=True)
            raise MergeAuditUnavailable(
                "merge audit read failed; this is not evidence that no snapshot exists"
            ) from exc

        # snapshot_json holds the absorbed node's content, and this read is keyed on uuids with
        # no graph-existence check -- the row outlives the node by design, which is what made it
        # readable after deletion. An erasure of EITHER participant suppresses it, matching the
        # two-party rule the purge uses; matching one side would serve the other's copy.
        from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

        candidates = [r.get("survivor_uuid") for r in out]
        candidates += [r.get("absorbed_uuid") for r in out]
        suppressed = suppressed_node_uuids(self.db_path, candidates)
        if not suppressed:
            return out
        return [
            r
            for r in out
            if r.get("survivor_uuid") not in suppressed
            and r.get("absorbed_uuid") not in suppressed
        ]

    def record_memory_revision(
        self,
        *,
        node_uuid: str,
        field: str,
        old_value: str | None,
        new_value: str | None,
        changed_by: str,
        episode_uuid: str | None = None,
    ) -> None:
        """Persist a per-memory field revision for audit/debugging."""

        if field not in self._VALID_FIELDS:
            raise ValueError(
                f"Invalid revision field {field!r}; expected one of {self._VALID_FIELDS}"
            )
        if changed_by not in self._VALID_CHANGED_BY:
            raise ValueError(
                f"Invalid changed_by {changed_by!r}; expected one of {self._VALID_CHANGED_BY}"
            )
        max_val_len = _MAX_REVISION_VALUE_LEN
        if old_value and len(old_value) > max_val_len:
            old_value = old_value[:max_val_len] + "...[truncated]"
        if new_value and len(new_value) > max_val_len:
            new_value = new_value[:max_val_len] + "...[truncated]"
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_revisions (
                        recorded_at, node_uuid, field, old_value, new_value,
                        changed_by, episode_uuid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        node_uuid,
                        field,
                        old_value,
                        new_value,
                        changed_by,
                        episode_uuid,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record memory revision node=%s field=%s",
                node_uuid,
                field,
                exc_info=True,
            )

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
