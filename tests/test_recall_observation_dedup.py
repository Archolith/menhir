"""Observation text dedup (MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED), offline."""

from __future__ import annotations

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import (
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)

QUERY = "how many postcards do I have"


def _hit(aid, span, cosine, operation="delta", valid_at="2023-08-11T00:00:00+00:00"):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id=aid, stated_span=span, cosine=cosine, operation=operation,
               valid_at=valid_at)
    return hit


async def _recall(client, adapter, hits, *, dedup, counting=False):
    _setup_search_and_metadata(client, adapter)
    _setup_observation_lane(adapter, obs_hit=hits)
    svc = RecallService(
        graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService(),
        scalar_view_authority_enabled=True, counting_enabled=counting)
    res = await svc.recall(
        QUERY, namespace="proj",
        tuning=RetrievalTuningConfig(enable_observation_dedup=dedup))
    return {r.uuid for r in res.results}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_keeps_only_highest_cosine_of_identical_text(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = [_hit("obs-a", "17 postcards  since I started", 0.80),
            _hit("obs-b", " 17 Postcards since I STARTED ", 0.90)]
    ids = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, dedup=True)
    assert "obs-b" in ids and "obs-a" not in ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_never_merges_occurrence_rows(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = [_hit("occ-a", "went to the post office", 0.80, operation="occurrence"),
            _hit("occ-b", "went to the post office", 0.90, operation="occurrence")]
    ids = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits,
                        dedup=True, counting=True)
    assert {"occ-a", "occ-b"} <= ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_keeps_both(stub_graphiti_client, stub_memory_graph_adapter):
    hits = [_hit("obs-a", "17 postcards since I started", 0.80),
            _hit("obs-b", "17 postcards since I started", 0.90)]
    ids = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, dedup=False)
    assert {"obs-a", "obs-b"} <= ids


@pytest.mark.unit
def test_skip_tiebreak_cosine_then_valid_at_then_id():
    from menhir.services.recall_pipeline import _observation_dedup_skip_ids

    hits = [_hit("c", "same", 0.9, valid_at="2024-01-01"),
            _hit("b", "same", 0.9, valid_at="2024-06-01"),
            _hit("a", "same", 0.9, valid_at="2024-06-01"),
            _hit("z", "same", 0.5, valid_at="2030-01-01")]
    assert _observation_dedup_skip_ids(hits) == {"c", "b", "z"}


@pytest.mark.unit
def test_dedup_flag_registered_default_off_and_needs_authority(monkeypatch):
    feature = FEATURES["personal_memory_observation_dedup_enabled"]
    assert feature.env_var == "MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED"
    assert feature.default is False
    assert "personal_memory_scalar_view_authority_enabled" in feature.requires
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED", "1")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_observation_dedup_enabled is True
    assert settings.retrieval_tuning().enable_observation_dedup is True
    assert RetrievalTuningConfig().enable_observation_dedup is False
