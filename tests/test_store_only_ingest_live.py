"""Store-only ingest against a REAL Neo4j (online).

Proves the Cypher half of `test_store_only_ingest.py`: the durable mode survives a re-claim, the
READY write is fenced on the live claim, the zero-extraction sweep leaves the row alone, and the
finished row is visible to the source-memory search.

Run with:  pytest --run-online tests/test_store_only_ingest_live.py
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuidlib.uuid4().hex[:10]}"


def _episode(graph: Any, ns: str, *, mode: str | None, content: str = "What should I cook?") -> str:
    uid = _uid("so")
    kw = {"extraction_mode": mode} if mode else {}
    EpisodeRepository(graph).create_pending_episode(
        episode_uuid=uid, name=uid, content=content, session_id="s", user_id="u",
        source="user", source_confidence=0.9, namespace=ns, **kw)
    return uid


def _claim(repo: EpisodeRepository, uid: str, worker: str = "worker-a") -> dict[str, Any]:
    claimed = repo.claim_pending_episode(uid, max_attempts=3, worker_id=worker, lease_seconds=60)
    assert claimed is not None
    return claimed


def _node(graph: Any, uid: str) -> dict[str, Any]:
    return graph.execute("MATCH (n:Episodic {uuid: $u}) RETURN properties(n) AS p",
                         params={"u": uid})[0]["p"]


@pytest.fixture
def ns(test_neo4j_repo: Any):
    namespace = _uid("ns-store-only")
    yield namespace
    test_neo4j_repo.execute("MATCH (n:Episodic {namespace: $ns}) DETACH DELETE n",
                            params={"ns": namespace})


def test_mode_is_durable_across_a_reclaim_and_the_fenced_write_finishes_it(
        test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    uid = _episode(graph, ns, mode=EXTRACTION_MODE_STORE_ONLY)
    first = _claim(repo, uid)
    assert first["extraction_mode"] == EXTRACTION_MODE_STORE_ONLY
    # Lease recovery returns the row to PENDING; the next claim still sees the mode.
    graph.execute("MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'PENDING'",
                  params={"u": uid})
    second = _claim(repo, uid, worker="worker-b")
    assert second["extraction_mode"] == EXTRACTION_MODE_STORE_ONLY
    # The first attempt's fence is stale: its write must not land.
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-a", claim_started_at=first["processing_started_at"]) is False
    assert _node(graph, uid)["processing_state"] == "ENRICHING"
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-b", claim_started_at=second["processing_started_at"]) is True
    node = _node(graph, uid)
    assert node["processing_state"] == "READY"
    assert node["processing_substage"] == EXTRACTION_MODE_STORE_ONLY
    assert node.get("processing_owner") is None
    assert repo.episode_is_store_only(uid) is True


def test_the_store_only_write_refuses_a_normal_episode(test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    uid = _episode(graph, ns, mode=None)
    claimed = _claim(repo, uid)
    assert claimed["extraction_mode"] is None
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) is False
    assert _node(graph, uid)["processing_state"] == "ENRICHING"
    assert repo.episode_is_store_only(uid) is False


def test_zero_extraction_reset_leaves_store_only_alone(test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    store_only = _episode(graph, ns, mode=EXTRACTION_MODE_STORE_ONLY)
    normal = _episode(graph, ns, mode=None)
    claimed = _claim(repo, store_only)
    assert repo.mark_episode_store_only(
        store_only, worker_id="worker-a", claim_started_at=claimed["processing_started_at"])
    # A normal episode READY with nothing extracted is what the sweep exists to retry.
    graph.execute("MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'READY'",
                  params={"u": normal})
    repo.reset_zero_extraction_episodes()
    assert _node(graph, store_only)["processing_state"] == "READY"
    assert _node(graph, normal)["processing_state"] == "PENDING"


def test_a_finished_store_only_episode_is_returned_by_the_source_memory_search(
        test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    uid = _episode(graph, ns, mode=EXTRACTION_MODE_STORE_ONLY)
    claimed = _claim(repo, uid)
    assert repo.set_episode_content_embedding(uid, [1.0, 0.0, 0.0], "m-1")
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-a", claim_started_at=claimed["processing_started_at"])
    hits = MemoryGraphAdapter(graph).search_episode_embeddings(
        [1.0, 0.0, 0.0], limit=5, namespace=ns, model="m-1")
    assert [h["uuid"] for h in hits] == [uid]
