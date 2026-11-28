"""#890: the bridge estimate is a backend operation, so the resolve_conflict dry run can use it.

Covers the whole path with no graph: local runtime -> adapter -> repository, and the HTTP client ->
internal-dispatch route -> runtime -> repository, ending in the tool's dry run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from menhir.api.routes_handlers import PINNED_GLOBAL_OPERATIONS, backend_invoke_impl
from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
from menhir.config import MemorySettings
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.unit

OP = "estimate_bridges_for_nodes"


class _Neo4j:
    """Answers the estimate statement with per-node neighbour counts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_k: Any) -> Any:
        self.calls.append((query, dict(params or {})))
        return [{"uuid": "t1", "k": 4}, {"uuid": "t2", "k": 30}]


def _provider() -> tuple[RuntimeProvider, _Neo4j]:
    neo4j = _Neo4j()
    built = SimpleNamespace(graph_adapter=MemoryGraphAdapter(neo4j=neo4j))
    return RuntimeProvider(built, process_session=None), neo4j


def test_the_operation_is_registered_read_only_and_namespace_scoped() -> None:
    assert OP in _BACKEND_METHODS
    assert OP in dir(MemoryBackend)
    # Operator, like the resolve it previews; the tier census wants every non-read-prefixed op named.
    assert _required_tier_for_operation(OP) == "operator"
    assert OP not in PINNED_GLOBAL_OPERATIONS


def test_the_local_runtime_returns_the_repository_estimate() -> None:
    provider, neo4j = _provider()

    out = asyncio.run(provider.estimate_bridges_for_nodes(["t1", "t2"], namespace="ns1"))

    assert out == {
        "neighbour_counts": {"t1": 4, "t2": 30},
        "estimated_edges": 6,  # k=4 gives 6 pairs; t2 is over the cap and gives none
        "over_cap": ["t2"],
    }
    assert neo4j.calls[0][1]["node_uuids"] == ["t1", "t2"]
    assert neo4j.calls[0][1]["tenant_namespaces"] == ["ns1"]


def _route_client(provider: RuntimeProvider, seen: list[str]) -> BackendClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        seen.append(operation)
        result = await backend_invoke_impl(
            object(),
            operation,
            json.loads(request.content),
            backend_methods=_BACKEND_METHODS,
            required_tier_for_operation=_required_tier_for_operation,
            require_tier=lambda _tier: None,
            try_record_destructive_op_rest=lambda _op: None,
            resolve_caller_session=lambda _req: SimpleNamespace(session_id="s"),
            get_backend=lambda _req: provider,
            drain_background_errors=lambda _sid: [],
            logger=logging.getLogger("menhir.test.estimate"),
        )
        return httpx.Response(200, json=result)

    http = httpx.AsyncClient(base_url="http://menhir.test", transport=httpx.MockTransport(handler))
    return BackendClient("http://menhir.test", client=http, settings=MemorySettings())


def test_the_http_client_reaches_the_route_and_the_runtime() -> None:
    provider, neo4j = _provider()
    seen: list[str] = []
    client = _route_client(provider, seen)

    out = asyncio.run(client.estimate_bridges_for_nodes(["t1", "t2"], namespace="ns1"))

    assert seen == [OP]
    assert out["estimated_edges"] == 6 and out["over_cap"] == ["t2"]
    assert neo4j.calls[0][1]["tenant_namespaces"] == ["ns1"]


@pytest.mark.parametrize("through", ["runtime", "http"])
def test_the_dry_run_reports_an_available_estimate(through: str) -> None:
    from menhir.mcp.tools.conflict.resolve_conflict import ResolveConflictTool

    provider, _ = _provider()
    estimator = provider if through == "runtime" else _route_client(provider, [])
    backend = MagicMock(spec=["list_conflict_groups", "resolve_conflict_group", OP])
    backend.list_conflict_groups = AsyncMock(return_value=[{
        "group_id": "g1", "status": "unresolved",
        "members": [{"uuid": u, "name": u, "status": "unresolved"} for u in ("keep", "t1", "t2")],
    }])
    backend.resolve_conflict_group = AsyncMock()
    backend.estimate_bridges_for_nodes = estimator.estimate_bridges_for_nodes
    tool = ResolveConflictTool()
    tool.get_backend = MagicMock(return_value=backend)

    out = json.loads(asyncio.run(tool.endpoint(
        group_id="g1", action="replace", keep_uuid="keep", dry_run=True, namespace="ns1"
    )))

    backend.resolve_conflict_group.assert_not_called()
    assert out["bridge_estimate_available"] is True
    assert out["bridge_count_estimate"] == 6
    assert out["bridge_over_cap_hubs"] == ["t2"]
    assert out["bridge_neighbour_counts"] == {"t1": 4, "t2": 30}
