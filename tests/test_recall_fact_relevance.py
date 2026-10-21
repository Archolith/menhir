"""Relevance-ranked attached facts (MENHIR_FRONTIER_FACT_RELEVANCE): ranking, query, wiring."""

from __future__ import annotations

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_policies import _build_temporal_facts
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import _setup_search_and_metadata


def _rows(similarities, node="n1"):
    """Eight rows; index 0 is the oldest. similarities maps index -> fact_similarity."""
    out = []
    for i in range(8):
        row = {
            "node_uuid": node, "fact": f"fact-{i}", "valid_at": None, "invalid_at": None,
            "created_at": f"2025-01-{i + 1:02d}T00:00:00Z", "expired_at": None,
        }
        if i in similarities:
            row["fact_similarity"] = similarities[i]
        out.append(row)
    return out


def _facts(result, node="n1"):
    return [f.fact for f in result[node]]


@pytest.mark.unit
def test_relevance_k_zero_is_newest_five():
    result = _build_temporal_facts(_rows({0: 0.99, 1: 0.98, 2: 0.97}))
    assert _facts(result) == ["fact-7", "fact-6", "fact-5", "fact-4", "fact-3"]


@pytest.mark.unit
def test_relevance_picks_oldest_high_similarity_plus_newest_ordered_newest_first():
    result = _build_temporal_facts(
        _rows({0: 0.99, 1: 0.98, 2: 0.97, 5: 0.1}), relevance_k=3,
    )
    assert _facts(result) == ["fact-7", "fact-6", "fact-2", "fact-1", "fact-0"]
    assert len(result["n1"]) == 5


@pytest.mark.unit
def test_none_similarity_never_takes_a_relevance_slot():
    result = _build_temporal_facts(_rows({0: 0.9}), relevance_k=3)
    assert _facts(result) == ["fact-7", "fact-6", "fact-5", "fact-4", "fact-0"]
    result = _build_temporal_facts(
        _rows({0: 0.9, 1: None, 2: float("nan"), 3: float("inf")}), relevance_k=3,
    )
    assert "fact-1" not in _facts(result) and "fact-2" not in _facts(result)
    assert "fact-3" not in _facts(result) and "fact-0" in _facts(result)


@pytest.mark.unit
def test_similarity_tie_breaks_to_newest():
    result = _build_temporal_facts(_rows({0: 0.5, 1: 0.5, 2: 0.5, 3: 0.5}), relevance_k=1)
    assert "fact-3" in _facts(result) and "fact-0" not in _facts(result)


class _FakeNeo4j:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return []


@pytest.mark.unit
def test_fetch_temporal_facts_query_vector_adds_similarity_field_and_param():
    neo = _FakeNeo4j()
    repo = MemoryQueryRepository(neo)
    repo.fetch_temporal_facts(["a"])
    query, params = neo.calls[-1]
    assert "fact_similarity" not in query
    assert "query_vector" not in params
    repo.fetch_temporal_facts(["a"], query_vector=[0.1, 0.2])
    query, params = neo.calls[-1]
    assert "fact_similarity" in query
    assert "size(r.fact_embedding) = size($query_vector)" in query
    assert params["query_vector"] == [0.1, 0.2]


@pytest.mark.unit
def test_fetch_temporal_facts_relevance_query_is_tenant_scoped():
    from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params

    neo = _FakeNeo4j()
    MemoryQueryRepository(neo).fetch_temporal_facts(["a"], query_vector=[0.1], namespace="proj")
    query, params = neo.calls[-1]
    assert tenant_scope_cypher("r") in query
    for key, value in tenant_scope_params("proj").items():
        assert params[key] == value


@pytest.mark.unit
def test_fact_relevance_flag_registered_and_reaches_tuning(monkeypatch):
    feature = FEATURES["frontier_fact_relevance"]
    assert feature.env_var == "MENHIR_FRONTIER_FACT_RELEVANCE"
    assert feature.default is False
    assert RetrievalTuningConfig().enable_fact_relevance is False
    monkeypatch.setenv("MENHIR_FRONTIER_FACT_RELEVANCE", "1")
    settings = MemorySettings.from_env()
    assert settings.frontier_fact_relevance is True
    assert settings.retrieval_tuning().enable_fact_relevance is True


def _fact_rows():
    return [
        {"node_uuid": "entity-1", "fact": f"fact-{i}", "valid_at": None, "invalid_at": None,
         "created_at": f"2025-01-{i + 1:02d}T00:00:00Z", "expired_at": None,
         "fact_similarity": 0.95 if i == 0 else 0.1}
        for i in range(8)
    ]


