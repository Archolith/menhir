"""#969 / #970: repair_embedding_dimensions skips blank text, matches identity on write, keeps the dimension."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair_embedding_dimensions.py"
    spec = importlib.util.spec_from_file_location("repair_dims_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


script = _load_script()


class _Neo4j:
    """Node store keyed by elementId; honours the uuid/name identity predicates in the write."""

    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        self.nodes = nodes
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None):
        params = params or {}
        self.queries.append((query, params))
        node = self.nodes.get(params["element_id"])
        ok = (node is not None and node["uuid"] == params["uuid"]
              and node.get("name", node.get("fact")) == params.get("name", params.get("fact")))
        if ok:
            node["embedding"] = params["embedding"]
        return [{"matched": 1 if ok else 0}]


class _Client:
    def __init__(self, vector_size: int = 4) -> None:
        self.inputs: list[list[str]] = []
        self.embeddings = self
        self.size = vector_size

    async def create(self, *, model: str, input: list[str]):
        self.inputs.append(list(input))
        return type("R", (), {"data": [
            type("D", (), {"index": i, "embedding": [float(i)] * self.size})() for i, _ in enumerate(input)
        ]})()


def _row(eid: str, uuid: str, name: str) -> dict[str, Any]:
    return {"element_id": eid, "uuid": uuid, "name": name, "labels": ["Entity"]}


def test_blank_name_is_skipped_and_never_sent_to_the_embedder() -> None:
    neo4j = _Neo4j({"1": {"uuid": "u1", "name": "alpha"}, "3": {"uuid": "u3", "name": "gamma"}})
    client = _Client()
    rows = [_row("1", "u1", "alpha"), _row("2", "u2", "   "), _row("3", "u3", "gamma")]
    result = asyncio.run(script._reembed_nodes(
        neo4j, client=client, model="m", rows=rows, label="Entity", expected_dim=4))
    assert client.inputs == [["alpha", "gamma"]]
    assert result["updated"] == 2 and result["skipped"] == 1


def test_renamed_node_is_reported_not_overwritten() -> None:
    neo4j = _Neo4j({"1": {"uuid": "u1", "name": "renamed"}})
    result = asyncio.run(script._reembed_nodes(
        neo4j, client=_Client(), model="m", rows=[_row("1", "u1", "alpha")], label="Entity",
        expected_dim=4))
    assert result["updated"] == 0 and result["stale"] == 1
    assert "embedding" not in neo4j.nodes["1"]
    query = neo4j.queries[0][0]
    assert "n.uuid" in query and "n.name = $name" in query and "labels(n)" in query


def test_relationship_write_is_typed_and_checks_identity() -> None:
    neo4j = _Neo4j({"9": {"uuid": "r1", "fact": "changed"}})
    row = {"element_id": "9", "uuid": "r1", "rel_type": "RELATES_TO", "start_uuid": "a",
           "end_uuid": "b", "fact": "original"}
    result = asyncio.run(script._reembed_relationships(
        neo4j, client=_Client(), model="m", rows=[row, {**row, "element_id": "8", "fact": " "}],
        expected_dim=4))
    assert result["stale"] == 1 and result["updated"] == 0 and result["skipped"] == 1
    assert "[r:RELATES_TO]" in neo4j.queries[0][0]


def test_oversized_vector_is_truncated_and_short_vector_refused() -> None:
    neo4j = _Neo4j({"1": {"uuid": "u1", "name": "alpha"}})
    asyncio.run(script._reembed_nodes(
        neo4j, client=_Client(vector_size=10), model="m", rows=[_row("1", "u1", "alpha")],
        label="Entity", expected_dim=4))
    assert len(neo4j.nodes["1"]["embedding"]) == 4

    neo4j = _Neo4j({"1": {"uuid": "u1", "name": "alpha"}})
    result = asyncio.run(script._reembed_nodes(
        neo4j, client=_Client(vector_size=2), model="m", rows=[_row("1", "u1", "alpha")],
        label="Entity", expected_dim=4))
    assert result["wrong_dimension"] == 1 and result["updated"] == 0
    assert neo4j.queries == []
