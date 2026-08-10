"""The PostToolUse admission hook: pair a written memory with the turn that prompted it.

THE PRIMARY CONTRACT IS SILENCE. Most add_memory calls have NO stashed turn -- the prompt was a
non-candidate, or the session predates the hook -- and a link must simply not be drawn. A hook that
guesses would attach memories to unrelated words, which is worse than no provenance at all, because
the whole point of this edge is that it is not something anyone asserted.

The second contract is that nothing here can break the host's tool call.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "scripts" / "hooks"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """The admission hook, with a stash helper attached.

    `stash_turn_id` lives in the shared producer core and the admission hook does not re-export it
    (it only reads). Tests stash through the core, which is exactly how the real UserPromptSubmit
    hook does it -- so these tests exercise the genuine cross-process handoff, not a shortcut.
    """
    monkeypatch.setenv("MENHIR_TURN_STASH_DIR", str(tmp_path / "stash"))
    monkeypatch.delenv("MENHIR_ADMISSION_URL", raising=False)
    mod = _load("menhir_memory_admission")
    mod.stash_turn_id = _load("menhir_turn_evidence_common").stash_turn_id
    return mod


def _event(**overrides):
    ev = {
        "tool_name": "mcp__memory__add_memory",
        "session_id": "session-a",
        "tool_response": "Queued. episode_id=a843bc52-f2a2-464d-8a70-e180c47732a0, state=PENDING",
    }
    ev.update(overrides)
    return ev


# ------------------------------------------------------------------ nothing to link (the default)

def test_no_stashed_turn_means_no_link(hook):
    """The common case, and it must be silent: a non-candidate prompt captured no turn."""
    assert hook.build_admission(_event()) is None


def test_a_different_tool_is_ignored(hook):
    hook.stash_turn_id("session-a", "turn-1")
    assert hook.build_admission(_event(tool_name="Bash")) is None
    assert hook.build_admission(_event(tool_name="mcp__memory__recall_memories")) is None


def test_a_response_without_an_episode_id_links_nothing(hook):
    hook.stash_turn_id("session-a", "turn-1")
    assert hook.build_admission(_event(tool_response="Error: backend unavailable")) is None
    assert hook.build_admission(_event(tool_response=None)) is None


def test_another_sessions_turn_is_never_borrowed(hook):
    """The failure this hook exists to avoid: linking one conversation's memory to another's words."""
    hook.stash_turn_id("session-b", "turn-b")
    assert hook.build_admission(_event(session_id="session-a")) is None


# ------------------------------------------------------------------------------- the happy path

def test_pairs_the_episode_with_the_stashed_turn(hook):
    hook.stash_turn_id("session-a", "turn-1")
    assert hook.build_admission(_event()) == {
        "episode_uuid": "a843bc52-f2a2-464d-8a70-e180c47732a0",
        "turn_evidence_uuid": "turn-1",
    }


@pytest.mark.parametrize("tool_name", [
    "add_memory", "mcp__memory__add_memory",
    "add_memory_and_track", "mcp__memory__add_memory_and_track",
])
def test_accepts_bare_and_mcp_qualified_tool_names(hook, tool_name):
    hook.stash_turn_id("session-a", "turn-1")
    assert hook.build_admission(_event(tool_name=tool_name)) is not None


# -------------------------------------------------------------- tool_response shape tolerance
#
# Hosts report tool results in different shapes and change them between versions. A shape change
# must cost the LINK, never raise inside the host's tool path.

@pytest.mark.parametrize("response", [
    "Queued. episode_id=abc12345-0000-0000-0000-000000000000, state=PENDING",
    {"content": "Queued. episode_id=abc12345-0000-0000-0000-000000000000"},
    [{"type": "text", "text": "episode_id=abc12345-0000-0000-0000-000000000000"}],
    {"result": {"episode_id": "abc12345-0000-0000-0000-000000000000"}},
    'episode_id="abc12345-0000-0000-0000-000000000000"',
])
def test_finds_the_episode_id_in_any_reported_shape(hook, response):
    assert hook.extract_episode_id(response) == "abc12345-0000-0000-0000-000000000000"


@pytest.mark.parametrize("response", [None, "", [], {}, 12345, {"unrelated": "value"}])
def test_an_unrecognizable_response_yields_no_id_rather_than_raising(hook, response):
    assert hook.extract_episode_id(response) is None


# ------------------------------------------------------------------------- never block the host

def test_a_malformed_event_exits_zero(hook, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin("{not json"))
    assert hook.main([]) == 0
    assert capsys.readouterr().out == ""


def test_an_unreachable_menhir_exits_zero(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_HOOK_LOG", str(tmp_path / "hook.log"))
    hook.stash_turn_id("session-a", "turn-1")
    assert hook.post_admission(
        {"episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1"},
        url="http://127.0.0.1:9/episode-admission", timeout=1.0) is False


def test_the_normal_path_prints_nothing(hook, monkeypatch, capsys):
    """PostToolUse stdout can reach the agent; this hook must stay invisible."""
    hook.stash_turn_id("session-a", "turn-1")
    monkeypatch.setattr(hook, "post_admission", lambda payload, **kw: True)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_event())))
    assert hook.main([]) == 0
    assert capsys.readouterr().out == ""


def test_disabled_by_env_posts_nothing(hook, monkeypatch):
    posted = []
    monkeypatch.setenv("MENHIR_TURN_EVIDENCE_ENABLED", "0")
    hook.stash_turn_id("session-a", "turn-1")
    monkeypatch.setattr(hook, "post_admission", lambda payload, **kw: posted.append(payload))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_event())))
    assert hook.main([]) == 0
    assert posted == []


# ------------------------------------------------------------------------------- ergonomics

def test_dry_run_reports_without_posting(hook, monkeypatch, capsys):
    posted = []
    hook.stash_turn_id("session-a", "turn-1")
    monkeypatch.setattr(hook, "post_admission", lambda payload, **kw: posted.append(payload))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(_event())))
    assert hook.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would_link: true" in out and "turn-1" in out
    assert posted == []


def test_health_never_prints_the_key(hook, monkeypatch, capsys):
    monkeypatch.setenv("MENHIR_AGENT_KEY", "super-secret-value")
    assert hook.main(["--health"]) == 0
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    assert "api_key_configured: yes" in out


def test_the_admission_url_is_derived_from_the_turns_url(hook, monkeypatch):
    """One host setting, not two -- a deployment that moved menhir configures it once."""
    monkeypatch.setenv("MENHIR_TURNS_URL", "http://example.invalid:9999/api/turn-evidence")
    assert hook.admission_url() == "http://example.invalid:9999/api/episode-admission"


def test_an_explicit_admission_url_wins(hook, monkeypatch):
    monkeypatch.setenv("MENHIR_TURNS_URL", "http://example.invalid:9999/api/turn-evidence")
    monkeypatch.setenv("MENHIR_ADMISSION_URL", "http://elsewhere.invalid/custom")
    assert hook.admission_url() == "http://elsewhere.invalid/custom"


class _Stdin:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload
