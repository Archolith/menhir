"""Stale-lead demotion: a leading count with newer related evidence in another slot is advisory.

The 89941a93 shape: owned_bike_count = 3 (Feb) led as authoritative while a later
"a new hybrid bike I just purchased" landed in bike_purchase_count (Oct). Offline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.self_identity import inferred_self_uuid_for_namespace, self_uuid_for_namespace
from menhir.domain.stale_lead import attribute_nouns, related_attributes
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_recall_service import _setup_search_and_metadata

SUBJ = "ent-self"
FEB = "2023-02-22T08:13:00Z"
OCT = "2023-10-10T23:08:00Z"


# --------------------------------------------------------------------------- domain

@pytest.mark.unit
@pytest.mark.parametrize("lead, other, expected", [
    ("owned_bike_count", "bike_purchase_count", True),     # the 89941a93 pair
    ("owned_bike_count", "bikes_sold", True),              # crude plural
    ("current_episode", "completed_episode_count", True),  # 72e3ee87: 10 then 50 completed
    ("owned_bike_count", "owned_car_count", False),        # only generic words shared
    ("reading_goal", "reading_session_count", False),      # 2311e44b: shares one of two nouns
    ("points_required_for_redemption", "loyalty_points", False),  # 9ee3ecd6
    ("owned_bike_count", "owned_bike_count", False),       # same attribute is the View's own slot
    ("owned_bike_count", "", False),
    (None, "bike_purchase_count", False),
])
def test_related_attributes(lead, other, expected) -> None:
    # Fails if generic count words relate slots, or a shared noun stops relating them.
    assert related_attributes(lead, other) is expected


@pytest.mark.unit
def test_attribute_nouns_drop_generic_words() -> None:
    assert attribute_nouns("owned_bike_count") == frozenset({"bike"})
    assert attribute_nouns("total_number_count") == frozenset()


# --------------------------------------------------------------------------- recall pipeline

def _hit(attr: str, valid_at: str, value_kind: str = "count") -> dict:
    return {
        "assertion_id": f"obs-{attr}", "stated_span": f"span {attr}", "cosine": 0.99,
        "subject_uuid": SUBJ, "subject_display": "user", "attribute": attr, "scope": "",
        "value_kind": value_kind, "unit": "", "namespace": "proj", "evidence_tier": "agent",
        "operation": "absolute", "value": "3", "valid_at": valid_at,
    }


def _service(client, adapter, *, newer_rows, value_kind="count", enabled=True, newer_raises=False,
             subject=SUBJ, inferred_self_mode="off", extra_hits=(), counting=False):
    _setup_search_and_metadata(client, adapter)
    hit = _hit("owned_bike_count", FEB, value_kind)
    hit["subject_uuid"] = subject
    adapter.search_assertion_embeddings = lambda vec, *, limit, namespaces: [hit, *extra_hits]
    adapter.counting_match_enabled = False
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: {
            "uuid": "view-bikes", "value": 3, "name": "user's owned bike count: 3",
            "view_key": "vk-bikes", "attribute": attribute, "subject_uuid": subject_uuid,
            "namespace": "proj", "valid_at": FEB, "recall_eligible": True})
    adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {
            ("owned_bike_count", "", value_kind, ""): "user"})
    calls: list[dict] = []

    def _newer(**kw):
        calls.append(kw)
        if newer_raises:
            raise RuntimeError("boom")
        return newer_rows

    adapter.newer_subject_assertions = _newer
    svc = RecallService(graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService(),
                        scalar_view_authority_enabled=True, stale_lead_demotion_enabled=enabled,
                        inferred_self_mode=inferred_self_mode, counting_enabled=counting)
    return svc, calls


async def _verdict(svc):
    res = await svc.recall("how many bikes do I currently own", namespace="proj")
    current = [v for v in (res.authority_layer or ()) if v.kind == "current"]
    assert len(current) == 1
    view = [r for r in res.results if r.uuid == "view-bikes"]
    return current[0], (view[0].is_scalar_authority if view else None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_newer_related_count_demotes_lead(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if a newer related count in another slot leaves the stale count leading.
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, newer_rows=[
        {"assertion_id": "a2", "attribute": "bike_purchase_count", "valid_at": OCT}])
    verdict, authority = await _verdict(svc)
    assert verdict.status == "advisory"
    assert verdict.has_foundation is True          # the foundation is real; only the lead is withdrawn
    assert authority is False
    assert calls and calls[0]["after"] == FEB and calls[0]["value_kind"] == "count"
    assert calls[0]["subject_uuids"] == [SUBJ] and calls[0]["before"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_newer_unrelated_count_keeps_lead(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if any newer count, related or not, demotes the lead.
    svc, _ = _service(stub_graphiti_client, stub_memory_graph_adapter, newer_rows=[
        {"assertion_id": "a2", "attribute": "owned_car_count", "valid_at": OCT}])
    verdict, authority = await _verdict(svc)
    assert verdict.status == "leads" and authority is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_keeps_lead_and_skips_read(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if the check runs or demotes with the flag off.
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, enabled=False, newer_rows=[
        {"assertion_id": "a2", "attribute": "bike_purchase_count", "valid_at": OCT}])
    verdict, _ = await _verdict(svc)
    assert verdict.status == "leads" and calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_count_lead_is_not_checked(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if the check widens past counts (non-count matches were all false positives on shard 5).
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, value_kind="status",
                          newer_rows=[{"assertion_id": "a2", "attribute": "bike_purchase_count",
                                       "valid_at": OCT}])
    verdict, _ = await _verdict(svc)
    assert verdict.status == "leads" and calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_failure_is_advisory(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if a failed newer-evidence read lets the count lead (authority read failures are advisory).
    svc, _ = _service(stub_graphiti_client, stub_memory_graph_adapter, newer_rows=[], newer_raises=True)
    verdict, _ = await _verdict(svc)
    assert verdict.status == "advisory"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("mode, both", [("observe", True), ("enforce", True), ("off", False)])
async def test_self_lead_reads_both_self_tiers_when_recall_does(
    stub_graphiti_client, stub_memory_graph_adapter, mode, both,
) -> None:
    # Fails if a canonical-self lead ignores newer evidence bound to the inferred self while recall
    # reads both tiers, or reads the inferred tier while inferred-self mode is off.
    canonical, inferred = self_uuid_for_namespace("proj"), inferred_self_uuid_for_namespace("proj")
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, subject=canonical,
                          inferred_self_mode=mode, newer_rows=[])
    verdict, _ = await _verdict(svc)
    assert verdict.status == "leads"
    expected = sorted({canonical, inferred}) if both else [canonical]
    assert calls and calls[0]["subject_uuids"] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_self_lead_reads_only_its_subject(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    # Fails if a third-party subject's lead is checked against the user's self tiers.
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, inferred_self_mode="observe",
                          newer_rows=[])
    await _verdict(svc)
    assert calls[0]["subject_uuids"] == [SUBJ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("gate, related, status, checked", [
    (True, False, "advisory", False),   # same-slot occurrence: freshness gate decides, no extra read
    (False, True, "advisory", True),    # gate off: the related check still demotes
    (True, True, "advisory", False),
    (False, False, "leads", True),
])
async def test_freshness_gate_then_related_check(
    stub_graphiti_client, stub_memory_graph_adapter, gate, related, status, checked,
) -> None:
    # Fails if the merged order drops either gate or lets one undo the other's demotion.
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig
    occurrence = _hit("owned_bike_count", OCT)
    occurrence.update(assertion_id="obs-occ", operation="occurrence")
    rows = [{"attribute": "bike_purchase_count", "valid_at": OCT}] if related else []
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, newer_rows=rows,
                          extra_hits=[occurrence], counting=True)
    res = await svc.recall("how many bikes do I currently own", namespace="proj",
                           tuning=RetrievalTuningConfig(scalar_view_freshness_gate=gate))
    verdict = next(v for v in res.authority_layer if v.kind == "current")
    assert verdict.status == status
    assert bool(calls) is checked


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_page_is_advisory(stub_graphiti_client, stub_memory_graph_adapter, monkeypatch) -> None:
    # Fails if a possibly truncated read (the related slot could be past the page) lets the count lead.
    import menhir.services.recall_pipeline as rp
    monkeypatch.setattr(rp, "_STALE_LEAD_ATTR_LIMIT", 2)
    svc, calls = _service(stub_graphiti_client, stub_memory_graph_adapter, newer_rows=[
        {"attribute": "owned_car_count", "valid_at": OCT},
        {"attribute": "owned_boat_count", "valid_at": OCT}])
    verdict, _ = await _verdict(svc)
    assert verdict.status == "advisory" and calls[0]["limit"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_lead_demoted_by_newer_related_count(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    # Fails if the expiry verdict ("current UNKNOWN") still leads over a newer related count.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    adapter = stub_memory_graph_adapter
    adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: [_hit("owned_bike_count", FEB)])
    adapter.fetch_current_scalar_view_for_slot = lambda **kw: None
    adapter.scalar_state_service = lambda: SimpleNamespace(
        current_expiries=lambda subj, *, namespace, as_of: {
            ("owned_bike_count", "", "count", ""): SimpleNamespace(
                subject_display="user", expired_value=3, valid_at=FEB, contributor_ids=("e1",))})
    adapter.assertions_have_user_foundation = lambda **kw: True
    calls: list[dict] = []
    adapter.newer_subject_assertions = lambda **kw: calls.append(kw) or [
        {"assertion_id": "a2", "attribute": "bike_purchase_count", "valid_at": OCT}]
    svc = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                        scoring_service=ScoringService(), scalar_view_authority_enabled=True,
                        stale_lead_demotion_enabled=True)
    res = await svc.recall("how many bikes do I currently own", namespace="proj")
    expired = [v for v in (res.authority_layer or ()) if v.kind == "expired"]
    assert len(expired) == 1 and expired[0].status == "advisory"
    assert calls and calls[0]["after"] == FEB


# --------------------------------------------------------------------------- settings

@pytest.mark.unit
def test_settings_default_on_service_default_off(monkeypatch) -> None:
    # Fails if the env var name is wrong or either default flips.
    monkeypatch.delenv("MENHIR_STALE_LEAD_DEMOTION_ENABLED", raising=False)
    assert MemorySettings.from_env().stale_lead_demotion_enabled is True
    monkeypatch.setenv("MENHIR_STALE_LEAD_DEMOTION_ENABLED", "false")
    assert MemorySettings.from_env().stale_lead_demotion_enabled is False
    assert RecallService.__dataclass_fields__["stale_lead_demotion_enabled"].default is False
