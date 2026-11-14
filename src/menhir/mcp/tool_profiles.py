"""Named tool profiles that a version 3 client policy composes into an allowlist.

A profile groups tools by what a client does with them, not by tier. The tier
gate still applies on top: a profile tool above the client's ``maximum_tier`` is
dropped and reported, never granted.

``CATALOG_VERSION`` grows when a tool is added to a profile or moved between
profiles. Each entry records the catalog version that put it there (``since``).
A policy pinned to an older catalog does not receive newer entries; they are
returned as withheld so startup can log them. A moved tool keeps its earlier
placements in ``EARLIER_PLACEMENTS``, so an older policy still resolves it
where its catalog put it. Release notes list every bump.

Catalog 2: ``get_client_context`` moved from operate to core.
Catalog 3: ``delete_memories`` and ``list_namespace_memories`` added to curate (#476).
Catalog 4: ``list_candidates`` and ``review_candidate`` added to curate (#431).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from menhir.tiers import TIER_RANK, tier_allows

CATALOG_VERSION = 4

CORE = "core"
CURATE = "curate"
OPERATE = "operate"
ADMIN = "admin"
PROFILE_NAMES: tuple[str, ...] = (CORE, CURATE, OPERATE, ADMIN)

#: tool name -> (profile, catalog version that placed it there)
TOOL_PROFILE: dict[str, tuple[str, int]] = {
    # core: everyday recall, context and todo work
    "recall_memories": (CORE, 1),
    "recall_context_memories": (CORE, 1),
    "recall_history": (CORE, 1),
    "recall_timeline": (CORE, 1),
    "read_flagged_memories": (CORE, 1),
    "build_context": (CORE, 1),
    "get_client_context": (CORE, 2),
    "query_structure": (CORE, 1),
    "get_provenance": (CORE, 1),
    "get_beacon_evidence": (CORE, 1),
    "get_scalar_view_items": (CORE, 1),
    "add_memory": (CORE, 1),
    "list_todos": (CORE, 1),
    "get_todo": (CORE, 1),
    "add_todo": (CORE, 1),
    "close_todo": (CORE, 1),
    "close_stale_todos": (CORE, 1),
    # curate: shaping memory, conflicts, artifacts, todos and ingest
    "flag_memory": (CURATE, 1),
    "unflag_memory": (CURATE, 1),
    "promote_memory": (CURATE, 1),
    "close_memory": (CURATE, 1),
    "delete_memory": (CURATE, 1),
    "delete_memories": (CURATE, 3),
    "list_namespace_memories": (CURATE, 3),
    "add_candidate": (CURATE, 1),
    "list_candidates": (CURATE, 4),
    "review_candidate": (CURATE, 4),
    "add_memory_and_track": (CURATE, 1),
    "rate_recall": (CURATE, 1),
    "review_inbox": (CURATE, 1),
    "acknowledge_warning": (CURATE, 1),
    "list_conflicts": (CURATE, 1),
    "resolve_conflict": (CURATE, 1),
    "scan_for_conflicts": (CURATE, 1),
    "run_llm_conflict_review": (CURATE, 1),
    "requeue_conflicts_for_llm_review": (CURATE, 1),
    "get_artifact": (CURATE, 1),
    "get_artifact_relationships": (CURATE, 1),
    "list_artifacts": (CURATE, 1),
    "list_artifact_questions": (CURATE, 1),
    "link_artifacts": (CURATE, 1),
    "supersede_artifact": (CURATE, 1),
    "transition_artifact": (CURATE, 1),
    "relocate_artifact_source": (CURATE, 1),
    "audit_artifact_corpus": (CURATE, 1),
    "reopen_todo": (CURATE, 1),
    "resolve_todo": (CURATE, 1),
    "supersede_todo": (CURATE, 1),
    "link_memory_to_todo": (CURATE, 1),
    "ingest_document": (CURATE, 1),
    "ingest_project": (CURATE, 1),
    # operate: runtime health, enrichment and scheduler control
    "get_memory_stats": (OPERATE, 1),
    "view_entropy": (OPERATE, 1),
    "get_enrichment_status": (OPERATE, 1),
    "watch_enrichment": (OPERATE, 1),
    "list_enrichment_queue": (OPERATE, 1),
    "get_episode_trace": (OPERATE, 1),
    "force_reenrich": (OPERATE, 1),
    "force_release_enrichment_lease": (OPERATE, 1),
    "repair_stale_enrichment": (OPERATE, 1),
    "recover_orphans": (OPERATE, 1),
    "pause_scheduler": (OPERATE, 1),
    "resume_scheduler": (OPERATE, 1),
    "force_scheduler_takeover": (OPERATE, 1),
    # admin: client identity and namespace lifecycle
    "list_clients": (ADMIN, 1),
    "mint_client": (ADMIN, 1),
    "revoke_client": (ADMIN, 1),
    "delete_namespace": (ADMIN, 1),
}

#: tool name -> earlier (profile, since) placements, oldest first, for moved tools.
EARLIER_PLACEMENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "get_client_context": ((OPERATE, 1),),
}

#: Profile membership in the current catalog.
PROFILES: dict[str, frozenset[str]] = {
    name: frozenset(tool for tool, (profile, _) in TOOL_PROFILE.items() if profile == name)
    for name in PROFILE_NAMES
}


def placement(tool: str, policy_catalog: int) -> str | None:
    """The profile ``tool`` belongs to under ``policy_catalog``; None if not yet placed."""
    profile, since = TOOL_PROFILE[tool]
    if since <= policy_catalog:
        return profile
    for earlier_profile, earlier_since in reversed(EARLIER_PLACEMENTS.get(tool, ())):
        if earlier_since <= policy_catalog:
            return earlier_profile
    return None


@dataclass(frozen=True)
class ResolvedTools:
    """Outcome of resolving one client's profile selection."""

    allowed: frozenset[str]
    above_tier: frozenset[str]
    withheld: frozenset[str]


