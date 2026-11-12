"""#476: delete_memories erases a capped batch exactly as delete_memory erases one id.

Drives the tool endpoints with a fake backend, so only the batch contract is under test: the cap,
up-front validation, the default preview, the per-id ownership guard, keep-going on failure and
the time budget. The real erase is exercised in test_delete_memories_online.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.mcp.tools.ingest import delete_memories as batch_module
from menhir.mcp.tools.ingest.delete_memories import MAX_IDS, DeleteMemoriesTool
from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool, describe_erase
from menhir.mcp.tools.ops.list_namespace_memories import ListNamespaceMemoriesTool

pytestmark = [pytest.mark.unit]

_SRC = Path(__file__).resolve().parents[1] / "src" / "menhir"


def _ids(n: int) -> list[str]:
    return [str(uuid.UUID(int=i + 1)) for i in range(n)]


class _Backend:
    """Records every erase. ``owners`` maps an id to the namespace it lives in (absent = gone)."""

    def __init__(self, owners: dict[str, str] | None = None,
                 outcomes: dict[str, Any] | None = None) -> None:
        self.owners = owners or {}
        self.outcomes = outcomes or {}
        self.erased: list[tuple[str, str | None]] = []

    async def fetch_memory_by_uuid(self, node_uuid: str, namespace: str | None = None):
        owner = self.owners.get(node_uuid)
        if owner is None or (namespace is not None and owner != namespace):
            return None
        return {"uuid": node_uuid, "namespace": owner}

    async def erase_memory(self, node_uuid: str, namespace: str | None = None) -> dict:
        self.erased.append((node_uuid, namespace))
        outcome = self.outcomes.get(node_uuid, {"reason": "erased", "purged": {}})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _call(monkeypatch, backend: _Backend, **kwargs: Any) -> dict:
    monkeypatch.setattr(DeleteMemoriesTool, "get_backend", lambda self: backend)
    return json.loads(asyncio.run(DeleteMemoriesTool().endpoint(**kwargs)))


def test_over_the_cap_is_refused_with_nothing_run(monkeypatch) -> None:
    """Fails if a 101-id call erases anything or is silently truncated to 100."""
    backend = _Backend()
    result = _call(monkeypatch, backend, node_uuids=_ids(MAX_IDS + 1), dry_run=False)
    assert "exceeds the limit of 100" in result["error"]
    assert backend.erased == []


def test_exactly_the_cap_runs(monkeypatch) -> None:
    backend = _Backend()
    result = _call(monkeypatch, backend, node_uuids=_ids(MAX_IDS), dry_run=False)
    assert len(backend.erased) == MAX_IDS
    assert result["totals"] == {"erased": MAX_IDS}


def test_one_invalid_id_refuses_the_whole_call(monkeypatch) -> None:
    """Fails if the valid ids run when another id in the same call is a short/partial id."""
    backend = _Backend()
    result = _call(monkeypatch, backend, node_uuids=[*_ids(2), "e"], dry_run=False)
    assert result["invalid"] == ["'e'"]
    assert backend.erased == []


def test_empty_list_is_refused(monkeypatch) -> None:
    backend = _Backend()
    assert "no ids" in _call(monkeypatch, backend, node_uuids=[], dry_run=False)["error"]


def test_default_is_a_preview_that_erases_nothing(monkeypatch) -> None:
    """Fails if omitting dry_run erases (the default must be the preview)."""
    ids = _ids(2)
    backend = _Backend(owners={ids[0]: "default"})
    result = _call(monkeypatch, backend, node_uuids=ids)
    assert result["dry_run"] is True
    assert backend.erased == []
    found = {r["id"]: r["found_as"] for r in result["results"]}
    assert found == {ids[0]: "memory", ids[1]: "not_a_memory_node"}


def test_duplicates_and_case_are_collapsed(monkeypatch) -> None:
    one = _ids(1)[0]
    backend = _Backend()
    result = _call(monkeypatch, backend, node_uuids=[one, one.upper(), f" {one} "], dry_run=False)
    assert backend.erased == [(one, None)]
    assert result["requested"] == 3 and result["distinct"] == 1


def test_an_id_in_another_namespace_is_refused_and_the_rest_run(monkeypatch) -> None:
    """Fails if the batch erases an id proven to belong to another silo (the delete_memory guard)."""
    mine, theirs, gone = _ids(3)
    backend = _Backend(owners={mine: "default", theirs: "other"})
    result = _call(monkeypatch, backend, node_uuids=[mine, theirs, gone], namespace="default",
                   dry_run=False)
    assert backend.erased == [(mine, "default"), (gone, "default")]
    reasons = {r["id"]: r["reason"] for r in result["results"]}
    assert reasons[theirs] == "outside_namespace"
    # Absent from the graph still proceeds: graph_already_absent is a supported erase path.
    assert reasons[gone] == "erased"


def test_a_failing_id_does_not_stop_the_batch(monkeypatch) -> None:
    first, bad, last = _ids(3)
    backend = _Backend(outcomes={bad: PermissionError("pinned elsewhere")})
    result = _call(monkeypatch, backend, node_uuids=[first, bad, last], dry_run=False)
    assert [e[0] for e in backend.erased] == [first, bad, last]
    reasons = {r["id"]: r["reason"] for r in result["results"]}
    assert reasons == {first: "erased", bad: "error", last: "erased"}
    assert "may not have completed" in result["results"][1]["message"]


def test_messages_match_delete_memory(monkeypatch) -> None:
    """Fails if the batch words an outcome differently from delete_memory for the same outcome."""
    target = _ids(1)[0]
    outcome = {"reason": "erased_incomplete", "derived": {"unknown_provenance": 2}}
    backend = _Backend(outcomes={target: outcome})
    result = _call(monkeypatch, backend, node_uuids=[target], dry_run=False)
    assert result["results"][0]["message"] == describe_erase(target, outcome)


def test_time_budget_reports_the_rest_as_not_run(monkeypatch) -> None:
    """Fails if a batch past its budget keeps erasing or drops the unattempted ids silently."""
    ids = _ids(4)
    from types import SimpleNamespace

    # deadline = 0 + budget; ids 1-2 start inside it, id 3 finds it spent.
    clock = iter([0.0, 0.0, 1.0, 1000.0])
    monkeypatch.setattr(batch_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    backend = _Backend()
    result = _call(monkeypatch, backend, node_uuids=ids, dry_run=False)
    assert [e[0] for e in backend.erased] == ids[:2]
    assert [r["reason"] for r in result["results"]] == ["erased", "erased", "not_run", "not_run"]


def test_batch_endpoint_carries_namespace_and_the_ownership_lookup() -> None:
    """The ET-002 census for delete_memories: the pin must reach it and it must look before erasing."""
    for fname, cls_name in (("delete_memories.py", "DeleteMemoriesTool"),
                            ("delete_memory.py", "DeleteMemoryTool")):
        tree = ast.parse((_SRC / "mcp/tools/ingest" / fname).read_text(encoding="utf-8"))
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
        endpoint = next(n for n in cls.body
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "endpoint")
        assert "namespace" in [a.arg for a in endpoint.args.args]
        assert any(isinstance(c, ast.Call) and getattr(c.func, "attr", None)
                   == "fetch_memory_by_uuid" for c in ast.walk(endpoint))


def test_tiers_and_hosted_access() -> None:
    """Owner decisions: both operator tier; delete_memories is not hosted-denied like delete_namespace."""
    from menhir.access_contract import OPERATOR_DENIED_TOOLS

    assert DeleteMemoriesTool.required_tier == "operator"
    assert ListNamespaceMemoriesTool.required_tier == "operator"
    assert ListNamespaceMemoriesTool.read_only_hint is True
    assert DeleteMemoryTool.required_tier == "operator"
    assert "delete_memories" not in OPERATOR_DENIED_TOOLS
    assert "list_namespace_memories" not in OPERATOR_DENIED_TOOLS
    assert "delete_namespace" in OPERATOR_DENIED_TOOLS


def test_listing_op_is_operator_on_the_internal_dispatch() -> None:
    from menhir.api.routes_support import _BACKEND_METHODS, _OP_TIER_OPERATOR

    assert "list_namespace_records" in _BACKEND_METHODS
    assert "list_namespace_records" in _OP_TIER_OPERATOR


class _Graph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_namespace_records(self, namespace, **kwargs):
        self.calls.append((namespace, kwargs))
        return [{"id": f"id-{i}"} for i in range(kwargs["limit"])]

    def delete_namespace(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("delete_namespace must not reach the graph for 'default'")


class _Runtime(RuntimeProviderDataOpsMixin):
    def __init__(self) -> None:
        self.built = type("B", (), {"graph_adapter": _Graph()})()

    async def _off_loop(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.mark.parametrize("spelling", ["default", " default ", "", "   "])
def test_delete_namespace_refuses_every_default_spelling(spelling: str) -> None:
    """Fails if any spelling of the default silo (including ' default ') gets past the refusal."""
    runtime = _Runtime()
    with pytest.raises(ValueError, match="refusing to delete the default/shared namespace"):
        asyncio.run(runtime.delete_namespace(spelling, force=True))


def test_listing_clamps_the_page_and_forces_the_pin(monkeypatch) -> None:
    import menhir.core.backend_runtime_data_ops as ops

    runtime = _Runtime()
    page = asyncio.run(runtime.list_namespace_records("default", limit=500))
    assert len(page["items"]) == 100 and page["next_after_id"] == "id-99"
    monkeypatch.setattr(ops, "pinned_namespace", lambda: "tenant-a")
    asyncio.run(runtime.list_namespace_records("default", limit=5))
    assert runtime.built.graph_adapter.calls[-1][0] == "tenant-a"


def test_listing_tool_rejects_an_unknown_kind() -> None:
    result = json.loads(asyncio.run(ListNamespaceMemoriesTool().endpoint(kind="entity")))
    assert "unknown kind" in result["error"]
