"""Durable, acknowledgeable operator warnings (provider auth/billing, failed episodes).

An acknowledgement belongs to exactly one incident: a row that is resolved and later fails again
is reset, ack fields included, so an old ack can never silence a new incident.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from menhir.infrastructure.telemetry.helpers import _utc_now_iso

logger = logging.getLogger(__name__)

KIND_PROVIDER_AUTH = "provider_auth"
KIND_PROVIDER_BILLING = "provider_billing"
KIND_FAILED_EPISODES = "failed_episodes"
WARNING_KINDS = (KIND_PROVIDER_AUTH, KIND_PROVIDER_BILLING, KIND_FAILED_EPISODES)
SUBJECT_GLOBAL = "global"

_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string (nanosecond fractions allowed) to an aware UTC datetime."""

    if not value:
        return None
    text = _FRACTION.sub(r"\1", str(value).strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def initialize_operator_warnings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_warnings (
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            state TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 1,
            acked_at TEXT,
            acked_by TEXT,
            ack_watermark TEXT,
            resolved_at TEXT,
            PRIMARY KEY (kind, subject)
        )
        """
    )


class TelemetryOperatorWarningsMixin:
    """Persistence methods composed into ``McpTelemetryStore``."""

    def record_operator_warning_occurrence(self, kind: str, subject: str) -> None:
        """Start a new incident (absent/resolved row) or bump the current one, keeping its ack."""

        now = _utc_now_iso()
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_warnings
                    (kind, subject, state, first_seen_at, last_seen_at, occurrences)
                VALUES (?, ?, 'active', ?, ?, 1)
                ON CONFLICT(kind, subject) DO UPDATE SET
                    state = 'active',
                    first_seen_at = CASE WHEN state = 'active'
                        THEN first_seen_at ELSE excluded.first_seen_at END,
                    occurrences = CASE WHEN state = 'active' THEN occurrences + 1 ELSE 1 END,
                    acked_at = CASE WHEN state = 'active' THEN acked_at ELSE NULL END,
                    acked_by = CASE WHEN state = 'active' THEN acked_by ELSE NULL END,
                    ack_watermark = CASE WHEN state = 'active' THEN ack_watermark ELSE NULL END,
                    resolved_at = NULL,
                    last_seen_at = excluded.last_seen_at
                """,
                (kind, subject, now, now),
            )
            conn.commit()

    def resolve_operator_warning(self, kind: str, subject: str | None = None) -> int:
        """Resolve active rows of ``kind`` (one ``subject``, or all when None); returns rows changed."""

        self._ensure_ready()
        sql = "UPDATE operator_warnings SET state = 'resolved', resolved_at = ? WHERE kind = ? AND state = 'active'"
        params: list[Any] = [_utc_now_iso(), kind]
        if subject is not None:
            sql += " AND subject = ?"
            params.append(subject)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount

    def ack_operator_warning(
        self,
        kind: str,
        subject: str,
        actor: str,
        watermark: str | None = None,
        first_seen_at: str | None = None,
    ) -> bool:
        """Acknowledge an ACTIVE row. A resolved or absent row is left alone (returns False).

        ``first_seen_at`` pins the ack to the incident the caller saw, so an incident that
        resolved and restarted in between is not acknowledged by mistake.
        """

        self._ensure_ready()
        sql = (
            "UPDATE operator_warnings SET acked_at = ?, acked_by = ?, ack_watermark = ? "
            "WHERE kind = ? AND subject = ? AND state = 'active'"
        )
        params: list[Any] = [_utc_now_iso(), actor, watermark, kind, subject]
        if first_seen_at is not None:
            sql += " AND first_seen_at = ?"
            params.append(first_seen_at)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount > 0

    def list_operator_warnings(
        self, *, kind: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        where: list[str] = []
        params: list[Any] = []
        if active_only:
            where.append("state = 'active'")
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM operator_warnings {clause} ORDER BY kind, subject", params
            ).fetchall()
        return [dict(row) for row in rows]