def _svc(client, adapter):
    return RecallService(graphiti_client=client, graph_adapter=adapter,
                         scoring_service=ScoringService())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_flag_on_passes_query_vector_and_keeps_old_relevant_fact(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    for row in stub_memory_graph_adapter.candidate_metadata:
        row["namespace"] = "proj"
    seen = []
    namespaces = []

    def fake(node_uuids, *, query_vector=None, namespace=None):
        seen.append(query_vector)
        namespaces.append(namespace)
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", namespace="proj", tuning=RetrievalTuningConfig(enable_fact_relevance=True))
    assert seen and seen[-1] == stub_graphiti_client.embed_query_result
    assert namespaces[-1] == "proj"
    facts = {f.fact for r in res.results if r.uuid == "entity-1" for f in r.temporal_facts}
    assert "fact-0" in facts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_relevance_query_failure_falls_back_to_newest_facts(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    def fake(node_uuids, *, query_vector=None, namespace=None):
        if query_vector is not None:
            raise RuntimeError("vector index unavailable")
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", tuning=RetrievalTuningConfig(enable_fact_relevance=True))
    facts = {f.fact for r in res.results if r.uuid == "entity-1" for f in r.temporal_facts}
    assert facts and "fact-7" in facts and "fact-0" not in facts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_relevance_query_programming_error_skips_the_fallback(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # A bug is not masked by the newest-5 fallback; the enrichment's outer handler logs it
    # and recall continues with no attached facts.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    def fake(node_uuids, *, query_vector=None, namespace=None):
        if query_vector is not None:
            raise TypeError("bad call")
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", tuning=RetrievalTuningConfig(enable_fact_relevance=True))
    assert res.results and all(not r.temporal_facts for r in res.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_flag_off_passes_no_query_vector(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    kwargs_seen = []

    def fake(node_uuids, **kwargs):
        kwargs_seen.append(kwargs)
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q")
    assert kwargs_seen and all(k == {} for k in kwargs_seen)
    facts = {f.fact for r in res.results if r.uuid == "entity-1" for f in r.temporal_facts}
    assert "fact-0" not in facts


@pytest.mark.unit
def test_fact_queries_require_fact_text():
    from menhir.infrastructure.cypher import FACT_TEXT_PRESENT

    neo = _FakeNeo4j()
    repo = MemoryQueryRepository(neo)
    repo.fetch_temporal_facts(["a"])
    repo.fetch_temporal_facts(["a"], query_vector=[0.1], namespace="proj")
    repo.fetch_candidate_fact_edges(["a"])
    assert len(neo.calls) == 3
    for query, _ in neo.calls:
        assert FACT_TEXT_PRESENT in query


@pytest.mark.online
def test_link_edges_without_fact_text_do_not_take_fact_slots(test_neo4j_repo):
    # Link edges (written like correlation/bridge links: no fact, uuid or namespace) are
    # newest, so without the filter they would fill all five per-node slots and push the real
    # facts out. Blank and non-string facts must be skipped without failing the query.
    from uuid import uuid4

    neo4j = test_neo4j_repo
    tag = "facttext-" + uuid4().hex
    links = [{"type": "correlation"}] * 3 + [{"type": "bridged", "group_id": ""}] * 2
    graphiti = [{"fact": f} for f in ("", "   ", [1, 2], 7)] + [
        {"fact": f"{tag}-real-{i}"} for i in range(3)]
    edges = []
    for i, props in enumerate(links + graphiti):
        props = dict(props)
        if "fact" in props:
            props.update(uuid=f"{tag}-r{i}", namespace=tag, group_id=tag)
        edges.append({"m": f"{tag}-m{i}", "day": 30 - i, "props": props})
    hub = tag + "-hub"
    try:
        neo4j.execute("""
            CREATE (n:Entity {uuid:$hub, name:$hub, namespace:$ns, group_id:$ns})
            WITH n UNWIND $edges AS e
            CREATE (m:Entity {uuid:e.m, name:e.m, namespace:$ns, group_id:$ns})
            CREATE (n)-[r:RELATES_TO]->(m)
            SET r += e.props,
                r.created_at = datetime('2025-01-01T00:00:00Z') + duration({days:e.day})
        """, {"hub": hub, "ns": tag, "edges": edges})
        repo = MemoryQueryRepository(neo4j)
        real = {f"{tag}-real-{i}" for i in range(3)}
        plain = repo.fetch_temporal_facts([hub])
        # namespace=None is the unscoped case, where the tenant filter lets link edges through.
        ranked = repo.fetch_temporal_facts([hub], query_vector=[0.1, 0.2], namespace=None)
        scoped = repo.fetch_temporal_facts([hub], query_vector=[0.1, 0.2], namespace=tag)
        shadow = repo.fetch_candidate_fact_edges([hub])
        assert {row["fact"] for row in plain} == real
        assert {row["fact"] for row in ranked} == real
        assert {row["fact"] for row in scoped} == real
        assert {row["fact_text"] for row in shadow} == real
        assert set(_facts(_build_temporal_facts(plain), node=hub)) == real
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid STARTS WITH $tag DETACH DELETE n",
                      {"tag": tag})
