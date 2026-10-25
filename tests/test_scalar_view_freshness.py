"""Freshness gate for scalar verdicts (MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE), offline.

o500 (#168): a View kept leading with a stale value when the newer statement was an occurrence the
fold ignores (painting: "just finished my 5th") or landed in another slot (Mustang -> F-150).
"""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.scalar_view_freshness import ObservationTime, newer_unfolded_evidence
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import (
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import QUERY, _state_view

SLOT = ("ent-postcards", "postcard_count", "collection", "count", "postcards")
VIEW_AT = "2026-07-01T00:00:00+00:00"


def _obs(aid, *, slot=SLOT, op="absolute", at="2026-08-01T00:00:00Z", cos=0.9):
    return ObservationTime(assertion_id=aid, slot=slot, operation=op, valid_at=at, cosine=cos)


# ---- pure gate -----------------------------------------------------------------------------


@pytest.mark.unit
def test_newer_same_slot_occurrence_is_unfolded_evidence():
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o1", op="occurrence")],
                                   min_similarity=0.675) == ("o1",)


@pytest.mark.unit
@pytest.mark.parametrize("op", ["absolute", "delta"])
def test_newer_same_slot_absolute_or_delta_is_folded(op):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o1", op=op)], min_similarity=0.675) == ()


@pytest.mark.unit
def test_newer_other_slot_same_subject_and_kind_above_gate_is_evidence():
    other = ("ent-postcards", "postcards_bought", "collection", "count", "postcards")
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o2", slot=other, cos=0.7)],
                                   min_similarity=0.675) == ("o2",)


@pytest.mark.unit
@pytest.mark.parametrize("slot,cos", [
    (("ent-postcards", "postcards_bought", "collection", "count", "postcards"), 0.6),  # below gate
    (("ent-postcards", "favourite_card", "collection", "text", ""), 0.9),             # other kind
    (("ent-dad", "postcard_count", "collection", "count", "postcards"), 0.9),         # other subject
])
def test_other_slot_needs_same_subject_same_kind_and_the_gate(slot, cos):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o3", slot=slot, cos=cos)],
                                   min_similarity=0.675) == ()


@pytest.mark.unit
@pytest.mark.parametrize("at", ["2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z", None, "not a date"])
def test_older_equal_or_unknown_times_never_demote(at):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o4", op="occurrence", at=at)],
                                   min_similarity=0.675) == ()


@pytest.mark.unit
def test_unknown_verdict_time_never_demotes():
    assert newer_unfolded_evidence(SLOT, None, [_obs("o5", op="occurrence")],
                                   min_similarity=0.675) == ()


@pytest.mark.unit
def test_neo4j_zone_suffix_and_offsets_compare_as_instants():
    # 2026-07-01T01:00+02:00 is 2026-06-30T23:00Z: older than the View.
    older = _obs("o6", op="occurrence", at="2026-07-01T01:00:00+02:00")
    newer = _obs("o7", op="occurrence", at="2026-07-01T00:00:01Z[UTC]")
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [older, newer], min_similarity=0.675) == ("o7",)


# ---- recall pipeline ---------------------------------------------------------------------


def _newer_other_slot_hit(cosine=0.9):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id="obs-bought-1", attribute="postcards_bought",
               stated_span="bought 5 more postcards", operation="absolute",
               valid_at="2026-08-01T00:00:00+00:00", cosine=cosine)
    return hit


async def _recall(client, adapter, hits, *, gate):
    _setup_observation_lane(adapter, obs_hit=hits, history_view=None, state_view=_state_view())
    _setup_search_and_metadata(client, adapter)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            _state_view() if attribute == "postcard_count" else None)
    adapter.scalar_view_has_user_foundation = lambda *, view_uuid, namespace=None: True
    svc = RecallService(graphiti_client=client, graph_adapter=adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True)
    tuning = RetrievalTuningConfig(scalar_view_freshness_gate=gate,
                                   scalar_verdict_min_similarity=0.675)
    return await svc.recall(QUERY, namespace="proj", tuning=tuning)


def _postcard_verdict(res):
    return next(v for v in res.authority_layer if v.attribute == "postcard_count")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_keeps_the_view_leading(stub_graphiti_client, stub_memory_graph_adapter):
    hits = _postcard_observation_hit() + [_newer_other_slot_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=False)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_demotes_a_view_with_newer_unfolded_evidence(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_other_slot_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    verdict = _postcard_verdict(res)
    assert verdict.status == "advisory"
    assert verdict.has_foundation is True  # foundation unchanged; only the lead is withheld
    view = next(m for m in res.results if m.uuid == "sv-1")
    assert view.is_scalar_authority is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_without_newer_evidence_keeps_the_view_leading(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # The only hit is the 2023 delta the View already folded.
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        _postcard_observation_hit(), gate=True)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_counts_hits_kept_out_of_the_ranked_list(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # With observation recall "off" the quote never enters the ranked list, but the gate sees it.
    hits = _postcard_observation_hit() + [_newer_other_slot_hit(cosine=0.7)]
    _setup_observation_lane(stub_memory_graph_adapter, obs_hit=hits, history_view=None,
                            state_view=_state_view())
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            _state_view() if attribute == "postcard_count" else None)
    stub_memory_graph_adapter.scalar_view_has_user_foundation = (
        lambda *, view_uuid, namespace=None: True)
    svc = RecallService(graphiti_client=stub_graphiti_client,
                        graph_adapter=stub_memory_graph_adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True)
    tuning = RetrievalTuningConfig(scalar_view_freshness_gate=True, scalar_observation_recall="off",
                                   scalar_verdict_min_similarity=0.675)
    res = await svc.recall(QUERY, namespace="proj", tuning=tuning)
    assert "obs-bought-1" not in [m.uuid for m in res.results]
    assert _postcard_verdict(res).status == "advisory"


@pytest.mark.unit
def test_setting_default_and_env_reach_tuning(monkeypatch):
    assert MemorySettings().personal_memory_scalar_view_freshness_gate is False
    assert RetrievalTuningConfig().scalar_view_freshness_gate is False
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE", "true")
    settings = MemorySettings.from_env()
    assert settings.retrieval_tuning().scalar_view_freshness_gate is True
