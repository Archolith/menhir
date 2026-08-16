"""MCP resource tier gate (resources now enforce the same tier as tools).

``BaseJsonResource`` previously served every read with no tier check while
``BaseTool.execute`` enforced four gates. The critical fix here is the tier gate:
an unknown/malformed tier string (which ``_tier_allows`` maps to -1) must refuse
reads, a valid tier must pass, and a resource that raises its own bar must be
enforced. The regression guard (test 3) proves the gate is skipped when no tier
is bound, preserving the unauthenticated/internal path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from menhir.mcp import contracts
from menhir.mcp.contracts import BaseJsonResource


class _StubResource(BaseJsonResource):
    uri = "memory://test/stub"
    name = "test-stub"
    description = "Test double resource."

    async def endpoint(self) -> dict[str, Any]:  # noqa: D102
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        return {"ok": True, "value": 42}


class _OperatorResource(_StubResource):
    uri = "memory://test/operator"
    name = "test-operator"
    required_tier = "operator"


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


@pytest.mark.unit
def test_unknown_tier_refuses_resource_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "bogus")
    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)
    assert payload["ok"] is False
    assert "error" in payload
    assert "cannot read" in payload["error"]["message"]
    assert "bogus" in payload["error"]["message"]


@pytest.mark.unit
def test_valid_tier_serves_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["value"] == 42


@pytest.mark.unit
def test_no_tier_preserves_existing_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gate is skipped when no tier is bound (unauthenticated/internal path).
    monkeypatch.setattr(contracts, "get_request_tier", lambda: None)
    result = asyncio.run(_StubResource().execute())
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["value"] == 42


@pytest.mark.unit
def test_resource_that_raises_its_bar_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    # A resource declaring a higher required_tier is refused for a lower tier,
    # proving the attribute is actually consulted rather than hardcoded.
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    result = asyncio.run(_OperatorResource().execute())
    payload = _payload(result)
    assert payload["ok"] is False
    assert "error" in payload
    assert "requires 'operator'" in payload["error"]["message"]
