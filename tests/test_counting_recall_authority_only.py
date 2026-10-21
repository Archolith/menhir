"""Counted authority only (unit R) and counting wording (unit 6), offline.

R: a counted scalar View is stated by its verdict only (never ranked) and the occurrence
observations of a counted slot leave recall. Wording: a counted verdict is a lower bound and never
leads. Both act only with counting_enabled and scalar_view_authority_enabled.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from menhir.api.routes_support import ScalarAuthorityVerdictResponse
from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.recall import RecallResult, ScalarAuthorityContributor, ScalarAuthorityVerdict
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_pipeline import _drop_counted_occurrence_observations
from tests.test_scalar_history_recall import (
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import (
    QUERY,
    _ids,
    _other_slot_hit,
    _state_view,
    _svc,
)

R_ON = dict(counting_enabled=True, counting_recall_authority_only=True)


def _occ_hit(aid, cosine, span, **over):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id=aid, operation="occurrence", cosine=cosine, stated_span=span)
    hit.update(over)
    return hit


def _delta_hit(aid="obs-delta-1", cosine=0.80):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id=aid, operation="delta", cosine=cosine, stated_span="added 3 postcards")
    return hit


def _coin_occ(aid, cosine):
    return _occ_hit(aid, cosine, "I counted coins " + aid, attribute="coin_count", unit="coins")


async def _run(client, adapter, hits, *, query=QUERY, state_view=None, search=True, **kw):
    """Wire the observation lane with exactly ``hits`` and recall. Views are routed by attribute."""
    _setup_observation_lane(adapter, obs_hit=hits, state_view=state_view)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            state_view if attribute == "postcard_count" else None
    )
    if search:
        _setup_search_and_metadata(client, adapter)
        for row in adapter.candidate_metadata:  # in-namespace, so the entities are real candidates
            row["namespace"] = "proj"
    limit = kw.pop("limit", 10)
    return await _svc(client, adapter, **kw).recall(query, namespace="proj", limit=limit)


def _verdict_for(res, view_uuid):
    return next((v for v in res.authority_layer if v.view_uuid == view_uuid), None)


# --------------------------------------------------------------------------- 1. flags off

@pytest.mark.unit
@pytest.mark.asyncio
async def test_flags_off_counted_view_ranked_and_basis_populated(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if R acts without its flag, or the verdict basis/lower_bound are not populated.
    res = await _run(stub_graphiti_client, stub_memory_graph_adapter,
                     [_postcard_observation_hit()[0], _occ_hit("occ-1", 0.90, "posted 1 card")],
                     state_view=_state_view("counted"), counting_enabled=True)
    assert "sv-1" in _ids(res)
    assert "occ-1" in _ids(res)
    verdict = _verdict_for(res, "sv-1")
    assert verdict.basis == "counted" and verdict.lower_bound is False


# --------------------------------------------------------------------------- 2. R on

@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_counted_view_unranked_and_slot_occurrence_dropped(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the counted View is still ranked, its verdict is lost, or the occurrence drop is
    # missing / too broad (delta in slot or other-slot observation dropped).
    res = await _run(stub_graphiti_client, stub_memory_graph_adapter,
                     [_occ_hit("occ-1", 0.97, "posted 1 card"), _delta_hit(),
                      _other_slot_hit()],
                     state_view=_state_view("counted"), **R_ON)
    ids = _ids(res)
    assert "sv-1" not in ids
    verdict = _verdict_for(res, "sv-1")
    assert verdict is not None and verdict.basis == "counted" and verdict.kind == "current"
    assert "occ-1" not in ids
    assert "obs-delta-1" in ids
    assert "obs-other-1" in ids


# --------------------------------------------------------------------------- 3. View won search

def _ranked_view_row():
    return {
        "uuid": "sv-1", "name": "postcards: 37", "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": "postcards: 37", "summary": None, "last_accessed": datetime.now(timezone.utc),
        "edge_count": 1, "freshness": "ACTIVE", "user_flagged": False, "namespace": "proj",
        "is_view": True, "view_class": "FACT", "view_audience": "RECALL", "view_current": True,
        "view_provenance_live": True, "view_kind": "scalar_state", "scalar_basis": "counted",
    }


def _view_wins_search(client, adapter):
    _setup_search_and_metadata(client, adapter)
    client.search_scored_results.append(("sv-1", "postcards: 37", 0.99))
    adapter.candidate_metadata.append(_ranked_view_row())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_removes_counted_view_that_won_vector_search(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if a counted View already ranked by vector search is upgraded in place (kept) instead
    # of removed, or its verdict is dropped with it.
    adapter, client = stub_memory_graph_adapter, stub_graphiti_client
    _view_wins_search(client, adapter)
    _setup_observation_lane(adapter, obs_hit=[_postcard_observation_hit()[0]],
                            state_view=_state_view("counted"))
    control = await _svc(client, adapter, counting_enabled=True).recall(QUERY, namespace="proj")
    assert "sv-1" in _ids(control)  # control: the row passes the View eligibility filters
    res = await _svc(client, adapter, **R_ON).recall(QUERY, namespace="proj")
    assert "sv-1" not in _ids(res)
    assert _verdict_for(res, "sv-1") is not None


# --------------------------------------------------------------------------- 4. counting off

@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_counting_off_is_inert(stub_graphiti_client, stub_memory_graph_adapter):
    # Fails if R acts without counting (verdict appears for a skipped counted View, or a
    # non-occurrence observation of the slot is dropped).
    res = await _run(stub_graphiti_client, stub_memory_graph_adapter,
                     [_postcard_observation_hit()[0], _delta_hit()],
                     state_view=_state_view("counted"),
                     counting_enabled=False, counting_recall_authority_only=True)
    assert not res.authority_layer
    assert {"obs-postcards-1", "obs-delta-1"} <= _ids(res)


# --------------------------------------------------------------------------- 5. one per slot

@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_injects_one_occurrence_per_slot_best_cosine(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if more than one occurrence of a slot is injected, the best-cosine one is not the one
    # kept, or the cap is global rather than per slot.
    res = await _run(
        stub_graphiti_client, stub_memory_graph_adapter,
        [_occ_hit("occ-best", 0.95, "first card"), _occ_hit("occ-worse", 0.90, "second card"),
         _coin_occ("occ-coin", 0.85)],
        state_view=None, **R_ON)
    ids = _ids(res)
    assert "occ-best" in ids and "occ-worse" not in ids
    assert "occ-coin" in ids


# --------------------------------------------------------------------------- 6. proof A: crowding

def _crowd_setup(client, adapter, n=10):
    client.search_scored_results = [(f"ent-{i}", f"Entity {i}", 0.80 - i * 0.01) for i in range(n)]
    adapter.candidate_metadata = [{
        "uuid": f"ent-{i}", "name": f"Entity {i}", "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": f"content {i}", "summary": None, "last_accessed": datetime.now(timezone.utc),
        "edge_count": 3, "freshness": "ACTIVE", "user_flagged": False, "namespace": "proj",
    } for i in range(n)]
    views = {
        "postcard_count": dict(_state_view("counted")),
        "coin_count": {**_state_view("counted"), "uuid": "sv-2", "attribute": "coin_count",
                       "value": 12, "view_key": "vk-2"},
    }
    hits = [
        _occ_hit("occ-p1", 0.97, "card one"), _occ_hit("occ-p2", 0.96, "card two"),
        _coin_occ("occ-c1", 0.95), _coin_occ("occ-c2", 0.94),
    ]
    _setup_observation_lane(adapter, obs_hit=hits)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: views.get(attribute))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_keeps_all_entities_when_counted_slots_would_crowd(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if counted Views / occurrences still compete for the limit and push entities out.
    entities = {f"ent-{i}" for i in range(10)}
    _crowd_setup(stub_graphiti_client, stub_memory_graph_adapter)
    off = await _svc(stub_graphiti_client, stub_memory_graph_adapter,
                     counting_enabled=True).recall(QUERY, namespace="proj", limit=10)
    assert entities - _ids(off), "precondition: with R off at least one entity is crowded out"
    assert entities & _ids(off), "precondition: entities are real candidates, not filtered"
    on = await _svc(stub_graphiti_client, stub_memory_graph_adapter, **R_ON).recall(
        QUERY, namespace="proj", limit=10)
    assert entities <= _ids(on)
    assert not ({"sv-1", "sv-2"} & _ids(on))
    counted = [v for v in on.authority_layer if v.kind == "current" and v.basis == "counted"]
    assert {v.view_uuid for v in counted} == {"sv-1", "sv-2"}


# --------------------------------------------------------------------------- 7. proof B: retellings

@pytest.mark.unit
@pytest.mark.asyncio
async def test_r_on_retellings_do_not_hide_stated_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if retellings of a counted slot crowd out / replace another slot's stated verdict, or
    # any slot-A occurrence survives.
    client, adapter = stub_graphiti_client, stub_memory_graph_adapter
    occs = [_occ_hit(f"occ-a{i}", 0.99 - i * 0.01, f"retelling {i}") for i in range(6)]
    stated_obs = _coin_occ("obs-b", 0.80)
    stated_obs["operation"] = "absolute"
    coin_view = {**_state_view(), "uuid": "sv-b", "attribute": "coin_count", "value": 12,
                 "view_key": "vk-b"}
    views = {"postcard_count": _state_view("counted"), "coin_count": coin_view}
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"
    _setup_observation_lane(adapter, obs_hit=[*occs, stated_obs])
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: views.get(attribute))
    res = await _svc(client, adapter, **R_ON).recall(QUERY, namespace="proj")
    ids = _ids(res)
    assert _verdict_for(res, "sv-b") is not None and "sv-b" in ids
    assert _verdict_for(res, "sv-1").basis == "counted"
    assert not any(i.startswith("occ-a") for i in ids)
    assert _verdict_for(res, "sv-b").basis is None
    assert {"entity-1", "entity-2"} <= ids


# --------------------------------------------------------------------------- 8. scalar-only parity

async def _render(res):
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=res)
    return (await ContextBuilderService(recall_service=recall).build_context(
        "q", max_tokens=4000)).context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stated_view_output_identical_with_flags_on(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if R or wording alters ranking, verdicts or rendered text for a stated (non-counted) View.
    client, adapter = stub_graphiti_client, stub_memory_graph_adapter
    adapter.scalar_view_has_user_foundation = lambda **kw: True
    hits = [_postcard_observation_hit()[0], _other_slot_hit()]
    off = await _run(client, adapter, hits, state_view=_state_view(), counting_enabled=True)
    on = await _run(client, adapter, hits, state_view=_state_view(),
                    counting_enabled=True, counting_recall_authority_only=True,
                    counting_wording_enabled=True)
    # Order-insensitive: the fixture stamps last_accessed with now(), so entity scores drift in the
    # 9th decimal between runs and flip their near-tie with obs-postcards-1 even with both flags off.
    # Same members and (rounded) scores, same verdicts and the same rendered lines is what holds.
    def _scored(res):
        return {r.uuid: round(r.final_score, 6) for r in res.results}

    assert _scored(off) == _scored(on)
    assert ([dataclasses.asdict(v) for v in off.authority_layer]
            == [dataclasses.asdict(v) for v in on.authority_layer])
    assert off.authority_layer and off.authority_layer[0].status == "leads"
    assert {"entity-1", "entity-2", "sv-1"} <= _ids(off)  # non-vacuous: real candidates ranked
    def _lines(text):  # "[Memory N]" numbers follow the (tie-broken) rank, so drop them
        return sorted(re.sub(r"^\[Memory \d+\] ", "", line) for line in text.splitlines())

    assert _lines(await _render(off)) == _lines(await _render(on))


# --------------------------------------------------------------------------- 9. wording

@pytest.mark.unit
@pytest.mark.asyncio
async def test_wording_counted_founded_is_advisory_lower_bound_and_not_authority(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if a founded counted View still leads, loses its foundation flag, or lacks lower_bound.
    client, adapter = stub_graphiti_client, stub_memory_graph_adapter
    adapter.scalar_view_has_user_foundation = lambda **kw: True
    res = await _run(client, adapter, [_postcard_observation_hit()[0]],
                     state_view=_state_view("counted"),
                     counting_enabled=True, counting_wording_enabled=True)
    verdict = _verdict_for(res, "sv-1")
    assert verdict.status == "advisory" and verdict.lower_bound is True
    assert verdict.has_foundation is True
    ranked = next(r for r in res.results if r.uuid == "sv-1")
    assert ranked.is_scalar_authority is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wording_stated_founded_view_still_leads(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if wording leaks onto stated Views (they must keep leads and no lower bound).
    client, adapter = stub_graphiti_client, stub_memory_graph_adapter
    adapter.scalar_view_has_user_foundation = lambda **kw: True
    res = await _run(client, adapter, [_postcard_observation_hit()[0]],
                     state_view=_state_view(),
                     counting_enabled=True, counting_wording_enabled=True)
    verdict = _verdict_for(res, "sv-1")
    assert verdict.status == "leads" and verdict.lower_bound is False
    assert next(r for r in res.results if r.uuid == "sv-1").is_scalar_authority is True


# --------------------------------------------------------------------------- 10. golden

def _verdict(**kw):
    base = dict(
        kind="current", status="advisory", subject_uuid="s", attribute="postcard_count", scope="",
        value_kind="count", unit="", value=37, valid_at="2026-07-01T00:00:00+00:00",
        view_uuid="view-37", has_foundation=True,
        contributors=(ScalarAuthorityContributor(
            assertion_id="a", relation="CURRENT_ANCHOR", operation="absolute", value=37,
            stated_span="I have 37 postcards", valid_at="2026-07-01", evidence_tier="user",
            episode_uuid="e"),),
        contributors_total=1,
    )
    base.update(kw)
    return ScalarAuthorityVerdict(**base)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_golden_lower_bound_wording_and_plain_value():
    # Fails if the lower-bound phrasing changes or leaks onto verdicts without lower_bound.
    lower = (await _render(RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(_verdict(lower_bound=True),)))).splitlines()
    assert lower[0] == (
        "[Scalar authority: ADVISORY] postcard_count = at least 37 you mentioned "
        "(current, valid 2026-07-01T00:00:00+00:00)")
    assert lower[1] == "  - CURRENT_ANCHOR: I have 37 postcards (valid 2026-07-01)"
    plain = await _render(RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(_verdict(),)))
    assert "postcard_count = 37 (current" in plain and "at least" not in plain


# --------------------------------------------------------------------------- 11. API model

@pytest.mark.unit
def test_api_response_round_trips_basis_and_lower_bound():
    # Fails if the response model drops basis / lower_bound (REST clients would never see them).
    d = dataclasses.asdict(_verdict(lower_bound=True, basis="counted"))
    out = ScalarAuthorityVerdictResponse(**d)
    assert out.basis == "counted" and out.lower_bound is True
    assert out.contributors[0].relation == "CURRENT_ANCHOR"
    default = ScalarAuthorityVerdictResponse(**dataclasses.asdict(_verdict()))
    assert default.basis is None and default.lower_bound is False


# --------------------------------------------------------------------------- 12. flags

@pytest.mark.unit
@pytest.mark.parametrize("name,env", [
    ("personal_memory_counting_recall_authority_only",
     "MENHIR_PERSONAL_MEMORY_COUNTING_RECALL_AUTHORITY_ONLY"),
    ("personal_memory_counting_wording_enabled",
     "MENHIR_PERSONAL_MEMORY_COUNTING_WORDING_ENABLED"),
])
def test_counting_flags_registered_default_off_and_need_authority(monkeypatch, name, env):
    # Fails if a flag is unregistered, defaults on, loses its requirements, or is not parsed.
    feature = FEATURES[name]
    assert feature.env_var == env
    assert feature.default is False
    assert "personal_memory_counting_enabled" in feature.requires
    assert "personal_memory_scalar_view_authority_enabled" in feature.requires
    assert getattr(MemorySettings.from_env(), name) is False
    monkeypatch.setenv(env, "1")
    assert getattr(MemorySettings.from_env(), name) is True


# --------------------------------------------------------------------------- 13. helper

def _meta(attribute, subject="s1"):
    return {"subject_uuid": subject, "ss_attribute": attribute, "ss_scope": "",
            "ss_value_kind": "count", "ss_unit": ""}


def _cverdict(attribute, basis, kind="current"):
    return _verdict(subject_uuid="s1", attribute=attribute, basis=basis, kind=kind)


@pytest.mark.unit
def test_drop_helper_only_drops_occurrences_in_counted_slots():
    # Fails if the helper drops by slot alone (non-occurrence), ignores basis, or mutates on no ids.
    inputs = [{"uuid": u} for u in ("occ-counted", "occ-stated", "delta-counted", "ent")]
    meta = {"occ-counted": _meta("a"), "occ-stated": _meta("b"), "delta-counted": _meta("a")}
    layer = [_cverdict("a", "counted"), _cverdict("b", None)]
    out = _drop_counted_occurrence_observations(
        inputs, meta, layer, {"occ-counted", "occ-stated"})
    assert [c["uuid"] for c in out] == ["occ-stated", "delta-counted", "ent"]
    same = _drop_counted_occurrence_observations(inputs, meta, layer, set())
    assert same == inputs
    only_history = _drop_counted_occurrence_observations(
        inputs, meta, [_cverdict("a", "counted", kind="history")], {"occ-counted"})
    assert len(only_history) == 4
