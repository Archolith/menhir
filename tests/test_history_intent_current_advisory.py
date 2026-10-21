"""A history question never lets the current scalar View lead (#170).

"How many coins did I have before?" is not answered by the current value. The current View is
still shown, but as advisory, so an agent reading the verdict does not take today's number as
the answer to a question about the past. Current-state and bare questions keep leading.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.domain.scalar_view_authority import QueryIntent
from menhir.domain.scalar_view_suppression import authority_query_intent
from tests.test_recall_service import (
    _build_recall_service,
    _one_observation_hit,
    _setup_search_and_metadata,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

VIEW_NAME = "user's owned: 37. current owned = 37."


def _wire(client, adapter):
    _setup_search_and_metadata(client, adapter)
    adapter.search_assertion_embeddings = lambda vec, *, limit, namespaces: _one_observation_hit()
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: {
            "uuid": "view-owned-current", "value": 37, "name": VIEW_NAME, "view_key": "vk-owned",
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00", "recall_eligible": True,
        })
    # A foundation tier, so the View would lead on a current-state question.
    adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "user"})


async def _current(client, adapter, query):
    _wire(client, adapter)
    svc = _build_recall_service(client, adapter, scalar_view_authority_enabled=True)
    res = await svc.recall(query, namespace="proj")
    verdicts = [v for v in res.authority_layer or [] if v.kind == "current"]
    assert len(verdicts) == 1, "the current View must still be shown"
    item = {r.name: r for r in res.results}[VIEW_NAME]
    return verdicts[0], item


@pytest.mark.parametrize("query", [
    "how many rare coins did I have before",
    "how many rare coins did I used to have",
])
async def test_history_question_keeps_current_view_advisory(
    stub_graphiti_client, stub_memory_graph_adapter, query,
):
    assert authority_query_intent(query) is QueryIntent.PREVIOUS_VALUE
    verdict, item = await _current(stub_graphiti_client, stub_memory_graph_adapter, query)
    assert verdict.status == "advisory"
    assert verdict.value == 37 and verdict.has_foundation is True
    assert item.is_scalar_authority is False


@pytest.mark.parametrize(("query", "intent"), [
    ("how many rare coins now", QueryIntent.CURRENT_STATE),
    ("how many rare coins do I have", QueryIntent.AMBIGUOUS),
])
async def test_current_and_bare_questions_still_lead(
    stub_graphiti_client, stub_memory_graph_adapter, query, intent,
):
    assert authority_query_intent(query) is intent
    verdict, item = await _current(stub_graphiti_client, stub_memory_graph_adapter, query)
    assert verdict.status == "leads"
    assert item.is_scalar_authority is True
