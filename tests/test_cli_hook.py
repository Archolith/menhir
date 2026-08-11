"""Unit tests for menhir hook CLI: output formatting, turn counter,
pattern detection, stop checkpoint, install/uninstall."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

import pytest

from menhir.cli.output import (
    detect_write_signals,
    format_hook_output,
    format_save_checkpoint,
    format_write_nudge,
    should_run_this_turn,
    wrap_hook_response,
)


# ---------------------------------------------------------------------------
# wrap_hook_response
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrap_hook_response_empty() -> None:
    result = json.loads(wrap_hook_response())
    assert result == {"continue": True}


@pytest.mark.unit
def test_wrap_hook_response_with_context() -> None:
    result = json.loads(wrap_hook_response("hello"))
    assert result == {"continue": True, "additionalContext": "hello"}


@pytest.mark.unit
def test_wrap_hook_response_none_context_omits_key() -> None:
    result = json.loads(wrap_hook_response(None))
    assert "additionalContext" not in result


# ---------------------------------------------------------------------------
# format_hook_output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_hook_output_empty() -> None:
    assert format_hook_output([], None, None) == ""


@pytest.mark.unit
def test_format_hook_output_flagged_only() -> None:
    flagged = [
        {"name": "git", "content": "Always use conventional commits"},
        {"name": "testing", "content": "Run tests before committing"},
    ]
    output = format_hook_output(flagged, None, None)
    assert "## Recalled Memories" in output
    assert "### Pinned (2)" in output
    assert "- git: Always use conventional commits" in output
    assert "- testing: Run tests before committing" in output


@pytest.mark.unit
def test_format_hook_output_skips_episodic() -> None:
    flagged = [
        {"name": "fact", "content": "Keep this", "type": "SEMANTIC"},
        {"name": "ephemeral", "content": "Skip this", "type": "EPISODIC"},
    ]
    output = format_hook_output(flagged, None, None)
    assert "Keep this" in output
    assert "Skip this" not in output


@pytest.mark.unit
def test_format_hook_output_skips_session_scope() -> None:
    flagged = [
        {"name": "fact", "content": "Keep this"},
        {"name": "session", "content": "Skip this", "scope": "SESSION"},
    ]
    output = format_hook_output(flagged, None, None)
    assert "Keep this" in output
    assert "Skip this" not in output


@pytest.mark.unit
def test_format_hook_output_truncates_long_content() -> None:
    flagged = [{"name": "verbose", "content": "x" * 200}]
    output = format_hook_output(flagged, None, None)
    assert "x" * 120 + "..." in output
    assert "x" * 121 not in output


@pytest.mark.unit
def test_format_hook_output_context_section() -> None:
    context = "- [0.82] cth.mcp.memory: Graph-based memory system"
    output = format_hook_output([], context, "memory system")
    assert '### Context (query="memory system")' in output
    assert "Graph-based memory system" in output


@pytest.mark.unit
def test_format_hook_output_both_sections() -> None:
    flagged = [{"name": "rule", "content": "Important rule"}]
    context = "- [0.9] Some context"
    output = format_hook_output(flagged, context, "query")
    assert "### Pinned (1)" in output
    assert '### Context (query="query")' in output


@pytest.mark.unit
def test_format_hook_output_empty_content_skipped() -> None:
    flagged = [{"name": "empty", "content": ""}]
    output = format_hook_output(flagged, None, None)
    assert output == ""


@pytest.mark.unit
def test_format_hook_output_summary_fallback() -> None:
    flagged = [{"name": "note", "summary": "From summary field"}]
    output = format_hook_output(flagged, None, None)
    assert "From summary field" in output


@pytest.mark.unit
def test_format_hook_output_with_write_nudge() -> None:
    flagged = [{"name": "rule", "content": "Some rule"}]
    nudge = "## Memory Write Reminder\n- Store this feedback."
    output = format_hook_output(flagged, None, None, write_nudge=nudge)
    assert "### Pinned (1)" in output
    assert "Memory Write Reminder" in output
    assert "Store this feedback" in output


@pytest.mark.unit
def test_format_hook_output_write_nudge_only() -> None:
    nudge = "## Memory Write Reminder\n- Store this."
    output = format_hook_output([], None, None, write_nudge=nudge)
    assert "## Recalled Memories" in output
    assert "Memory Write Reminder" in output


# ---------------------------------------------------------------------------
# detect_write_signals
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detect_write_signals_correction() -> None:
    assert "correction" in detect_write_signals("don't use that approach")
    assert "correction" in detect_write_signals("stop doing that")
    assert "correction" in detect_write_signals("no, not like that")
    assert "correction" in detect_write_signals("that's not right")
    assert "correction" in detect_write_signals("you shouldn't mock the database")


@pytest.mark.unit
def test_detect_write_signals_confirmation() -> None:
    assert "confirmation" in detect_write_signals("perfect, that's exactly what I wanted")
    assert "confirmation" in detect_write_signals("keep doing it that way")
    assert "confirmation" in detect_write_signals("good call on the bundled PR")
    assert "confirmation" in detect_write_signals("that's correct")


@pytest.mark.unit
def test_detect_write_signals_explicit() -> None:
    assert "explicit" in detect_write_signals("remember that we use pytest")
    assert "explicit" in detect_write_signals("note that the API changed")
    assert "explicit" in detect_write_signals("important: always run lint first")
    assert "explicit" in detect_write_signals("from now on use snake_case")


@pytest.mark.unit
def test_detect_write_signals_decision() -> None:
    assert "decision" in detect_write_signals("let's go with the Typer approach")
    assert "decision" in detect_write_signals("we decided to use Neo4j")
    assert "decision" in detect_write_signals("the plan is to ship by Friday")
    assert "decision" in detect_write_signals("we're going with option B")


@pytest.mark.unit
def test_detect_write_signals_multiple() -> None:
    signals = detect_write_signals("don't do that, remember to always use fixtures")
    assert "correction" in signals
    assert "explicit" in signals


@pytest.mark.unit
def test_detect_write_signals_empty() -> None:
    assert detect_write_signals("") == []
    assert detect_write_signals("hi") == []
    assert detect_write_signals("fix the bug in main.py") == []


@pytest.mark.unit
def test_detect_write_signals_case_insensitive() -> None:
    assert "correction" in detect_write_signals("DON'T do that")
    assert "explicit" in detect_write_signals("REMEMBER that we use pytest")


# ---------------------------------------------------------------------------
# format_write_nudge
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_write_nudge_correction() -> None:
    nudge = format_write_nudge(["correction"])
    assert "Memory Write Reminder" in nudge
    assert "corrected your approach" in nudge
    assert "mcp__memory__add_memory" in nudge


@pytest.mark.unit
def test_format_write_nudge_multiple() -> None:
    nudge = format_write_nudge(["correction", "explicit"])
    assert nudge.count("- ") == 2


@pytest.mark.unit
def test_format_write_nudge_empty() -> None:
    assert format_write_nudge([]) == ""


# ---------------------------------------------------------------------------
# format_save_checkpoint (Stop hook)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_save_checkpoint_content() -> None:
    checkpoint = format_save_checkpoint()
    assert "Memory Checkpoint" in checkpoint
    assert "Corrections/feedback" in checkpoint
    assert "Decisions made" in checkpoint
    assert "mcp__memory__add_memory" in checkpoint


# ---------------------------------------------------------------------------
# should_run_this_turn (turn counter)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_turn_counter_first_turn_always_runs(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    assert should_run_this_turn("sess1", 5, counter) is True


@pytest.mark.unit
def test_turn_counter_frequency_gate(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    results = []
    for _ in range(10):
        results.append(should_run_this_turn("sess1", 3, counter))
    # Should run on turns 0, 3, 6, 9
    assert results == [True, False, False, True, False, False, True, False, False, True]


@pytest.mark.unit
def test_turn_counter_independent_sessions(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    assert should_run_this_turn("sess_a", 5, counter) is True
    assert should_run_this_turn("sess_b", 5, counter) is True
    assert should_run_this_turn("sess_a", 5, counter) is False
    assert should_run_this_turn("sess_b", 5, counter) is False


@pytest.mark.unit
def test_turn_counter_frequency_zero_always_runs(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    for _ in range(5):
        assert should_run_this_turn("sess1", 0, counter) is True


@pytest.mark.unit
def test_turn_counter_prunes_stale_sessions(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    stale_data = {
        "stale_session": {"count": 50, "ts": time.time() - 100_000},
        "fresh_session": {"count": 3, "ts": time.time()},
    }
    counter.write_text(json.dumps(stale_data))
    should_run_this_turn("new_session", 5, counter)
    data = json.loads(counter.read_text())
    assert "stale_session" not in data
    assert "fresh_session" in data
    assert "new_session" in data


@pytest.mark.unit
def test_turn_counter_handles_corrupted_file(tmp_path: Path) -> None:
    counter = tmp_path / "counter.json"
    counter.write_text("not valid json!!!")
    assert should_run_this_turn("sess1", 5, counter) is True


@pytest.mark.unit
def test_turn_counter_creates_parent_dirs(tmp_path: Path) -> None:
    counter = tmp_path / "nested" / "dir" / "counter.json"
    assert should_run_this_turn("sess1", 5, counter) is True
    assert counter.exists()


@pytest.mark.unit
def test_turn_counter_stop_namespace_independent(tmp_path: Path) -> None:
    """Stop counter (session__stop) doesn't interfere with prompt counter."""
    counter = tmp_path / "counter.json"
    # Prompt counter: turn 0 → runs
    assert should_run_this_turn("sess1", 5, counter) is True
    # Stop counter: turn 0 → also runs (different key)
    assert should_run_this_turn("sess1__stop", 5, counter) is True
    # Prompt counter: turn 1 → gated
    assert should_run_this_turn("sess1", 5, counter) is False
    # Stop counter: turn 1 → also gated
    assert should_run_this_turn("sess1__stop", 5, counter) is False


