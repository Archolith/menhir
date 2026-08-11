"""Unit coverage for the idempotent Menhir post-install setup command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app
from menhir.cli.hook import install_hooks
from menhir.cli.setup import SetupError, apply_setup, find_checkout, inspect_setup


def _make_checkout(path: Path) -> Path:
    path.mkdir()
    (path / "pyproject.toml").write_text(
        '[project]\nname = "archolith-menhir"\n',
        encoding="utf-8",
    )
    (path / ".env.example").write_text("NEO4J_URI=bolt://localhost:7687\n", encoding="utf-8")
    hooks_dir = path / ".githooks"
    hooks_dir.mkdir()
    (hooks_dir / "pre-push").write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


@pytest.mark.unit
def test_find_checkout_walks_up_from_nested_directory(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    nested = repo / "src" / "menhir"
    nested.mkdir(parents=True)

    assert find_checkout(nested) == repo.resolve()


@pytest.mark.unit
def test_apply_setup_creates_env_and_wires_git_hooks_idempotently(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")

    first = apply_setup(repo)
    second = apply_setup(repo)

    assert (repo / ".env").read_text(encoding="utf-8") == "NEO4J_URI=bolt://localhost:7687\n"
    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert hooks_path == ".githooks"
    assert len(first) >= 2
    assert second == []
    assert all(item.status == "ok" for item in inspect_setup(repo))


@pytest.mark.unit
def test_apply_setup_refuses_to_replace_custom_git_hooks(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", "custom-hooks"],
        cwd=repo,
        check=True,
    )

    with pytest.raises(SetupError, match="Refusing to replace custom"):
        apply_setup(repo, create_env=False)

    assert inspect_setup(repo, check_env=False)[0].status == "custom"


@pytest.mark.unit
def test_apply_setup_can_install_project_claude_hooks(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")

    changes = apply_setup(repo, install_claude=True, workspace="Example Workspace")

    settings_path = repo / ".claude" / "settings.local.json"
    config = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(config["hooks"]) >= {"UserPromptSubmit", "Stop", "PostCompact"}
    assert any("Claude-compatible hooks" in change for change in changes)
    assert inspect_setup(repo, check_claude_hooks=True)[-1].status == "ok"


@pytest.mark.unit
def test_apply_setup_validates_project_workspace_before_mutation(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")

    with pytest.raises(SetupError, match="requires --workspace"):
        apply_setup(repo, install_claude=True)

    assert not (repo / ".env").exists()
    assert subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        check=False,
    ).returncode == 1


@pytest.mark.unit
def test_hook_install_rejects_malformed_settings_without_overwriting(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    settings = repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="Could not parse"):
        install_hooks(location="project", workspace="example", project_dir=repo)

    assert settings.read_text(encoding="utf-8") == "{not-json"


@pytest.mark.unit
def test_setup_check_is_non_mutating_and_reports_missing_items(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")

    result = CliRunner().invoke(app, ["setup", "--repo", str(repo), "--check"])

    assert result.exit_code == 1
    assert "[MISSING] environment" in result.output
    assert "[MISSING] git-hooks" in result.output
    assert not (repo / ".env").exists()
