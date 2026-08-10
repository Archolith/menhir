"""Scalar history recall lane (Slice 3).

Tests the advisory scalar_history lane in recall_pipeline.py:
- Generic exclusion of scalar_history Views when feature flag is OFF
- Dedicated history lane activation for history/comparison queries
- Bounded support: history surfaces when scalar_state has no current View
- History remains below authoritative scalar_state when both exist
- Advisory rendering: delta-only slots labeled "not an absolute current total"
- Authority layer includes history verdict as advisory
- History lane failures never break recall

Uses the same stub pattern as test_recall_service.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from menhir.domain.recall import QueryPreset, RecallResult
from menhir.services.recall_service import RecallService
from menhir.services.recall_policies import _render_scalar_history_content
from menhir.services.scoring_service import ScoringService


# ---- helpers -----------------------------------------------------------------

def _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter):
    """Baseline search results and metadata — the standard two-entity pool."""
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Test Entity", 0.85),
        ("entity-2", "Another Entity", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Test Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "test content", "summary": None,
            "last_accessed": datetime.now(timezone.utc), "edge_count": 5,
            "freshness": "ACTIVE", "user_flagged": False,
        },
        {
            "uuid": "entity-2", "name": "Another Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "other content", "summary": None,
            "last_accessed": datetime.now(timezone.utc), "edge_count": 3,
            "freshness": "ACTIVE", "user_flagged": False,
        },
    ]


def _build_svc(stub_graphiti_client, stub_memory_graph_adapter, **kwargs):
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        **kwargs,
    )


def _postcard_observation_hit():
    """Delta-only postcard observation — the motivating case."""
    return [{
        "assertion_id": "obs-postcards-1", "stated_span": "17 postcards since I started",
        "cosine": 0.95, "subject_uuid": "ent-postcards", "subject_display": "user",
        "attribute": "postcard_count", "scope": "collection", "value_kind": "count",
        "unit": "postcards", "namespace": "proj", "evidence_tier": "user",
        "operation": "delta", "value": "17", "valid_at": "2023-08-11T00:00:00+00:00",
    }]


def _postcard_history_view():
    """Parsed scalar_history View — two delta entries, no absolute."""
    return {
        "uuid": "history-view-postcards",
        "subject": "user",
        "subject_uuid": "ent-postcards",
        "attribute": "postcard_count",
        "scope": "collection",
        "value_kind": "count",
        "unit": "postcards",
        "entry_count": 2,
        "history_signature": "sig123",
        "operation_counts": {"delta": 2},
        "first_valid_at": "2023-08-11T00:00:00+00:00",
        "last_valid_at": "2023-11-30T00:00:00+00:00",
        "entries": [
            {
                "assertion_id": "a1", "operation": "delta", "value": 17,
                "valid_at": "2023-08-11T00:00:00+00:00", "episode_uuid": "ep-1",
                "evidence_tier": "user", "stated_span": "17 postcards since I started",
            },
            {
                "assertion_id": "a2", "operation": "delta", "value": 25,
                "valid_at": "2023-11-30T00:00:00+00:00", "episode_uuid": "ep-2",
                "evidence_tier": "user", "stated_span": "25 postcards since I started",
            },
        ],
        "valid_at": "2023-11-30T00:00:00+00:00",
    }


def _setup_observation_lane(stub_memory_graph_adapter, *, obs_hit=None, history_view=None,
                            state_view=None):
    """Wire the observation lane + scalar_history lane stubs on the adapter."""
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: obs_hit or _postcard_observation_hit()
    )
    stub_memory_graph_adapter.fetch_scalar_history = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            history_view
    )
    stub_memory_graph_adapter.list_scalar_history_views = (
        lambda *, subject_uuid, namespace: [history_view] if history_view else []
    )
    stub_memory_graph_adapter.list_scalar_history_views_for_namespace = (
        lambda *, namespace, limit: [history_view] if history_view else []
    )
    # No current scalar_state View by default (delta-only → state abstains)
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            state_view
    )
    # Stubs for methods the observation lane may also call
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {},
        current_expiries=lambda subj, *, namespace, as_of: {},
    )


# ---- generic exclusion (flag OFF) -------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_history_view_excluded_from_generic_recall_when_flag_off(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """A scalar_history View that won vector search is excluded when the flag is OFF."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    # Inject a scalar_history View into the normal candidate pool via metadata
    stub_graphiti_client.search_scored_results.append(
        ("history-view-1", "history of postcards", 0.9))
    stub_memory_graph_adapter.candidate_metadata.append({
        "uuid": "history-view-1", "name": "history of postcards", "scope": "PERSISTENT",
        "type": "SEMANTIC", "content": "history content", "summary": None,
        "last_accessed": datetime.now(timezone.utc), "edge_count": 1,
        "freshness": "ACTIVE", "user_flagged": False,
        "view_kind": "scalar_history", "view_current": True,
    })
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_history_enabled=False)
    # namespace=None: the generic exclusion check does not require namespace
    res = await svc.recall("postcards")
    # The history View must be excluded
    assert "history-view-1" not in [r.uuid for r in res.results]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_history_view_not_excluded_when_flag_on(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """A scalar_history View that won vector search passes through when the flag is ON."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_graphiti_client.search_scored_results.append(
        ("history-view-1", "history of postcards", 0.9))
    stub_memory_graph_adapter.candidate_metadata.append({
        "uuid": "history-view-1", "name": "history of postcards", "scope": "PERSISTENT",
        "type": "SEMANTIC", "content": "history content", "summary": None,
        "last_accessed": datetime.now(timezone.utc), "edge_count": 1,
        "freshness": "ACTIVE", "user_flagged": False,
        "view_kind": "scalar_history", "view_current": True,
    })
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_history_enabled=True)
    # namespace=None: the generic exclusion check does not require namespace
    res = await svc.recall("postcards")
    assert "history-view-1" in [r.uuid for r in res.results]


# ---- dedicated history lane (flag ON) ----------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_injects_advisory_for_delta_only_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """Delta-only postcard slot: no scalar_state, so the history lane surfaces the
    advisory history View as bounded support."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=None)  # no current scalar_state
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    history_results = [r for r in res.results if r.view_kind == "scalar_history"]
    assert len(history_results) == 1
    h = history_results[0]
    assert h.is_scalar_authority is False
    assert "advisory" in (h.content or "").lower()
    assert "not an absolute current total" in (h.content or "").lower()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_enabled", "authority_enabled", "expected_history"),
    [(False, False, False), (False, True, False), (True, False, True), (True, True, True)],
)
async def test_history_and_authority_flags_are_independent(
    stub_graphiti_client, stub_memory_graph_adapter,
    history_enabled, authority_enabled, expected_history,
) -> None:
    """The advisory lane works with history ON and authority OFF, without becoming authority."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(
        stub_memory_graph_adapter, history_view=_postcard_history_view(), state_view=None,
    )
    svc = _build_svc(
        stub_graphiti_client, stub_memory_graph_adapter,
        scalar_view_authority_enabled=authority_enabled,
        scalar_history_enabled=history_enabled,
    )
    res = await svc.recall("how many postcards do I have", namespace="proj")
    histories = [r for r in res.results if r.view_kind == "scalar_history"]
    assert bool(histories) is expected_history
    for history in histories:
        assert history.is_scalar_authority is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_only_discovery_is_query_matched_not_namespace_enumerated(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """Authority-off history recall must not inject unrelated same-namespace slots."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    postcard_view = _postcard_history_view()
    coins_view = dict(postcard_view)
    coins_view.update({
        "uuid": "history-view-coins", "subject_uuid": "ent-coins",
        "attribute": "coin_count", "unit": "coins", "entry_count": 1,
        "operation_counts": {"absolute": 1},
        "first_valid_at": "2024-01-01T00:00:00+00:00",
        "last_valid_at": "2024-01-01T00:00:00+00:00",
        "entries": [{
            "assertion_id": "coin-a1", "operation": "absolute", "value": 30,
            "valid_at": "2024-01-01T00:00:00+00:00", "episode_uuid": "coin-ep-1",
            "evidence_tier": "user", "stated_span": "I have 30 coins",
        }],
    })
    _setup_observation_lane(
        stub_memory_graph_adapter,
        obs_hit=_postcard_observation_hit(), history_view=None, state_view=None,
    )
    search_calls: list[dict] = []
    malformed_coin_hit = dict(_postcard_observation_hit()[0])
    malformed_coin_hit.update({
        "assertion_id": "", "stated_span": "", "cosine": float("nan"),
        "subject_uuid": "ent-coins", "attribute": "coin_count", "unit": "coins",
    })
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: (
            search_calls.append({"limit": limit, "namespaces": namespaces}),
            [malformed_coin_hit, *_postcard_observation_hit()],
        )[1]
    )
    history_by_slot = {
        ("ent-postcards", "postcard_count", "collection", "count", "postcards"): postcard_view,
        ("ent-coins", "coin_count", "collection", "count", "coins"): coins_view,
    }

    def _fetch_history(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        return history_by_slot.get((subject_uuid, attribute, scope, value_kind, unit))

    stub_memory_graph_adapter.fetch_scalar_history = _fetch_history
    stub_memory_graph_adapter.list_scalar_history_views_for_namespace = (
        lambda **kwargs: pytest.fail("recall must not enumerate namespace history")
    )
    svc = _build_svc(
        stub_graphiti_client, stub_memory_graph_adapter,
        scalar_view_authority_enabled=False, scalar_history_enabled=True,
    )
    res = await svc.recall("how many postcards do I have", namespace="proj")

    assert search_calls == [{"limit": 10, "namespaces": ["proj"]}]
    result_ids = {r.uuid for r in res.results}
    assert "history-view-postcards" in result_ids
    assert "history-view-coins" not in result_ids
    assert "obs-postcards-1" not in result_ids
    assert all(
        r.view_kind != "scalar_history" or r.uuid == "history-view-postcards"
        for r in res.results
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_skips_when_scalar_state_exists_on_current_query(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """When scalar_state has a current View for the slot AND the query is current-state,
    the history lane does not inject (the authority leads)."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _state_view = {
        "uuid": "sv-1", "value": 37, "name": "postcards: 37",
        "view_key": "vk-1", "attribute": "postcard_count",
        "subject_uuid": "ent-postcards", "namespace": "proj",
        "valid_at": "2026-07-01T00:00:00+00:00",
    }
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=_state_view)
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    history_results = [r for r in res.results if r.view_kind == "scalar_history"]
    assert len(history_results) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_surfaces_for_history_query_even_when_state_exists(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """A PREVIOUS_VALUE query surfaces scalar_history even when scalar_state exists."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _state_view = {
        "uuid": "sv-1", "value": 37, "name": "postcards: 37",
        "view_key": "vk-1", "attribute": "postcard_count",
        "subject_uuid": "ent-postcards", "namespace": "proj",
        "valid_at": "2026-07-01T00:00:00+00:00",
    }
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=_state_view)
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    # "what was ... before" classifies as PREVIOUS_VALUE → history lane fires
    res = await svc.recall("what was my postcard count before", namespace="proj")
    history_results = [r for r in res.results if r.view_kind == "scalar_history"]
    assert len(history_results) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_never_marks_authority(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """scalar_history candidates are always is_scalar_authority=False."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=None)
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    for r in res.results:
        if r.view_kind == "scalar_history":
            assert r.is_scalar_authority is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_adds_advisory_verdict_to_authority_layer(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """The authority_layer includes a history verdict with kind='history', status='advisory'."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=None)
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    assert res.authority_layer is not None
    history_verdicts = [v for v in res.authority_layer if v.kind == "history"]
    assert len(history_verdicts) == 1
    v = history_verdicts[0]
    assert v.status == "advisory"
    assert v.has_foundation is False
    assert v.attribute == "postcard_count"
    assert len(v.contributors) == 2  # two delta entries
    assert v.contributors[0].relation == "HISTORY_ENTRY"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_no_injection_when_flag_off(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """When scalar_history_enabled is False, the dedicated lane does not run at all."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    fetch_calls: list[dict] = []

    def _track_fetch(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        fetch_calls.append({"attribute": attribute})
        return _postcard_history_view()

    _setup_observation_lane(stub_memory_graph_adapter, state_view=None)
    stub_memory_graph_adapter.fetch_scalar_history = _track_fetch
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=False)
    await svc.recall("how many postcards do I have", namespace="proj")
    assert fetch_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_failure_does_not_break_recall(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """If the history lane raises, recall still returns results."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter, state_view=None)

    def _explode(**kwargs):
        raise RuntimeError("history boom")

    stub_memory_graph_adapter.fetch_scalar_history = _explode
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    # Recall still succeeds
    assert isinstance(res, RecallResult)
    assert len(res.results) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_no_duplicate_when_view_already_ranked(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    """If the history View already won vector search, the lane does not inject a duplicate."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    # History View already in the candidate pool from vector search
    stub_graphiti_client.search_scored_results.append(
        ("history-view-postcards", "history of postcards", 0.9))
    stub_memory_graph_adapter.candidate_metadata.append({
        "uuid": "history-view-postcards", "name": "history of postcards",
        "scope": "PERSISTENT", "type": "SEMANTIC", "content": "history content",
        "summary": None, "last_accessed": datetime.now(timezone.utc),
        "edge_count": 1, "freshness": "ACTIVE", "user_flagged": False,
        "view_kind": "scalar_history", "view_current": True,
    })
    _setup_observation_lane(stub_memory_graph_adapter,
                           history_view=_postcard_history_view(),
                           state_view=None)
    svc = _build_svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_authority_enabled=True, scalar_history_enabled=True)
    res = await svc.recall("how many postcards do I have", namespace="proj")
    # Only ONE instance of the history View in results
    history_ids = [r.uuid for r in res.results if r.uuid == "history-view-postcards"]
    assert len(history_ids) <= 1


# ---- rendering tests ---------------------------------------------------------

@pytest.mark.unit
def test_render_delta_only_history():
    """Delta-only history renders with advisory disclaimer."""
    content = _render_scalar_history_content(_postcard_history_view())
    assert "advisory" in content.lower()
    assert "not an absolute current total" in content.lower()
    assert "2023-08-11" in content
    assert "2023-11-30" in content
    assert "+17" in content  # delta prefix
    assert "+25" in content
    assert "Latest: 25" in content
    # Must NOT contain 42 (sum of deltas)
    assert "42" not in content


@pytest.mark.unit
def test_render_mixed_operations_no_absolute_disclaimer():
    """Mixed operations (absolute + delta) do not get the 'not absolute' label."""
    view = dict(_postcard_history_view())
    view["operation_counts"] = {"absolute": 1, "delta": 1}
    view["entries"] = [
        {"assertion_id": "a1", "operation": "absolute", "value": 20,
         "valid_at": "2023-01-01T00:00:00+00:00", "stated_span": "I had 20"},
        {"assertion_id": "a2", "operation": "delta", "value": 5,
         "valid_at": "2023-06-01T00:00:00+00:00", "stated_span": "5 more"},
    ]
    content = _render_scalar_history_content(view)
    assert "advisory" in content.lower()
    # Should NOT have "not an absolute current total" because there's an absolute
    assert "not an absolute current total" not in content.lower()


@pytest.mark.unit
def test_render_empty_entries():
    """History with no entries renders just the header."""
    view = dict(_postcard_history_view())
    view["entries"] = []
    view["entry_count"] = 0
    content = _render_scalar_history_content(view)
    assert "advisory" in content.lower()
    assert "Latest" not in content


@pytest.mark.unit
def test_render_uses_source_time_not_ingest():
    """Entries display valid_at (source/world time), not recorded/ingest time."""
    content = _render_scalar_history_content(_postcard_history_view())
    # valid_at dates are present
    assert "2023-08-11" in content
    assert "2023-11-30" in content
    # No reference to any other timestamp


@pytest.mark.unit
def test_render_preserves_stated_span():
    """Each entry renders the original stated_span."""
    content = _render_scalar_history_content(_postcard_history_view())
    assert "17 postcards since I started" in content
    assert "25 postcards since I started" in content
