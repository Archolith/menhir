"""Live-graph regression for the #79/#70 transient-exhausted recovery arm.

The v0.2.2 tag shipped `fail_transient_exhausted_pending_episodes()` referencing
`LLM_RESET_SET` without importing it: every offline recovery test passed because it went
through a stub adapter, the CI lint lane flagged the undefined name but the tests lane was
green, and the PyPI job published the failing SHA. These tests invoke the REAL repository
method against a real Neo4j through the same `MemoryGraphAdapter` surface
`_recover_stale_episode_leases` uses, so a missing import, a malformed statement, or wrong
parking semantics fail here instead of at first pending-recovery sweep in production.

Covers the three required recovery shapes: the repository method itself (only rows at the
transient cap park; resumable rows survive), startup/resume (`resume_pending_episodes`),
and the recurring sweep (`recover_stale_enrichment_leases`) staying stable after parking.

Run with:  pytest --run-online -m online tests/test_transient_exhausted_recovery_live.py
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.episode_lifecycle import TRANSIENT_RETRY_CAP
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services import IngestService

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph(test_neo4j_repo) -> MemoryGraphAdapter:
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


def _create_pending_episode(graph: MemoryGraphAdapter, suffix: str) -> str:
    return graph.create_pending_episode(
        episode_uuid=f"pending-transient-{suffix}",
        name=f"episode-session-live-pending-transient-{suffix}",
        content=f"transient recovery probe {suffix}",
        session_id="session-live",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )


def _exhaust_transient_budget(graph: MemoryGraphAdapter, episode_uuid: str) -> None:
    """Drive a PENDING episode to the transient cap through the real refund protocol."""
    for attempt in range(TRANSIENT_RETRY_CAP):
        worker_id = f"transient-worker-{attempt}"
        claimed = graph.claim_pending_episode(
            episode_uuid,
            max_attempts=3,
            worker_id=worker_id,
            lease_seconds=900,
        )
        assert claimed is not None
        assert graph.mark_episode_pending(
            episode_uuid,
            worker_id=worker_id,
            transient_requeue=True,
            claim_started_at=claimed["processing_started_at"],
        )


def _episode_row(test_neo4j_repo, episode_uuid: str) -> dict:
    rows = test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) RETURN n.processing_state AS state,"
        " n.processing_substage AS substage, n.processing_error AS error,"
        " n.processing_llm_active_task AS llm_active_task",
        params={"uuid": episode_uuid},
    )
    assert rows, f"episode {episode_uuid} not found"
    return rows[0]


def _service(
    graph: MemoryGraphAdapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[str],
) -> IngestService:
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=graph,
        llm=stub_llm_adapter,
    )

    async def _noop_worker() -> None:
        return None

    async def _record_enqueue(episode_uuid: str) -> None:
        queued.append(episode_uuid)
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _record_enqueue)
    return service


@pytest.mark.asyncio
async def test_transient_exhausted_parks_only_capped_rows(
    test_neo4j_repo,
    graph: MemoryGraphAdapter,
) -> None:
    exhausted = _create_pending_episode(graph, "exhausted")
    resumable = _create_pending_episode(graph, "resumable")
    _exhaust_transient_budget(graph, exhausted)

    failed = graph.fail_transient_exhausted_pending_episodes()

    assert failed == 1
    row = _episode_row(test_neo4j_repo, exhausted)
    assert row["state"] == "FAILED"
    assert row["substage"] == "pending_transient_exhausted"
    assert row["error"] == "pending_transient_exhausted"
    assert row["llm_active_task"] is None
    survivor = _episode_row(test_neo4j_repo, resumable)
    assert survivor["state"] == "PENDING"

    # A recurring sweep must not re-fail or double-count anything.
    assert graph.fail_transient_exhausted_pending_episodes() == 0


@pytest.mark.asyncio
async def test_resume_pending_episodes_queues_resumable_and_parks_transient_exhausted(
    test_neo4j_repo,
    graph: MemoryGraphAdapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exhausted = _create_pending_episode(graph, "resume-exhausted")
    resumable = _create_pending_episode(graph, "resume-resumable")
    _exhaust_transient_budget(graph, exhausted)
    queued: list[str] = []
    service = _service(
        graph, stub_graphiti_client, stub_llm_adapter, monkeypatch, queued
    )

    resumed = await service.resume_pending_episodes(limit=10)

    assert resumed == 1
    assert queued == [resumable]
    assert _episode_row(test_neo4j_repo, exhausted)["state"] == "FAILED"
    assert _episode_row(test_neo4j_repo, resumable)["state"] == "PENDING"


@pytest.mark.asyncio
async def test_recurring_recovery_stays_stable_after_parking(
    test_neo4j_repo,
    graph: MemoryGraphAdapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exhausted = _create_pending_episode(graph, "recurring-exhausted")
    resumable = _create_pending_episode(graph, "recurring-resumable")
    _exhaust_transient_budget(graph, exhausted)
    queued: list[str] = []
    service = _service(
        graph, stub_graphiti_client, stub_llm_adapter, monkeypatch, queued
    )

    first_resets, first_queued = await service.recover_stale_enrichment_leases(limit=10)
    assert first_resets == 0
    assert first_queued == 1
    assert sorted(queued) == sorted([resumable])
    assert _episode_row(test_neo4j_repo, exhausted)["state"] == "FAILED"

    # The second sweep re-offers only the still-PENDING work; the parked row never
    # resurrects and never re-enters the failed count.
    second_resets, second_queued = await service.recover_stale_enrichment_leases(
        limit=10
    )
    assert second_resets == 0
    assert second_queued == 1
    assert queued == [resumable, resumable]
    parked = _episode_row(test_neo4j_repo, exhausted)
    assert parked["state"] == "FAILED"
    assert parked["substage"] == "pending_transient_exhausted"
