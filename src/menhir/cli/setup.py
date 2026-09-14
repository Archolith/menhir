"""Idempotent post-install setup for a Menhir source checkout."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from menhir.cli.hook import _entry_has_menhir_hook, install_hooks


class SetupError(RuntimeError):
    """Raised when setup cannot preserve an existing local configuration safely."""


@dataclass(frozen=True)
class SetupItem:
    name: str
    status: str
    detail: str
    required: bool = True


#: Providers `menhir setup --provider` can make fully consistent. Gemini is chat-only
#: (Graphiti extraction requires an OpenAI-compatible provider), so it is not a
#: one-flag path and stays a manual .env edit.
SETUP_PROVIDERS = ("local", "openai")

#: Keys written for each provider. A value of ``None`` means "ensure the key exists,
#: never overwrite a non-empty value" -- used for secrets the operator fills in.
_PROVIDER_KEYS: dict[str, dict[str, str | None]] = {
    "local": {
        "LLM_CHAT_PROVIDER": "local",
        "GRAPHITI_LLM_PROVIDER": "local",
        "GRAPHITI_EMBED_PROVIDER": "local",
        "LOCAL_LLM_BASE_URL": None,
        "LOCAL_LLM_CHAT_MODEL": None,
        "LOCAL_LLM_EMBED_MODEL": None,
    },
    "openai": {
        "LLM_CHAT_PROVIDER": "openai",
        "GRAPHITI_LLM_PROVIDER": "openai",
        "GRAPHITI_EMBED_PROVIDER": "openai",
        "OPENAI_API_KEY": None,
        "OPENAI_CHAT_MODEL": None,
        "OPENAI_EMBED_MODEL": None,
    },
}

#: Credentials matching the root docker-compose.yml Neo4j service.
COMPOSE_NEO4J_KEYS: dict[str, str | None] = {
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "password",
    "NEO4J_DATABASE": "neo4j",
}


def _split_env_line(line: str) -> tuple[str, str, str] | None:
    """Return (prefix, key, value) for ``KEY=value`` or ``# KEY=value`` lines, else None."""

    stripped = line.lstrip()
    prefix = ""
    if stripped.startswith("#"):
        prefix = "#"
        stripped = stripped[1:].lstrip()
    if "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key or not all(ch.isalnum() or ch == "_" for ch in key):
        return None
    return prefix, key, value


def upsert_env_keys(env_path: Path, updates: dict[str, str | None]) -> list[str]:
    """Set keys in a dotenv file, preserving every other line. Returns the changes made.

    - An active ``KEY=`` line is rewritten in place (only when the value differs).
    - A commented ``# KEY=`` line is uncommented in place.
    - A missing key is appended.
    - ``None`` values mean *ensure present*: an existing active line with a non-empty
      value is left untouched, otherwise ``KEY=`` is written so the operator sees what
      to fill in. Secrets therefore never get overwritten by re-running setup.
    """

    lines = env_path.read_text(encoding="utf-8").split("\n") if env_path.exists() else [""]
    changes: list[str] = []
    active_idx: dict[str, int] = {}
    commented_idx: dict[str, int] = {}
    for i, line in enumerate(lines):
        parsed = _split_env_line(line)
        if parsed is None:
            continue
        prefix, key, _ = parsed
        if prefix == "" and key not in active_idx:
            active_idx[key] = i
        elif prefix == "#" and key not in commented_idx:
            commented_idx[key] = i

    for key, desired in updates.items():
        if key in active_idx:
            i = active_idx[key]
            _, _, current = _split_env_line(lines[i])  # type: ignore[misc]
            current = current.strip()
            if desired is None:
                continue  # ensure-present only; an active line already satisfies it
            if current != desired:
                lines[i] = f"{key}={desired}"
                changes.append(f"set {key}")
            continue
        value = "" if desired is None else desired
        if key in commented_idx:
            lines[commented_idx[key]] = f"{key}={value}"
            changes.append(f"enabled {key}")
            continue
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, f"{key}={value}")
        else:
            lines.append(f"{key}={value}")
        changes.append(f"added {key}")

    if changes:
        env_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return changes


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
    for event in ("UserPromptSubmit", "Stop", "PostCompact"):
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
        raise SetupError(
            f"--provider must be one of {', '.join(SETUP_PROVIDERS)} "
            "(gemini is chat-only and cannot back Graphiti extraction; edit .env by hand)."
        )
    if (provider is not None or compose_neo4j) and not create_env:
        raise SetupError("--provider and --compose-neo4j need the .env step; drop --no-env.")

    hook_location = hook_location.strip().lower()
    if hook_location not in ("project", "user"):
        raise SetupError("--hook-location must be 'project' or 'user'.")
    if install_claude and hook_location == "project" and not workspace.strip():
        raise SetupError("Project Claude hook installation requires --workspace.")
    if install_claude and hook_location == "user" and workspace.strip():
        raise SetupError("User Claude hooks are general-only; omit --workspace.")

    if create_env:
        env_path = repo / ".env"
        env_example = repo / ".env.example"
        if not env_path.exists():
            if not env_example.is_file():
                raise SetupError(f"Missing environment template: {env_example}")
            shutil.copyfile(env_example, env_path)
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

    if configure_git_hooks:
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
        checkout = find_checkout(repo or Path.cwd())
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

        typer.echo("Next: configure .env, run 'menhir check', start 'menhir serve', then register your MCP client.")
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
    "inspect_setup",
    "setup",
]


def _restrict_secret_file(path: Path) -> list[str]:
    """Tighten a secret-bearing file's ACL on Windows, where ``chmod`` cannot (CF-43).

    ``os.chmod`` on win32 toggles ONE bit -- the read-only attribute -- and does nothing to the
    ACL. So ``env_path.chmod(0o600)`` above is correct and sufficient on POSIX and inert on the
    platform this project is primarily developed on: the file stays readable by every account on
    the machine. The finding was closed on the POSIX half alone.

    `icacls` is the supported way to do this without a pywin32 dependency: strip inheritance and
    grant only the current user. Best-effort by design -- a setup command must not fail because
    an ACL could not be tightened, and the caller is told either way rather than being left to
    assume it worked.
    """
    if sys.platform != "win32":
        return []

    user = os.environ.get("USERNAME") or ""
    if not user:
        return [f"WARNING: could not restrict {path}: USERNAME is unset"]
    domain = os.environ.get("USERDOMAIN") or ""
    # Joined rather than interpolated: a literal backslash inside an f-string is an escape
    # sequence waiting to be mangled, and DOMAIN\user is exactly the shape icacls expects.
    principal = "\\".join([domain, user]) if domain else user

    try:
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"WARNING: could not restrict {path} to {principal}: {exc}"]

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return [
            f"WARNING: could not restrict {path} to {principal}: "
            f"icacls exited {completed.returncode}: {detail[0] if detail else 'no output'}"
        ]
    return [f"restricted {path} to {principal} (ACL)"]
