"""Counterexample tests for HIGH wave 4: the REST pin bypass (CF-30) and the unscoped
tenant reads/mutations behind CF-33 (CF-216, CF-217).

Each test reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


class _RecordingNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None):
        self.calls.append((query, params or {}))
        return list(self._rows)


# ---------------------------------------------------------------------------
# CF-30 -- REST ignored the namespace pin that MCP enforces
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, header_ns: str = "") -> None:
        self.headers = {"x-menhir-namespace": header_ns} if header_ns else {}


def test_cf30_pin_overrides_the_request_body(monkeypatch) -> None:
    """A credential restricted to one namespace through MCP reached every namespace through
    REST by putting one in the request body. Same client, same server-side policy, one
    transport enforcing it."""
    from menhir.api import routes_support

    monkeypatch.setattr(routes_support, "get_pinned_namespace", lambda: "tenant-a")
    assert routes_support._resolve_namespace(_Req(), "tenant-b") == "tenant-a"


def test_cf30_pin_overrides_the_header(monkeypatch) -> None:
    from menhir.api import routes_support

    monkeypatch.setattr(routes_support, "get_pinned_namespace", lambda: "tenant-a")
    assert routes_support._resolve_namespace(_Req("tenant-b"), None) == "tenant-a"


def test_cf30_pin_applies_when_the_caller_omits_the_namespace(monkeypatch) -> None:
    """The pin's stated guarantee covers omission, not just substitution: a small model
    'cannot escape it, whether by passing another namespace or by omitting the argument
    entirely'."""
    from menhir.api import routes_support

    monkeypatch.setattr(routes_support, "get_pinned_namespace", lambda: "tenant-a")
    assert routes_support._resolve_namespace(_Req(), None) == "tenant-a"


def test_cf30_unpinned_client_is_unaffected(monkeypatch) -> None:
    """Default behaviour must be byte-identical for the unpinned case."""
    from menhir.api import routes_support

    monkeypatch.setattr(routes_support, "get_pinned_namespace", lambda: "")
    assert routes_support._resolve_namespace(_Req(), "tenant-b") == "tenant-b"
    assert routes_support._resolve_namespace(_Req("tenant-c"), None) == "tenant-c"
    assert routes_support._resolve_namespace(_Req(), None) is None


def test_cf30_every_rest_route_resolves_its_namespace() -> None:
    """The invariant, not the instance. `bootstrap_context` passed `body.namespace` straight
    through and so was exempt from the pin even after the resolver itself was fixed -- exactly
    the hole a per-site patch leaves behind."""
    tree = ast.parse((_SRC / "api/routes.py").read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n

    def enclosing(node: ast.AST) -> ast.AST | None:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(cur)
        return None

    offenders: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "namespace":
            rendered = ast.unparse(node.value)
            if "_resolve_namespace" in rendered or "resolved" in rendered:
                continue
            fn = enclosing(node)
            if fn is not None:
                offenders.setdefault(fn.name, set()).add(rendered)
    assert offenders == {}


# ---------------------------------------------------------------------------
# CF-216 -- read_flagged_memories returned every tenant's flagged content
# ---------------------------------------------------------------------------


def _flagged_call(namespace: str | None) -> tuple[str, dict[str, Any]]:
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    neo4j = _RecordingNeo4j()
    MemoryQueryRepository(neo4j).fetch_flagged_memories(limit=10, namespace=namespace)
    return neo4j.calls[0]


def test_cf216_a_named_namespace_scopes_the_flagged_read() -> None:
    """readonly tier, returns MEMORY_RETURN_FIELDS (content and summary), and the workspace
    convention is that agents call this at the start of every session."""
    query, params = _flagged_call("tenant-a")
    assert "n.group_id IN $group_ids" in query
    assert params["group_ids"] == ["tenant-a"]


def test_cf216_default_namespace_maps_to_the_empty_group_id() -> None:
    _query, params = _flagged_call("default")
    assert params["group_ids"] == [""]


def test_cf216_unscoped_read_is_unchanged() -> None:
    """Isolation is opt-in: an unspecified namespace must not add a tenant parameter/filter.

    The shared View lifecycle predicate may still mention ``group_id`` to prove that a View and
    each of its exact incoming evidence relationships belong to the same canonical tenant.
    """
    query, params = _flagged_call(None)
    assert "$group_ids" not in query
    assert "group_ids" not in params


def test_cf216_bootstrap_version_is_scoped_with_the_rows_it_describes() -> None:
    """The fingerprint gates the bootstrap receipt. Computing it over every tenant's rows
    while returning one tenant's rows would make the gate answer a different question than
    the read."""
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    neo4j = _RecordingNeo4j(rows=[{"uuids": []}])
    MemoryQueryRepository(neo4j).fetch_flagged_memory_bootstrap_version(namespace="tenant-a")
    query, params = neo4j.calls[0]
    assert "n.group_id IN $group_ids" in query
    assert params["group_ids"] == ["tenant-a"]


def test_cf216_tool_declares_namespace_so_the_pin_can_reach_it() -> None:
    """_apply_pinned_namespace is pure signature introspection: without the parameter no pin
    can ever constrain this tool, however the server is configured."""
    tree = ast.parse(
        (_SRC / "mcp/tools/recall/read_flagged_memories.py").read_text(encoding="utf-8")
    )
    cls = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ReadFlaggedMemoriesTool"
    )
    endpoint = next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"
    )
    assert "namespace" in [a.arg for a in endpoint.args.args + endpoint.args.kwonlyargs]


