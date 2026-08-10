"""Output formatting, turn counter, pattern detection, and JSON envelope for hook CLI."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_SUMMARY = 120
DEFAULT_COUNTER_PATH = Path.home() / ".claude" / "hooks" / ".turn_counter.json"
PRUNE_AGE_S = 86_400  # 24 hours


# ---------------------------------------------------------------------------
# Turn counter
# ---------------------------------------------------------------------------

def should_run_this_turn(
    session_id: str,
    frequency: int,
    counter_path: Path = DEFAULT_COUNTER_PATH,
) -> bool:
    """Increment the per-session turn counter and return True on every Nth turn."""
    if frequency <= 0:
        return True

    data: dict[str, dict] = {}
    if counter_path.exists():
        try:
            data = json.loads(counter_path.read_text())
        except Exception:
            data = {}

    raw_entry = data.get(session_id, {"count": 0, "ts": time.time()})
    # Migration: old format stored bare ints, new format uses {count, ts} dicts
    if isinstance(raw_entry, int):
        raw_entry = {"count": raw_entry, "ts": time.time()}
    count = int(raw_entry.get("count", 0))
    raw_entry["count"] = count + 1
    raw_entry["ts"] = time.time()
    data[session_id] = raw_entry

    # Prune stale sessions
    now = time.time()
    data = {
        k: v for k, v in data.items()
        if isinstance(v, dict) and now - v.get("ts", 0) < PRUNE_AGE_S
    }

    try:
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        counter_path.write_text(json.dumps(data))
    except Exception:
        pass

    return count % frequency == 0


# ---------------------------------------------------------------------------
# Write-signal detection (prompt pattern matching)
# ---------------------------------------------------------------------------

# Patterns grouped by signal type — order matters (first match wins per group)
_CORRECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bdon'?t\b", re.IGNORECASE),
    re.compile(r"\bstop\s+(doing|using|adding)\b", re.IGNORECASE),
    re.compile(r"\bno[,.]?\s+not\b", re.IGNORECASE),
    re.compile(r"\bnever\b", re.IGNORECASE),
    re.compile(r"\bwrong\b", re.IGNORECASE),
    re.compile(r"\binstead\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+not\b", re.IGNORECASE),
    re.compile(r"\byou\s+should(n'?t| not)\b", re.IGNORECASE),
]

_CONFIRMATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bperfect\b", re.IGNORECASE),
    re.compile(r"\bexactly\b", re.IGNORECASE),
    re.compile(r"\bkeep\s+doing\b", re.IGNORECASE),
    re.compile(r"\bthat'?s?\s+(right|correct)\b", re.IGNORECASE),
    re.compile(r"\bgood\s+(call|approach|choice)\b", re.IGNORECASE),
]

_EXPLICIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bremember\s+(that|this|to)\b", re.IGNORECASE),
    re.compile(r"\bnote\s+that\b", re.IGNORECASE),
    re.compile(r"\bimportant:\b", re.IGNORECASE),
    re.compile(r"\balways\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+now\s+on\b", re.IGNORECASE),
]

_DECISION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\blet'?s?\s+go\s+with\b", re.IGNORECASE),
    re.compile(r"\bwe\s+decided\b", re.IGNORECASE),
    re.compile(r"\bthe\s+plan\s+is\b", re.IGNORECASE),
    re.compile(r"\bwe'?re\s+going\s+(with|to)\b", re.IGNORECASE),
]

_SIGNAL_GROUPS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("correction", _CORRECTION_PATTERNS),
    ("confirmation", _CONFIRMATION_PATTERNS),
    ("explicit", _EXPLICIT_PATTERNS),
    ("decision", _DECISION_PATTERNS),
]

# Nudge templates per signal type
_WRITE_NUDGES: dict[str, str] = {
    "correction": (
        "The user corrected your approach. Store this feedback via "
        "`mcp__memory__add_memory` so you don't repeat the mistake."
    ),
    "confirmation": (
        "The user confirmed a non-obvious approach. Store what worked via "
        "`mcp__memory__add_memory` so you can reuse it."
    ),
    "explicit": (
        "The user explicitly asked you to remember something. Store it via "
        "`mcp__memory__add_memory` now."
    ),
    "decision": (
        "A decision was made. Store the decision and rationale via "
        "`mcp__memory__add_memory` for future context."
    ),
}


def detect_write_signals(prompt: str) -> list[str]:
    """Scan prompt text for correction/confirmation/decision signals.

    Returns a list of detected signal types (e.g. ["correction", "explicit"]).
    """
    if not prompt or len(prompt.strip()) < 5:
        return []

    detected: list[str] = []
    for signal_type, patterns in _SIGNAL_GROUPS:
        if any(p.search(prompt) for p in patterns):
            detected.append(signal_type)
    return detected


def format_write_nudge(signals: list[str]) -> str:
    """Format a write reminder block for detected signals."""
    if not signals:
        return ""
    nudges = [_WRITE_NUDGES[s] for s in signals if s in _WRITE_NUDGES]
    if not nudges:
        return ""
    return "## Memory Write Reminder\n" + "\n".join(f"- {n}" for n in nudges)


# ---------------------------------------------------------------------------
# Stop hook checkpoint
# ---------------------------------------------------------------------------

_SAVE_CHECKPOINT = """\
## Memory Checkpoint
Before finishing, check if anything from this conversation turn should be stored:
- **Corrections/feedback** the user gave on your approach → `mcp__memory__add_memory`
- **Decisions made** (architecture, tooling, scope) with rationale
- **Project context** not in code/git (deadlines, stakeholders, motivation)
- **User preferences** or workflow patterns you discovered
- **Non-obvious approaches** that worked or failed
Only store what would help a future session. Skip ephemeral task details."""


def format_save_checkpoint() -> str:
    """Return the memory-save checkpoint block for Stop hook injection."""
    return _SAVE_CHECKPOINT


# ---------------------------------------------------------------------------
# Temporal context
# ---------------------------------------------------------------------------

def format_temporal_line(last_accessed_iso: str | None) -> str:
    """Return a single-line temporal context string for hook/recall injection.

    Shows the current UTC time and how long ago the client last accessed memory.
    ``last_accessed_iso`` is the ISO timestamp from the client registry *before*
    this session's touch — so it reflects the previous session's time.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not last_accessed_iso:
        return f"_{now_str} — first session_"

    try:
        last_dt = datetime.fromisoformat(last_accessed_iso)
        delta = now - last_dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            elapsed = f"{int(delta.total_seconds() / 60)}m"
        elif hours < 48:
            elapsed = f"{hours:.1f}h"
        else:
            elapsed = f"{delta.days}d"
        return f"_{now_str} — {elapsed} since last session_"
    except Exception:
        return f"_{now_str}_"


