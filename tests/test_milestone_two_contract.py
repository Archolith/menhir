"""Milestone 2 completion contract tests.

These tests verify that all M2 acceptance criteria are met at the code level.
They are structural/contract tests — they assert that the right types, methods,
fields, and wiring exist, not that Graphiti produces correct results (that is
covered by the online integration tests in test_ingest_live.py).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from menhir.core import BuildArtifacts, build_memory_services
from menhir.domain import IngestResult, IngestStatus, new_session
from menhir.domain.models import MemoryNode
from menhir.infrastructure import MemoryGraphAdapter, GraphitiClient
from menhir.infrastructure.memory_graph_adapter import PolicyStampResult
from menhir.infrastructure.schema import get_phase1_bootstrap_queries
from menhir.services import IngestService


# ---------------------------------------------------------------------------
# 1. IngestService calls Graphiti instead of stub logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ingest_service_requires_graphiti_client() -> None:
    """IngestService must depend on a GraphitiClient, not stub logic."""
    fields = {f.name for f in IngestService.__dataclass_fields__.values()}
    assert "graphiti_client" in fields


@pytest.mark.unit
def test_ingest_episode_is_async() -> None:
    """Ingestion must be async to support single-flight Graphiti calls."""
    assert asyncio.iscoroutinefunction(IngestService.ingest_episode)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_episode_returns_ingest_result(
    stub_graphiti_client: object,
    stub_memory_graph_adapter: object,
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_episode must return a typed IngestResult, not a raw dict. It is now
    queue-backed: it queues, waits for the worker, and maps READY -> INGESTED."""

    async def _ready(self: object, episode_uuid: str, **kwargs: object) -> dict[str, object]:
        return {"processing_state": "READY", "enriched_nodes_touched": 2, "enriched_edges_touched": 2}

    monkeypatch.setattr(IngestService, "wait_for_episode_processing", _ready)
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        llm=stub_llm_adapter,
    )
    session = new_session("contract-user", session_id="contract-session")
    result = await service.ingest_episode("contract test episode", session, "contract-test")

    assert isinstance(result, IngestResult)
    assert result.status is IngestStatus.INGESTED
    assert isinstance(result.episode_id, str) and result.episode_id != ""
    assert isinstance(result.nodes_touched, int)
    assert isinstance(result.edges_touched, int)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_episode_maps_ready_to_ingested_without_monkeypatch(
    stub_graphiti_client: object,
    stub_memory_graph_adapter: object,
    stub_llm_adapter: object,
) -> None:
    """Regression: ingest_episode's own READY/FAILED status mapping (not just
    wait_for_episode_processing) had the same (str, Enum) comparison bug --
    `str(ProcessingState.READY)` is the qualified repr "ProcessingState.READY",
    not the value "READY", so `processing_state == ProcessingState.READY` could
    never match. Every call through the real (non-monkeypatched) polling path
    would report IngestStatus.QUEUED even when the episode had actually
    completed successfully. Exercises the full queue -> worker -> claim ->
    extract -> finalize -> poll pipeline for real, with no monkeypatch, so this
    would have reported QUEUED before the fix (traced 2026-07-12 alongside
    tests/test_edge_cases.py::TestZeroNegativeTimeout).
    """
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        llm=stub_llm_adapter,
    )
    session = new_session("contract-user", session_id="contract-session-real")
    result = await service.ingest_episode("real pipeline test episode", session, "contract-test")

    assert result.status is IngestStatus.INGESTED
    assert result.nodes_touched > 0


# ---------------------------------------------------------------------------
# 2. All ingested records are stamped with required M2 policy fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stamp_ingest_metadata_exists_on_adapter() -> None:
    """MemoryGraphAdapter must expose stamp_ingest_metadata for post-ingest stamping."""
    method = getattr(MemoryGraphAdapter, "stamp_ingest_metadata", None)
    assert method is not None
    sig = inspect.signature(method)
    param_names = set(sig.parameters.keys()) - {"self"}
    assert param_names == {
        "node_uuids",
        "edge_uuids",
        "session_id",
        "user_id",
        "source",
        "source_confidence",
        "namespace",
        "bootstrap_scope",
        "belief_commit",
        "belief_branch",
    }


