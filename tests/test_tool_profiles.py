"""Tool profiles: catalog coverage and resolution rules (#435)."""

from __future__ import annotations

import pytest

from menhir.mcp import tool_profiles
from menhir.mcp.tool_profiles import (
    ADMIN,
    CATALOG_VERSION,
    PROFILE_NAMES,
    PROFILES,
    TOOL_PROFILE,
    production_tool_tiers,
    resolve,
)

TIERS = production_tool_tiers()


def _resolve(**kwargs):
    kwargs.setdefault("tool_tiers", TIERS)
    kwargs.setdefault("policy_catalog", CATALOG_VERSION)
    return resolve(**kwargs)


def test_every_registered_tool_is_in_exactly_one_profile() -> None:
    assert set(TOOL_PROFILE) == set(TIERS)
    seen: list[str] = []
    for name in PROFILE_NAMES:
        seen.extend(PROFILES[name])
    assert sorted(seen) == sorted(TIERS)


def test_catalog_entries_are_not_newer_than_the_catalog() -> None:
    assert all(1 <= since <= CATALOG_VERSION for _, since in TOOL_PROFILE.values())


def test_admin_profile_holds_client_and_namespace_lifecycle() -> None:
    assert PROFILES[ADMIN] == {"list_clients", "mint_client", "revoke_client", "delete_namespace"}


def test_profile_tools_above_the_tier_are_dropped_and_reported() -> None:
    resolved = _resolve(profiles=["core", "curate"], maximum_tier="agent")

    assert "delete_memory" not in resolved.allowed  # curate, operator tier
    assert "delete_memory" in resolved.above_tier
    assert "flag_memory" in resolved.allowed  # curate, agent tier
    assert "recall_memories" in resolved.allowed
    assert all(TIERS[tool] != "operator" for tool in resolved.allowed)


def test_readonly_tier_keeps_only_readonly_tools() -> None:
    resolved = _resolve(profiles=["core", "operate"], maximum_tier="readonly")

    assert resolved.allowed
    assert {TIERS[tool] for tool in resolved.allowed} == {"readonly"}
    assert "add_memory" in resolved.above_tier


def test_explicit_allow_above_the_tier_is_an_error() -> None:
    with pytest.raises(ValueError, match="above maximum_tier"):
        _resolve(profiles=["core"], allow=["delete_memory"], maximum_tier="agent")


def test_allow_and_deny_adjust_the_profile_union() -> None:
    resolved = _resolve(
        profiles=["core"],
        allow=["get_memory_stats"],
        deny=["add_memory"],
        maximum_tier="agent",
    )

    assert "get_memory_stats" in resolved.allowed
    assert "add_memory" not in resolved.allowed


def test_deny_wins_over_profile_membership_for_admin_tools() -> None:
    resolved = _resolve(profiles=list(PROFILE_NAMES), deny=sorted(PROFILES[ADMIN]), maximum_tier="operator")

    assert not resolved.allowed & PROFILES[ADMIN]
    assert resolved.allowed == frozenset(TIERS) - PROFILES[ADMIN]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"profiles": ["everything"]}, "unknown profiles"),
        ({"profiles": ["core"], "allow": ["no_such_tool"]}, "unknown tools"),
        ({"profiles": ["core"], "deny": ["no_such_tool"]}, "unknown tools"),
        ({"profiles": ["core"], "allow": ["get_memory_stats"], "deny": ["get_memory_stats"]}, "both allowed and denied"),
        ({"profiles": ["core", "core"]}, "unique"),
        ({"profiles": [""]}, "non-empty strings"),
        ({"profiles": []}, "no tools"),
        ({"profiles": ["core"], "deny": sorted(PROFILES["core"])}, "no tools"),
    ],
)
def test_invalid_selections_fail_closed(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        _resolve(maximum_tier="operator", **kwargs)


@pytest.mark.parametrize("tier", ["", "admin", "root"])
def test_unknown_tier_fails_closed(tier) -> None:
    with pytest.raises(ValueError, match="maximum_tier"):
        _resolve(profiles=["core"], maximum_tier=tier)


@pytest.mark.parametrize("catalog", [0, -1, True, "1", None])
def test_policy_catalog_must_be_a_positive_integer(catalog) -> None:
    with pytest.raises(ValueError, match="profile_catalog"):
        _resolve(profiles=["core"], maximum_tier="agent", policy_catalog=catalog)


def test_policy_pinned_to_a_newer_catalog_is_refused() -> None:
    with pytest.raises(ValueError, match="newer than this build"):
        _resolve(profiles=["core"], maximum_tier="agent", policy_catalog=CATALOG_VERSION + 1)


def test_tools_newer_than_the_policy_catalog_are_withheld(monkeypatch) -> None:
    # Simulate the next catalog adding a curate tool; a policy still pinned to
    # the current catalog must not receive it.
    monkeypatch.setattr(tool_profiles, "CATALOG_VERSION", CATALOG_VERSION + 1)
    monkeypatch.setitem(tool_profiles.TOOL_PROFILE, "flag_memory", ("curate", CATALOG_VERSION + 1))

    pinned = tool_profiles.resolve(
        profiles=["core", "curate"],
        maximum_tier="agent",
        tool_tiers=TIERS,
        policy_catalog=CATALOG_VERSION,
    )
    current = tool_profiles.resolve(
        profiles=["core", "curate"],
        maximum_tier="agent",
        tool_tiers=TIERS,
        policy_catalog=CATALOG_VERSION + 1,
    )

    assert "flag_memory" not in pinned.allowed
    assert pinned.withheld == {"flag_memory"}
    assert "flag_memory" in current.allowed
    assert not current.withheld


def test_explicit_allow_grants_a_withheld_tool(monkeypatch) -> None:
    monkeypatch.setattr(tool_profiles, "CATALOG_VERSION", CATALOG_VERSION + 1)
    monkeypatch.setitem(tool_profiles.TOOL_PROFILE, "flag_memory", ("curate", CATALOG_VERSION + 1))

    resolved = tool_profiles.resolve(
        profiles=["curate"],
        allow=["flag_memory"],
        maximum_tier="agent",
        tool_tiers=TIERS,
        policy_catalog=CATALOG_VERSION,
    )

    assert "flag_memory" in resolved.allowed
    assert not resolved.withheld


def test_profile_tool_missing_from_runtime_is_never_granted() -> None:
    tiers = {name: tier for name, tier in TIERS.items() if name != "recall_memories"}

    resolved = resolve(
        profiles=["core"],
        maximum_tier="agent",
        tool_tiers=tiers,
        policy_catalog=CATALOG_VERSION,
    )

    assert "recall_memories" not in resolved.allowed


def test_mcp_tools_yaml_profiles_match_the_catalog() -> None:
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".agent" / "mcp-tools.yaml").read_text(
        encoding="utf-8"
    )
    section = text.split("\n  profiles:\n", 1)[1].split("\n  resources:\n", 1)[0]
    version = re.search(r"^    catalog_version: (\d+)$", section, re.MULTILINE)
    assert version is not None and int(version.group(1)) == CATALOG_VERSION
    documented = {
        name: frozenset(re.findall(r"mcp\.tool\.(\w+)", ids))
        for name, ids in re.findall(r"^    (\w+):\n      ids: \[([^\]]*)\]", section, re.MULTILINE)
    }
    assert documented == PROFILES