# ---------------------------------------------------------------------------
# CF-217 -- close_stale_todos read and MUTATED every tenant's todos
# ---------------------------------------------------------------------------


def _close_stale_call(namespace: str | None, dry_run: bool = True):
    from menhir.infrastructure.todo_repository import TodoRepository

    neo4j = _RecordingNeo4j()
    repo = TodoRepository(neo4j)
    repo.close_stale_todos(older_than_days=60, dry_run=dry_run, namespace=namespace)
    return neo4j.calls


def test_cf217_a_named_namespace_scopes_the_stale_scan() -> None:
    """agent tier, returns n.content in the preview, and then CLOSES the rows it found --
    a cross-tenant read and a cross-tenant mutation in one call."""
    query, params = _close_stale_call("tenant-a")[0]
    assert "n.namespace = $namespace" in query
    assert params["namespace"] == "tenant-a"


def test_cf217_scoping_does_not_widen_to_the_shared_default_bucket() -> None:
    """This file's READ idiom is requested-plus-default, which is a convenience for reads.
    A bulk mutation must not close the shared bucket's todos as a side effect of tidying
    your own, so this one matches exactly."""
    _query, params = _close_stale_call("tenant-a")[0]
    ns = params["namespace"]
    assert ns == "tenant-a"
    assert not isinstance(ns, list)


def test_cf217_unscoped_call_is_unchanged() -> None:
    query, params = _close_stale_call(None)[0]
    assert "namespace" not in query
    assert "namespace" not in params


def test_cf217_the_pin_actually_forces_the_namespace_on_this_tool() -> None:
    """DECLARING the parameter is not the same as the pin REACHING it (trap T17).

    The test below asserts the signature, which is what `_apply_pinned_namespace` introspects --
    but a signature check passes just as well if the forcing never runs for this tool. Since the
    whole containment argument for CF-217 is "an unpinned sweep is the opt-in contract, a pinned
    client is forced", assert the forcing on the real tool object, not on a test double.

    Both directions matter: a pinned client that OMITS the argument (the small-model case the pin
    exists for) and one that passes a different silo.
    """
    from menhir.mcp import contracts as _contracts
    from menhir.mcp.tools.ops.close_stale_todos import CloseStaleTodosTool

    original = _contracts.get_pinned_namespace
    _contracts.get_pinned_namespace = lambda: "tenant-a"
    try:
        tool = CloseStaleTodosTool()
        omitted = tool._apply_pinned_namespace({"older_than_days": 60, "dry_run": False})
        assert omitted["namespace"] == "tenant-a", "pinned client's omitted namespace not supplied"

        overridden = tool._apply_pinned_namespace({"dry_run": False, "namespace": "tenant-b"})
        assert overridden["namespace"] == "tenant-a", "pinned client escaped its silo"
    finally:
        _contracts.get_pinned_namespace = original


