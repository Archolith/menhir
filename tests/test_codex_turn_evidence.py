"""Selective `:TurnEvidence` capture for the Codex producer (ADR 0001) -- the third producer feeding
the SAME `/api/turn-evidence` contract as the Claude Code and OpenCode producers.

No live Neo4j and no live server: the producer is exercised as a pure function. Proves the Codex
producer is selective, LLM-free, fail-open, normalises Codex's field aliases, and correctly labels its
provenance. Cross-client triage parity (Claude/OpenCode/Codex) is asserted in test_producer_pack.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "scripts" / "hooks"


def _load(module_name: str, filename: str):
    path = _HOOKS / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_codex():
    return _load("menhir_codex_turn_evidence", "menhir_codex_turn_evidence.py")


_CANDIDATES = [
    "I have 25 movies on my watch list.",
    "I bought one bike for $50 and another for $75.",
    "I no longer use soy sauce because it triggers me.",
    "We decided to use Option B for Turn capture.",
]
_JUNK = ["Can you rewrite this?", "Explain this error.", "refactor the parser", ""]


# ----------------------------------------------------------------------------- triage (acceptance)


@pytest.mark.unit
def test_codex_triage_drops_non_candidate_prompts():
    hook = _load_codex()
    for boring in _JUNK:
        is_cand, reasons = hook.triage_user_prompt(boring)
        assert is_cand is False and reasons == []
        assert hook.build_evidence_payload({"prompt": boring, "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_codex_triage_accepts_durable_examples():
    hook = _load_codex()
    for text in _CANDIDATES:
        is_cand, reasons = hook.triage_user_prompt(text)
        assert is_cand is True and reasons, f"expected candidate for {text!r}"


# ----------------------------------------------------------------------------- field normalisation


@pytest.mark.unit
def test_codex_normalises_field_aliases():
    hook = _load_codex()
    # Codex may send `user_prompt`/`workspace_root` instead of `prompt`/`cwd`.
    norm = hook.normalize({"user_prompt": "I have 25 movies", "workspace_root": "/home/u/menhir",
                           "session_id": "cx-1"})
    assert norm["prompt"] == "I have 25 movies"
    assert norm["cwd"] == "/home/u/menhir"
    assert norm["session_id"] == "cx-1"
    # Primary keys win over aliases when both are present.
    norm2 = hook.normalize({"prompt": "p", "user_prompt": "alias", "cwd": "/c", "workspace_root": "/w"})
    assert norm2["prompt"] == "p" and norm2["cwd"] == "/c"


@pytest.mark.unit
def test_codex_payload_accepts_alias_payload(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_codex()
    payload = hook.build_evidence_payload({
        "user_prompt": "I have 25 movies on my watch list.", "workspace_root": "/home/u/menhir",
        "session_id": "cx-1", "hook_event_name": "UserPromptSubmit"})
    assert payload is not None
    assert payload["text"] == "I have 25 movies on my watch list."
    assert payload["namespace"] == "menhir"


# ----------------------------------------------------------------------------- provenance / payload


@pytest.mark.unit
def test_codex_payload_identifies_source_client(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_codex()
    payload = hook.build_evidence_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "cx-abc",
        "cwd": "/home/u/IdeaProjects/menhir", "prompt": "I have 25 movies on my watch list."})
    assert payload["role"] == "user" and payload["declarant"] == "user"
    assert payload["source_client"] == hook.SOURCE_CLIENT == "codex"
    assert payload["source_kind"] == hook.SOURCE_KIND == "codex_hook"
    assert payload["hook_version"] == hook.HOOK_VERSION == "menhir-codex-turn-evidence-hook-v1"
    assert payload["triage_version"] == hook.TRIAGE_VERSION == "codex-hook-v1"
    assert payload["metadata"]["hook_event_name"] == "UserPromptSubmit"
    assert "number" in payload["triage_reason"]


@pytest.mark.unit
def test_codex_git_provenance_is_fail_open(monkeypatch):
    hook = _load_codex()
    monkeypatch.setattr(hook, "_git", lambda args, cwd: None)
    payload = hook.build_evidence_payload({
        "session_id": "s", "cwd": "/not/a/repo", "prompt": "I bought one bike for $50."})
    assert payload is not None
    assert payload["metadata"]["project_root"] is None
    assert payload["metadata"]["git_branch"] is None and payload["metadata"]["git_commit"] is None
    assert payload["metadata"]["prompt_hash"]


@pytest.mark.unit
def test_codex_junk_prompt_still_dropped_with_provenance(monkeypatch):
    hook = _load_codex()
    monkeypatch.setattr(hook, "_git", lambda args, cwd: "should-not-be-used")
    assert hook.build_evidence_payload({"prompt": "write the handoff", "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_codex_ignores_missing_or_blank_prompt():
    hook = _load_codex()
    assert hook.build_evidence_payload({"session_id": "x"}) is None
    assert hook.build_evidence_payload({"prompt": "   "}) is None
    assert hook.build_evidence_payload({"user_prompt": "   "}) is None


@pytest.mark.unit
def test_codex_does_not_block_when_menhir_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_HOOK_LOG", str(tmp_path / "hook.log"))
    hook = _load_codex()
    ok = hook.post_evidence(
        {"text": "I have 25 things", "namespace": "proj", "session_id": "s",
         "source_client": "codex", "triage_reason": ["number"]},
        url="http://127.0.0.1:9/turn-evidence", timeout=1.0)
    assert ok is None  # post_evidence returns the turn_id now; failure is the absence of one
    entry = json.loads((tmp_path / "hook.log").read_text().strip())
    assert entry["prompt_len"] == len("I have 25 things") and "text" not in entry
    assert entry["source_client"] == "codex"


@pytest.mark.unit
def test_codex_main_is_fail_open_on_malformed_stdin(monkeypatch):
    import io
    hook = _load_codex()
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    monkeypatch.setattr("sys.argv", ["menhir_codex_turn_evidence.py"])
    assert hook.main() == 0
