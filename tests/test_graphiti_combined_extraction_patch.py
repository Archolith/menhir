"""Tests for Menhir's Graphiti single-episode combined extraction bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.graphiti as graphiti_module  # noqa: E402

import menhir.infrastructure.graphiti_extraction_patches as patches  # noqa: E402


def test_patch_installs_once() -> None:
    patches._patch_graphiti_combined_extraction()
    assert graphiti_module.extract_nodes is patches._extract_nodes_combined_for_add_episode
    assert graphiti_module.extract_edges is patches._extract_edges_from_combined_cache

    extract_nodes = graphiti_module.extract_nodes
    extract_edges = graphiti_module.extract_edges
    patches._patch_graphiti_combined_extraction()
    assert graphiti_module.extract_nodes is extract_nodes
    assert graphiti_module.extract_edges is extract_edges


@pytest.mark.asyncio
async def test_combined_edges_are_reused_once(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = object()
    original_calls: list[tuple[object, ...]] = []

    async def fake_combined(*args: object, **kwargs: object):
        return ["node"], [edge], {"node": [0]}

    async def fake_original(*args: object, **kwargs: object):
        original_calls.append(args)
        return ["fallback"]

    monkeypatch.setattr(patches, "_run_graphiti_combined_extraction", fake_combined)
    monkeypatch.setattr(patches, "_original_graphiti_extract_edges", fake_original)
    patches._combined_extraction_cache.set(None)
    episode = SimpleNamespace(uuid="episode-1")

    nodes, index_map = await patches._extract_nodes_combined_for_add_episode(
        object(), episode, []
    )
    assert nodes == ["node"]
    assert index_map == {"node": [0]}

    edges = await patches._extract_edges_from_combined_cache(
        object(), episode, nodes, [], {}, "group"
    )
    assert edges == [edge]
    assert original_calls == []

    fallback = await patches._extract_edges_from_combined_cache(
        object(), episode, nodes, [], {}, "group"
    )
    assert fallback == ["fallback"]
    assert len(original_calls) == 1


@pytest.mark.asyncio
async def test_context_cache_is_isolated_between_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_combined(
        clients: object,
        episode: object,
        previous_episodes: list[object],
        entity_types: object,
        excluded_entity_types: object,
        custom_extraction_instructions: str | None,
    ):
        uuid = str(getattr(episode, "uuid"))
        return [f"node-{uuid}"], [f"edge-{uuid}"], {}

    async def fail_original(*args: object, **kwargs: object):
        raise AssertionError("combined cache unexpectedly missed")

    monkeypatch.setattr(patches, "_run_graphiti_combined_extraction", fake_combined)
    monkeypatch.setattr(patches, "_original_graphiti_extract_edges", fail_original)
    patches._combined_extraction_cache.set(None)

    async def run_one(uuid: str) -> list[object]:
        episode = SimpleNamespace(uuid=uuid)
        nodes, _ = await patches._extract_nodes_combined_for_add_episode(
            object(), episode, []
        )
        await asyncio.sleep(0)
        return await patches._extract_edges_from_combined_cache(
            object(), episode, nodes, [], {}, "group"
        )

    first, second = await asyncio.gather(run_one("one"), run_one("two"))
    assert first == ["edge-one"]
    assert second == ["edge-two"]


@pytest.mark.asyncio
async def test_custom_edge_schema_uses_original_edge_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_original(*args: object, **kwargs: object):
        return ["typed-edge"]

    monkeypatch.setattr(patches, "_original_graphiti_extract_edges", fake_original)
    patches._combined_extraction_cache.set(("episode-1", ["combined-edge"]))
    episode = SimpleNamespace(uuid="episode-1")

    edges = await patches._extract_edges_from_combined_cache(
        object(), episode, [], [], {}, "group", {"CustomEdge": object}
    )
    assert edges == ["typed-edge"]
    assert patches._combined_extraction_cache.get() is None
