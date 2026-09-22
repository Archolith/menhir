"""#79/#70 transient refunds are atomic with the claim-fenced lifecycle transition.

These tests execute the real Cypher against Neo4j. They assert refund semantics, owner loss,
duplicate idempotency, NULL-attempt accommodation, and the stale-worker interleaving that used
to let a UUID-only refund decrement a newer worker's claim.

Run with: pytest --run-online -m online tests/test_transient_refund_online.py
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph(test_neo4j_repo):
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


def _seed_claimed_episode(
    repo,
    episode_uuid: str,
    *,
    attempts: int | None,
    owner: str | None = "worker-A",
    claim_started_at: str = "2026-09-22T12:00:00Z",
) -> None:
    repo.execute(
        """
        CREATE (n:Episodic {
            uuid: $uuid,
            name: $uuid,
            processing_state: 'ENRICHING',
            processing_attempts: $attempts,
            processing_owner: $owner,
            processing_started_at: datetime($claim_started_at)
        })
        """,
        params={
            "uuid": episode_uuid,
            "attempts": attempts,
            "owner": owner,
            "claim_started_at": claim_started_at,
        },
    )


def _row(repo, episode_uuid: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) "
        "RETURN n.processing_attempts AS attempts, "
        "coalesce(toInteger(n.transient_retries), 0) AS transient, "
        "n.processing_state AS state, n.processing_owner AS owner",
        params={"uuid": episode_uuid},
    )
    assert rows, "seeded episode vanished"
    return rows[0]


@pytest.mark.online
def test_atomic_pending_refund_is_claim_fenced_and_idempotent(graph, test_neo4j_repo):
    _seed_claimed_episode(test_neo4j_repo, "ep-refund-online", attempts=1)
    claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-refund-online"},
    )[0]["started"]

    assert graph.mark_episode_pending(
        "ep-refund-online",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-online")
    assert int(row["attempts"]) == 0, "the claim's attempt was not refunded"
    assert int(row["transient"]) == 1, "the transient counter did not move"
    assert row["state"] == "PENDING"
    assert row["owner"] is None

    assert not graph.mark_episode_pending(
        "ep-refund-online",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-online")
    assert int(row["attempts"]) == 0
    assert int(row["transient"]) == 1


@pytest.mark.online
def test_atomic_refund_clamps_null_attempts_to_zero(graph, test_neo4j_repo):
    _seed_claimed_episode(
        test_neo4j_repo, "ep-refund-null", attempts=None, owner="worker-null"
    )
    claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-refund-null"},
    )[0]["started"]

    assert graph.mark_episode_pending(
        "ep-refund-null",
        worker_id="worker-null",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-null")
    assert int(row["attempts"]) == 0
    assert int(row["transient"]) == 1


@pytest.mark.online
def test_stale_failed_refund_cannot_touch_a_new_claim(graph, test_neo4j_repo):
    _seed_claimed_episode(test_neo4j_repo, "ep-refund-race", attempts=1)
    first_claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-refund-race"},
    )[0]["started"]
    assert graph.mark_episode_failed(
        "ep-refund-race", "connection refused 503", worker_id="worker-A"
    )
    assert graph.claim_pending_episode(
        "ep-refund-race",
        max_attempts=3,
        worker_id="worker-B",
        lease_seconds=900,
    )

    assert not graph.mark_episode_failed(
        "ep-refund-race",
        "connection refused 503",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=first_claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-race")
    assert int(row["attempts"]) == 2
    assert int(row["transient"]) == 0
    assert row["state"] == "ENRICHING"
    assert row["owner"] == "worker-B"


@pytest.mark.online
def test_stale_pending_refund_cannot_touch_same_workers_new_claim(
    graph, test_neo4j_repo
):
    _seed_claimed_episode(test_neo4j_repo, "ep-refund-same-worker", attempts=1)
    first_claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-refund-same-worker"},
    )[0]["started"]
    assert graph.mark_episode_failed(
        "ep-refund-same-worker", "connection refused 503", worker_id="worker-A"
    )
    second_claim = graph.claim_pending_episode(
        "ep-refund-same-worker",
        max_attempts=3,
        worker_id="worker-A",
        lease_seconds=900,
    )
    assert second_claim is not None
    assert second_claim["processing_started_at"] != first_claim_started_at

    assert not graph.mark_episode_pending(
        "ep-refund-same-worker",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=first_claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-same-worker")
    assert int(row["attempts"]) == 2
    assert int(row["transient"]) == 0
    assert row["state"] == "ENRICHING"
    assert row["owner"] == "worker-A"


@pytest.mark.online
def test_atomic_refund_rejects_existing_episode_without_owner(graph, test_neo4j_repo):
    _seed_claimed_episode(
        test_neo4j_repo, "ep-refund-owner-null", attempts=1, owner=None
    )
    claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-refund-owner-null"},
    )[0]["started"]

    assert not graph.mark_episode_pending(
        "ep-refund-owner-null",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-refund-owner-null")
    assert int(row["attempts"]) == 1
    assert int(row["transient"]) == 0
    assert row["state"] == "ENRICHING"
    assert row["owner"] is None


@pytest.mark.online
def test_atomic_refund_without_an_owned_episode_returns_false(graph):
    assert not graph.mark_episode_pending(
        "no-such-episode-online",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at="2026-09-22T12:00:00Z",
    )


@pytest.mark.online
def test_atomic_failed_refund_is_claim_fenced_and_idempotent(graph, test_neo4j_repo):
    _seed_claimed_episode(test_neo4j_repo, "ep-failed-refund-online", attempts=1)
    claim_started_at = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_started_at AS started",
        params={"uuid": "ep-failed-refund-online"},
    )[0]["started"]

    assert graph.mark_episode_failed(
        "ep-failed-refund-online",
        "connection refused 503",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
    row = _row(test_neo4j_repo, "ep-failed-refund-online")
    assert int(row["attempts"]) == 0
    assert int(row["transient"]) == 1
    assert row["state"] == "FAILED"
    assert row["owner"] is None

    assert not graph.mark_episode_failed(
        "ep-failed-refund-online",
        "connection refused 503",
        worker_id="worker-A",
        transient_requeue=True,
        claim_started_at=claim_started_at,
    )
