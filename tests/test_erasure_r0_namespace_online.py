"""CF-326 (plan R0.3): a namespace-pinned caller must not erase another namespace's TurnEvidence.

The transport ownership guards (`fetch_memory_by_uuid`) match only :Entity / :Episodic, so a pinned
caller that knew a foreign `turn_id` slipped past them. The fix is four layers; these tests pin the
behaviour at the coordinator (the layer below every transport, CF-127), at three transports, and at
the two defense-in-depth layers (`seal_memory`, `delete_memory_with_scalar_cascade`).

Real Neo4j through ``test_neo4j_repo``; the sidecar journal is a per-test SQLite file. Two silos, A
and B, each with a queue memory ADMITTED_ON a TurnEvidence; B also has the turn's evidence
projection. Every test uses unique uuid4 values and namespaces and cleans up in ``finally``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from menhir.core.request_context import bind_request_session, reset_request_session
from menhir.domain.session import new_session
from menhir.infrastructure.erasure_fence import seal_memory
from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator

pytestmark = pytest.mark.online

PIN_CLIENT = "botpinneda"


class _Rig:
    """Two seeded silos plus a real coordinator over the real graph."""

    def __init__(self, repo: Any, tmp_path: Path) -> None:
        self.repo = repo
        self.tag = f"r03-{uuidlib.uuid4().hex[:10]}"
        self.ns_a = f"{self.tag}-a"
        self.ns_b = f"{self.tag}-b"
        self.q_a, self.turn_a = str(uuidlib.uuid4()), str(uuidlib.uuid4())
        self.q_b, self.turn_b = str(uuidlib.uuid4()), str(uuidlib.uuid4())
        self.proj_b = str(uuidlib.uuid4())
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        self.db = tmp_path / "r03.db"
        McpTelemetryStore(db_path=self.db)._ensure_ready()
        self.journal = GraphOperationsJournal(db_path=self.db)
        self.subjects = ErasureSubjectStore(db_path=self.db)
        self.coord = ErasureCoordinator(
            graph_adapter=self.adapter, journal=self.journal, subjects=self.subjects
        )
        self._seed()

    def _seed(self) -> None:
        self.repo.execute(
            """
            CREATE (ta:TurnEvidence {turn_id:$turn_a, turn_key:$turn_a, namespace:$ns_a,
                                     role:'user', text:'a text', test_tag:$tag})
            CREATE (qa:Episodic {uuid:$q_a, group_id:$ns_a, namespace:$ns_a,
                                 processing_state:'READY', content:'a memory', test_tag:$tag})
            CREATE (qa)-[:ADMITTED_ON]->(ta)
            CREATE (tb:TurnEvidence {turn_id:$turn_b, turn_key:$turn_b, namespace:$ns_b,
                                     role:'user', text:'b text', test_tag:$tag})
            CREATE (qb:Episodic {uuid:$q_b, group_id:$ns_b, namespace:$ns_b,
                                 processing_state:'READY', content:'b memory', test_tag:$tag})
            CREATE (qb)-[:ADMITTED_ON]->(tb)
            CREATE (pb:Episodic {uuid:$proj_b, group_id:$ns_b, namespace:$ns_b,
                                 evidence_projection_of:$turn_b, is_evidence_projection:true,
                                 processing_state:'READY', content:'b text', test_tag:$tag})
            CREATE (pb)-[:ADMITTED_ON]->(tb)
            """,
            params={
                "tag": self.tag,
                "ns_a": self.ns_a,
                "ns_b": self.ns_b,
                "turn_a": self.turn_a,
                "q_a": self.q_a,
                "turn_b": self.turn_b,
                "q_b": self.q_b,
                "proj_b": self.proj_b,
            },
        )

    def exists(self, label: str, prop: str, value: str) -> bool:
        rows = self.repo.execute(
            f"MATCH (n:{label} {{{prop}: $v}}) RETURN count(n) AS c", params={"v": value}
        )
        return int(rows[0]["c"]) == 1

    def b_intact(self) -> dict[str, bool]:
        return {
            "turn": self.exists("TurnEvidence", "turn_id", self.turn_b),
            "projection": self.exists("Episodic", "uuid", self.proj_b),
            "queue_memory": self.exists("Episodic", "uuid", self.q_b),
        }

    def a_intact(self) -> bool:
        return self.exists("TurnEvidence", "turn_id", self.turn_a) and self.exists(
            "Episodic", "uuid", self.q_a
        )

    def fence_b(self) -> dict[str, Any]:
        rows = self.repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) "
            "RETURN coalesce(f.erasure_epoch, 0) AS epoch, coalesce(f.erasing_ops, []) AS ops",
            params={"k": self.ns_b},
        )
        return dict(rows[0]) if rows else {"epoch": 0, "ops": []}

    def markers_for(self, subject_uuid: str) -> int:
        rows = self.repo.execute(
            "MATCH (m:ErasureMarker {subject_uuid: $u}) RETURN count(m) AS c",
            params={"u": subject_uuid},
        )
        return int(rows[0]["c"])

    def journal_rows(self) -> int:
        with sqlite3.connect(self.db) as conn:
            return int(conn.execute("SELECT count(*) FROM graph_operations").fetchone()[0])

    def assert_b_untouched(self, context: str) -> None:
        assert self.b_intact() == {"turn": True, "projection": True, "queue_memory": True}, context
        assert self.a_intact(), f"{context}: A's own data must be untouched too"
        assert self.fence_b()["ops"] == [], context
        assert self.fence_b()["epoch"] == 0, f"{context}: B's fence epoch moved"
        assert self.markers_for(self.turn_b) == 0, f"{context}: marker written for B's turn"
        assert self.markers_for(self.proj_b) == 0, context

    def cleanup(self) -> None:
        self.repo.execute(
            "MATCH (n) WHERE n.test_tag = $t OR n.namespace_key IN [$a, $b] "
            "OR n.subject_uuid IN [$ta, $tb, $pb, $qa, $qb] DETACH DELETE n",
            params={
                "t": self.tag,
                "a": self.ns_a,
                "b": self.ns_b,
                "ta": self.turn_a,
                "tb": self.turn_b,
                "pb": self.proj_b,
                "qa": self.q_a,
                "qb": self.q_b,
            },
        )


@pytest.fixture
def rig(test_neo4j_repo, tmp_path):
    r = _Rig(test_neo4j_repo, tmp_path)
    try:
        yield r
    finally:
        r.cleanup()


# ---------------------------------------------------------------- coordinator level
def test_pinned_caller_cannot_erase_foreign_turn_at_the_coordinator(rig: _Rig) -> None:
    """Fails if `erase_memory` stops resolving the target's namespace before PREPARE (drop the
    `erase_target_namespace_keys` check) AND the deeper layers stop refusing."""
    out = rig.coord.erase_memory(rig.turn_b, caller_namespace=rig.ns_a)

    assert out["reason"] == "erased_incomplete", out
    assert "foreign_target" in out["incomplete_reasons"], out
    rig.assert_b_untouched("coordinator, caller A, target B's turn_id")
    assert rig.journal_rows() == 0, "a journal row was prepared for a foreign target"


def test_unscoped_and_owning_callers_still_erase_the_turn(rig: _Rig) -> None:
    """Control: the refusal is about the caller's namespace, not about erasing turns. Fails if the
    scope check over-refuses (e.g. refuses every TurnEvidence target)."""
    out_b = rig.coord.erase_memory(rig.turn_b, caller_namespace=rig.ns_b)
    assert "foreign_target" not in (out_b.get("incomplete_reasons") or []), out_b
    assert not rig.exists("TurnEvidence", "turn_id", rig.turn_b), out_b
    assert rig.a_intact()
    assert rig.journal_rows() == 1

    # Unscoped (operator / no pin): erase A's turn, no scope applies.
    out_none = rig.coord.erase_memory(rig.turn_a, caller_namespace=None)
    assert "foreign_target" not in (out_none.get("incomplete_reasons") or []), out_none
    assert not rig.exists("TurnEvidence", "turn_id", rig.turn_a), out_none


def test_pinned_caller_cannot_expand_a_foreign_projection_either(rig: _Rig) -> None:
    """The projection uuid is an Episodic whose home is B; the Entity/Episodic guard sees it, and
    the coordinator must also refuse it for A. Fails if the scope check only handled turn_ids."""
    out = rig.coord.erase_memory(rig.proj_b, caller_namespace=rig.ns_a)
    assert out["reason"] == "erased_incomplete" and "foreign_target" in out["incomplete_reasons"], out
    rig.assert_b_untouched("coordinator, caller A, target B's projection uuid")


def test_pinned_caller_still_erases_its_own_turn_and_b_is_unaffected(rig: _Rig) -> None:
    """Scope must not leak the other way: A erasing A's own turn leaves all of B alone."""
    out = rig.coord.erase_memory(rig.turn_a, caller_namespace=rig.ns_a)
    assert "foreign_target" not in (out.get("incomplete_reasons") or []), out
    assert not rig.exists("TurnEvidence", "turn_id", rig.turn_a), out
    assert rig.b_intact() == {"turn": True, "projection": True, "queue_memory": True}


