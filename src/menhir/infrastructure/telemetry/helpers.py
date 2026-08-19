"""Shared serialization and time helpers for telemetry persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.paths import telemetry_db_path


def default_telemetry_db_path() -> Path:
    """Return the default SQLite path for MCP telemetry."""
    return telemetry_db_path()


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


_SAFE_TELEMETRY_STRING_KEYS = frozenset(
    {
        "action",
        "bootstrap_scope",
        "classification",
        "client_id",
        "endpoint",
        "failure_stage",
        "kind",
        "memory_uuid",
        "model",
        "namespace",
        "node_uuid",
        "operation",
        "phase",
        "preset",
        "reader_id",
        "scope",
        "session_id",
        "source",
        "status",
        "surface",
        "tier",
        "trigger",
        "type",
        "user_id",
        "uuid",
        "valid_at",
        "workspace",
    }
)


def _redact_telemetry_value(value: Any, *, key: str | None = None) -> Any:
    """Return a payload shape safe to persist in telemetry previews.

    Telemetry needs operation shape and sizing, not a second durable copy of arbitrary
    user/memory prose. String values are therefore retained only for a narrow structural
    allowlist; every other non-empty string is replaced with a marker. Containers are
    traversed so nested request bodies cannot bypass the rule.
    """
    if isinstance(value, dict):
        return {
            str(child_key): _redact_telemetry_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_telemetry_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_telemetry_value(item, key=key) for item in value)
    if isinstance(value, str):
        if not value:
            return value
        if key in _SAFE_TELEMETRY_STRING_KEYS:
            return value
        return "[redacted]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _safe_preview_of(value: Any, limit: int = 500) -> str:
    """Serialize a telemetry preview after recursively removing free-text content."""
    return _preview_of(_redact_telemetry_value(value), limit=limit)
