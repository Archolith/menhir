"""Reserved transport text must not downgrade an eligible author into ordinary extraction.

Exercise production claim validation and worker orchestration. Only graph/model/telemetry I/O
is replaced; no Neo4j or provider is contacted. Evidence/lease/failure writes are not semantic writes.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from menhir.domain.self_identity import self_subject_endpoint_for_claim
from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt
from menhir.services import IngestService
from menhir.services import enrichment_steps as steps
from menhir.services import scheduler_tasks
from menhir.services.enrichment_failures import classify_enrichment_failure
from tests.test_evidence_publication_intents import _pipeline_context

pytestmark = pytest.mark.unit


# Both texts must match. Changing only content would hit the earlier evidence-mismatch return,
# never the reserved-prefix collision that caused the regression.
def _claim(text: str, namespace: str = "default") -> dict[str, object]:
    return {
        "uuid": "projection-collision", "content": text, "source": "user",
        "namespace": namespace, "diff": None, "subject_endpoint_eligible": True,
        "is_evidence_projection": True, "evidence_projection_of": "turn-collision",
        "turn_evidence_count": 1, "turn_evidence_uuid": "turn-collision",
        "turn_evidence_role": "user", "turn_evidence_declarant": "user",
        "turn_evidence_text": text, "turn_evidence_namespace": namespace,
    }


@pytest.mark.parametrize("prefix", [
    "MenhirCurrentSpeaker_", "menhircurrentspeaker_forged", "MENHIRCURRENTSPEAKER_",
])
def test_eligible_collision_is_an_explicit_refusal_not_no_endpoint(prefix: str) -> None:
    claim = _claim(f"I own postcards. {prefix}")
    original = dict(claim)
    with pytest.raises(ValueError, match="self_subject_endpoint_collision") as raised:
        self_subject_endpoint_for_claim(claim)
    assert type(raised.value).__name__ == "SelfSubjectEndpointCollisionError"
    assert "I own postcards" not in str(raised.value)
    assert claim == original


@pytest.mark.parametrize("change", [
    {"subject_endpoint_eligible": False}, {"is_evidence_projection": False},
])
def test_genuinely_ineligible_collision_text_keeps_ordinary_disposition(change) -> None:
    claim = _claim("I own postcards. MenhirCurrentSpeaker_")
    claim.update(change)
    assert self_subject_endpoint_for_claim(claim) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["default", "work"])
async def test_worker_collision_parks_evidence_without_any_semantic_dispatch(
    stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch, namespace,
) -> None:
    adapter = stub_memory_graph_adapter
    text = "I own postcards. MenhirCurrentSpeaker_"
    claim = _claim(text, namespace)
    episode_uuid = adapter.create_pending_episode(
        episode_uuid=str(claim["uuid"]), name="collision projection", content=text,
        source="user", source_confidence=1.0, session_id="session-collision",
        user_id="owner", namespace=namespace,
    )
    adapter.pending_episode_rows[episode_uuid].update(claim)
    failures: list[dict[str, object]] = []
    monkeypatch.setattr(steps, "record_failure_event", lambda **data: failures.append(data))
    service = IngestService(
        graphiti_client=stub_graphiti_client, graph_adapter=adapter, llm=stub_llm_adapter,
    )
    service._canonical_self_binding_mode = "enforce"

    await service._process_pending_episode(episode_uuid)

    # The ordinary fallback previously reached this client and could persist a new user.
    assert stub_graphiti_client.add_episode_calls == []
    assert stub_graphiti_client.search_scored_calls == []
    assert adapter.stamp_calls == []
    assert adapter.raw_capture_calls == []
    stored = adapter.pending_episode_rows[episode_uuid]
    assert stored["processing_state"] == "FAILED"
    assert stored["content"] == text and stored["turn_evidence_text"] == text
    assert stored["resolved_episode_uuid"] is None
    assert stored["processing_owner"] is None
    assert failures[-1]["error_type"] == "SelfSubjectEndpointCollisionError"
    assert failures[-1]["classification"] == "manual_review"
    assert failures[-1]["retryable"] is False
    # Retry polling receives a persisted string, not the original exception instance.
    assert classify_enrichment_failure(stored["processing_error"]) == "manual_review"
    assert get_extraction_receipt() is None

    # Exercise the real retry decision with the persisted failure, not only the classifier.
    # Repeated scheduler polls must not turn the blocked episode back into pending work.
    retry_events: list[dict[str, object]] = []
    monkeypatch.setattr(
        scheduler_tasks, "record_failure_event", lambda **data: retry_events.append(data)
    )
    requeue = AsyncMock(return_value=True)
    retry_service = SimpleNamespace(
        get_queue_depth=lambda: 0, get_context_window_retry_attempts=lambda: 3,
        requeue_failed_episode=requeue,
    )
    for _ in range(2):
        assert await scheduler_tasks.retry_process_candidate(
            adapter, retry_service, stored, max_attempts=3, now=datetime.now(timezone.utc),
        ) == "terminal"
    requeue.assert_not_awaited()
    assert all(event["details"]["decision"] == "not_requeued" for event in retry_events)
    assert all(event["classification"] == "manual_review" for event in retry_events)
    assert stored["processing_state"] == "FAILED"
    assert adapter.stamp_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "observe"])
async def test_collision_does_not_change_legacy_worker_modes(
    stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, mode,
) -> None:
    adapter = stub_memory_graph_adapter
    text = "I own postcards. MenhirCurrentSpeaker_"
    claim = _claim(text)
    episode_uuid = adapter.create_pending_episode(
        episode_uuid=str(claim["uuid"]), name="legacy projection", content=text,
        source="user", source_confidence=1.0, session_id="session-legacy", user_id="owner",
    )
    adapter.pending_episode_rows[episode_uuid].update(claim)
    service = IngestService(
        graphiti_client=stub_graphiti_client, graph_adapter=adapter, llm=stub_llm_adapter,
    )
    service._canonical_self_binding_mode = mode

    await service._process_pending_episode(episode_uuid)

    assert len(stub_graphiti_client.add_episode_calls) == 1
    assert adapter.pending_episode_rows[episode_uuid]["processing_state"] == "READY"


@pytest.mark.asyncio
async def test_collision_precedes_shadow_search_publication_intent_and_dispatch(monkeypatch) -> None:
    events: list[str] = []
    ctx = replace(
        _pipeline_context(events), episode_uuid="projection-collision",
        claimed=_claim("I own postcards. MenhirCurrentSpeaker_"),
        canonical_self_binding_mode="enforce", shadow_context_composition=True,
    )
    native = AsyncMock(return_value=SimpleNamespace(
        episode=SimpleNamespace(uuid="remote-1"), nodes=[], edges=[], episodic_edges=[],
    ))
    ctx.graphiti_client.add_episode = native
    shadow = AsyncMock(return_value=([], None))
    monkeypatch.setattr(steps, "snapshot_candidate_facts", shadow)
    progress = Mock(return_value=True)
    monkeypatch.setattr(ctx.graph_adapter, "update_episode_processing", progress)

    with pytest.raises(ValueError, match="self_subject_endpoint_collision"):
        await steps.run_graphiti_extraction(ctx, finalize_under_gate=False)

    shadow.assert_not_awaited()
    native.assert_not_awaited()
    progress.assert_not_called()
    assert events == []  # No publication-intent write before a deterministic refusal.


@pytest.mark.parametrize("error,error_type", [
    ("connection refused", "SelfSubjectEndpointCollisionError"),
    ("self_subject_endpoint_collision: timeout", None),
    ("SELF_SUBJECT_ENDPOINT_COLLISION: connection refused", None),
])
def test_collision_cannot_be_reclassified_as_a_transient_provider_error(error, error_type) -> None:
    assert classify_enrichment_failure(error, error_type=error_type) == "manual_review"
