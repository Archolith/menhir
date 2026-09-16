"""CLI coverage for `menhir sync --check`.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P1 gate: inspect-only, local only).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "widget"
    (project / "src").mkdir(parents=True)
    try:
        _git(project, "init", "--quiet")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "config", "user.name", "Test")
    (project / "src" / "main.py").write_bytes(b"print('hi')\n")
    (project / "README.md").write_bytes(b"# widget\n")
    _git(project, "add", "-A")
    _git(project, "commit", "--quiet", "-m", "initial")
    return project


def test_check_reports_what_would_upload(repo: Path) -> None:
    result = runner.invoke(app, ["sync", str(repo), "--check"])
    assert result.exit_code == 0, result.output
    assert "files to upload 2" in result.output
    assert "tree digest     sha256:" in result.output
    assert "widget" in result.output


def test_check_makes_no_network_call(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 is inspect-only; a socket here would mean the report reached out."""
    import socket

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("sync --check must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", explode)
    result = runner.invoke(app, ["sync", str(repo), "--check"])
    assert result.exit_code == 0, result.output


def test_check_writes_no_archive(repo: Path, tmp_path: Path) -> None:
    before = {p for p in tmp_path.rglob("*")}
    runner.invoke(app, ["sync", str(repo), "--check"])
    assert {p for p in tmp_path.rglob("*")} == before


def test_secret_risk_blocks_and_explains_the_override(repo: Path) -> None:
    (repo / ".env").write_bytes(b"TOKEN=live\n")
    _git(repo, "add", "-f", ".env")
    result = runner.invoke(app, ["sync", str(repo), "--check"])
    assert result.exit_code == 1
    assert "REFUSED" in result.output
    assert "--allow-path .env" in result.output
    assert "not remembered" in result.output


def test_explicit_override_clears_the_block(repo: Path) -> None:
    (repo / ".env").write_bytes(b"TOKEN=live\n")
    _git(repo, "add", "-f", ".env")
    result = runner.invoke(app, ["sync", str(repo), "--check", "--allow-path", ".env"])
    assert result.exit_code == 0, result.output
    assert "REFUSED" not in result.output


def test_name_override_is_used(repo: Path) -> None:
    result = runner.invoke(app, ["sync", str(repo), "--check", "--name", "other-name"])
    assert "project name    other-name" in result.output


def test_upload_without_check_refuses_and_says_why(repo: Path) -> None:
    result = runner.invoke(app, ["sync", str(repo)])
    assert result.exit_code == 2
    assert "only run with --check" in result.output
    assert "measured" in result.output


def test_outside_a_repository_fails_cleanly(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["sync", str(plain), "--check"])
    assert result.exit_code == 1
    assert "git repositor" in result.output
