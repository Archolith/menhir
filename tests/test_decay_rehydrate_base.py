"""Compress/rehydrate base selection and the arguments the lifecycle service hands to the graph.

Offline: the graph is a stub, the revision sidecar is a real temp SQLite database. The Cypher
behaviour (locks, re-checks, restore) is covered online in test_decay_rehydrate_online.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from menhir.infrastructure.consolidation_queries import summary_sha256
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.infrastructure.telemetry import record_memory_revision, telemetry_store
from menhir.services.lifecycle_models import _DEFAULT_COMPRESS_DAYS
from menhir.services.lifecycle_service import LifecycleService
from tests.test_lifecycle_service import _FakeLLM


def _service(adapter, graphiti, tmp_path, llm=None) -> LifecycleService:
    return LifecycleService(
        graph_adapter=adapter,
        graphiti_client=graphiti,
        llm=llm,
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "pending.db"),
    )


def _compressed_node(**extra) -> dict[str, object]:
    return {
        "uuid": "n-1", "name": "Node", "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": "summary 2", "summary": None, "freshness": "COMPRESSED",
        "last_accessed": datetime.now(timezone.utc) - timedelta(days=100),
        "edge_count": 1, "sharpness": 0.05, "user_flagged": False, **extra,
    }


@pytest.mark.asyncio
async def test_rehydrate_merges_into_the_stored_original_not_the_oldest_archive_row(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    """Cycle 2: the node's original_content already holds 'orig + X'. The oldest archive row is
    the pre-X body; merging into it would lose X. Fails on main: it merges into the oldest row."""
    record_memory_revision(node_uuid="n-1", field="content", old_value="orig",
                           new_value="summary 1", changed_by="decay")
    record_memory_revision(node_uuid="n-1", field="content", old_value="orig + X",
                           new_value="summary 2", changed_by="decay")
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="orig + X")
    ]
    llm = _FakeLLM(merge_return="orig + X + Y")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)

    assert await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1") is True

    assert llm.merge_calls == [("orig + X", "Y")]


@pytest.mark.asyncio
async def test_rehydrate_fallback_uses_the_latest_decay_row_for_the_current_summary(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    """A node compressed before original_content was kept has none: the archive row whose
    new_value is the node's current summary is the base. Fails on main: oldest row wins."""
    record_memory_revision(node_uuid="n-1", field="content", old_value="orig",
                           new_value="summary 1", changed_by="decay")
    record_memory_revision(node_uuid="n-1", field="content", old_value="orig + X",
                           new_value="summary 2", changed_by="decay")
    stub_memory_graph_adapter.candidate_metadata = [_compressed_node()]
    llm = _FakeLLM(merge_return="merged")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)

    await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")

    assert llm.merge_calls == [("orig + X", "Y")]


@pytest.mark.asyncio
async def test_rehydrate_fallback_falls_through_to_the_oldest_row_then_the_content(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    record_memory_revision(node_uuid="n-1", field="content", old_value="oldest",
                           new_value="something else", changed_by="decay")
    stub_memory_graph_adapter.candidate_metadata = [_compressed_node()]
    llm = _FakeLLM(merge_return="merged")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)
    await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")
    assert llm.merge_calls == [("oldest", "Y")]

    stub_memory_graph_adapter.candidate_metadata = [_compressed_node(uuid="n-2")]
    llm2 = _FakeLLM(merge_return="merged")
    svc2 = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm2)
    await svc2.rehydrate_node("n-2", new_context="Y", source_episode_uuid="ep-1")
    assert llm2.merge_calls == [("summary 2", "Y")]


@pytest.mark.asyncio
async def test_rehydrate_hands_the_read_summary_and_base_to_the_write(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
) -> None:
    """Both rehydrate paths must pass what they read so the write can refuse if it moved.
    Fails on main: complete_rehydration is called without them."""
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="the body")
    ]
    seen: list[dict[str, object]] = []

    def record(node_uuid, updated_content=None, **kwargs):
        seen.append({"content": updated_content, **kwargs})
        return True

    monkeypatch.setattr(stub_memory_graph_adapter, "complete_rehydration", record)
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, _FakeLLM())

    await svc.rehydrate_node("n-1")
    await svc.rehydrate_node("n-1", new_context="ctx", source_episode_uuid="ep-1")

    assert [(s["read_summary"], s["read_base"]) for s in seen] == [
        ("summary 2", "the body"), ("summary 2", "the body"),
    ]


def test_decay_archive_lookup_is_vetoed_for_an_erased_node(tmp_path, monkeypatch) -> None:
    record_memory_revision(node_uuid="n-e", field="content", old_value="secret",
                           new_value="gist", changed_by="decay")
    assert telemetry_store.get_decay_original_content("n-e", "gist") == "secret"

    monkeypatch.setattr(telemetry_store, "_erasure_suppresses", lambda _uuid: True)

    assert telemetry_store.get_decay_original_content("n-e", "gist") is None


