"""`menhir tool-usage`: per-tool MCP call counts from the local telemetry sidecar (read-only)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

_COUNT_KEYS = ("calls", "ok", "failed", "denied", "timeout")

#: Upper bound for ``--since-hours``: 10 years. SQLite's ``datetime('now', '-N hours')`` returns NULL
#: once the result falls before year 0 (about 1.7e7 hours back from today), and ``ts >= NULL`` matches
#: no row, which would print "no calls" for every tool. 87600 is far below that point and is longer
#: than any retention window the sidecar keeps.
MAX_SINCE_HOURS = 24 * 365 * 10


def _sqlite_failure_message(db_path: Path, exc: sqlite3.Error) -> str:
    """One line naming the database and the kind of problem, for any sqlite3 failure."""

    text = str(exc)
    low = text.lower()
    if "no such column" in low:
        return (
            f"Telemetry database at {db_path} predates the client/stage columns; "
            "start the server once to migrate it."
        )
    if "no such table" in low:
        return (
            f"Telemetry database at {db_path} has no mcp_events table (empty or never initialised); "
            "start the server once to create it."
        )
    if "not a database" in low or "malformed" in low:
        return f"Telemetry database at {db_path} is not a valid SQLite database ({text})."
    if "locked" in low or "busy" in low:
        return f"Telemetry database at {db_path} is locked by another process ({text}); try again shortly."
    if "unable to open" in low or "readonly" in low or "read-only" in low:
        return (
            f"Telemetry database at {db_path} could not be opened read-only ({text}); "
            "check file permissions and that its directory is readable."
        )
    return f"Telemetry database at {db_path} could not be read: {type(exc).__name__}: {text}"


def _history_note(oldest: str | None, since_hours: int) -> str | None:
    """Plain-language caveat when the recorded history does not cover the requested window.

    ``oldest`` is the earliest ``started_at`` as UTC ``YYYY-MM-DD HH:MM:SS`` (None: no events at
    all). Retention prunes ``mcp_events``, so a window longer than the surviving history makes
    "had no calls" mean "no calls recorded", not "no calls happened".
    """

    if oldest is None:
        return "The telemetry database holds no events, so nothing is established about tool usage."
    try:
        first = datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None  # unparseable timestamp: say nothing rather than guess
    if datetime.now(timezone.utc) - timedelta(hours=since_hours) < first:
        return (
            f"Telemetry history only starts at {oldest} UTC, so the last {since_hours} hours are not "
            "fully covered and unused tools may be overstated "
            "(retention is set by MENHIR_TELEMETRY_OBSERVABILITY_RETENTION_DAYS)."
        )
    return None


def _row(
    tool: str, stats: dict | None, client: str | None = None, kind: str | None = None
) -> dict:
    row: dict = {"tool": tool}
    if kind is not None:
        row["kind"] = kind
    if client is not None:
        row["client_name"] = client
    for key in _COUNT_KEYS:
        row[key] = int(stats[key]) if stats else 0
    row["p50_ms"] = stats["p50_ms"] if stats else None
    return row


def _format_table(
    rows: list[dict],
    *,
    name_header: str = "tool",
    with_client: bool = False,
    with_kind: bool = False,
) -> list[str]:
    headers = (
        [name_header]
        + (["kind"] if with_kind else [])
        + (["client"] if with_client else [])
        + [*_COUNT_KEYS, "p50_ms"]
    )
    body: list[list[str]] = []
    for r in rows:
        cells = [str(r["tool"])]
        if with_kind:
            cells.append(str(r.get("kind") or "-"))
        if with_client:
            cells.append(str(r.get("client_name") or "-"))
        cells.extend(str(r[k]) for k in _COUNT_KEYS)
        cells.append("-" if r["p50_ms"] is None else str(r["p50_ms"]))
        body.append(cells)
    widths = [max(len(h), *(len(c[i]) for c in body)) if body else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for cells in body:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip())
    return lines


def tool_usage(
    since_hours: int = typer.Option(720, "--since-hours", help=f"Window in hours (1 to {MAX_SINCE_HOURS}, i.e. 10 years)"),
    by: str = typer.Option("tool", "--by", help="Group by 'tool' or 'client'"),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    all_operations: bool = typer.Option(
        False, "--all-operations", help="Also list recorded operations that are not registered tools"
    ),
) -> None:
    """Report MCP call counts for every registered tool, including tools never called.

    Read-only: reads the local telemetry sidecar (mcp_telemetry.db); no server, Neo4j or LLM needed.
    """
    import json

    if not 1 <= since_hours <= MAX_SINCE_HOURS:
        typer.echo(f"--since-hours must be between 1 and {MAX_SINCE_HOURS} (10 years)", err=True)
        raise typer.Exit(2)
    if by not in ("tool", "client"):
        typer.echo("--by must be 'tool' or 'client'", err=True)
        raise typer.Exit(2)

    from menhir.api.mcp_remote import production_tool_catalog
    from menhir.infrastructure.paths import telemetry_db_path
    from menhir.infrastructure.telemetry.tool_usage_query import (
        fetch_oldest_started_at_readonly,
        fetch_tool_usage_readonly,
    )

    db_path = telemetry_db_path()
    if not db_path.exists():
        typer.echo(f"No telemetry database at {db_path}", err=True)
        raise typer.Exit(1)

    by_client = by == "client"
    try:
        usage = fetch_tool_usage_readonly(db_path, since_hours=since_hours, by_client=by_client)
        oldest = fetch_oldest_started_at_readonly(db_path)
    except sqlite3.Error as exc:
        # Errors go to stderr so `--json` stdout is never polluted by a message.
        typer.echo(_sqlite_failure_message(db_path, exc), err=True)
        raise typer.Exit(1) from None
    history_note = _history_note(oldest, since_hours)
    registered = sorted(production_tool_catalog())
    registered_set = set(registered)

    # Only kind='tool' rows are tool invocations. A background / resource / deprecated-operation row
    # whose operation happens to equal a tool name must not change that tool's counts.
    def _is_tool_call(u: dict) -> bool:
        return u["kind"] == "tool" and u["operation"] in registered_set

    tool_stats = [u for u in usage if _is_tool_call(u)]
    other = [u for u in usage if not _is_tool_call(u)]

    called = {u["operation"] for u in tool_stats if u["calls"] > 0}
    unused = [t for t in registered if t not in called]

    rows: list[dict]
    if by_client:
        rows = [_row(u["operation"], u, u["client_name"]) for u in tool_stats if u["calls"] > 0]
        rows.sort(key=lambda r: (r["tool"], -r["calls"], r["client_name"]))
    else:
        by_name = {u["operation"]: u for u in tool_stats}
        rows = [_row(t, by_name.get(t)) for t in registered]
        rows.sort(key=lambda r: (-r["calls"], r["tool"]))

    other_rows = [_row(u["operation"], u, u.get("client_name"), kind=u["kind"]) for u in other]
    other_rows.sort(
        key=lambda r: (r["tool"], r["kind"], -r["calls"], r.get("client_name") or "")
        if by_client
        else (-r["calls"], r["tool"], r["kind"])
    )

    if json_mode:
        payload: dict = {
            "since_hours": since_hours,
            "by": by,
            "rows": rows,
            "unused_tools": unused,
            "registered_tool_count": len(registered),
        }
        if all_operations:
            payload["other_operations"] = other_rows
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    for line in _format_table(rows, with_client=by_client):
        typer.echo(line)
    typer.echo("")
    if by_client:
        typer.echo("Unused tools: " + (", ".join(unused) if unused else "(none)"))
        if history_note:
            typer.echo(f"Note: {history_note}")
    else:
        summary = (
            f"{len(unused)} of {len(registered)} registered tools had no calls "
            f"in the last {since_hours} hours."
        )
        typer.echo(f"{summary} {history_note}" if history_note else summary)
    if all_operations:
        typer.echo("")
        typer.echo("Other recorded operations")
        if other_rows:
            for line in _format_table(
                other_rows, name_header="operation", with_client=by_client, with_kind=True
            ):
                typer.echo(line)
        else:
            typer.echo("(none)")
