"""Scalar observation recall mode (MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL), offline.

Raw observation quotes entered the ranked list with their cosine as similarity and no floor; on
o500 they displaced memories (#168). "gated" keeps only quotes at or above the verdict gate, "off"
keeps none. The slot is collected in every mode, so verdicts are unchanged.
"""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import (
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import QUERY, _ids, _state_view

OBS = "obs-postcards-1"


def _hit(cosine, **kw):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(cosine=cosine, **kw)
    return hit


async def _recall(client, adapter, hits, mode, min_sim=0.0):
    _setup_observation_lane(adapter, obs_hit=hits, history_view=None, state_view=_state_view())
    _setup_search_and_metadata(client, adapter)
    svc = RecallService(graphiti_client=client, graph_adapter=adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True)
    tuning = RetrievalTuningConfig(scalar_observation_recall=mode,
                                   scalar_verdict_min_similarity=min_sim)
    return await svc.recall(QUERY, namespace="proj", tuning=tuning)


def _verdict_attrs(res):
    return [v.attribute for v in (res.authority_layer or ())]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_mode_keeps_the_observation(stub_graphiti_client, stub_memory_graph_adapter):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.2)], "all", 0.35)
    assert OBS in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_drops_the_observation_but_keeps_the_verdict(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # The verdict passes the G17 subject gate only through the observation's subject display,
    # so this also fails if "off" stops recording displays.
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.95)], "off")
    assert OBS not in _ids(res)
    assert _verdict_attrs(res) == ["postcard_count"]
    assert "sv-1" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gated_mode_drops_observation_below_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.2)], "gated", 0.35)
    assert OBS not in _ids(res)
    assert _verdict_attrs(res) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cosine", [0.35, 0.5])
async def test_gated_mode_keeps_observation_at_or_above_the_floor(
    stub_graphiti_client, stub_memory_graph_adapter, cosine,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(cosine)], "gated", 0.35)
    assert OBS in _ids(res)
    assert _verdict_attrs(res) == ["postcard_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gated_mode_with_gate_off_keeps_every_observation(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [_hit(0.2)], "gated", 0.0)
    assert OBS in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gated_mode_judges_each_observation_not_the_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    strong = _hit(0.5, assertion_id="obs-postcards-2", stated_span="20 postcards now")
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [strong, _hit(0.2)],
                        "gated", 0.35)
    assert "obs-postcards-2" in _ids(res)
    assert OBS not in _ids(res)
    assert _verdict_attrs(res) == ["postcard_count"]


@pytest.mark.unit
def test_setting_default_and_env_reach_tuning(monkeypatch):
    # Shipped default is "gated"; the bare tuning dataclass keeps today's "all", like the other flags.
    assert MemorySettings().personal_memory_scalar_observation_recall == "gated"
    assert RetrievalTuningConfig().scalar_observation_recall == "all"
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL", " Off ")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_observation_recall == "off"
    assert settings.retrieval_tuning().scalar_observation_recall == "off"


@pytest.mark.unit
def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL", "none")
    with pytest.raises(ValueError, match="scalar_observation_recall"):
        MemorySettings.from_env().retrieval_tuning()