def _names(values: Iterable[object], field: str) -> frozenset[str]:
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} entries must be non-empty strings")
        names.append(value)
    if len(names) != len(set(names)):
        raise ValueError(f"{field} entries must be unique")
    return frozenset(names)


def resolve(
    *,
    profiles: Iterable[object],
    allow: Iterable[object] = (),
    deny: Iterable[object] = (),
    maximum_tier: str,
    tool_tiers: Mapping[str, str],
    policy_catalog: int,
) -> ResolvedTools:
    """Resolve profiles plus explicit allow/deny into a tool allowlist.

    ``tool_tiers`` maps every runtime tool name to its required tier. Profile
    tools above ``maximum_tier`` and tools newer than ``policy_catalog`` are
    left out and reported. An explicit ``allow`` above the tier is an error,
    because the author asked for something the tier gate would refuse anyway.
    """

    if maximum_tier not in TIER_RANK:
        raise ValueError(f"unknown maximum_tier {maximum_tier!r}")
    if isinstance(policy_catalog, bool) or not isinstance(policy_catalog, int) or policy_catalog < 1:
        raise ValueError("profile_catalog must be a positive integer")
    if policy_catalog > CATALOG_VERSION:
        raise ValueError(
            f"policy profile_catalog {policy_catalog} is newer than this build's "
            f"catalog {CATALOG_VERSION}"
        )
    profile_names = _names(profiles, "profiles")
    allow_names = _names(allow, "allow")
    deny_names = _names(deny, "deny")
    unknown_profiles = sorted(profile_names - set(PROFILE_NAMES))
    if unknown_profiles:
        raise ValueError(f"unknown profiles {unknown_profiles}")
    unknown_tools = sorted((allow_names | deny_names) - set(tool_tiers))
    if unknown_tools:
        raise ValueError(f"unknown tools {unknown_tools}")
    overlap = sorted(allow_names & deny_names)
    if overlap:
        raise ValueError(f"tools both allowed and denied {overlap}")
    over_tier_allow = sorted(
        tool for tool in allow_names if not tier_allows(maximum_tier, tool_tiers[tool])
    )
    if over_tier_allow:
        raise ValueError(
            f"allow names tools above maximum_tier {maximum_tier!r}: {over_tier_allow}"
        )

    selected: set[str] = set()
    withheld: set[str] = set()
    for tool, (current_profile, _) in TOOL_PROFILE.items():
        pinned_profile = placement(tool, policy_catalog)
        if pinned_profile in profile_names:
            selected.add(tool)
        elif current_profile in profile_names:
            # A newer catalog adds this tool to a selected profile; the pin keeps it out.
            withheld.add(tool)
    # A profile entry the runtime does not register cannot be granted.
    selected &= set(tool_tiers)
    above_tier = {
        tool for tool in selected if not tier_allows(maximum_tier, tool_tiers[tool])
    }
    allowed = ((selected - above_tier) | allow_names) - deny_names
    if not allowed:
        raise ValueError("profile selection resolves to no tools")
    return ResolvedTools(
        allowed=frozenset(allowed),
        above_tier=frozenset(above_tier - deny_names),
        withheld=frozenset(withheld - allow_names - deny_names),
    )


def production_tool_tiers() -> dict[str, str]:
    """Required tier for every registered production tool (lazy, avoids import cycles)."""

    from menhir.mcp.tools import ALL_TOOLS

    return {
        tool_cls.name: getattr(tool_cls, "required_tier", "agent")
        for tool_cls in ALL_TOOLS
        if hasattr(tool_cls, "name")
    }


__all__ = [
    "ADMIN",
    "CATALOG_VERSION",
    "CORE",
    "CURATE",
    "EARLIER_PLACEMENTS",
    "OPERATE",
    "PROFILES",
    "PROFILE_NAMES",
    "TOOL_PROFILE",
    "ResolvedTools",
    "placement",
    "production_tool_tiers",
    "resolve",
]
