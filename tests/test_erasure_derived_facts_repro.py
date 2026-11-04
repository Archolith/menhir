"""Repro for Archolith/menhir#423: erasure leaves single-source derived entities and facts behind.

Graphiti derives `:Entity` nodes (via `(:Episodic)-[:MENTIONS]->(:Entity)`) and `RELATES_TO` fact
edges from a memory's `:Episodic` twin. When that memory is erased, anything derived ONLY from the
erased episode should go with it. On current main it does not.

The graph is seeded directly with Cypher in the shape Graphiti writes; no LLM and no enrichment run.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ERASED, ErasureCoordinator

SECRET = "The repro probe's favourite colour is teal-4417"


def _exists(repo, node_uuid: str) -> bool:
    rows = repo.execute(
        "MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid}
    )
    return int(rows[0]["c"]) > 0


def _edge_exists(repo, edge_uuid: str) -> bool:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN count(r) AS c",
        params={"u": edge_uuid},
    )
    return int(rows[0]["c"]) > 0


def _seed_and_erase(repo, tmp_path, tag: str) -> dict[str, str]:
    """Seed queue node + Graphiti twin + derived entities/fact, erase the queue node."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    ids = {
        "tag": tag,
        "ns": f"{tag}-ns",
        "queue": f"{tag}-queue",
        "graphiti": f"{tag}-graphiti",
        "entity": f"{tag}-entity",
        "entity2": f"{tag}-entity2",
        "edge": f"{tag}-edge",
    }
    repo.execute(
        """
        CREATE (g:Episodic {uuid:$graphiti, group_id:$ns, valid_at:datetime(), content:$text,
                            test_tag:$tag})
        CREATE (:Episodic {uuid:$queue, namespace:$ns, resolved_episode_uuid:$graphiti,
                           processing_state:'READY', content:$text, test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'repro probe', group_id:$ns,
                           summary:$text, created_at:datetime(), test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-4417', group_id:$ns,
                           summary:'a colour', created_at:datetime(), test_tag:$tag})
        CREATE (g)-[:MENTIONS {uuid:$mention, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$text,
                                  episodes:[$graphiti], group_id:$ns, created_at:datetime(),
                                  test_tag:$tag}]->(e2)
        """,
        params={**ids, "text": SECRET, "mention": f"{tag}-mention"},
    )
    adapter = MemoryGraphAdapter(neo4j=repo)
    db = tmp_path / "derived.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    assert adapter.episodic_twin_uuids(ids["queue"]) == [ids["graphiti"]]
    assert coord.erase_memory(ids["queue"])["reason"] == ERASED
    return ids


def _cleanup(repo, tag: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


@pytest.mark.online
def test_erasure_removes_both_episodes_sanity(test_neo4j_repo, tmp_path) -> None:
    """Setup sanity: the erase succeeds and both episodes go (independent of #423)."""
    repo = test_neo4j_repo
    tag = f"test-derived-{uuidlib.uuid4()}"
    try:
        ids = _seed_and_erase(repo, tmp_path, tag)
        assert _exists(repo, ids["queue"]) is False
        assert _exists(repo, ids["graphiti"]) is False
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
@pytest.mark.xfail(
    strict=True,
    reason="#423: erasure leaves single-source derived entities and facts behind",
)
def test_erasure_removes_single_source_entity_and_fact(
    test_neo4j_repo, tmp_path
) -> None:
    repo = test_neo4j_repo
    tag = f"test-derived-{uuidlib.uuid4()}"
    try:
        ids = _seed_and_erase(repo, tmp_path, tag)
        # Strict xfail also absorbs a failure here, so the sanity test above is the real guard
        # against a setup bug; these stay for visibility under --runxfail.
        assert _exists(repo, ids["queue"]) is False
        assert _exists(repo, ids["graphiti"]) is False
        # The #423 claim: derived only from the erased episode, so both must be gone.
        survivors = [
            name
            for name, alive in (
                ("entity", _exists(repo, ids["entity"])),
                ("RELATES_TO fact", _edge_exists(repo, ids["edge"])),
            )
            if alive
        ]
        assert survivors == [], f"derived data survived erasure: {survivors}"
    finally:
        _cleanup(repo, tag)
