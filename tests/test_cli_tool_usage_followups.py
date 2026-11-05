"""Follow-up coverage for `menhir tool-usage` (post-merge review of #480).

Kind filter, URI quoting, window bound, sqlite failure handling and the data-horizon note. The
original tests live in ``test_cli_tool_usage.py`` and are deliberately untouched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app
from menhir.cli.tool_usage import MAX_SINCE_HOURS
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.infrastructure.telemetry.tool_usage_query import _readonly_uri

runner = CliRunner()

_FAKE_CATALOG = frozenset({"alpha", "beta", "gamma"})


def _patch_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("menhir.api.mcp_remote.production_tool_catalog", lambda: _FAKE_CATALOG)


def _store_at(path: Path, monkeypatch: pytest.MonkeyPatch) -> McpTelemetryStore:
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    return McpTelemetryStore(db_path=path)


def _seed(
    store: McpTelemetryStore,
    op: str,
    *,
    kind: str = "tool",
    success: bool = True,
    stage: str | None = "completed",
    client: str | None = None,
    age_hours: float = 1,
) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    store.record(
        kind=kind,
        operation=op,
        started_at=ts,
        completed_at=ts,
        duration_ms=10,
        success=success,
        error=None if success else "boom",
        input_size=None,
        result_size=None,
        payload_preview=None,
        client_name=client,
        stage=stage,
    )


def _json_rows(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- F1: kind filter


def test_non_tool_row_named_like_a_tool_does_not_change_its_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha")  # the only real tool call
    _seed(db, "alpha", kind="background", client="bg")
    _seed(db, "alpha", kind="deprecated_operation", success=False, stage=None)
    _seed(db, "alpha", kind="resource")

    data = _json_rows(runner.invoke(app, ["tool-usage", "--json", "--all-operations"]))
    alpha = next(r for r in data["rows"] if r["tool"] == "alpha")
    assert (alpha["calls"], alpha["ok"], alpha["failed"]) == (1, 1, 0)

    others = {(r["tool"], r["kind"]): r["calls"] for r in data["other_operations"]}
    assert others == {
        ("alpha", "background"): 1,
        ("alpha", "deprecated_operation"): 1,
        ("alpha", "resource"): 1,
    }
    assert data["unused_tools"] == ["beta", "gamma"]


def test_all_operations_text_shows_kind_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha")
    _seed(db, "alpha", kind="background")
    _seed(db, "scheduler_job", kind="tool")  # tool-kind but not registered
    result = runner.invoke(app, ["tool-usage", "--all-operations"])
    assert result.exit_code == 0, result.output
    other_block = result.stdout.split("Other recorded operations", 1)[1].splitlines()
    header = other_block[1].split()
    assert header[:2] == ["operation", "kind"]
    cells = {tuple(line.split()[:2]) for line in other_block[2:] if line.strip()}
    assert cells == {("alpha", "background"), ("scheduler_job", "tool")}


def test_tool_with_only_non_tool_rows_is_still_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "beta", kind="background")
    data = _json_rows(runner.invoke(app, ["tool-usage", "--json", "--by", "client"]))
    assert data["rows"] == []
    assert data["unused_tools"] == ["alpha", "beta", "gamma"]


def test_real_catalog_is_used_when_not_patched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.api.mcp_remote import production_tool_catalog

    catalog = sorted(production_tool_catalog())
    assert len(catalog) > 3
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, catalog[0])
    data = _json_rows(runner.invoke(app, ["tool-usage", "--json"]))
    assert data["registered_tool_count"] == len(catalog)
    assert {r["tool"] for r in data["rows"]} == set(catalog)
    assert next(r for r in data["rows"] if r["tool"] == catalog[0])["calls"] == 1


# --------------------------------------------------------------------------- F2: URI quoting


@pytest.mark.parametrize("dirname", ["hash#dir", "pct%41 dir", "plain dir", "a%23b"])
def test_special_characters_in_db_path_read_correctly_and_create_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirname: str
) -> None:
    _patch_catalog(monkeypatch)
    folder = tmp_path / dirname
    folder.mkdir()
    db = _store_at(folder / "mcp_telemetry.db", monkeypatch)
    _seed(db, "alpha")

    top_before = sorted(p.name for p in tmp_path.iterdir())
    inner_before = sorted(p.name for p in folder.iterdir())
    data = _json_rows(runner.invoke(app, ["tool-usage", "--json"]))
    alpha = next(r for r in data["rows"] if r["tool"] == "alpha")
    assert alpha["calls"] == 1

    assert sorted(p.name for p in tmp_path.iterdir()) == top_before  # no stray truncated-path file
    # a read-only reader may add an EMPTY -wal/-shm pair, but never a differently named file
    new_inner = set(p.name for p in folder.iterdir()) - set(inner_before)
    assert all(n.startswith("mcp_telemetry.db-") for n in new_inner), new_inner


@pytest.mark.skipif(sys.platform == "win32", reason="'?' is not allowed in Windows paths")
def test_question_mark_in_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    folder = tmp_path / "q?dir"
    folder.mkdir()
    db = _store_at(folder / "mcp_telemetry.db", monkeypatch)
    _seed(db, "alpha")
    data = _json_rows(runner.invoke(app, ["tool-usage", "--json"]))
    assert next(r for r in data["rows"] if r["tool"] == "alpha")["calls"] == 1


def test_readonly_uri_is_percent_encoded(tmp_path: Path) -> None:
    uri = _readonly_uri(tmp_path / "x#y" / "a%b?.db")
    assert uri.startswith("file:") and uri.endswith("?mode=ro")
    path_part = uri.split("?mode=ro")[0]
    assert "#" not in path_part and "?" not in path_part
    assert "%23" in path_part and "%25" in path_part and "%3F" in path_part


# --------------------------------------------------------------------------- F3: window bound


def test_since_hours_upper_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha")

    assert MAX_SINCE_HOURS == 24 * 365 * 10 == 87600
    ok = runner.invoke(app, ["tool-usage", "--json", "--since-hours", str(MAX_SINCE_HOURS)])
    assert ok.exit_code == 0, ok.output
    assert next(r for r in json.loads(ok.stdout)["rows"] if r["tool"] == "alpha")["calls"] == 1

    over = runner.invoke(app, ["tool-usage", "--json", "--since-hours", str(MAX_SINCE_HOURS + 1)])
    assert over.exit_code == 2
    assert over.stdout == ""
    assert str(MAX_SINCE_HOURS) in over.stderr

    huge = runner.invoke(app, ["tool-usage", "--since-hours", "100000000"])
    assert huge.exit_code == 2  # the value that used to print "62 of 62 had no calls"


def test_since_hours_lower_bound_and_negative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    _store_at(tmp_path / "t.db", monkeypatch)._ensure_ready()
    assert runner.invoke(app, ["tool-usage", "--since-hours", "-3"]).exit_code == 2
    assert runner.invoke(app, ["tool-usage", "--since-hours", "0"]).exit_code == 2


# --------------------------------------------------------------------------- F4: sqlite failures


def _assert_one_line_error(result, path: Path, fragment: str) -> None:
    assert result.exit_code == 1, result.output
    assert result.stdout == "", "errors must not reach stdout (breaks --json consumers)"
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, lines
    assert str(path) in lines[0]
    assert fragment in lines[0]
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("extra", [[], ["--json"], ["--by", "client"]])
def test_not_a_database_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "bad.db"
    path.write_text("this is definitely not sqlite " * 50)
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    _assert_one_line_error(runner.invoke(app, ["tool-usage", *extra]), path, "not a valid SQLite database")


def test_zero_byte_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage", "--json"])
    _assert_one_line_error(result, path, "no mcp_events table")
    assert "predates" not in result.stderr  # the migration hint is for the missing-column case only
    assert path.stat().st_size == 0  # still untouched


def test_missing_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage"])
    _assert_one_line_error(result, path, "no mcp_events table")
    assert "predates" not in result.stderr


def test_missing_column_keeps_migration_hint_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE mcp_events (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, "
        "duration_ms INTEGER, operation TEXT NOT NULL, kind TEXT NOT NULL, success INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage", "--json"])
    _assert_one_line_error(result, path, "predates the client/stage columns")
    assert "start the server once to migrate it" in result.stderr


def test_locked_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "t.db"
    _store_at(path, monkeypatch)._ensure_ready()

    def _locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "menhir.infrastructure.telemetry.tool_usage_query.fetch_tool_usage_readonly", _locked
    )
    _assert_one_line_error(runner.invoke(app, ["tool-usage", "--json"]), path, "locked by another process")


def test_really_locked_rollback_journal_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine lock: a rollback-journal DB under an EXCLUSIVE writer blocks even readers."""

    _patch_catalog(monkeypatch)
    path = tmp_path / "locked.db"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute(
        "CREATE TABLE mcp_events (id INTEGER PRIMARY KEY, started_at TEXT, operation TEXT, kind TEXT,"
        " success INTEGER, duration_ms INTEGER, client_name TEXT, stage TEXT)"
    )
    writer.execute("BEGIN EXCLUSIVE")
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        "menhir.infrastructure.telemetry.tool_usage_query.sqlite3.connect",
        lambda *a, **k: real_connect(*a, **{**k, "timeout": 0.1}),
    )
    try:
        result = runner.invoke(app, ["tool-usage", "--json"])
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    _assert_one_line_error(result, path, "locked by another process")


