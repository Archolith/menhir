"""Unit coverage for the default-off content-vector infrastructure seam."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.memory_queries import MemoryQueryRepository


@dataclass
class _RecordingRepository:
    rows: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None):
        self.calls.append((query, params))
        return self.rows


def test_content_embedding_query_is_scoped_and_read_only() -> None:
    neo4j = _RecordingRepository(rows=[{"uuid": "u1", "name": "N", "cosine": 0.8}])
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    rows = repository.search_content_embeddings(
        [0.1, 0.2], limit=999, group_ids=["tenant-a"]
    )

    assert rows == neo4j.rows
    query, params = neo4j.calls[0]
    assert "MATCH (n:Entity)" in query
    assert "vector.similarity.cosine" in query
    assert not any(word in query.upper() for word in (" CREATE ", " MERGE ", " SET ", " DELETE "))
    assert params == {
        "query_vector": [0.1, 0.2], "group_ids": ["tenant-a"], "limit": 500
    }


@pytest.mark.asyncio
async def test_embed_query_reuses_graphiti_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    class Embedder:
        async def create(self, text: str) -> list[int]:
            assert text == "hello"
            return [1, 2]

    client = GraphitiClient(client=object(), embedder_ref=Embedder())  # type: ignore[arg-type]

    async def _alive(*, task: str) -> None:
        assert task == "memory: content-vector query embedding"

    monkeypatch.setattr(client, "_ensure_graphiti_endpoints_alive", _alive)
    assert await client.embed_query("hello") == [1.0, 2.0]