# ---------------------------------------------------------------------------
# _parse_stdin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_stdin_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli.hook import _parse_stdin

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert _parse_stdin() == ("unknown", "")


@pytest.mark.unit
def test_parse_stdin_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from menhir.cli.hook import _parse_stdin

    mock_stdin = io.StringIO(json.dumps({"session_id": "sess-123", "prompt": "hello"}))
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert _parse_stdin() == ("sess-123", "hello")


@pytest.mark.unit
def test_parse_stdin_partial_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from menhir.cli.hook import _parse_stdin

    mock_stdin = io.StringIO(json.dumps({"session_id": "sess-123"}))
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _parse_stdin() == ("sess-123", "")

    mock_stdin = io.StringIO(json.dumps({"prompt": "recalled"}))
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _parse_stdin() == ("unknown", "recalled")


@pytest.mark.unit
def test_parse_stdin_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from menhir.cli.hook import _parse_stdin

    mock_stdin = io.StringIO("not valid json")
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert _parse_stdin() == ("unknown", "")


@pytest.mark.unit
def test_parse_stdin_empty_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from menhir.cli.hook import _parse_stdin

    mock_stdin = io.StringIO("{}")
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert _parse_stdin() == ("unknown", "")


# ---------------------------------------------------------------------------
# hook install / uninstall config merging
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_creates_both_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["install", "--location", "user", "--frequency", "3", "--save-frequency", "7"])
    assert result.exit_code == 0

    config = json.loads(settings_file.read_text())

    # UserPromptSubmit hook
    prompt_hooks = config["hooks"]["UserPromptSubmit"]
    assert len(prompt_hooks) == 1
    assert "menhir.cli" in prompt_hooks[0]["hooks"][0]["command"]
    assert "--frequency 3" in prompt_hooks[0]["hooks"][0]["command"]

    # Stop hook
    stop_hooks = config["hooks"]["Stop"]
    assert len(stop_hooks) == 1
    assert "--event stop" in stop_hooks[0]["hooks"][0]["command"]
    assert "--frequency 7" in stop_hooks[0]["hooks"][0]["command"]


