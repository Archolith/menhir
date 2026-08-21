"""Shared serialization and time helpers for telemetry persistence."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.paths import telemetry_db_path
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)


def default_telemetry_db_path() -> Path:
    """Return the default SQLite path for MCP telemetry."""
    return telemetry_db_path()


_SQLITE_BUSY_TIMEOUT_S = float(os.getenv("MENHIR_TELEMETRY_BUSY_TIMEOUT_S", "5"))


def connect_telemetry_db(db_path: Path) -> sqlite3.Connection:
    """Single connect seam for every store sharing the telemetry DB file.

    Applies ``MENHIR_TELEMETRY_BUSY_TIMEOUT_S`` and the WAL journal mode to all of them.
    WAL is set here rather than in any single store's init because at least five stores
    share this file and whichever writes first is the one that creates it; applying the
    pragma on every connect means the file is created in WAL mode no matter which writer
    gets there first. ``PRAGMA journal_mode=WAL`` is a cheap no-op when already WAL.
    """
    conn = sqlite3.connect(db_path, timeout=_SQLITE_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(_SQLITE_BUSY_TIMEOUT_S * 1000)}")
    except sqlite3.Error:  # pragma: no cover - a pragma failure must never break a connect
        pass
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:  # pragma: no cover - a pragma failure must never break a connect
        logger.debug("Could not enable WAL on telemetry DB", exc_info=True)
    return conn




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
    """Render a payload for `mcp_events.payload_preview`, with free text masked.

    This is the single write boundary for that column -- both `record_mcp_event` and the
    MCP tracker reach the store through here -- so redacting at this point covers every
    sink. It used to `json.dumps` the caller's kwargs verbatim, which meant the first 500
    characters of every memory submitted through `add_memory(text=...)` were persisted in
    plaintext to the sidecar.

    `_size_of` deliberately still measures the UNREDACTED payload: a size is structural
    and is the reason this preview is useful for debugging in the first place.

    Redaction happens HERE rather than in a separate opt-in helper. That is the difference
    between "no current caller leaks" and "no caller can leak": a future writer reaching for
    the obvious name gets the safe behaviour instead of the raw one.
    """
    rendered = json.dumps(
        _redact_telemetry_value(value), default=_json_default, sort_keys=True
    )
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


#: Keys whose STRING values may be retained in a telemetry preview. Two provenances, merged:
#: the telemetry-internal keys this module records (phase, model, endpoint, surface, ...) and the
#: MCP tool ARGUMENT names, enumerated by AST over all 54 `endpoint()` signatures. Both populations
#: reach `mcp_events.payload_preview`, so one list governs both.
#:
#: Membership alone is NOT sufficient -- see `_STRUCTURAL_VALUE_RE`. Widening this list is
#: therefore comparatively safe: a key added here still only retains identifier-shaped values.
_SAFE_TELEMETRY_STRING_KEYS = frozenset(
    {
        "action",
        "artifact_type",
        "artifact_uuid",
        "bootstrap_scope",
        "candidate_type",
        "classification",
        "client_id",
        "client_name",
        "cluster_id",
        "cursor",
        "document_type",
        "endpoint",
        "episode_uuid",
        "evidence_strength",
        "expected_old_integrity",
        "failure_stage",
        "from_commit",
        "from_status",
        "group_id",
        "keep_uuid",
        "kind",
        "memory_uuid",
        "model",
        "namespace",
        "new_uuid",
        "node_uuid",
        "observed_integrity",
        "old_uuid",
        "operation",
        "phase",
        "preset",
        "priority",
        "project",
        "query_type",
        "reader_id",
        "recall_id",
        "relation",
        "remove_uuid",
        "repository",
        "scope",
        "session_id",
        "source",
        "source_uuid",
        "state",
        "status",
        "structure_project",
        "surface",
        "target_uuid",
        "tier",
        "to_status",
        "trigger",
        "turn_evidence_uuid",
        "type",
        "user_id",
        "uuid",
        "valid_at",
        "workspace",
    }
)

# Structural values are observed BEFORE the protected MCP runner has necessarily validated them.
# Retaining a string merely because its key is named ``namespace`` would let a denied caller smuggle
# arbitrary prose into telemetry. Keep only compact identifier/date/URL-ish tokens; everything else
# is treated as content and redacted.
_STRUCTURAL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,200}$")


def _redact_telemetry_value(value: Any, *, key: str | None = None) -> Any:
    """Return a payload shape safe to persist in telemetry previews.

    Telemetry needs operation shape and sizing, not a second durable copy of arbitrary
    user/memory prose. String values are therefore retained only for a narrow structural
    allowlist AND only when they have a compact identifier-like shape; every other non-empty
    string is replaced with a marker. Containers are traversed so nested request bodies cannot
    bypass the rule.
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
        if key in _SAFE_TELEMETRY_STRING_KEYS and _STRUCTURAL_VALUE_RE.fullmatch(value):
            return value
        return "[redacted]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _safe_preview_of(value: Any, limit: int = 500) -> str:
    """Serialize a telemetry preview after recursively removing free-text content.

    Retained as the explicit name for call sites that want the guarantee stated at the call.
    It is now an alias: `_preview_of` redacts unconditionally, so applying the redactor twice
    would be redundant rather than safer.
    """
    return _preview_of(value, limit=limit)
