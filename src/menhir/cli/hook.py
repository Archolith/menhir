"""Hook subcommand group: run, install, uninstall."""

from __future__ import annotations

import asyncio
import json
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
    except Exception:
        # Never crash in hook mode — let Claude Code proceed
        print(wrap_hook_response(), flush=True)


def _parse_stdin() -> tuple[str, str]:
    """Parse session_id and prompt from stdin JSON. Returns (session_id, prompt)."""
    session_id = "unknown"
    prompt = ""
    if not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            session_id = hook_input.get("session_id", session_id)
            prompt = hook_input.get("prompt", "")
        except Exception:
            pass
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
    except Exception:
        pass

    if recall_gated:
        print(wrap_hook_response(write_nudge), flush=True)
        return

    from menhir.cli.bootstrap import build_hook_services
    svc = build_hook_services(settings)

    # Fast path: flagged memories + open TODOs + TEMPORAL reminders (always)
    flagged = svc.graph_adapter.fetch_flagged_memories(limit=10, workspace=workspace)

    todos: list[dict] = []
    try:
        todos = svc.graph_adapter.list_todos(status="open", limit=5)
    except Exception:
        pass

    temporal_memories: list[dict] = []
    try:
        temporal_memories = svc.graph_adapter.list_temporal_in_window(window_days=30)
    except Exception:
        pass

    # Full path: context recall (if prompt is long enough and services available)
    context_text: str | None = None
    effective_query = prompt.strip() if len(prompt.strip()) >= MIN_PROMPT_LEN else None
    if effective_query and svc.context_builder is not None:
        try:
            from menhir.domain.recall import QueryPreset

            result = asyncio.run(
                svc.context_builder.build_context(
                    effective_query,
                    max_tokens=max_tokens,
                    preset=QueryPreset.KNOWLEDGE,
                    namespace=workspace,
                )
            )
            context_text = result.context
        except Exception:
            print("menhir hook: context recall failed, using flagged only", file=sys.stderr)

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
        except Exception:
            pass

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
    except Exception:
        pass

    # Fast path: flagged memories + open TODOs + TEMPORAL reminders
    flagged = svc.graph_adapter.fetch_flagged_memories(limit=10, workspace=workspace)

    todos: list[dict] = []
    try:
        todos = svc.graph_adapter.list_todos(status="open", limit=5)
    except Exception:
        pass

    temporal_memories: list[dict] = []
    try:
        temporal_memories = svc.graph_adapter.list_temporal_in_window(window_days=30)
    except Exception:
        pass

    # Context recall — always, no frequency gate
    context_text: str | None = None
    if svc.context_builder is not None:
        try:
            from menhir.domain.recall import QueryPreset

            result = asyncio.run(
                svc.context_builder.build_context(
                    effective_query,
                    max_tokens=max_tokens,
                    preset=QueryPreset.KNOWLEDGE,
                    namespace=workspace,
                )
            )
            context_text = result.context
        except Exception:
            print("menhir hook: postcompact recall failed", file=sys.stderr)

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

    location = location.strip().lower()
    if location not in ("user", "project"):
        typer.echo(f"Invalid location: {location!r}. Use 'user' or 'project'.", err=True)
        raise typer.Exit(1)

    from menhir.domain.bootstrap_scope import normalize_workspace_key

    if location == "project" and not workspace:
        typer.echo("Project hook installation requires --workspace.", err=True)
        raise typer.Exit(1)
    if location == "user" and workspace:
        typer.echo("User hooks are general-only; omit --workspace.", err=True)
        raise typer.Exit(1)
    workspace = normalize_workspace_key(workspace) if workspace else ""
    if location == "project" and not workspace:
        typer.echo("Project hook installation requires a non-empty --workspace.", err=True)
        raise typer.Exit(1)

    settings_path = _resolve_settings_path(location)

    # Detect the Python executable — prefer the venv that has menhir installed
    python_exe = sys.executable
    venv_python = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin") / Path(sys.executable).name
    if venv_python.exists():
        python_exe = str(venv_python)

    workspace_arg = f" --workspace {json.dumps(workspace)}" if workspace else ""
    recall_cmd = f"{python_exe} -m menhir.cli hook run --frequency {frequency}{workspace_arg}"
    save_cmd = f"{python_exe} -m menhir.cli hook run --event stop --frequency {save_frequency}"
    postcompact_cmd = f"{python_exe} -m menhir.cli hook run --event postcompact{workspace_arg}"

    # Read existing settings
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            existing = {}

    hooks = existing.setdefault("hooks", {})

    # --- UserPromptSubmit (recall + write nudges) ---
    recall_entry = {
        "hooks": [{"type": "command", "command": recall_cmd}]
    }
    _upsert_hook_entry(hooks, "UserPromptSubmit", recall_entry)

    # --- Stop (save checkpoint) ---
    save_entry = {
        "hooks": [{"type": "command", "command": save_cmd}]
    }
    _upsert_hook_entry(hooks, "Stop", save_entry)

    # --- PostCompact (forced recall into fresh context after compaction) ---
    postcompact_entry = {
        "hooks": [{"type": "command", "command": postcompact_cmd}]
    }
    _upsert_hook_entry(hooks, "PostCompact", postcompact_entry)

    # Write
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")

    typer.echo(f"Installed menhir hooks to {settings_path}")
    typer.echo(f"  Recall:     {recall_cmd}")
    typer.echo(f"  Save check: {save_cmd}")
    typer.echo(f"  PostCompact: {postcompact_cmd}")


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
    for i, existing_entry in enumerate(event_hooks):
        entry_hooks = existing_entry.get("hooks", [])
        if any(_HOOK_MARKER in h.get("command", "") for h in entry_hooks):
            event_hooks[i] = entry
            return
    event_hooks.append(entry)


def _resolve_settings_path(location: str) -> Path:
    if location == "user":
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"
