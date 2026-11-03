"""Compact recall mode also bounds the source_memories section (issue #434, option B)."""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

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


async def _recall(sources: list[dict], **kwargs) -> dict:
    tool, _ = _tool(sources)
    return json.loads(await tool.endpoint(query="q", **kwargs))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, None])
async def test_non_compact_output_is_unchanged(monkeypatch, compact) -> None:
    monkeypatch.delenv("MENHIR_RECALL_COMPACT", raising=False)
    sources = _sources(10, 2500)
    payload = await _recall(copy.deepcopy(sources), compact=compact)
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
async def test_compact_cuts_count_and_length() -> None:
    sources = _sources(10, 2500)
    payload = await _recall(sources, compact=True)
    got = payload["source_memories"]
    assert [e["uuid"] for e in got] == [f"src-{i}" for i in range(5)]
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    assert "5 entries, 1000 chars each" in payload["source_memories_note"]
    assert "compact=false" in payload["source_memories_note"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_limit_wins_over_count_cap() -> None:
    sources = _sources(8, 2500)
    tool, backend = _tool(sources)
    payload = json.loads(await tool.endpoint(query="q", compact=True, source_memory_limit=8))
    got = payload["source_memories"]
    assert len(got) == 8
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    assert backend.recall.await_args.kwargs["source_memory_limit"] == 8
    assert "source_memories_note" in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_entries_pass_through() -> None:
    sources = _sources(3, 500)
    payload = await _recall(copy.deepcopy(sources), compact=True)
    assert payload["source_memories"] == sources
    assert all("truncated" not in e for e in payload["source_memories"])
    assert "source_memories_note" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_env_driven_compact(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_RECALL_COMPACT", "1")
    payload = await _recall(_sources(10, 2500), compact=None)
    got = payload["source_memories"]
    assert len(got) == 5
    assert all(len(e["content"]) == 1000 and e["truncated"] is True for e in got)
    assert "source_memories_note" in payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_dicts_are_not_mutated() -> None:
    sources = _sources(10, 2500)
    snapshot = copy.deepcopy(sources)
    await _recall(sources, compact=True)
    assert sources == snapshot
    assert len(sources) == 10
