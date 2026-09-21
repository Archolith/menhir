"""CLI coverage for `menhir sync --check`.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P1 gate: inspect-only, local only).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app
from menhir.cli.sync import _write_remote_project_id
from menhir.snapshot.upload_client import SnapshotUploadError, UploadOutcome

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


def test_upload_without_a_remote_says_which_setting_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2B replaced the blanket refusal. This pins the message that took its place.

    The old test asserted `menhir sync` refuses because the upload path does not exist and the
    limits are unmeasured. Both became false, so it could not stay -- but the useful half of it
    can: an unqualified sync that cannot proceed must say exactly which setting is missing.
    """
    monkeypatch.delenv("MENHIR_BACKEND_URL", raising=False)

    result = runner.invoke(app, ["sync", str(repo)])

    assert result.exit_code == 2
    assert "MENHIR_BACKEND_URL" in result.output
    assert "--check" in result.output, "the local alternative should be offered"


def test_upload_with_a_non_operator_key_names_the_tier(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receive tools are operator-tier.

    Sending an agent key gets a permission refusal from the server that says nothing about tiers,
    so the caller goes looking for a broken server instead of a wrong key. Catch it locally and
    name the tier.
    """
    monkeypatch.setenv("MENHIR_BACKEND_URL", "https://example.invalid")
    monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
    monkeypatch.setenv("MENHIR_AGENT_KEY", "an-agent-key")

    result = runner.invoke(app, ["sync", str(repo)])

    assert result.exit_code == 2
    assert "MENHIR_OPERATOR_KEY" in result.output
    assert "operator-tier" in result.output


def test_successful_sync_reports_the_terminal_server_stage(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENHIR_BACKEND_URL", "https://example.invalid")
    monkeypatch.setenv("MENHIR_OPERATOR_KEY", "operator-key")
    seen_project_ids: list[str | None] = []

    class _Uploader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def upload(self, *_args, **kwargs) -> UploadOutcome:
            seen_project_ids.append(kwargs.get("project_id"))
            return UploadOutcome(
                upload_id="upload-1",
                state="READY",
                chunk_bytes=1024,
                total_chunks=1,
                sent_chunks=1,
                bytes_sent=100,
                project_id="project-" + ("1" * 32),
                snapshot_id="snapshot-1",
                result={"stage": "published", "generation": 2},
            )

    monkeypatch.setattr("menhir.cli.sync.SnapshotUploader", _Uploader)

    result = runner.invoke(app, ["sync", str(repo)])

    assert result.exit_code == 0, result.output
    assert "server stage    published" in result.output
    assert "project id      project-" + ("1" * 32) in result.output
    assert "published the snapshot" in result.output
    receipts = list((repo / ".menhir" / "sources").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["project_id"] == (
        "project-" + ("1" * 32)
    )

    again = runner.invoke(app, ["sync", str(repo)])
    assert again.exit_code == 0, again.output
    assert seen_project_ids == [None, "project-" + ("1" * 32)]


def test_receipt_write_failure_is_reported_after_server_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENHIR_BACKEND_URL", "https://example.invalid")
    monkeypatch.setenv("MENHIR_OPERATOR_KEY", "operator-key")

    class _Uploader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def upload(self, *_args, **_kwargs) -> UploadOutcome:
            return UploadOutcome(
                upload_id="upload-1",
                state="READY",
                chunk_bytes=1024,
                total_chunks=1,
                sent_chunks=1,
                bytes_sent=100,
                project_id="project-" + ("1" * 32),
                snapshot_id="snapshot-1",
                result={"stage": "published"},
            )

    monkeypatch.setattr("menhir.cli.sync.SnapshotUploader", _Uploader)
    monkeypatch.setattr(
        "menhir.cli.sync.os.link", lambda *_args: (_ for _ in ()).throw(OSError("disk"))
    )

    result = runner.invoke(app, ["sync", str(repo)])

    assert result.exit_code == 1
    assert "sync completed, but the local identity receipt failed" in result.output
    assert "sync.identity.receipt_write_failed" in result.output
    receipt_dir = repo / ".menhir" / "sources"
    assert not list(receipt_dir.glob("*.json"))


def test_receipt_directory_creation_error_uses_the_stable_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(SnapshotUploadError) as excinfo:
        _write_remote_project_id(
            tmp_path, "https://example.invalid", "project-" + ("1" * 32)
        )

    assert excinfo.value.code == "sync.identity.receipt_write_failed"


def test_a_secret_refusal_blocks_the_upload_before_any_network_call(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety property of the whole command, asserted without `--check`.

    Every other refusal test runs in `--check`, where nothing could be sent anyway. This one
    configures a complete, valid remote and proves the send is unreachable while a refusal stands
    -- by making any attempt to construct an uploader fail the test outright.
    """
    # `-f` because the file must be TRACKED to enter the plan at all -- an untracked .env is
    # never enumerated, so it can neither be refused nor sent, and a test that skipped this would
    # pass while proving nothing.
    (repo / ".env").write_bytes(b"TOKEN=live\n")
    _git(repo, "add", "-f", ".env")
    monkeypatch.setenv("MENHIR_BACKEND_URL", "https://example.invalid")
    monkeypatch.setenv("MENHIR_OPERATOR_KEY", "an-operator-key")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a blocked plan reached the network")

    monkeypatch.setattr("menhir.cli.sync.SnapshotUploader", explode)

    result = runner.invoke(app, ["sync", str(repo)])

    assert result.exit_code == 1
    assert "REFUSED" in result.output


def test_outside_a_repository_fails_cleanly(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["sync", str(plain), "--check"])
    assert result.exit_code == 1
    assert "git repositor" in result.output
