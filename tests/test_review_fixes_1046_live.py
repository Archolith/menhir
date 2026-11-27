"""Survivor promotion, unmerge lineage and correlation-only edges against a real Neo4j (online).

- consolidation that merges a promotion candidate into a SESSION survivor leaves the
  survivor PERSISTENT, with no TTL, and audits its promotion.
- after the original survivor is absorbed into a third node, the split node still cannot
  merge into that node.
- a Graphiti fact edge between the pair is neither counted nor modified; a correlation
  edge is still created.
"""

from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services import lifecycle_consolidation
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.unmerge_coordinator import UnmergeCoordinator


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def tag(live_repo):
    value = f"test-1046-{uuidlib.uuid4().hex[:12]}"
    yield value
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})


def _seed(repo, tag: str, key: str, **props) -> str:
    uuid = f"{tag}-{key}"
    repo.execute(
        "CREATE (n:Entity) SET n = $p",
        params={"p": {"uuid": uuid, "name": f"entity {key}", "test_tag": tag,
                      "namespace": "default", "freshness": "ACTIVE", "summary": f"summary {key}",
                      "scope": "SESSION", **props}},
    )
    return uuid


def _props(repo, uuid: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.scope AS scope, n.ttl_expires AS ttl", params={"u": uuid},
    )
    return dict(rows[0]) if rows else {}


# --------------------------------------------------------------------------- survivor promotion

@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidation_promotes_the_survivor_of_an_absorbed_candidate(
    live_repo, tag, monkeypatch,
) -> None:
    session = f"{tag}-session"
    candidate = _seed(live_repo, tag, "candidate", session_id=session)
    # The survivor is in another session, so only the merge can promote it in this pass.
    survivor = _seed(live_repo, tag, "survivor", session_id=f"{tag}-other")
    live_repo.execute(
        "MATCH (n:Entity {uuid:$s}) SET n.ttl_expires = datetime() + duration({days: 3})",
        params={"s": survivor},
    )
    graphiti = SimpleNamespace(
        search_scored=AsyncMock(return_value=[(survivor, "entity survivor", 0.97)]),
        count_similar_by_cosine=AsyncMock(return_value=0),  # sharpness 1.0: a promotion candidate
    )
    llm = SimpleNamespace(confirm_same_entity=AsyncMock(return_value=True))
    revisions: list[str] = []
    real_revision = lifecycle_consolidation.record_memory_revision

    def _capture(**kw):
        revisions.append(kw["node_uuid"])
        return real_revision(**kw)

    monkeypatch.setattr(lifecycle_consolidation, "record_memory_revision", _capture)
    svc = LifecycleService(
        graph_adapter=MemoryGraphAdapter(neo4j=live_repo), graphiti_client=graphiti, llm=llm,
    )

    result = await svc.consolidate_session(session)

    assert _props(live_repo, candidate) == {}, "the candidate was absorbed"
    assert _props(live_repo, survivor) == {"scope": "PERSISTENT", "ttl": None}
    assert revisions == [survivor] and result.promoted == 1


# --------------------------------------------------------------------------- unmerge lineage

@pytest.mark.online
def test_a_split_node_cannot_rejoin_through_the_survivors_absorber(live_repo, tag, tmp_path) -> None:
    s = _seed(live_repo, tag, "s")
    b = _seed(live_repo, tag, "b")
    c = _seed(live_repo, tag, "c")
    graph = CorrelationRepository(live_repo)
    journal = GraphOperationsJournal(db_path=tmp_path / "saga.db")
    merger = MergeCoordinator(graph_adapter=graph, journal=journal)

    first = merger.merge(survivor_uuid=s, absorbed_uuid=b, similarity=0.97)
    assert first["merged"] == 1 and first["survivor_uuid"] == s and first["absorbed_uuid"] == b
    assert UnmergeCoordinator(graph_adapter=graph, journal=journal).unmerge(first["op_id"])["restored"] == 1
    assert merger.merge(survivor_uuid=c, absorbed_uuid=s, similarity=0.97)["merged"] == 1

    again = merger.merge(survivor_uuid=c, absorbed_uuid=b, similarity=0.97)
    assert again["merged"] == 0 and again["reason"] == me.PREVIOUSLY_UNMERGED
    assert _props(live_repo, b)["scope"] == "SESSION"


# --------------------------------------------------------------------------- correlation-only edges

@pytest.mark.online
def test_a_fact_edge_is_untouched_and_a_correlation_edge_is_created(live_repo, tag) -> None:
    low, high = sorted([_seed(live_repo, tag, "p"), _seed(live_repo, tag, "q")])
    live_repo.execute(
        "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
        "CREATE (a)-[:RELATES_TO {uuid:$f, name:'WORKS_WITH', fact:'p works with q'}]->(b)",
        params={"a": low, "b": high, "f": f"{tag}-fact"},
    )
    repo = CorrelationRepository(live_repo)

    assert repo.create_related_to_edge(high, low, similarity=0.75) is True
    assert repo.create_related_to_edge(low, high, similarity=0.80) is False

    edges = live_repo.execute(
        "MATCH (:Entity {uuid:$a})-[r:RELATES_TO]-(:Entity {uuid:$b}) "
        "RETURN r.uuid AS uuid, r.type AS type, r.similarity AS similarity, "
        "r.weight AS weight, r.last_traversed AS last_traversed ORDER BY r.type",
        params={"a": low, "b": high},
    )
    by_kind = {("fact" if e["uuid"] else e["type"]): e for e in edges}
    assert set(by_kind) == {"fact", "correlation"} and len(edges) == 2
    fact = by_kind["fact"]
    assert fact["similarity"] is None and fact["weight"] is None and fact["last_traversed"] is None
    assert by_kind["correlation"]["similarity"] == pytest.approx(0.80)
