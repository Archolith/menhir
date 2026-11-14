"""MCP resources obey the per-client tool allowlist and the namespace pin.

A resource is readable by exactly the clients allowed its governing tool. A pinned client
gets every resource scoped to its namespace, or is refused before any backend read.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.core.backend_protocol import MemoryBackend
from menhir.mcp import contracts
from menhir.mcp import resources as resources_module
from menhir.mcp.contracts import BaseJsonResource
from menhir.mcp.resources import RESOURCE_TYPES
from menhir.mcp.tool_profiles import production_tool_tiers

pytestmark = pytest.mark.unit

PIN = "silo-a"
UUID = "11111111-1111-4111-8111-111111111111"
_ARGS = {
    "memory://node/{node_uuid}": (UUID,),
    "memory://scope/{scope}": ("PERSISTENT",),
    "memory://search/{term}": ("remembered fact",),
    "memory://type/{memory_type}": ("SEMANTIC",),
}
#: Backend reads that are deployment-wide by design: numbers and runtime config, no rows.
#: get_memory_stats shows the same operational sections to a pinned client.
DEPLOYMENT_WIDE = {
    "get_provider_config",
    "get_queue_depth",
    "get_failed_enrichment_count",
    "scheduler_status_snapshot",
    "get_max_enrichment_attempts",
}
_RETURNS: dict[str, Any] = {
    "fetch_memory_overview": {},
    "get_provider_config": {},
    "get_queue_depth": 0,
    "get_failed_enrichment_count": 0,
    "scheduler_status_snapshot": None,
    "get_max_enrichment_attempts": 3,
    "fetch_memory_by_uuid": None,
    "recall": {"results": [], "candidates_evaluated": 0},
}


class _RecordingBackend:
    """Records every backend call; methods must exist on the protocol."""

    def __init__(self, lifecycle_rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._lifecycle_rows = lifecycle_rows or []

    def __getattr__(self, name: str):
        if not hasattr(MemoryBackend, name):
            raise AttributeError(name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            bound = inspect.signature(getattr(MemoryBackend, name)).bind(None, *args, **kwargs)
            self.calls.append((name, dict(list(bound.arguments.items())[1:])))
            if name == "fetch_recent_lifecycle_events":
                return self._lifecycle_rows
            return _RETURNS.get(name, [])

        return _call


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """Bind a header-auth request at `tier`, with an optional policy/env allowlist and pin."""

    def _bind(
        *,
        tier: str = "readonly",
        policy: frozenset[str] | None = None,
        env: frozenset[str] = frozenset(),
        pin: str | None = None,
    ) -> _RecordingBackend:
        backend = _RecordingBackend()
        monkeypatch.setattr(BaseJsonResource, "get_backend", lambda _self: backend)
        monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)
        monkeypatch.setattr(contracts, "get_request_tier", lambda: tier)
        monkeypatch.setattr(contracts, "get_request_tool_allowlist", lambda: policy)
        monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: env)
        monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: pin or "")
        monkeypatch.setattr(contracts, "require_trusted_client_identity", lambda: None)
        monkeypatch.setattr(resources_module, "_neo4j_dependency_snapshot", lambda: {})
        monkeypatch.setattr(
            resources_module,
            "get_mcp_session",
            lambda: SimpleNamespace(session_id="s", user_id="u", build_id="b"),
        )
        return backend

    return _bind


def _read(resource_type: type[BaseJsonResource]) -> dict[str, Any]:
    resource = resource_type()
    return json.loads(asyncio.run(resource.execute(*_ARGS.get(resource.uri, ()))))


def _denied(payload: dict[str, Any]) -> bool:
    return payload.get("ok") is False


# ---------------------------------------------------------------------------- census


@pytest.mark.parametrize("resource_type", RESOURCE_TYPES, ids=lambda t: t.uri)
def test_every_resource_is_governed_by_a_registered_tool(resource_type) -> None:
    tiers = production_tool_tiers()
    assert resource_type.governing_tool in tiers
    # The tool admits no lower tier than the resource: tool access is the bar for the data.
    assert contracts._tier_allows(resource_type.required_tier, tiers[resource_type.governing_tool])


def test_a_resource_without_a_governing_tool_is_refused(gate) -> None:
    class _Ungoverned(BaseJsonResource):
        uri = "memory://test/ungoverned"
        name = "test-ungoverned"
        description = "no governing tool"

        async def endpoint(self) -> dict[str, Any]:
            return await self.build_payload()

        async def build_payload(self) -> dict[str, Any]:
            raise AssertionError("must be refused before the read")

    gate()
    payload = _read(_Ungoverned)
    assert _denied(payload) and "declares no governing tool" in payload["error"]["message"]


# ---------------------------------------------------------------------------- allowlist


def test_the_recall_only_probe_reads_content_resources_and_no_system_resource(gate) -> None:
    """The deploy probe's policy allows only recall_memories."""
    for resource_type in RESOURCE_TYPES:
        backend = gate(policy=frozenset({"recall_memories"}))
        payload = _read(resource_type)
        if resource_type.governing_tool == "recall_memories":
            assert not _denied(payload), resource_type.uri
        else:
            assert _denied(payload), resource_type.uri
            message = payload["error"]["message"]
            assert "client policy" in message or "requires 'operator'" in message
            assert backend.calls == []


