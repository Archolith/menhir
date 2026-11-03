"""Coverage for `menhir tool-usage` (read-only per-tool MCP usage report)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app
from menhir.infrastructure.telemetry.store import McpTelemetryStore

runner = CliRunner()


def _fake_tool(name: str) -> type:
    return type(f"Fake_{name}", (), {"name": name})


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> McpTelemetryStore:
    path = tmp_path / "mcp_telemetry.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    monkeypatch.setattr(
        "menhir.mcp.tools.registered_tools",
        lambda: [_fake_tool(n) for n in ("alpha", "beta", "gamma")],
    )
    return McpTelemetryStore(db_path=path)


def _seed(
    store: McpTelemetryStore,
    op: str,
    *,
    success: bool = True,
    stage: str | None = "completed",
    client: str | None = None,
    duration_ms: int = 10,
    age_hours: float = 1,
) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    store.record(
        kind="tool",
        operation=op,
        started_at=ts,
        completed_at=ts,
        duration_ms=duration_ms,
        success=success,
        error=None if success else "boom",
        input_size=None,
        result_size=None,
        payload_preview=None,
        client_name=client,
        stage=stage,
    )


def _json(args: list[str]) -> dict:
    result = runner.invoke(app, ["tool-usage", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_zero_call_tools_listed_with_zero_counts(db: McpTelemetryStore) -> None:
    _seed(db, "alpha")
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 0, result.output
    assert "2 of 3 registered tools had no calls in the last 720 hours." in result.output
    beta = next(ln for ln in result.output.splitlines() if ln.startswith("beta"))
    assert beta.split() == ["beta", "0", "0", "0", "0", "0", "-"]
    assert result.output.splitlines()[1].startswith("alpha")


def test_stage_split(db: McpTelemetryStore) -> None:
    _seed(db, "alpha", duration_ms=10)
    _seed(db, "alpha", success=False, stage="failed")
    _seed(db, "alpha", success=False, stage="denied")
    _seed(db, "alpha", success=False, stage="timeout")
    _seed(db, "alpha", success=False, stage=None)
    row = next(r for r in _json([])["rows"] if r["tool"] == "alpha")
    assert (row["calls"], row["ok"], row["failed"], row["denied"], row["timeout"]) == (5, 1, 2, 1, 1)


def test_by_client_rows(db: McpTelemetryStore) -> None:
    _seed(db, "alpha", client="c1")
    _seed(db, "alpha", client="c1")
    _seed(db, "alpha", client="c2")
    _seed(db, "beta", client=None)
    result = runner.invoke(app, ["tool-usage", "--by", "client"])
    assert result.exit_code == 0, result.output
    data = _json(["--by", "client"])
    assert [(r["tool"], r["client_name"], r["calls"]) for r in data["rows"]] == [
        ("alpha", "c1", 2),
        ("alpha", "c2", 1),
        ("beta", "", 1),  # NULL client_name; the CLI never runs the back-fill migration
    ]
    assert data["unused_tools"] == ["gamma"]
    assert "Unused tools: gamma" in result.output


def test_json_shape(db: McpTelemetryStore) -> None:
    _seed(db, "alpha")
    data = _json([])
    assert set(data) == {"since_hours", "by", "rows", "unused_tools", "registered_tool_count"}
    assert data["since_hours"] == 720 and data["by"] == "tool"
    assert data["registered_tool_count"] == 3
    assert data["unused_tools"] == ["beta", "gamma"]
    for row in data["rows"]:
        assert set(row) == {"tool", "calls", "ok", "failed", "denied", "timeout", "p50_ms"}
    assert "other_operations" not in data


def test_unregistered_operations_hidden_unless_requested(db: McpTelemetryStore) -> None:
    _seed(db, "alpha")
    _seed(db, "scheduler_job")
    default = runner.invoke(app, ["tool-usage"])
    assert "scheduler_job" not in default.output
    shown = runner.invoke(app, ["tool-usage", "--all-operations"])
    assert "Other recorded operations" in shown.output
    assert "scheduler_job" in shown.output
    data = _json(["--all-operations"])
    assert [r["tool"] for r in data["other_operations"]] == ["scheduler_job"]
    assert all(r["tool"] != "scheduler_job" for r in data["rows"])


def test_window_excludes_old_events(db: McpTelemetryStore) -> None:
    _seed(db, "alpha", age_hours=1)
    _seed(db, "alpha", age_hours=100)
    row = next(r for r in _json(["--since-hours", "24"])["rows"] if r["tool"] == "alpha")
    assert row["calls"] == 1


def test_missing_db_exits_1_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sub" / "missing.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 1
    assert f"No telemetry database at {path}" in result.output
    assert not path.exists()
    assert not path.parent.exists()


def test_since_hours_zero_exits_2(db: McpTelemetryStore) -> None:
    result = runner.invoke(app, ["tool-usage", "--since-hours", "0"])
    assert result.exit_code == 2


def test_old_schema_reports_migration_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE mcp_events (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, "
        "completed_at TEXT NOT NULL, duration_ms INTEGER NOT NULL, operation TEXT NOT NULL, "
        "kind TEXT NOT NULL, success INTEGER NOT NULL, error TEXT, input_size INTEGER, "
        "result_size INTEGER, payload_preview TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 1
    assert (
        f"Telemetry database at {path} predates the client/stage columns; "
        "start the server once to migrate it."
    ) in result.output


def test_command_writes_nothing(db: McpTelemetryStore) -> None:
    import sqlite3

    _seed(db, "alpha")
    path = db.db_path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    wal = Path(f"{path}-wal")
    before = (path.read_bytes(), path.stat().st_mtime_ns, wal.exists())
    result = runner.invoke(app, ["tool-usage", "--all-operations"])
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before[0]
    assert path.stat().st_mtime_ns == before[1]
    # A mode=ro reader of a WAL database may create an EMPTY -wal/-shm pair (SQLite needs the
    # shared-memory file to read); it must never leave frames in the log.
    if wal.exists() and not before[2]:
        assert wal.stat().st_size == 0
