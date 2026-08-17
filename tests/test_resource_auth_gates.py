"""Regression tests for the remaining CF-16 resource authorization gates.

Resources deliberately do not mirror every BaseTool gate. Query-string auth is a
legacy tool compatibility path, so resources are refused outright rather than
being admitted through QUERY_AUTH_ALLOWED_TOOLS or given the add_memory rate
budget. MENHIR_CLIENT_TOOLS is likewise a tool-name allowlist; applying it to
resource names/URIs would silently deny every resource for restricted clients.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from menhir.mcp import contracts
from menhir.mcp.contracts import BaseJsonResource


class _StubResource(BaseJsonResource):
    uri = "memory://test/auth-gates"
    name = "test-auth-gates"
    description = "Resource authorization test double."

    async def endpoint(self) -> dict[str, Any]:  # noqa: D102
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        return {"ok": True, "value": 42}


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


@pytest.mark.unit
def test_query_string_auth_refuses_readonly_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate 1: readonly classification must not auto-admit resources to query auth."""
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: True)
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")

    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)

    assert payload["ok"] is False
    assert "query-string auth cannot read" in payload["error"]["message"]
    assert _StubResource.uri in payload["error"]["message"]


@pytest.mark.unit
def test_resource_query_auth_refusal_does_not_consume_add_memory_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate 2 is N/A: the add_memory query-auth rate limiter is tool-only and unreachable."""
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: True)
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")

    def _unexpected_budget_call(*_args: Any, **_kwargs: Any) -> tuple[int, float]:
        raise AssertionError("resource reads must never enter the add_memory rate budget")

    monkeypatch.setattr(contracts, "_consume_query_add_memory_budget", _unexpected_budget_call)

    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)

    assert payload["ok"] is False
    assert "query-string auth cannot read" in payload["error"]["message"]


@pytest.mark.unit
def test_resource_does_not_consult_tool_name_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate 4: MENHIR_CLIENT_TOOLS remains a tool-only policy surface by design."""
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")

    def _unexpected_tool_allowlist_call() -> set[str]:
        raise AssertionError("resources must not consult MENHIR_CLIENT_TOOLS")

    monkeypatch.setattr(contracts, "get_client_tool_allowlist", _unexpected_tool_allowlist_call)

    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["value"] == 42
