"""A counted slot is stated once, by its counted verdict, never also as a history verdict."""

from __future__ import annotations

import pytest

from tests.test_scalar_history_recall import (
    _build_svc,
    _postcard_history_view,
    _setup_observation_lane,
    _setup_search_and_metadata,
)

_HISTORY_QUERY = "what was my postcard count before"
_CURRENT_QUERY = "how many postcards do I have"


def _state_view(*, counted: bool) -> dict:
    view = {
        "uuid": "sv-1", "value": 37, "name": "postcards: 37",
        "view_key": "vk-1", "attribute": "postcard_count",
        "subject_uuid": "ent-postcards", "namespace": "proj",
        "valid_at": "2026-07-01T00:00:00+00:00",
        "recall_eligible": True,
    }
    if counted:
        view["scalar_basis"] = "counted"
    return view


async def _recall(stub_graphiti_client, stub_memory_graph_adapter, query, *, counted, counting):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(
        stub_memory_graph_adapter,
        history_view=_postcard_history_view(),
        state_view=_state_view(counted=counted),
    )
    svc = _build_svc(
        stub_graphiti_client, stub_memory_graph_adapter,
        scalar_view_authority_enabled=True, scalar_history_enabled=True,
        counting_enabled=counting,
    )
    return await svc.recall(query, namespace="proj")


def _history_rows(res):
    results = [r for r in res.results if r.view_kind == "scalar_history"]
    verdicts = [v for v in (res.authority_layer or ()) if v.kind == "history"]
    return results, verdicts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_query_skips_counted_slot_when_counting_on(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        _HISTORY_QUERY, counted=True, counting=True)
    results, verdicts = _history_rows(res)
    assert results == []
    assert verdicts == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_query_counted_slot_counting_off_keeps_history(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        _HISTORY_QUERY, counted=True, counting=False)
    results, verdicts = _history_rows(res)
    assert len(results) == 1
    assert len(verdicts) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_query_keeps_history_for_non_counted_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        _HISTORY_QUERY, counted=False, counting=True)
    results, _verdicts = _history_rows(res)
    assert len(results) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_current_query_skips_counted_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        _CURRENT_QUERY, counted=True, counting=True)
    results, verdicts = _history_rows(res)
    assert results == []
    assert verdicts == []
