"""Unit coverage for the idempotent Menhir post-install setup command."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
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
def test_created_env_is_not_world_readable(tmp_path: Path, monkeypatch) -> None:
    """CF-43: `.env` holds the Neo4j password and every API key.

    `shutil.copyfile` copies CONTENTS only, so the destination landed at the process default
    mode (typically 0o644) -- and setup then tells the operator to put secrets in it. The tell
    was thirteen lines further down, where the git hook IS chmod'ed.

    Asserted through a recorder rather than a mode read because this suite's host is Windows,
    where chmod only toggles the read-only bit. The real-mode assertion lives in the POSIX test
    below.
    """
    repo = _make_checkout(tmp_path / "menhir")
    seen: list[tuple[str, int]] = []
    real_chmod = Path.chmod

    def _record(self: Path, mode: int, **kwargs) -> None:
        seen.append((self.name, mode))
        real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", _record)
    changes = apply_setup(repo)

    # POSITIVE CONTROL: the env branch actually executed and produced the file. Without this,
    # the assertions below would pass against a setup run that skipped .env creation entirely
    # (it is guarded by `if not env_path.exists()`), recording no chmod and asserting nothing.
    assert any("created" in c and ".env" in c for c in changes), changes
    assert (repo / ".env").read_text(encoding="utf-8") == "NEO4J_URI=bolt://localhost:7687\n"

    modes = dict(seen)
    assert ".env" in modes, f"setup never chmod'ed .env; recorded: {seen}"
    assert modes[".env"] == 0o600


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_created_env_has_no_group_or_other_bits(tmp_path: Path) -> None:
    """The real-behaviour half of CF-43, on platforms where the mode means something."""
    repo = _make_checkout(tmp_path / "menhir")
    apply_setup(repo)
    mode = stat.S_IMODE((repo / ".env").stat().st_mode)
    assert mode & 0o077 == 0, f"world/group readable: {oct(mode)}"


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


# --- --provider / --compose-neo4j ---------------------------------------------------------

from menhir.cli.setup import COMPOSE_NEO4J_KEYS, upsert_env_keys  # noqa: E402


def _env_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


@pytest.mark.unit
def test_upsert_env_keys_rewrites_uncomments_and_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# header\nA=1\n# B=old\nC=keep\n", encoding="utf-8")

    changes = upsert_env_keys(env, {"A": "2", "B": "new", "D": "4", "C": None})

    assert changes == ["set A", "enabled B", "added D"]
    assert env.read_text(encoding="utf-8") == "# header\nA=2\nB=new\nC=keep\nD=4\n"
    assert upsert_env_keys(env, {"A": "2", "B": "new", "D": "4"}) == []


@pytest.mark.unit
def test_upsert_env_keys_never_overwrites_a_filled_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-real\n", encoding="utf-8")

    assert upsert_env_keys(env, {"OPENAI_API_KEY": None}) == []
    assert _env_map(env)["OPENAI_API_KEY"] == "sk-real"

    env.write_text("# OPENAI_API_KEY=\n", encoding="utf-8")
    assert upsert_env_keys(env, {"OPENAI_API_KEY": None}) == ["enabled OPENAI_API_KEY"]
    assert _env_map(env)["OPENAI_API_KEY"] == ""


@pytest.mark.unit
def test_apply_setup_provider_openai_with_compose_neo4j(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    (repo / ".env.example").write_text(
        "NEO4J_URI=bolt://localhost:7687\nNEO4J_PASSWORD=\nLLM_CHAT_PROVIDER=local\nGRAPHITI_LLM_PROVIDER=local\n"
        "# OPENAI_API_KEY=\nOPENAI_CHAT_MODEL=gpt-4.1-nano\n",
        encoding="utf-8",
    )

    first = apply_setup(repo, provider="openai", compose_neo4j=True)
    second = apply_setup(repo, provider="openai", compose_neo4j=True)

    env = _env_map(repo / ".env")
    assert env["NEO4J_PASSWORD"] == COMPOSE_NEO4J_KEYS["NEO4J_PASSWORD"] == "password"
    assert env["LLM_CHAT_PROVIDER"] == "openai"
    assert env["GRAPHITI_LLM_PROVIDER"] == "openai"
    assert env["GRAPHITI_EMBED_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == ""  # uncommented for the operator to fill in
    assert env["OPENAI_CHAT_MODEL"] == "gpt-4.1-nano"  # existing value untouched
    assert any(c.startswith("set NEO4J_PASSWORD") for c in first)
    assert second == []


@pytest.mark.unit
def test_apply_setup_rejects_unsupported_provider(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    with pytest.raises(SetupError, match="--provider must be one of"):
        apply_setup(repo, provider="gemini")
    with pytest.raises(SetupError, match="drop --no-env"):
        apply_setup(repo, create_env=False, provider="local")


@pytest.mark.unit
def test_setup_cli_accepts_provider_flags(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    result = CliRunner().invoke(
        app, ["setup", "--repo", str(repo), "--provider", "local", "--compose-neo4j"]
    )
    assert result.exit_code == 0, result.output
    env = _env_map(repo / ".env")
    assert env["LLM_CHAT_PROVIDER"] == "local"
    assert env["NEO4J_PASSWORD"] == "password"


@pytest.mark.unit
def test_check_and_diagnostics_load_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cold-start finding: only `serve` read .env, so the documented diagnostics -> check -> serve
    sequence reported stale defaults after the operator had configured everything."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("MENHIR_API_HOST", raising=False)
    (tmp_path / ".env").write_text("MENHIR_API_HOST=0.0.0.0\nMENHIR_API_KEY=k\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["diagnostics", "--json"])
    assert result.exit_code == 0, result.output
    assert '"host": "0.0.0.0"' in result.output

    seen: dict[str, str] = {}
    monkeypatch.setattr(
        "menhir.core.collect_runtime_failures",
        lambda settings, **kw: seen.__setitem__("host", settings.api_host) or [],
    )
    CliRunner().invoke(app, ["check"])
    assert seen["host"] == "0.0.0.0"
    assert os.environ["MENHIR_API_HOST"] == "0.0.0.0"


@pytest.mark.unit
def test_setup_ends_with_the_readiness_report_and_one_next_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three cold-start evaluators never tried `menhir up`: setup's old hint listed check/serve.
    Now setup ends where `up --check` would -- report, then exactly one next command."""
    repo = _make_checkout(tmp_path / "menhir")
    monkeypatch.delenv("ENV_FILE", raising=False)
    calls: list[Path] = []

    def _fake_report(checkout: Path):
        calls.append(checkout)
        print("[ok  ] neo4j: reachable")
        return object(), False

    monkeypatch.setattr("menhir.cli.up.report_readiness", _fake_report)
    result = CliRunner().invoke(app, ["setup", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert calls == [repo.resolve()]
    assert "[ok  ] neo4j: reachable" in result.output
    assert "Next: run 'menhir up' to start the server" in result.output
    assert "menhir check" not in result.output and "menhir serve" not in result.output


@pytest.mark.unit
def test_bare_menhir_runs_the_readiness_check_inside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_checkout(tmp_path / "menhir")
    monkeypatch.chdir(repo)
    seen: dict[str, object] = {}

    def _fake_up(**kwargs):
        seen.update(kwargs)

    import menhir.cli as cli_module

    monkeypatch.setattr(cli_module, "up_command", _fake_up)
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert seen == {"check": True}


@pytest.mark.unit
def test_bare_menhir_outside_a_checkout_shows_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output and "up" in result.output
