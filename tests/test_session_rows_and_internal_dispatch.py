"""Isolation follow-ups to the resource gate.

1. SESSION scope is a relevance scope, not access control: the memory resources return
   SESSION rows from every session in the namespace (docs/your-data.md).
2. The internal backend dispatch refuses what it cannot bound for a restricted client: every
   operation for a client with a tool allowlist, and, for a pinned client, every operation
   that takes no `namespace` other than the deployment-wide stats.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from menhir.api import routes_handlers
from menhir.api.routes_handlers import PINNED_GLOBAL_OPERATIONS, backend_invoke_impl
from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
from menhir.core.backend_impl import RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.backend_scrub import wrap_backend
from menhir.mcp import contracts
from menhir.mcp import resources as resources_module
from menhir.mcp.contracts import BaseJsonResource
from menhir.mcp.resources import (
    MemoriesByScopeResource,
    MemoriesByTypeResource,
    MemoryByUuidResource,
    RecentMemoriesResource,
)

pytestmark = pytest.mark.unit

MINE = "sess-mine"
UUID = "11111111-1111-4111-8111-111111111111"


def _row(uuid: str, scope: str, session_id: str | None) -> dict[str, Any]:
    return {"uuid": uuid, "scope": scope, "session_id": session_id, "labels": ["Entity"]}


ROWS = [
    _row("own-session", "SESSION", MINE),
    _row("other-session", "SESSION", "sess-other"),
    _row("unstamped-session", "SESSION", None),
    _row("persistent", "PERSISTENT", "sess-other"),
]


class _RowBackend:
    async def fetch_recent_memories(self, limit=20, namespace=None):
        return [dict(r) for r in ROWS]

    async def fetch_memories_by_scope(self, scope, limit=20, *, namespace=None):
        return [dict(r) for r in ROWS]

    async def fetch_memories_by_type(self, memory_type, limit=20, *, namespace=None):
        return [dict(r) for r in ROWS]

    def __init__(self, single: dict[str, Any] | None = None) -> None:
        self.single = single

    async def fetch_memory_by_uuid(self, node_uuid, *, namespace=None):
        return dict(self.single) if self.single else None

    async def fetch_episode_processing(self, episode_uuid):
        return None


@pytest.fixture
def caller(monkeypatch):
    def _bind(session_id: str | None, backend: _RowBackend | None = None) -> _RowBackend:
        backend = backend or _RowBackend()
        monkeypatch.setattr(BaseJsonResource, "get_backend", lambda _self: backend)
        monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)
        monkeypatch.setattr(contracts, "get_request_tier", lambda: "operator")
        monkeypatch.setattr(contracts, "get_request_tool_allowlist", lambda: None)
        monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
        monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "")
        monkeypatch.setattr(contracts, "require_trusted_client_identity", lambda: None)
        monkeypatch.setattr(
            resources_module, "get_mcp_session", lambda: SimpleNamespace(session_id=session_id)
        )
        return backend

    return _bind


def _read(resource, *args) -> dict[str, Any]:
    return json.loads(asyncio.run(resource.execute(*args)))


# ---------------------------------------------------------------------------- SESSION rows


@pytest.mark.parametrize(
    "resource,args",
    [
        (RecentMemoriesResource(), ()),
        (MemoriesByScopeResource(), ("SESSION",)),
        (MemoriesByTypeResource(), ("Fact",)),
    ],
    ids=["recent", "scope", "type"],
)
def test_listing_resources_return_session_rows_from_every_session(caller, resource, args) -> None:
    """Decided 2026-11-14: SESSION is not access control; namespaces are."""
    caller(MINE)
    uuids = [item["uuid"] for item in _read(resource, *args)["items"]]
    assert uuids == [row["uuid"] for row in ROWS]


def test_node_resource_returns_another_sessions_session_row(caller) -> None:
    caller(MINE, _RowBackend(single=_row(UUID, "SESSION", "sess-other")))
    assert _read(MemoryByUuidResource(), UUID)["found"] is True


# ---------------------------------------------------------------------------- internal dispatch


class _Recorder:
    """Backs every protocol operation with a recorder sharing the protocol's signature."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        proto = getattr(MemoryBackend, name)

        async def _call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return []

        _call.__signature__ = inspect.signature(proto)  # type: ignore[attr-defined]
        return _call


def _invoke(backend: _Recorder, operation: str, body: dict[str, Any] | None = None):
    return asyncio.run(
        backend_invoke_impl(
            object(),
            operation,
            body or {},
            backend_methods=_BACKEND_METHODS,
            required_tier_for_operation=lambda _op: "readonly",
            require_tier=lambda _tier: None,
            try_record_destructive_op_rest=lambda _op: None,
            resolve_caller_session=lambda _req: SimpleNamespace(session_id="s"),
            get_backend=lambda _req: backend,
            drain_background_errors=lambda _sid: [],
            logger=logging.getLogger("menhir.test.dispatch"),
        )
    )


