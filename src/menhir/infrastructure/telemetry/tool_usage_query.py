"""Read-only per-operation usage query over the telemetry sidecar (no migrations, no pragmas)."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from menhir.infrastructure.telemetry.lifecycle_store_stats import TelemetryRetentionStatsMixin


def fetch_tool_usage_readonly(
    db_path: Path,
    *,
    since_hours: int,
    by_client: bool = False,
) -> list[dict[str, Any]]:
    """Per-operation (optionally per-client) call counts split by outcome stage. No LIMIT.

    Opens the database ``mode=ro`` and never initialises or migrates it. ``failed`` is success=0
    whose stage is not denied/timeout; a NULL stage counts as failed when success=0. A NULL
    ``client_name`` is reported as ``""``. Raises ``sqlite3.OperationalError`` if the database
    predates the client/stage columns.
    """

    from menhir.mcp.telemetry.tracker import STAGE_DENIED, STAGE_TIMEOUT

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                operation,
                COALESCE(client_name, '') AS client_name,
                success,
                COALESCE(stage, '') AS stage,
                duration_ms
            FROM mcp_events
            WHERE substr(replace(started_at, 'T', ' '), 1, 19) >= datetime('now', '-' || ? || ' hours')
            """,
            (since_hours,),
        ).fetchall()

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["operation"], row["client_name"] if by_client else "")
        g = groups.setdefault(
            key, {"calls": 0, "ok": 0, "failed": 0, "denied": 0, "timeout": 0, "lat": []}
        )
        g["calls"] += 1
        if row["success"] == 1:
            g["ok"] += 1
        elif row["stage"] == STAGE_DENIED:
            g["denied"] += 1
        elif row["stage"] == STAGE_TIMEOUT:
            g["timeout"] += 1
        else:
            g["failed"] += 1
        if row["duration_ms"] is not None:
            g["lat"].append(int(row["duration_ms"]))

    results: list[dict[str, Any]] = []
    for (op, client), g in groups.items():
        item: dict[str, Any] = {"operation": op}
        if by_client:
            item["client_name"] = client
        item.update(
            calls=g["calls"],
            ok=g["ok"],
            failed=g["failed"],
            denied=g["denied"],
            timeout=g["timeout"],
            p50_ms=TelemetryRetentionStatsMixin._percentile(sorted(g["lat"]), 0.5),
        )
        results.append(item)
    return results
