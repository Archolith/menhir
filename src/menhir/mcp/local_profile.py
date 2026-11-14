"""The MCP tool set an unauthenticated local agent gets (#439).

A keyless local install used to hand every connected agent the operator tier and so every
registered tool, including scheduler, lease, client and namespace administration. By default a
keyless agent now gets the ``core`` and ``curate`` profiles. ``MENHIR_LOOPBACK_PROFILE=operator``
restores the full tool set.

This shapes what an agent is offered; it is not a security boundary against local processes.
The backend's ``/api`` routes keep their tier, so the CLI and the explorer are unaffected, and
anything that can read the state directory can already do more than any tool (CT-002).
"""

from __future__ import annotations

from menhir.config.settings_model import LOOPBACK_PROFILES
from menhir.config.settings_model import (
    _normalize_loopback_profile as normalize_loopback_profile,
)
from menhir.mcp.tool_profiles import (
    CATALOG_VERSION,
    CORE,
    CURATE,
    production_tool_tiers,
    resolve,
)

DEFAULT_PROFILES: tuple[str, ...] = (CORE, CURATE)
#: add_candidate and review_candidate stay out of the keyless default (#431). Approving a
#: candidate turns unreviewed emitter text into recallable memory, so it stays an operator
#: (or MENHIR_LOOPBACK_PROFILE=operator) action; and with both tools, the agent that stages a
#: claim could approve it itself. Whether to allow either is an open decision.
DEFAULT_DENY: tuple[str, ...] = ("add_candidate", "review_candidate")


def loopback_tool_allowlist(
    loopback_profile: str | None,
    *,
    tool_tiers: dict[str, str] | None = None,
) -> frozenset[str] | None:
    """Tools a keyless local agent may see and call, or None for every tool."""
    if normalize_loopback_profile(loopback_profile) == "operator":
        return None
    resolved = resolve(
        profiles=DEFAULT_PROFILES,
        deny=DEFAULT_DENY,
        maximum_tier="operator",
        tool_tiers=tool_tiers if tool_tiers is not None else production_tool_tiers(),
        policy_catalog=CATALOG_VERSION,
    )
    return resolved.allowed


__all__ = [
    "DEFAULT_DENY",
    "DEFAULT_PROFILES",
    "LOOPBACK_PROFILES",
    "loopback_tool_allowlist",
    "normalize_loopback_profile",
]
