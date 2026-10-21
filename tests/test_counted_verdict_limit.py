"""counted_verdict_limit: a counted occurrence is dropped only for a verdict the client renders."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.api.routes_support import RecallRequest
from menhir.core.backend_client_ops import BackendClientOpsMixin
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from tests.test_counting_recall_authority_only import R_ON, _occ_hit
from tests.test_scalar_history_recall import _setup_observation_lane, _setup_search_and_metadata
from tests.test_scalar_view_collapse_drilldown import QUERY, _state_view, _svc

# Distinct cosines: ranking order is e (0.95) > b (0.90) > d (0.85) > c (0.80) > a (0.75).
COUNTED = {"attr_a": 0.75, "attr_b": 0.90, "attr_c": 0.80, "attr_d": 0.85, "attr_e": 0.95}
BY_COSINE = sorted(COUNTED, key=lambda a: -COUNTED[a])


async def _recall(client, adapter, *, stated=None, limit=None, **flags):
    """Recall with one counted slot per COUNTED attr (plus optional stated slots {attr: cosine})."""
    stated = stated or {}
    hits = [_occ_hit(f"occ-{a}", c, f"counted {a}", attribute=a, unit=a)
            for a, c in COUNTED.items()]
    hits += [_occ_hit(f"occ-{a}", c, f"stated {a}", attribute=a, unit=a)
             for a, c in stated.items()]
    views = {a: {**_state_view("counted"), "uuid": f"sv-{a}", "attribute": a,
                 "view_key": f"vk-{a}", "unit": a} for a in COUNTED}
    views.update({a: {**_state_view(), "uuid": f"sv-{a}", "attribute": a,
                      "view_key": f"vk-{a}", "unit": a} for a in stated})
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"
    _setup_observation_lane(adapter, obs_hit=hits)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: views.get(attribute))
    kw = {} if limit is None else {"counted_verdict_limit": limit}
    return await _svc(client, adapter, **flags).recall(QUERY, namespace="proj", limit=10, **kw)


def _counted_attrs(res):
    return [v.attribute for v in (res.authority_layer or ())
            if v.kind == "current" and v.basis == "counted"]


def _assert_evidence_invariant(res):
    """Every counted slot has its counted verdict or, failing that, its occurrence in the result."""
    ids = {r.uuid for r in res.results}
    verdicts = set(_counted_attrs(res))
    for attr in COUNTED:
        assert attr in verdicts or f"occ-{attr}" in ids, attr


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_caps_verdicts_and_keeps_evidence_past_the_cap(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, limit=3, **R_ON)
    ids = {r.uuid for r in res.results}
    assert _counted_attrs(res) == BY_COSINE[:3]
    for attr in BY_COSINE[:3]:
        assert f"occ-{attr}" not in ids
    for attr in BY_COSINE[3:]:
        assert f"occ-{attr}" in ids
    for attr in COUNTED:
        assert f"sv-{attr}" not in ids  # a counted View is never ranked
    _assert_evidence_invariant(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_none_is_unchanged(stub_graphiti_client, stub_memory_graph_adapter):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, **R_ON)
    ids = {r.uuid for r in res.results}
    assert _counted_attrs(res) == BY_COSINE
    assert not any(f"occ-{a}" in ids for a in COUNTED)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_zero_emits_no_counted_verdicts_and_keeps_all_evidence(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, limit=0, **R_ON)
    ids = {r.uuid for r in res.results}
    assert _counted_attrs(res) == []
    assert all(f"occ-{a}" in ids for a in COUNTED)
    _assert_evidence_invariant(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stated_verdict_neither_blocked_nor_counted(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # The stated slot outranks every counted slot; limit=1 must still yield its verdict plus
    # exactly one counted verdict (the best counted slot).
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        stated={"attr_s": 0.99}, limit=1, **R_ON)
    ids = {r.uuid for r in res.results}
    current = [v.attribute for v in res.authority_layer if v.kind == "current"]
    assert "attr_s" in current
    assert _counted_attrs(res) == BY_COSINE[:1]
    for attr in BY_COSINE[1:]:
        assert f"occ-{attr}" in ids
    _assert_evidence_invariant(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_limit_has_no_effect(stub_graphiti_client, stub_memory_graph_adapter):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, limit=1,
                        counting_enabled=True)
    assert len(_counted_attrs(res)) == len(COUNTED)


# --------------------------------------------------------------------------- API / backend seam

@pytest.mark.unit
def test_recall_request_counted_verdict_limit_bounds():
    assert RecallRequest(query="q").counted_verdict_limit is None
    assert RecallRequest(query="q", counted_verdict_limit=0).counted_verdict_limit == 0
    assert RecallRequest(query="q", counted_verdict_limit=50).counted_verdict_limit == 50
    with pytest.raises(ValidationError):
        RecallRequest(query="q", counted_verdict_limit=-1)
    with pytest.raises(ValidationError):
        RecallRequest(query="q", counted_verdict_limit=51)


@pytest.mark.unit
def test_route_passes_counted_verdict_limit_only_when_set():
    backend = SimpleNamespace(recall=AsyncMock(return_value={
        "query": "q", "preset": "knowledge", "results": [], "candidates_evaluated": 0}))
    app = FastAPI()
    app.include_router(router)
    with patch.object(api_routes, "_get_backend", return_value=backend):
        http = TestClient(app)
        assert http.post("/api/recall", json={"query": "q"}).status_code == 200
        assert "counted_verdict_limit" not in backend.recall.call_args.kwargs
        resp = http.post("/api/recall", json={"query": "q", "counted_verdict_limit": 3})
        assert resp.status_code == 200
        assert backend.recall.call_args.kwargs["counted_verdict_limit"] == 3
        assert http.post("/api/recall",
                         json={"query": "q", "counted_verdict_limit": 51}).status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_ops_forward_counted_verdict_limit_only_when_set():
    recall = AsyncMock(return_value={"query": "q", "results": []})
    ops = object.__new__(RuntimeProviderDataOpsMixin)
    ops.built = SimpleNamespace(settings=SimpleNamespace(),
                                recall_service=SimpleNamespace(recall=recall))
    ops._effective_session_id = lambda: None
    await ops.recall("q")
    assert "counted_verdict_limit" not in recall.call_args.kwargs
    await ops.recall("q", counted_verdict_limit=3)
    assert recall.call_args.kwargs["counted_verdict_limit"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_ops_send_counted_verdict_limit_only_when_set():
    ops = object.__new__(BackendClientOpsMixin)
    ops._request = AsyncMock(return_value={})
    await ops.recall("q")
    assert "counted_verdict_limit" not in ops._request.call_args.args[1]
    await ops.recall("q", counted_verdict_limit=3)
    assert ops._request.call_args.args[1]["counted_verdict_limit"] == 3
