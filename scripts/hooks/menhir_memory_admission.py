#!/usr/bin/env python3
"""`PostToolUse` hook: join a just-written memory to the user turn that prompted it.

Completes the provenance loop that `menhir_turn_evidence.py` starts. That hook fires on
`UserPromptSubmit`, captures the turn, and parks the server's `turn_id` in a session-keyed stash.
This one fires after `add_memory` returns, reads the `episode_id` out of the tool result, pairs it
with the stashed turn, and reports the pair to `/api/episode-admission`.

WHY IT IS A SECOND HOOK RATHER THAN AN ARGUMENT. `PostToolUse` runs AFTER the tool call, so it
cannot add `turn_evidence_uuid` to an `add_memory` that already happened; it can only report the
pairing afterwards. And the pairing cannot be made server-side: menhir's own MCP `session_id` is a
derived constant identical across every window (`api/auth.py`), so the server cannot tell two
conversations apart. The host's `session_id` can, and both hooks see it -- so the join is
hook-to-hook, keyed on a session id that is genuinely per-conversation.

WHAT THE LINK MEANS. That a memory was written in the context of a captured turn. It is NOT proof a
human said the memory's content: every id involved is supplied by whoever holds the agent key. See
`.agent/plans/menhir-evidence-projection-episodes.md`.

Contract, matching the turn-evidence producers:
- **Never block the host agent.** Any failure logs locally and exits 0.
- **Silent stdout on the normal path.** `--dry-run` and `--health` print deliberately.
- **Stdlib only.** No menhir install required.
- **Nothing to link is normal.** Most `add_memory` calls have no stashed turn (the prompt was a
  non-candidate, or the session predates the hook). Absence is silence, not an error.

Register on `add_memory` (and `add_memory_and_track`) -- see `scripts/hooks/README.md`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menhir_turn_evidence_common import (  # noqa: E402  (after sys.path shim, by design)
    log_failure,
    read_stashed_turn_id,
    resolve_url,
)

HOOK_VERSION = "menhir-memory-admission-hook-v1"

#: add_memory answers with a human-readable line: "Queued. episode_id=<uuid>, state=PENDING, ...".
#: Tolerates quoting/whitespace so a formatting tweak degrades to "no link" rather than a crash.
_EPISODE_ID_RE = re.compile(r"episode_id[\"']?\s*[=:]\s*[\"']?([0-9a-fA-F-]{8,})")

_MEMORY_TOOLS = ("add_memory", "add_memory_and_track")


def admission_url() -> str:
    """Derive the admission endpoint from the configured turn-evidence URL, so a deployment that
    moved menhir does not have to configure the same host twice."""
    explicit = (os.environ.get("MENHIR_ADMISSION_URL") or "").strip()
    if explicit:
        return explicit
    return re.sub(r"/turn-evidence/?$", "/episode-admission", resolve_url())


def extract_episode_id(tool_response) -> str | None:
    """Pull the episode uuid out of whatever shape the host reports a tool result in.

    Hosts differ (and change): a plain string, a dict with `content`, or a list of content blocks.
    Rather than encode one shape, flatten to text and search -- a shape change then costs the link,
    never an exception in the host's tool path.
    """
    if tool_response is None:
        return None
    if isinstance(tool_response, dict):
        text = json.dumps(tool_response)
    elif isinstance(tool_response, (list, tuple)):
        text = json.dumps(list(tool_response))
    else:
        text = str(tool_response)
    match = _EPISODE_ID_RE.search(text)
    return match.group(1) if match else None


def is_memory_tool(tool_name: str) -> bool:
    """Match the bare tool name or an MCP-qualified one (`mcp__memory__add_memory`)."""
    name = (tool_name or "").strip().lower()
    return any(name == t or name.endswith(f"__{t}") for t in _MEMORY_TOOLS)


def build_admission(hook_input: dict) -> dict | None:
    """Map a PostToolUse event to an /episode-admission body, or None when there is nothing to link."""
    if not is_memory_tool(str(hook_input.get("tool_name") or "")):
        return None
    episode_uuid = extract_episode_id(hook_input.get("tool_response"))
    if not episode_uuid:
        return None
    turn_id = read_stashed_turn_id(str(hook_input.get("session_id") or ""))
    if not turn_id:
        return None  # normal: the prompt was a non-candidate, so no turn was ever captured
    return {"episode_uuid": episode_uuid, "turn_evidence_uuid": turn_id}


def post_admission(payload: dict, *, url: str | None = None, timeout: float = 3.0) -> bool:
    url = url or admission_url()
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as exc:  # menhir down, 4xx/5xx, timeout -- never block the host agent
        log_failure({"source_client": "memory_admission",
                     "session_id": payload.get("episode_uuid"), "text": ""}, exc)
        return False


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if "--health" in argv:
        print(f"admission_url: {admission_url()}")
        print(f"api_key_configured: {'yes' if (os.environ.get('MENHIR_AGENT_KEY') or '').strip() else 'no'}")
        print(f"hook_version: {HOOK_VERSION}")
        print(f"enabled: {'no' if _disabled() else 'yes'}")
        return 0

    try:
        raw = json.load(sys.stdin)
        hook_input = raw if isinstance(raw, dict) else {}
    except Exception:
        return 0  # malformed input is not the host's problem to hear about

    payload = build_admission(hook_input)

    if "--dry-run" in argv:
        print("would_link: " + ("true" if payload else "false"))
        if payload:
            print(f"episode_uuid: {payload['episode_uuid']}")
            print(f"turn_evidence_uuid: {payload['turn_evidence_uuid']}")
        return 0

    if payload is None or _disabled():
        return 0
    post_admission(payload)
    return 0


def _disabled() -> bool:
    raw = os.environ.get("MENHIR_TURN_EVIDENCE_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("0", "false", "no", "off", "")


if __name__ == "__main__":
    raise SystemExit(main())
