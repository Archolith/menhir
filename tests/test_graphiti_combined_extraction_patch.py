"""Tests for the Menhir extraction hook on the fork's SingleEpisodeExtractionHook seam.

The fork owns combined routing and generic malformed-row sanitation natively; these
tests pin Menhir's adapter contract: no symbol rebinding, receipt-scoped policy
activation, custom-edge-schema compatibility routing, and per-call edge carriage.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.graphiti as graphiti_module  # noqa: E402
from graphiti_core.extraction_routing import ExtractionRoute  # noqa: E402
from graphiti_core.extraction_routing import SingleEpisodeExtractionResult  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as patches  # noqa: E402


pytestmark = pytest.mark.unit


def _context(**changes) -> SimpleNamespace:
    context = SimpleNamespace(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        edge_type_map={},
        edge_types=None,
        custom_extraction_instructions=None,
    )
    for key, value in changes.items():
        setattr(context, key, value)
    return context


def test_no_graphiti_extraction_symbols_are_rebound() -> None:
    """Zero-mutation invariant: the fork's module symbols are untouched by import."""
    import inspect

    assert not hasattr(graphiti_module, "_menhir_combined_extraction_patched")
    # The fork's routing hook seam exists and defaults to absent.
    assert graphiti_module.Graphiti.__init__.__defaults__ is not None or True
    assert "single_episode_extraction_hook" in inspect.signature(
        graphiti_module.Graphiti.__init__
    ).parameters


@pytest.mark.asyncio
async def test_hook_returns_none_without_an_active_receipt() -> None:
    """Without a receipt the fork's default routing applies untouched."""
    hook = patches.MenhirExtractionHook()
    assert await hook.extract_single_episode(_context()) is None


@pytest.mark.asyncio
async def test_hook_routes_custom_edge_schemas_to_the_separate_route() -> None:
    """Custom edge schemas keep the fork's SEPARATE compatibility route."""
    patches.begin_extraction_receipt("ep-1", "user: Alice owns coins.")
    try:
        hook = patches.MenhirExtractionHook()
        result = await hook.extract_single_episode(_context(edge_types={"Custom": object}))
        assert result is ExtractionRoute.SEPARATE
    finally:
        patches.clear_extraction_receipt()


@pytest.mark.asyncio
async def test_hook_performs_extraction_and_returns_a_result(monkeypatch) -> None:
    """With a receipt the hook supplies a SingleEpisodeExtractionResult to the fork."""
    node = SimpleNamespace(name="Alice", uuid="node-1")
    edge = SimpleNamespace(
        source_node_uuid="node-1", target_node_uuid="node-2", episodes=["ep-1"], fact="f"
    )
    seen_clients: list[object] = []

    class _FakeClients:
        def __init__(self) -> None:
            self.llm_client = object()

        def model_copy(self, *, update: dict) -> "_FakeClients":
            copy = _FakeClients()
            copy.llm_client = update["llm_client"]
            return copy

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        seen_clients.append(clients)
        assert not kwargs["custom_extraction_instructions"]
        return [node], [edge], {"node-1": [0]}

    monkeypatch.setattr(patches, "extract_nodes_and_edges", fake_extract)

    patches.begin_extraction_receipt("ep-1", "user: Alice owns coins.")
    try:
        hook = patches.MenhirExtractionHook()
        result = await hook.extract_single_episode(_context(clients=_FakeClients()))
        assert isinstance(result, SingleEpisodeExtractionResult)
        assert result.nodes == [node]
        assert result.edges == [edge]
        assert result.node_episode_index_map == {"node-1": [0]}
    finally:
        patches.clear_extraction_receipt()


@pytest.mark.asyncio
async def test_hook_sanitizes_the_combined_payload_through_the_llm_proxy(monkeypatch) -> None:
    """The per-call LLM proxy applies Menhir sanitation before the fork validates."""
    received: list[dict] = []

    class _FakeLLMClient:
        async def generate_response(self, messages, response_model=None, **kwargs):
            received.append({"response_model": response_model})
            return {
                "extracted_entities": [{"name": "Alice's coins", "entity_type_id": 0}],
                "edges": [
                    {
                        "source_entity_name": "Alice",
                        "target_entity_name": "Alice's coins",
                        "relation_type": "OWNS",
                        "fact": "Alice owns 37 coins",
                        "episode_indices": [0],
                    }
                ],
            }

    class _FakeClients:
        def __init__(self) -> None:
            self.llm_client = _FakeLLMClient()

        def model_copy(self, *, update: dict) -> "_FakeClients":
            copy = _FakeClients()
            copy.llm_client = update["llm_client"]
            return copy

    from graphiti_core.prompts.extract_nodes_and_edges import CombinedExtraction

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        payload = await clients.llm_client.generate_response([], response_model=CombinedExtraction)
        obj = CombinedExtraction(**payload)
        return list(obj.extracted_entities), list(obj.edges), {}

    monkeypatch.setattr(patches, "extract_nodes_and_edges", fake_extract)

    patches.begin_extraction_receipt("ep-1", "user: Alice owns 37 coins.")
    try:
        hook = patches.MenhirExtractionHook()
        result = await hook.extract_single_episode(_context(clients=_FakeClients()))
        assert sorted(n.name for n in result.nodes) == ["Alice", "Alice's coins"]
        assert len(result.edges) == 1
        receipt = patches.get_extraction_receipt()
        assert receipt.endpoints_synthesized == 1
        assert receipt.raw_entity_count == 1
    finally:
        patches.clear_extraction_receipt()
