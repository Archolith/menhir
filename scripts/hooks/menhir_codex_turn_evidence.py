#!/usr/bin/env python3
"""Codex `UserPromptSubmit` hook: SELECTIVE user-turn evidence capture (ADR 0001).

Third TurnEvidence producer, feeding the SAME `/api/turn-evidence` contract as the Claude Code and
OpenCode producers. Codex exposes a Claude-compatible `hooks.json` with a `UserPromptSubmit` event, so
this producer is registered exactly like the Claude hook -- Codex pipes the event JSON to stdin and
this script POSTs only triage candidates. See `scripts/hooks/README.md` for the `.codex/hooks.json`
registration.

This is a THIN ADAPTER over the shared producer core (`menhir_turn_evidence_common.py`): all triage,
provenance, POST, fail-open, dry-run, and health logic lives there. This module supplies only Codex's
identity constants plus a small normaliser for Codex's field aliases (`user_prompt`/`workspace_root`).
The evidence semantics are byte-for-byte identical to the other producers (a cross-client parity test
enforces it); only the provenance labels differ.

Usage:
  - Normal (registered hook): Codex pipes the UserPromptSubmit JSON to stdin; candidates are POSTed.
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

DEFAULT_EVENT = "UserPromptSubmit"
SOURCE_KIND = "codex_hook"
SOURCE_CLIENT = "codex"
TRIAGE_VERSION = "codex-hook-v1"
HOOK_VERSION = "menhir-codex-turn-evidence-hook-v1"

__all__ = [
    "DEFAULT_URL", "SOURCE_KIND", "SOURCE_CLIENT", "TRIAGE_VERSION", "HOOK_VERSION",
    "triage_user_prompt", "infer_namespace", "build_evidence_payload", "main", "normalize",
    "post_evidence",
]


def normalize(raw: dict) -> dict:
    """Map Codex's UserPromptSubmit payload to the common producer envelope.

    Codex sends `prompt` (with `user_prompt` as an older alias) and `cwd` (with `workspace_root` as an
    alias); everything else already matches the common shape. Missing keys resolve to None so capture
    stays fail-open on an unfamiliar payload."""
    if not isinstance(raw, dict):
        return {}
    return {
        "prompt": raw.get("prompt") if raw.get("prompt") is not None else raw.get("user_prompt"),
        "session_id": raw.get("session_id"),
        "cwd": raw.get("cwd") if raw.get("cwd") is not None else raw.get("workspace_root"),
        "transcript_path": raw.get("transcript_path"),
        "permission_mode": raw.get("permission_mode"),
        "hook_event_name": raw.get("hook_event_name"),
    }


def build_evidence_payload(hook_input: dict) -> dict | None:
    """Map a Codex UserPromptSubmit payload (raw or normalised) to a /turn-evidence body IFF the
    prompt passes triage. Returns None when there is no prompt OR the prompt is a non-candidate."""
    return build_payload(
        normalize(hook_input), source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git,
        default_event=DEFAULT_EVENT)


def main() -> int:
    return run_cli(
        sys.argv[1:], source_kind=SOURCE_KIND, source_client=SOURCE_CLIENT,
        hook_version=HOOK_VERSION, triage_version=TRIAGE_VERSION, git_fn=_git,
        default_event=DEFAULT_EVENT, normalize=normalize)


if __name__ == "__main__":
    raise SystemExit(main())
