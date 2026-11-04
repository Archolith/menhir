"""Compact recall mode also bounds the source_memories section (issue #434, option B)."""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.tools.recall import recall_memories as rm
from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool

_VOLATILE = ("retrieved_at", "recall_id")


def _sources(count: int, length: int) -> list[dict]:
    return [
        {
            "content": f"{i}" * length,
            "cosine": 0.9 - i * 0.01,
            "reference_time": "2026-07-22T00:00:00Z",
            "source": "claude-code",
            "uuid": f"src-{i}",
        }
        for i in range(count)
    ]


def _tool(sources: list[dict]) -> tuple[RecallMemoriesTool, MagicMock]:
    backend = MagicMock()
    backend.recall = AsyncMock(return_value={
        "preset": "knowledge", "results": [], "candidates_evaluated": 0,
        "source_memories": sources,
    })
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool, backend


async def _recall(sources: list[dict], **kwargs) -> tuple[dict, MagicMock]:
    tool, backend = _tool(sources)
    return json.loads(await tool.endpoint(query="q", **kwargs)), backend


def _limit(backend: MagicMock):
    return backend.recall.await_args.kwargs["source_memory_limit"]


@pytest.fixture(autouse=True)
def _deployment_default_10(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_deployment_source_memory_limit", lambda: 10)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, None])
async def test_non_compact_output_is_unchanged(monkeypatch, compact) -> None:
    monkeypatch.delenv("MENHIR_RECALL_COMPACT", raising=False)
    sources = _sources(10, 2500)
    payload, backend = await _recall(copy.deepcopy(sources), compact=compact)
    assert _limit(backend) is None
    assert payload["source_memories"] == sources
    assert all("truncated" not in e for e in payload["source_memories"])
    assert "source_memories_note" not in payload
    expected = {
        "query": "q", "preset": "knowledge", "count": 0, "items": [],
        "candidates_evaluated": 0, "source_memories": sources,
    }
    assert {k: v for k, v in payload.items() if k not in _VOLATILE} == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_asks_backend_for_five() -> None:
    payload, backend = await _recall(_sources(5, 2500), compact=True)
    assert _limit(backend) == 5
    got = payload["source_memories"]
    assert len(got) == 5
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    note = payload["source_memories_note"]
    assert "at most 5 source memories" in note
    assert "each cut to 1000 chars" in note
    assert "compact=false" in note


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deployment_default_below_five_is_respected(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_deployment_source_memory_limit", lambda: 3)
    payload, backend = await _recall(_sources(3, 100), compact=True)
    assert _limit(backend) == 3
    assert "source_memories_note" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deployment_default_read_from_env(monkeypatch) -> None:
    monkeypatch.undo()
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT", "3")
    _, backend = await _recall(_sources(3, 100), compact=True)
    assert _limit(backend) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deployment_default_exactly_five_has_no_count_note(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_deployment_source_memory_limit", lambda: 5)
    payload, backend = await _recall(_sources(5, 100), compact=True)
    assert _limit(backend) == 5
    assert "source_memories_note" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_limit_passes_through_and_cuts_length() -> None:
    sources = _sources(8, 2500)
    payload, backend = await _recall(sources, compact=True, source_memory_limit=8)
    got = payload["source_memories"]
    assert _limit(backend) == 8
    assert len(got) == 8
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    note = payload["source_memories_note"]
    assert "5" not in note
    assert "cut to 1000 chars" in note


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_zero_passes_through() -> None:
    _, backend = await _recall([], compact=True, source_memory_limit=0)
    assert _limit(backend) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_limit_with_short_entries_has_no_note() -> None:
    sources = _sources(8, 100)
    payload, backend = await _recall(copy.deepcopy(sources), compact=True, source_memory_limit=8)
    assert _limit(backend) == 8
    assert payload["source_memories"] == sources
    assert "source_memories_note" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cut_marks_with_ellipsis_at_exactly_1000_chars() -> None:
    payload, _ = await _recall(_sources(1, 2500), compact=True, source_memory_limit=1)
    entry = payload["source_memories"][0]
    assert len(entry["content"]) == 1000
    assert entry["content"].endswith("…")
    assert entry["content"][:-1] == "0" * 999
    assert entry["truncated"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_and_already_ellipsized_entries_unchanged() -> None:
    sources = _sources(3, 500)
    sources[1]["content"] = "x" * 999 + "…"  # exactly 1000 chars, already clipped by backend
    payload, _ = await _recall(copy.deepcopy(sources), compact=True, source_memory_limit=3)
    assert payload["source_memories"] == sources
    assert all("truncated" not in e for e in payload["source_memories"])
    assert "source_memories_note" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_note_on_count_reduction_only() -> None:
    payload, _ = await _recall(_sources(5, 100), compact=True)
    note = payload["source_memories_note"]
    assert "at most 5 source memories" in note
    assert "cut" not in note
    assert "more entries" in note


@pytest.mark.unit
@pytest.mark.asyncio
async def test_env_driven_compact(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_RECALL_COMPACT", "1")
    payload, backend = await _recall(_sources(5, 2500), compact=None)
    assert _limit(backend) == 5
    got = payload["source_memories"]
    assert len(got) == 5
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    assert "source_memories_note" in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_dicts_are_not_mutated() -> None:
    sources = _sources(5, 2500)
    snapshot = copy.deepcopy(sources)
    await _recall(sources, compact=True)
    assert sources == snapshot
