"""Assistant-turn policy against a REAL Neo4j (online).

Proves the Cypher half of `test_assistant_turn_store_only.py`: the policy finish writes the mode in
the fenced READY set, a stale claim cannot write it, the zero-extraction sweep leaves the row alone,
and the finished node matches one sent with the old explicit store_only request.

Run with:  pytest --run-online tests/test_assistant_turn_store_only_live.py
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]

TEXT = "Multnomah Falls is 2.4 miles round trip."
# Lifecycle and timing fields differ between any two runs; everything else must match.
_VOLATILE = {"uuid", "name", "created_at", "queued_at", "processing_started_at",
             "processing_completed_at", "processing_heartbeat_at", "processing_substage_started_at",
             "reference_time", "valid_at", "content_embedding", "content_embedding_at",
             "last_accessed"}


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuidlib.uuid4().hex[:10]}"


def _episode(graph: Any, ns: str, *, mode: str | None) -> str:
    uid = _uid("ats")
    kw = {"extraction_mode": mode} if mode else {}
    EpisodeRepository(graph).create_pending_episode(
        episode_uuid=uid, name=uid, content=TEXT, session_id="s", user_id="u",
        source="remote-api", source_confidence=0.5, namespace=ns, turn_role="assistant", **kw)
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
    namespace = _uid("ns-assistant-turn")
    yield namespace
    test_neo4j_repo.execute("MATCH (n:Episodic {namespace: $ns}) DETACH DELETE n",
                            params={"ns": namespace})


def test_policy_finish_is_fenced_and_writes_the_mode(test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    uid = _episode(graph, ns, mode=None)
    first = _claim(repo, uid)
    assert first["extraction_mode"] is None and first["turn_role"] == "assistant"
    # Without the policy flag the write still refuses a normal row.
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-a", claim_started_at=first["processing_started_at"]) is False
    # Lease recovery hands the row to worker-b; worker-a's policy finish must not land.
    graph.execute("MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'PENDING'",
                  params={"u": uid})
    second = _claim(repo, uid, worker="worker-b")
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-a", claim_started_at=first["processing_started_at"],
        by_turn_role=True) is False
    node = _node(graph, uid)
    assert node["processing_state"] == "ENRICHING" and node.get("extraction_mode") is None
    assert repo.mark_episode_store_only(
        uid, worker_id="worker-b", claim_started_at=second["processing_started_at"],
        by_turn_role=True) is True
    node = _node(graph, uid)
    assert node["processing_state"] == "READY"
    assert node["extraction_mode"] == EXTRACTION_MODE_STORE_ONLY
    assert node["processing_substage"] == EXTRACTION_MODE_STORE_ONLY
    assert repo.episode_is_store_only(uid) is True
    # READY with nothing extracted: the sweep must leave it, or it would loop forever.
    repo.reset_zero_extraction_episodes()
    assert _node(graph, uid)["processing_state"] == "READY"


def test_policy_node_matches_the_old_explicit_store_only_node(test_neo4j_repo: Any, ns: str) -> None:
    graph = test_neo4j_repo
    repo = EpisodeRepository(graph)
    old = _episode(graph, ns, mode=EXTRACTION_MODE_STORE_ONLY)
    new = _episode(graph, ns, mode=None)
    for uid, by_role in ((old, False), (new, True)):
        claimed = _claim(repo, uid)
        assert repo.set_episode_content_embedding(uid, [1.0, 0.0, 0.0], "m-1")
        assert repo.mark_episode_store_only(
            uid, worker_id="worker-a", claim_started_at=claimed["processing_started_at"],
            by_turn_role=by_role)
    old_props = {k: v for k, v in _node(graph, old).items() if k not in _VOLATILE}
    new_props = {k: v for k, v in _node(graph, new).items() if k not in _VOLATILE}
    assert old_props == new_props
    hits = MemoryGraphAdapter(graph).search_episode_embeddings(
        [1.0, 0.0, 0.0], limit=5, namespace=ns, model="m-1")
    assert sorted(h["uuid"] for h in hits) == sorted([old, new])
