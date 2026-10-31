"""Turn role (speaker) of an episode, decided from structured data only (Archolith/menhir#271).

Never parse a "user:"/"assistant:" prefix out of episode text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from menhir.domain.namespace import normalize_namespace

TURN_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool", "agent", "system"})
UNKNOWN_TURN_ROLE = "unknown"
# Raw turns the worker stores and embeds but never extracts (always on since the #168 freeze).
# An assistant restating or guessing at the user's facts must not become memory of them.
STORE_ONLY_TURN_ROLES: frozenset[str] = frozenset({"assistant"})


AGENT_QUOTED_SOURCE_KIND = "agent_quoted"


def normalize_source_kind(value: object) -> str:
    """Canonical spelling of a TurnEvidence source_kind (stripped, lowercased, '-' as '_')."""
    return str(value or "").strip().lower().replace("-", "_")


def is_agent_quoted(source_kind: object) -> bool:
    """True when a TurnEvidence source_kind marks agent-supplied words, in any spelling."""
    return normalize_source_kind(source_kind) == AGENT_QUOTED_SOURCE_KIND


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
    # non-user memories at face value); equality with the turn text does. Evidence text is stripped
    # on write, so compare stripped. agent_quoted evidence is the agent's claim, not a captured
    # turn, so it proves nothing (the admission gate refuses it for the same reason). A turn from
    # another namespace proves nothing either, matching subject_endpoint_eligible in the claim.
    if (
        type(count) is int
        and count == 1
        and isinstance(content, str)
        and isinstance(text, str)
        and content.strip() == text.strip()
        and not is_agent_quoted(claimed.get("turn_evidence_source_kind"))
        and normalize_namespace(claimed.get("namespace"))
        == normalize_namespace(claimed.get("turn_evidence_namespace"))
    ):
        proven = normalize_turn_role(claimed.get("turn_evidence_role"))
        if proven is not None:
            return proven
    declared = normalize_turn_role(claimed.get("turn_role"))
    if declared is not None:
        return declared
    return UNKNOWN_TURN_ROLE
