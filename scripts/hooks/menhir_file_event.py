#!/usr/bin/env python3
"""Hook Center file-event producer: Claude/Codex `PostToolUse` -> normalized file_changed event.

Claude Code and Codex share a `PostToolUse` hook shape: after a file tool runs (Edit / Write /
MultiEdit / NotebookEdit / create / delete / move), the host pipes a JSON event to this script on
stdin. We OBSERVE it, normalize it to a Hook Center `file_changed` event, and POST it to menhir's
`/api/tool-events`, which marks the affected structure-file node dirty so stale references are
detectable. This does NOT wrap the tool — the tool already did its work; we just observe the event.

Hook Center invariants (enforced here):
- Observe file/tool EVENTS; never a raw transcript.
- Upload NO file content — only a local sha256 HASH of the file (provenance, not content) + mtime.
- Fail open: menhir down / unreadable file / malformed input / no path -> log locally + exit 0. Never
  blocks the coding tool, never prints into the agent context.
- No secrets: only the path + hash + git branch/commit are sent.

Install (Claude/Codex `settings.local.json` / `hooks.json`), register on `PostToolUse` matching the
file tools; pipe the event JSON to this script. See docs/hook-center-tool-events.md.

Config (env): reuses the producer core (MENHIR_TURNS_URL host is IGNORED here; this posts to
/api/tool-events derived from MENHIR_TOOL_EVENTS_URL or the default). See below.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

# Reuse the shared producer core (git provenance, failure log, config) — same dir, path-shim like the
# other producers so this runs standalone or under importlib in tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menhir_turn_evidence_common import (  # noqa: E402
    capture_enabled,
    git_probe,
    log_failure,
)

DEFAULT_URL = "http://127.0.0.1:8090/api/tool-events"
SOURCE_KIND = "hook"

#: Claude/Codex tool_name -> file operation. Tools not here are ignored (not file events).
_TOOL_OPERATION = {
    "write": "write",
    "edit": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "create": "create",
    "delete": "delete",
    "rm": "delete",
    "move": "rename",
    "rename": "rename",
}


def _resolve_url() -> str:
    return (os.environ.get("MENHIR_TOOL_EVENTS_URL")
            or os.environ.get("MENHIR_EVENTS_URL")
            or DEFAULT_URL)


def _detect_source_client(hook_input: dict) -> str:
    explicit = (hook_input.get("source_client") or os.environ.get("MENHIR_SOURCE_CLIENT") or "").strip()
    if explicit:
        return explicit
    # Codex sets workspace_root; Claude sets transcript_path with .jsonl. Best-effort, else 'unknown'.
    if hook_input.get("workspace_root"):
        return "codex"
    if hook_input.get("transcript_path"):
        return "claude_code"
    return "unknown"


def _file_hash(path: str) -> tuple[str | None, str | None]:
    """(sha256 hex, mtime iso) of a file, or (None, None) on any failure. Reads bytes to hash ONLY —
    the digest is provenance, never uploaded content. Never raises."""
    try:
        import datetime
        st = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        mtime = datetime.datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z"
        return h.hexdigest(), mtime
    except Exception:
        return None, None


def _structure_relative_path(path: str, project_root: str | None, cwd: str | None = None) -> str:
    """Return a repo-relative path when *path* is absolute and under *project_root*.
    Preserves relative paths as-is. Returns the original path unchanged when it is
    outside project_root or project_root is unknown. Never raises."""
    if not path or not project_root:
        return path
    if os.path.isabs(path):
        try:
            rel = os.path.relpath(path, project_root)
            if not rel.startswith(".."):
                return rel.replace("\\", "/")
        except (ValueError, OSError):
            pass
    return path


def normalize_event(hook_input: dict) -> dict | None:
    """Map a Claude/Codex PostToolUse payload to a normalized `file_changed` event, or None if it is
    not a file tool / has no path. Pure and fail-safe: unknown shapes -> None (skip, never raise)."""
    if not isinstance(hook_input, dict):
        return None
    tool = str(hook_input.get("tool_name") or hook_input.get("tool") or "").strip().lower()
    op = _TOOL_OPERATION.get(tool)
    if op is None:
        return None  # not a file tool -> nothing to observe
    tool_input = hook_input.get("tool_input") or hook_input.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    path = (tool_input.get("file_path") or tool_input.get("path")
            or hook_input.get("path") or hook_input.get("file_path") or "")
    path = str(path).strip()
    if not path:
        return None
    old_path_raw = (tool_input.get("old_path") or tool_input.get("source")
                    or hook_input.get("old_path") or None)
    cwd = hook_input.get("cwd") or hook_input.get("workspace_root")
    project_root = git_probe(["rev-parse", "--show-toplevel"], cwd)
    # Normalize to repo-relative paths for the structure graph match.
    normalized_path = _structure_relative_path(path, project_root, cwd)
    normalized_old_path = None
    if old_path_raw:
        normalized_old_path = _structure_relative_path(str(old_path_raw).strip(), project_root, cwd)
    git_branch = git_commit = None
    if project_root:
        git_branch = git_probe(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        git_commit = git_probe(["rev-parse", "--short", "HEAD"], cwd)
    # local hash of the file AFTER the change (skip for delete). Hash only — never content.
    # Hash uses the ORIGINAL raw path (absolute or cwd-joined) for correct file access.
    after_hash = mtime = None
    if op != "delete":
        abs_path = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        after_hash, mtime = _file_hash(abs_path)
    meta = {"tool_name": tool, "hook_event_name": hook_input.get("hook_event_name")}
    if path != normalized_path:
        meta["original_path"] = path
    return {
        "event_type": "file_changed",
        "source_client": _detect_source_client(hook_input),
        "source_kind": SOURCE_KIND,
        "session_id": hook_input.get("session_id"),
        "project_root": project_root,
        "cwd": cwd,
        "path": normalized_path,
        "old_path": normalized_old_path,
        "operation": op,
        "after_hash": after_hash,
        "mtime": mtime,
        "git_branch": git_branch,
        "git_commit": git_commit,
        "metadata": meta,
    }


def post_event(payload: dict, *, url: str | None = None, timeout: float = 3.0) -> bool:
    import urllib.request
    url = url or _resolve_url()
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as exc:  # menhir down / 4xx-5xx / timeout -- never block the coding tool
        log_failure({"source_client": payload.get("source_client"),
                     "text": payload.get("path") or "", "triage_reason": [payload.get("operation")]},
                    exc)
        return False


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return 0  # malformed hook input must not block the tool
    if not capture_enabled():
        return 0
    if "--dry-run" in sys.argv[1:]:
        payload = normalize_event(hook_input) if isinstance(hook_input, dict) else None
        print(json.dumps({"would_send": payload is not None,
                          "operation": (payload or {}).get("operation"),
                          "path": (payload or {}).get("path"),
                          "content_uploaded": False}, indent=2))
        return 0
    payload = normalize_event(hook_input)
    if payload is None:
        return 0  # not a file tool / no path -> nothing to observe
    post_event(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
