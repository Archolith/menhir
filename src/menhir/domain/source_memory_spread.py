"""Session-spread selection for the source-memory lane.

Plain cosine top-k fills the section with several turns of the best-matching session and repeats of
the same text, while another session that holds part of the answer gets no slot. With spread on, the
lane fetches a larger cosine-ordered pool and this picks from it:

1. drop rows whose content repeats an earlier (higher-cosine) row's content exactly;
2. take each session's best row, in cosine order;
3. fill the remaining slots from the rest, in cosine order.

A row without a ``session_id`` counts as its own session, so a namespace with no sessions gets plain
top-k minus exact repeats. Pure and deterministic: the order of ``rows`` decides every tie.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The candidate pool is this many times the section limit (capped by the episode search's 50).
POOL_FACTOR = 3


def pool_size(limit: int) -> int:
    """How many cosine hits to fetch for a section of ``limit`` memories."""
    return max(1, min(50, limit * POOL_FACTOR))


def _content_key(row: Mapping[str, Any]) -> str:
    return " ".join(str(row.get("content") or "").split()).casefold()


def _session_key(row: Mapping[str, Any]) -> str:
    session = str(row.get("session_id") or "").strip()
    return f"session:{session}" if session else f"episode:{row.get('uuid')}"


def spread_by_session(rows: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    """Pick up to ``limit`` rows from cosine-ordered ``rows`` (best first); see the module doc.

    Rows with empty content are never treated as repeats of each other.
    """
    if limit <= 0:
        return []
    seen_content: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for row in rows:
        key = _content_key(row)
        if key and key in seen_content:
            continue
        seen_content.add(key)
        unique.append(row)
    seen_sessions: set[str] = set()
    session_best: list[Mapping[str, Any]] = []
    rest: list[Mapping[str, Any]] = []
    for row in unique:
        session = _session_key(row)
        (rest if session in seen_sessions else session_best).append(row)
        seen_sessions.add(session)
    picked = session_best[:limit]
    picked.extend(rest[: limit - len(picked)])
    return picked
