"""resources/list and resources/templates/list advertise only what the caller may read (#657)."""

from __future__ import annotations

import pytest

from menhir.api import mcp_remote
from menhir.mcp import contracts
from menhir.mcp.resources import RESOURCE_TYPES, register_memory_resources

pytestmark = pytest.mark.unit

RECALL_URIS = {
    "memory://recent",
    "memory://scope/{scope}",
    "memory://search/{term}",
    "memory://type/{memory_type}",
}
ALL_URIS = {t.uri for t in RESOURCE_TYPES}


@pytest.fixture
def listed(monkeypatch):
    async def _listed(
        *,
        tier: str = "operator",
        policy: frozenset[str] | None = None,
        env: frozenset[str] = frozenset(),
        pin: str = "",
    ) -> set[str]:
        monkeypatch.setattr(contracts, "get_request_tier", lambda: tier)
        monkeypatch.setattr(contracts, "get_request_tool_allowlist", lambda: policy)
        monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: env)
        monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: pin)
        server = mcp_remote.TierFilteredFastMCP("menhir-test")
        register_memory_resources(server)
        resources = await server.list_resources()
        templates = await server.list_resource_templates()
        return {str(r.uri) for r in resources} | {t.uriTemplate for t in templates}

    return _listed


@pytest.mark.anyio
async def test_an_unrestricted_client_lists_every_resource_and_template(listed) -> None:
    assert await listed() == ALL_URIS


@pytest.mark.anyio
async def test_a_recall_only_policy_lists_only_recall_resources(listed) -> None:
    assert await listed(policy=frozenset({"recall_memories"})) == RECALL_URIS


@pytest.mark.anyio
async def test_a_recall_only_env_allowlist_lists_only_recall_resources(listed) -> None:
    assert await listed(env=frozenset({"recall_memories"})) == RECALL_URIS


@pytest.mark.anyio
async def test_an_empty_bound_policy_lists_nothing(listed) -> None:
    assert await listed(policy=frozenset()) == set()


@pytest.mark.anyio
async def test_a_readonly_tier_hides_the_operator_system_resources(listed) -> None:
    uris = await listed(tier="readonly")
    assert "memory://system/dependency-health" not in uris
    assert "memory://system/metadata" not in uris
    assert "memory://recent" in uris


@pytest.mark.anyio
async def test_a_pinned_client_does_not_see_system_wide_resources(listed) -> None:
    uris = await listed(pin="silo-a")
    hidden = {t.uri for t in RESOURCE_TYPES if t.refuse_when_pinned}
    assert hidden
    assert not (uris & hidden)
    assert uris == ALL_URIS - hidden


@pytest.mark.anyio
async def test_an_entry_with_no_resource_class_is_dropped(monkeypatch, listed) -> None:
    monkeypatch.setattr(mcp_remote, "_resource_type_by_uri", lambda: {})
    assert await listed() == set()


def test_the_census_every_resource_type_is_visible_to_an_unrestricted_operator(monkeypatch) -> None:
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "operator")
    monkeypatch.setattr(contracts, "get_request_tool_allowlist", lambda: None)
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "")
    mapping = mcp_remote._resource_type_by_uri()
    assert set(mapping) == ALL_URIS
    assert all(contracts.resource_listed_for_request(t) for t in RESOURCE_TYPES)
