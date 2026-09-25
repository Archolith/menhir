"""Hook subcommand group: run, install, uninstall."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated

import typer

from menhir.cli.output import (
    HOOK_EVENT_NAMES,
    DEFAULT_HOOK_TOKEN_BUDGET,
    REMINDER_LIMIT,
    detect_write_signals,
    format_hook_output,
    format_save_checkpoint,
    format_temporal_line,
    format_write_nudge,
    should_run_this_turn,
    wrap_hook_response,
)

# Settings-file machinery moved to the hook_install/hook_events siblings; these re-exports
# keep every existing menhir.cli.hook import site and monkeypatch target working unchanged.
from menhir.cli.hook_events import _load_env, _parse_stdin
from menhir.cli.hook_install import (
    _HOOK_MARKER,
    _entry_has_menhir_hook,
    _format_hook_command,
    _remove_managed_hook_entries,
    _resolve_settings_path,
    _strip_menhir_hook_commands,
    _upsert_hook_entry,
    install_hooks,
)

hook_app = typer.Typer(name="hook", help="Claude Code hook integration.")

MIN_PROMPT_LEN = 15
CONTEXT_TIMEOUT_S: float = 8.0


# ---------------------------------------------------------------------------
# hook run
# ---------------------------------------------------------------------------

@hook_app.command()
def run(
    query: Annotated[str, typer.Option(help="Override recall query.")] = "",
    max_tokens: Annotated[
        int,
        typer.Option(help="Token budget for the whole injected block, context recall included."),
    ] = DEFAULT_HOOK_TOKEN_BUDGET,
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
        print(
            wrap_hook_response(
                degraded=f"unexpected {type(exc).__name__}",
                event=HOOK_EVENT_NAMES.get(event, "UserPromptSubmit"),
            ),
            flush=True,
        )


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
        print(wrap_hook_response(write_nudge, event="Stop"), flush=True)
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
            window_days=30, namespace=workspace, limit=REMINDER_LIMIT
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

    output = format_hook_output(
        flagged, context_text, effective_query, write_nudge, temporal_line,
        todos=todos, temporal_memories=temporal_memories, max_tokens=max_tokens,
    )
    print(wrap_hook_response(output or None, event="UserPromptSubmit"), flush=True)


# ---------------------------------------------------------------------------
# PostCompact event (forced recall into fresh context — no frequency gate)
# ---------------------------------------------------------------------------

def _run_postcompact_impl(*, max_tokens: int, workspace: str | None = None) -> None:
    """Post-compaction recall, delivered on SessionStart.

    Registered on SessionStart rather than PostCompact: the harness has no context channel on
    the compaction event, but SessionStart fires immediately afterwards with source="compact".
    SessionStart also fires on startup/resume/clear, so the source is checked to keep the
    behaviour exactly what it was -- recall after compaction only.
    """
    session_id = "unknown"
    compact_summary = ""
    source = "compact"
    if not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            session_id = hook_input.get("session_id", session_id)
            compact_summary = hook_input.get("compact_summary", "")
            source = str(hook_input.get("source") or "compact")
        except Exception as exc:
            print(f"menhir hook: stdin parse failed ({type(exc).__name__})", file=sys.stderr)

    if source != "compact":
        print(wrap_hook_response(event="SessionStart"), flush=True)
        return

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
            window_days=30, namespace=workspace, limit=REMINDER_LIMIT
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

    output = format_hook_output(
        flagged, context_text, effective_query, None, temporal_line,
        todos=todos, temporal_memories=temporal_memories, max_tokens=max_tokens,
    )
    print(wrap_hook_response(output or None, event="SessionStart"), flush=True)


# ---------------------------------------------------------------------------
# Stop event (memory save checkpoint)
# ---------------------------------------------------------------------------

def _run_stop_impl(*, frequency: int) -> None:
    session_id, _ = _parse_stdin()

    # Use a separate counter namespace so stop doesn't interfere with prompt
    counter_key = f"{session_id}__stop"
    if frequency > 0 and not should_run_this_turn(counter_key, frequency):
        print(wrap_hook_response(event="Stop"), flush=True)
        return

    checkpoint = format_save_checkpoint()
    print(wrap_hook_response(checkpoint, event="Stop"), flush=True)


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
    typer.echo(f"  SessionStart (post-compaction): {commands['postcompact']}")


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
    kept_third_party = 0

    # Remove from all event types that might contain menhir entries
    if not isinstance(hooks, dict):
        hooks = {}
    # PostCompact stays in this sweep: installs predating the SessionStart move left an
    # entry there, and an uninstall that skips it would strand a hook that still runs.
    for event_key in ("UserPromptSubmit", "Stop", "SessionStart", "PostCompact"):
        event_hooks: list = hooks.get(event_key, [])
        if not isinstance(event_hooks, list):
            continue
        surviving: list = []
        for entry in event_hooks:
            entry_removed, entry_kept = _strip_menhir_hook_commands(entry)
            removed += entry_removed
            # Only commands preserved OUT of a menhir entry count as co-registered;
            # entries that never held a menhir command were never at risk (#113 P3).
            if entry_removed:
                kept_third_party += entry_kept
            # Drop the entry only when menhir commands were removed and nothing
            # else remains. Entries that never held a menhir command (including
            # malformed ones) pass through untouched.
            if entry_kept or not entry_removed:
                surviving.append(entry)
        event_hooks[:] = surviving
        if not event_hooks:
            hooks.pop(event_key, None)

    if removed == 0:
        typer.echo("No menhir hooks found to remove.")
        raise typer.Exit(0)

    if not hooks:
        existing.pop("hooks", None)

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    typer.echo(f"Removed {removed} menhir hook(s) from {settings_path}")
    if kept_third_party:
        typer.echo(f"Kept {kept_third_party} third-party hook(s) co-registered in menhir entries")
