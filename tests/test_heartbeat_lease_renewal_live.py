"""Live-Neo4j counterexamples for heartbeat lease renewal.

Each test claims a real episode with a short lease and races the real stale sweep
(`reset_stale_enriching_episodes`) against the real renewing touch. Offline fakes cannot
show these: they need Neo4j's own `datetime()` arithmetic and nanosecond claim-instant fence.

The stale sweep is database-wide, so assertions are made on THIS episode's row, never on the
global reset count.

Run with:  pytest --run-online -m online tests/test_heartbeat_lease_renewal_live.py
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]

MAX_ATTEMPTS = 10


@pytest.fixture
def graph(test_neo4j_repo) -> MemoryGraphAdapter:
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


@pytest.fixture
def episode(test_neo4j_repo, graph):
    """A fresh PENDING episode in a unique namespace; deleted afterwards."""
    suffix = uuid4().hex[:12]
    episode_uuid = graph.create_pending_episode(
        episode_uuid=f"hb-lease-{suffix}",
        name=f"episode-hb-lease-{suffix}",
        content=f"heartbeat lease probe {suffix}",
        session_id=f"session-hb-{suffix}",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
        namespace=f"hb-lease-ns-{suffix}",
    )
    yield episode_uuid
    test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) DETACH DELETE n", params={"uuid": episode_uuid}
    )


def _row(repo, episode_uuid: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_state AS state,"
        " n.processing_owner AS owner,"
        " toString(n.processing_lease_expires_at) AS lease,"
        " toString(n.processing_heartbeat_at) AS heartbeat,"
        " toString(n.processing_started_at) AS started,"
        " n.processing_lease_expires_at > datetime() AS lease_in_future",
        params={"uuid": episode_uuid},
    )
    assert rows, f"episode {episode_uuid} not found"
    return rows[0]


def _claim(graph, episode_uuid: str, worker_id: str, lease_seconds: int) -> dict:
    claimed = graph.claim_pending_episode(
        episode_uuid,
        max_attempts=MAX_ATTEMPTS,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    assert claimed is not None, "claim failed"
    return claimed


def test_renewed_lease_survives_the_stale_sweep(test_neo4j_repo, graph, episode) -> None:
    """Fails if the renewing touch does not push processing_lease_expires_at out (or the
    sweep stops honouring the lease): the row would be reset despite a live heartbeat."""
    claimed = _claim(graph, episode, "worker-a", lease_seconds=2)

    assert graph.touch_episode_processing_heartbeat(
        episode,
        worker_id="worker-a",
        claim_started_at=claimed["processing_started_at"],
        lease_seconds=30,
        max_enrichment_seconds=3600,
    ) is True
    time.sleep(2.6)  # past the ORIGINAL 2 s lease
    graph.reset_stale_enriching_episodes(max_attempts=MAX_ATTEMPTS)

    row = _row(test_neo4j_repo, episode)
    assert row["state"] == "ENRICHING"
    assert row["owner"] == "worker-a"
    assert row["lease_in_future"] is True


def test_unrenewed_lease_is_reclaimed_by_the_stale_sweep(test_neo4j_repo, graph, episode) -> None:
    """Baseline for the test above: without renewal the 2 s lease expires and the sweep resets
    the row. Fails if the sweep stops reclaiming expired leases (making the survival test vacuous)."""
    _claim(graph, episode, "worker-a", lease_seconds=2)
    time.sleep(2.6)

    graph.reset_stale_enriching_episodes(max_attempts=MAX_ATTEMPTS)

    row = _row(test_neo4j_repo, episode)
    assert row["state"] == "PENDING"
    assert row["owner"] is None


def test_stale_claim_instant_cannot_touch_a_reclaimed_episode(test_neo4j_repo, graph, episode) -> None:
    """Fails if the claim-instant fence is dropped or compared only at second granularity: the
    old claim (same worker id, same row) would renew the NEW claim's lease."""
    first = _claim(graph, episode, "worker-a", lease_seconds=60)
    assert graph.force_release_episode_lease(episode, max_attempts=MAX_ATTEMPTS)
    second = _claim(graph, episode, "worker-a", lease_seconds=60)  # same worker id on purpose
    assert str(first["processing_started_at"]) != str(second["processing_started_at"])
    before = _row(test_neo4j_repo, episode)

    touched = graph.touch_episode_processing_heartbeat(
        episode,
        worker_id="worker-a",
        claim_started_at=first["processing_started_at"],
        lease_seconds=3000,
        max_enrichment_seconds=3600,
    )

    after = _row(test_neo4j_repo, episode)
    assert touched is False
    assert after["lease"] == before["lease"]
    assert after["heartbeat"] == before["heartbeat"]
    assert after["started"] == before["started"]

    # The current claim instant is still accepted, so the False above was the fence, not a bug.
    assert graph.touch_episode_processing_heartbeat(
        episode,
        worker_id="worker-a",
        claim_started_at=second["processing_started_at"],
        lease_seconds=3000,
        max_enrichment_seconds=3600,
    ) is True
    assert _row(test_neo4j_repo, episode)["lease"] != before["lease"]


def test_ceiling_stops_lease_renewal_but_heartbeat_still_lands(test_neo4j_repo, graph, episode) -> None:
    """Fails if the CASE ceiling is removed (lease would move) or if a past-ceiling touch
    returns False/skips the heartbeat stamp (the contract is: True, stamp lands, lease frozen)."""
    claimed = _claim(graph, episode, "worker-a", lease_seconds=60)
    before = _row(test_neo4j_repo, episode)
    time.sleep(1.5)  # now past started_at + 1 s

    touched = graph.touch_episode_processing_heartbeat(
        episode,
        worker_id="worker-a",
        claim_started_at=claimed["processing_started_at"],
        lease_seconds=3000,
        max_enrichment_seconds=1,
    )

    after = _row(test_neo4j_repo, episode)
    assert touched is True
    assert after["lease"] == before["lease"]
    assert after["heartbeat"] != before["heartbeat"]
    assert after["state"] == "ENRICHING"