@pytest.mark.unit
def test_project_install_persists_explicit_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module
    from menhir.cli.hook import hook_app
    from typer.testing import CliRunner

    settings_file = tmp_path / "settings.local.json"
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    result = CliRunner().invoke(
        hook_app,
        ["install", "--location", "project", "--workspace", " Project Alpha "],
    )

    assert result.exit_code == 0
    config = json.loads(settings_file.read_text())
    prompt_command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    compact_command = config["hooks"]["PostCompact"][0]["hooks"][0]["command"]
    assert shlex.split(prompt_command)[-2:] == ["--workspace", "project alpha"]
    assert shlex.split(compact_command)[-2:] == ["--workspace", "project alpha"]


@pytest.mark.unit
def test_project_install_requires_explicit_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module
    from menhir.cli.hook import hook_app
    from typer.testing import CliRunner

    settings_file = tmp_path / "settings.local.json"
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    result = CliRunner().invoke(hook_app, ["install", "--location", "project"])

    assert result.exit_code == 1
    assert "requires --workspace" in result.output

    blank = CliRunner().invoke(
        hook_app, ["install", "--location", "project", "--workspace", "   "]
    )
    assert blank.exit_code == 1
    assert "non-empty --workspace" in blank.output


@pytest.mark.unit
def test_install_replaces_existing_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "python -m menhir.cli hook run --frequency 5"}
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "python -m menhir.cli hook run --event stop --frequency 5"}
                    ]
                }
            ],
        }
    }
    settings_file.write_text(json.dumps(existing))
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["install", "--location", "user", "--frequency", "10"])
    assert result.exit_code == 0

    config = json.loads(settings_file.read_text())
    assert len(config["hooks"]["UserPromptSubmit"]) == 1
    assert "--frequency 10" in config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert len(config["hooks"]["Stop"]) == 1


