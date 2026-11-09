"""A newer same-slot approximate value is listed beneath the advisory View (#519 phase 3)."""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from menhir.api.routes_support import ScalarAuthorityVerdictResponse
from menhir.domain.recall import NewerApproximateValue, RecallResult, ScalarAuthorityVerdict
from menhir.domain.scalar_view_freshness import ObservationTime, newer_unfolded_evidence
from menhir.services.context_builder import ContextBuilderService
from tests.test_scalar_history_recall import _postcard_observation_hit
from tests.test_scalar_view_freshness import (
    SLOT,
    VIEW_AT,
    _newer_occurrence_hit,
    _obs,
    _postcard_verdict,
    _recall,
)

NEWER = "2026-08-01T00:00:00+00:00"
OLDER = "2026-06-01T00:00:00+00:00"


def _approx_hit(*, at=NEWER, **overrides):
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id="obs-approx-1", stated_span="close to 1300 postcards",
               operation="approximate", value=1300, valid_at=at, cosine=0.9)
    hit.update(overrides)
    return hit


# ---- gate (phase 1, verified) ----------------------------------------------------------------


@pytest.mark.unit
def test_gate_keeps_view_advisory_for_newer_approximate_only():
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [_obs("a1", op="approximate")]) == ("a1",)
    assert newer_unfolded_evidence(
        SLOT, VIEW_AT, [_obs("a1", op="approximate", at="2026-05-01T00:00:00Z")]) == ()
    other = ("ent-postcards", "postcards_bought", "collection", "count", "postcards")
    assert newer_unfolded_evidence(
        SLOT, VIEW_AT, [ObservationTime("a2", other, "approximate", NEWER)]) == ()


# ---- pipeline ----------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_newer_approximate_demotes_and_is_listed(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_approx_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    verdict = _postcard_verdict(res)
    assert verdict.status == "advisory"
    assert verdict.newer_approximate == (NewerApproximateValue(
        assertion_id="obs-approx-1", value=1300, quote="close to 1300 postcards", valid_at=NEWER),)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_older_approximate_leaves_the_view_leading_and_unlisted(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_approx_hit(at=OLDER)]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    verdict = _postcard_verdict(res)
    assert verdict.status == "leads"
    assert verdict.newer_approximate == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_newer_occurrence_demotes_but_is_not_listed(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_newer_occurrence_hit()]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    verdict = _postcard_verdict(res)
    assert verdict.status == "advisory"
    assert verdict.newer_approximate == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_approximate_in_another_slot_has_no_effect(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    hits = _postcard_observation_hit() + [_approx_hit(attribute="postcards_bought")]
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, hits, gate=True)
    verdict = _postcard_verdict(res)
    assert verdict.status == "leads"
    assert verdict.newer_approximate == ()


# ---- rendering and response model --------------------------------------------------------------


def _verdict(**overrides) -> ScalarAuthorityVerdict:
    base = {
        "kind": "current", "status": "advisory", "subject_uuid": "ent-p",
        "attribute": "postcard_count", "scope": "", "value_kind": "count",
        "unit": "postcards", "value": 1200, "valid_at": "2026-07-01T00:00:00+00:00",
        "view_uuid": "sv-1", "has_foundation": True,
    }
    base.update(overrides)
    return ScalarAuthorityVerdict(**base)


async def _context(verdict: ScalarAuthorityVerdict) -> str:
    result = RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(verdict,),
    )
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=result)
    built = await ContextBuilderService(recall_service=recall).build_context("q", max_tokens=2000)
    return built.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_renderer_unchanged_when_empty_and_lists_one_entry():
    plain = await _context(_verdict())
    assert "approximate" not in plain
    listed = await _context(_verdict(newer_approximate=(
        NewerApproximateValue("a1", 1300, "close to 1300 postcards", NEWER),)))
    assert '  newer, approximate: about 1300 ("close to 1300 postcards", ' + NEWER + ")" in listed
    assert listed.startswith(plain.split("\n")[0])


@pytest.mark.unit
def test_response_model_carries_newer_approximate_and_defaults_empty():
    verdict = _verdict(newer_approximate=(
        NewerApproximateValue("a1", 1300, "close to 1300 postcards", NEWER),))
    out = ScalarAuthorityVerdictResponse(**asdict(verdict))
    assert out.newer_approximate[0].value == 1300
    assert out.newer_approximate[0].quote == "close to 1300 postcards"
    assert out.newer_approximate[0].valid_at == NEWER
    assert ScalarAuthorityVerdictResponse(**asdict(_verdict())).newer_approximate == []
