"""Selective `:TurnEvidence` capture for the OpenCode producer (ADR 0001) -- the second producer
feeding the SAME `/api/turn-evidence` contract as the Claude Code hook.

No live Neo4j and no live server: the producer is exercised as a pure function. These tests prove the
OpenCode producer is selective, LLM-free, fail-open, correctly labels its provenance, and -- via a
cross-client parity test -- that its triage stays byte-for-byte identical to the Claude hook's triage
so the two producers can never silently diverge on what counts as durable evidence.
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


def _load_opencode():
    return _load("menhir_opencode_turn_evidence", "menhir_opencode_turn_evidence.py")


def _load_claude():
    return _load("menhir_turn_evidence", "menhir_turn_evidence.py")


# Shared corpus reused by the parity test and the acceptance tests. Mix of durable-evidence
# candidates and boring instructions that must be dropped.
_CANDIDATES = [
    "I have 25 movies on my watch list.",
    "I bought one bike for $50 and another for $75.",
    "I no longer use soy sauce because it triggers me.",
    "We decided to use Option B for Turn capture.",
    "The current threshold is 0.82.",
    "Actually it is 20, not 25.",
    "Remember that my API key rotates every Monday.",
    "I prefer dark mode and I use Postgres.",
]
_JUNK = [
    "Can you rewrite this?",
    "Explain this error.",
    "What do you think?",
    "Make this shorter.",
    "Write the handoff.",
    "Continue.",
    "refactor the parser",
    "",
]


# ----------------------------------------------------------------------------- triage (acceptance)


@pytest.mark.unit
def test_opencode_triage_drops_non_candidate_prompts():
    hook = _load_opencode()
    for boring in _JUNK:
        is_cand, reasons = hook.triage_user_prompt(boring)
        assert is_cand is False and reasons == []
        assert hook.build_evidence_payload({"prompt": boring, "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_opencode_triage_accepts_durable_examples():
    hook = _load_opencode()
    for text in _CANDIDATES:
        is_cand, reasons = hook.triage_user_prompt(text)
        assert is_cand is True and reasons, f"expected candidate for {text!r}"
    # spot-check reason labels match the semantics the Claude producer captures
    assert "number" in hook.triage_user_prompt("I have 25 movies on my watch list.")[1]
    assert "cessation" in hook.triage_user_prompt("I no longer use soy sauce because it triggers me.")[1]
    assert "decision" in hook.triage_user_prompt("We decided to use Option B for Turn capture.")[1]


@pytest.mark.unit
def test_opencode_triage_is_deterministic_and_llm_free():
    hook = _load_opencode()
    r1 = hook.triage_user_prompt("The current threshold is 0.82.")
    r2 = hook.triage_user_prompt("The current threshold is 0.82.")
    assert r1 == r2 and r1[0] is True
    assert not hasattr(hook, "openai") and not hasattr(hook, "OpenAI")


# ----------------------------------------------------------------------------- provenance / payload


@pytest.mark.unit
def test_opencode_payload_identifies_source_client(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_opencode()
    payload = hook.build_evidence_payload({
        "hook_event_name": "chat.message", "session_id": "oc-abc",
        "cwd": "/home/u/IdeaProjects/menhir", "prompt": "I have 25 movies on my watch list.",
    })
    assert payload["role"] == "user" and payload["declarant"] == "user"
    assert payload["source_client"] == hook.SOURCE_CLIENT == "opencode"
    assert payload["source_kind"] == hook.SOURCE_KIND == "opencode_hook"
    assert payload["hook_version"] == hook.HOOK_VERSION == "menhir-opencode-turn-evidence-hook-v1"
    assert payload["triage_version"] == hook.TRIAGE_VERSION == "opencode-hook-v1"
    assert payload["namespace"] == "menhir"
    assert "number" in payload["triage_reason"]


@pytest.mark.unit
def test_opencode_payload_carries_provenance(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_opencode()
    text = "I have 25 movies on my watch list."
    payload = hook.build_evidence_payload({
        "session_id": "oc-abc", "cwd": "/home/u/IdeaProjects/menhir", "prompt": text})
    assert payload["metadata"]["prompt_hash"] == hook._prompt_hash(text)
    assert payload["metadata"]["hook_event_name"] == "chat.message"
    for key in ("project_root", "git_branch", "git_commit"):
        assert key in payload["metadata"]


@pytest.mark.unit
def test_opencode_git_provenance_is_fail_open(monkeypatch):
    hook = _load_opencode()
    monkeypatch.setattr(hook, "_git", lambda args, cwd: None)
    payload = hook.build_evidence_payload({
        "session_id": "s", "cwd": "/not/a/repo", "prompt": "I bought one bike for $50."})
    assert payload is not None
    assert payload["metadata"]["project_root"] is None
    assert payload["metadata"]["git_branch"] is None and payload["metadata"]["git_commit"] is None
    assert payload["metadata"]["prompt_hash"]


@pytest.mark.unit
def test_opencode_junk_prompt_still_dropped_with_provenance(monkeypatch):
    hook = _load_opencode()
    monkeypatch.setattr(hook, "_git", lambda args, cwd: "should-not-be-used")
    assert hook.build_evidence_payload({"prompt": "write the handoff", "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_opencode_ignores_missing_or_blank_prompt():
    hook = _load_opencode()
    assert hook.build_evidence_payload({"session_id": "x"}) is None
    assert hook.build_evidence_payload({"prompt": "   "}) is None


@pytest.mark.unit
def test_opencode_does_not_block_when_menhir_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_HOOK_LOG", str(tmp_path / "hook.log"))
    hook = _load_opencode()
    ok = hook.post_evidence(
        {"text": "I have 25 things", "namespace": "proj", "session_id": "s",
         "source_client": "opencode", "triage_reason": ["number"]},
        url="http://127.0.0.1:9/turn-evidence", timeout=1.0)
    assert ok is None  # post_evidence returns the turn_id now; failure is the absence of one
    entry = json.loads((tmp_path / "hook.log").read_text().strip())
    assert entry["prompt_len"] == len("I have 25 things") and "text" not in entry
    assert entry["source_client"] == "opencode"


@pytest.mark.unit
def test_opencode_main_is_fail_open_on_malformed_stdin(monkeypatch):
    import io
    hook = _load_opencode()
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0  # malformed input must not raise or block


# ----------------------------------------------------------------------------- cross-client parity


@pytest.mark.unit
def test_triage_parity_with_claude_producer():
    """The OpenCode triage must be byte-for-byte equivalent to the Claude hook's triage: same
    candidate verdict AND same reason labels for every prompt. This is the guard that lets the two
    producers keep independent copies of the rules without silently diverging."""
    oc = _load_opencode()
    cc = _load_claude()
    for text in _CANDIDATES + _JUNK:
        assert oc.triage_user_prompt(text) == cc.triage_user_prompt(text), (
            f"triage diverged on {text!r}: opencode={oc.triage_user_prompt(text)} "
            f"claude={cc.triage_user_prompt(text)}")
    # The rule tables themselves are identical (structural parity, not just behavioral).
    assert oc._PHRASE_RULES == cc._PHRASE_RULES
    assert oc._NUMBER_RE.pattern == cc._NUMBER_RE.pattern
    assert oc._MONEY_RE.pattern == cc._MONEY_RE.pattern
    assert oc._DATE_RE.pattern == cc._DATE_RE.pattern


@pytest.mark.unit
def test_claude_producer_provenance_labels_unchanged():
    """Adding the OpenCode producer must not change the Claude producer's identity labels."""
    cc = _load_claude()
    assert cc.SOURCE_CLIENT == "claude_code"
    assert cc.SOURCE_KIND == "claude_code_hook"
    assert cc.TRIAGE_VERSION == "claude-hook-v1"
    assert cc.HOOK_VERSION == "menhir-turn-evidence-hook-v1"
