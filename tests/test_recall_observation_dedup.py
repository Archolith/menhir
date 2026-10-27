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


def _hit(aid, span, cosine, operation="absolute", valid_at="2023-08-11T00:00:00+00:00",
         **slot):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id=aid, stated_span=span, cosine=cosine, operation=operation,
               valid_at=valid_at, **slot)
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
async def test_dedup_never_merges_delta_rows(stub_graphiti_client, stub_memory_graph_adapter):
    # Two identical "bought 2 more" deltas are two increments; one copy would undercount.
    hits = [_hit("dl-a", "bought 2 more postcards", 0.80, operation="delta"),
            _hit("dl-b", "bought 2 more postcards", 0.90, operation="delta")]
    ids = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, dedup=True)
    assert {"dl-a", "dl-b"} <= ids


@pytest.mark.unit
def test_identical_text_in_different_slots_is_not_deduped():
    from menhir.services.recall_pipeline import _observation_dedup_skip_ids

    span = "17 postcards since I started"
    assert _observation_dedup_skip_ids([
        _hit("a", span, 0.9), _hit("b", span, 0.8, attribute="stamp_count")]) == set()
    assert _observation_dedup_skip_ids([
        _hit("a", span, 0.9), _hit("b", span, 0.8, subject_uuid="ent-other")]) == set()
    assert _observation_dedup_skip_ids([
        _hit("a", span, 0.9), _hit("b", span, 0.8, unit="cards")]) == set()


def _car_hit(aid, subj, disp, cos):
    return _hit(aid, "my dad and my brother each have two cars", cos, subject_uuid=subj,
                subject_display=disp, attribute="car_count", scope="", unit="cars", value="2")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_keeps_named_subject_for_authority_lookup(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    _setup_observation_lane(stub_memory_graph_adapter, obs_hit=[
        _car_hit("obs-dad", "ent-dad", "dad", 0.91),
        _car_hit("obs-bro", "ent-bro", "brother", 0.90)])
    looked_up = []

    def _view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        looked_up.append(subject_uuid)
        return None

    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = _view
    svc = RecallService(graphiti_client=stub_graphiti_client,
                        graph_adapter=stub_memory_graph_adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True)
    await svc.recall("how many cars does my brother have", namespace="proj",
                     tuning=RetrievalTuningConfig(enable_observation_dedup=True))
    assert "ent-bro" in looked_up


@pytest.mark.unit
@pytest.mark.asyncio
async def test_echo_dropped_slot_does_not_take_other_slots_identical_text(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    from tests.test_scalar_view_collapse_drilldown import (
        _ids, _other_slot_hit, _state_view, _svc, _wire)

    other = _other_slot_hit()
    other.update(stated_span="17 postcards since I started", cosine=0.90)
    _wire(stub_memory_graph_adapter, state_view=_state_view(), extra_hits=[other])
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter,
                     scalar_view_echo_drop_enabled=True).recall(
        QUERY, namespace="proj", tuning=RetrievalTuningConfig(enable_observation_dedup=True))
    assert "obs-other-1" in _ids(res)


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
def test_valid_at_tiebreak_compares_instants_not_strings():
    from menhir.services.recall_pipeline import _observation_dedup_skip_ids

    # 10:00+05:00 is 05:00Z, earlier than 06:00Z even though it sorts later as text.
    hits = [_hit("early", "same", 0.9, valid_at="2024-01-01T10:00:00+05:00"),
            _hit("late", "same", 0.9, valid_at="2024-01-01T06:00:00Z"),
            _hit("bad", "same", 0.9, valid_at="not a date")]
    assert _observation_dedup_skip_ids(hits) == {"early", "bad"}


@pytest.mark.unit
def test_dedup_flag_registered_default_off_and_needs_authority(monkeypatch):
    feature = FEATURES["personal_memory_observation_dedup_enabled"]
    assert feature.env_var == "MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED"
    assert feature.default is False
    assert "personal_memory_scalar_state_enabled" in feature.requires
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED", "1")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_observation_dedup_enabled is True
    assert settings.retrieval_tuning().enable_observation_dedup is True
    assert RetrievalTuningConfig().enable_observation_dedup is False
