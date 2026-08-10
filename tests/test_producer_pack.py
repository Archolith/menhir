"""Producer Pack v1 cross-cutting guarantees (ADR 0001).

Proves the properties that must hold ACROSS all TurnEvidence producers (Claude / OpenCode / Codex):
- one shared triage that cannot drift (behavioral + structural parity),
- `--dry-run` and `--health` never POST and never leak secrets,
- the `MENHIR_TURN_EVIDENCE_ENABLED` / `MENHIR_TURN_EVIDENCE_DRY_RUN` env toggles behave,
- all producers share the ONE triage implementation (same objects from the common module).
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "scripts" / "hooks"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _common():
    """Return the canonical shared module the hooks actually import (registered in sys.modules by the
    hooks' `from menhir_turn_evidence_common import ...`), not a fresh copy -- so identity assertions
    and the post_evidence spy target the same object run_cli closes over."""
    import sys
    if "menhir_turn_evidence_common" not in sys.modules:
        _claude()  # side effect: puts the hooks dir on sys.path and imports the shared module
    return sys.modules["menhir_turn_evidence_common"]


def _claude():
    return _load("menhir_turn_evidence", "menhir_turn_evidence.py")


def _opencode():
    return _load("menhir_opencode_turn_evidence", "menhir_opencode_turn_evidence.py")


def _codex():
    return _load("menhir_codex_turn_evidence", "menhir_codex_turn_evidence.py")


_CORPUS = [
    "I have 25 movies on my watch list.",
    "I bought one bike for $50 and another for $75.",
    "I no longer use soy sauce because it triggers me.",
    "We decided to use Option B for Turn capture.",
    "The current threshold is 0.82.",
    "Actually it is 20, not 25.",
    "Not 25 anymore, it is 20.",
    "Remember that my API key rotates every Monday.",
    "I prefer dark mode and I use Postgres.",
    "Can you rewrite this?",
    "Explain this error.",
    "refactor the parser",
    "Continue.",
    "",
]

_ALL_PRODUCERS = ("claude_code", "opencode", "codex")


# ----------------------------------------------------------------------------- triage parity


@pytest.mark.unit
def test_triage_parity_across_all_producers():
    """Claude, OpenCode, and Codex must agree on candidate verdict AND reason labels for every
    prompt -- the guard against producer drift."""
    cc, oc, cx = _claude(), _opencode(), _codex()
    for text in _CORPUS:
        rc = cc.triage_user_prompt(text)
        ro = oc.triage_user_prompt(text)
        rx = cx.triage_user_prompt(text)
        assert rc == ro == rx, f"triage diverged on {text!r}: claude={rc} opencode={ro} codex={rx}"


@pytest.mark.unit
def test_all_producers_share_one_triage_implementation():
    """Structural single-source: every producer's triage objects ARE the common module's objects, so
    they cannot drift by construction."""
    common = _common()
    for mod in (_claude(), _opencode(), _codex()):
        assert mod.triage_user_prompt is common.triage_user_prompt
        assert mod._PHRASE_RULES is common._PHRASE_RULES
        assert mod._NUMBER_RE is common._NUMBER_RE
        assert mod._MONEY_RE is common._MONEY_RE
        assert mod._DATE_RE is common._DATE_RE


@pytest.mark.unit
def test_each_producer_has_distinct_source_client_labels():
    cc, oc, cx = _claude(), _opencode(), _codex()
    clients = {cc.SOURCE_CLIENT, oc.SOURCE_CLIENT, cx.SOURCE_CLIENT}
    assert clients == set(_ALL_PRODUCERS)
    kinds = {cc.SOURCE_KIND, oc.SOURCE_KIND, cx.SOURCE_KIND}
    assert kinds == {"claude_code_hook", "opencode_hook", "codex_hook"}


# ----------------------------------------------------------------------------- dry-run never POSTs


@pytest.mark.unit
@pytest.mark.parametrize("filename", [
    "menhir_turn_evidence.py", "menhir_opencode_turn_evidence.py", "menhir_codex_turn_evidence.py"])
def test_dry_run_flag_never_posts(filename, monkeypatch, capsys):
    common = _common()
    posted = []
    monkeypatch.setattr(common, "post_evidence", lambda *a, **k: posted.append(a))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "I have 25 movies on my watch list."}'))
    monkeypatch.setattr("sys.argv", [filename, "--dry-run"])
    hook = _load(filename[:-3], filename)
    assert hook.main() == 0
    out = capsys.readouterr().out
    assert "would_capture: true" in out
    assert posted == [], "dry-run must never POST"


@pytest.mark.unit
def test_dry_run_env_forces_no_post(monkeypatch, capsys):
    common = _common()
    posted = []
    monkeypatch.setattr(common, "post_evidence", lambda *a, **k: posted.append(a))
    monkeypatch.setenv("MENHIR_TURN_EVIDENCE_DRY_RUN", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "I have 25 movies"}'))
    monkeypatch.setattr("sys.argv", ["menhir_turn_evidence.py"])  # no --dry-run flag; env forces it
    hook = _claude()
    assert hook.main() == 0
    assert posted == []
    assert "would_capture: true" in capsys.readouterr().out


@pytest.mark.unit
def test_disabled_env_no_post_but_not_dry_run(monkeypatch, capsys):
    common = _common()
    posted = []
    monkeypatch.setattr(common, "post_evidence", lambda *a, **k: posted.append(a))
    monkeypatch.setenv("MENHIR_TURN_EVIDENCE_ENABLED", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt": "I have 25 movies"}'))
    monkeypatch.setattr("sys.argv", ["menhir_turn_evidence.py"])
    hook = _claude()
    assert hook.main() == 0
    assert posted == []  # disabled -> silent no-op
    assert capsys.readouterr().out == ""  # not dry-run, so no verdict printed


# ----------------------------------------------------------------------------- health never POSTs / leaks


@pytest.mark.unit
@pytest.mark.parametrize("filename,client", [
    ("menhir_turn_evidence.py", "claude_code"),
    ("menhir_opencode_turn_evidence.py", "opencode"),
    ("menhir_codex_turn_evidence.py", "codex")])
def test_health_reports_config_without_posting_or_leaking(filename, client, monkeypatch, capsys):
    common = _common()
    posted = []
    monkeypatch.setattr(common, "post_evidence", lambda *a, **k: posted.append(a))
    monkeypatch.setenv("MENHIR_AGENT_KEY", "super-secret-token-value")
    monkeypatch.setattr("sys.argv", [filename, "--health"])
    hook = _load(filename[:-3], filename)
    assert hook.main() == 0
    out = capsys.readouterr().out
    assert f"source_client: {client}" in out
    assert "api_key_configured: yes" in out
    assert "super-secret-token-value" not in out, "health must never print the API key"
    assert posted == [], "health must never POST"
