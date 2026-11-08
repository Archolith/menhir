"""Catalog 2 moves get_client_context from operate to core without changing catalog-1 policies."""

from __future__ import annotations

from itertools import combinations

import pytest

from menhir.mcp.tool_profiles import (
    CATALOG_VERSION,
    EARLIER_PLACEMENTS,
    PROFILE_NAMES,
    TOOL_PROFILE,
    placement,
    production_tool_tiers,
    resolve,
)

pytestmark = pytest.mark.unit

TIERS = production_tool_tiers()
MOVED = "get_client_context"


def _resolve(profiles, tier, catalog):
    return resolve(
        profiles=list(profiles), maximum_tier=tier, tool_tiers=TIERS, policy_catalog=catalog
    )


def test_catalog_2_puts_get_client_context_in_core_for_agent_clients() -> None:
    assert CATALOG_VERSION >= 2
    resolved = _resolve(["core", "curate"], "agent", 2)
    assert MOVED in resolved.allowed


def test_catalog_1_agent_policy_still_lacks_it_and_says_so() -> None:
    resolved = _resolve(["core", "curate"], "agent", 1)
    assert MOVED not in resolved.allowed
    assert MOVED in resolved.withheld


def test_catalog_1_operate_policy_keeps_it() -> None:
    """The silent-loss case: a pinned operate policy must not drop a tool it has today."""
    resolved = _resolve(["operate"], "operator", 1)
    assert MOVED in resolved.allowed
    assert MOVED not in resolved.withheld


def test_catalog_2_operate_only_policy_no_longer_lists_it() -> None:
    resolved = _resolve(["operate"], "operator", 2)
    assert MOVED not in resolved.allowed


@pytest.mark.parametrize("tier", ["readonly", "agent", "operator"])
def test_catalog_1_resolution_is_unchanged_for_every_other_tool(tier) -> None:
    for size in range(1, len(PROFILE_NAMES) + 1):
        for profiles in combinations(PROFILE_NAMES, size):
            try:
                old = _resolve(profiles, tier, 1)
                new = _resolve(profiles, tier, 2)
            except ValueError:
                continue  # selection resolves to nothing at this tier under both
            assert old.allowed - {MOVED} == new.allowed - {MOVED}, profiles


def test_catalog_1_membership_matches_the_released_catalog() -> None:
    """Catalog 1 as shipped in v0.3.5: get_client_context was operate."""
    assert placement(MOVED, 1) == "operate"
    assert placement(MOVED, 2) == "core"


def test_earlier_placements_are_older_than_the_current_one() -> None:
    for tool, history in EARLIER_PLACEMENTS.items():
        current_profile, current_since = TOOL_PROFILE[tool]
        sinces = [since for _, since in history]
        assert sinces == sorted(sinces)
        assert all(since < current_since for since in sinces)
        assert all(profile in PROFILE_NAMES for profile, _ in history)
        assert history[-1][0] != current_profile
