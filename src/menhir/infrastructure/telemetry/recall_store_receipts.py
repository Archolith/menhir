"""Recall receipt recording, rating, and per-operation usefulness aggregation."""

from __future__ import annotations

import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _utc_now_iso


class TelemetryRecallReceiptsMixin:
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
                # The token path is scoped the same way the no-token path below already is.
                # It was `WHERE token = ?` alone, so any agent-tier caller holding or guessing
                # a receipt token could overwrite the rating on another client's recall -- and
                # read back that recall's `operation` in the response. The scoping mechanism
                # existed on the sibling branch of this same method and simply was not applied
                # here.
                #
                # Scoped by session OR client, not session alone: rating a recall from a later
                # session of the same client is legitimate and was always possible. When the
                # caller presents neither identity there is nothing to enforce, and the lookup
                # stays as it was rather than failing closed on the local/CLI path.
                if session_id or client_id:
                    target = conn.execute(
                        """
                        SELECT id, token, operation FROM recall_receipts
                        WHERE token = ?
                          AND (
                                (? <> '' AND session_id = ?)
                             OR (? <> '' AND client_id = ?)
                              )
                        """,
                        (token, session_id, session_id, client_id, client_id),
                    ).fetchone()
                else:
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
            window_clause = "WHERE substr(replace(created_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')"
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
