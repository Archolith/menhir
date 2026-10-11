"""Turn role (speaker) of an episode, decided from structured data only (Archolith/menhir#271).

Never parse a "user:"/"assistant:" prefix out of episode text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TURN_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool", "agent", "system"})
UNKNOWN_TURN_ROLE = "unknown"


def normalize_turn_role(value: object) -> str | None:
    """A member of TURN_ROLES (str, stripped, lowercased), else None."""
    if not isinstance(value, str):
        return None
    role = value.strip().lower()
    return role if role in TURN_ROLES else None


def resolve_turn_role(claimed: Mapping[str, Any]) -> str:
    """Speaker of a claimed episode, from structured data only."""
    count = claimed.get("turn_evidence_count")
    content = claimed.get("content")
    text = claimed.get("turn_evidence_text")
    # A link alone does not prove the speaker (intake links a caller-supplied turn_evidence_uuid to
    # non-user memories at face value); equality with the turn text does.
    if (
        type(count) is int
        and count == 1
        and isinstance(content, str)
        and isinstance(text, str)
        and content == text
    ):
        proven = normalize_turn_role(claimed.get("turn_evidence_role"))
        if proven is not None:
            return proven
    declared = normalize_turn_role(claimed.get("turn_role"))
    if declared is not None:
        return declared
    return UNKNOWN_TURN_ROLE