# ---------------------------------------------------------------------------
# Memory formatting (recall output)
# ---------------------------------------------------------------------------

def _format_item(item: dict) -> str:
    """Format a single memory item as a bullet line."""
    if item.get("type") in ("EPISODIC",) or item.get("scope") == "SESSION":
        return ""
    name = item.get("name", "Memory")
    content = (item.get("content") or item.get("summary") or "").strip()
    if not content:
        return ""
    if len(content) > MAX_SUMMARY:
        content = content[:MAX_SUMMARY].rstrip() + "..."
    return f"- {name}: {content}"


def format_hook_output(
    flagged: list[dict],
    context_text: str | None = None,
    query: str | None = None,
    write_nudge: str | None = None,
    temporal_line: str | None = None,
    todos: list[dict] | None = None,
    temporal_memories: list[dict] | None = None,
) -> str:
    """Build the recalled-memories block for hook injection."""
    sections: list[str] = []

    # Temporal context (client-id-specific current time + elapsed)
    if temporal_line:
        sections.append(temporal_line)

    # Reminders section — TEMPORAL nodes within ±30-day window (before TODOs)
    if temporal_memories:
        sections.append(f"### Reminders ({len(temporal_memories)})")
        for mem in temporal_memories:
            target_date = mem.get("target_date") or ""
            content = mem.get("content") or mem.get("name") or ""
            snippet = (content[:80] + "...") if len(content) > 80 else content
            sections.append(f"- {target_date} — {snippet}")

    # TODOs section (after Reminders, before Pinned)
    if todos:
        sections.append(f"### TODOs ({len(todos)} open)")
        for todo in todos:
            tag = (todo.get("priority") or "normal").upper()
            ref = todo.get("code_ref") or ""
            content = todo.get("content") or ""
            snippet = (content[:80] + "...") if len(content) > 80 else content
            ref_part = f" {ref} —" if ref else " —"
            sections.append(f"- [{tag}]{ref_part} {snippet}")

    # Pinned section
    pinned_lines = [_format_item(item) for item in flagged]
    pinned_lines = [line for line in pinned_lines if line]
    if pinned_lines:
        sections.append(f"### Pinned ({len(pinned_lines)})")
        sections.extend(pinned_lines)

    # Context section (pre-formatted by ContextBuilderService)
    if context_text and context_text.strip():
        header = f'### Context (query="{query}")' if query else "### Context"
        sections.append(header)
        sections.append(context_text.strip())

    # Write nudge (from prompt pattern detection)
    if write_nudge:
        sections.append(write_nudge)

    if not sections:
        return ""

    return "## Recalled Memories\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# JSON envelope
# ---------------------------------------------------------------------------

def wrap_hook_response(additional_context: str | None = None) -> str:
    """Wrap output in the Claude Code hook JSON envelope."""
    payload: dict = {"continue": True}
    if additional_context:
        payload["additionalContext"] = additional_context
    return json.dumps(payload)