# ---------------------------------------------------------------- transport level
def _pinned_runtime(rig: _Rig):
    """A real backend mixin over the real adapter and coordinator. Substituted: the full
    RuntimeProvider (needs a built runtime and the default sidecar db), replaced by the production
    `RuntimeProviderDataOpsMixin` (the class holding erase_memory / delete_memory /
    fetch_memory_by_uuid) with `_off_loop` run inline, as tests/test_erasure_rebuild_p0_scrub_online
    does."""
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    class _Runtime(RuntimeProviderDataOpsMixin):
        def __init__(self) -> None:
            self.built = SimpleNamespace(graph_adapter=rig.adapter)
            self._erasure_coordinator = rig.coord

        async def _off_loop(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    return _Runtime()


class _PinnedCaller:
    """Binds a REAL request session whose client name is pinned to namespace A through the real
    `MENHIR_CLIENT_NAMESPACES` resolution (core.tenancy.pinned_namespace); nothing is patched."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, namespace: str) -> None:
        monkeypatch.setenv("MENHIR_CLIENT_NAMESPACES", f"{PIN_CLIENT}={namespace}")
        self._token = None

    def __enter__(self):
        self._token = bind_request_session(
            new_session("r03-user", client_name=PIN_CLIENT)
        )
        return self

    def __exit__(self, *exc) -> None:
        reset_request_session(self._token)


def test_pin_resolves_for_the_bound_session(rig: _Rig, monkeypatch) -> None:
    """Guards the harness: the pin really resolves, so the transport tests below are pinned."""
    from menhir.core.tenancy import pinned_namespace

    with _PinnedCaller(monkeypatch, rig.ns_a):
        assert pinned_namespace() == rig.ns_a


def test_mcp_delete_memory_pinned_to_a_cannot_erase_b_turn(rig: _Rig, monkeypatch) -> None:
    """MCP `delete_memory` tool. Fails if the refusal is only at the tool/guard layer and the
    backend/coordinator stops refusing (the tool's guard misses TurnEvidence)."""
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    tool = DeleteMemoryTool()
    runtime = _pinned_runtime(rig)
    tool.get_backend = lambda: runtime  # type: ignore[method-assign]

    async def call() -> str:
        with _PinnedCaller(monkeypatch, rig.ns_a):
            return await tool.endpoint(rig.turn_b, namespace=rig.ns_a)

    result = asyncio.run(call())

    # S4: nothing ran, so the reply must not claim an erasure of any kind.
    assert result.startswith("Refused"), result
    assert "Nothing was erased" in result, result
    rig.assert_b_untouched("MCP delete_memory")


def _rest_app(runtime: Any, monkeypatch) -> FastAPI:
    from menhir.api import routes

    # Substituted: `_get_backend` returns the real-graph mixin above instead of building a
    # RuntimeProvider from a runtime context. Routing, tier checks, the pin injection and
    # exception mapping are the production code.
    monkeypatch.setattr(routes, "_get_backend", lambda request: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    return app


def test_rest_delete_memory_pinned_to_a_cannot_erase_b_turn(rig: _Rig, monkeypatch) -> None:
    """REST `DELETE /api/memory/{uuid}`. Fails if the saga stops refusing: the route reports
    `deleted` from the outcome, which must be false, or the request must be refused (403)."""
    app = _rest_app(_pinned_runtime(rig), monkeypatch)

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            with _PinnedCaller(monkeypatch, rig.ns_a):
                return await client.delete(f"/api/memory/{rig.turn_b}")

    resp = asyncio.run(call())

    assert resp.status_code in (200, 403), (resp.status_code, resp.text)
    if resp.status_code == 200:
        assert resp.json()["deleted"] is False, resp.json()
    rig.assert_b_untouched("REST DELETE /api/memory/{uuid}")


@pytest.mark.parametrize("operation", ["erase_memory", "delete_memory"])
def test_internal_dispatch_pinned_to_a_cannot_erase_b_turn(
    rig: _Rig, monkeypatch, operation: str
) -> None:
    """Generic `/api/internal/backend/{operation}`. The body names namespace B (erase_memory
    accepts `namespace`); the pin must override it and the coordinator must still refuse."""
    app = _rest_app(_pinned_runtime(rig), monkeypatch)

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            with _PinnedCaller(monkeypatch, rig.ns_a):
                body: dict[str, Any] = {"node_uuid": rig.turn_b}
                if operation == "erase_memory":
                    body["namespace"] = rig.ns_b
                return await client.post(f"/api/internal/backend/{operation}", json=body)

    resp = asyncio.run(call())

    assert resp.status_code in (200, 403), (resp.status_code, resp.text)
    if resp.status_code == 200:
        payload = resp.json()
        if operation == "erase_memory":
            assert payload["reason"] == "erased_incomplete", payload
            assert "foreign_target" in payload["incomplete_reasons"], payload
        else:
            assert payload is False, payload
    rig.assert_b_untouched(f"internal dispatch {operation}")


# ---------------------------------------------------------------- defense in depth
def test_seal_memory_refuses_foreign_fence_key_and_leaves_fence_untouched(rig: _Rig) -> None:
    """Fails if `seal_memory` stops comparing the resolved fence keys to `caller_namespace`."""
    op_id = uuidlib.uuid4().hex
    with pytest.raises(PermissionError):
        seal_memory(rig.repo, op_id, [rig.turn_b], caller_namespace=rig.ns_a)
    rig.assert_b_untouched("seal_memory(caller_namespace=A) on B's turn")


def test_seal_memory_allows_own_namespace_and_unscoped(rig: _Rig) -> None:
    """Control for the seal guard: own-namespace and unscoped seals still work. Fails if the
    guard over-refuses."""
    from menhir.infrastructure.erasure_fence import release_memory

    for caller in (rig.ns_b, None):
        op_id = uuidlib.uuid4().hex
        res = seal_memory(rig.repo, op_id, [rig.turn_b], caller_namespace=caller)
        try:
            assert res.namespace_keys == [rig.ns_b], res
        finally:
            release_memory(rig.repo, op_id)


def test_scalar_cascade_refuses_foreign_scope_and_deletes_nothing(rig: _Rig) -> None:
    """Fails if `delete_memory_with_scalar_cascade` stops comparing `scope_namespace` to the
    target's namespace keys."""
    queries = rig.adapter._memory_queries
    with pytest.raises(PermissionError):
        queries.delete_memory_with_scalar_cascade(
            rig.turn_b, operation_id=uuidlib.uuid4().hex, scope_namespace=rig.ns_a
        )
    rig.assert_b_untouched("scalar cascade scope=A on B's turn")


def test_scalar_cascade_with_own_scope_deletes_the_turn(rig: _Rig) -> None:
    """Control: the same cascade with B's own scope proceeds. Fails if it over-refuses."""
    queries = rig.adapter._memory_queries
    queries.delete_memory_with_scalar_cascade(
        rig.turn_b, operation_id=uuidlib.uuid4().hex, scope_namespace=rig.ns_b
    )
    assert not rig.exists("TurnEvidence", "turn_id", rig.turn_b)


def test_erasure_evidence_uuids_returns_nothing_for_a_foreign_home(rig: _Rig) -> None:
    """Layer 2: scoped to A, B's queue memory expands to no evidence; unscoped and scoped to B
    it still expands to B's turn. Fails if `caller_namespace` is ignored."""
    assert rig.adapter.erasure_evidence_uuids(rig.q_b, caller_namespace=rig.ns_a) == []
    own = rig.adapter.erasure_evidence_uuids(rig.q_b, caller_namespace=rig.ns_b)
    assert rig.turn_b in own, own
    assert rig.turn_b in rig.adapter.erasure_evidence_uuids(rig.q_b)
