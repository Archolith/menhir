"""Hook subcommand group: run, install, uninstall."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from menhir.cli.output import (
    detect_write_signals,
    format_hook_output,
    format_save_checkpoint,
    format_temporal_line,
    format_write_nudge,
    should_run_this_turn,
    wrap_hook_response,
)

hook_app = typer.Typer(name="hook", help="Claude Code hook integration.")

MIN_PROMPT_LEN = 15
_HOOK_MARKER = "menhir.cli"
CONTEXT_TIMEOUT_S: float = 8.0


# ---------------------------------------------------------------------------
# hook run
# ---------------------------------------------------------------------------

@hook_app.command()
def run(
    query: Annotated[str, typer.Option(help="Override recall query.")] = "",
    max_tokens: Annotated[int, typer.Option(help="Token budget for context recall.")] = 1500,
    frequency: Annotated[int, typer.Option(help="Run every N turns (0 = every turn).")] = 5,
    event: Annotated[str, typer.Option(help="Hook event type: 'prompt' (UserPromptSubmit) or 'stop' (Stop).")] = "prompt",
    workspace: Annotated[str, typer.Option(help="Explicit workspace key for scoped bootstrap recall.")] = "",
) -> None:
    """Execute memory recall or save checkpoint for Claude Code hook injection."""
    try:
        if event == "stop":
            _run_stop_impl(frequency=frequency)
        elif event == "postcompact":
            _run_postcompact_impl(max_tokens=max_tokens, workspace=workspace or None)
        else:
            _run_prompt_impl(
                query=query,
                max_tokens=max_tokens,
                frequency=frequency,
                workspace=workspace or None,
            )
    except Exception as exc:
        # Never crash in hook mode -- let Claude Code proceed. But a failure must not be
        # indistinguishable from an empty graph (CF-40), so it is reported in the payload too.
        print(f"menhir hook: unexpected {type(exc).__name__}, emitting empty response", file=sys.stderr)
        print(wrap_hook_response(degraded=f"unexpected {type(exc).__name__}"), flush=True)


def _parse_stdin() -> tuple[str, str]:
    """Parse session_id and prompt from stdin JSON. Returns (session_id, prompt)."""
    session_id = "unknown"
    prompt = ""
    if not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            session_id = hook_input.get("session_id", session_id)
            prompt = hook_input.get("prompt", "")
        except Exception as exc:
            print(f"menhir hook: stdin parse failed ({type(exc).__name__})", file=sys.stderr)
    return session_id, prompt


def _load_env() -> None:
    """Load .env from ENV_FILE env var or package root."""
    import os
    from dotenv import load_dotenv

    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        pkg_dir = Path(__file__).resolve().parent.parent.parent.parent
        load_dotenv(pkg_dir / ".env")


# ---------------------------------------------------------------------------
# UserPromptSubmit event (reading + write nudges)
# ---------------------------------------------------------------------------

def _run_prompt_impl(
    *, query: str, max_tokens: int, frequency: int, workspace: str | None = None
) -> None:
    session_id, stdin_prompt = _parse_stdin()
    prompt = query or stdin_prompt

    # Detect write signals BEFORE frequency gate (always check for corrections)
    signals = detect_write_signals(prompt)
    write_nudge = format_write_nudge(signals) if signals else None

    # Load env + settings once — needed for both temporal tracking and hook services
    _load_env()
    from menhir.config import MemorySettings
    settings = MemorySettings.from_env()

    # Gate check first so we know whether to compute temporal_line
    recall_gated = frequency > 0 and not should_run_this_turn(session_id, frequency)

    # Always touch session for tracking; only build temporal_line on recall turns
    temporal_line: str | None = None
    try:
        from menhir.infrastructure.telemetry.store import telemetry_store
        client_id = settings.mcp_client_id
        if not client_id and settings.api_key:
            import hashlib
            client_id = hashlib.sha256(settings.api_key.encode()).hexdigest()[:16]
        client_name = settings.mcp_client_name or settings.mcp_client_user_id
        last_accessed = telemetry_store.get_session_last_accessed(session_id)
        telemetry_store.touch_session(session_id, client_id, client_name)
        if client_id:
            telemetry_store.touch_client(client_id, client_name)
        if not recall_gated:
            temporal_line = format_temporal_line(last_accessed)
    except Exception as exc:
        print(f"menhir hook: temporal telemetry failed ({type(exc).__name__})", file=sys.stderr)

    if recall_gated:
        print(wrap_hook_response(write_nudge), flush=True)
        return

    from menhir.cli.bootstrap import build_hook_services
    svc = build_hook_services(settings)

    # Fast path: flagged memories + open TODOs + TEMPORAL reminders (always)
    flagged = svc.graph_adapter.fetch_flagged_memories(limit=10, workspace=workspace)

    todos: list[dict] = []
    try:
        todos = svc.graph_adapter.list_todos(status="open", limit=5, namespace=workspace)
    except Exception as exc:
        print(f"menhir hook: todo recall failed ({type(exc).__name__})", file=sys.stderr)

    temporal_memories: list[dict] = []
    try:
        temporal_memories = svc.graph_adapter.list_temporal_in_window(
            window_days=30, namespace=workspace
        )
    except Exception as exc:
        print(f"menhir hook: temporal recall failed ({type(exc).__name__})", file=sys.stderr)

    # Full path: context recall (if prompt is long enough and services available)
    context_text: str | None = None
    effective_query = prompt.strip() if len(prompt.strip()) >= MIN_PROMPT_LEN else None
    if effective_query and svc.context_builder is not None:
        try:
            from menhir.domain.recall import QueryPreset

            result = asyncio.run(
                asyncio.wait_for(
                    svc.context_builder.build_context(
                        effective_query,
                        max_tokens=max_tokens,
                        preset=QueryPreset.KNOWLEDGE,
                        namespace=workspace,
                    ),
                    timeout=CONTEXT_TIMEOUT_S,
                )
            )
            context_text = result.context
        except TimeoutError:
            print(
                f"menhir hook: context recall timed out after {CONTEXT_TIMEOUT_S}s, using flagged only",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"menhir hook: context recall failed ({type(exc).__name__}), using flagged only",
                file=sys.stderr,
            )

    output = format_hook_output(flagged, context_text, effective_query, write_nudge, temporal_line, todos=todos, temporal_memories=temporal_memories)
    print(wrap_hook_response(output or None), flush=True)


# ---------------------------------------------------------------------------
# PostCompact event (forced recall into fresh context — no frequency gate)
# ---------------------------------------------------------------------------

def _run_postcompact_impl(*, max_tokens: int, workspace: str | None = None) -> None:
    """PostCompact: always inject memories into the fresh context window."""
    session_id = "unknown"
    compact_summary = ""
    if not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            session_id = hook_input.get("session_id", session_id)
            compact_summary = hook_input.get("compact_summary", "")
        except Exception as exc:
            print(f"menhir hook: stdin parse failed ({type(exc).__name__})", file=sys.stderr)

    _load_env()

    from menhir.cli.bootstrap import build_hook_services
    from menhir.config import MemorySettings

    settings = MemorySettings.from_env()
    svc = build_hook_services(settings)

    # Use the compact summary as query — it's the most targeted signal available
    effective_query = compact_summary.strip() if len(compact_summary.strip()) >= MIN_PROMPT_LEN else "recent session context and active work"

    # Temporal tracking — touch session so elapsed time resets at compaction boundary
    temporal_line: str | None = None
    try:
        from menhir.infrastructure.telemetry.store import telemetry_store
        last_accessed = telemetry_store.get_session_last_accessed(session_id)
        telemetry_store.touch_session(session_id, None, None)
        temporal_line = format_temporal_line(last_accessed)
    except Exception as exc:
        print(f"menhir hook: temporal telemetry failed ({type(exc).__name__})", file=sys.stderr)

    # Fast path: flagged memories + open TODOs + TEMPORAL reminders
    flagged = svc.graph_adapter.fetch_flagged_memories(limit=10, workspace=workspace)

    todos: list[dict] = []
    try:
        todos = svc.graph_adapter.list_todos(status="open", limit=5, namespace=workspace)
    except Exception as exc:
        print(f"menhir hook: todo recall failed ({type(exc).__name__})", file=sys.stderr)

    temporal_memories: list[dict] = []
    try:
        temporal_memories = svc.graph_adapter.list_temporal_in_window(
            window_days=30, namespace=workspace
        )
    except Exception as exc:
        print(f"menhir hook: temporal recall failed ({type(exc).__name__})", file=sys.stderr)

    # Context recall — always, no frequency gate
    context_text: str | None = None
    if svc.context_builder is not None:
        try:
            from menhir.domain.recall import QueryPreset

            result = asyncio.run(
                asyncio.wait_for(
                    svc.context_builder.build_context(
                        effective_query,
                        max_tokens=max_tokens,
                        preset=QueryPreset.KNOWLEDGE,
                        namespace=workspace,
                    ),
                    timeout=CONTEXT_TIMEOUT_S,
                )
            )
            context_text = result.context
        except TimeoutError:
            print(
                f"menhir hook: postcompact recall timed out after {CONTEXT_TIMEOUT_S}s, using flagged only",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"menhir hook: postcompact recall failed ({type(exc).__name__})",
                file=sys.stderr,
            )

    output = format_hook_output(flagged, context_text, effective_query, None, temporal_line, todos=todos, temporal_memories=temporal_memories)
    print(wrap_hook_response(output or None), flush=True)


# ---------------------------------------------------------------------------
# Stop event (memory save checkpoint)
# ---------------------------------------------------------------------------

def _run_stop_impl(*, frequency: int) -> None:
    session_id, _ = _parse_stdin()

    # Use a separate counter namespace so stop doesn't interfere with prompt
    counter_key = f"{session_id}__stop"
    if frequency > 0 and not should_run_this_turn(counter_key, frequency):
        print(wrap_hook_response(), flush=True)
        return

    checkpoint = format_save_checkpoint()
    print(wrap_hook_response(checkpoint), flush=True)


# ---------------------------------------------------------------------------
# hook install
# ---------------------------------------------------------------------------

@hook_app.command()
def install(
    location: Annotated[
        str,
        typer.Option(help="Install location: 'user' (~/.claude/) or 'project' (.claude/).")
    ] = "",
    frequency: Annotated[int, typer.Option(help="Recall every N turns.")] = 10,
    save_frequency: Annotated[int, typer.Option(help="Save checkpoint every N turns.")] = 10,
    workspace: Annotated[
        str,
        typer.Option(help="Required explicit workspace key for project hooks; user hooks remain general-only."),
    ] = "",
) -> None:
    """Write menhir hook config to Claude Code settings."""
    if not location:
        location = typer.prompt(
            "Install to [user] ~/.claude/ (all projects) or [project] .claude/ (this project)?",
            default="user",
        )

    try:
        settings_path, commands = install_hooks(
            location=location,
            frequency=frequency,
            save_frequency=save_frequency,
            workspace=workspace,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Installed menhir hooks to {settings_path}")
    typer.echo(f"  Recall:      {commands['recall']}")
    typer.echo(f"  Save check:  {commands['save']}")
    typer.echo(f"  PostCompact: {commands['postcompact']}")


# ---------------------------------------------------------------------------
# hook uninstall
# ---------------------------------------------------------------------------

@hook_app.command()
def uninstall(
    location: Annotated[
        str,
        typer.Option(help="Uninstall location: 'user' or 'project'.")
    ] = "",
) -> None:
    """Remove menhir hook config from Claude Code settings."""
    if not location:
        location = typer.prompt(
            "Uninstall from [user] ~/.claude/ or [project] .claude/?",
            default="user",
        )

    location = location.strip().lower()
    if location not in ("user", "project"):
        typer.echo(f"Invalid location: {location!r}. Use 'user' or 'project'.", err=True)
        raise typer.Exit(1)

    settings_path = _resolve_settings_path(location)

    if not settings_path.exists():
        typer.echo(f"No settings file found at {settings_path}")
        raise typer.Exit(0)

    try:
        existing = json.loads(settings_path.read_text())
    except Exception:
        typer.echo(f"Could not parse {settings_path}", err=True)
        raise typer.Exit(1)

    hooks = existing.get("hooks", {})
    removed = 0

    # Remove from all event types that might contain menhir entries
    for event_key in ("UserPromptSubmit", "Stop", "PostCompact"):
        event_hooks: list = hooks.get(event_key, [])
        original_len = len(event_hooks)
        event_hooks[:] = [
            entry
            for entry in event_hooks
            if not any(
                _HOOK_MARKER in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        removed += original_len - len(event_hooks)
        if not event_hooks:
            hooks.pop(event_key, None)

    if removed == 0:
        typer.echo("No menhir hooks found to remove.")
        raise typer.Exit(0)

    if not hooks:
        existing.pop("hooks", None)

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    typer.echo(f"Removed {removed} menhir hook(s) from {settings_path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upsert_hook_entry(hooks: dict, event_key: str, entry: dict) -> None:
    """Insert or replace a menhir hook entry in the given event key."""
    event_hooks: list = hooks.setdefault(event_key, [])
    if not isinstance(event_hooks, list):
        raise ValueError(f"Expected hooks.{event_key} to be a JSON array")
    for i, existing_entry in enumerate(event_hooks):
        if not isinstance(existing_entry, dict):
            continue
        entry_hooks = existing_entry.get("hooks", [])
        if not isinstance(entry_hooks, list):
            continue
        if any(
            isinstance(hook, dict) and _HOOK_MARKER in hook.get("command", "")
            for hook in entry_hooks
        ):
            event_hooks[i] = entry
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
        settings_path = _resolve_settings_path(location)
    else:
        settings_path = _resolve_settings_path(location, project_dir)

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
        "PostCompact",
        {"hooks": [{"type": "command", "command": commands["postcompact"]}]},
    )

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return settings_path, commands


def _format_hook_command(args: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _resolve_settings_path(location: str, project_dir: Path | None = None) -> Path:
    if location == "user":
        return Path.home() / ".claude" / "settings.json"
    return (project_dir or Path.cwd()) / ".claude" / "settings.local.json"
