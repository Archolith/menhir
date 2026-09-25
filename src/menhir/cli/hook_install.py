"""Settings-file install/uninstall machinery behind the `menhir hook` commands."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

_HOOK_MARKER = "menhir.cli"


def _entry_has_menhir_hook(entry: object) -> bool:
    """True when `entry` is a settings hook-entry containing a menhir hook command.

    Total on arbitrary JSON: `~/.claude/settings.json` is hand-edited, so a malformed
    entry is expected input, not an exceptional one. Returns False rather than raising
    so a bad entry cannot abort an uninstall part-way through.
    """
    # The marker is read through the facade at call time so a monkeypatched
    # menhir.cli.hook._HOOK_MARKER stays authoritative for this helper.
    from menhir.cli import hook as _host

    marker = _host._HOOK_MARKER
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if isinstance(command, str) and marker in command:
            return True
    return False


def _strip_menhir_hook_commands(entry: object) -> tuple[int, int]:
    """Remove menhir hook commands from one entry in place, keeping the rest.

    A settings entry can co-register third-party commands alongside menhir's, so
    removal is per command, not per entry: the entry survives while any non-menhir
    command remains, and its caller drops it only once empty. Total on arbitrary
    JSON (same contract as `_entry_has_menhir_hook`): malformed entries return
    (0, 0) untouched so a bad entry cannot abort an uninstall part-way through.
    """
    # Same facade read as `_entry_has_menhir_hook`: monkeypatched
    # menhir.cli.hook._HOOK_MARKER stays authoritative.
    from menhir.cli import hook as _host

    marker = _host._HOOK_MARKER
    if not isinstance(entry, dict):
        return 0, 0
    hook_list = entry.get("hooks")
    if not isinstance(hook_list, list):
        return 0, 0
    kept = [
        hook
        for hook in hook_list
        if not (
            isinstance(hook, dict)
            and isinstance(hook.get("command"), str)
            and marker in hook["command"]
        )
    ]
    removed = len(hook_list) - len(kept)
    if removed:
        entry["hooks"] = kept
    return removed, len(kept)


def _upsert_hook_entry(hooks: dict, event_key: str, entry: dict) -> None:
    """Insert or refresh the menhir hook entry in the given event key."""
    event_hooks: list = hooks.setdefault(event_key, [])
    if not isinstance(event_hooks, list):
        raise ValueError(f"Expected hooks.{event_key} to be a JSON array")
    new_commands = entry.get("hooks")
    for i, existing_entry in enumerate(event_hooks):
        if not isinstance(existing_entry, dict):
            continue
        if not _entry_has_menhir_hook(existing_entry):
            continue
        if not isinstance(new_commands, list):
            # Malformed installer input: fall back to the old whole-entry replace.
            event_hooks[i] = entry
            return
        # Same granularity rule as uninstall (#113): a reinstall refreshes only
        # menhir's commands; third-party commands co-registered in the same entry
        # survive it.
        _strip_menhir_hook_commands(existing_entry)
        refreshed = existing_entry.get("hooks")
        if not isinstance(refreshed, list):
            refreshed = []
            existing_entry["hooks"] = refreshed
        refreshed.extend(new_commands)
        return
    event_hooks.append(entry)


def install_hooks(
    *,
    location: str,
    frequency: int = 10,
    save_frequency: int = 10,
    workspace: str = "",
    project_dir: Path | None = None,
) -> tuple[Path, dict[str, str]]:
    """Install the package-native recall hooks and preserve unrelated client config."""
    from menhir.domain.bootstrap_scope import normalize_workspace_key

    # The settings path is resolved through the facade at call time so a monkeypatched
    # menhir.cli.hook._resolve_settings_path stays authoritative.
    from menhir.cli import hook as _host

    location = location.strip().lower()
    if location not in ("user", "project"):
        raise ValueError(f"Invalid location: {location!r}. Use 'user' or 'project'.")
    if location == "project" and not workspace:
        raise ValueError("Project hook installation requires --workspace.")
    if location == "user" and workspace:
        raise ValueError("User hooks are general-only; omit --workspace.")

    workspace = normalize_workspace_key(workspace) if workspace else ""
    if location == "project" and not workspace:
        raise ValueError("Project hook installation requires a non-empty --workspace.")

    if project_dir is None:
        settings_path = _host._resolve_settings_path(location)
    else:
        settings_path = _host._resolve_settings_path(location, project_dir)

    python_exe = sys.executable
    venv_python = (
        Path(sys.prefix)
        / ("Scripts" if sys.platform == "win32" else "bin")
        / Path(sys.executable).name
    )
    if venv_python.exists():
        python_exe = str(venv_python)

    recall_args = [python_exe, "-m", "menhir.cli", "hook", "run", "--frequency", str(frequency)]
    if workspace:
        recall_args.extend(("--workspace", workspace))
    postcompact_args = [python_exe, "-m", "menhir.cli", "hook", "run", "--event", "postcompact"]
    if workspace:
        postcompact_args.extend(("--workspace", workspace))
    commands = {
        "recall": _format_hook_command(recall_args),
        "save": _format_hook_command(
            [python_exe, "-m", "menhir.cli", "hook", "run", "--event", "stop", "--frequency", str(save_frequency)]
        ),
        "postcompact": _format_hook_command(postcompact_args),
    }

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Could not parse {settings_path}: {exc}") from exc
        if not isinstance(existing, dict):
            raise ValueError(f"Expected a JSON object in {settings_path}")

    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected 'hooks' to be a JSON object in {settings_path}")

    _upsert_hook_entry(
        hooks,
        "UserPromptSubmit",
        {"hooks": [{"type": "command", "command": commands["recall"]}]},
    )
    _upsert_hook_entry(
        hooks,
        "Stop",
        {"hooks": [{"type": "command", "command": commands["save"]}]},
    )
    _upsert_hook_entry(
        hooks,
        "SessionStart",
        {"hooks": [{"type": "command", "command": commands["postcompact"]}]},
    )
    _remove_managed_hook_entries(hooks, "PostCompact")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return settings_path, commands


def _remove_managed_hook_entries(hooks: dict, event: str) -> None:
    """Drop Menhir-owned hook commands for *event*, and the event itself once empty.

    Needed when a registration moves: without it the old PostCompact entry survives every
    reinstall, and an installer that only ever adds leaves the dead one running forever.
    Third-party commands co-registered in a menhir entry survive the move.
    """
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return
    remaining: list = []
    for entry in entries:
        removed, kept = _strip_menhir_hook_commands(entry)
        if kept or not removed:
            remaining.append(entry)
    if remaining:
        hooks[event] = remaining
    else:
        hooks.pop(event, None)


def _format_hook_command(args: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _resolve_settings_path(location: str, project_dir: Path | None = None) -> Path:
    if location == "user":
        return Path.home() / ".claude" / "settings.json"
    return (project_dir or Path.cwd()) / ".claude" / "settings.local.json"
