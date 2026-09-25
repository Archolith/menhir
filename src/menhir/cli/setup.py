"""Idempotent post-install setup for a Menhir source checkout."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from menhir.cli.hook import _entry_has_menhir_hook, install_hooks
from menhir.cli.setup_env import (
    _GENERATED_COMPOSE,
    _GENERATED_ENV_HEADER,
    _PROVIDER_KEYS,
    _restrict_secret_file,
    COMPOSE_NEO4J_KEYS,
    SETUP_PROVIDERS,
    upsert_env_keys,
)


class SetupError(RuntimeError):
    """Raised when setup cannot preserve an existing local configuration safely."""


@dataclass(frozen=True)
class SetupItem:
    name: str
    status: str
    detail: str
    required: bool = True


def is_checkout(path: Path) -> bool:
    """True when *path* is a Menhir source checkout rather than a state directory."""

    return (path / "pyproject.toml").is_file() and (path / ".env.example").is_file()


def find_home(start: Path, *, explicit: bool = False) -> Path:
    """Return the directory Menhir should configure itself in.

    A source checkout when one is found -- that is where `.env`, the compose file and the
    managed Git hooks belong. Otherwise the state directory (`MENHIR_STATE_DIR`, else
    `~/.menhir`), which is what a `pip install` has: no repository, but a home for `.env` all
    the same. Without this, every documented first command failed on a pip install with
    "requires a source checkout".

    ``explicit`` is set when the user named a path with ``--repo``: that is a statement about
    where the checkout is, so a wrong path should say so rather than silently configuring
    somewhere else.
    """

    try:
        return find_checkout(start)
    except SetupError:
        if explicit:
            raise
        from menhir.infrastructure.paths import state_dir

        home = state_dir()
        home.mkdir(parents=True, exist_ok=True)
        return home


def find_checkout(start: Path) -> Path:
    """Find the nearest checkout whose package metadata identifies Menhir."""
    candidate = start.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == "archolith-menhir":
            return directory

    raise SetupError(
        "Menhir setup requires a source checkout. Run it from the cloned repository "
        "or pass --repo PATH."
    )


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _git_hooks_path(repo: Path) -> str:
    result = _run_git(repo, "config", "--local", "--get", "core.hooksPath")
    if result.returncode not in (0, 1):
        raise SetupError(result.stderr.strip() or "Could not read local Git hook configuration.")
    return result.stdout.strip()


def _is_managed_hooks_path(value: str) -> bool:
    return value.replace("\\", "/").rstrip("/") == ".githooks"


def _claude_settings_path(repo: Path, location: str) -> Path:
    if location == "user":
        return Path.home() / ".claude" / "settings.json"
    return repo / ".claude" / "settings.local.json"


def _claude_hooks_installed(repo: Path, location: str) -> bool:
    path = _claude_settings_path(repo, location)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    for event in ("UserPromptSubmit", "Stop", "SessionStart"):
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        if not any(_entry_has_menhir_hook(entry) for entry in entries):
            return False
    return True


def inspect_setup(
    repo: Path,
    *,
    check_env: bool = True,
    check_git_hooks: bool = True,
    check_claude_hooks: bool = False,
    hook_location: str = "project",
) -> list[SetupItem]:
    """Return filesystem-only setup status without contacting Menhir dependencies."""
    items: list[SetupItem] = []

    if check_env:
        env_path = repo / ".env"
        items.append(
            SetupItem(
                "environment",
                "ok" if env_path.exists() else "missing",
                str(env_path),
            )
        )

    if check_git_hooks:
        pre_push = repo / ".githooks" / "pre-push"
        hooks_path = _git_hooks_path(repo)
        if not pre_push.is_file():
            items.append(SetupItem("git-hooks", "error", f"Missing managed hook: {pre_push}"))
        elif _is_managed_hooks_path(hooks_path):
            items.append(SetupItem("git-hooks", "ok", "core.hooksPath=.githooks"))
        elif hooks_path:
            items.append(SetupItem("git-hooks", "custom", f"core.hooksPath={hooks_path}"))
        else:
            items.append(SetupItem("git-hooks", "missing", "core.hooksPath is not configured"))

    if check_claude_hooks:
        installed = _claude_hooks_installed(repo, hook_location)
        items.append(
            SetupItem(
                "claude-hooks",
                "ok" if installed else "missing",
                str(_claude_settings_path(repo, hook_location)),
            )
        )

    return items


def apply_setup(
    repo: Path,
    *,
    create_env: bool = True,
    configure_git_hooks: bool = True,
    force_git_hooks: bool = False,
    install_claude: bool = False,
    hook_location: str = "project",
    workspace: str = "",
    provider: str | None = None,
    compose_neo4j: bool = False,
) -> list[str]:
    """Apply safe checkout setup and return a list of changes made."""
    changes: list[str] = []

    provider = provider.strip().lower() if provider else None
    if provider is not None and provider not in SETUP_PROVIDERS:
        raise SetupError(f"--provider must be one of {', '.join(SETUP_PROVIDERS)}.")
    if (provider is not None or compose_neo4j) and not create_env:
        raise SetupError("--provider and --compose-neo4j need the .env step; drop --no-env.")

    hook_location = hook_location.strip().lower()
    if hook_location not in ("project", "user"):
        raise SetupError("--hook-location must be 'project' or 'user'.")
    if install_claude and hook_location == "project" and not workspace.strip():
        raise SetupError("Project Claude hook installation requires --workspace.")
    if install_claude and hook_location == "user" and workspace.strip():
        raise SetupError("User Claude hooks are general-only; omit --workspace.")

    checkout = is_checkout(repo)

    if create_env:
        env_path = repo / ".env"
        env_example = repo / ".env.example"
        if not env_path.exists():
            if not env_example.is_file() and checkout:
                raise SetupError(f"Missing environment template: {env_example}")
            if env_example.is_file():
                shutil.copyfile(env_example, env_path)
            else:
                env_path.write_text(_GENERATED_ENV_HEADER, encoding="utf-8", newline="\n")
            # copyfile copies CONTENTS only, so the new file lands at the process
            # default mode (typically 0o644). This file holds the Neo4j password and
            # every API key, so restrict it before the operator is told to fill it in.
            env_path.chmod(0o600)
            changes.append(f"created {env_path}")
            changes.extend(_restrict_secret_file(env_path))
        env_updates: dict[str, str | None] = {}
        if compose_neo4j:
            env_updates.update(COMPOSE_NEO4J_KEYS)
        if provider is not None:
            env_updates.update(_PROVIDER_KEYS[provider])
        if env_updates:
            changes.extend(upsert_env_keys(env_path, env_updates))

    if configure_git_hooks and not checkout:
        # Nothing to hook: a pip install has no working tree. Reported rather than skipped
        # silently, so the absence is visible in the change list.
        changes.append("skipped Git hooks (no source checkout)")
    elif configure_git_hooks:
        pre_push = repo / ".githooks" / "pre-push"
        if not pre_push.is_file():
            raise SetupError(f"Missing managed Git hook: {pre_push}")

        current = _git_hooks_path(repo)
        if current and not _is_managed_hooks_path(current) and not force_git_hooks:
            raise SetupError(
                f"Refusing to replace custom core.hooksPath={current!r}. "
                "Re-run with --force-git-hooks after merging any existing hooks."
            )
        if not _is_managed_hooks_path(current):
            result = _run_git(repo, "config", "--local", "core.hooksPath", ".githooks")
            if result.returncode != 0:
                raise SetupError(result.stderr.strip() or "Could not configure Git hooks.")
            changes.append("configured core.hooksPath=.githooks")

        if os.name != "nt":
            mode = pre_push.stat().st_mode
            executable_mode = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if executable_mode != mode:
                pre_push.chmod(executable_mode)
                changes.append(f"made {pre_push} executable")

    if install_claude:
        settings_path, _ = install_hooks(
            location=hook_location,
            workspace=workspace,
            project_dir=repo,
        )
        changes.append(f"installed Claude-compatible hooks in {settings_path}")

    return changes


def _install_windows_watchdog(repo: Path) -> None:
    if os.name != "nt":
        raise SetupError("--install-watchdog is available only on Windows.")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise SetupError("PowerShell was not found; cannot install the Windows watchdog task.")
    script = repo / "scripts" / "start-server.ps1"
    result = subprocess.run(
        [powershell, "-NonInteractive", "-File", str(script), "-Action", "install-task"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SetupError(result.stderr.strip() or result.stdout.strip() or "Watchdog installation failed.")


def _print_items(items: list[SetupItem]) -> None:
    labels = {"ok": "OK", "missing": "MISSING", "custom": "CUSTOM", "error": "ERROR"}
    for item in items:
        typer.echo(f"[{labels.get(item.status, item.status.upper())}] {item.name}: {item.detail}")


def setup(
    repo: Annotated[
        Path | None,
        typer.Option(help="Menhir source checkout (default: search from cwd)."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Inspect only; do not change files or config."),
    ] = False,
    create_env: Annotated[
        bool,
        typer.Option("--env/--no-env", help="Create .env from .env.example if missing."),
    ] = True,
    git_hooks: Annotated[
        bool,
        typer.Option("--git-hooks/--no-git-hooks", help="Configure repository-managed Git hooks."),
    ] = True,
    force_git_hooks: Annotated[
        bool,
        typer.Option(help="Replace a different existing core.hooksPath."),
    ] = False,
    install_claude: Annotated[
        bool,
        typer.Option("--install-claude-hooks", help="Install opt-in Claude-compatible recall hooks."),
    ] = False,
    hook_location: Annotated[str, typer.Option(help="Claude hook location: project or user.")] = "project",
    workspace: Annotated[str, typer.Option(help="Workspace key required for project Claude hooks.")] = "",
    install_watchdog: Annotated[
        bool,
        typer.Option(help="Install the opt-in Windows login watchdog task."),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(help="Write a consistent LLM provider block into .env: local or openai."),
    ] = None,
    compose_neo4j: Annotated[
        bool,
        typer.Option("--compose-neo4j", help="Point .env at the root docker-compose.yml Neo4j (neo4j/password)."),
    ] = False,
) -> None:
    """Finish safe post-install setup and report conditional next steps."""
    try:
        checkout = find_home(repo or Path.cwd(), explicit=repo is not None)
        hook_location = hook_location.strip().lower()
        if hook_location not in ("project", "user"):
            raise SetupError("--hook-location must be 'project' or 'user'.")

        if check:
            items = inspect_setup(
                checkout,
                check_env=create_env,
                check_git_hooks=git_hooks,
                check_claude_hooks=install_claude,
                hook_location=hook_location,
            )
            typer.echo(f"Menhir setup check: {checkout}")
            _print_items(items)
            if any(item.required and item.status != "ok" for item in items):
                raise typer.Exit(1)
            typer.echo("Next: run 'menhir setup' to apply, then 'menhir up' to start the server.")
        else:
            changes = apply_setup(
                checkout,
                create_env=create_env,
                configure_git_hooks=git_hooks,
                force_git_hooks=force_git_hooks,
                install_claude=install_claude,
                hook_location=hook_location,
                workspace=workspace,
                provider=provider,
                compose_neo4j=compose_neo4j,
            )
            if install_watchdog:
                _install_windows_watchdog(checkout)
                changes.append("installed the menhir-watchdog scheduled task")
            typer.echo(f"Menhir setup complete: {checkout}")
            if changes:
                for change in changes:
                    typer.echo(f"[CHANGED] {change}")
            else:
                typer.echo("[OK] already configured")
            # End where `menhir up --check` would: the same tier report, one next command.
            # Three cold-start evaluators read "Next: configure .env, check, serve" here and
            # never tried `up`; showing the report removes the choice.
            if create_env:
                from menhir.cli.up import next_step_hint, report_readiness

                typer.echo("")
                _capabilities, blocking = report_readiness(checkout)
                typer.echo(next_step_hint(blocking))
            else:
                typer.echo("Next: run 'menhir up' to start the server.")
        typer.echo("Optional capture integrations: docs/post-install.md and scripts/hooks/README.md")
    except typer.Exit:
        raise
    except (OSError, SetupError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"Setup failed: {exc}", err=True)
        raise typer.Exit(1) from exc


__all__ = [
    "COMPOSE_NEO4J_KEYS",
    "SETUP_PROVIDERS",
    "SetupError",
    "SetupItem",
    "apply_setup",
    "upsert_env_keys",
    "find_checkout",
    "find_home",
    "is_checkout",
    "inspect_setup",
    "setup",
]
