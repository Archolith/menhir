"""Counted-View selection (read side): current counted Views are ranked directly by cosine to the
query and can only ADD authority candidates; flag off leaves recall untouched. Offline."""

from __future__ import annotations

import pytest

from menhir.domain.self_identity import self_uuid_for_namespace
from tests.test_counting_recall_authority_only import _occ_hit
from tests.test_scalar_history_recall import (
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import QUERY, _state_view, _svc

SELF = self_uuid_for_namespace("proj")
VERSION = "http://embed|model-x"
# The flag lives on the adapter: bootstrap sets counting_view_embed_version only when the flag (and
# its prerequisites) are on, so "flag off" == no version on the adapter (version=None below).
ON = {"counting_enabled": True}


def _view(attr, *, subject=SELF, eligible=True):
    return {**_state_view("counted"), "uuid": f"sv-{attr}", "attribute": attr,
            "view_key": f"vk-{attr}", "subject_uuid": subject, "unit": "",
            "recall_eligible": eligible}


def _vhit(attr, cosine, *, subject=SELF):
    return {"view_subject_uuid": subject, "ss_attribute": attr, "ss_scope": "",
            "ss_kind": "count", "ss_unit": "", "cosine": cosine}


def _occ(attr, cosine):
    return _occ_hit(f"occ-{attr}", cosine, f"counted {attr}", attribute=attr, unit="",
                    subject_uuid=SELF, scope="", value_kind="count")


class _Probe:
    def __init__(self, hits=None, exc=None):
        self.hits, self.exc, self.calls = hits or [], exc, []

    def __call__(self, vec, namespace, *, embed_version, limit=10):
        self.calls.append((namespace, embed_version, limit))
        if self.exc is not None:
            raise self.exc
        return list(self.hits)


async def _recall(client, adapter, *, obs, views, probe=None, version=VERSION, limit=None,
                  **flags):
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"
    _setup_observation_lane(adapter, obs_hit=obs)
    by_attr = {v["attribute"]: v for v in views}
    adapter.fetch_current_scalar_view_for_slot_calls = []

    def _fetch(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        adapter.fetch_current_scalar_view_for_slot_calls.append(attribute)
        return by_attr.get(attribute)

    adapter.fetch_current_scalar_view_for_slot = _fetch
    if probe is not None:
        adapter.search_counted_view_embeddings = probe
    adapter.counting_view_embed_version = version
    kw = {} if limit is None else {"counted_verdict_limit": limit}
    return await _svc(client, adapter, **flags).recall(QUERY, namespace="proj", limit=10, **kw)


def _verdicts(res):
    return [v.attribute for v in (res.authority_layer or ()) if v.kind == "current"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mattress_case_view_search_adds_the_missed_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if a counted View the assertion hits missed is not added to the authority lane.
    obs = [_occ("coin_count", 0.80)]
    views = [_view("coin_count"), _view("mattress_count")]
    probe = _Probe([_vhit("mattress_count", 0.90)])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, obs=obs, views=views,
                        probe=probe, **ON)
    assert _verdicts(res) == ["mattress_count", "coin_count"]  # merged cosine order
    assert probe.calls == [("proj", VERSION, 10)]
    mattress = next(v for v in res.authority_layer if v.attribute == "mattress_count")
    assert mattress.basis == "counted"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_never_calls_view_search_and_output_is_baseline(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    obs = [_occ("coin_count", 0.80)]
    views = [_view("coin_count"), _view("mattress_count")]
    base = await _recall(stub_graphiti_client, stub_memory_graph_adapter, obs=obs, views=views,
                         version=None, counting_enabled=True)
    probe = _Probe([_vhit("mattress_count", 0.90)])
    off = await _recall(stub_graphiti_client, stub_memory_graph_adapter, obs=obs, views=views,
                        probe=probe, version=None, counting_enabled=True)
    assert probe.calls == []
    assert _verdicts(off) == _verdicts(base) == ["coin_count"]
    assert [r.uuid for r in off.results] == [r.uuid for r in base.results]
    assert off.authority_layer == base.authority_layer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_counting_off_never_calls_view_search_even_with_a_version(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the read-side counting kill switch stops gating the View search.
    probe = _Probe([_vhit("mattress_count", 0.90)])
    await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                  obs=[_occ("coin_count", 0.80)],
                  views=[_view("coin_count"), _view("mattress_count")],
                  probe=probe, counting_enabled=False)
    assert probe.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_without_recorded_version_never_calls_view_search(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    probe = _Probe([_vhit("mattress_count", 0.90)])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)],
                        views=[_view("coin_count"), _view("mattress_count")],
                        probe=probe, version=None, **ON)
    assert probe.calls == []
    assert _verdicts(res) == ["coin_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_subject_view_is_gated_for_a_first_person_query(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    probe = _Probe([_vhit("mattress_count", 0.90, subject="ent-someone-else")])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)],
                        views=[_view("coin_count"),
                               _view("mattress_count", subject="ent-someone-else")],
                        probe=probe, **ON)
    assert probe.calls
    assert _verdicts(res) == ["coin_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_view_search_raising_leaves_assertion_verdicts_intact(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    probe = _Probe(exc=RuntimeError("neo4j down"))
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)],
                        views=[_view("coin_count"), _view("mattress_count")],
                        probe=probe, **ON)
    assert probe.calls
    assert _verdicts(res) == ["coin_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_without_view_search_is_tolerated(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    assert not hasattr(stub_memory_graph_adapter, "search_counted_view_embeddings")
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)], views=[_view("coin_count")], **ON)
    assert _verdicts(res) == ["coin_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_lane_never_sees_view_slots(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # The authority loop looks a slot up once; the history lane looks up every observation slot
    # once more. A view-only slot must be looked up once (authority only).
    probe = _Probe([_vhit("mattress_count", 0.90)])
    await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                  obs=[_occ("coin_count", 0.80)],
                  views=[_view("coin_count"), _view("mattress_count")],
                  probe=probe, scalar_history_enabled=True, **ON)
    calls = stub_memory_graph_adapter.fetch_current_scalar_view_for_slot_calls
    assert calls.count("mattress_count") == 1
    assert calls.count("coin_count") == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ineligible_view_is_not_emitted(stub_graphiti_client, stub_memory_graph_adapter):
    probe = _Probe([_vhit("mattress_count", 0.90)])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)],
                        views=[_view("coin_count"), _view("mattress_count", eligible=False)],
                        probe=probe, **ON)
    assert _verdicts(res) == ["coin_count"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cap_respected_with_merged_cosine_ordering(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Merged order: mattress .90, sofa .85, coin .80 (assertion) -> a cap of 2 keeps the first two
    # and the coin occurrence stays as evidence.
    probe = _Probe([_vhit("mattress_count", 0.90), _vhit("sofa_count", 0.85),
                    _vhit("coin_count", 0.50)])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        obs=[_occ("coin_count", 0.80)],
                        views=[_view("coin_count"), _view("mattress_count"), _view("sofa_count")],
                        probe=probe, limit=2, counting_recall_authority_only=True, **ON)
    assert _verdicts(res) == ["mattress_count", "sofa_count"]
    assert "occ-coin_count" in {r.uuid for r in res.results}