def test_cf217_an_unpinned_client_is_still_unscoped_by_design() -> None:
    """THE RESIDUAL, PINNED SO IT IS NOT MISREAD AS CLOSED. CF-217's fix scopes the query when a
    namespace arrives; it does not make one arrive. An unpinned agent-tier caller still sweeps
    every silo, which is the opt-in isolation contract in `domain/namespace.py`, not an oversight.

    Whether that contract is right is CF-127's question, not this entry's."""
    from menhir.mcp import contracts as _contracts
    from menhir.mcp.tools.ops.close_stale_todos import CloseStaleTodosTool

    original = _contracts.get_pinned_namespace
    _contracts.get_pinned_namespace = lambda: ""
    try:
        tool = CloseStaleTodosTool()
        assert "namespace" not in tool._apply_pinned_namespace({"dry_run": False})
    finally:
        _contracts.get_pinned_namespace = original

    query, params = _close_stale_call(None)[0]
    assert "n.namespace" not in query and "namespace" not in params


def test_cf217_tool_declares_namespace_so_the_pin_can_reach_it() -> None:
    tree = ast.parse(
        (_SRC / "mcp/tools/ops/close_stale_todos.py").read_text(encoding="utf-8")
    )
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "CloseStaleTodosTool"
    )
    endpoint = next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"
    )
    assert "namespace" in [a.arg for a in endpoint.args.args + endpoint.args.kwonlyargs]


# ---------------------------------------------------------------------------
# ET-002 -- a UUID is an identifier, not proof of tenancy
# ---------------------------------------------------------------------------

_UUID_MUTATORS = {
    "DeleteMemoryTool": ("delete_memory.py", "node_uuid"),
    "FlagMemoryTool": ("flag_memory.py", "node_uuid"),
    "UnflagMemoryTool": ("unflag_memory.py", "node_uuid"),
    "PromoteMemoryTool": ("promote_memory.py", "node_uuid"),
    "CloseMemoryTool": ("close_memory.py", "uuid"),
}


@pytest.mark.parametrize("cls_name", sorted(_UUID_MUTATORS))
def test_et002_every_uuid_mutator_declares_namespace_and_guards(cls_name: str) -> None:
    """The pinned-client regression ET-002 asks for, as a structural invariant.

    `unflag_memory` is the sharpest of these: it runs at the DEFAULT agent tier and removes
    another tenant's retention protection, returning their data to ordinary lifecycle decay.
    No operator tier is needed for that chain.
    """
    fname, _param = _UUID_MUTATORS[cls_name]
    tree = ast.parse((_SRC / "mcp/tools/ingest" / fname).read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
    endpoint = next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"
    )

    params = [a.arg for a in endpoint.args.args + endpoint.args.kwonlyargs]
    assert "namespace" in params, f"{cls_name}: pin cannot reach an endpoint that omits it"

    guarded = any(
        isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "fetch_memory_by_uuid"
        for c in ast.walk(endpoint)
    )
    assert guarded, f"{cls_name}: no ownership check before mutating"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name,cls_name,kwargs",
    [
        ("unflag_memory", "UnflagMemoryTool", {"node_uuid": "u1"}),
        ("promote_memory", "PromoteMemoryTool", {"node_uuid": "u1"}),
        ("close_memory", "CloseMemoryTool", {"uuid": "u1"}),
    ],
)
async def test_et002_pinned_caller_refused_on_a_foreign_node(
    module_name: str, cls_name: str, kwargs: dict[str, Any]
) -> None:
    import importlib

    module = importlib.import_module(f"menhir.mcp.tools.ingest.{module_name}")
    tool = getattr(module, cls_name)()

    mutated: list[str] = []

    class _Backend:
        async def fetch_memory_by_uuid(self, node_uuid, *, namespace=None):
            # The node exists, but it belongs to tenant-b.
            return None if namespace not in (None, "tenant-b") else {"uuid": node_uuid}

        async def unflag_memory(self, node_uuid):
            mutated.append(node_uuid)
            return True

        async def promote_memory(self, node_uuid):
            mutated.append(node_uuid)
            return True

        async def complete_temporal(self, uuid):
            mutated.append(uuid)
            return True

    tool.get_backend = lambda: _Backend()  # type: ignore[method-assign]
    result = await tool.endpoint(namespace="tenant-a", **kwargs)

    assert "Refused" in result
    assert mutated == []
