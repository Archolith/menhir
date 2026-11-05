"""Freshness gate for scalar verdicts (always on since the #168 freeze), offline.

o500 (#168): a View kept leading with a stale value when the newer statement was a same-slot
occurrence the fold ignores (painting: "just finished my 5th"). Only the exact slot counts: E2E-17
showed that a newer statement in another slot of the same user ("22 books") is not evidence about a
coin count.
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


def _obs(aid, *, slot=SLOT, op="absolute", at="2026-08-01T00:00:00Z"):
    return ObservationTime(assertion_id=aid, slot=slot, operation=op, valid_at=at)


# ---- pure gate -----------------------------------------------------------------------------


@pytest.mark.unit
def test_newer_same_slot_occurrence_is_unfolded_evidence():
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o1", op="occurrence")]) == ("o1",)


@pytest.mark.unit
@pytest.mark.parametrize("op", ["absolute", "delta", "expire"])
def test_newer_same_slot_folded_operations_are_not_evidence(op):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o1", op=op)]) == ()


@pytest.mark.unit
@pytest.mark.parametrize("slot", [
    ("ent-postcards", "postcards_bought", "collection", "count", "postcards"),  # other attribute
    ("ent-postcards", "postcard_count", "", "count", "postcards"),             # other scope
    ("ent-postcards", "postcard_count", "collection", "count", ""),            # other unit
    ("ent-dad", "postcard_count", "collection", "count", "postcards"),         # other subject
])
@pytest.mark.parametrize("op", ["occurrence", "absolute"])
def test_other_slots_never_demote(slot, op):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o3", slot=slot, op=op)]) == ()


@pytest.mark.unit
def test_e2e17_newer_statement_about_another_count_does_not_demote():
    # Coins View as of 07-03; the user later restates books (07-08) and drops the cats (07-12).
    coins = ("self", "owned_coin_count", "", "count", "")
    later = [
        ObservationTime("books-22", ("self", "owned_book_count", "", "count", ""), "absolute",
                        "2026-07-08T10:00:00+00:00"),
        ObservationTime("cats-gone", ("self", "cat_count", "", "count", ""), "expire",
                        "2026-07-12T10:00:00+00:00"),
    ]
    assert newer_unfolded_evidence(coins, "2026-07-03T10:00:00+00:00", later) == ()


@pytest.mark.unit
@pytest.mark.parametrize("at", ["2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z", None, "not a date"])
def test_older_equal_or_unknown_times_never_demote(at):
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("o4", op="occurrence", at=at)]) == ()


@pytest.mark.unit
def test_unknown_verdict_time_never_demotes():
    assert newer_unfolded_evidence(SLOT, None, [_obs("o5", op="occurrence")]) == ()


@pytest.mark.unit
def test_neo4j_zone_suffix_and_offsets_compare_as_instants():
    # 2026-07-01T01:00+02:00 is 2026-06-30T23:00Z: older than the View.
    older = _obs("o6", op="occurrence", at="2026-07-01T01:00:00+02:00")
    newer = _obs("o7", op="occurrence", at="2026-07-01T00:00:01Z[UTC]")
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [older, newer]) == ("o7",)


# ---- recall pipeline ---------------------------------------------------------------------


def _newer_occurrence_hit(cosine=0.9):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id="obs-bought-1", stated_span="bought another postcard",
               operation="occurrence", valid_at="2026-08-01T00:00:00+00:00", cosine=cosine)
    return hit


def _newer_other_slot_hit(cosine=0.9):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id="obs-other-1", attribute="postcards_bought",
               stated_span="bought 5 more postcards", operation="absolute",
               valid_at="2026-08-01T00:00:00+00:00", cosine=cosine)
    return hit


def _service(client, adapter, hits, *, counting=True):
    # Occurrence rows reach recall only with the counting lane on (recall_pipeline drops them
    # otherwise), so the gate only has evidence to act on there.
    _setup_observation_lane(adapter, obs_hit=hits, history_view=None, state_view=_state_view())
    _setup_search_and_metadata(client, adapter)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            _state_view() if attribute == "postcard_count" else None)
    adapter.scalar_view_has_user_foundation = lambda *, view_uuid, namespace=None: True
    adapter.counting_match_enabled = False
    return RecallService(graphiti_client=client, graph_adapter=adapter,
                         scoring_service=ScoringService(), scalar_view_authority_enabled=True,
                         counting_enabled=counting)


async def _recall(client, adapter, hits, *, gate, counting=True):
    svc = _service(client, adapter, hits, counting=counting)
    tuning = RetrievalTuningConfig(scalar_view_freshness_gate=gate,
                                   scalar_verdict_min_similarity=0.675)
    return await svc.recall(QUERY, namespace="proj", tuning=tuning)


def _postcard_verdict(res):
    return next(v for v in res.authority_layer if v.attribute == "postcard_count")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_keeps_the_view_leading(stub_graphiti_client, stub_memory_graph_adapter):
    hits = _postcard_observation_hit() + [_newer_occurrence_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=False)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_demotes_a_view_with_newer_unfolded_evidence(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_occurrence_hit()]
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
async def test_gate_on_ignores_a_newer_other_slot_of_the_same_subject(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_other_slot_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_counts_hits_kept_out_of_the_ranked_list(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # With observation recall "off" the quote never enters the ranked list, but the gate sees it.
    hits = _postcard_observation_hit() + [_newer_occurrence_hit(cosine=0.7)]
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter, hits)
    tuning = RetrievalTuningConfig(scalar_view_freshness_gate=True, scalar_observation_recall="off",
                                   scalar_verdict_min_similarity=0.675)
    res = await svc.recall(QUERY, namespace="proj", tuning=tuning)
    assert "obs-bought-1" not in [m.uuid for m in res.results]
    assert _postcard_verdict(res).status == "advisory"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_skips_counted_views_which_are_built_from_occurrences(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_occurrence_hit()]
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter, hits)
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            _state_view(basis="counted") if attribute == "postcard_count" else None)
    svc.counting_wording_enabled = False  # a counted View may lead only with wording off
    tuning = RetrievalTuningConfig(scalar_view_freshness_gate=True,
                                   scalar_verdict_min_similarity=0.675)
    res = await svc.recall(QUERY, namespace="proj", tuning=tuning)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_counting_off_drops_occurrences_so_the_gate_has_no_evidence(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_occurrence_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True,
                        counting=False)
    assert _postcard_verdict(res).status == "leads"


@pytest.mark.unit
def test_gate_is_on_by_default_and_retired_as_a_setting(monkeypatch):
    # Always on since the #168 freeze; the tuning field stays as a per-request knob.
    assert RetrievalTuningConfig().scalar_view_freshness_gate is True
    assert RetrievalTuningConfig(scalar_view_freshness_gate=False).scalar_view_freshness_gate is False
    assert not hasattr(MemorySettings(), "personal_memory_scalar_view_freshness_gate")
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE", "false")
    assert MemorySettings.from_env().retrieval_tuning().scalar_view_freshness_gate is True