def test_directory_instead_of_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "dir.db"
    path.mkdir()
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1 and str(path) in result.stderr


def test_missing_database_message_goes_to_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    path = tmp_path / "nope.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(path))
    result = runner.invoke(app, ["tool-usage", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"No telemetry database at {path}" in result.stderr
    assert not path.exists()


# --------------------------------------------------------------------------- F5: data horizon


def test_note_when_window_starts_before_oldest_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha", age_hours=2)
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 0, result.output
    summary = next(ln for ln in result.stdout.splitlines() if "registered tools had no calls" in ln)
    assert "2 of 3 registered tools had no calls in the last 720 hours." in summary
    assert "Telemetry history only starts at" in summary
    assert "MENHIR_TELEMETRY_OBSERVABILITY_RETENTION_DAYS" in summary
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    assert start.strftime("%Y-%m-%d") in summary or (start - timedelta(days=1)).strftime("%Y-%m-%d") in summary


def test_note_in_client_mode_is_a_separate_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha", client="c1", age_hours=2)
    result = runner.invoke(app, ["tool-usage", "--by", "client"])
    assert "Unused tools: beta, gamma" in result.stdout
    assert any(ln.startswith("Note: Telemetry history only starts at") for ln in result.stdout.splitlines())


def test_no_note_when_history_covers_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha", age_hours=100)
    result = runner.invoke(app, ["tool-usage", "--since-hours", "24"])
    assert result.exit_code == 0, result.output
    assert "Telemetry history" not in result.stdout
    assert "3 of 3 registered tools had no calls in the last 24 hours." in result.stdout


def test_note_when_database_has_no_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_catalog(monkeypatch)
    _store_at(tmp_path / "t.db", monkeypatch)._ensure_ready()
    result = runner.invoke(app, ["tool-usage"])
    assert result.exit_code == 0, result.output
    assert "The telemetry database holds no events" in result.stdout


def test_horizon_note_does_not_alter_json_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON payload is unchanged by the note (see report: a JSON field is blocked by an existing
    exact-key-set assertion), and nothing but the JSON document reaches stdout."""

    _patch_catalog(monkeypatch)
    db = _store_at(tmp_path / "t.db", monkeypatch)
    _seed(db, "alpha", age_hours=2)
    result = runner.invoke(app, ["tool-usage", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["registered_tool_count"] == 3
    assert result.stderr == ""
