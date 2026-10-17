"""Scalar verdict relevance gate (MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY), offline.

The authority and history lanes turn the 10 nearest assertions into verdicts with no similarity
floor; the gate drops a slot whose best query cosine is below the setting, in both lanes. The
observation itself stays in recall.
"""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import (
    _postcard_history_view,
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import QUERY, _ids, _state_view


def _hit(cosine, **kw):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(cosine=cosine, **kw)
    return hit


async def _recall(client, adapter, hits, min_sim):
    _setup_observation_lane(adapter, obs_hit=hits, history_view=None, state_view=_state_view())
    _setup_search_and_metadata(client, adapter)
    svc = RecallService(graphiti_client=client, graph_adapter=adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True)
    return await svc.recall(QUERY, namespace="proj",
                            tuning=RetrievalTuningConfig(scalar_verdict_min_similarity=min_sim))


def _verdict_attrs(res):
    return [v.attribute for v in (res.authority_layer or ())]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_keeps_low_similarity_verdict(stub_graphiti_client, stub_memory_graph_adapter):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.2)], 0.0)
    assert _verdict_attrs(res) == ["postcard_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_drops_verdict_below_floor_but_keeps_observation(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.2)], 0.35)
    assert _verdict_attrs(res) == []
    assert "obs-postcards-1" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cosine", [0.35, 0.5])
async def test_gate_keeps_verdict_at_or_above_floor(
    stub_graphiti_client, stub_memory_graph_adapter, cosine,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(cosine)], 0.35)
    assert _verdict_attrs(res) == ["postcard_count"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["descending", "ascending"])
async def test_gate_uses_best_cosine_of_the_slot(stub_graphiti_client, stub_memory_graph_adapter, order):
    # Production hits arrive sorted by cosine descending; both orders must keep the best.
    hits = [_hit(0.5, assertion_id="obs-postcards-2", stated_span="20 postcards now"), _hit(0.2)]
    if order == "ascending":
        hits.reverse()
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, 0.35)
    assert _verdict_attrs(res) == ["postcard_count"]


async def _history_recall(client, adapter, cosine, min_sim):
    _setup_observation_lane(adapter, obs_hit=[_hit(cosine)], history_view=_postcard_history_view(),
                            state_view=None)
    _setup_search_and_metadata(client, adapter)
    svc = RecallService(graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService(),
                        scalar_view_authority_enabled=True, scalar_history_enabled=True)
    return await svc.recall("how many postcards do I have", namespace="proj",
                            tuning=RetrievalTuningConfig(scalar_verdict_min_similarity=min_sim))


def _history_kinds(res):
    return [v.kind for v in (res.authority_layer or ()) if v.kind == "history"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_drops_history_verdict_below_floor(stub_graphiti_client, stub_memory_graph_adapter):
    res = await _history_recall(stub_graphiti_client, stub_memory_graph_adapter, 0.2, 0.35)
    assert _history_kinds(res) == []
    assert not [r for r in res.results if r.view_kind == "scalar_history"]
    assert "obs-postcards-1" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("cosine", "min_sim"), [(0.2, 0.0), (0.5, 0.35)])
async def test_history_verdict_kept_when_gate_off_or_passed(
    stub_graphiti_client, stub_memory_graph_adapter, cosine, min_sim,
):
    res = await _history_recall(stub_graphiti_client, stub_memory_graph_adapter, cosine, min_sim)
    assert _history_kinds(res) == ["history"]


@pytest.mark.unit
def test_setting_default_off_and_reaches_tuning(monkeypatch):
    assert MemorySettings().personal_memory_scalar_verdict_min_similarity == 0.0
    assert RetrievalTuningConfig().scalar_verdict_min_similarity == 0.0
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY", "0.35")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_verdict_min_similarity == 0.35
    assert settings.retrieval_tuning().scalar_verdict_min_similarity == 0.35


@pytest.mark.unit
def test_negative_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY", "-0.1")
    with pytest.raises(ValueError):
        MemorySettings.from_env()


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["1.5", "inf", "nan"])
def test_out_of_range_setting_is_rejected(monkeypatch, raw):
    # Above 1 would drop every verdict; NaN would silently turn the gate off.
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY", raw)
    with pytest.raises(ValueError, match="scalar_verdict_min_similarity"):
        MemorySettings.from_env().retrieval_tuning()
