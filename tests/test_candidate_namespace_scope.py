"""`list_candidates` takes an opt-in `namespace`, and the internal dispatch pins it (#481).

Before this, `list_candidates` declared no `namespace`, so `_pin_backend_invoke_namespace` had
nothing to inject into: a client pinned to one silo read every silo's candidates through
`/api/internal/backend/list_candidates`, and `review_inbox` over the stdio bridge (where the pin
exists only server-side) listed them under an "all namespaces" header.

The repository default must stay global: the explorer and every existing caller pass nothing.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest

from menhir.api import routes_handlers
from menhir.api.routes_handlers import backend_invoke_impl
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.domain.namespace import tenant_scope_cypher
from menhir.infrastructure.candidate_repository import CandidateRepository


class _RecordingNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return self.rows


# ---------------------------------------------------------------------------
# Repository: opt-in predicate from the shared builder, plus the row's silo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unscoped_list_keeps_the_global_read() -> None:
    neo4j = _RecordingNeo4j()
    CandidateRepository(neo4j).list_candidates(limit=10)

    query, params = neo4j.calls[0]
    assert tenant_scope_cypher("n") in query
    # A NULL parameter is what makes the shared fragment a no-op.
    assert params["tenant_namespaces"] is None
    assert params["limit"] == 10 and params["source"] is None


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_namespace_is_unscoped_not_the_default_silo(blank: str) -> None:
    neo4j = _RecordingNeo4j()
    CandidateRepository(neo4j).list_candidates(namespace=blank)
    assert neo4j.calls[0][1]["tenant_namespaces"] is None


@pytest.mark.unit
def test_scoped_list_narrows_to_that_silo_only() -> None:
    neo4j = _RecordingNeo4j()
    CandidateRepository(neo4j).list_candidates(source="painscan-friction", namespace="tenant-a")

    query, params = neo4j.calls[0]
    assert params["tenant_namespaces"] == ["tenant-a"]
    assert params["source"] == "painscan-friction"
    assert "($source IS NULL OR n.source = $source)" in query


@pytest.mark.unit
def test_each_row_reports_its_silo() -> None:
    neo4j = _RecordingNeo4j()
    CandidateRepository(neo4j).list_candidates()
    assert "coalesce(n.namespace, n.group_id) AS namespace" in neo4j.calls[0][0]


# ---------------------------------------------------------------------------
# Backend surfaces: the parameter exists everywhere the dispatch can reach
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("surface", [RuntimeProvider, BackendClient])
def test_every_backend_surface_declares_namespace(surface: type) -> None:
    param = inspect.signature(surface.list_candidates).parameters.get("namespace")
    assert param is not None and param.default is None


class _CandidateBackend:
    """Records the kwargs the dispatch hands `list_candidates`.

    Its signature is pinned to RuntimeProvider's below, because the dispatch decides whether to
    inject the pin by inspecting that signature -- a fake with a different one would prove nothing.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append({"source": source, "limit": limit, "namespace": namespace})
        return []


class _Session:
    session_id = "sess-r481"


def _invoke(backend: _CandidateBackend, body: dict[str, Any]):
    return backend_invoke_impl(
        object(),
        "list_candidates",
        body,
        backend_methods={"list_candidates"},
        required_tier_for_operation=lambda _op: "readonly",
        require_tier=lambda _tier: None,
        try_record_destructive_op_rest=lambda _op: None,
        resolve_caller_session=lambda _req: _Session(),
        get_backend=lambda _req: backend,
        drain_background_errors=lambda _sid: [],
        logger=logging.getLogger("menhir.test.r481"),
    )


@pytest.mark.unit
def test_the_fake_matches_the_real_signature() -> None:
    real = inspect.signature(RuntimeProvider.list_candidates)
    fake = inspect.signature(_CandidateBackend.list_candidates)
    assert list(real.parameters) == list(fake.parameters)


@pytest.mark.asyncio
async def test_internal_dispatch_pins_an_omitted_namespace(monkeypatch) -> None:
    """THE FINDING. The stdio bridge sends no namespace on an unscoped review_inbox call; the
    server must force the pinned silo, exactly as it does for list_conflict_groups."""
    monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: "tenant-a")
    backend = _CandidateBackend()

    await _invoke(backend, {"source": None, "limit": 6})

    assert backend.calls == [{"source": None, "limit": 6, "namespace": "tenant-a"}]


@pytest.mark.asyncio
async def test_internal_dispatch_overrides_another_silo(monkeypatch) -> None:
    monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: "tenant-a")
    backend = _CandidateBackend()

    await _invoke(backend, {"limit": 6, "namespace": "tenant-b"})

    assert backend.calls[0]["namespace"] == "tenant-a"


@pytest.mark.asyncio
async def test_internal_dispatch_leaves_unpinned_callers_global(monkeypatch) -> None:
    monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: "")
    backend = _CandidateBackend()

    await _invoke(backend, {"limit": 6})

    assert backend.calls[0]["namespace"] is None


# ---------------------------------------------------------------------------
# Online: the predicate partitions a real graph, and unscoped still spans every silo
# ---------------------------------------------------------------------------

_SEED_UUIDS = ["r481-cand-a", "r481-cand-b", "r481-cand-legacy", "r481-cand-empty"]


def _seed(repo: Any) -> None:
    repo.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": _SEED_UUIDS})
    repo.execute(
        """
        CREATE (:Entity {uuid:'r481-cand-a', scope:'CANDIDATE', name:'A', namespace:'tenant-a',
                         group_id:'tenant-a', source:'r481'})
        CREATE (:Entity {uuid:'r481-cand-b', scope:'CANDIDATE', name:'B', namespace:'tenant-b',
                         group_id:'tenant-b', source:'r481'})
        CREATE (:Entity {uuid:'r481-cand-legacy', scope:'CANDIDATE', name:'LEGACY', source:'r481'})
        CREATE (:Entity {uuid:'r481-cand-empty', scope:'CANDIDATE', name:'EMPTY', namespace:'',
                         group_id:'', source:'r481'})
        """
    )


def _uuids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["uuid"]) for r in rows}


@pytest.mark.online
def test_unscoped_list_returns_every_silo_on_a_real_graph(test_neo4j_repo) -> None:
    _seed(test_neo4j_repo)
    try:
        repo = CandidateRepository(test_neo4j_repo)
        rows = repo.list_candidates(source="r481", limit=50)
        assert _uuids(rows) == set(_SEED_UUIDS)
        silos = {r["uuid"]: r["namespace"] for r in rows}
        assert silos["r481-cand-a"] == "tenant-a" and silos["r481-cand-b"] == "tenant-b"
    finally:
        test_neo4j_repo.execute(
            "MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": _SEED_UUIDS}
        )


@pytest.mark.online
def test_scoped_list_returns_only_that_silo_on_a_real_graph(test_neo4j_repo) -> None:
    _seed(test_neo4j_repo)
    try:
        repo = CandidateRepository(test_neo4j_repo)
        assert _uuids(repo.list_candidates(source="r481", namespace="tenant-a")) == {"r481-cand-a"}
        assert _uuids(repo.list_candidates(source="r481", namespace="tenant-b")) == {"r481-cand-b"}
        # The default silo covers every legacy shape: '' and an absent namespace/group_id.
        assert _uuids(repo.list_candidates(source="r481", namespace="default")) == {
            "r481-cand-empty",
            "r481-cand-legacy",
        }
    finally:
        test_neo4j_repo.execute(
            "MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": _SEED_UUIDS}
        )
