"""Focused coverage for the activation-gated Graphiti publication intent protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.evidence_publication_intents import (
    FINALIZED,
    EvidencePublicationIntentRepository,
    GraphitiArtifactManifest,
    PublicationActivationBlocked,
    PublicationIntent,
    PublicationTransition,
    TombstoneProbe,
    publication_intent_key,
    publication_operation_id,
)
from menhir.services.enrichment_steps import EnrichmentContext
from menhir.services.ingest_gate import IngestGate


class _CaptureNeo4j:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}, kwargs))
        return self.responses.pop(0) if self.responses else []


class _OpaqueDigests:
    def active_key_ids(self) -> tuple[str, ...]:
        return ("active-v2", "retained-v1")

    def probes_for_publication(self, **_kwargs: Any) -> tuple[TombstoneProbe, ...]:
        return (
            TombstoneProbe(digest="a" * 64, key_id="active-v2"),
            TombstoneProbe(digest="b" * 64, key_id="retained-v1"),
        )


class _CompleteManifest:
    def created_artifacts(
        self,
        *,
        intent: PublicationIntent,
        remote_episode_uuid: str | None,
    ) -> GraphitiArtifactManifest:
        assert intent.episode_uuid == "queued-1"
        assert remote_episode_uuid == "remote-1"
        return GraphitiArtifactManifest(
            node_uuids=("remote-1", "entity-1", "entity-2"),
            edge_uuids=("edge-1", "mentions-1"),
            complete=True,
            quarantine_safe=True,
        )


def _intent(*, dispatch_allowed: bool = True) -> PublicationIntent:
    return PublicationIntent(
        intent_key=publication_intent_key("queued-1"),
        operation_id=publication_operation_id("queued-1"),
        episode_uuid="queued-1",
        namespace_key="tenant-a",
        group_id="tenant-a",
        expected_name="episode-name",
        source_description="test",
        reference_time="2026-08-28T12:00:00+00:00",
        generation=7,
        status="PENDING",
        dispatch_allowed=dispatch_allowed,
    )


@pytest.mark.unit
def test_activation_is_blocked_without_hmac_and_created_artifact_services() -> None:
    neo4j = _CaptureNeo4j()
    repo = EvidencePublicationIntentRepository(neo4j)  # type: ignore[arg-type]

    status = repo.activation_status()

    assert status.enabled is False
    assert "HMAC key ring" in " ".join(status.blockers)
    assert "artifact manifest" in " ".join(status.blockers)
    with pytest.raises(PublicationActivationBlocked, match="activation blocked"):
        repo.begin(
            episode_uuid="queued-1",
            namespace="tenant-a",
            expected_name="episode-name",
            source_description="test",
            reference_time=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        )
    assert neo4j.calls == []


@pytest.mark.unit
def test_begin_persists_stable_ids_under_fence_before_dispatch() -> None:
    intent = _intent()
    neo4j = _CaptureNeo4j(
        responses=[
            [
                {
                    **intent.__dict__,
                    "dispatch_allowed": True,
                }
            ]
        ]
    )
    repo = EvidencePublicationIntentRepository(
        neo4j,  # type: ignore[arg-type]
        tombstone_digests=_OpaqueDigests(),
        artifact_manifests=_CompleteManifest(),
    )

    created = repo.begin(
        episode_uuid="queued-1",
        namespace="tenant-a",
        expected_name="episode-name",
        source_description="test",
        reference_time=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
    )

    query, params, kwargs = neo4j.calls[0]
    assert query.index("MERGE (f:EvidenceNamespaceFence") < query.index(
        "MERGE (i:EvidencePublicationIntent"
    )
    assert query.index("SET f.lock_nonce") < query.index(
        "MERGE (i:EvidencePublicationIntent"
    )
    assert params["intent_key"] == publication_intent_key("queued-1")
    assert params["operation_id"] == publication_operation_id("queued-1")
    assert "i.generation = f.generation" in query
    assert created.generation == 7
    assert kwargs["safe_to_reexecute"] is True
    assert created.dispatch_allowed is True


@pytest.mark.unit
def test_finalize_locks_fence_and_uses_only_opaque_tombstone_probes() -> None:
    neo4j = _CaptureNeo4j(
        responses=[
            [
                {
                    "intent_key": publication_intent_key("queued-1"),
                    "status": FINALIZED,
                    "resolved_episode_uuid": "remote-1",
                    "candidate_count": 1,
                    "tombstone_count": 0,
                    "reason": None,
                }
            ]
        ]
    )
    repo = EvidencePublicationIntentRepository(
        neo4j,  # type: ignore[arg-type]
        tombstone_digests=_OpaqueDigests(),
        artifact_manifests=_CompleteManifest(),
    )

    result = repo.finalize_remote_outcome(_intent(), remote_episode_uuid="remote-1")

    query, params, kwargs = neo4j.calls[0]
    fence_lock = query.index("SET f.lock_nonce")
    assert fence_lock < query.index("MATCH (i:EvidencePublicationIntent")
    assert fence_lock < query.index("OPTIONAL MATCH (e:Episodic)")
    tombstone_clause = query[query.index("OPTIONAL MATCH (t:EvidenceTombstone)") :]
    assert "t.digest = probe.digest" in tombstone_clause
    assert "t.key_id = probe.key_id" in tombstone_clause
    assert "t.tombstone_key" not in tombstone_clause
    assert "t.evidence_uuid" not in tombstone_clause
    assert params["tombstone_probes"] == [
        {"digest": "a" * 64, "key_id": "active-v2"},
        {"digest": "b" * 64, "key_id": "retained-v1"},
    ]
    serialized_probes = repr(params["tombstone_probes"])
    for raw_identifier in ("queued-1", "remote-1", publication_operation_id("queued-1")):
        assert raw_identifier not in serialized_probes
    assert "artifact_node.evidence_finalized = false" in query
    assert "artifact_edge.evidence_finalized = false" in query
    assert "artifact_node.evidence_generation = $generation" in query
    assert "artifact_edge.evidence_generation = $generation" in query
    assert "AS already_finalized" in query
    assert "WHEN already_finalized THEN 'FINALIZED'" in query
    assert "i.completed_at = coalesce(i.completed_at" in query
    assert params["artifact_node_uuids"] == ["remote-1", "entity-1", "entity-2"]
    assert params["artifact_edge_uuids"] == ["edge-1", "mentions-1"]
    assert kwargs["safe_to_reexecute"] is True
    assert result.finalized is True


@pytest.mark.unit
def test_incomplete_created_artifact_manifest_blocks_before_finalize_write() -> None:
    class _IncompleteManifest:
        def created_artifacts(self, **_kwargs: Any) -> GraphitiArtifactManifest:
            return GraphitiArtifactManifest(
                node_uuids=("remote-1",),
                edge_uuids=(),
                complete=False,
                quarantine_safe=False,
            )

    neo4j = _CaptureNeo4j()
    repo = EvidencePublicationIntentRepository(
        neo4j,  # type: ignore[arg-type]
        tombstone_digests=_OpaqueDigests(),
        artifact_manifests=_IncompleteManifest(),
    )

    with pytest.raises(PublicationActivationBlocked, match="created-only quarantine"):
        repo.finalize_remote_outcome(_intent(), remote_episode_uuid="remote-1")
    assert neo4j.calls == []


@pytest.mark.unit
def test_claim_lease_preserves_and_returns_captured_publication_generation() -> None:
    claimed = _intent().__dict__ | {
        "status": "CLAIMED",
        "lease_owner": "reconciler-1",
        "lease_token": "lease-token",
        "lease_generation": 3,
    }
    neo4j = _CaptureNeo4j(responses=[[claimed]])
    repo = EvidencePublicationIntentRepository(
        neo4j,  # type: ignore[arg-type]
        tombstone_digests=_OpaqueDigests(),
        artifact_manifests=_CompleteManifest(),
    )

    intents = repo.claim_pending(owner_id="reconciler-1", limit=1, lease_seconds=30)

    query, _params, kwargs = neo4j.calls[0]
    assert "SET i.lease_generation" in query
    assert "i.generation AS generation" in query
    assert "SET i.generation" not in query
    assert intents[0].generation == 7
    assert intents[0].lease_generation == 3
    assert kwargs["safe_to_reexecute"] is True


@dataclass
class _PipelineAdapter:
    events: list[str]

    def update_episode_processing(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


class _PipelinePublicationRepo:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.intent = _intent()

    def begin(self, **_kwargs: Any) -> PublicationIntent:
        self.events.append("begin")
        return self.intent

    def finalize_remote_outcome(
        self,
        _intent_value: PublicationIntent,
        *,
        remote_episode_uuid: str | None,
    ) -> PublicationTransition:
        assert remote_episode_uuid == "remote-1"
        self.events.append("finalize")
        return PublicationTransition(
            intent_key=self.intent.intent_key,
            status=FINALIZED,
            resolved_episode_uuid="remote-1",
            candidate_count=1,
            tombstone_count=0,
            reason=None,
        )


def _pipeline_context(events: list[str]) -> EnrichmentContext:
    return EnrichmentContext(
        episode_uuid="queued-1",
        claimed={
            "name": "episode-name",
            "content": "remember this",
            "source": "test",
            "namespace": "tenant-a",
            "reference_time": "2026-08-28T12:00:00+00:00",
        },
        started=0.0,
        processing_attempts=1,
        worker_id="worker-1",
        graph_adapter=_PipelineAdapter(events),  # type: ignore[arg-type]
        graphiti_client=SimpleNamespace(),
        lifecycle_service=None,
        llm=None,
        ingest_gate=IngestGate(1),
        processing_steps_total=5,
        settings_record_revisions=False,
        ready_warning_ms=5000,
        graphiti_add_episode_timeout_s=5.0,
        graphiti_episode_max_estimated_tokens=8000,
        get_queue_depth=lambda: 0,
        evidence_publication_intents=_PipelinePublicationRepo(events),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_begins_before_dispatch_and_finalizes_after_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.services import enrichment_steps as steps

    events: list[str] = []

    async def fake_add_episode(*_args: Any, **_kwargs: Any) -> Any:
        events.append("dispatch")
        return SimpleNamespace(episode=SimpleNamespace(uuid="remote-1"))

    monkeypatch.setattr(steps, "add_episode_with_timeout", fake_add_episode)
    monkeypatch.setattr(steps, "record_lifecycle_event", lambda **_kwargs: None)
    monkeypatch.setattr(steps, "emit_scheduler_task_event", _noop_async)

    result = await steps.run_graphiti_extraction(
        _pipeline_context(events),
        finalize_under_gate=False,
    )

    assert result.episode.uuid == "remote-1"
    assert events == ["begin", "dispatch", "finalize"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_timeout_leaves_created_intent_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.services import enrichment_steps as steps

    events: list[str] = []

    async def timeout_after_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        events.append("dispatch")
        raise TimeoutError("remote completion status unknown")

    monkeypatch.setattr(steps, "add_episode_with_timeout", timeout_after_dispatch)
    monkeypatch.setattr(steps, "record_lifecycle_event", lambda **_kwargs: None)
    monkeypatch.setattr(steps, "emit_scheduler_task_event", _noop_async)

    with pytest.raises(TimeoutError, match="remote completion status unknown"):
        await steps.run_graphiti_extraction(
            _pipeline_context(events),
            finalize_under_gate=False,
        )

    assert events == ["begin", "dispatch"]


async def _noop_async(**_kwargs: Any) -> None:
    return None
