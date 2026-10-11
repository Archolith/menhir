"""Live-graph regression: the structured speaker role persists on the pending episode (#271).

`create_pending_episode(turn_role=...)` writes `n.turn_role` ON CREATE only, and
`claim_pending_episode` returns it as the `turn_role` column, including after a requeue.

Run with:  pytest --run-online -m online tests/test_turn_role_lifecycle_live.py
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph(test_neo4j_repo) -> MemoryGraphAdapter:
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


def _create(graph: MemoryGraphAdapter, suffix: str, **extra) -> str:
    return graph.create_pending_episode(
        episode_uuid=f"pending-turn-role-{suffix}",
        name=f"episode-session-live-turn-role-{suffix}",
        content=f"turn role probe {suffix}",
        session_id="session-live",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
        **extra,
    )


def _claim(graph: MemoryGraphAdapter, episode_uuid: str, worker_id: str = "worker-role") -> dict:
    claimed = graph.claim_pending_episode(
        episode_uuid, max_attempts=3, worker_id=worker_id, lease_seconds=900,
    )
    assert claimed is not None
    return claimed


def test_claim_returns_the_turn_role_given_at_creation(graph: MemoryGraphAdapter) -> None:
    episode_uuid = _create(graph, "assistant", turn_role="assistant")
    assert _claim(graph, episode_uuid)["turn_role"] == "assistant"


def test_episode_created_without_a_turn_role_claims_as_none(graph: MemoryGraphAdapter) -> None:
    episode_uuid = _create(graph, "absent")
    claimed = _claim(graph, episode_uuid)
    assert "turn_role" in claimed
    assert claimed["turn_role"] is None


def test_second_create_with_the_same_uuid_does_not_change_the_turn_role(
    graph: MemoryGraphAdapter,
) -> None:
    episode_uuid = _create(graph, "on-create", turn_role="assistant")
    _create(graph, "on-create", turn_role="user")
    assert _claim(graph, episode_uuid)["turn_role"] == "assistant"


def test_second_create_cannot_add_a_role_to_a_role_less_episode(
    graph: MemoryGraphAdapter,
) -> None:
    episode_uuid = _create(graph, "no-late-role")
    _create(graph, "no-late-role", turn_role="user")
    assert _claim(graph, episode_uuid)["turn_role"] is None


def test_turn_role_survives_a_requeue_and_reclaim(graph: MemoryGraphAdapter) -> None:
    episode_uuid = _create(graph, "requeue", turn_role="tool")
    first = _claim(graph, episode_uuid, worker_id="worker-1")
    assert first["turn_role"] == "tool"

    assert graph.mark_episode_pending(
        episode_uuid,
        worker_id="worker-1",
        transient_requeue=True,
        claim_started_at=first["processing_started_at"],
    )
    again = _claim(graph, episode_uuid, worker_id="worker-2")
    assert again["turn_role"] == "tool"
