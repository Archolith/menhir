"""Recall feedback, aggregate diagnostics, and client-session telemetry operations."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from menhir.infrastructure.telemetry.helpers import _json_default, _span_days, _utc_now_iso

logger = logging.getLogger(__name__)

class TelemetryRecallStoreMixin:
    def record_recall_receipt(
        self,
        *,
        token: str,
        operation: str,
        client_id: str = "",
        session_id: str = "",
        created_at: str | None = None,
    ) -> None:
        """Register a ratable recall so a later rate_recall can attach a score.

        Best-effort: a duplicate token (retry) is ignored rather than raised so a
        receipt write can never break the recall it describes.
        """
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO recall_receipts
                    (token, operation, client_id, session_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token, operation, client_id or "", session_id or "", created_at or _utc_now_iso()),
            )
            conn.commit()

    def record_recall_feedback(
        self,
        *,
        score_label: str,
        score_value: float | None,
        token: str | None = None,
        session_id: str = "",
        client_id: str = "",
        reason: str | None = None,
        rated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Attach a usefulness score to a recall receipt.

        When ``token`` is given the matching receipt is rated; otherwise the most
        recent *unrated* receipt for the session (falling back to the client) is
        used. Returns the rated receipt's ``token``/``operation`` or ``None`` when
        no matching receipt exists.
        """
        self._ensure_ready()
        stamp = rated_at or _utc_now_iso()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if token:
                target = conn.execute(
                    "SELECT id, token, operation FROM recall_receipts WHERE token = ?",
                    (token,),
                ).fetchone()
            else:
                target = conn.execute(
                    """
                    SELECT id, token, operation FROM recall_receipts
                    WHERE rated_at IS NULL
                      AND (
                            (? <> '' AND session_id = ?)
                         OR (? = '' AND ? <> '' AND client_id = ?)
                          )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (session_id, session_id, session_id, client_id, client_id),
                ).fetchone()
            if target is None:
                return None
            conn.execute(
                """
                UPDATE recall_receipts
                SET score_label = ?, score_value = ?, reason = ?, rated_at = ?
                WHERE id = ?
                """,
                (score_label, score_value, reason, stamp, target["id"]),
            )
            conn.commit()
            return {"token": target["token"], "operation": target["operation"]}

    def fetch_usefulness_stats(
        self, *, since_hours: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """Per-operation usefulness: receipts, rated count, and average score.

        ``avg_score`` averages ``score_value`` over *scored* receipts only, so an
        ``unused`` rating (null value) counts toward coverage but not the mean.
        """
        self._ensure_ready()
        params: list[Any] = []
        window_clause = ""
        if since_hours is not None:
            window_clause = "WHERE created_at >= datetime('now', '-' || ? || ' hours')"
            params.append(since_hours)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT
                    operation,
                    COUNT(*) AS receipts,
                    SUM(CASE WHEN rated_at IS NOT NULL THEN 1 ELSE 0 END) AS rated,
                    SUM(CASE WHEN score_value IS NOT NULL THEN 1 ELSE 0 END) AS scored,
                    AVG(score_value) AS avg_score
                FROM recall_receipts
                {window_clause}
                GROUP BY operation
                """,
                params,
            ).fetchall()
        stats: dict[str, dict[str, Any]] = {}
        for row in rows:
            receipts = int(row["receipts"])
            rated = int(row["rated"] or 0)
            stats[row["operation"]] = {
                "receipts": receipts,
                "rated": rated,
                "scored": int(row["scored"] or 0),
                "rated_pct": round(rated / receipts * 100, 1) if receipts else 0.0,
                "avg_score": round(row["avg_score"], 2) if row["avg_score"] is not None else None,
            }
        return stats

    def fetch_failure_summary(self, *, since_hours: int = 24) -> dict[str, Any]:
        """Counts by classification, top operations by failure count."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            by_classification = conn.execute(
                """
                SELECT coalesce(classification, 'unknown') AS cls, COUNT(*) AS cnt
                FROM failure_events
                WHERE recorded_at >= datetime('now', '-' || ? || ' hours')
                GROUP BY cls
                ORDER BY cnt DESC
                """,
                (since_hours,),
            ).fetchall()
            by_operation = conn.execute(
                """
                SELECT operation, COUNT(*) AS cnt
                FROM failure_events
                WHERE recorded_at >= datetime('now', '-' || ? || ' hours')
                GROUP BY operation
                ORDER BY cnt DESC
                LIMIT 5
                """,
                (since_hours,),
            ).fetchall()
        return {
            "by_classification": {
                row["cls"]: int(row["cnt"]) for row in by_classification
            },
            "by_operation": {row["operation"]: int(row["cnt"]) for row in by_operation},
            "total": sum(int(row["cnt"]) for row in by_classification),
        }

    def fold_failures_by_operation(self, *, min_count: int = 1) -> list[dict[str, Any]]:
        """Lifetime failure counts per (operation, error_type) with a sample of contributing
        episode_uuids — the FailureEvent->QuantState fold. Unlike fetch_failure_summary this is
        time-unbounded (a durable counter), keyed for a QuantState node, and carries provenance."""
        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT operation,
                       coalesce(error_type, 'unknown') AS error_type,
                       COUNT(*) AS cnt,
                       group_concat(DISTINCT episode_uuid) AS episodes
                FROM failure_events
                GROUP BY operation, error_type
                HAVING cnt >= ?
                ORDER BY cnt DESC
                """,
                (int(min_count),),
            ).fetchall()
        out = []
        for r in rows:
            eps = [e for e in (r["episodes"] or "").split(",") if e]
            out.append({"operation": r["operation"], "error_type": r["error_type"],
                        "count": int(r["cnt"]), "episode_uuids": eps})
        return out

    def fold_revisions_by_field(
        self, *, min_count: int = 2, exclude_fields: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        """Belief-instability fold: how many times each (node_uuid, field) was corrected, with the
        latest new_value and contributing episode_uuids. min_count defaults to 2 because a single
        write is not instability — a field is only 'unstable' once it has been revised. The count
        is the instability signal; the agent lowers confidence in a field revised many times.

        `exclude_fields` drops mechanical (non-belief) fields BEFORE the GROUP BY — e.g. `scope`,
        which records namespace/lifecycle promotions (SESSION->PERSISTENT), not belief corrections.
        Filtering in SQL keeps the fold from materializing tens of thousands of churn rows."""
        self._ensure_ready()
        excl = [f.strip().lower() for f in exclude_fields if f and f.strip()]
        where = ""
        params: list[Any] = []
        if excl:
            where = f"WHERE lower(field) NOT IN ({','.join('?' for _ in excl)})"
            params.extend(excl)
        params.append(int(min_count))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT node_uuid, field, COUNT(*) AS cnt,
                       group_concat(DISTINCT episode_uuid) AS episodes,
                       (SELECT new_value FROM memory_revisions r2
                        WHERE r2.node_uuid = r.node_uuid AND r2.field = r.field
                        ORDER BY r2.recorded_at DESC LIMIT 1) AS latest_value
                FROM memory_revisions r
                {where}
                GROUP BY node_uuid, field
                HAVING cnt >= ?
                ORDER BY cnt DESC
                """,
                params,
            ).fetchall()
        out = []
        for r in rows:
            eps = [e for e in (r["episodes"] or "").split(",") if e]
            out.append({"node_uuid": r["node_uuid"], "field": r["field"], "count": int(r["cnt"]),
                        "latest_value": r["latest_value"], "episode_uuids": eps})
        # latest_value is memory text keyed by node_uuid with no graph-existence check, so an
        # in-flight erasure must suppress it here too.
        return self._drop_erased_rows(out)

    # ---------------------------------------------------------------- cutoff-bound folds (Metric)

    def fold_failures_bounded(
        self, *, cutoff_id: int | None = None, after_id: int = 0, min_count: int = 1
    ) -> dict[str, Any]:
        """Cutoff-bound failure fold for Metric receipts (Metric plan C3).

        Unlike ``fold_failures_by_operation`` (unbounded, arrival-racy), this captures
        ``cutoff_id = MAX(id)`` and folds only ``after_id < id <= cutoff_id`` IN THE SAME
        transaction, so rows arriving mid-fold belong to the NEXT receipt and cannot silently
        change the meaning of this one.

        ``after_id`` supports the chained accumulator (C4): pass the previous receipt's cutoff to
        fold only the delta. Returns {cutoff_id, groups:[{operation, error_type, count, row_ids}]}
        where ``count`` is the DELTA count in the window (the coordinator adds the prior aggregate).
        """
        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if cutoff_id is None:
                row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM failure_events").fetchone()
                cutoff_id = int(row["m"])
            rows = conn.execute(
                """
                SELECT operation,
                       coalesce(error_type, 'unknown') AS error_type,
                       COUNT(*) AS cnt,
                       group_concat(id) AS row_ids
                FROM failure_events
                WHERE id > ? AND id <= ?
                GROUP BY operation, error_type
                HAVING cnt >= ?
                ORDER BY operation, error_type
                """,
                (int(after_id), int(cutoff_id), int(min_count)),
            ).fetchall()
        groups = [
            {
                "operation": r["operation"],
                "error_type": r["error_type"],
                "count": int(r["cnt"]),
                # canonical: sorted ints, so the input_digest is stable across runs
                "row_ids": sorted(int(x) for x in (r["row_ids"] or "").split(",") if x),
            }
            for r in rows
        ]
        return {"cutoff_id": int(cutoff_id), "groups": groups}

    def fold_revisions_bounded(
        self,
        *,
        cutoff_id: int | None = None,
        after_id: int = 0,
        min_count: int = 1,
        exclude_fields: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Cutoff-bound belief-instability fold for Metric receipts (Metric plan C3).

        Same cutoff contract as ``fold_failures_bounded``. ``latest_value`` is resolved with the
        deterministic ``(recorded_at, id)`` ordering the plan requires -- ``recorded_at`` alone ties
        on same-timestamp rows and would pick an arbitrary winner.

        NOTE min_count defaults to 1 here (not 2). The unbounded fold's min_count=2 encodes
        "a single write is not instability", but in a DELTA window a group with one new revision may
        still be the 5th lifetime revision. The coordinator applies the instability threshold to the
        ACCUMULATED total, not to the window.
        """
        self._ensure_ready()
        excl = [f.strip().lower() for f in exclude_fields if f and f.strip()]
        where_excl = ""
        params: list[Any] = []
        if excl:
            where_excl = f"AND lower(field) NOT IN ({','.join('?' for _ in excl)})"
            params.extend(excl)

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if cutoff_id is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM memory_revisions"
                ).fetchone()
                cutoff_id = int(row["m"])
            rows = conn.execute(
                f"""
                SELECT node_uuid, field, COUNT(*) AS cnt,
                       group_concat(id) AS row_ids,
                       (SELECT new_value FROM memory_revisions r2
                        WHERE r2.node_uuid = r.node_uuid AND r2.field = r.field
                          AND r2.id <= ?
                        ORDER BY r2.recorded_at DESC, r2.id DESC LIMIT 1) AS latest_value
                FROM memory_revisions r
                WHERE id > ? AND id <= ? {where_excl}
                GROUP BY node_uuid, field
                HAVING cnt >= ?
                ORDER BY node_uuid, field
                """,
                (int(cutoff_id), int(after_id), int(cutoff_id), *params, int(min_count)),
            ).fetchall()
        groups = [
            {
                "node_uuid": r["node_uuid"],
                "field": r["field"],
                "count": int(r["cnt"]),
                "latest_value": r["latest_value"],
                "row_ids": sorted(int(x) for x in (r["row_ids"] or "").split(",") if x),
            }
            for r in rows
        ]
        return {"cutoff_id": int(cutoff_id), "groups": self._drop_erased_rows(groups)}

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
                WHERE started_at >= datetime('now', '-' || ? || ' hours')
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
                WHERE recorded_at >= datetime('now', '-' || ? || ' hours')
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
                      AND resolved_at > datetime('now', ?)
                    """,
                    (pair[0], pair[1], f"-{cooldown_days} days"),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM conflict_resolutions WHERE uuid_a = ? AND uuid_b = ?",
                    (pair[0], pair[1]),
                ).fetchone()
        return row is not None

    def touch_session(
        self, session_id: str, client_id: str = "", client_name: str = ""
    ) -> None:
        """Upsert a session_registry row — sets last_accessed to now.

        Keyed by ``session_id`` so each conversation/window tracks independently.
        """
        if not session_id:
            return
        now = _utc_now_iso()
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_registry
                        (session_id, client_id, client_name, first_accessed, last_accessed)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        client_id     = excluded.client_id,
                        client_name   = excluded.client_name,
                        last_accessed = excluded.last_accessed
                    """,
                    (session_id, client_id, client_name, now, now),
                )
                conn.commit()
        except Exception:
            logger.warning(
                "Failed to touch session registry session_id=%s",
                session_id,
                exc_info=True,
            )

    def get_session_last_accessed(self, session_id: str) -> str | None:
        """Return the ISO timestamp of when this session was last seen, or None."""
        if not session_id:
            return None
        self._ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_accessed FROM session_registry WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    def touch_client(self, client_id: str, client_name: str) -> None:
        """Upsert a client registry row — sets last_accessed to now."""

        if not client_id:
            return
        now = _utc_now_iso()
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO client_registry (client_id, client_name, first_accessed, last_accessed)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(client_id) DO UPDATE SET
                        client_name   = excluded.client_name,
                        last_accessed = excluded.last_accessed
                    """,
                    (client_id, client_name, now, now),
                )
                conn.commit()
        except Exception:
            logger.warning(
                "Failed to touch client registry client_id=%s", client_id, exc_info=True
            )

    def get_client_last_accessed(self, client_id: str) -> str | None:
        """Return the ISO timestamp of when this client was last seen, or None."""

        if not client_id:
            return None
        self._ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_accessed FROM client_registry WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return row[0] if row else None

    def list_clients(self) -> list[dict[str, Any]]:
        """Return all registered clients ordered by last_accessed desc."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT client_id, client_name, first_accessed, last_accessed FROM client_registry ORDER BY last_accessed DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def _drop_erased_rows(self, rows: list) -> list:
        """Remove rows whose node_uuid a live erasure covers, in one bulk lookup.

        These folds return ``latest_value`` -- actual memory text -- keyed by node_uuid with no
        graph-existence check, so they answer for a node that has already been deleted. Between
        a committed erasure intent and a finished purge the row still holds that text.
        """
        from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

        if not rows:
            return rows
        suppressed = suppressed_node_uuids(
            self.db_path, [r.get("node_uuid") for r in rows]
        )
        if not suppressed:
            return rows
        return [r for r in rows if r["node_uuid"] not in suppressed]

    def _erasure_suppresses(self, node_uuid: str) -> bool:
        """Whether a live erasure covers this node. Fails CLOSED.

        A lookup that raises means suppressed: a veto that failed open would serve exactly the
        content it exists to withhold, which is a worse outcome than a missing archive read.
        """
        from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

        return bool(suppressed_node_uuids(self.db_path, [node_uuid]))

    def get_original_content(self, node_uuid: str) -> str | None:
        """Retrieve the original (pre-compression) content for a node from the revision archive.

        Returns the `old_value` of the earliest `memory_revisions` row where
        `node_uuid` matches and `field = 'content'`. This is the content before
        the first lossy rewrite (compression).

        Returns None if no such row exists (archive miss / node was never compressed).
        Intended for rehydration recovery: use this archive content instead of
        LLM-merging the compressed summary alone, preventing photocopy-loss from
        repeated compress/rehydrate cycles.
        """
        if not node_uuid:
            return None
        self._ensure_ready()
        # CF-165 read veto. This query is the finding's own proof that erased content stayed
        # readable: it is keyed on node_uuid alone, with no graph-existence check, so it answers
        # for a node that was deleted. An erasure that has committed its intent but not finished
        # purging must already suppress it -- otherwise the window between the two is exactly
        # when the supposedly erased text is served.
        #
        # Checked against the subject inventory directly rather than through services'
        # ErasureVeto: infrastructure importing services would invert the layering. The predicate
        # is the same one that veto calls.
        if self._erasure_suppresses(node_uuid):
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT old_value FROM memory_revisions
                    WHERE node_uuid = ? AND field = 'content'
                    ORDER BY recorded_at ASC
                    LIMIT 1
                    """,
                    (node_uuid,),
                ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            logger.warning(
                "Failed to fetch original content for node=%s",
                node_uuid,
                exc_info=True,
            )
            return None
