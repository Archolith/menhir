"""Access tiers and their order, shared by every tier check."""

from __future__ import annotations

TIER_RANK: dict[str, int] = {"readonly": 0, "agent": 1, "operator": 2}


def tier_allows(tier: str, required: str) -> bool:
    """True when ``tier`` is at or above ``required``; unknown tiers allow nothing."""

    return TIER_RANK.get(tier, -1) >= TIER_RANK.get(required, len(TIER_RANK))


__all__ = ["TIER_RANK", "tier_allows"]
