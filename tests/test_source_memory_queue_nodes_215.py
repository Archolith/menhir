"""#215: source-memory reads and the embedding backfill select only Menhir's queue `:Episodic`.

`processing_state` cannot discriminate: the node-defaults migration stamps it on Graphiti's
episodes at bootstrap, so both nodes of a memory matched and reads returned it twice.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.domain.episode_nodes import menhir_queue_episode_cypher
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository

pytestmark = [pytest.mark.unit]


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return []


def test_helper_is_valid_at_null_and_validates_identifier() -> None:
    assert menhir_queue_episode_cypher("n") == "n.valid_at IS NULL"
    assert menhir_queue_episode_cypher("ep") == "ep.valid_at IS NULL"
    with pytest.raises(ValueError):
        menhir_queue_episode_cypher("n; MATCH (x) DETACH DELETE x")
    with pytest.raises(ValueError):
        menhir_queue_episode_cypher("")


@pytest.mark.parametrize("namespace", [None, "default", "tenant-a"])
def test_source_memory_search_selects_only_queue_nodes(namespace: str | None) -> None:
    neo4j = _Recording()
    MemoryQueryRepository(neo4j).search_episode_embeddings(  # type: ignore[arg-type]
        [0.1, 0.2], limit=5, namespace=namespace
    )
    query, _params = neo4j.calls[0]
    assert menhir_queue_episode_cypher("n") in query
    # The FAILED exclusion and the processing_state guard stay.
    assert "n.processing_state <> 'FAILED'" in query


@pytest.mark.parametrize("namespace", [None, "default", "tenant-a"])
def test_backfill_listing_selects_only_queue_nodes(namespace: str | None) -> None:
    neo4j = _Recording()
    EpisodeRepository(neo4j).list_episodes_missing_content_embedding(  # type: ignore[arg-type]
        namespace=namespace, limit=10
    )
    query, _params = neo4j.calls[0]
    assert menhir_queue_episode_cypher("n") in query
    assert "n.content_embedding IS NULL" in query