class OperatorWarningSink:
    """Serve-side sink: persists provider failures and resolves them on success.

    Success calls are hot, so ``resolve`` returns without touching SQLite unless an
    active key of that kind is known in-process. The known set is reloaded from the store
    at most every ``RELOAD_S`` seconds, so rows another process recorded still get resolved.
    """

    RELOAD_S = 60.0

    def __init__(self, store: Any | None = None) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._active: set[tuple[str, str]] | None = None
        self._loaded_at = 0.0

    def _get_store(self) -> Any:
        if self._store is None:
            from menhir.infrastructure.telemetry.store import telemetry_store

            self._store = telemetry_store
        return self._store

    def record(self, kind: str, subject: str) -> None:
        try:
            self._get_store().record_operator_warning_occurrence(kind, subject)
            with self._lock:
                if self._active is not None:
                    self._active.add((kind, subject))
        except Exception:  # noqa: BLE001 - must never break the LLM call path
            logger.warning("could not persist operator warning %s/%s", kind, subject, exc_info=True)

    def resolve(self, kind: str, subject: str | None = None) -> None:
        try:
            with self._lock:
                if self._active is None or monotonic() - self._loaded_at >= self.RELOAD_S:
                    rows = self._get_store().list_operator_warnings(active_only=True)
                    self._active = {(r["kind"], r["subject"]) for r in rows}
                    self._loaded_at = monotonic()
                hits = {
                    key
                    for key in self._active
                    if key[0] == kind and (subject is None or key[1] == subject)
                }
                if not hits:
                    return
            self._get_store().resolve_operator_warning(kind, subject)
            with self._lock:
                if self._active is not None:
                    self._active -= hits
        except Exception:  # noqa: BLE001 - must never break the LLM call path
            logger.warning("could not resolve operator warning %s/%s", kind, subject, exc_info=True)


def build_operator_warnings(
    rows: list[dict[str, Any]],
    *,
    auth_failure: Any = None,
    quota_exhaustion: Any = None,
    failed_count: int = 0,
    latest_failed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Merge durable active rows with the in-memory singletons and the live FAILED count.

    A singleton with no row (no sink registered) counts as unacked. ``failed_episodes`` is
    acked only while nothing newer than the ack watermark has entered FAILED.
    """

    by_key = {(r["kind"], r["subject"]): r for r in rows if r.get("state") == "active"}
    out: list[dict[str, Any]] = []

    def _entry(kind: str, subject: str, row: dict[str, Any] | None, summary: str, **extra: Any) -> dict[str, Any]:
        row = row or {}
        entry = {
            "kind": kind,
            "subject": subject,
            "durable": bool(row),
            "acked": bool(row.get("acked_at")),
            "acked_by": row.get("acked_by"),
            "acked_at": row.get("acked_at"),
            "first_seen_at": row.get("first_seen_at"),
            "last_seen_at": row.get("last_seen_at"),
            "occurrences": row.get("occurrences"),
            "summary": summary,
        }
        entry.update(extra)
        return entry

    singleton_by_kind = {KIND_PROVIDER_AUTH: auth_failure, KIND_PROVIDER_BILLING: quota_exhaustion}
    for kind in (KIND_PROVIDER_AUTH, KIND_PROVIDER_BILLING):
        singleton = singleton_by_kind[kind]
        subjects = {s for (k, s) in by_key if k == kind}
        if singleton is not None:
            subjects.add(singleton.kind)
        for subject in sorted(subjects):
            row = by_key.get((kind, subject))
            if singleton is not None and singleton.kind == subject:
                summary = singleton.summary()
            elif kind == KIND_PROVIDER_AUTH:
                summary = (
                    f"the LLM provider rejected our credentials (last seen "
                    f"{(row or {}).get('last_seen_at')}) on {subject} calls"
                )
            else:
                summary = (
                    f"provider_quota_exhausted: the LLM provider refused for billing "
                    f"(last seen {(row or {}).get('last_seen_at')}) on {subject} calls"
                )
            out.append(_entry(kind, subject, row, summary))

    if failed_count > 0:
        row = by_key.get((KIND_FAILED_EPISODES, SUBJECT_GLOBAL))
        acked = bool(row and row.get("acked_at"))
        if acked:
            watermark = parse_iso_utc(row.get("ack_watermark"))
            newest = parse_iso_utc(latest_failed_at)
            if watermark is None or (newest is not None and newest > watermark):
                acked = False
        entry = _entry(
            KIND_FAILED_EPISODES,
            SUBJECT_GLOBAL,
            row,
            f"{failed_count} FAILED episode(s) are stored but not recallable",
            count=int(failed_count),
            latest_failed_at=latest_failed_at,
        )
        entry["acked"] = acked
        out.append(entry)
    return out