@pytest.mark.asyncio
async def test_decay_compress_passes_the_content_it_read_and_the_recency_window(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
) -> None:
    """The write re-checks against what the sweep read. Fails on main: neither is passed."""
    old = datetime.now(timezone.utc) - timedelta(days=400)
    stub_memory_graph_adapter.decay_candidates_by_freshness = {
        "ACTIVE": [
            {"uuid": "a-1", "name": "A", "content": "x" * 240, "summary": None,
             "scope": "PERSISTENT", "sharpness": 0.0, "edge_count": 2, "freshness": "ACTIVE",
             "user_flagged": False, "last_accessed": old, "created_at": old},
            # Blank content, summary only: the summary is compressed, but the node's content
            # as read is the blank one.
            {"uuid": "a-2", "name": "B", "content": None, "summary": "y" * 240,
             "scope": "PERSISTENT", "sharpness": 0.0, "edge_count": 2, "freshness": "ACTIVE",
             "user_flagged": False, "last_accessed": old, "created_at": old},
        ],
    }
    stub_graphiti_client.count_similar_by_cosine_results = 4
    calls: dict[str, dict[str, object]] = {}

    def record(node_uuid, summary, **kwargs):
        calls[node_uuid] = kwargs
        return True

    monkeypatch.setattr(stub_memory_graph_adapter, "compress_node", record)
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path,
                   _FakeLLM(compress_return="gist"))

    await svc.apply_decay()

    assert calls["a-1"]["read_content"] == "x" * 240
    assert calls["a-2"]["read_content"] is None
    assert calls["a-1"]["min_days_since_accessed"] == _DEFAULT_COMPRESS_DAYS
    assert calls["a-1"]["read_summary"] is None
    assert calls["a-2"]["read_summary"] == "y" * 240


@pytest.mark.asyncio
async def test_rehydrate_trusts_a_stored_base_only_with_a_matching_marker(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    """After an absorb or a rehydrate the stored base may belong to an older compression: it is
    used only while its marker equals the digest of the current summary."""
    record_memory_revision(node_uuid="n-1", field="content", old_value="archived body",
                           new_value="summary 2", changed_by="decay")
    marked = _compressed_node(original_content="stored body", rehydration_count=3,
                              compressed_summary_sha256=summary_sha256("summary 2"))
    stub_memory_graph_adapter.candidate_metadata = [marked]
    llm = _FakeLLM(merge_return="merged")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)
    await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")
    assert llm.merge_calls == [("stored body", "Y")]

    stale = _compressed_node(original_content="stored body", rehydration_count=3,
                             compressed_summary_sha256=summary_sha256("another summary"))
    stub_memory_graph_adapter.candidate_metadata = [stale]
    llm2 = _FakeLLM(merge_return="merged")
    svc2 = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm2)
    await svc2.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")
    assert llm2.merge_calls == [("archived body", "Y")]


@pytest.mark.asyncio
async def test_rehydrate_ignores_an_unmarked_base_of_a_node_already_rehydrated(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    """An unmarked original_content is a legacy body only while the node was never rehydrated."""
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="stale body", rehydration_count=2)
    ]
    llm = _FakeLLM(merge_return="merged")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)

    await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")

    assert llm.merge_calls == [("summary 2", "Y")]


@pytest.mark.asyncio
async def test_contextless_rehydrate_restores_the_base_and_records_the_revision(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
) -> None:
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="the body", fence_epoch=7,
                         compressed_summary_sha256=summary_sha256("summary 2"))
    ]
    seen: list[tuple[object, dict[str, object]]] = []

    def record(node_uuid, updated_content=None, **kwargs):
        seen.append((updated_content, kwargs))
        return True

    monkeypatch.setattr(stub_memory_graph_adapter, "complete_rehydration", record)
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node("n-1") is True

    assert seen[0][0] == "the body"
    assert seen[0][1]["read_epoch"] == 7
    assert telemetry_store.get_original_content("n-1") == "summary 2"


@pytest.mark.asyncio
async def test_contextless_rehydrate_without_a_known_base_only_flips_state(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
) -> None:
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="stale body", rehydration_count=2, fence_epoch=7)
    ]
    seen: list[tuple[object, dict[str, object]]] = []

    def record(node_uuid, updated_content=None, **kwargs):
        seen.append((updated_content, kwargs))
        return True

    monkeypatch.setattr(stub_memory_graph_adapter, "complete_rehydration", record)
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node("n-1") is True

    assert seen[0][0] is None
    assert seen[0][1]["read_epoch"] is None
    assert telemetry_store.get_original_content("n-1") is None


@pytest.mark.asyncio
async def test_contextless_rehydrate_of_a_legacy_node_that_absorbed_only_flips_state(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path, monkeypatch
) -> None:
    """Under the old code an absorb never reached original_content, so an unmarked base of a
    node with absorbed blocks lacks them and is not restored."""
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(original_content="old body", rehydration_count=0, absorbed_count=1)
    ]
    seen: list[object] = []

    def record(node_uuid, updated_content=None, **kwargs):
        seen.append(updated_content)
        return True

    monkeypatch.setattr(stub_memory_graph_adapter, "complete_rehydration", record)
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node("n-1") is True

    assert seen == [None]


@pytest.mark.asyncio
async def test_contextful_fallback_merges_the_summary_the_write_will_check(
    stub_memory_graph_adapter, stub_graphiti_client, tmp_path
) -> None:
    """With no base anywhere, the merge input is the summary read with the state (not the
    metadata row's summary), the same value passed as read_summary."""
    stub_memory_graph_adapter.candidate_metadata = [
        _compressed_node(summary="metadata summary")
    ]
    llm = _FakeLLM(merge_return="merged")
    svc = _service(stub_memory_graph_adapter, stub_graphiti_client, tmp_path, llm)

    await svc.rehydrate_node("n-1", new_context="Y", source_episode_uuid="ep-1")

    assert llm.merge_calls == [("summary 2", "Y")]
