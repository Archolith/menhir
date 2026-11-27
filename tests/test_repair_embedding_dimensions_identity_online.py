"""#970 online: the repair's identity-matched writes against a real Neo4j.

The offline fakes model the predicates; these run the emitted Cypher. A snapshot row whose
node or edge changed since the snapshot must report `matched: 0` and leave the vector alone.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.online


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair_embedding_dimensions.py"
    spec = importlib.util.spec_from_file_location("repair_dims_online_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


script = _load_script()


class _Client:
    def __init__(self, size: int) -> None:
        self.embeddings = self
        self.size = size

    async def create(self, *, model: str, input: list[str]):
        return type("R", (), {"data": [
            type("D", (), {"index": i, "embedding": [0.25] * self.size})() for i, _ in enumerate(input)
        ]})()


def _seed(repo) -> dict[str, Any]:
    repo.execute("MATCH (n) DETACH DELETE n")
    rows = repo.execute(
        """
        CREATE (a:Entity {uuid: 'a', name: 'alpha'})
        CREATE (b:Entity {uuid: 'b', name: 'beta'})
        CREATE (c:Entity {uuid: 'c', name: 'gamma'})
        CREATE (a)-[r1:RELATES_TO {uuid: 'r1', fact: 'alpha knows beta'}]->(b)
        CREATE (a)-[r2:RELATES_TO {uuid: 'r2', fact: 'alpha knows gamma'}]->(c)
        RETURN elementId(a) AS a, elementId(b) AS b, elementId(c) AS c,
               elementId(r1) AS r1, elementId(r2) AS r2
        """
    )
    return rows[0]


def test_node_write_updates_the_snapshotted_node_and_skips_a_renamed_one(test_neo4j_repo) -> None:
    ids = _seed(test_neo4j_repo)
    # Renamed after the snapshot: the stored name no longer matches the row.
    test_neo4j_repo.execute("MATCH (n:Entity {uuid: 'b'}) SET n.name = 'beta-renamed'")
    rows = [
        {"element_id": ids["a"], "uuid": "a", "name": "alpha", "labels": ["Entity"]},
        {"element_id": ids["b"], "uuid": "b", "name": "beta", "labels": ["Entity"]},
    ]

    result = asyncio.run(script._reembed_nodes(
        test_neo4j_repo, client=_Client(4), model="m", rows=rows, label="Entity", expected_dim=4,
    ))

    assert result == {"updated": 1, "skipped": 0, "stale": 1, "wrong_dimension": 0}
    got = {r["uuid"]: r["emb"] for r in test_neo4j_repo.execute(
        "MATCH (n:Entity) RETURN n.uuid AS uuid, n.name_embedding AS emb")}
    assert got["a"] == [0.25] * 4
    assert got["b"] is None


def test_relationship_write_checks_the_fact_and_both_ends(test_neo4j_repo) -> None:
    ids = _seed(test_neo4j_repo)
    # r2's fact changed after the snapshot.
    test_neo4j_repo.execute("MATCH ()-[r {uuid: 'r2'}]->() SET r.fact = 'alpha left gamma'")
    rows = [
        {"element_id": ids["r1"], "uuid": "r1", "rel_type": "RELATES_TO", "fact": "alpha knows beta",
         "start_uuid": "a", "end_uuid": "b"},
        {"element_id": ids["r2"], "uuid": "r2", "rel_type": "RELATES_TO", "fact": "alpha knows gamma",
         "start_uuid": "a", "end_uuid": "c"},
        # Right edge, wrong recorded end: identity includes the endpoints.
        {"element_id": ids["r1"], "uuid": "r1", "rel_type": "RELATES_TO", "fact": "alpha knows beta",
         "start_uuid": "a", "end_uuid": "c"},
    ]

    result = asyncio.run(script._reembed_relationships(
        test_neo4j_repo, client=_Client(4), model="m", rows=rows, expected_dim=4,
    ))

    assert result == {"updated": 1, "skipped": 0, "stale": 2, "wrong_dimension": 0}
    got = {r["uuid"]: r["emb"] for r in test_neo4j_repo.execute(
        "MATCH ()-[r:RELATES_TO]->() RETURN r.uuid AS uuid, r.fact_embedding AS emb")}
    assert got["r1"] == [0.25] * 4
    assert got["r2"] is None


def test_a_deleted_node_reports_stale_not_an_error(test_neo4j_repo) -> None:
    ids = _seed(test_neo4j_repo)
    test_neo4j_repo.execute("MATCH (n:Entity {uuid: 'c'}) DETACH DELETE n")
    rows = [{"element_id": ids["c"], "uuid": "c", "name": "gamma", "labels": ["Entity"]}]

    result = asyncio.run(script._reembed_nodes(
        test_neo4j_repo, client=_Client(6), model="m", rows=rows, label="Entity", expected_dim=4,
    ))

    # A 6-dim vector is cut to 4 like Graphiti's embedder; the write then finds no node.
    assert result == {"updated": 0, "skipped": 0, "stale": 1, "wrong_dimension": 0}