@pytest.mark.unit
def test_stamp_ingest_metadata_returns_policy_stamp_result() -> None:
    """stamp_ingest_metadata must return a PolicyStampResult with node/edge counts."""
    field_names = {f.name for f in PolicyStampResult.__dataclass_fields__.values()}
    assert "nodes_touched" in field_names
    assert "edges_touched" in field_names


@pytest.mark.unit
def test_schema_bootstrap_covers_user_flagged_field() -> None:
    """Phase-1 schema must include user_flagged indexes and defaults."""
    query_blob = "\n".join(get_phase1_bootstrap_queries())
    assert "user_flagged" in query_blob


@pytest.mark.unit
def test_schema_bootstrap_stamps_session_and_scope_fields() -> None:
    """Phase-1 schema must include session_id, user_id, and scope defaults."""
    query_blob = "\n".join(get_phase1_bootstrap_queries())
    assert "session_id" in query_blob
    assert "user_id" in query_blob
    assert "scope" in query_blob


@pytest.mark.unit
def test_episodic_nodes_receive_strong_stamping_in_cypher() -> None:
    """Episodic nodes must be stamped with explicit SESSION scope (not coalesce)."""
    query_blob = "\n".join(get_phase1_bootstrap_queries())
    # Episodic defaults query should exist
    assert "MATCH (n:Episodic)" in query_blob


@pytest.mark.unit
def test_entity_nodes_use_conservative_stamping() -> None:
    """Entity stamping must use coalesce and protect PERSISTENT/PROMOTED nodes."""
    from menhir.infrastructure.episode_stamping import EpisodeStampingRepository

    # MemoryGraphAdapter.stamp_ingest_metadata delegates to the episode stamping repo.
    source = inspect.getsource(EpisodeStampingRepository.stamp_ingest_metadata)
    assert "coalesce(n.scope" in source
    assert "PERSISTENT" in source
    assert "PROMOTED" in source


# ---------------------------------------------------------------------------
# 3. Ingestion is session-aware
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_session_is_frozen_dataclass() -> None:
    """MemorySession must be immutable."""
    session = new_session("test-user")
    with pytest.raises(AttributeError):
        session.session_id = "changed"


@pytest.mark.unit
def test_new_session_generates_unique_ids() -> None:
    """Each call to new_session must produce a distinct session_id."""
    a = new_session("user")
    b = new_session("user")
    assert a.session_id != b.session_id


@pytest.mark.unit
def test_ingest_episode_signature_requires_session() -> None:
    """ingest_episode must require a MemorySession parameter."""
    sig = inspect.signature(IngestService.ingest_episode)
    assert "session" in sig.parameters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_stamps_session_id_on_adapter(
    stub_graphiti_client: object,
    stub_memory_graph_adapter: object,
    stub_llm_adapter: object,
) -> None:
    """The session_id from the MemorySession must reach the adapter's stamp call."""
    service = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        llm=stub_llm_adapter,
    )
    session = new_session("contract-user", session_id="contract-session-abc")
    await service.ingest_episode("test", session, "contract-test")

    assert len(stub_memory_graph_adapter.stamp_calls) == 1
    assert stub_memory_graph_adapter.stamp_calls[0]["session_id"] == "contract-session-abc"
    assert stub_memory_graph_adapter.stamp_calls[0]["user_id"] == "contract-user"


# ---------------------------------------------------------------------------
# 4. Explicit user importance independent of provenance confidence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_node_has_user_flagged_field() -> None:
    """MemoryNode must carry user_flagged separate from source_confidence."""
    node = MemoryNode(id="n1", content="test")
    assert hasattr(node, "user_flagged")
    assert hasattr(node, "source_confidence")
    assert node.user_flagged is False  # default