@pytest.fixture
def client(monkeypatch):
    def _bind(*, pin: str = "", policy=None, env=frozenset()) -> _Recorder:
        monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: pin)
        monkeypatch.setattr(routes_handlers, "get_request_tool_allowlist", lambda: policy)
        monkeypatch.setattr(routes_handlers, "get_client_tool_allowlist", lambda: env)
        return _Recorder()

    return _bind


def _no_namespace_operations() -> list[str]:
    return sorted(
        op
        for op in _BACKEND_METHODS
        if "namespace" not in inspect.signature(getattr(MemoryBackend, op)).parameters
    )


@pytest.mark.parametrize(
    "policy,env",
    [(frozenset({"recall_memories"}), frozenset()), (None, frozenset({"recall_memories"}))],
    ids=["policy", "env"],
)
def test_an_allowlisted_client_is_refused_every_operation(client, policy, env) -> None:
    for operation in ("recall", "fetch_recent_memories", "get_queue_depth", "delete_memory"):
        backend = client(policy=policy, env=env)
        with pytest.raises(HTTPException) as err:
            _invoke(backend, operation)
        assert err.value.status_code == 403 and "tool allowlist" in err.value.detail
        assert backend.calls == []


def test_a_pinned_client_is_refused_every_unscoped_operation_but_the_stats() -> None:
    refused = [op for op in _no_namespace_operations() if op not in PINNED_GLOBAL_OPERATIONS]
    assert "fetch_recent_lifecycle_events" in refused and "delete_memory" in refused
    assert len(refused) >= 40


@pytest.mark.parametrize("operation", _no_namespace_operations())
def test_pinned_dispatch_of_an_unscoped_operation(client, operation) -> None:
    backend = client(pin="silo-a")
    if operation in PINNED_GLOBAL_OPERATIONS:
        _invoke(backend, operation)
        assert [name for name, _ in backend.calls] == [operation]
        return
    with pytest.raises(HTTPException) as err:
        _invoke(backend, operation)
    assert err.value.status_code == 403 and "pinned" in err.value.detail
    assert backend.calls == []


def test_a_pinned_client_keeps_scoped_operations_with_the_pin_forced(client) -> None:
    backend = client(pin="silo-a")
    _invoke(backend, "fetch_recent_memories", {"limit": 5, "namespace": "silo-b"})
    assert backend.calls == [("fetch_recent_memories", {"limit": 5, "namespace": "silo-a"})]


def test_an_unrestricted_client_keeps_every_operation(client) -> None:
    backend = client()
    _invoke(backend, "fetch_recent_lifecycle_events", {"limit": 5})
    assert [name for name, _ in backend.calls] == ["fetch_recent_lifecycle_events"]


def test_pinned_global_operations_are_readonly_unscoped_dispatch_operations() -> None:
    for operation in PINNED_GLOBAL_OPERATIONS:
        assert operation in _BACKEND_METHODS
        assert _required_tier_for_operation(operation) == "readonly"
        assert "namespace" not in inspect.signature(getattr(MemoryBackend, operation)).parameters


def test_the_real_wrapped_backend_exposes_namespace_to_the_check() -> None:
    """The route inspects ScrubbingBackend(RuntimeProvider) methods; wraps must keep signatures."""
    wrapped = wrap_backend(RuntimeProvider(SimpleNamespace(), process_session=None))
    assert "namespace" in inspect.signature(wrapped.fetch_recent_memories).parameters
    assert "namespace" not in inspect.signature(wrapped.fetch_recent_lifecycle_events).parameters


# ---------------------------------------------------------------------------- deprecation evidence


@pytest.mark.parametrize("pin,admitted", [("silo-a", False), ("", True)])
def test_a_refused_deprecated_call_is_not_counted_as_admitted(
    client, monkeypatch, pin, admitted
) -> None:
    """write_project_structure takes no namespace, so a pinned client is refused it."""
    from menhir.api import routes_support

    recorded: list[bool] = []
    monkeypatch.setattr(
        routes_support,
        "record_deprecated_operation_call",
        lambda _op, *, admitted: recorded.append(admitted),
    )
    backend = client(pin=pin)
    if admitted:
        _invoke(backend, "write_project_structure", {"scan": {}})
    else:
        with pytest.raises(HTTPException):
            _invoke(backend, "write_project_structure", {"scan": {}})
    assert recorded == [admitted]
