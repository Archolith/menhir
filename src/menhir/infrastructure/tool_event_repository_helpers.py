"""Pure helpers and constants for the Hook Center tool-event repository.

Extracted verbatim from `tool_event_repository` (the facade) and re-exported there; import from
either module. Nothing here touches neo4j — these are deterministic, pure functions/constants.
"""

from __future__ import annotations

from datetime import datetime


def _parse_iso_utc(text: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string, returning None on failure."""
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return None
        return dt
    except (ValueError, TypeError):
        return None


#: file operations a v0 file_changed event may carry. rename also touches `old_path`.
FILE_OPERATIONS = ("write", "edit", "delete", "rename", "create")


def affected_paths(path: str | None, old_path: str | None, operation: str | None) -> list[str]:
    """The structure paths an event touches: always `path`; also `old_path` on a rename/move (both the
    source and destination file anchors are affected). Deterministic, pure — deduped, order-preserving,
    empties dropped."""
    out: list[str] = []
    for p in (path, old_path if (operation or "").lower() == "rename" else None):
        if p and p.strip() and p not in out:
            out.append(p.strip())
    return out
