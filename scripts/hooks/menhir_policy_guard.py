#!/usr/bin/env python3
"""Hook Center Policy Guard v0: optional pre-edit guard for protected files.

Reads a Claude/Codex `PreToolUse` JSON from stdin, checks the target path(s) against
a local policy file (`.menhir/policy.json`), and optionally warns or blocks based on
the policy mode. Never calls an LLM, never uploads file content.

Modes:
    watch   Log locally if useful; always exit 0.
    warn    Print a warning if a path matches; exit 0.
    block   Print a blocking message if a path matches frozen_paths; exit 2.

Policy format (.menhir/policy.json):
    {
        "mode": "warn",
        "frozen_paths": [".env*", "*.key", "docker-compose.prod.yml", ...],
        "branch_scope": ["scripts/hooks/**", "src/menhir/api/**", ...]
    }

Install: register on `PreToolUse` matching the file tools, similar to
`menhir_file_event.py` on `PostToolUse`. See docs/hook-center-tool-events.md.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys

#: Non-file tools are silently ignored.
_NON_FILE_TOOLS = frozenset()

#: Tools that operate on files, mapped to (path_key, old_path_key).
_FILE_TOOL_PATHS = {
    "write": ("path", None),
    "edit": ("file_path", None),
    "multiedit": ("file_path", None),
    "notebookedit": ("file_path", None),
    "create": ("file_path", None),
    "delete": ("file_path", None),
    "rm": ("file_path", None),
    "move": ("path", "old_path"),
    "rename": ("path", "old_path"),
}


def load_policy(path: str = ".menhir/policy.json") -> dict | None:
    """Load the policy file, or return None if it doesn't exist. Never raises."""
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None


def _normalize(p: str) -> str:
    """Normalize path separators to forward slash for matching."""
    return p.replace("\\", "/")


def _path_matches_any(path: str, patterns: list[str]) -> bool:
    """True if *path* matches any *patterns* via fnmatch (forward-slash normalized on both sides)."""
    p = _normalize(path)
    for pat in patterns:
        if fnmatch.fnmatch(p, _normalize(pat)):
            return True
    return False


def check_path(path: str, policy: dict) -> tuple[str, str] | None:
    """Check a single path against the policy. Returns (action, message) or None if allowed.

    If *path* matches frozen_paths and mode is block -> ("block", message).
    If *path* matches frozen_paths and mode is warn -> ("warn", message).
    If *path* is outside branch_scope and mode is warn -> ("warn", message).
    If *path* is outside branch_scope and mode is block -> ("warn", message) (soften to warn).
    """
    mode = policy.get("mode", "watch")
    frozen = policy.get("frozen_paths") or []
    branch_scope = policy.get("branch_scope")

    if _path_matches_any(path, frozen):
        pat = _matching_pattern(path, frozen)
        if mode == "block":
            return ("block", f"Menhir policy blocked edit: {path} matches frozen_paths pattern \"{pat}\".")
        if mode == "warn":
            return ("warn", f"Menhir policy warning: {path} matches frozen_paths pattern \"{pat}\".")

    if branch_scope and mode in ("warn", "block"):
        if not _path_matches_any(path, branch_scope):
            return ("warn", f"Menhir policy warning: {path} is outside this branch scope.")

    return None


def _matching_pattern(path: str, patterns: list[str]) -> str:
    """Return the first pattern that matches *path* (forward-slash normalized on both sides)."""
    p = _normalize(path)
    for pat in patterns:
        if fnmatch.fnmatch(p, _normalize(pat)):
            return pat
    return patterns[0] if patterns else "?"


def _extract_paths(hook_input: dict) -> list[str]:
    """Extract file path(s) from a PreToolUse payload. Returns [] for non-file tools or missing path."""
    tool = str(hook_input.get("tool_name") or hook_input.get("tool") or "").strip().lower()
    path_keys = _FILE_TOOL_PATHS.get(tool)
    if path_keys is None:
        return []
    tool_input = hook_input.get("tool_input") or hook_input.get("input") or {}
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    path_key, old_key = path_keys
    p = (tool_input.get(path_key) or "").strip()
    if p:
        paths.append(p)
    if old_key:
        old = (tool_input.get(old_key) or "").strip()
        if old:
            paths.append(old)
    return paths


def main() -> int:
    policy_path = os.environ.get("MENHIR_POLICY_FILE") or ".menhir/policy.json"
    policy = load_policy(policy_path)
    if policy is None:
        return 0

    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(hook_input, dict):
        return 0

    mode = str(policy.get("mode", "watch")).lower()
    paths = _extract_paths(hook_input)
    if not paths:
        return 0

    for p in paths:
        result = check_path(p, policy)
        if result is None:
            continue
        action, msg = result
        print(msg)
        if action == "block" and mode == "block":
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
