"""Source-memory embedding repair against a REAL Neo4j (online).

Run with:  pytest --run-online tests/test_source_embedding_repair_live.py
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.source_embedding_repair import repair_source_embeddings

pytestmark = [pytest.mark.online]


def _episode(graph: Any, uuid: str, state: str, content: str | None = None) -> None:
    EpisodeRepository(graph).create_pending_episode(
        episode_uuid=uuid, name=f"name-{uuid}", content=content or f"note {uuid}",
        session_id="sess-1", user_id="user-1", source="claude-code", source_confidence=0.9,
        namespace="default",
    )
    graph.execute("MATCH (n:Episodic {uuid: $u}) SET n.processing_state = $s",
                  params={"u": uuid, "s": state})


def _stamp(graph: Any, uuid: str) -> tuple[Any, Any]:
    row = graph.execute("MATCH (n:Episodic {uuid: $u}) RETURN n.content_embedding AS e, "
                        "n.content_embedding_model AS m", params={"u": uuid})[0]
    return row["e"], row["m"]


def test_listing_ready_only_selects_finished_unembedded_episodes(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    for uuid, state in (("r-ready", "READY"), ("r-pending", "PENDING"), ("r-enriching", "ENRICHING"),
                        ("r-failed", "FAILED")):
        _episode(graph, uuid, state)
    _episode(graph, "r-done", "READY")
    EpisodeRepository(graph).set_episode_content_embedding("r-done", [1.0, 0.0], "m-1")
    repo = EpisodeRepository(graph)
    ready = {r["uuid"] for r in repo.list_episodes_missing_content_embedding(None, 50, ready_only=True)}
    every = {r["uuid"] for r in repo.list_episodes_missing_content_embedding(None, 50)}
    assert ready == {"r-ready"}
    assert every == {"r-ready", "r-pending", "r-enriching"}


def test_write_is_refused_when_content_no_longer_matches(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _episode(graph, "c-1", "READY", content="original text")
    repo = EpisodeRepository(graph)
    assert repo.set_episode_content_embedding("c-1", [1.0], "m-1", expected_content="other") is False
    assert _stamp(graph, "c-1") == (None, None)
    assert repo.set_episode_content_embedding("c-1", [1.0], "m-1", expected_content="original text")
    assert _stamp(graph, "c-1") == ([1.0], "m-1")


def test_repair_job_end_to_end_including_an_edit_during_the_embed(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    adapter = MemoryGraphAdapter(graph)
    _episode(graph, "e-ready", "READY", content="ready text")
    _episode(graph, "e-edited", "READY", content="before edit")
    _episode(graph, "e-pending", "PENDING", content="pending text")

    async def embed(text: str) -> list[float]:
        if text == "before edit":  # the episode changes while its vector is being computed
            graph.execute("MATCH (n:Episodic {uuid: 'e-edited'}) SET n.content = 'after edit'")
        return [float(len(text)), 1.0]

    result = asyncio.run(repair_source_embeddings(adapter, embed=embed, model_name=lambda: "m-1"))
    assert _stamp(graph, "e-ready") == ([10.0, 1.0], "m-1")
    assert _stamp(graph, "e-edited") == (None, None)      # stale vector refused
    assert _stamp(graph, "e-pending") == (None, None)     # still owned by the ingest step
    assert result["written"] == 1 and result["skipped"] == 1
    # The edited episode is picked up with its new text on the next run.
    asyncio.run(repair_source_embeddings(adapter, embed=embed, model_name=lambda: "m-1"))
    assert _stamp(graph, "e-edited") == ([10.0, 1.0], "m-1")
