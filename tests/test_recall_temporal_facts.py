"""Attached temporal facts: newest-five cap, fact-text filter, and the pipeline fetch call."""

from __future__ import annotations

import pytest

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
def test_newest_five_ignores_fact_similarity():
    result = _build_temporal_facts(_rows({0: 0.99, 1: 0.98, 2: 0.97}))
    assert _facts(result) == ["fact-7", "fact-6", "fact-5", "fact-4", "fact-3"]


class _FakeNeo4j:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return []


@pytest.mark.unit
def test_fetch_temporal_facts_sends_only_the_uuids():
    neo = _FakeNeo4j()
    MemoryQueryRepository(neo).fetch_temporal_facts(["a"])
    query, params = neo.calls[-1]
    assert "fact_similarity" not in query
    assert params == {"uuids": ["a"]}


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
async def test_pipeline_fetches_temporal_facts_with_only_the_uuids(
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
    repo.fetch_candidate_fact_edges(["a"])
    assert len(neo.calls) == 2
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
        shadow = repo.fetch_candidate_fact_edges([hub])
        assert {row["fact"] for row in plain} == real
        assert {row["fact_text"] for row in shadow} == real
        assert set(_facts(_build_temporal_facts(plain), node=hub)) == real
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid STARTS WITH $tag DETACH DELETE n",
                      {"tag": tag})
