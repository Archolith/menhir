"""Failure and belief-revision folds over the telemetry tables."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable


class TelemetryRecallFoldsMixin:
    def fetch_failure_summary(self, *, since_hours: int = 24) -> dict[str, Any]:
        """Counts by classification, top operations by failure count."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            by_classification = conn.execute(
                """
                SELECT coalesce(classification, 'unknown') AS cls, COUNT(*) AS cnt
                FROM failure_events
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
                GROUP BY cls
                ORDER BY cnt DESC
                """,
                (since_hours,),
            ).fetchall()
            by_operation = conn.execute(
                """
                SELECT operation, COUNT(*) AS cnt
                FROM failure_events
                WHERE substr(replace(recorded_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
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
