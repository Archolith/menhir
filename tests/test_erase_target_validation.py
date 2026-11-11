"""Erase-target validation and the exact-match, namespace-scoped merge_audit scrub (P2)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.domain.erase_target import canonical_uuid_needles, normalize_erase_target
from menhir.infrastructure.memory_queries import MemoryQueryRepository

U1 = "11111111-2222-3333-4444-555555555555"
U2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
U3 = "99999999-8888-7777-6666-555555555555"


@pytest.mark.parametrize("bad", ["e", "", "  ", "1111", U1[:-1], U1 + "0", U1.replace("-", ""), None, 7])
def test_normalize_rejects_non_canonical(bad):
    assert normalize_erase_target(bad) is None


def test_normalize_strips_and_lowercases():
    assert normalize_erase_target(f"  {U2.upper()}\n") == U2


def test_needles_drop_invalid_and_dedupe():
    assert canonical_uuid_needles([U1, "e", U1.upper(), "", None, U2]) == [U1, U2]


class _FakeNeo4j:
    def __init__(self, merge_audit: list[str]) -> None:
        self.merge_audit = merge_audit
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None):
        params = params or {}
        self.calls.append((query, params))
        if "SET s.merge_audit" in query:
            assert params["old"] == self.merge_audit
            self.merge_audit = params["new"]
            return [{"updated": 1}]
        return [{"uuid": "survivor", "merge_audit": list(self.merge_audit)}]


def _entry(**fields: Any) -> str:
    return json.dumps(fields)


def test_scrub_exact_leaf_match_only_and_namespace_param_passed():
    hit = _entry(survivor_uuid="survivor", absorbed_uuid=U1, snapshot={"k": ["x", U1]})
    # U1 appears only as a substring of a longer leaf: must NOT match.
    near = _entry(survivor_uuid="survivor", absorbed_uuid=U3, note=f"copy of {U1} text")
    neo = _FakeNeo4j([hit, near])
    result = MemoryQueryRepository(neo).scrub_merge_audit_for_episodes([U1], namespace="ns-a")

    assert result["merge_audits_tombstoned"] == 1
    assert result["merge_absorbed_uuids"] == [U1]
    assert result["merge_audits_unscrubbed"] == 0
    assert json.loads(neo.merge_audit[0])["erased"] is True
    assert neo.merge_audit[1] == near
    assert neo.calls[0][1]["tenant_namespaces"] == ["ns-a"]
    assert "tenant_namespaces" in neo.calls[0][0]


def test_scrub_drops_invalid_needles_without_querying():
    neo = _FakeNeo4j(["anything with e"])
    result = MemoryQueryRepository(neo).scrub_merge_audit_for_episodes(["e", "ab"], ["x"])
    assert neo.calls == []
    assert result["merge_audits_tombstoned"] == 0
    assert result["merge_audits_unscrubbed"] == 0


def test_scrub_counts_unparseable_entries_and_leaves_them():
    bad = f"not json but mentions {U1}"
    neo = _FakeNeo4j([bad])
    result = MemoryQueryRepository(neo).scrub_merge_audit_for_episodes([U1])
    assert result["merge_audits_tombstoned"] == 0
    assert result["merge_audits_unscrubbed"] == 1
    assert neo.merge_audit == [bad]


def _runtime():
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    class _Coord:
        called = False

        def erase_memory(self, *a, **k):
            _Coord.called = True
            return {"reason": "erased"}

    class _Runtime(RuntimeProviderDataOpsMixin):
        def __init__(self) -> None:
            self.built = SimpleNamespace(graph_adapter=object())
            self._erasure_coordinator = _Coord()

        async def _require_own_memory(self, node_uuid):  # pragma: no cover - must not run
            raise AssertionError("guard reached for an invalid target")

        async def _off_loop(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    return _Runtime(), _Coord


def test_erase_memory_invalid_target_short_circuits_and_reads_as_not_deleted():
    runtime, coord = _runtime()
    outcome = asyncio.run(runtime.erase_memory("e"))
    assert outcome["reason"] == "invalid_target"
    assert coord.called is False
    assert asyncio.run(runtime.delete_memory("e")) is False


def test_delete_memory_tool_refuses_invalid_target_without_saying_erased():
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    class _Backend:
        async def fetch_memory_by_uuid(self, *a, **k):
            raise AssertionError("lookup before validation")

        async def erase_memory(self, *a, **k):
            raise AssertionError("erase before validation")

    tool = DeleteMemoryTool()
    tool.get_backend = lambda: _Backend()  # type: ignore[method-assign]
    message = asyncio.run(tool.endpoint(node_uuid="e"))
    assert message.startswith("Refused")
    assert "Erased" not in message
