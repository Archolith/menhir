"""Shared serialization and time helpers for telemetry persistence."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.paths import telemetry_db_path

def default_telemetry_db_path() -> Path:
    """Return the default SQLite path for MCP telemetry."""
    return telemetry_db_path()


_SQLITE_BUSY_TIMEOUT_S = float(os.getenv("MENHIR_TELEMETRY_BUSY_TIMEOUT_S", "5"))


def connect_telemetry_db(db_path: Path) -> sqlite3.Connection:
    """Single connect seam for every store sharing the telemetry DB file.

    Applies ``MENHIR_TELEMETRY_BUSY_TIMEOUT_S`` to all of them.
    """
    conn = sqlite3.connect(db_path, timeout=_SQLITE_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(_SQLITE_BUSY_TIMEOUT_S * 1000)}")
    except sqlite3.Error:  # pragma: no cover - a pragma failure must never break a connect
        pass
    return conn


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _span_days(first_seen: str | None, last_seen: str | None) -> float:
    """Return the day span between two ISO timestamps (minimum 1.0 day).

    Used to normalise call counts into a calls-per-day rate. A single-sample or
    unparseable range collapses to 1.0 so the rate equals the raw count.
    """
    if not first_seen or not last_seen:
        return 1.0
    try:
        start = datetime.fromisoformat(first_seen)
        end = datetime.fromisoformat(last_seen)
    except ValueError:
        return 1.0
    return max((end - start).total_seconds() / 86400.0, 1.0)


def _size_of(value: Any) -> int:
    return len(json.dumps(value, default=_json_default))


def _preview_of(value: Any, limit: int = 500) -> str:
    rendered = json.dumps(value, default=_json_default, sort_keys=True)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."
