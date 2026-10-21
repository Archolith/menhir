"""Regression coverage for Graphiti node-dedupe candidate fan-out.

Adaptive request bisection is fork-native (``GraphitiRequestTooLargeError`` +
``_resolve_unresolved_indices``); these tests exercise it through the fork's
public ``resolve_extracted_nodes`` exactly as Menhir now consumes it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from graphiti_core.errors import GraphitiRequestTooLargeError
from graphiti_core.llm_client.request_guard import CHARS_PER_TOKEN


def _node(uuid: str, *, metadata: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        uuid=uuid,
        name=uuid,
        labels=["Entity"],
        attributes={"metadata": metadata} if metadata else {},
        summary="",
    )


async def _run_synthetic_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entity_count: int,
    max_entities_per_request: int,
) -> tuple[list[SimpleNamespace], dict[str, str], list[tuple[int, int]]]:
    import graphiti_core.utils.maintenance.node_operations as node_operations

    extracted_nodes = [_node(f"extracted-{idx}") for idx in range(entity_count)]
    candidates_by_extracted = [
        [_node(f"candidate-{entity_idx}-{candidate_idx}") for candidate_idx in range(15)]
        for entity_idx in range(entity_count)
    ]
    calls: list[tuple[int, int]] = []

    async def _collect_candidates(clients, nodes, existing_nodes_override):
        del clients, nodes, existing_nodes_override
        return candidates_by_extracted

    def _build_indexes(candidates):
        return SimpleNamespace(existing_nodes=list(candidates))

    def _leave_for_llm(nodes, indexes, state):
        del nodes, indexes, state

    async def _resolve_with_llm(
        llm_client,
        nodes,
        indexes,
        state,
        episode,
        previous_episodes,
        entity_types,
        **kwargs,
    ):
        del llm_client, episode, previous_episodes, entity_types
        batch_size = len(state.unresolved_indices)
        calls.append((batch_size, len(indexes.existing_nodes)))
        if batch_size > max_entities_per_request:
            raise GraphitiRequestTooLargeError("synthetic context limit")
        for idx in state.unresolved_indices:
            state.resolved_nodes[idx] = nodes[idx]
            state.uuid_map[nodes[idx].uuid] = nodes[idx].uuid

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect_candidates)
    monkeypatch.setattr(node_operations, "_build_candidate_indexes", _build_indexes)
    monkeypatch.setattr(node_operations, "_resolve_with_similarity", _leave_for_llm)
    monkeypatch.setattr(node_operations, "_resolve_with_llm", _resolve_with_llm)

    resolved, uuid_map, _duplicate_pairs = await node_operations.resolve_extracted_nodes(
        SimpleNamespace(llm_client=object()),
        extracted_nodes,
    )
    return resolved, uuid_map, calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_69_entities_keep_the_normal_single_request_path(monkeypatch) -> None:
    resolved, uuid_map, calls = await _run_synthetic_resolution(
        monkeypatch,
        entity_count=69,
        max_entities_per_request=69,
    )

    assert [node.uuid for node in resolved] == [f"extracted-{idx}" for idx in range(69)]
    assert uuid_map == {f"extracted-{idx}": f"extracted-{idx}" for idx in range(69)}
    assert calls == [(69, 69 * 15)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_98_entities_bisect_oversized_candidate_union(monkeypatch) -> None:
    resolved, uuid_map, calls = await _run_synthetic_resolution(
        monkeypatch,
        entity_count=98,
        max_entities_per_request=24,
    )

    assert [node.uuid for node in resolved] == [f"extracted-{idx}" for idx in range(98)]
    assert uuid_map == {f"extracted-{idx}": f"extracted-{idx}" for idx in range(98)}
    assert calls[0] == (98, 98 * 15)
    assert any(entity_count > 24 for entity_count, _candidate_count in calls)
    successful_calls = [call for call in calls if call[0] <= 24]
    assert sum(entity_count for entity_count, _candidate_count in successful_calls) == 98
    assert all(candidate_count == entity_count * 15 for entity_count, candidate_count in calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_graphiti_prompt_builder_splits_until_request_fits(monkeypatch) -> None:
    """Exercise real prompt assembly, not an entity-count stand-in for payload size.

    The oversized request is rejected by the fork-native ceiling mechanism (the same
    ``GraphitiRequestTooLargeError`` the fork's request guard raises) and the batch
    is bisected until every sub-request fits.
    """
    import graphiti_core.utils.maintenance.node_operations as node_operations

    ceiling = 60_000
    extracted_nodes = [_node(f"probe-{idx}") for idx in range(98)]
    candidates_by_extracted = [
        [
            _node(
                f"existing-{entity_idx}-{candidate_idx}",
                metadata=f"entity={entity_idx};candidate={candidate_idx};" + ("x" * 300),
            )
            for candidate_idx in range(15)
        ]
        for entity_idx in range(98)
    ]
    attempted_estimates: list[int] = []
    accepted_estimates: list[int] = []

    async def _collect_candidates(clients, nodes, existing_nodes_override):
        del clients, nodes, existing_nodes_override
        return candidates_by_extracted

    def _leave_for_llm(nodes, indexes, state):
        del nodes, indexes, state

    class _GuardedLlmClient:
        async def generate_response(self, messages, response_model=None, **kwargs):
            del response_model, kwargs
            request = [{"role": message.role, "content": message.content} for message in messages]
            total_chars = sum(len(message["content"]) for message in request)
            estimated_tokens = (total_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
            attempted_estimates.append(estimated_tokens)
            if estimated_tokens > ceiling:
                raise GraphitiRequestTooLargeError("synthetic context limit")
            accepted_estimates.append(estimated_tokens)

            extracted_count = messages[-1].content.count('"entity_type"')
            return {
                "entity_resolutions": [
                    {"id": idx, "name": f"probe-{idx}", "duplicate_candidate_id": -1}
                    for idx in range(extracted_count)
                ]
            }

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect_candidates)
    monkeypatch.setattr(node_operations, "_resolve_with_similarity", _leave_for_llm)

    resolved, uuid_map, duplicate_pairs = await node_operations.resolve_extracted_nodes(
        SimpleNamespace(llm_client=_GuardedLlmClient()),
        extracted_nodes,
    )

    assert [node.uuid for node in resolved] == [f"probe-{idx}" for idx in range(98)]
    assert uuid_map == {f"probe-{idx}": f"probe-{idx}" for idx in range(98)}
    assert duplicate_pairs == []
    assert attempted_estimates[0] > ceiling
    assert len(attempted_estimates) > len(accepted_estimates)
    assert accepted_estimates
    assert all(estimate <= ceiling for estimate in accepted_estimates)
