"""Memory write bounds are shared and run before write-side effects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.core.backend_runtime import RuntimeProvider
from menhir.domain.session import new_session
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
from menhir.mcp.tools.ingest.add_memory_and_track import AddMemoryAndTrackTool
from menhir.services.ingest_limits import MAX_DIFF_CHARS, MAX_EPISODE_CHARS
from menhir.services.ingest_service import IngestService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _service(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter):
    return IngestService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )


@pytest.mark.parametrize(
    ("text_size", "diff_size"),
    [
        (MAX_EPISODE_CHARS - 1, None),
        (MAX_EPISODE_CHARS, None),
        (1, MAX_DIFF_CHARS - 1),
        (1, MAX_DIFF_CHARS),
    ],
)
async def test_shared_intake_accepts_payloads_through_the_limit(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
    text_size,
    diff_size,
) -> None:
    service = _service(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter)
    monkeypatch.setattr(service, "_ensure_enrichment_worker", AsyncMock())
    monkeypatch.setattr(service, "_enqueue_pending_episode", AsyncMock())

    result = await service.queue_episode_for_enrichment(
        "x" * text_size,
        new_session("bounds-user"),
        "unit-test",
        diff=None if diff_size is None else "d" * diff_size,
    )

    assert result.episode_id in stub_memory_graph_adapter.pending_episode_rows
    service._enqueue_pending_episode.assert_awaited_once_with(result.episode_id)


@pytest.mark.parametrize(
    ("text", "diff", "message"),
    [
        ("x" * (MAX_EPISODE_CHARS + 1), None, "Memory text exceeds"),
        ("x", "d" * (MAX_DIFF_CHARS + 1), "Memory diff exceeds"),
    ],
    ids=["text-over-limit", "diff-over-limit"],
)
async def test_shared_intake_rejects_before_persistence_queue_or_evidence(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
    text,
    diff,
    message,
) -> None:
    service = _service(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter)
    side_effects = [
        "fetch_turn_evidence",
        "create_pending_episode",
        "link_episode_admission",
        "create_evidence_projection",
        "record_admission_audit",
    ]
    for name in side_effects:
        monkeypatch.setattr(
            stub_memory_graph_adapter,
            name,
            MagicMock(side_effect=AssertionError(f"unexpected side effect: {name}")),
            raising=False,
        )
    monkeypatch.setattr(
        service,
        "_ensure_enrichment_worker",
        AsyncMock(side_effect=AssertionError("unexpected worker start")),
    )
    monkeypatch.setattr(
        service,
        "_enqueue_pending_episode",
        AsyncMock(side_effect=AssertionError("unexpected enqueue")),
    )

    with pytest.raises(ValueError, match=message):
        await service.queue_episode_for_enrichment(
            text,
            new_session("bounds-user"),
            "user",
            diff=diff,
            turn_evidence_uuid="evidence-1",
        )

    assert stub_memory_graph_adapter.pending_episode_rows == {}
    assert service._pending_queue is None
    assert service._queued_episode_ids == set()
    for name in side_effects:
        getattr(stub_memory_graph_adapter, name).assert_not_called()
    service._ensure_enrichment_worker.assert_not_awaited()
    service._enqueue_pending_episode.assert_not_awaited()


@pytest.mark.parametrize("tool_class", [AddMemoryTool, AddMemoryAndTrackTool])
@pytest.mark.parametrize(
    ("text", "diff", "message"),
    [
        ("x" * (MAX_EPISODE_CHARS + 1), None, "Memory text exceeds"),
        ("x", "d" * (MAX_DIFF_CHARS + 1), "Memory diff exceeds"),
    ],
    ids=["text-over-limit", "diff-over-limit"],
)
async def test_mcp_queued_writers_reach_the_shared_boundary(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
    tool_class,
    text,
    diff,
    message,
) -> None:
    service = _service(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter)
    backend = RuntimeProvider(
        SimpleNamespace(ingest_service=service, graph_adapter=stub_memory_graph_adapter),
        new_session("process-user"),
    )
    monkeypatch.setattr(tool_class, "get_backend", lambda self: backend)
    session = SimpleNamespace(user_id="mcp-user", session_id="mcp-session")
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.add_memory.get_mcp_session", lambda: session
    )
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.add_memory_and_track.get_mcp_session", lambda: session
    )

    with pytest.raises(ValueError, match=message):
        await tool_class().endpoint(text=text, diff=diff)

    assert stub_memory_graph_adapter.pending_episode_rows == {}


async def test_temporal_mcp_write_rejects_oversized_text_before_direct_write(
    monkeypatch,
) -> None:
    graph_adapter = SimpleNamespace(create_temporal=MagicMock())
    backend = RuntimeProvider(
        SimpleNamespace(graph_adapter=graph_adapter),
        new_session("process-user"),
    )
    monkeypatch.setattr(AddMemoryTool, "get_backend", lambda self: backend)

    with pytest.raises(ValueError, match="Memory text exceeds"):
        await AddMemoryTool().endpoint(
            text="x" * (MAX_EPISODE_CHARS + 1),
            type="TEMPORAL",
            valid_at="2027-02-16",
        )

    graph_adapter.create_temporal.assert_not_called()
