"""Unit tests for service orchestration and adapter plumbing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from menhir.domain import IngestStatus, new_session
from menhir.config import MemorySettings
from menhir.core import BuildArtifacts, build_memory_services, prepare_memory_runtime
from menhir.domain.models import ProcessingState
from menhir.infrastructure import MemoryGraphAdapter
from menhir.infrastructure.observability import LLMUsageEvent
from menhir.infrastructure.scheduler_trace import build_episode_scheduler_task
from menhir.services import IngestService, LifecycleService, MaintenanceScheduler, RecallService, ScoringService
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.scheduler_lease import SchedulerLeaseStore


@pytest.mark.unit
def test_build_memory_services_wires_shared_graph_adapter(
    monkeypatch: pytest.MonkeyPatch,
    stub_graphiti_client: "StubGraphitiClient",
) -> None:
    monkeypatch.setattr(
        "menhir.core.bootstrap.GraphitiClient.from_settings_with_capabilities",
        lambda settings, **kwargs: stub_graphiti_client,
    )
    built = build_memory_services()

    assert isinstance(built, BuildArtifacts)
    assert isinstance(built.graph_adapter, MemoryGraphAdapter)
    assert built.graph_adapter is built.recall_service.graph_adapter
    assert built.graph_adapter is built.lifecycle_service.graph_adapter
    assert built.graphiti_client is built.ingest_service.graphiti_client
    assert built.graphiti_client is built.recall_service.graphiti_client
    assert built.recall_service.ingest_service is built.ingest_service
    assert built.recall_service.lifecycle_service is built.lifecycle_service
    assert built.ingest_service.lifecycle_service is built.lifecycle_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_extracts_and_stamps(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    """Worker happy path: Graphiti extracts nodes/edges, they are stamped through the
    choke point with the episode's session/source, and the episode is marked READY."""
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-extract",
        name="episode-session-1-pending-extract",
        content="first event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
    assert len(stub_graphiti_client.add_episode_calls) == 1
    assert len(adapter.stamp_calls) == 1
    assert adapter.stamp_calls[0]["session_id"] == "session-1"
    assert adapter.stamp_calls[0]["source"] == "unit-test"
    assert adapter.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_episode_for_enrichment_returns_queued(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    session = new_session("test-user", session_id="session-queued-1")

    async def _noop_worker() -> None:
        return None

    async def _noop_enqueue(episode_uuid: str) -> None:
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _noop_enqueue)

    result = await service.queue_episode_for_enrichment("queued event", session, "unit-test")

    assert result.status is IngestStatus.QUEUED
    assert result.nodes_touched == 1
    assert result.edges_touched == 0
    assert result.episode_id in adapter.pending_episode_rows
    assert adapter.pending_episode_rows[result.episode_id]["processing_state"] == "PENDING"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_pending_episodes_resets_stale_enriching_leases(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["stale-1"] = {
        "uuid": "stale-1",
        "name": "episode-session-1-stale-1",
        "content": "stale queued event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "ENRICHING",
        "processing_attempts": 1,
        "processing_owner": "dead-worker",
        "processing_lease_expires_at": 1,
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    queued: list[str] = []

    async def _noop_worker() -> None:
        return None

    async def _record_enqueue(episode_uuid: str) -> None:
        queued.append(episode_uuid)
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _record_enqueue)

    resumed = await service.resume_pending_episodes(limit=10)

    assert resumed == 1
    assert queued == ["stale-1"]
    assert adapter.pending_episode_rows["stale-1"]["processing_state"] == "PENDING"
    assert adapter.pending_episode_rows["stale-1"]["processing_error"] == "lease_expired_reset"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_pending_episodes_marks_exhausted_stale_rows_failed(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["stale-exhausted-1"] = {
        "uuid": "stale-exhausted-1",
        "name": "episode-session-1-stale-exhausted-1",
        "content": "stale queued event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "ENRICHING",
        "processing_attempts": 3,
        "processing_owner": "dead-worker",
        "processing_lease_expires_at": 1,
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    queued: list[str] = []

    async def _noop_worker() -> None:
        return None

    async def _record_enqueue(episode_uuid: str) -> None:
        queued.append(episode_uuid)
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _record_enqueue)

    resumed = await service.resume_pending_episodes(limit=10)

    assert resumed == 0
    assert queued == []
    assert adapter.pending_episode_rows["stale-exhausted-1"]["processing_state"] == "FAILED"
    assert (
        adapter.pending_episode_rows["stale-exhausted-1"]["processing_error"]
        == "lease_expired_reset_attempts_exhausted"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_pending_episodes_resets_orphaned_enriching_rows(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["orphan-1"] = {
        "uuid": "orphan-1",
        "name": "episode-session-1-orphan-1",
        "content": "orphaned event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "ENRICHING",
        "processing_attempts": 1,
        "processing_owner": None,
        "processing_lease_expires_at": 999999,
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    queued: list[str] = []

    async def _noop_worker() -> None:
        return None

    async def _record_enqueue(episode_uuid: str) -> None:
        queued.append(episode_uuid)
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _record_enqueue)

    resumed = await service.resume_pending_episodes(limit=10)

    assert resumed == 1
    assert queued == ["orphan-1"]
    assert adapter.pending_episode_rows["orphan-1"]["processing_state"] == "PENDING"
    assert adapter.pending_episode_rows["orphan-1"]["processing_error"] == "orphaned_enriching_reset"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_pending_episodes_skips_exhausted_pending_rows(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["pending-exhausted"] = {
        "uuid": "pending-exhausted",
        "name": "episode-session-1-pending-exhausted",
        "content": "stuck pending event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "PENDING",
        "processing_attempts": 3,
        "processing_owner": None,
        "processing_lease_expires_at": None,
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    queued: list[str] = []

    async def _noop_worker() -> None:
        return None

    async def _record_enqueue(episode_uuid: str) -> None:
        queued.append(episode_uuid)
        service._queued_episode_ids.add(episode_uuid)

    monkeypatch.setattr(service, "_ensure_enrichment_worker", _noop_worker)
    monkeypatch.setattr(service, "_enqueue_pending_episode", _record_enqueue)

    resumed = await service.resume_pending_episodes(limit=10)

    assert resumed == 0
    assert queued == []
    assert adapter.pending_episode_rows["pending-exhausted"]["processing_state"] == "FAILED"
    assert adapter.pending_episode_rows["pending-exhausted"]["processing_attempts"] == 3
    assert (
        adapter.pending_episode_rows["pending-exhausted"]["processing_substage"]
        == "pending_attempts_exhausted"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_ready_after_graphiti(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-1",
        name="episode-session-1-pending-1",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
    assert adapter.pending_episode_rows[episode_uuid]["processing_stage"] == "ready"
    assert adapter.pending_episode_rows[episode_uuid]["processing_progress"] == 100.0
    assert adapter.pending_episode_rows[episode_uuid]["processing_steps_completed"] == 5
    assert adapter.pending_episode_rows[episode_uuid]["processing_owner"] is None
    assert adapter.pending_episode_rows[episode_uuid]["processing_lease_expires_at"] is None
    assert adapter.pending_episode_rows[episode_uuid]["resolved_episode_uuid"] == "episode-1"
    assert len(stub_graphiti_client.add_episode_calls) == 1
    assert len(adapter.stamp_calls) == 1


def _instrument_add_episode_concurrency(stub_graphiti_client, monkeypatch) -> dict:
    """Wrap the stub's add_episode to record peak simultaneous occupancy."""
    state = {"inside": 0, "peak": 0}
    orig = stub_graphiti_client.add_episode

    async def tracking_add(**kwargs):
        state["inside"] += 1
        state["peak"] = max(state["peak"], state["inside"])
        try:
            await asyncio.sleep(0.02)
            return await orig(**kwargs)
        finally:
            state["inside"] -= 1

    monkeypatch.setattr(stub_graphiti_client, "add_episode", tracking_add)
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_enrichment_worker_spawns_pool_of_size_concurrency(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    service = IngestService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter, llm=stub_llm_adapter,
    )
    service.configure(ingest_concurrency=4)
    await service._ensure_enrichment_worker()
    try:
        assert len(service._worker_tasks) == 4
        # Idempotent: a second call does not over-spawn.
        await service._ensure_enrichment_worker()
        assert len(service._worker_tasks) == 4
    finally:
        for task in service._worker_tasks:
            task.cancel()
        await asyncio.gather(*service._worker_tasks, return_exceptions=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_enrichment_parallelizes_across_namespaces(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(graphiti_client=stub_graphiti_client, graph_adapter=adapter, llm=stub_llm_adapter)
    service.configure(ingest_concurrency=3)
    state = _instrument_add_episode_concurrency(stub_graphiti_client, monkeypatch)

    uuids = []
    for i in range(6):
        uuids.append(
            adapter.create_pending_episode(
                episode_uuid=f"pending-{i}", name=f"episode-session-{i}-pending-{i}",
                content="queued event", session_id=f"session-{i}", user_id="user-1",
                source="unit-test", source_confidence=0.5, namespace=f"ns-{i}",
            )
        )

    await asyncio.gather(*[service._process_pending_episode(u) for u in uuids])

    # Distinct namespaces -> parallel, bounded by ingest_concurrency=3.
    assert state["peak"] == 3
    assert len(stub_graphiti_client.add_episode_calls) == 6
    assert all(adapter.pending_episode_rows[u]["processing_state"] == "READY" for u in uuids)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_enrichment_serializes_within_one_namespace(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(graphiti_client=stub_graphiti_client, graph_adapter=adapter, llm=stub_llm_adapter)
    service.configure(ingest_concurrency=3)  # global headroom...
    state = _instrument_add_episode_concurrency(stub_graphiti_client, monkeypatch)

    uuids = []
    for i in range(4):
        uuids.append(
            adapter.create_pending_episode(
                episode_uuid=f"pending-{i}", name=f"episode-session-{i}-pending-{i}",
                content="queued event", session_id=f"session-{i}", user_id="user-1",
                source="unit-test", source_confidence=0.5, namespace="shared-ns",
            )
        )

    await asyncio.gather(*[service._process_pending_episode(u) for u in uuids])

    # ...but the same namespace must stay single-flight (dedup safety).
    assert state["peak"] == 1
    assert len(stub_graphiti_client.add_episode_calls) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_direct_call_still_processes_failed_context_window_rows(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["pending-context-1"] = {
        "uuid": "pending-context-1",
        "name": "episode-session-1-pending-context-1",
        "content": "queued event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "FAILED",
        "processing_attempts": 3,
        "processing_owner": None,
        "processing_lease_expires_at": None,
        "processing_error": "Error code: 400 - {'error': 'Cannot truncate prompt with n_keep (8199) >= n_ctx (4096)'}",
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    await service._process_pending_episode("pending-context-1")


    assert adapter.pending_episode_rows["pending-context-1"]["processing_state"] == "READY"
    assert adapter.pending_episode_rows["pending-context-1"]["processing_attempts"] == 4
    assert len(stub_graphiti_client.add_episode_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_direct_call_still_processes_available_context_window_rows(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.pending_episode_rows["pending-context-2"] = {
        "uuid": "pending-context-2",
        "name": "episode-session-1-pending-context-2",
        "content": "queued event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "FAILED",
        "processing_attempts": 3,
        "processing_owner": None,
        "processing_lease_expires_at": None,
        "processing_error": "request (81920 tokens) exceeds the available context window of 32768 tokens",
        "resolved_episode_uuid": None,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    await service._process_pending_episode("pending-context-2")


    assert adapter.pending_episode_rows["pending-context-2"]["processing_state"] == "READY"
    assert adapter.pending_episode_rows["pending-context-2"]["processing_attempts"] == 4
    assert len(stub_graphiti_client.add_episode_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_rehydrates_compressed_entities(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    class FakeLifecycle:
        calls: list[dict[str, object]] = []

        async def rehydrate_node(
            self,
            node_uuid: str,
            new_context: str | None = None,
            *,
            source_node_uuid: str | None = None,
            source_episode_uuid: str | None = None,
        ) -> bool:
            self.calls.append(
                {
                    "node_uuid": node_uuid,
                    "new_context": new_context,
                    "source_node_uuid": source_node_uuid,
                    "source_episode_uuid": source_episode_uuid,
                }
            )
            return True

    adapter = stub_memory_graph_adapter
    adapter.node_freshness["entity-1"] = "COMPRESSED"
    lifecycle = FakeLifecycle()
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
        lifecycle_service=lifecycle,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-1",
        name="episode-session-1-pending-1",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert lifecycle.calls == [
        {
            "node_uuid": "entity-1",
            "new_context": "queued event",
            "source_node_uuid": "entity-1",
            "source_episode_uuid": "episode-1",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_failed_and_requests_retry(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingGraphitiClient:
        async def add_episode(self, **kwargs: object) -> object:
            raise RuntimeError("graphiti unavailable")

    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)

    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=FailingGraphitiClient(),
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-2",
        name="episode-session-1-pending-2",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert adapter.pending_episode_rows[episode_uuid]["processing_owner"] is None
    assert adapter.pending_episode_rows[episode_uuid]["processing_lease_expires_at"] is None
    assert "graphiti unavailable" in adapter.pending_episode_rows[episode_uuid]["processing_error"]
    assert failures[-1]["classification"] == "retryable"
    assert failures[-1]["error_type"] == "RuntimeError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_timeout_failed_and_requests_retry(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingGraphitiClient:
        scheduler_fallback_base_url = "http://127.0.0.1:8081/v1"

        async def add_episode(self, **kwargs: object) -> object:
            await asyncio.sleep(60.0)
            raise AssertionError("unreachable")

    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)

    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=HangingGraphitiClient(),
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._graphiti_add_episode_timeout_s = 0.01
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-timeout",
        name="episode-session-1-pending-timeout",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert adapter.pending_episode_rows[episode_uuid]["processing_owner"] is None
    assert adapter.pending_episode_rows[episode_uuid]["processing_lease_expires_at"] is None
    assert "graphiti add_episode timed out" in str(
        adapter.pending_episode_rows[episode_uuid]["processing_error"]
    )
    assert "remote completion status unknown" in str(
        adapter.pending_episode_rows[episode_uuid]["processing_error"]
    )
    assert failures[-1]["classification"] == "manual_review"
    assert failures[-1]["retryable"] is False
    assert failures[-1]["error_type"] == "TimeoutError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_service_shutdown_releases_owned_leases(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._worker_id = "worker-1"
    adapter.pending_episode_rows["owned-1"] = {
        "uuid": "owned-1",
        "name": "episode-session-1-owned-1",
        "content": "owned event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "ENRICHING",
        "processing_attempts": 1,
        "processing_owner": "worker-1",
        "processing_lease_expires_at": 999999,
        "resolved_episode_uuid": None,
    }

    released = await service.shutdown()

    assert released == 1
    assert adapter.pending_episode_rows["owned-1"]["processing_state"] == "PENDING"
    assert adapter.pending_episode_rows["owned-1"]["processing_error"] == "worker_shutdown_release"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_service_shutdown_marks_exhausted_owned_leases_failed(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._worker_id = "worker-1"
    adapter.pending_episode_rows["owned-exhausted-1"] = {
        "uuid": "owned-exhausted-1",
        "name": "episode-session-1-owned-exhausted-1",
        "content": "owned event",
        "scope": "SESSION",
        "type": "EPISODIC",
        "source": "unit-test",
        "source_confidence": 0.5,
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": "ENRICHING",
        "processing_attempts": 3,
        "processing_owner": "worker-1",
        "processing_lease_expires_at": 999999,
        "resolved_episode_uuid": None,
    }

    released = await service.shutdown()

    assert released == 1
    assert adapter.pending_episode_rows["owned-exhausted-1"]["processing_state"] == "FAILED"
    assert (
        adapter.pending_episode_rows["owned-exhausted-1"]["processing_error"]
        == "worker_shutdown_release_attempts_exhausted"
    )


@pytest.mark.unit
def test_record_episode_llm_usage_records_episode_task_event(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-llm-1",
        name="episode-session-1-pending-llm-1",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )
    task_events: list[dict[str, object]] = []

    def fake_record_episode_task_event(**kwargs: object) -> None:
        task_events.append(kwargs)

    monkeypatch.setattr(
        "menhir.services.ingest_worker.record_episode_task_event",
        fake_record_episode_task_event,
    )

    service._record_episode_llm_usage(
        episode_uuid,
        LLMUsageEvent(
            kind="chat",
            phase="started",
            model="test-model",
            endpoint="chat.completions.create",
        ),
    )

    assert adapter.pending_episode_rows[episode_uuid]["processing_llm_tasks_attempt"] == 1
    assert adapter.pending_episode_rows[episode_uuid]["processing_llm_tasks_total"] == 1
    assert adapter.pending_episode_rows[episode_uuid]["processing_llm_active_task"] == "memory: graphiti add_episode"
    assert adapter.pending_episode_rows[episode_uuid]["processing_llm_active_endpoint"] == "chat.completions.create"
    assert task_events == [
        {
            "episode_uuid": episode_uuid,
            "parent_task": "memory: graphiti add_episode",
            "child_task": "chat.completions.create",
            "phase": "started",
            "kind": "chat",
            "model": "test-model",
            "endpoint": "chat.completions.create",
            "scheduler_task": build_episode_scheduler_task(
                episode_uuid=episode_uuid,
                provider="graphiti",
                action="add-episode",
            ),
            "details": None,
        }
    ]


@pytest.mark.unit
def test_build_memory_services_applies_graphiti_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.core.bootstrap import build_memory_services
    from menhir.config import MemorySettings

    class FakeGraphitiClient:
        @classmethod
        def from_settings_with_capabilities(cls, settings: object, **kwargs: object) -> object:
            return SimpleNamespace(scheduler_fallback_base_url="http://127.0.0.1:8081/v1")

    class FakeNeo4jRepository:
        def __init__(self, **kwargs: object) -> None:
            return None

    class FakeMemoryGraphAdapter:
        def __init__(self, neo4j: object) -> None:
            self.neo4j = neo4j

    class FakeLifecycleService:
        def __init__(self, **kwargs: object) -> None:
            return None

    class FakeRecallService:
        def __init__(self, **kwargs: object) -> None:
            return None

    monkeypatch.setattr("menhir.core.bootstrap.GraphitiClient", FakeGraphitiClient)
    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", FakeNeo4jRepository)
    monkeypatch.setattr("menhir.core.bootstrap.MemoryGraphAdapter", FakeMemoryGraphAdapter)
    monkeypatch.setattr("menhir.core.bootstrap.LifecycleService", FakeLifecycleService)
    monkeypatch.setattr("menhir.core.bootstrap.RecallService", FakeRecallService)

    settings = MemorySettings(
        graphiti_add_episode_timeout_seconds=420.0,
        graphiti_episode_max_estimated_tokens=9000,
    )

    artifacts = build_memory_services(settings=settings)

    assert artifacts.ingest_service._graphiti_add_episode_timeout_s == 420.0
    assert artifacts.ingest_service._graphiti_episode_max_estimated_tokens == 9000


@pytest.mark.unit
def test_build_memory_services_wires_event_history_authority_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.core.bootstrap import build_memory_services
    from menhir.config import MemorySettings

    class FakeNeo4jRepository:
        def __init__(self, **kwargs: object) -> None:
            return None

    class FakeMemoryGraphAdapter:
        def __init__(self, neo4j: object) -> None:
            self.neo4j = neo4j

    class FakeGraphitiClient:
        @classmethod
        def from_settings_with_capabilities(cls, settings: object, **kwargs: object) -> object:
            return SimpleNamespace()

    class FakeLLMAdapter:
        @classmethod
        def from_settings(cls, settings: object) -> object:
            return SimpleNamespace()

    captured: dict[str, object] = {}

    class FakeRecallService:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", FakeNeo4jRepository)
    monkeypatch.setattr("menhir.core.bootstrap.MemoryGraphAdapter", FakeMemoryGraphAdapter)
    monkeypatch.setattr("menhir.core.bootstrap.GraphitiClient", FakeGraphitiClient)
    monkeypatch.setattr("menhir.core.bootstrap.LLMAdapter", FakeLLMAdapter)
    monkeypatch.setattr("menhir.core.bootstrap.RecallService", FakeRecallService)

    settings = MemorySettings(personal_memory_event_history_authority_enabled=True)
    build_memory_services(settings=settings)

    assert captured["event_history_authority_enabled"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_failed_when_graphiti_raises(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
) -> None:
    """A Graphiti error surfaces in the enrichment worker (queue-backed path), which
    marks the episode FAILED. (Direct ingest is now queue-delegating, so this error is
    no longer synchronous — it lives in _process_pending_episode.)"""

    class FailingGraphitiClient:
        async def add_episode(self, **kwargs: object) -> object:
            raise RuntimeError("graphiti unavailable")

    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=FailingGraphitiClient(),
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-graphiti-fail",
        name="episode-session-1-pending-graphiti-fail",
        content="first event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert adapter.stamp_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_rejects_oversized_with_raw_capture(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
) -> None:
    """An oversized episode is rejected in the worker BEFORE Graphiti is called, marked
    FAILED, but its prose is preserved as a raw-capture entity (Part 2 substrate
    guarantee: terminally-broken episodes stay recallable)."""

    class RecordingGraphitiClient:
        add_episode_calls: list[dict[str, object]] = []

        async def add_episode(self, **kwargs: object) -> object:
            self.add_episode_calls.append(kwargs)
            raise AssertionError("Graphiti should not be called for oversized episode")

    client = RecordingGraphitiClient()
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._graphiti_episode_max_estimated_tokens = 8
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-oversize",
        name="episode-session-1-pending-oversize",
        content="x" * 128,
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert client.add_episode_calls == []            # rejected before Graphiti
    assert len(adapter.raw_capture_calls) == 1       # prose preserved (Part 2)
    assert adapter.raw_capture_calls[0]["episode_uuid"] == episode_uuid


@pytest.mark.unit
@pytest.mark.asyncio
async def test_processing_heartbeat_loop_pings_scheduler_for_scheduler_managed_graphiti(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ping_urls: list[str] = []
    sleep_calls = 0

    class SchedulerManagedGraphitiClient:
        scheduler_fallback_base_url = "http://127.0.0.1:8081/v1"

    class FakeAsyncClient:
        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str) -> object:
            ping_urls.append(url)
            return SimpleNamespace(status_code=200)

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            stop_event.set()

    service = IngestService(
        graphiti_client=SchedulerManagedGraphitiClient(),
        graph_adapter=stub_memory_graph_adapter,
        llm=stub_llm_adapter,
    )
    service._processing_heartbeat_interval_s = 60.0
    stop_event = asyncio.Event()

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=2.0: FakeAsyncClient())
    monkeypatch.setattr("menhir.services.ingest_worker.asyncio.sleep", fake_sleep)

    await service._processing_heartbeat_loop("missing-episode", stop_event)

    assert ping_urls == ["http://127.0.0.1:8082/ping"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_failed_when_stamping_raises(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamping error during finalize marks the episode FAILED (worker path): Graphiti
    extraction runs, then the stamp choke point raises."""

    adapter = stub_memory_graph_adapter

    def failing_stamp(**kwargs: object) -> object:
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(adapter, "stamp_ingest_metadata", failing_stamp)
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-stamp-fail",
        name="episode-session-1-pending-stamp-fail",
        content="first event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert len(stub_graphiti_client.add_episode_calls) == 1   # graphiti ran; stamp failed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_ready_on_zero_extraction(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part 1: zero-extraction is a SUCCESSFUL empty determination, not a failure.

    An "ok thanks" episode with nothing memorable is marked READY (0 nodes/edges) with
    an empty-extraction receipt — it must NOT be marked FAILED or emit a failure event
    (that would inflate failure telemetry and mislabel the anchor)."""

    class ZeroExtractionGraphitiClient:
        add_episode_calls: list[dict[str, object]] = []

        async def add_episode(self, **kwargs: object) -> object:
            self.add_episode_calls.append(kwargs)
            return SimpleNamespace(
                episode=SimpleNamespace(uuid="episode-zero"),
                nodes=[],
                edges=[],
                episodic_edges=[],
            )

    client = ZeroExtractionGraphitiClient()
    adapter = stub_memory_graph_adapter
    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)
    service = IngestService(
        graphiti_client=client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-zero",
        name="episode-session-1-pending-zero",
        content="some content that yields no entities",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
    assert adapter.stamp_calls == []          # nothing memorable to stamp
    assert failures == []                     # zero-extraction is success -> no failure event


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_rejects_oversized_episode_before_graphiti(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingGraphitiClient:
        add_episode_calls: list[dict[str, object]] = []

        async def add_episode(self, **kwargs: object) -> object:
            self.add_episode_calls.append(kwargs)
            raise AssertionError("Graphiti should not be called for oversized pending episodes")

    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)
    client = RecordingGraphitiClient()
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._graphiti_episode_max_estimated_tokens = 8
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-too-large",
        name="episode-session-1-pending-too-large",
        content="x" * 128,
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert "episode_preflight_too_large" in str(adapter.pending_episode_rows[episode_uuid]["processing_error"])
    assert client.add_episode_calls == []
    assert failures[-1]["classification"] == "terminal"
    assert failures[-1]["retryable"] is False
    assert failures[-1]["failure_stage"] == "graphiti_preflight_rejected"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_zero_extraction_stops_after_max_attempts(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
) -> None:
    """Zero-extraction should fail once and defer any retry decision to the scheduler."""

    class ZeroExtractionGraphitiClient:
        add_episode_calls: list[dict[str, object]] = []

        async def add_episode(self, **kwargs: object) -> object:
            self.add_episode_calls.append(kwargs)
            return SimpleNamespace(
                episode=SimpleNamespace(uuid="episode-zero"),
                nodes=[],
                edges=[],
                episodic_edges=[],
            )

    client = ZeroExtractionGraphitiClient()
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    service._max_enrichment_attempts = 3
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-exhaust",
        name="episode-session-1-pending-exhaust",
        content="content that always yields nothing",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_reconciles_existing_graphiti_completion_before_retry(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-reconcile",
        name="episode-session-1-pending-reconcile",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )
    adapter.pending_episode_rows[episode_uuid]["processing_state"] = "FAILED"
    adapter.pending_episode_rows[episode_uuid]["processing_error"] = (
        "graphiti add_episode timed out after 0.1s; remote completion status unknown"
    )
    adapter.completed_episode_artifacts[(episode_uuid, "episode-session-1-pending-reconcile")] = {
        "resolved_episode_uuid": "episode-existing-1",
        "entity_uuids": ["entity-1"],
        "edge_uuids": ["edge-1"],
    }

    async def _should_not_run(**_: object) -> object:
        raise AssertionError("add_episode should not run when an existing completion is reconciled")

    stub_graphiti_client.add_episode = _should_not_run
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"
    assert adapter.pending_episode_rows[episode_uuid]["resolved_episode_uuid"] == "episode-existing-1"
    assert adapter.stamp_calls[-1]["node_uuids"] == ["episode-existing-1", "entity-1"]
    assert adapter.stamp_calls[-1]["edge_uuids"] == ["edge-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_invalid_graphiti_output_manual_review(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ParseFailingGraphitiClient:
        async def add_episode(self, **kwargs: object) -> object:
            raise json.JSONDecodeError("Expecting value", "bad", 0)

    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=ParseFailingGraphitiClient(),
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-invalid-output",
        name="episode-session-1-pending-invalid-output",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert failures[-1]["classification"] == "manual_review"
    assert failures[-1]["retryable"] is False
    assert failures[-1]["failure_stage"] == "graphiti_invalid_output"
    assert failures[-1]["error_type"] == "JSONDecodeError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_episode_marks_empty_graphiti_output_manual_review(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyGraphitiClient:
        async def add_episode(self, **kwargs: object) -> object:
            exc = ValueError("empty JSON response from Graphiti OpenAI-compatible client")
            exc.menhir_failure_details = {
                "graphiti_prompt_preview": "system: edge extraction | user: queued event",
                "graphiti_raw_response_preview": "",
                "graphiti_raw_response_length": 0,
            }
            raise exc

    failures: list[dict[str, object]] = []

    def fake_record_failure_event(**kwargs: object) -> None:
        failures.append(kwargs)

    monkeypatch.setattr("menhir.services.enrichment_steps.record_failure_event", fake_record_failure_event)
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=EmptyGraphitiClient(),
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-empty-output",
        name="episode-session-1-pending-empty-output",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    await service._process_pending_episode(episode_uuid)


    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "FAILED"
    assert failures[-1]["classification"] == "manual_review"
    assert failures[-1]["retryable"] is False
    assert failures[-1]["failure_stage"] == "graphiti_invalid_output"
    assert failures[-1]["details"]["graphiti_prompt_preview"] == "system: edge extraction | user: queued event"
    assert failures[-1]["details"]["graphiti_raw_response_preview"] == ""


@pytest.mark.unit
def test_pipeline_bootstrap_schema_delegates_to_adapter(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter

    result = adapter.bootstrap_phase_one()

    assert result.success
    assert adapter.calls == 1


@pytest.mark.unit
def test_ingest_service_flag_memory_delegates_to_adapter(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    result = service.flag_memory("entity-123")

    assert result is True
    assert adapter.flagged_nodes == ["entity-123"]


@pytest.mark.unit
def test_ingest_service_unflag_memory_delegates_to_adapter(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    # First flag, then unflag
    service.flag_memory("entity-123")
    assert adapter.flagged_nodes == ["entity-123"]

    result = service.unflag_memory("entity-123")

    assert result is True
    assert adapter.flagged_nodes == []


@pytest.mark.unit
def test_ingest_service_unflag_memory_is_idempotent(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    """Unflagging an already-unflagged node should not error and return True."""
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    result = service.unflag_memory("never-flagged-uuid")

    assert result is True  # idempotent


@pytest.mark.unit
def test_ingest_service_delete_memory_delegates_to_adapter(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    result = service.delete_memory("entity-456")

    assert result is True
    assert adapter.deleted_nodes == ["entity-456"]


@pytest.mark.unit
def test_delete_memory_cancels_live_episode_instead_of_hard_delete(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    episode_uuid = "episode-live-delete-1"
    adapter.pending_episode_rows[episode_uuid] = {
        "uuid": episode_uuid,
        "processing_state": ProcessingState.ENRICHING,
        "processing_stage": "graphiti_extracting",
        "processing_substage": "awaiting_graphiti_response",
        "processing_attempts": 1,
        "processing_owner": "worker-1",
        "processing_lease_expires_at": 900,
    }
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    result = service.delete_memory(episode_uuid)

    assert result is True
    row = adapter.pending_episode_rows[episode_uuid]
    assert row["processing_state"] == ProcessingState.FAILED
    assert row["processing_substage"] == "deleted_by_user"
    assert row["processing_error"] == "deleted_by_user"
    assert row["processing_owner"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_does_not_finalize_after_force_release(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    episode_uuid = "episode-live-1"
    adapter.pending_episode_rows[episode_uuid] = {
        "uuid": episode_uuid,
        "name": "episode-live-1",
        "content": "Alice likes tea.",
        "source": "claude-code",
        "session_id": "session-1",
        "user_id": "user-1",
        "processing_state": ProcessingState.PENDING,
        "processing_stage": "queued",
        "processing_substage": "queued",
        "processing_attempts": 0,
        "processing_owner": None,
        "processing_lease_expires_at": None,
    }

    gate = asyncio.Event()

    async def _blocked_add_episode(**_: object):
        await gate.wait()
        row = adapter.pending_episode_rows[episode_uuid]
        row["processing_state"] = ProcessingState.PENDING
        row["processing_stage"] = "queued"
        row["processing_substage"] = "lease_released"
        row["processing_error"] = "manual_lease_release"
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        return SimpleNamespace(
            episode=SimpleNamespace(uuid="resolved-episode-1"),
            nodes=[SimpleNamespace(uuid="entity-1")],
            edges=[],
            episodic_edges=[],
        )

    stub_graphiti_client.add_episode = _blocked_add_episode
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )

    task = asyncio.create_task(service._process_pending_episode(episode_uuid))
    await asyncio.sleep(0)
    gate.set()
    await task

    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == ProcessingState.PENDING
    assert adapter.pending_episode_rows[episode_uuid]["processing_error"] == "manual_lease_release"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_memory_runtime_initializes_graphiti_then_schema() -> None:
    built = BuildArtifacts(
        settings=MemorySettings.from_env(),
        capabilities=None,
        neo4j=object(),
        graphiti_client=SimpleNamespace(
            calls=[],
            build_indices_and_constraints=None,
        ),
        llm=object(),
        graph_adapter=SimpleNamespace(
            calls=[],
            phase_one_schema_ready=lambda: False,
            bootstrap_phase_one=None,
            sync_edge_counts=None,
        ),
        ingest_service=object(),
        recall_service=object(),
        scoring_service=object(),
        lifecycle_service=object(),
        context_builder=object(),
        candidate_service=object(),
    )
    built.graphiti_client.build_indices_and_constraints = lambda force=False: _noop_async_increment(
        built.graphiti_client, "graphiti"
    )
    built.graph_adapter.bootstrap_phase_one = lambda: _schema_result_increment(built.graph_adapter, "schema")
    built.graph_adapter.sync_edge_counts = lambda: _sync_edge_counts_increment(built.graph_adapter, 7)

    result = await prepare_memory_runtime(built)

    assert result["graphiti"] == "ok"
    assert result["schema"]["status"] == "ok"
    assert result["edge_count_sync"] == 7
    assert built.graphiti_client.calls == ["graphiti"]
    assert built.graph_adapter.calls == ["schema", "sync"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_memory_runtime_marks_partial_schema_failures() -> None:
    graphiti_stub = SimpleNamespace(
        calls=0,
        forces=[],
        build_indices_and_constraints=None,
    )
    graphiti_stub.build_indices_and_constraints = lambda force=False: _noop_async_force_increment(
        graphiti_stub, force
    )
    built = BuildArtifacts(
        settings=MemorySettings.from_env(),
        capabilities=None,
        neo4j=object(),
        graphiti_client=graphiti_stub,
        llm=object(),
        graph_adapter=SimpleNamespace(
            sync_calls=0,
            phase_one_schema_ready=lambda: False,
            bootstrap_phase_one=lambda: type(
                "StubSchemaResult",
                (),
                {"success": False, "queries_executed": 3, "failures": ["query failed"]},
            )(),
            sync_edge_counts=None,
        ),
        ingest_service=object(),
        recall_service=object(),
        scoring_service=object(),
        lifecycle_service=object(),
        context_builder=object(),
        candidate_service=object(),
    )

    built.graph_adapter.sync_edge_counts = lambda: _sync_call_increment(built.graph_adapter)

    result = await prepare_memory_runtime(built)

    assert graphiti_stub.calls == 1
    assert graphiti_stub.forces == [False]
    assert result["graphiti"] == "ok"
    assert result["schema"]["status"] == "partial"
    assert result["schema"]["queries_executed"] == 3
    assert result["schema"]["failures"] == ["query failed"]
    assert result["edge_count_sync"] == 1
    assert built.graph_adapter.sync_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_memory_runtime_forwards_force_graphiti_flag() -> None:
    graphiti_stub = SimpleNamespace(
        calls=0,
        forces=[],
        build_indices_and_constraints=None,
    )
    graphiti_stub.build_indices_and_constraints = lambda force=False: _noop_async_force_increment(
        graphiti_stub, force
    )
    built = BuildArtifacts(
        settings=MemorySettings.from_env(),
        capabilities=None,
        neo4j=object(),
        graphiti_client=graphiti_stub,
        llm=object(),
        graph_adapter=SimpleNamespace(
            sync_calls=0,
            phase_one_schema_ready=lambda: False,
            bootstrap_phase_one=lambda: type(
                "StubSchemaResult",
                (),
                {"success": True, "queries_executed": 1, "failures": []},
            )(),
            sync_edge_counts=None,
        ),
        ingest_service=object(),
        recall_service=object(),
        scoring_service=object(),
        lifecycle_service=object(),
        context_builder=object(),
        candidate_service=object(),
    )

    built.graph_adapter.sync_edge_counts = lambda: _sync_call_increment(built.graph_adapter)

    result = await prepare_memory_runtime(built, force_graphiti=True)

    assert graphiti_stub.calls == 1
    assert graphiti_stub.forces == [True]
    assert result["schema"]["status"] == "ok"
    assert result["edge_count_sync"] == 1
    assert built.graph_adapter.sync_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_memory_runtime_skips_schema_bootstrap_when_phase_one_ready() -> None:
    graphiti_stub = SimpleNamespace(
        calls=0,
        forces=[],
        build_indices_and_constraints=None,
    )
    graphiti_stub.build_indices_and_constraints = lambda force=False: _noop_async_force_increment(
        graphiti_stub, force
    )
    built = BuildArtifacts(
        settings=MemorySettings.from_env(),
        capabilities=None,
        neo4j=object(),
        graphiti_client=graphiti_stub,
        llm=object(),
        graph_adapter=SimpleNamespace(
            sync_calls=0,
            phase_one_schema_ready=lambda: True,
            bootstrap_phase_one=lambda: (_ for _ in ()).throw(AssertionError("should not run bootstrap")),
            sync_edge_counts=None,
        ),
        ingest_service=object(),
        recall_service=object(),
        scoring_service=object(),
        lifecycle_service=object(),
        context_builder=object(),
        candidate_service=object(),
    )

    built.graph_adapter.sync_edge_counts = lambda: _sync_call_increment(built.graph_adapter)

    result = await prepare_memory_runtime(built)

    assert graphiti_stub.calls == 1
    assert result["schema"] == {
        "status": "ok",
        "queries_executed": 0,
        "failures": [],
        "skipped": True,
    }
    assert result["edge_count_sync"] == 1
    assert built.graph_adapter.sync_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_runs_phase_one_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, object]] = []

    def fake_record_mcp_event(**kwargs: object) -> None:
        events.append(kwargs)

    class FakeIngestService:
        def __init__(self) -> None:
            self.recovery_calls = 0
            self.retry_calls: list[str] = []

        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            self.recovery_calls += 1
            return 2, 4

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            self.retry_calls.append(episode_uuid)
            return True

        def get_queue_depth(self) -> int:
            return 3

        def get_failed_enrichment_count(self) -> int:
            return 1

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 4, "enriching_count": 2, "failed_count": 5}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return [
                {
                    "uuid": "failed-1",
                    "processing_attempts": 1,
                    "processing_error": "Failed to load model",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                }
            ]

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_mcp_event",
        fake_record_mcp_event,
    )

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-phase1-{uuid4()}"
    run_dir.mkdir()
    try:
        scheduler = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            stale_recovery_interval_s=0.01,
            queue_health_interval_s=0.01,
            tick_interval_s=0.01,
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
        )
        await scheduler.start()
        await asyncio.sleep(0.05)
        snapshot = scheduler.status_snapshot()
        await scheduler.stop()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    assert snapshot["running"] is True
    assert snapshot["jobs"]["recover_stale_leases"]["runs"] >= 1
    assert snapshot["jobs"]["observe_queue_health"]["runs"] >= 1
    assert {event["operation"] for event in events} >= {
        "scheduler_recover_stale_leases",
        "scheduler_retry_failed_enrichments",
        "scheduler_queue_health",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_start_stop_is_idempotent() -> None:
    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return True

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-idempotent-{uuid4()}"
    run_dir.mkdir()
    try:
        scheduler = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            stale_recovery_interval_s=60.0,
            queue_health_interval_s=60.0,
            tick_interval_s=0.01,
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
        )
        await scheduler.start()
        first_task = scheduler._task
        await scheduler.start()
        assert scheduler._task is first_task
        await scheduler.stop()
        await scheduler.stop()
        assert scheduler.is_running() is False
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_stop_ignores_cancelled_task() -> None:
    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-cancelled-stop-{uuid4()}"
    run_dir.mkdir()
    try:
        scheduler = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            tick_interval_s=0.01,
        )
        scheduler._stop_event = asyncio.Event()
        scheduler._task = asyncio.create_task(asyncio.sleep(3600))
        scheduler._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler._task

        await scheduler.stop()

        assert scheduler._task is None
        assert scheduler.is_running() is False
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_allows_only_one_live_owner() -> None:
    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-lock-{uuid4()}"
    run_dir.mkdir()
    try:
        first = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            tick_interval_s=0.01,
            lease_duration_s=1.0,
        )
        second = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            tick_interval_s=0.01,
            lease_duration_s=1.0,
        )

        await first.start()
        await asyncio.sleep(0.03)
        await second.start()

        first_snapshot = first.status_snapshot()
        second_snapshot = second.status_snapshot()

        assert first_snapshot["running"] is True
        assert first_snapshot["lease"]["acquired"] is True
        assert second_snapshot["running"] is False
        assert second_snapshot["lease"]["acquired"] is False
        assert second_snapshot["lease"]["active_owner"]["owner_id"] == first_snapshot["lease"]["owner_id"]

        await first.stop()
        await second.start()
        await asyncio.sleep(0.03)

        second_after = second.status_snapshot()
        assert second_after["running"] is True
        assert second_after["lease"]["acquired"] is True

        await second.stop()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_force_takeover_transfers_active_owner() -> None:
    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-force-{uuid4()}"
    run_dir.mkdir()
    try:
        # This test exercises lease *transfer*, not the structure-watcher / experience-counter
        # jobs — disable them (they'd crash on this minimal FakeGraphAdapter and slow the loop),
        # and use a fast heartbeat so the displaced owner detects the takeover promptly. Mirrors
        # the sibling heartbeat/fence tests.
        first = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=1.0,
            lease_heartbeat_s=0.1,
        )
        second = MaintenanceScheduler(
            ingest_service=FakeIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=1.0,
            lease_heartbeat_s=0.1,
        )

        await first.start()
        await asyncio.sleep(0.03)
        await second.start()
        assert second.status_snapshot()["running"] is False

        forced = await second.force_takeover(reason="unit-test")
        # The displaced owner is fenced asynchronously (per-tick renew + heartbeat), not instantly.
        # Poll for the guarantee — it stops — rather than assuming a fixed latency.
        for _ in range(200):
            if first.status_snapshot()["running"] is False:
                break
            await asyncio.sleep(0.01)

        first_snapshot = first.status_snapshot()
        second_snapshot = second.status_snapshot()
        assert forced is True
        assert first_snapshot["running"] is False
        assert second_snapshot["running"] is True
        assert second_snapshot["lease"]["acquired"] is True
        assert second_snapshot["lease"]["active_owner"]["owner_id"] == second_snapshot["lease"]["owner_id"]
        assert second_snapshot["lease"]["last_forced_takeover"]["reason"] == "unit-test"

        await second.stop()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_heartbeat_keeps_lease_alive_during_long_job() -> None:
    """Bug AR-02: the lease used to be renewed only once per loop iteration, so a job that ran
    longer than the lease window let a second process acquire the "expired" lease and mutate
    shared state concurrently. The background heartbeat must keep the lease live across a long
    job so a second owner cannot acquire it mid-run."""

    class SlowIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            await asyncio.sleep(1.2)  # longer than lease_duration_s=0.6
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-heartbeat-{uuid4()}"
    run_dir.mkdir()
    try:
        first = MaintenanceScheduler(
            ingest_service=SlowIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            stale_recovery_interval_s=0.0,
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=0.6,
            lease_heartbeat_s=0.1,
        )
        second = MaintenanceScheduler(
            ingest_service=SlowIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=0.6,
            lease_heartbeat_s=0.1,
        )

        await first.start()
        # Past the 0.6s lease window but still inside the 1.2s recover job. Without the
        # heartbeat the lease would have expired and `second` could take it.
        #
        # Widened from the original 0.3s/0.1s/0.6s/0.4s windows (2026-07-12): those margins
        # were thin enough that a heartbeat tick delayed by system load during a long,
        # heavily loaded full-suite run could miss renewing the lease before the check,
        # flaking this real-wall-clock-timing test without any actual scheduler bug. Doubling
        # every window preserves the same ordering/intent while giving heartbeat ticks (still
        # every 0.1s, so ~6 renewals during the lease window instead of ~3) much more slack.
        await asyncio.sleep(0.8)

        acquired = await second.start()
        first_snapshot = first.status_snapshot()
        second_snapshot = second.status_snapshot()

        assert acquired is False
        assert first_snapshot["running"] is True
        assert first_snapshot["lease"]["active_owner"]["expired"] is False
        assert second_snapshot["running"] is False
        assert second_snapshot["lease"]["active_owner"]["owner_id"] == first_snapshot["lease"]["owner_id"]

        await first.stop()
        await second.stop()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_forced_takeover_fences_displaced_owner_mid_batch() -> None:
    """Bug FC-02/AR-04: a forced takeover overwrote the lease but the displaced owner kept
    running its entire in-flight job batch before noticing. The heartbeat must mark the lease
    lost and the batch must fence out the remaining jobs, so the displaced owner does not run
    further maintenance after being taken over."""

    class SlowFirstJobIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            await asyncio.sleep(0.4)  # takeover happens while this first job is in flight
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return []

    tmp_root = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_root / f"scheduler-fence-{uuid4()}"
    run_dir.mkdir()
    try:
        first = MaintenanceScheduler(
            ingest_service=SlowFirstJobIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=1.0,
            lease_heartbeat_s=0.1,
        )
        second = MaintenanceScheduler(
            ingest_service=SlowFirstJobIngestService(),
            graph_adapter=FakeGraphAdapter(),
            lease_store=SchedulerLeaseStore(db_path=run_dir / "scheduler-lock.db"),
            structure_watcher_enabled=False,
            experience_counter_enabled=False,
            tick_interval_s=0.01,
            lease_duration_s=1.0,
            lease_heartbeat_s=0.1,
        )

        await first.start()
        await asyncio.sleep(0.05)  # first is now inside recover_stale_leases (0.4s)
        forced = await second.force_takeover(reason="unit-test")
        # Wait past the in-flight job so the fence has a chance to skip the rest of the batch.
        await asyncio.sleep(0.6)

        first_snapshot = first.status_snapshot()
        assert forced is True
        assert first_snapshot["running"] is False
        assert first_snapshot["lease"]["blocked_reason"] == "lease_lost"
        # recover_stale_leases (the in-flight job) completed; the remaining batch jobs were
        # fenced out because the lease was lost mid-batch.
        assert first_snapshot["jobs"]["recover_stale_leases"]["runs"] == 1
        assert first_snapshot["jobs"]["retry_failed_enrichments"]["runs"] == 0
        assert first_snapshot["jobs"]["observe_queue_health"]["runs"] == 0
        assert second.status_snapshot()["running"] is True

        await second.stop()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.unit
def test_maintenance_scheduler_classifies_terminal_and_retryable_failures() -> None:
    assert classify_enrichment_failure("zero_extraction") == "terminal"
    assert classify_enrichment_failure(
        "episode_preflight_too_large estimated_tokens=24000 limit=12000 chars=96000"
    ) == "terminal"
    assert classify_enrichment_failure(
        "graphiti add_episode timed out after 0.1s; remote completion status unknown"
    ) == "manual_review"
    assert classify_enrichment_failure(
        "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
    ) == "manual_review"
    assert classify_enrichment_failure(
        "ValueError: empty JSON response from Graphiti OpenAI-compatible client"
    ) == "manual_review"
    assert classify_enrichment_failure(
        "ValidationError: extracted_entities -> 0 -> name field required"
    ) == "manual_review"
    assert classify_enrichment_failure(
        "Error code: 400 - {'error': 'Cannot truncate prompt with n_keep (8199) >= n_ctx (4096)'}"
    ) == "terminal"
    assert classify_enrichment_failure(
        "request (81920 tokens) exceeds the available context window of 32768 tokens"
    ) == "terminal"
    assert classify_enrichment_failure(
        "Error code: 400 - {'error': 'Failed to load model \"text-embedding-nomic-embed-text-v1.5\". Error: Operation canceled.'}"
    ) == "retryable"
    assert classify_enrichment_failure(
        "Neo.ClientError.Statement.EntityNotFound: Unable to load NODE 4."
    ) == "retryable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_requeues_only_retryable_failed_episodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requeued: list[str] = []
    events: list[dict[str, object]] = []
    failure_events: list[dict[str, object]] = []

    def fake_record_mcp_event(**kwargs: object) -> None:
        events.append(kwargs)

    def fake_record_failure_event(**kwargs: object) -> None:
        failure_events.append(kwargs)

    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            requeued.append(episode_uuid)
            return True

        def get_queue_depth(self) -> int:
            return len(requeued)

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

        def get_context_window_retry_attempts(self) -> int:
            return 6

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 3}

        def find_completed_episode_artifact(
            self,
            *,
            anchor_uuid: str,
            anchor_name: str,
        ) -> dict[str, object] | None:
            return None

        def stamp_ingest_metadata(
            self,
            *,
            node_uuids: list[str],
            edge_uuids: list[str],
            session_id: str,
            user_id: str,
            source: str,
            source_confidence: float,
            namespace: str = "default",
        ):
            raise AssertionError("stamp_ingest_metadata should not be called in this test")

        def mark_episode_ready(
            self,
            episode_uuid: str,
            *,
            worker_id: str | None = None,
            required_state: str | None = None,
            resolved_episode_uuid: str,
            nodes_touched: int,
            edges_touched: int,
        ) -> bool:
            raise AssertionError("mark_episode_ready should not be called in this test")

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return [
                {
                    "uuid": "retry-now",
                    "name": "episode-retry-now",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 1,
                    "processing_error": "graphiti unavailable",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                },
                {
                    "uuid": "terminal",
                    "name": "episode-terminal",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 1,
                    "processing_error": "zero_extraction",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                },
                {
                    "uuid": "manual-review",
                    "name": "episode-manual-review",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 1,
                    "processing_error": "graphiti add_episode timed out after 0.1s; remote completion status unknown",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                },
                {
                    "uuid": "backoff",
                    "name": "episode-backoff",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 2,
                    "processing_error": "Failed to load model",
                    "processing_completed_at": "2999-01-01T00:00:00+00:00",
                },
            ]

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_mcp_event",
        fake_record_mcp_event,
    )
    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_failure_event",
        fake_record_failure_event,
    )
    monkeypatch.setattr(
        "menhir.services.scheduler_tasks.record_failure_event",
        fake_record_failure_event,
    )

    scheduler = MaintenanceScheduler(
        ingest_service=FakeIngestService(),
        graph_adapter=FakeGraphAdapter(),
    )
    job = scheduler._jobs["retry_failed_enrichments"]
    await scheduler._run_retry_failed_enrichments(job)

    assert requeued == ["retry-now"]
    assert job.last_result == {
        "candidates": 4,
        "reconciled": 0,
        "requeued": 1,
        "terminal": 2,
        "waiting": 1,
        "exhausted": 0,
        "queue_depth": 1,
    }
    assert events[-1]["operation"] == "scheduler_retry_failed_enrichments"
    assert {event["failure_stage"] for event in failure_events} == {
        "retry_backoff_wait",
        "retry_classification",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_reconciles_silent_completed_failed_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamped_calls: list[dict[str, object]] = []
    ready_calls: list[dict[str, object]] = []

    class FakeStamp:
        nodes_touched = 2
        edges_touched = 1

    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            raise AssertionError("requeue_failed_episode should not be called for reconciled episodes")

        def get_queue_depth(self) -> int:
            return 0

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 1}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return [
                {
                    "uuid": "manual-review",
                    "name": "episode-session-1-manual-review",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 1,
                    "processing_error": "graphiti add_episode timed out after 0.1s; remote completion status unknown",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                }
            ]

        def find_completed_episode_artifact(
            self,
            *,
            anchor_uuid: str,
            anchor_name: str,
        ) -> dict[str, object] | None:
            return {
                "resolved_episode_uuid": "episode-existing-1",
                "entity_uuids": ["entity-1"],
                "edge_uuids": ["edge-1"],
            }

        def stamp_ingest_metadata(
            self,
            *,
            node_uuids: list[str],
            edge_uuids: list[str],
            session_id: str,
            user_id: str,
            source: str,
            source_confidence: float,
            namespace: str = "default",
        ):
            stamped_calls.append(
                {
                    "node_uuids": node_uuids,
                    "edge_uuids": edge_uuids,
                    "session_id": session_id,
                    "user_id": user_id,
                    "source": source,
                    "source_confidence": source_confidence,
                }
            )
            return FakeStamp()

        def mark_episode_ready(
            self,
            episode_uuid: str,
            *,
            worker_id: str | None = None,
            required_state: str | None = None,
            resolved_episode_uuid: str,
            nodes_touched: int,
            edges_touched: int,
        ) -> bool:
            ready_calls.append(
                {
                    "episode_uuid": episode_uuid,
                    "required_state": required_state,
                    "resolved_episode_uuid": resolved_episode_uuid,
                    "nodes_touched": nodes_touched,
                    "edges_touched": edges_touched,
                }
            )
            return True

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_mcp_event",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_failure_event",
        lambda **_: None,
    )

    scheduler = MaintenanceScheduler(
        ingest_service=FakeIngestService(),
        graph_adapter=FakeGraphAdapter(),
    )
    job = scheduler._jobs["retry_failed_enrichments"]
    await scheduler._run_retry_failed_enrichments(job)

    assert stamped_calls == [
        {
            "node_uuids": ["episode-existing-1", "entity-1"],
            "edge_uuids": ["edge-1"],
            "session_id": "session-1",
            "user_id": "user-1",
            "source": "unit-test",
            "source_confidence": 0.5,
        }
    ]
    assert ready_calls == [
        {
            "episode_uuid": "manual-review",
            "required_state": "FAILED",
            "resolved_episode_uuid": "episode-existing-1",
            "nodes_touched": 2,
            "edges_touched": 1,
        }
    ]
    assert job.last_result == {
        "candidates": 1,
        "reconciled": 1,
        "requeued": 0,
        "terminal": 0,
        "waiting": 0,
        "exhausted": 0,
        "queue_depth": 0,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_does_not_retry_context_window_mismatch_beyond_default_attempt_cap() -> None:
    requeued: list[str] = []

    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            requeued.append(episode_uuid)
            return True

        def get_queue_depth(self) -> int:
            return len(requeued)

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

        def get_context_window_retry_attempts(self) -> int:
            return 6

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 0, "enriching_count": 0, "failed_count": 1}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return [
                {
                    "uuid": "ctx-window-retry",
                    "processing_attempts": 7,
                    "processing_error": "Error code: 400 - {'error': 'Cannot truncate prompt with n_keep (8199) >= n_ctx (4096)'}",
                    "processing_completed_at": "2026-03-08T00:00:00+00:00",
                }
            ]

    scheduler = MaintenanceScheduler(
        ingest_service=FakeIngestService(),
        graph_adapter=FakeGraphAdapter(),
    )
    job = scheduler._jobs["retry_failed_enrichments"]
    await scheduler._run_retry_failed_enrichments(job)

    assert requeued == []
    assert job.last_result == {
        "candidates": 1,
        "reconciled": 0,
        "requeued": 0,
        "terminal": 0,
        "waiting": 0,
        "exhausted": 1,
        "queue_depth": 0,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_apply_decay_awaits_lifecycle_service(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    class FakeLifecycle:
        calls = 0

        async def apply_decay(self):
            self.calls += 1
            return {"status": "ok"}

    lifecycle = FakeLifecycle()

    result = await lifecycle.apply_decay()

    assert result == {"status": "ok"}
    assert lifecycle.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maintenance_scheduler_reconciliation_skips_when_row_is_no_longer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamped_calls: list[dict[str, object]] = []
    ready_calls: list[dict[str, object]] = []
    requeued: list[str] = []

    class FakeStamp:
        nodes_touched = 2
        edges_touched = 1

    class FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0

        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            requeued.append(episode_uuid)
            return True

        def get_queue_depth(self) -> int:
            return len(requeued)

        def get_failed_enrichment_count(self) -> int:
            return 0

        def get_max_enrichment_attempts(self) -> int:
            return 3

        def get_context_window_retry_attempts(self) -> int:
            return 6

    class FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict[str, object]:
            return {"pending_count": 1, "enriching_count": 0, "failed_count": 0}

        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
            return [
                {
                    "uuid": "manual-review",
                    "name": "episode-session-1-manual-review",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "source": "unit-test",
                    "processing_attempts": 1,
                    "processing_error": "graphiti unavailable",
                    "processing_completed_at": "2026-03-09T00:00:00+00:00",
                }
            ]

        def find_completed_episode_artifact(
            self,
            *,
            anchor_uuid: str,
            anchor_name: str,
        ) -> dict[str, object] | None:
            return {
                "resolved_episode_uuid": "episode-existing-1",
                "entity_uuids": ["entity-1"],
                "edge_uuids": ["edge-1"],
            }

        def stamp_ingest_metadata(
            self,
            *,
            node_uuids: list[str],
            edge_uuids: list[str],
            session_id: str,
            user_id: str,
            source: str,
            source_confidence: float,
            namespace: str = "default",
        ):
            stamped_calls.append(
                {
                    "node_uuids": node_uuids,
                    "edge_uuids": edge_uuids,
                    "session_id": session_id,
                    "user_id": user_id,
                    "source": source,
                    "source_confidence": source_confidence,
                }
            )
            return FakeStamp()

        def mark_episode_ready(
            self,
            episode_uuid: str,
            *,
            worker_id: str | None = None,
            required_state: str | None = None,
            resolved_episode_uuid: str,
            nodes_touched: int,
            edges_touched: int,
        ) -> bool:
            ready_calls.append(
                {
                    "episode_uuid": episode_uuid,
                    "required_state": required_state,
                    "resolved_episode_uuid": resolved_episode_uuid,
                    "nodes_touched": nodes_touched,
                    "edges_touched": edges_touched,
                }
            )
            return False

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_mcp_event",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.record_failure_event",
        lambda **_: None,
    )

    scheduler = MaintenanceScheduler(
        ingest_service=FakeIngestService(),
        graph_adapter=FakeGraphAdapter(),
    )
    job = scheduler._jobs["retry_failed_enrichments"]
    await scheduler._run_retry_failed_enrichments(job)

    assert stamped_calls == [
        {
            "node_uuids": ["episode-existing-1", "entity-1"],
            "edge_uuids": ["edge-1"],
            "session_id": "session-1",
            "user_id": "user-1",
            "source": "unit-test",
            "source_confidence": 0.5,
        }
    ]
    assert ready_calls == [
        {
            "episode_uuid": "manual-review",
            "required_state": "FAILED",
            "resolved_episode_uuid": "episode-existing-1",
            "nodes_touched": 2,
            "edges_touched": 1,
        }
    ]
    assert requeued == ["manual-review"]
    assert job.last_result == {
        "candidates": 1,
        "reconciled": 0,
        "requeued": 1,
        "terminal": 0,
        "waiting": 0,
        "exhausted": 0,
        "queue_depth": 1,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_resolve_conflicts_resolves_stale_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0
        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False
        def get_queue_depth(self) -> int:
            return 0
        def get_failed_enrichment_count(self) -> int:
            return 0
        def get_max_enrichment_attempts(self) -> int:
            return 3

    class _FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict:
            return {}
        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list:
            return []
        def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
            return None
        def stamp_ingest_metadata(self, **_: object) -> object:
            return None
        def mark_episode_ready(self, *_: object, **__: object) -> bool:
            return False

    class _FakeLifecycleService:
        def auto_resolve_stale_conflicts(self, *, max_age_days: int, limit: int) -> int:
            return 3

    monkeypatch.setattr("menhir.services.maintenance_scheduler.record_mcp_event", lambda **_: None)
    monkeypatch.setattr("menhir.services.maintenance_scheduler.record_failure_event", lambda **_: None)
    monkeypatch.setattr("menhir.services.scheduler_tasks.record_failure_event", lambda **_: None)

    scheduler = MaintenanceScheduler(
        ingest_service=_FakeIngestService(),
        graph_adapter=_FakeGraphAdapter(),
        lifecycle_service=_FakeLifecycleService(),
    )
    job = scheduler._jobs["auto_resolve_conflicts"]
    await scheduler._run_auto_resolve_conflicts(job)

    assert job.last_success is True
    assert job.last_result == {"groups_resolved": 3}
    assert job.runs == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_resolve_conflicts_skips_when_no_lifecycle_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeIngestService:
        async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
            return 0, 0
        async def requeue_failed_episode(self, episode_uuid: str) -> bool:
            return False
        def get_queue_depth(self) -> int:
            return 0
        def get_failed_enrichment_count(self) -> int:
            return 0
        def get_max_enrichment_attempts(self) -> int:
            return 3

    class _FakeGraphAdapter:
        def fetch_memory_overview(self) -> dict:
            return {}
        def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list:
            return []
        def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
            return None
        def stamp_ingest_metadata(self, **_: object) -> object:
            return None
        def mark_episode_ready(self, *_: object, **__: object) -> bool:
            return False

    scheduler = MaintenanceScheduler(
        ingest_service=_FakeIngestService(),
        graph_adapter=_FakeGraphAdapter(),
        lifecycle_service=None,
    )
    job = scheduler._jobs["auto_resolve_conflicts"]
    # dispatch: lifecycle_service is None so job should not run
    if scheduler.lifecycle_service is not None:
        await scheduler._run_auto_resolve_conflicts(job)

    assert "auto_resolve_conflicts" in scheduler._jobs
    assert job.runs == 0


@pytest.mark.unit
def test_lifecycle_service_auto_resolve_stale_conflicts_resolves_old_groups() -> None:
    from datetime import datetime, timedelta, timezone
    from menhir.services.lifecycle_service import LifecycleService

    resolved_groups: list[str] = []
    stale_created = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    fresh_created = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    class _FakeAdapter:
        def list_conflict_groups(self, *, status: str | None, limit: int) -> list[dict]:
            return [
                {"group_id": "stale-group", "created_at": stale_created},
                {"group_id": "fresh-group", "created_at": fresh_created},
            ]

        def resolve_conflict_group(self, group_id: str, action: str, **kwargs: object) -> dict:
            resolved_groups.append(group_id)
            return {"action": action, "group_id": group_id, "resolved": 2, "removed_uuids": []}

    svc = object.__new__(LifecycleService)
    svc.graph_adapter = _FakeAdapter()

    count = svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert count == 1
    assert resolved_groups == ["stale-group"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_worker_cannot_mutate_processing_metadata_after_force_release(
    stub_memory_graph_adapter: "StubMemoryGraphAdapter",
    stub_graphiti_client: "StubGraphitiClient",
    stub_llm_adapter: object,
) -> None:
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=adapter,
        llm=stub_llm_adapter,
    )
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="pending-metadata-race",
        name="episode-session-1-pending-metadata-race",
        content="queued event",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )
    claimed = adapter.claim_pending_episode(
        episode_uuid,
        max_attempts=service.get_max_enrichment_attempts(),
        context_retry_attempts=service.get_max_enrichment_attempts(),
        worker_id=service._worker_id,
        lease_seconds=60,
    )
    assert claimed is not None

    adapter.force_release_episode_lease(
        episode_uuid,
        max_attempts=service.get_max_enrichment_attempts(),
    )

    updated = adapter.update_episode_processing(
        episode_uuid,
        worker_id=service._worker_id,
        stage="finalizing",
        substage="marking_ready",
        progress=95.0,
    )
    touched = adapter.touch_episode_processing_heartbeat(
        episode_uuid,
        worker_id=service._worker_id,
    )
    row = adapter.fetch_episode_processing(episode_uuid)

    assert updated is False
    assert touched is False
    assert row is not None
    assert row["processing_state"] == ProcessingState.PENDING
    assert row["processing_stage"] == "queued"
    assert row["processing_substage"] == "lease_released"


async def _noop_async_increment(stub: object, marker: str) -> None:
    stub.calls.append(marker)


async def _noop_async_force_increment(stub: object, force: bool) -> None:
    stub.calls += 1
    stub.forces.append(force)


def _schema_result_increment(stub: object, marker: str) -> object:
    stub.calls.append(marker)
    return type(
        "StubSchemaResult",
        (),
        {"success": True, "queries_executed": 1, "failures": []},
    )()


def _sync_edge_counts_increment(stub: object, result: int) -> int:
    stub.calls.append("sync")
    return result


def _sync_call_increment(stub: object) -> int:
    stub.sync_calls += 1
    return 1


# ---------------------------------------------------------------------------
# SchedulerLeaseStore — dead-local-owner reclaim (restart self-healing)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_lease_reclaimed_from_dead_local_owner_before_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart hard-kills the old server (Windows TerminateProcess) so it never releases its
    lease, and on a fast restart the TTL has not expired. The successor on the same host must
    reclaim the lease immediately once the prior owner's PID is dead."""
    store = SchedulerLeaseStore(db_path=tmp_path / "lease.db")

    # Prior owner takes a long (non-expiring) lease, then "dies".
    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="old-owner",
        owner_pid=424242, lease_duration_s=3600.0,
    ) is True

    # Its PID is no longer alive on this host.
    monkeypatch.setattr(SchedulerLeaseStore, "_pid_alive", staticmethod(lambda pid: False))

    # Successor on the same host reclaims despite the lease being well within its TTL.
    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="new-owner",
        owner_pid=os.getpid(), lease_duration_s=3600.0,
    ) is True
    assert store.fetch(lease_name="maintenance_scheduler")["owner_id"] == "new-owner"


@pytest.mark.unit
def test_lease_not_reclaimed_from_live_local_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live owner is never displaced: a second acquirer is blocked while the holder's PID is
    alive and the lease is still within its TTL."""
    store = SchedulerLeaseStore(db_path=tmp_path / "lease.db")
    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="live-owner",
        owner_pid=424242, lease_duration_s=3600.0,
    ) is True

    monkeypatch.setattr(SchedulerLeaseStore, "_pid_alive", staticmethod(lambda pid: True))

    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="other-owner",
        owner_pid=os.getpid(), lease_duration_s=3600.0,
    ) is False
    assert store.fetch(lease_name="maintenance_scheduler")["owner_id"] == "live-owner"


@pytest.mark.unit
def test_lease_not_reclaimed_from_dead_owner_on_other_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID liveness is only meaningful on the local host. A dead PID belonging to a different host
    must NOT be reclaimed on liveness grounds — only expiry can free a remote lease."""
    store = SchedulerLeaseStore(db_path=tmp_path / "lease.db")
    # Seed a lease owned by a different hostname, still within TTL.
    monkeypatch.setattr(SchedulerLeaseStore, "_hostname", staticmethod(lambda: "other-host"))
    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="remote-owner",
        owner_pid=424242, lease_duration_s=3600.0,
    ) is True

    # Local successor: even though the remote PID is "dead" here, we cannot reclaim across hosts.
    monkeypatch.setattr(SchedulerLeaseStore, "_hostname", staticmethod(lambda: "this-host"))
    monkeypatch.setattr(SchedulerLeaseStore, "_pid_alive", staticmethod(lambda pid: False))
    assert store.try_acquire(
        lease_name="maintenance_scheduler", owner_id="local-owner",
        owner_pid=os.getpid(), lease_duration_s=3600.0,
    ) is False


@pytest.mark.unit
def test_pid_alive_true_for_current_process_false_for_bogus() -> None:
    """The liveness probe recognizes this running process and rejects an impossible PID."""
    assert SchedulerLeaseStore._pid_alive(os.getpid()) is True
    assert SchedulerLeaseStore._pid_alive(-1) is False


# ---------------------------------------------------------------------------
# Stage 1 shadow-mode context composition wiring
# (.agent/plans/menhir-context-composition-production-integration.md)
# ---------------------------------------------------------------------------

class _FakeGraphAdapterForShadow:
    def update_episode_processing(self, *args, **kwargs):
        pass

    def fetch_candidate_fact_edges(self, uuids):
        return []


class _FakeGraphitiClientForShadow:
    """Minimal fake satisfying run_graphiti_extraction's real add_episode call plus
    the shadow snapshot's driver/search_scored lookups (both no-op safe)."""

    client = type("C", (), {"clients": type("CL", (), {"driver": None})()})()
    search_scored = None

    async def add_episode(self, **kwargs):
        class _Episode:
            uuid = "resolved-ep-1"

        class _Result:
            episode = _Episode()
            nodes = []
            edges = []
            episodic_edges = []

        return _Result()


def _build_shadow_test_ctx(
    *,
    shadow_on: bool,
    register=None,
    llm=None,
    episode_uuid: str = "shadow-ep-1",
    namespace: str = "default",
    ingest_gate=None,
):
    from menhir.services.enrichment_steps import EnrichmentContext
    from menhir.services.ingest_gate import IngestGate

    return EnrichmentContext(
        episode_uuid=episode_uuid,
        claimed={"content": "Rachel moved to Chicago", "namespace": namespace},
        started=0.0,
        processing_attempts=1,
        worker_id="w-1",
        graph_adapter=_FakeGraphAdapterForShadow(),
        graphiti_client=_FakeGraphitiClientForShadow(),
        lifecycle_service=None,
        llm=llm,
        ingest_gate=ingest_gate or IngestGate(1),
        processing_steps_total=5,
        settings_record_revisions=False,
        ready_warning_ms=5000,
        graphiti_add_episode_timeout_s=5.0,
        graphiti_episode_max_estimated_tokens=8000,
        get_queue_depth=lambda: 0,
        shadow_context_composition=shadow_on,
        shadow_composition_timeout_s=5.0,
        register_background_task=register,
    )


@pytest.mark.asyncio
async def test_run_graphiti_extraction_flag_off_never_touches_shadow_module(monkeypatch):
    """Flag off => byte-for-byte the old behavior: snapshot_candidate_facts is never
    called (a call would raise via the monkeypatch below), graphiti_result unchanged."""
    from menhir.services import enrichment_steps as es

    async def _boom(*args, **kwargs):
        raise AssertionError("snapshot_candidate_facts must not be called when the flag is off")

    monkeypatch.setattr(es, "snapshot_candidate_facts", _boom)

    ctx = _build_shadow_test_ctx(shadow_on=False)
    result = await es.run_graphiti_extraction(ctx, finalize_under_gate=False)
    assert result.episode.uuid == "resolved-ep-1"


@pytest.mark.asyncio
async def test_run_graphiti_extraction_flag_on_returns_before_shadow_task_completes():
    """Flag on: the gate is released and graphiti_result is returned to the caller
    BEFORE the detached shadow task finishes -- shadow latency must never leak onto
    the real completion path."""
    from menhir.services import enrichment_steps as es

    shadow_started = asyncio.Event()
    shadow_may_finish = asyncio.Event()
    shadow_finished = asyncio.Event()

    class _SlowLLM:
        async def classify_shadow_context(self, episode_body, candidates):
            shadow_started.set()
            await shadow_may_finish.wait()
            return None  # metadata_generation_failed is fine -- we only care about timing

        async def break_shadow_tie(self, episode_body, tied_candidates):
            return None

    registered = []
    ctx = _build_shadow_test_ctx(
        shadow_on=True, llm=_SlowLLM(), register=registered.append,
    )

    result = await es.run_graphiti_extraction(ctx, finalize_under_gate=False)
    assert result.episode.uuid == "resolved-ep-1"  # real path completed
    assert len(registered) == 1  # background task was dispatched + tracked

    task = registered[0]
    task.add_done_callback(lambda _t: shadow_finished.set())
    assert not shadow_finished.is_set()  # real return happened before shadow work finished

    shadow_may_finish.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert shadow_finished.is_set()


@pytest.mark.asyncio
async def test_run_graphiti_extraction_shadow_exception_never_propagates():
    """A bug inside shadow processing must never surface as an episode failure."""
    from menhir.services import enrichment_steps as es

    class _BrokenLLM:
        async def classify_shadow_context(self, episode_body, candidates):
            raise RuntimeError("boom")

        async def break_shadow_tie(self, episode_body, tied_candidates):
            raise RuntimeError("boom")

    registered = []
    ctx = _build_shadow_test_ctx(
        shadow_on=True, llm=_BrokenLLM(), register=registered.append,
    )

    result = await es.run_graphiti_extraction(ctx, finalize_under_gate=False)
    assert result.episode.uuid == "resolved-ep-1"

    # Draining the background task must not raise either -- _run_shadow_composition_and_log
    # catches everything internally (compose_shadow_prediction already never raises, but this
    # confirms the outer wrapper's own safety net too).
    await asyncio.wait_for(registered[0], timeout=5.0)
    assert SchedulerLeaseStore._pid_alive(0) is False


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_namespace", "may_overlap"),
    [("shared", False), ("other", True)],
    ids=["same-namespace-serial", "distinct-namespaces-parallel"],
)
async def test_run_graphiti_finalization_respects_namespace_boundary(
    monkeypatch: pytest.MonkeyPatch,
    second_namespace: str,
    may_overlap: bool,
) -> None:
    """Finalization stays serialized per namespace without blocking unrelated namespaces."""
    from menhir.services import enrichment_steps as es
    from menhir.services.ingest_gate import IngestGate

    first_finalize_started = asyncio.Event()
    release_first_finalize = asyncio.Event()
    second_extraction_started = asyncio.Event()

    async def fake_emit_scheduler_task_event(**kwargs):
        return None

    async def fake_add_episode_with_timeout(*args, **kwargs):
        episode_uuid = kwargs["episode_uuid"]
        if episode_uuid == "episode-b":
            second_extraction_started.set()
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=episode_uuid),
            nodes=[],
            edges=[],
            episodic_edges=[],
        )

    async def fake_stamp_and_finalize(ctx, graphiti_result):
        if ctx.episode_uuid == "episode-a":
            first_finalize_started.set()
            await release_first_finalize.wait()

    monkeypatch.setattr(es, "emit_scheduler_task_event", fake_emit_scheduler_task_event)
    monkeypatch.setattr(es, "add_episode_with_timeout", fake_add_episode_with_timeout)
    monkeypatch.setattr(es, "stamp_and_finalize", fake_stamp_and_finalize)
    monkeypatch.setattr(es, "record_lifecycle_event", lambda **kwargs: None)

    gate = IngestGate(2)
    first = asyncio.create_task(
        es.run_graphiti_extraction(
            _build_shadow_test_ctx(
                shadow_on=False,
                episode_uuid="episode-a",
                namespace="shared",
                ingest_gate=gate,
            ),
            finalize_under_gate=True,
        )
    )
    await asyncio.wait_for(first_finalize_started.wait(), timeout=1.0)
    second = asyncio.create_task(
        es.run_graphiti_extraction(
            _build_shadow_test_ctx(
                shadow_on=False,
                episode_uuid="episode-b",
                namespace=second_namespace,
                ingest_gate=gate,
            ),
            finalize_under_gate=True,
        )
    )
    try:
        if may_overlap:
            await asyncio.wait_for(second_extraction_started.wait(), timeout=1.0)
        else:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_extraction_started.wait(), timeout=0.05)
    finally:
        release_first_finalize.set()
    await asyncio.gather(first, second)
    assert second_extraction_started.is_set()
