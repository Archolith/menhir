"""U12 delta-audit fixes: CF-278, CF-279, CF-281, CF-282, CF-283."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from menhir.config import MemorySettings
from menhir.core import build_memory_services
from menhir.infrastructure import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_pipeline import run_recall

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("hook", [True, False])
def test_hook_and_backend_recall_agree_on_scalar_history(monkeypatch, stub_graphiti_client, flag, hook):
    from menhir.cli.bootstrap import build_hook_services

    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr("menhir.cli.bootstrap.Neo4jRepository", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "menhir.core.bootstrap.GraphitiClient.from_settings_with_capabilities",
        lambda settings, **kwargs: stub_graphiti_client,
    )
    monkeypatch.setattr(
        "menhir.infrastructure.graphiti_client.GraphitiClient.from_settings",
        lambda settings: stub_graphiti_client,
    )
    monkeypatch.setattr("menhir.core.bootstrap.LLMAdapter.from_settings", lambda settings: SimpleNamespace())
    settings = MemorySettings(personal_memory_scalar_history_enabled=flag)
    built = build_hook_services(settings) if hook else build_memory_services(settings)
    assert built.context_builder.recall_service.scalar_history_enabled is flag


def test_fetch_temporal_facts_has_no_namespace_parameter():
    assert "namespace" not in inspect.signature(MemoryQueryRepository.fetch_temporal_facts).parameters
    assert "namespace" not in inspect.signature(MemoryGraphAdapter.fetch_temporal_facts).parameters
    with pytest.raises(TypeError):
        MemoryQueryRepository(SimpleNamespace()).fetch_temporal_facts(["u"], namespace="ns")


def test_fetch_node_receipts_name_falls_back_to_view_name():
    captured: list[str] = []

    class _Neo4j:
        def execute(self, query, params=None):
            captured.append(query)
            return []

    MemoryQueryRepository(_Neo4j()).fetch_node_receipts("u")
    assert "coalesce(n['view_name'], n.name) AS name" in captured[0]


@pytest.mark.asyncio
async def test_typed_history_prev_offset_never_negative():
    from tests.test_recall_timeline import _scalar_entry, _StubAdapter, _svc

    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    adapter.scalar_views = [{"uuid": "view-1", "attribute": "version"}]
    adapter.scalar_entries["view-1"] = [_scalar_entry(i) for i in range(1, 14)]
    result = await _svc(adapter).recall_timeline(
        namespace="ns", subject="Rachel", history_view="view-1",
        history_offset=3, history_limit=10,
    )
    assert result.histories[0].prev_offset == 0


@pytest.mark.asyncio
async def test_source_memory_task_cancelled_when_recall_raises():
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig

    cancelled = {"flag": False}
    started = asyncio.Event()

    class _Client:
        async def search_scored(self, query, num_results, group_ids):
            return [("entity-1", "Entity One", 0.9)]

        async def embed_query(self, query):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled["flag"] = True
                raise

    def _boom(uuids):
        raise RuntimeError("metadata down")

    service = SimpleNamespace(
        graphiti_client=_Client(),
        graph_adapter=SimpleNamespace(fetch_candidate_metadata=_boom),
        scalar_view_authority_enabled=False,
        scalar_history_enabled=False,
    )
    with pytest.raises(RuntimeError):
        await run_recall(
            service, "q", source_memory_limit=3, include_session=True,
            tuning=RetrievalTuningConfig(),
        )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started.is_set()
    assert cancelled["flag"] is True