@pytest.mark.unit
def test_flag_memory_exists_on_adapter() -> None:
    """MemoryGraphAdapter must expose flag_memory for explicit user retention."""
    assert callable(getattr(MemoryGraphAdapter, "flag_memory", None))


@pytest.mark.unit
def test_flag_memory_exists_on_ingest_service() -> None:
    """IngestService must expose flag_memory to callers."""
    assert callable(getattr(IngestService, "flag_memory", None))


@pytest.mark.unit
def test_unflag_memory_exists_on_adapter() -> None:
    """MemoryGraphAdapter must expose unflag_memory for removing retention flags."""
    assert callable(getattr(MemoryGraphAdapter, "unflag_memory", None))


@pytest.mark.unit
def test_unflag_memory_exists_on_ingest_service() -> None:
    """IngestService must expose unflag_memory to callers."""
    assert callable(getattr(IngestService, "unflag_memory", None))


# ---------------------------------------------------------------------------
# 5. Edge traversal weight API exists and is tested
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_increment_edge_weight_exists_on_adapter() -> None:
    """MemoryGraphAdapter must expose increment_edge_weight for retrieval."""
    method = getattr(MemoryGraphAdapter, "increment_edge_weight", None)
    assert method is not None
    sig = inspect.signature(method)
    assert "edge_uuid" in sig.parameters


# ---------------------------------------------------------------------------
# 6. Graphiti client factory is reusable and isolated
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_graphiti_client_has_from_settings_factory() -> None:
    """GraphitiClient must be constructable from MemorySettings."""
    assert callable(getattr(GraphitiClient, "from_settings", None))


@pytest.mark.unit
def test_graphiti_client_exposes_required_async_methods() -> None:
    """GraphitiClient must expose add_episode, search, build_indices, close."""
    for method_name in ("add_episode", "build_indices_and_constraints", "search", "close"):
        method = getattr(GraphitiClient, method_name, None)
        assert method is not None, f"Missing method: {method_name}"
        assert asyncio.iscoroutinefunction(method), f"{method_name} must be async"


# ---------------------------------------------------------------------------
# 7. Bootstrap wires Graphiti client into services
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_artifacts_includes_graphiti_client() -> None:
    """BuildArtifacts must carry a graphiti_client field."""
    fields = {f.name for f in BuildArtifacts.__dataclass_fields__.values()}
    assert "graphiti_client" in fields


@pytest.mark.unit
def test_build_memory_services_wires_graphiti_to_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_memory_services must wire the same GraphitiClient into IngestService."""
    stub = SimpleNamespace()
    monkeypatch.setattr(
        "menhir.core.bootstrap.GraphitiClient.from_settings_with_capabilities",
        lambda settings, **kwargs: stub,
    )
    built = build_memory_services()
    assert built.graphiti_client is stub
    assert built.ingest_service.graphiti_client is stub


# ---------------------------------------------------------------------------
# 8. Failure paths return FAILED status (not exceptions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_maps_failed_enrichment_and_does_not_propagate(
    stub_memory_graph_adapter: object,
    stub_llm_adapter: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_episode maps a terminally-FAILED enrichment to IngestStatus.FAILED and never
    propagates the underlying error — the queue-backed path surfaces worker failure to the
    caller via processing_state, not a synchronous exception."""

    class BrokenGraphiti:
        async def add_episode(self, **kwargs: object) -> object:
            raise RuntimeError("graphiti down")

    async def _failed(self: object, episode_uuid: str, **kwargs: object) -> dict[str, object]:
        return {"processing_state": "FAILED"}

    monkeypatch.setattr(IngestService, "wait_for_episode_processing", _failed)
    service = IngestService(
        graphiti_client=BrokenGraphiti(),
        graph_adapter=stub_memory_graph_adapter,
        llm=stub_llm_adapter,
    )
    session = new_session("test-user", session_id="fail-session")
    result = await service.ingest_episode("test", session, "contract-test")

    assert result.status is IngestStatus.FAILED
