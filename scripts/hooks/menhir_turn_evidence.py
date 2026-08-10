#!/usr/bin/env python3
"""Claude Code `UserPromptSubmit` hook: SELECTIVE user-turn evidence capture (ADR 0001, Claude MVP).

The hook OBSERVES every user prompt but STORES only prompts that pass a cheap, deterministic, local
triage -- a prompt that looks like durable memory evidence (a number, a possession, a preference, a
decision, a correction). Boring prompts ("rewrite this", "continue", "explain this error") are dropped
and never leave the machine. **No LLM runs here.**

This is a THIN ADAPTER over the shared producer core (`menhir_turn_evidence_common.py`): all triage,
provenance, POST, fail-open, dry-run, and health logic lives there so Claude, OpenCode, and Codex
producers cannot drift. This module supplies only Claude's identity constants and wires the shared
entrypoint. Claude's capture BEHAVIOR is unchanged from before the shared refactor (verified by the
existing test suite + the cross-client parity test).

Usage:
  - Normal (registered hook): Claude pipes the UserPromptSubmit JSON to stdin; candidates are POSTed.
  - `--dry-run`: print whether a piped prompt WOULD be captured (triage only, never POSTs).
  - `--health`: print local producer config (never POSTs, never prints the API key or prompt text).

Config (env): see `menhir_turn_evidence_common` -- MENHIR_TURNS_URL, MENHIR_AGENT_KEY,
MENHIR_TURN_NAMESPACE, MENHIR_TURN_HOOK_LOG, MENHIR_TURN_EVIDENCE_ENABLED, MENHIR_TURN_EVIDENCE_DRY_RUN.
"""

from __future__ import annotations

import os
import sys

# Make the sibling shared module importable whether this file is run directly (its dir is sys.path[0])
# or loaded by path in tests via importlib (which sets __file__ but not sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menhir_turn_evidence_common import (  # noqa: E402  (after sys.path shim, by design)
    DEFAULT_URL,
    build_payload,
    infer_namespace,
    post_evidence,
    run_cli,
    triage_user_prompt,
)

# Re-export the monkeypatch/inspection seams the test suite references on this module.
from menhir_turn_evidence_common import _log_failure  # noqa: E402,F401
from menhir_turn_evidence_common import _prompt_hash  # noqa: E402,F401
from menhir_turn_evidence_common import git_probe as _git  # noqa: E402
from menhir_turn_evidence_common import (  # noqa: E402  (parity-test seams)
    _DATE_RE,
    _MONEY_RE,
    _NUMBER_RE,
    _PHRASE_RULES,
)

SOURCE_KIND = "claude_code_hook"
SOURCE_CLIENT = "claude_code"
TRIAGE_VERSION = "claude-hook-v1"
HOOK_VERSION = "menhir-turn-evidence-hook-v1"

__all__ = [
    "DEFAULT_URL", "SOURCE_KIND", "SOURCE_CLIENT", "TRIAGE_VERSION", "HOOK_VERSION",
    "triage_user_prompt", "infer_namespace", "build_evidence_payload", "post_evidence", "main",
]


def build_evidence_payload(hook_input: dict) -> dict | None:
    """Map a Claude UserPromptSubmit payload to a /turn-evidence body IFF the prompt passes triage.
    Returns None when there is no prompt OR the prompt is a non-candidate (nothing to store).

    `_git` is read from this module's globals at call time, preserving the `monkeypatch.setattr(hook,
    "_git", ...)` seam the tests rely on."""
    return build_payload(
        hook_input, source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git)


def main() -> int:
    return run_cli(
        sys.argv[1:], source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git)


if __name__ == "__main__":
    raise SystemExit(main())