@pytest.mark.unit
def test_install_preserves_other_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "some-other-hook"}]}
            ]
        }
    }
    settings_file.write_text(json.dumps(existing))
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["install", "--location", "user", "--frequency", "5"])
    assert result.exit_code == 0

    config = json.loads(settings_file.read_text())
    hooks = config["hooks"]["UserPromptSubmit"]
    assert len(hooks) == 2
    commands = [h["hooks"][0]["command"] for h in hooks]
    assert "some-other-hook" in commands


@pytest.mark.unit
def test_uninstall_removes_both_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "python -m menhir.cli hook run --frequency 5"}
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "python -m menhir.cli hook run --event stop --frequency 10"}
                    ]
                }
            ],
        }
    }
    settings_file.write_text(json.dumps(existing))
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["uninstall", "--location", "user"])
    assert result.exit_code == 0
    assert "2 menhir hook(s)" in result.output

    config = json.loads(settings_file.read_text())
    assert "hooks" not in config


@pytest.mark.unit
def test_uninstall_preserves_other_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "some-other-hook"}]},
                {
                    "hooks": [
                        {"type": "command", "command": "python -m menhir.cli hook run --frequency 5"}
                    ]
                },
            ]
        }
    }
    settings_file.write_text(json.dumps(existing))
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["uninstall", "--location", "user"])
    assert result.exit_code == 0

    config = json.loads(settings_file.read_text())
    hooks = config["hooks"]["UserPromptSubmit"]
    assert len(hooks) == 1
    assert hooks[0]["hooks"][0]["command"] == "some-other-hook"


@pytest.mark.unit
def test_uninstall_no_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.cli import hook as hook_module

    settings_file = tmp_path / "settings.local.json"
    monkeypatch.setattr(hook_module, "_resolve_settings_path", lambda loc: settings_file)

    from typer.testing import CliRunner
    from menhir.cli.hook import hook_app

    runner = CliRunner()
    result = runner.invoke(hook_app, ["uninstall", "--location", "user"])
    assert result.exit_code == 0
    assert "No settings file" in result.output
