"""The session-scoped turn stash: carrying a turn_id from UserPromptSubmit to a later PostToolUse.

WHY A FILE. The two hooks that must agree are separate processes -- one learns the turn_id, a later
one knows a memory was written -- and nothing else spans them. Menhir's own MCP `session_id` is a
derived constant identical across every window (api/auth.py:225 seeds it from user/path/user-agent/
client_id/api_key, none of which is per-conversation), so a server-side join cannot tell conversations
apart. The host's session_id can, and both hooks see it.

THE RISK IS CONFIDENTLY-WRONG PROVENANCE. A stale or cross-session read attaches a memory to words
the user said in some other context. The link is read as "this memory came from that turn", so a
wrong one is a false record nobody has reason to doubt -- worse than no link at all. (The edge is NOT
unforgeable evidence: `/api/turn-evidence` takes `declarant` from the caller. But a link drawn WRONG
is worse than one that is merely trust-limited.) The isolation and staleness tests below are the
primary contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

_COMMON = (Path(__file__).resolve().parents[1]
           / "scripts" / "hooks" / "menhir_turn_evidence_common.py")


def _load():
    spec = importlib.util.spec_from_file_location("menhir_turn_evidence_common", _COMMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_STASH_DIR", str(tmp_path / "stash"))
    return _load()


# ------------------------------------------------------------------- round trip

def test_round_trips_a_turn_id(hook):
    assert hook.stash_turn_id("session-a", "turn-1") is True
    assert hook.read_stashed_turn_id("session-a") == "turn-1"


def test_the_newest_turn_wins(hook):
    """The stash is a 'current turn' pointer, not a log: a memory written now belongs to the turn
    that prompted it, not to the first one of the session."""
    hook.stash_turn_id("session-a", "turn-1")
    hook.stash_turn_id("session-a", "turn-2")
    assert hook.read_stashed_turn_id("session-a") == "turn-2"


def test_an_unknown_session_reads_nothing(hook):
    assert hook.read_stashed_turn_id("never-seen") is None


# --------------------------------------------------------- the contract (confidently-wrong reads)

def test_sessions_do_not_leak_into_each_other(hook):
    """Two conversations open at once must not cross. This is the failure the whole file exists to
    prevent: attaching one conversation's memory to another's words."""
    hook.stash_turn_id("session-a", "turn-a")
    hook.stash_turn_id("session-b", "turn-b")
    assert hook.read_stashed_turn_id("session-a") == "turn-a"
    assert hook.read_stashed_turn_id("session-b") == "turn-b"


def test_a_stale_entry_reports_nothing(hook):
    """Past the age bound the stash must report nothing rather than pointing at whatever the user
    said hours ago. Absent provenance is recoverable; wrong provenance is not."""
    hook.stash_turn_id("session-a", "turn-old")
    path = hook.stash_path("session-a")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["recorded_at"] = time.time() - 7200
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    assert hook.read_stashed_turn_id("session-a") is None
    assert hook.read_stashed_turn_id("session-a", max_age_s=10800) == "turn-old"


def test_a_corrupt_stash_reads_nothing_rather_than_raising(hook):
    """A half-written file must not take down the host agent's tool call."""
    hook.stash_turn_id("session-a", "turn-1")
    Path(hook.stash_path("session-a")).write_text("{not json", encoding="utf-8")
    assert hook.read_stashed_turn_id("session-a") is None


# ------------------------------------------------------------------- path safety

@pytest.mark.parametrize("session_id", [
    "../../escape", "a/b/c", "..\\..\\escape", "a\\b", "..", ".", "/etc/passwd",
])
def test_a_session_id_cannot_escape_the_stash_directory(hook, tmp_path, session_id):
    """session_id arrives from the host's event JSON. Interpolating it straight into a filename would
    let a crafted value write outside the stash directory."""
    path = hook.stash_path(session_id)
    if path is None:
        return
    stash_root = (tmp_path / "stash").resolve()
    assert Path(path).resolve().parent == stash_root


def test_an_empty_session_id_is_refused(hook):
    assert hook.stash_path("") is None
    assert hook.stash_turn_id("", "turn-1") is False
    assert hook.read_stashed_turn_id("") is None


def test_distinct_sessions_get_distinct_files(hook):
    """Sanitization must not collapse two real sessions onto one path -- that would be the
    cross-session leak wearing a different hat."""
    assert hook.stash_path("session-a") != hook.stash_path("session-b")


# ----------------------------------------------------------------- capture-path wiring

def test_capture_stashes_the_returned_turn_id(hook, monkeypatch):
    monkeypatch.setattr(hook, "post_evidence", lambda payload, **kw: "turn-from-server")
    monkeypatch.setattr(hook, "load_stdin_json",
                        lambda: {"prompt": "I have 25 postcards", "session_id": "session-a"})
    assert hook.run_cli([], source_kind="k", source_client="c", hook_version="v",
                        triage_version="t", git_fn=lambda *a, **k: None) == 0
    assert hook.read_stashed_turn_id("session-a") == "turn-from-server"


def test_a_failed_post_stashes_nothing(hook, monkeypatch):
    """No id means no link. It must not fall back to an older turn, which would silently attach the
    memory to the wrong words."""
    hook.stash_turn_id("session-a", "turn-previous")
    monkeypatch.setattr(hook, "post_evidence", lambda payload, **kw: None)
    monkeypatch.setattr(hook, "load_stdin_json",
                        lambda: {"prompt": "I have 25 postcards", "session_id": "session-a"})
    hook.run_cli([], source_kind="k", source_client="c", hook_version="v",
                 triage_version="t", git_fn=lambda *a, **k: None)
    # unchanged -- the previous turn is still there, but nothing NEW was claimed
    assert hook.read_stashed_turn_id("session-a") == "turn-previous"


def test_a_dropped_non_candidate_never_posts_or_stashes(hook, monkeypatch):
    posted = []
    monkeypatch.setattr(hook, "post_evidence", lambda payload, **kw: posted.append(payload))
    monkeypatch.setattr(hook, "load_stdin_json",
                        lambda: {"prompt": "continue", "session_id": "session-a"})
    hook.run_cli([], source_kind="k", source_client="c", hook_version="v",
                 triage_version="t", git_fn=lambda *a, **k: None)
    assert posted == []
    assert hook.read_stashed_turn_id("session-a") is None


def test_the_stash_directory_is_configurable(hook, tmp_path):
    """So a test (or a sandboxed host) never writes into the real ~/.claude/menhir-turns.

    Checks the DEFAULT is not used rather than 'the path is outside home' -- pytest's tmp_path is
    itself under the home directory here, so the latter proves nothing."""
    default = os.path.join(os.path.expanduser("~"), ".claude", "menhir-turns")
    assert str(tmp_path / "stash") in hook.stash_path("session-a")
    assert default not in hook.stash_path("session-a")


def test_the_default_directory_is_used_when_unset(hook, monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_STASH_DIR", raising=False)
    assert os.path.join(".claude", "menhir-turns") in hook.stash_path("session-a")
