"""Named REST routes apply the client tool allowlist (#658).

Each route maps to the MCP tool that makes the same service call. A client restricted to some
tools is refused every route whose tool it lacks, and every route with no mapping.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from menhir.api import route_tools
from menhir.api.mcp_remote import production_tool_catalog
from menhir.api.route_tools import (
    EXEMPT_ROUTES,
    INTERNAL_DISPATCH_PATH,
    REST_ROUTE_TOOLS,
    enforce_route_tool_allowlist,
)
from menhir.api.routes import router
from menhir.mcp import contracts

pytestmark = pytest.mark.unit

RECALL_ONLY = frozenset({"recall_memories"})


def _route_keys() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


@pytest.fixture
def bind(monkeypatch):
    def _bind(policy=None, env=frozenset()) -> None:
        monkeypatch.setattr(contracts, "get_request_tool_allowlist", lambda: policy)
        monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: env)

    return _bind


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_every_route_is_mapped_or_exempt() -> None:
    covered = set(REST_ROUTE_TOOLS) | set(EXEMPT_ROUTES)
    uncovered = {
        key for key in _route_keys() if key not in covered and key[1] != INTERNAL_DISPATCH_PATH
    }
    assert not uncovered, f"routes with no tool mapping in REST_ROUTE_TOOLS: {sorted(uncovered)}"


def test_the_map_names_no_route_that_does_not_exist() -> None:
    stale = (set(REST_ROUTE_TOOLS) | set(EXEMPT_ROUTES)) - _route_keys()
    assert not stale, f"mapped routes that are not on the router: {sorted(stale)}"


def test_every_mapped_tool_is_in_the_production_catalog() -> None:
    catalog = production_tool_catalog()
    missing = {tool for tool in REST_ROUTE_TOOLS.values() if tool is not None} - catalog
    assert not missing


@pytest.mark.parametrize(
    "policy,env",
    [(RECALL_ONLY, frozenset()), (None, RECALL_ONLY)],
    ids=["policy", "env"],
)
def test_a_recall_only_client_is_refused_a_write_route(client, bind, policy, env) -> None:
    bind(policy, env)
    response = client.post("/api/memory", json={})
    assert response.status_code == 403
    assert "add_memory" in response.json()["detail"]


@pytest.mark.parametrize(
    "policy,env",
    [(RECALL_ONLY, frozenset()), (None, RECALL_ONLY)],
    ids=["policy", "env"],
)
def test_a_recall_only_client_passes_the_recall_route(client, bind, policy, env) -> None:
    bind(policy, env)
    # The empty body fails validation (422) but the allowlist dependency let it through.
    assert client.post("/api/recall", json={}).status_code != 403


def test_a_parameterized_route_resolves_by_its_template(client, bind) -> None:
    # Resolved via the matched route template, not the concrete URL: a refusal names the
    # route's tool (not "no tool mapping"), and an allowed client gets past the dependency.
    bind(RECALL_ONLY)
    refused = client.post("/api/memory/some-uuid/flag")
    assert refused.status_code == 403 and "`flag_memory`" in refused.json()["detail"]
    bind(frozenset({"flag_memory"}))
    allowed = client.post("/api/memory/some-uuid/flag")
    assert "allowlist" not in str(allowed.json().get("detail", ""))


def test_a_route_with_no_tool_is_refused_to_a_restricted_client(client, bind) -> None:
    bind(RECALL_ONLY)
    response = client.post("/api/turn-evidence", json={})
    assert response.status_code == 403
    assert "no tool mapping" in response.json()["detail"]


def test_an_empty_bound_policy_denies_everything_but_health(client, bind) -> None:
    bind(frozenset())
    assert client.post("/api/recall", json={}).status_code == 403
    assert client.get("/api/health").status_code == 200


def test_an_unrestricted_client_is_not_refused(client, bind) -> None:
    bind(None, frozenset())
    assert client.post("/api/memory", json={}).status_code != 403
    assert client.post("/api/turn-evidence", json={}).status_code != 403


@pytest.mark.parametrize("path", ["/api/health", "/api/ready"])
def test_health_and_ready_are_always_allowed(client, bind, path) -> None:
    bind(RECALL_ONLY)
    assert client.get(path).status_code != 403


@pytest.mark.anyio
async def test_the_dependency_skips_the_internal_dispatch_route(monkeypatch, bind) -> None:
    bind(RECALL_ONLY)

    class _Route:
        path = INTERNAL_DISPATCH_PATH

    class _Request:
        method = "POST"
        scope = {"route": _Route()}

    await enforce_route_tool_allowlist(_Request())  # type: ignore[arg-type]
    assert route_tools.INTERNAL_DISPATCH_PATH == INTERNAL_DISPATCH_PATH
