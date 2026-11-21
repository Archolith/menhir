"""P4a: build ops (counts, failed projections, scope promotion, reset) on the backend surface."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from menhir.api import routes_handlers
from menhir.api.routes_handlers import PINNED_GLOBAL_OPERATIONS, backend_invoke_impl
from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
from menhir.config import MemorySettings
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.backend_scrub import wrap_backend
from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY
from menhir.infrastructure.build_report_queries import BuildReportQueries

pytestmark = pytest.mark.unit

OPS = {
    "fetch_namespace_build_counts": "readonly",
    "list_failed_evidence_projections": "readonly",
    "promote_namespace_scope": "operator",
    "reset_namespace": "operator",
}


@pytest.mark.parametrize("op,tier", sorted(OPS.items()))
def test_op_is_dispatched_with_the_right_tier(op: str, tier: str) -> None:
    assert op in _BACKEND_METHODS
    assert _required_tier_for_operation(op) == tier
    assert op not in PINNED_GLOBAL_OPERATIONS
    assert op in dir(MemoryBackend)


def _client(handler) -> BackendClient:
    http = httpx.AsyncClient(base_url="http://menhir.test", transport=httpx.MockTransport(handler))
    return BackendClient("http://menhir.test", client=http, settings=MemorySettings())


def test_backend_client_sends_op_name_and_params() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[] if "list_failed" in request.url.path else {"ok": 1})

    c = _client(handler)
    asyncio.run(c.fetch_namespace_build_counts("ns-a", [1, 2]))
    asyncio.run(c.list_failed_evidence_projections("ns-a"))
    asyncio.run(c.promote_namespace_scope("ns-a"))
    asyncio.run(c.reset_namespace("ns-a"))
    base = "/api/internal/backend/"
    assert seen == [
        (base + "fetch_namespace_build_counts",
         {"namespace": "ns-a", "submitted_epoch_millis": [1, 2], "sections": None}),
        (base + "list_failed_evidence_projections", {"namespace": "ns-a"}),
        (base + "promote_namespace_scope", {"namespace": "ns-a"}),
        (base + "reset_namespace", {"namespace": "ns-a", "force": False}),
    ]
    sig = set(inspect.signature(MemoryBackend.fetch_namespace_build_counts).parameters) - {"self"}
    assert sig == set(seen[0][1])


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name: str, *a: Any, **k: Any) -> None:
        self.calls.append((name, a, k))

    def fetch_namespace_build_counts(self, *a: Any, **k: Any) -> dict:
        self._rec("counts", *a, **k)
        return {"namespace": a[0]}

    def list_failed_evidence_projections(self, *a: Any, **k: Any) -> list:
        self._rec("failed", *a, **k)
        return [{"uuid": "u", "processing_error": None}]

    def promote_namespace_scope(self, *a: Any, **k: Any) -> dict:
        self._rec("promote", *a, **k)
        return {"namespace": a[0], "entities_promoted": 1, "edges_promoted": 2}

    def purge_turn_evidence(self, *a: Any, **k: Any) -> int:
        self._rec("purge", *a, **k)
        return 3

    # delete_namespace path used by RuntimeProvider.delete_namespace
    def count_namespace(self, *a: Any, **k: Any) -> int:
        return 5

    def delete_namespace(self, *a: Any, **k: Any) -> int:
        self._rec("delete", *a, **k)
        return 5


def _provider(adapter: _Adapter) -> RuntimeProvider:
    provider = RuntimeProvider(SimpleNamespace(graph_adapter=adapter), process_session=None)

    async def _delete(namespace: str, **k: Any) -> dict[str, Any]:
        # The real delete_namespace (erasure coordinator, default refusal) is covered elsewhere.
        if namespace.strip() == "default":
            raise ValueError("refusing to delete the default/shared namespace")
        adapter.delete_namespace(namespace, force=k.get("force", False))
        return {"deleted": 5}

    provider.delete_namespace = _delete  # type: ignore[method-assign]
    return provider


def test_runtime_provider_calls_adapter_with_namespace() -> None:
    a = _Adapter()
    p = _provider(a)
    asyncio.run(p.fetch_namespace_build_counts(" ns-a ", [7]))
    assert asyncio.run(p.list_failed_evidence_projections("ns-a")) == [
        {"uuid": "u", "processing_error": None}]
    out = asyncio.run(p.promote_namespace_scope("ns-a"))
    assert out["entities_promoted"] == 1
    assert a.calls[0] == ("counts", ("ns-a", [7], None), {})
    assert a.calls[1] == ("failed", ("ns-a",), {})
    assert a.calls[2] == ("promote", ("ns-a",), {})


@pytest.mark.parametrize("blank", [None, "", "   "])
@pytest.mark.parametrize("op", sorted(OPS))
def test_runtime_provider_rejects_blank_namespace(op: str, blank: Any) -> None:
    a = _Adapter()
    with pytest.raises(ValueError):
        asyncio.run(getattr(_provider(a), op)(blank))
    assert a.calls == []


def test_promote_refuses_default_namespace() -> None:
    a = _Adapter()
    for ns in ("default", " default "):
        with pytest.raises(ValueError, match="default"):
            asyncio.run(_provider(a).promote_namespace_scope(ns))
    assert a.calls == []


def test_reset_namespace_deletes_then_purges_turn_evidence() -> None:
    a = _Adapter()
    out = asyncio.run(_provider(a).reset_namespace("ns-a"))
    assert out == {"namespace": "ns-a", "nodes_deleted": 5, "turn_evidence_deleted": 3}
    assert [c[0] for c in a.calls] == ["delete", "purge"]
    assert a.calls[0][2] == {"force": False}


def test_reset_namespace_passes_force_to_delete_namespace() -> None:
    a = _Adapter()
    asyncio.run(_provider(a).reset_namespace("ns-a", force=True))
    assert a.calls[0] == ("delete", ("ns-a",), {"force": True})


def test_reset_namespace_refuses_default() -> None:
    a = _Adapter()
    with pytest.raises(ValueError):
        asyncio.run(_provider(a).reset_namespace("default"))
    assert a.calls == []


class _FakeNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows

    def execute(self, query: str, params: dict[str, Any] | None = None, **_k: Any):
        self.calls.append((query, dict(params or {})))
        if self.rows is not None:
            return self.rows
        return [{k: 0 for k in (
            "pending", "ready", "enriching", "failed", "llm_tasks", "processing_attempts",
            "total", "c", "admitted", "unprojected")}]


def test_queries_are_parameterized_and_shape_the_result() -> None:
    ns = "bench-Secret-NS"
    fake = _FakeNeo4j()
    q = BuildReportQueries(fake)
    out = q.namespace_build_counts(ns, [10])
    assert set(out) == {"namespace", "state", "scalar", "integrity"}
    assert set(out["scalar"]) == {
        "turn_evidence", "typed_assertions", "scalar_views", "scalar_history_views",
        "user_founded_scalar_views"}
    assert set(out["integrity"]) == {
        "user_turns_admitted", "admitted_turns_unprojected", "assertions_unfounded",
        "episodes_valid_at_unsubmitted", "ready_episodes_time_unpaired",
        "cross_namespace_links"}
    assert out["integrity"]["episodes_valid_at_unsubmitted"] == 0
    q.failed_evidence_projections(ns)
    q.promote_namespace_scope(ns)
    assert fake.calls
    for text, params in fake.calls:
        assert ns not in text and ns.lower() not in text
        assert "$ns" in text or "$nl" in text
        assert params.get("ns") == ns or params.get("nl") == ns.lower()
    store = [p for _, p in fake.calls if "store_only" in p]
    assert store and all(p["store_only"] == EXTRACTION_MODE_STORE_ONLY for p in store)
    assert any(p.get("epochs") == [10] for _, p in fake.calls)


def test_epochs_omitted_leaves_valid_at_unsubmitted_none() -> None:
    fake = _FakeNeo4j()
    out = BuildReportQueries(fake).namespace_build_counts("ns-a")
    assert out["integrity"]["episodes_valid_at_unsubmitted"] is None
    assert not any("epochs" in p for _, p in fake.calls)


def test_sections_limit_the_queries_run() -> None:
    fake = _FakeNeo4j()
    out = BuildReportQueries(fake).namespace_build_counts("ns-a", sections=["state"])
    assert set(out) == {"namespace", "state"}
    assert len(fake.calls) == 1 and "processing_state='PENDING'" in fake.calls[0][0]


def test_unknown_section_raises() -> None:
    with pytest.raises(ValueError):
        BuildReportQueries(_FakeNeo4j()).namespace_build_counts("ns-a", sections=["nope"])


# Clause checks ported from archolith-bench tests of the same queries (before they moved here).
def _integrity_queries() -> dict[str, str]:
    from menhir.infrastructure import build_report_queries as m
    return {
        "admitted": m._ADMITTED_QUERY, "time_unpaired": m._TIME_UNPAIRED_QUERY,
        "valid_at": m._VALID_AT_UNSUBMITTED_QUERY, "scalar": " ".join(m._SCALAR_QUERIES.values()),
    }


def test_integrity_exempts_store_only_from_projection_and_pairing() -> None:
    import re
    q = _integrity_queries()
    # Admission still counts a store-only admitter; only the projection check skips it.
    admitted = re.search(r"EXISTS \{[^}]*\} AS admitted", q["admitted"]).group(0)
    assert "store_only" not in admitted
    assert "coalesce(q.extraction_mode, '') <> $store_only } AS extracted" in q["admitted"]
    assert "WHEN extracted AND NOT projected" in q["admitted"]
    assert "coalesce(q.extraction_mode, '') <> $store_only" in q["time_unpaired"]


def test_verbatim_skip_passes_only_through_ready_projection_at_same_time() -> None:
    q = _integrity_queries()["time_unpaired"]
    assert (
        "q.processing_substage = 'verbatim_skipped' AND q.extraction_skipped_for IS NOT NULL "
        "AS skipped" in q
    )
    assert "OPTIONAL MATCH (p:Episodic {namespace:$ns}) WHERE skipped" in q
    assert "AND p.uuid = q.extraction_skipped_for" in q
    assert "coalesce(p.is_evidence_projection, false) AND p.processing_state = 'READY'" in q
    assert (
        "datetime(toString(ps[0].reference_time)).epochMillis "
        "= datetime(toString(q.reference_time)).epochMillis THEN 0 ELSE 1 END" in q
    )


def test_scalar_views_split_state_and_history_and_valid_at_uses_epochs() -> None:
    q = _integrity_queries()
    assert "view_kind:'scalar_state'" in q["scalar"]
    assert "view_kind:'scalar_history'" in q["scalar"]
    assert "epochMillis IN $epochs" in q["valid_at"]


def test_empty_read_raises() -> None:
    with pytest.raises(RuntimeError):
        BuildReportQueries(_FakeNeo4j(rows=[])).namespace_build_counts("ns-a")


def _invoke(op: str, backend: Any, body: dict[str, Any], *, recorded: list[str]):
    return asyncio.run(backend_invoke_impl(
        object(), op, body,
        backend_methods=_BACKEND_METHODS,
        required_tier_for_operation=_required_tier_for_operation,
        require_tier=lambda _t: None,
        try_record_destructive_op_rest=recorded.append,
        resolve_caller_session=lambda _req: SimpleNamespace(session_id="s"),
        get_backend=lambda _req: backend,
        drain_background_errors=lambda _sid: [],
        logger=logging.getLogger("menhir.test.build_ops"),
    ))


@pytest.fixture
def pinned(monkeypatch):
    def _bind(pin: str) -> None:
        monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: pin)
        monkeypatch.setattr(routes_handlers, "get_request_tool_allowlist", lambda: None)
        monkeypatch.setattr(routes_handlers, "get_client_tool_allowlist", lambda: frozenset())

    return _bind


def test_pinned_client_namespace_is_forced_on_promote(pinned) -> None:
    pinned("silo-a")
    a = _Adapter()
    recorded: list[str] = []
    _invoke("promote_namespace_scope", wrap_backend(_provider(a)),
            {"namespace": "silo-b"}, recorded=recorded)
    assert a.calls == [("promote", ("silo-a",), {})]


def test_operator_ops_are_recorded_as_destructive_and_reads_are_not(pinned) -> None:
    pinned("")
    a = _Adapter()
    recorded: list[str] = []
    _invoke("promote_namespace_scope", wrap_backend(_provider(a)),
            {"namespace": "ns-a"}, recorded=recorded)
    _invoke("reset_namespace", wrap_backend(_provider(a)),
            {"namespace": "ns-a"}, recorded=recorded)
    _invoke("list_failed_evidence_projections", wrap_backend(_provider(a)),
            {"namespace": "ns-a"}, recorded=recorded)
    assert recorded == ["promote_namespace_scope", "reset_namespace"]
