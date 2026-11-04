"""`menhir tool-usage`: per-tool MCP call counts from the local telemetry sidecar (read-only)."""

from __future__ import annotations

import typer

_COUNT_KEYS = ("calls", "ok", "failed", "denied", "timeout")


def _row(tool: str, stats: dict | None, client: str | None = None) -> dict:
    row: dict = {"tool": tool}
    if client is not None:
        row["client_name"] = client
    for key in _COUNT_KEYS:
        row[key] = int(stats[key]) if stats else 0
    row["p50_ms"] = stats["p50_ms"] if stats else None
    return row


def _format_table(rows: list[dict], *, name_header: str = "tool", with_client: bool = False) -> list[str]:
    headers = [name_header] + (["client"] if with_client else []) + [*_COUNT_KEYS, "p50_ms"]
    body: list[list[str]] = []
    for r in rows:
        cells = [str(r["tool"])]
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
    since_hours: int = typer.Option(720, "--since-hours", help="Window in hours (>= 1)"),
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

    if since_hours < 1:
        typer.echo("--since-hours must be >= 1", err=True)
        raise typer.Exit(2)
    if by not in ("tool", "client"):
        typer.echo("--by must be 'tool' or 'client'", err=True)
        raise typer.Exit(2)

    from menhir.api.mcp_remote import production_tool_catalog
    from menhir.infrastructure.paths import telemetry_db_path
    import sqlite3

    from menhir.infrastructure.telemetry.tool_usage_query import fetch_tool_usage_readonly

    db_path = telemetry_db_path()
    if not db_path.exists():
        typer.echo(f"No telemetry database at {db_path}")
        raise typer.Exit(1)

    by_client = by == "client"
    try:
        usage = fetch_tool_usage_readonly(db_path, since_hours=since_hours, by_client=by_client)
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc):
            typer.echo(
                f"Telemetry database at {db_path} predates the client/stage columns; "
                "start the server once to migrate it."
            )
            raise typer.Exit(1)
        raise
    registered = sorted(production_tool_catalog())
    registered_set = set(registered)

    tool_stats = [u for u in usage if u["operation"] in registered_set]
    other = [u for u in usage if u["operation"] not in registered_set]

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

    other_rows = [_row(u["operation"], u, u.get("client_name")) for u in other]
    other_rows.sort(
        key=lambda r: (r["tool"], -r["calls"], r.get("client_name") or "")
        if by_client
        else (-r["calls"], r["tool"])
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
    else:
        typer.echo(
            f"{len(unused)} of {len(registered)} registered tools had no calls "
            f"in the last {since_hours} hours."
        )
    if all_operations:
        typer.echo("")
        typer.echo("Other recorded operations")
        if other_rows:
            for line in _format_table(other_rows, name_header="operation", with_client=by_client):
                typer.echo(line)
        else:
            typer.echo("(none)")
