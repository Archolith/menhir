"""Live-Neo4j counterexamples for attempt-ownership fences and ingest-key release on delete.

Offline fakes cannot show these: they need Neo4j's own temporal comparison of the claim instant
(epochSeconds + nanosecond) and a real REMOVE of the uniquely constrained `ingest_key`.

Run with:  pytest --run-online -m online tests/test_attempt_ownership_fences_live.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from menhir.domain import IngestStatus, MemorySession
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.ingest_intake import IngestIntakeMixin

pytestmark = [pytest.mark.online]

MAX_ATTEMPTS = 10
WORKER = "worker-same"


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


@pytest.fixture
def graph(test_neo4j_repo) -> MemoryGraphAdapter:
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    return adapter


@pytest.fixture
def namespaces(test_neo4j_repo):
    created: list[str] = []
    yield created
    for ns in created:
        test_neo4j_repo.execute(
            "MATCH (n:Episodic {namespace: $ns}) DETACH DELETE n", params={"ns": ns}
        )


def _new_episode(graph, namespaces) -> str:
    suffix = uuid4().hex[:12]
    ns = f"fence-ns-{suffix}"
    namespaces.append(ns)
    return graph.create_pending_episode(
        episode_uuid=f"fence-{suffix}",
        name=f"episode-fence-{suffix}",
        content=f"ownership fence probe {suffix}",
        session_id=f"session-fence-{suffix}",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
        namespace=ns,
    )


def _row(repo, episode_uuid: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_state AS state,"
        " n.processing_owner AS owner,"
        " toString(n.processing_started_at) AS started,"
        " n.processing_error AS error,"
        " n.resolved_episode_uuid AS resolved",
        params={"uuid": episode_uuid},
    )
    assert rows, f"episode {episode_uuid} not found"
    return rows[0]


def _claim(graph, episode_uuid: str) -> dict:
    claimed = graph.claim_pending_episode(
        episode_uuid, max_attempts=MAX_ATTEMPTS, worker_id=WORKER, lease_seconds=60
    )
    assert claimed is not None, "claim failed"
    return claimed


def _two_claims_by_the_same_worker(graph, episode_uuid: str) -> tuple[object, object]:
    """Claim, force-release, re-claim with the SAME worker id: returns (t1, t2)."""
    first = _claim(graph, episode_uuid)
    assert graph.force_release_episode_lease(episode_uuid, max_attempts=MAX_ATTEMPTS)
    second = _claim(graph, episode_uuid)
    t1, t2 = first["processing_started_at"], second["processing_started_at"]
    assert str(t1) != str(t2), "claims must have distinct instants for this test to mean anything"
    return t1, t2


def test_stale_attempt_cannot_mark_ready_a_newer_claim(test_neo4j_repo, graph, namespaces) -> None:
    """Fails if mark_episode_ready fences on owner alone: attempt 1 (same worker id) would
    finalize attempt 2's claim, and attempt 2's work would be lost or double-finalized.
    Also fails if the current instant stops being accepted (the False would be a bug, not a fence)."""
    episode = _new_episode(graph, namespaces)
    t1, t2 = _two_claims_by_the_same_worker(graph, episode)
    before = _row(test_neo4j_repo, episode)
    assert before["state"] == "ENRICHING" and before["owner"] == WORKER

    stale_ready = graph.mark_episode_ready(
        episode, worker_id=WORKER, claim_started_at=t1,
        resolved_episode_uuid="resolved-stale", nodes_touched=1, edges_touched=1,
    )
    stale_failed = graph.mark_episode_failed(
        episode, "stale failure", worker_id=WORKER, claim_started_at=t1
    )

    after = _row(test_neo4j_repo, episode)
    assert stale_ready is False
    assert stale_failed is False
    assert after == before  # untouched: still ENRICHING under the t2 claim
    assert after["state"] == "ENRICHING"

    assert graph.mark_episode_ready(
        episode, worker_id=WORKER, claim_started_at=t2,
        resolved_episode_uuid="resolved-current", nodes_touched=2, edges_touched=3,
    ) is True
    done = _row(test_neo4j_repo, episode)
    assert done["state"] == "READY"
    assert done["resolved"] == "resolved-current"


def test_stale_attempt_cannot_mark_failed_a_newer_claim(test_neo4j_repo, graph, namespaces) -> None:
    """Fails if the NON-transient mark_episode_failed owner branch ignores the claim instant
    (the legacy text only demanded it for transient requeues)."""
    episode = _new_episode(graph, namespaces)
    t1, t2 = _two_claims_by_the_same_worker(graph, episode)
    before = _row(test_neo4j_repo, episode)

    assert graph.mark_episode_failed(
        episode, "stale failure", worker_id=WORKER, claim_started_at=t1
    ) is False
    assert _row(test_neo4j_repo, episode) == before
    assert before["state"] == "ENRICHING"

    assert graph.mark_episode_failed(
        episode, "current failure", worker_id=WORKER, claim_started_at=t2
    ) is True
    done = _row(test_neo4j_repo, episode)
    assert done["state"] == "FAILED"
    assert done["error"] == "current failure"


def test_delete_of_a_pending_anchor_releases_its_ingest_key(test_neo4j_repo, graph, namespaces) -> None:
    """Fails if delete_memory keeps n.ingest_key / n.ingest_request_sha256 on the surviving
    PENDING/ENRICHING anchor: the unique key would stay bound to a dead, content-less node and a
    resubmit would be a 'replay' of an erased memory (or a conflict) instead of a new anchor."""
    ns = f"fence-del-{uuid4().hex[:10]}"
    namespaces.append(ns)
    key = "k-delete-release"
    session = MemorySession(
        session_id=f"s-{uuid4().hex[:8]}", user_id="u",
        started_at=datetime.now(timezone.utc), client_name="live-test",
    )

    def submit():
        return asyncio.run(
            _Intake(graph).queue_episode_for_enrichment(
                episode="remember this once", session=session, source="claude-code",
                namespace=ns, idempotency_key=key,
            )
        )

    first = submit()
    assert first.status == IngestStatus.QUEUED and first.idempotent_replay is False
    stamped = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $u}) RETURN n.ingest_key AS k, n.ingest_request_sha256 AS d,"
        " n.processing_state AS state",
        params={"u": first.episode_id},
    )[0]
    assert stamped["k"] and stamped["d"] and stamped["state"] == "PENDING"

    assert graph.delete_memory(first.episode_id) is True

    node = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $u}) RETURN n.ingest_key AS k, n.ingest_request_sha256 AS d,"
        " n.content AS content, n.processing_state AS state, n.processing_error AS error",
        params={"u": first.episode_id},
    )
    # The delete query intends to keep this anchor as FAILED, but its later UNION branch sees the
    # SET and hard-deletes it (pre-existing). Either outcome must leave no key or text behind.
    for row in node:
        assert row["k"] is None
        assert row["d"] is None
        assert row["content"] is None
        assert row["state"] == "FAILED" and row["error"] == "deleted_by_user"

    second = submit()
    assert second.idempotent_replay is False
    assert second.status == IngestStatus.QUEUED
    assert second.episode_id and second.episode_id != first.episode_id
    live = test_neo4j_repo.execute(
        "MATCH (n:Episodic {namespace: $ns}) WHERE n.ingest_key IS NOT NULL RETURN n.uuid AS uuid",
        params={"ns": ns},
    )
    assert [r["uuid"] for r in live] == [second.episode_id]