def test_an_empty_policy_allowlist_denies_every_resource(gate) -> None:
    for resource_type in RESOURCE_TYPES:
        backend = gate(policy=frozenset())
        assert _denied(_read(resource_type)), resource_type.uri
        assert backend.calls == []


def test_the_env_allowlist_applies_only_without_a_policy(gate) -> None:
    from menhir.mcp.resources import ProcessingQueueResource, RecentMemoriesResource

    gate(env=frozenset({"recall_memories"}))
    assert not _denied(_read(RecentMemoriesResource))
    payload = _read(ProcessingQueueResource)
    assert _denied(payload) and "MENHIR_CLIENT_TOOLS" in payload["error"]["message"]

    # A bound policy decides alone, even when the env list would refuse.
    gate(policy=frozenset({"list_enrichment_queue"}), env=frozenset({"recall_memories"}))
    assert not _denied(_read(ProcessingQueueResource))


def test_an_unrestricted_client_reads_every_resource(gate) -> None:
    for resource_type in RESOURCE_TYPES:
        gate(tier="operator")
        assert not _denied(_read(resource_type)), resource_type.uri


def test_identity_is_checked_before_the_read(gate, monkeypatch) -> None:
    from menhir.mcp.resources import RecentMemoriesResource

    backend = gate()

    def _untrusted() -> None:
        raise PermissionError("unrecognized client name")

    monkeypatch.setattr(contracts, "require_trusted_client_identity", _untrusted)
    payload = _read(RecentMemoriesResource)
    assert _denied(payload) and "unrecognized client name" in payload["error"]["message"]
    assert backend.calls == []


# ---------------------------------------------------------------------------- namespace pin


@pytest.mark.parametrize("resource_type", RESOURCE_TYPES, ids=lambda t: t.uri)
def test_a_pinned_client_reads_only_its_namespace_or_is_refused(gate, resource_type) -> None:
    backend = gate(tier="operator", pin=PIN)
    payload = _read(resource_type)
    if resource_type.refuse_when_pinned:
        assert _denied(payload) and "pinned" in payload["error"]["message"]
        assert backend.calls == []
        return
    assert not _denied(payload), payload
    for name, kwargs in backend.calls:
        params = inspect.signature(getattr(MemoryBackend, name)).parameters
        if "namespace" in params:
            assert kwargs.get("namespace") == PIN, (resource_type.uri, name, kwargs)
        else:
            assert name in DEPLOYMENT_WIDE, (resource_type.uri, name)


# ---------------------------------------------------------------------------- tracebacks


@pytest.mark.parametrize("tier,withheld", [("readonly", True), ("operator", False)])
def test_lifecycle_trace_withholds_tracebacks_below_operator(
    gate, monkeypatch, tier, withheld
) -> None:
    from menhir.mcp.resources import LifecycleTraceResource

    backend = gate(tier=tier)
    backend._lifecycle_rows = [
        {"event": "failed", "details_json": json.dumps({"traceback": "Traceback: secret"})}
    ]
    # The resource reads the tier through service_access, as get_episode_trace does.
    monkeypatch.setattr(resources_module, "get_request_tier", lambda: tier)
    payload = _read(LifecycleTraceResource)
    details = payload["items"][0]["details"]
    assert ("secret" not in json.dumps(details)) is withheld


def test_node_detail_and_system_endpoints_need_more_than_recall(gate) -> None:
    """memory://node carries processing and identity fields; system/* carry endpoints."""
    from menhir.mcp.resources import (
        DependencyHealthResource,
        MemoryByUuidResource,
        SystemMetadataResource,
    )

    assert MemoryByUuidResource.governing_tool == "get_enrichment_status"
    for resource_type in (DependencyHealthResource, SystemMetadataResource):
        gate(tier="agent", policy=frozenset({"get_memory_stats"}))
        payload = _read(resource_type)
        assert _denied(payload) and "requires 'operator'" in payload["error"]["message"]
        gate(tier="operator", policy=frozenset({"get_memory_stats"}))
        assert not _denied(_read(resource_type))
