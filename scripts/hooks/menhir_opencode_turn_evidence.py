#!/usr/bin/env python3
"""OpenCode `chat.message` producer: SELECTIVE user-turn evidence capture (ADR 0001).

Second TurnEvidence producer, feeding the SAME `/api/turn-evidence` contract as the Claude Code hook.
OpenCode has no native shell-command prompt hook, so a thin JS plugin
(`scripts/opencode-plugin/menhir-turn-evidence.js`) observes each `chat.message`, extracts the user
prompt, and pipes a JSON envelope to this script on stdin -- mirroring how Claude Code pipes a
`UserPromptSubmit` payload to its hook.

This is a THIN ADAPTER over the shared producer core (`menhir_turn_evidence_common.py`): all triage,
provenance, POST, fail-open, dry-run, and health logic lives there. This module supplies only
OpenCode's identity constants. The evidence semantics are byte-for-byte identical to the Claude
producer (a cross-client parity test enforces it); only the provenance labels differ.

Usage:
  - Normal (via the plugin): a JSON envelope {prompt, session_id, cwd, ...} on stdin; candidates POST.
  - `--dry-run`: print whether a piped prompt WOULD be captured (triage only, never POSTs).
  - `--health`: print local producer config (never POSTs, never prints the API key or prompt text).

Config (env): see `menhir_turn_evidence_common`.
"""

from __future__ import annotations

import os
import sys

# Make the sibling shared module importable whether run directly or loaded by path in tests.
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

DEFAULT_EVENT = "chat.message"
SOURCE_KIND = "opencode_hook"
SOURCE_CLIENT = "opencode"
TRIAGE_VERSION = "opencode-hook-v1"
HOOK_VERSION = "menhir-opencode-turn-evidence-hook-v1"

__all__ = [
    "DEFAULT_URL", "SOURCE_KIND", "SOURCE_CLIENT", "TRIAGE_VERSION", "HOOK_VERSION",
    "triage_user_prompt", "infer_namespace", "build_evidence_payload", "post_evidence", "main",
]


def build_evidence_payload(hook_input: dict) -> dict | None:
    """Map an OpenCode chat.message envelope to a /turn-evidence body IFF the prompt passes triage.
    Returns None when there is no prompt OR the prompt is a non-candidate (nothing to store)."""
    return build_payload(
        hook_input, source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git,
        default_event=DEFAULT_EVENT)


def main() -> int:
    return run_cli(
        sys.argv[1:], source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git,
        default_event=DEFAULT_EVENT)


if __name__ == "__main__":
    raise SystemExit(main())
