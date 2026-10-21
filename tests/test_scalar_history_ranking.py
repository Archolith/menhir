"""Scalar-history advisory rows rank by relevance, not a fixed prior (#243).

LME b10 a3838d2b / gpt4_2312f94c: single-value slots at a fixed 0.85 similarity plus a
just-accessed recency bonus took ranks 2-6 on event-ordering questions and pushed the answer
facts out of the top 10.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.services.recall_policies import _scalar_history_records_change
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

_CHARITY_QUERY = "How many charity events did I participate in before the 'Run for the Cure' event?"
_USER = "ent-user"


def _entry(value, *, operation="absolute", valid_at="2023-11-29T11:35:00Z", aid="a"):
    return {
        "assertion_id": aid, "operation": operation, "value": value, "valid_at": valid_at,
        "episode_uuid": "ep", "evidence_tier": "agent", "stated_span": str(value),
    }


def _view(uuid, attribute, entries, *, last_valid_at="2023-11-29T11:35:00Z", omitted=0):
    return {
        "uuid": uuid, "subject": "user", "subject_uuid": _USER, "attribute": attribute,
        "scope": "", "value_kind": "status", "unit": "", "entry_count": len(entries) + omitted,
        "payload_entry_count": len(entries), "omitted_entry_count": omitted,
        "first_valid_at": entries[0]["valid_at"] if entries else None,
        "last_valid_at": last_valid_at, "recall_eligible": True, "entries": entries,
    }


def _hit(aid, attribute, cosine):
    return {
        "assertion_id": aid, "stated_span": f"span {aid}", "cosine": cosine,
        "subject_uuid": _USER, "subject_display": "user", "attribute": attribute, "scope": "",
        "value_kind": "status", "unit": "", "namespace": "proj", "evidence_tier": "agent",
        "operation": "absolute", "value": "x", "valid_at": "2023-11-29T11:35:00Z",
    }


def _wire(graphiti, adapter, *, hits, views):
    graphiti.search_scored_results = [
        ("entity-1", "Run for the Cure", 0.85),
        ("entity-2", "Bike-a-Thon", 0.65),
    ]
    now = datetime.now(timezone.utc)
    adapter.candidate_metadata = [
        {"uuid": "entity-1", "name": "Run for the Cure", "scope": "PERSISTENT", "type": "SEMANTIC",
         "content": "ran 5 km in Run for the Cure", "summary": None, "last_accessed": now,
         "edge_count": 5, "freshness": "ACTIVE", "user_flagged": False, "namespace": "proj"},
        {"uuid": "entity-2", "name": "Bike-a-Thon", "scope": "PERSISTENT", "type": "SEMANTIC",
         "content": "did the Bike-a-Thon in November", "summary": None, "last_accessed": now,
         "edge_count": 3, "freshness": "ACTIVE", "user_flagged": False, "namespace": "proj"},
    ]
    adapter.search_assertion_embeddings = lambda vec, *, limit, namespaces: hits
    adapter.fetch_scalar_history = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: views.get(attribute)
    )
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: None
    )
    adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {},
        current_expiries=lambda subj, *, namespace, as_of: {},
    )


async def _recall(graphiti, adapter, query):
    svc = RecallService(
        graphiti_client=graphiti, graph_adapter=adapter, scoring_service=ScoringService(),
        scalar_view_authority_enabled=False, scalar_history_enabled=True,
    )
    return await svc.recall(query, namespace="proj")


def _histories(result):
    return [r for r in result.results if r.view_kind == "scalar_history"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entries", "omitted", "expected"),
    [
        ([], 0, False),
        ([_entry("weekends")], 0, False),
        ([_entry("true"), _entry("True ")], 0, False),
        ([_entry("not experienced"), _entry("not as experienced")], 0, True),
        ([_entry(17, operation="delta")], 0, True),
        ([_entry("weekends")], 3, True),
    ],
)
def test_records_change(entries, omitted, expected) -> None:
    assert _scalar_history_records_change(_view("v", "attr", entries, omitted=omitted)) is expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_value_slots_are_not_injected(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    # a3838d2b / gpt4_2312f94c shape: one restated value is not history.
    views = {
        "volunteer_travel_limit": _view("h-travel", "volunteer_travel_limit", [_entry("1800")]),
        "owned": _view("h-owned", "owned", [_entry("true"), _entry("true")]),
        "cycling_experience": _view(
            "h-cycling", "cycling_experience",
            [_entry("not experienced"), _entry("not as experienced as I'd like")],
        ),
    }
    hits = [_hit("o1", "volunteer_travel_limit", 0.5), _hit("o2", "owned", 0.5),
            _hit("o3", "cycling_experience", 0.5)]
    _wire(stub_graphiti_client, stub_memory_graph_adapter, hits=hits, views=views)

    result = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _CHARITY_QUERY)

    assert [r.uuid for r in _histories(result)] == ["h-cycling"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_row_scores_by_best_slot_cosine_and_ranks_below_matched_facts(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    views = {"interest": _view("h-interest", "interest",
                               [_entry("food banks"), _entry("arts programs")])}
    hits = [_hit("o1", "interest", 0.32), _hit("o2", "interest", 0.41)]
    _wire(stub_graphiti_client, stub_memory_graph_adapter, hits=hits, views=views)

    result = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _CHARITY_QUERY)

    [history] = _histories(result)
    assert history.retrieval_score == pytest.approx(0.41)
    assert history.breakdown.semantic_similarity == pytest.approx(0.41)
    # Old evidence earns no recency bonus (it used to get the full just-accessed bonus).
    assert history.breakdown.recency_bonus < 0.01
    order = [r.uuid for r in result.results]
    assert order.index("entity-1") < order.index("h-interest")
    assert order.index("entity-2") < order.index("h-interest")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weak_slot_match_falls_below_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    views = {"interest": _view("h-interest", "interest",
                               [_entry("food banks"), _entry("arts programs")])}
    _wire(stub_graphiti_client, stub_memory_graph_adapter,
          hits=[_hit("o1", "interest", 0.1)], views=views)

    result = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _CHARITY_QUERY)

    assert _histories(result) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_history_query_still_surfaces_with_recent_evidence_recency(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    views = {"car_mileage": _view(
        "h-mileage", "car_mileage",
        [_entry("10000", valid_at="2024-01-01T00:00:00Z"), _entry("15000", valid_at=now)],
        last_valid_at=now,
    )}
    _wire(stub_graphiti_client, stub_memory_graph_adapter,
          hits=[_hit("o1", "car_mileage", 0.62)], views=views)

    result = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter, "what was my car mileage before")

    [history] = _histories(result)
    assert history.uuid == "h-mileage"
    assert history.retrieval_score == pytest.approx(0.62)
    assert history.breakdown.recency_bonus > 0.99
    assert history.is_scalar_authority is False
