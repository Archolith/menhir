"""Unit 4R match surface, offline: recall keys occurrence hits by object family under the match
flag, the current-View verdict carries the match-audit fields, context_builder renders them (only for
a lower-bound verdict), and the API model / explorer redaction know them.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

import pytest

from menhir.api.routes_support import ScalarAuthorityVerdictResponse
from menhir.domain.recall import RecallResult, ScalarAuthorityContributor, ScalarAuthorityVerdict
from menhir.explorer.recall_lab import _redact_authority_verdicts
from menhir.privacy import MASK
from menhir.services.context_builder import ContextBuilderService
from tests.test_scalar_history_recall import (
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)
from tests.test_scalar_view_collapse_drilldown import QUERY, _ids, _state_view, _svc

R_ON = dict(counting_enabled=True, counting_recall_authority_only=True)

ACTIONS = '{"bought": 2, "finished": 1.0}'
ITEMS = '["Zaku", "Gundam"]'


def _kit_hit(aid, cosine, attribute, scope, count_object, **over):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id=aid, operation="occurrence", cosine=cosine, attribute=attribute,
               scope=scope, unit="", count_object=count_object, count_family="",
               stated_span="span " + aid)
    hit.update(over)
    return hit


def _kit_view():
    view = _state_view("counted")
    view.update(attribute="model_kit_count", value=3, count_uncertain=2.0,
                count_actions=ACTIONS, count_items=ITEMS)
    return view


def _two_kit_hits():
    return [
        _kit_hit("occ-bought", 0.97, "kit_bought_count", "weekend", "model kit"),
        _kit_hit("occ-finished", 0.96, "kit_finished_count", "", "model kits"),
    ]


async def _recall(client, adapter, hits, *, match, view_attrs=("model_kit_count",), **kw):
    """Wire the lane with ``hits``; every View lookup is recorded as (attribute, scope)."""
    calls: list[tuple[str, str]] = []
    view = _kit_view()

    def lookup(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        calls.append((attribute, scope))
        return view if attribute in view_attrs else None

    _setup_observation_lane(adapter, obs_hit=hits)
    adapter.fetch_current_scalar_view_for_slot = lookup
    adapter.counting_match_enabled = match
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"
    res = await _svc(client, adapter, **kw).recall(QUERY, namespace="proj")
    return res, calls


# --------------------------------------------------------------------------- 1. match on

@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_on_keys_occurrences_by_family_and_carries_audit_fields(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the hit keeps its raw attribute/scope for the View lookup, the verdict loses the
    # View's audit fields, or a family occurrence survives / is injected twice for the slot.
    res, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _two_kit_hits(),
                               match=True, **R_ON)
    assert ("model_kit_count", "") in calls
    assert not any(a in ("kit_bought_count", "kit_finished_count") for a, _ in calls)
    assert not any(s == "weekend" for _, s in calls)
    verdict = next(v for v in res.authority_layer if v.attribute == "model_kit_count")
    assert verdict.basis == "counted"
    assert verdict.count_uncertain == 2.0
    assert verdict.count_actions == ACTIONS
    assert verdict.count_items == ITEMS
    assert not ({"occ-bought", "occ-finished"} & _ids(res))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_on_injects_at_most_one_occurrence_per_family_slot_when_view_missing(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the per-slot occurrence cap still keys on the raw attribute (both would be injected).
    res, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _two_kit_hits(),
                               match=True, view_attrs=(), **R_ON)
    injected = {"occ-bought", "occ-finished"} & _ids(res)
    assert injected == {"occ-bought"}  # best cosine of the one family slot
    assert set(calls) == {("model_kit_count", "")}


# --------------------------------------------------------------------------- 2. match off

@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_off_looks_up_raw_attribute_and_scope(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if family keying happens without the adapter flag.
    res, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter, _two_kit_hits(),
                               match=False, view_attrs=(), **R_ON)
    assert ("kit_bought_count", "weekend") in calls
    assert ("kit_finished_count", "") in calls
    assert not any(a == "model_kit_count" for a, _ in calls)
    assert {"occ-bought", "occ-finished"} <= _ids(res)  # two raw slots, one each


# --------------------------------------------------------------------------- 3. exemptions

@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_on_keeps_raw_slot_for_absolute_and_familyless_occurrence(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if keying applies to non-occurrence hits, or to occurrences with no object/family.
    absolute = _kit_hit("abs-1", 0.95, "kit_stock_count", "shelf", "model kit", operation="absolute")
    bare = _kit_hit("occ-bare", 0.94, "stamp_count", "album", "")
    _, calls = await _recall(stub_graphiti_client, stub_memory_graph_adapter, [absolute, bare],
                             match=True, view_attrs=(), **R_ON)
    assert ("kit_stock_count", "shelf") in calls
    assert ("stamp_count", "album") in calls
    assert not any(a == "model_kit_count" for a, _ in calls)


# --------------------------------------------------------------------------- 4. counting off

@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_flag_without_counting_does_no_keying_and_drops_occurrences(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if keying uses the adapter flag alone, or occurrences leak into recall with counting off.
    absolute = _kit_hit("abs-1", 0.95, "kit_stock_count", "shelf", "model kit", operation="absolute")
    res, calls = await _recall(
        stub_graphiti_client, stub_memory_graph_adapter, [*_two_kit_hits(), absolute],
        match=True, view_attrs=(), counting_enabled=False)
    assert not any(a == "model_kit_count" for a, _ in calls)
    assert not any(a in ("kit_bought_count", "kit_finished_count") for a, _ in calls)
    assert not ({"occ-bought", "occ-finished"} & _ids(res))
    assert ("kit_stock_count", "shelf") in calls  # control: the lane ran, only occurrences dropped


# --------------------------------------------------------------------------- context_builder

def _verdict(**kw):
    base = dict(
        kind="current", status="advisory", subject_uuid="s", attribute="model_kit_count", scope="",
        value_kind="count", unit="", value=3, valid_at="2026-07-01T00:00:00+00:00",
        view_uuid="view-3", has_foundation=True,
        contributors=(ScalarAuthorityContributor(
            assertion_id="a", relation="CURRENT_ANCHOR", operation="absolute", value=3,
            stated_span="I have 3 kits", valid_at="2026-07-01", evidence_tier="user",
            episode_uuid="e"),),
        contributors_total=1,
    )
    base.update(kw)
    return ScalarAuthorityVerdict(**base)


async def _render(verdict):
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(verdict,)))
    return (await ContextBuilderService(recall_service=recall).build_context(
        "q", max_tokens=4000)).context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_lower_bound_renders_uncertain_actions_items_in_order():
    # Fails if the heading wording, line text/order, or int formatting (1.0 -> 1, 2.0 -> 2) changes.
    lines = (await _render(_verdict(
        lower_bound=True, count_uncertain=2.0, count_actions=ACTIONS, count_items=ITEMS))
    ).splitlines()
    assert "= at least 3 you mentioned, plus 2 more that may be repeats (" in lines[0]
    assert lines[1] == "  - per action: bought 2, finished 1"
    assert lines[2] == "  - items: Zaku, Gundam"
    assert lines[3] == "  - CURRENT_ANCHOR: I have 3 kits (valid 2026-07-01)"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [0, 0.0, None])
async def test_context_no_uncertain_keeps_old_heading_but_still_lists_audit_lines(uncertain):
    # Fails if a zero/absent uncertain count adds "plus ..." or suppresses the audit lines.
    lines = (await _render(_verdict(
        lower_bound=True, count_uncertain=uncertain, count_actions=ACTIONS, count_items=ITEMS))
    ).splitlines()
    assert lines[0] == (
        "[Scalar authority: ADVISORY] model_kit_count = at least 3 you mentioned "
        "(current, valid 2026-07-01T00:00:00+00:00)")
    assert lines[1] == "  - per action: bought 2, finished 1"
    assert lines[2] == "  - items: Zaku, Gundam"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_without_lower_bound_ignores_match_fields():
    # Fails if the audit heading/lines leak onto a verdict that is not a lower bound.
    plain = await _render(_verdict(lower_bound=False))
    withf = await _render(_verdict(
        lower_bound=False, count_uncertain=2.0, count_actions=ACTIONS, count_items=ITEMS))
    assert withf == plain
    assert "model_kit_count = 3 (current" in withf
    assert "per action" not in withf and "items:" not in withf and "repeats" not in withf


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_malformed_audit_json_is_skipped_without_error():
    # Fails if bad JSON raises, or a non-list items / unparsable actions line is still rendered.
    text = await _render(_verdict(
        lower_bound=True, count_uncertain=1.0, count_actions="not json", count_items='{"a":1}'))
    assert "per action" not in text and "items:" not in text
    lines = text.splitlines()
    assert "at least 3 you mentioned, plus 1 more that may be repeats (" in lines[0]
    assert lines[1] == "  - CURRENT_ANCHOR: I have 3 kits (valid 2026-07-01)"


# --------------------------------------------------------------------------- API and redaction

@pytest.mark.unit
def test_api_response_round_trips_match_audit_fields():
    # Fails if the response model drops any of the three (REST clients would never see them).
    verdict = _verdict(lower_bound=True, count_uncertain=2.5, count_actions=ACTIONS,
                       count_items=ITEMS)
    out = ScalarAuthorityVerdictResponse(**dataclasses.asdict(verdict))
    assert (out.count_uncertain, out.count_actions, out.count_items) == (2.5, ACTIONS, ITEMS)
    bare = ScalarAuthorityVerdictResponse(**dataclasses.asdict(_verdict()))
    assert (bare.count_uncertain, bare.count_actions, bare.count_items) == (None, None, None)


@pytest.mark.unit
def test_redaction_masks_match_audit_fields_only_when_not_revealed():
    # Fails if item names / actions / the exact uncertain figure leak in redacted mode, a None is
    # turned into a mask, or reveal mode alters them.
    full = dataclasses.asdict(_verdict(
        lower_bound=True, count_uncertain=2.0, count_actions=ACTIONS, count_items=ITEMS))
    empty = dataclasses.asdict(_verdict(lower_bound=True))
    masked_full, masked_empty = _redact_authority_verdicts([full, empty], reveal=False)
    for key in ("count_uncertain", "count_actions", "count_items"):
        assert masked_full[key] == MASK
        assert masked_empty[key] is None
    revealed_full, revealed_empty = _redact_authority_verdicts([full, empty], reveal=True)
    assert revealed_full["count_uncertain"] == 2.0
    assert revealed_full["count_actions"] == ACTIONS
    assert revealed_full["count_items"] == ITEMS
    assert revealed_empty["count_items"] is None
    assert full["count_items"] == ITEMS  # input not mutated
