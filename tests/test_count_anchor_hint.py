"""Count-anchor hint: pure detector, recall wiring, wire shape, context rendering, settings. Offline."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.api.routes_support import RecallResponse
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.domain.count_anchor_hint import count_anchor_hint
from menhir.domain.recall import RecallResult, ScalarAuthorityVerdict
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import _setup_search_and_metadata

ANCHORED = (
    "How many properties did I view before making an offer on the townhouse "
    "in the Brookside neighborhood?"
)


# --------------------------------------------------------------------------- detector

@pytest.mark.unit
def test_anchored_query_yields_hint_with_anchor_phrase() -> None:
    # Fails if the detector stops matching before+anchor noun+preposition, or drops the object.
    hint = count_anchor_hint(ANCHORED)
    assert hint is not None
    assert "making an offer on the townhouse in the Brookside neighborhood" in hint


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "How many weddings have I attended in this year?",
    "What did I buy before the offer on the house?",
    "How many properties did I view before the townhouse?",
    "",
    None,
])
def test_non_anchored_queries_yield_no_hint(query) -> None:
    # Fails if the how-many gate or the anchor-noun requirement is loosened.
    assert count_anchor_hint(query) is None


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "how many trips did I take after the booking for the cabin",
    "How many cars did I test drive prior to the purchase of the sedan?",
])
def test_other_anchor_nouns_and_connectors(query) -> None:
    # Fails if after / prior to or the non-offer anchor nouns are not supported.
    assert count_anchor_hint(query) is not None


# --------------------------------------------------------------------------- recall pipeline

async def _recall(client, adapter, query, **svc_kw) -> RecallResult:
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:  # in-namespace, so candidates survive
        row["namespace"] = "proj"
    svc =RecallService(
        graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService(), **svc_kw)
    return await svc.recall(query, namespace="proj")


@pytest.mark.unit
def test_recall_defaults() -> None:
    # Fails if the hint stops defaulting to None on the result or on for the service (#168 freeze).
    assert RecallResult.__dataclass_fields__["query_hint"].default is None
    assert RecallService.__dataclass_fields__["anchored_count_hint"].default is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_sets_hint_by_default_and_knob_turns_it_off(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    # Fails if the pipeline ignores the knob in either direction.
    off = await _recall(stub_graphiti_client, stub_memory_graph_adapter, ANCHORED,
                        anchored_count_hint=False)
    assert off.query_hint is None
    on = await _recall(stub_graphiti_client, stub_memory_graph_adapter, ANCHORED)
    assert on.query_hint is not None
    assert "the townhouse in the Brookside neighborhood" in on.query_hint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_flag_on_non_anchored_query_has_no_hint(
    stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    # Fails if the hint is emitted for queries the detector rejects.
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        "How many weddings have I attended in this year?")
    assert res.query_hint is None


# --------------------------------------------------------------------------- wire shape

def _ops(payload_result) -> RuntimeProviderDataOpsMixin:
    ops = object.__new__(RuntimeProviderDataOpsMixin)
    ops.built = SimpleNamespace(
        settings=SimpleNamespace(),
        recall_service=SimpleNamespace(recall=AsyncMock(return_value=payload_result)))
    ops._effective_session_id = lambda: None
    return ops


def _result(**kw) -> RecallResult:
    return RecallResult(query="q", preset="knowledge", results=[], candidates_evaluated=0,
                        nodes_touched=0, **kw)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_recall_omits_query_hint_when_none_and_keeps_when_set() -> None:
    # Fails if the pop is removed (flag-off wire shape changes) or over-broad (hint lost).
    assert "query_hint" not in await _ops(_result()).recall("q")
    payload = await _ops(_result(query_hint="H")).recall("q")
    assert payload["query_hint"] == "H"


@pytest.mark.unit
def test_recall_response_carries_query_hint() -> None:
    # Fails if the REST model drops the field.
    base = dict(query="q", preset="knowledge", results=[], candidates_evaluated=0)
    assert RecallResponse(**base).query_hint is None
    assert RecallResponse(**base, query_hint="H").model_dump()["query_hint"] == "H"


# --------------------------------------------------------------------------- context builder

async def _context(result: RecallResult) -> str:
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=result)
    built = await ContextBuilderService(recall_service=recall).build_context("q", max_tokens=2000)
    return built.context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_renders_query_note_only_when_set() -> None:
    # Fails if the hint is not rendered, rendered without the prefix, or rendered when None.
    with_hint = await _context(_result(query_hint="anchor says X"))
    assert "Query note: anchor says X" in with_hint
    assert with_hint.index("nothing relevant found") < with_hint.index("Query note:")
    without = await _context(_result())
    assert "anchor says X" not in without
    assert "Query note:" not in without


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_note_sits_between_authority_and_ranked_memories() -> None:
    # The replay A/B tested this order; fails if the note moves to the tail or above authority.
    from tests.test_context_builder import _mem
    verdict = ScalarAuthorityVerdict(
        kind="current", status="leads", subject_uuid="ent-self", attribute="owned",
        scope="", value_kind="count", unit="", value=3, valid_at="2026-07-02T00:00:00+00:00",
        view_uuid="v3", has_foundation=True, contributors=(), contributors_total=0)
    ctx = await _context(RecallResult(
        query="q", preset="knowledge", results=[_mem("m1", "n", "c", 0.9)],
        candidates_evaluated=1, nodes_touched=1, authority_layer=(verdict,),
        query_hint="anchor says X"))
    assert ctx.index("[Scalar authority: LEADS]") < ctx.index("Query note:") < ctx.index("[Memory 1]")
