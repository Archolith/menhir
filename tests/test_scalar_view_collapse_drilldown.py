"""Scalar View collapse (flag) and drill-down (no flag), offline.

Collapse: counted verdicts drop inline contributors and observations echoing a verdict slot are
removed from recall. Drill-down: the shared helper, REST route, backend method and MCP tool that
page the items a View was built from.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from menhir.access_contract import AGENT_ALLOWED_TOOLS
from menhir.api import routes
from menhir.domain.recall import RecallResult, ScalarAuthorityContributor, ScalarAuthorityVerdict
from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.recall.get_scalar_view_items import GetScalarViewItemsTool
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scalar_view_items import fetch_scalar_view_items
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import (
    _postcard_history_view,
    _postcard_observation_hit,
    _setup_observation_lane,
    _setup_search_and_metadata,
)

QUERY = "how many postcards do I have"


def _state_view(basis=None):
    view = {
        "uuid": "sv-1", "value": 37, "name": "postcards: 37", "view_key": "vk-1",
        "attribute": "postcard_count", "subject_uuid": "ent-postcards", "namespace": "proj",
        "valid_at": "2026-07-01T00:00:00+00:00", "recall_eligible": True,
    }
    if basis:
        view["scalar_basis"] = basis
    return view


def _other_slot_hit():
    hit = dict(_postcard_observation_hit()[0])
    hit.update(assertion_id="obs-other-1", attribute="coin_count", unit="coins",
               stated_span="12 coins in a jar")
    return hit


def _wire(adapter, *, state_view, history_view=None, extra_hits=()):
    hits = _postcard_observation_hit() + list(extra_hits)
    _setup_observation_lane(adapter, obs_hit=hits, history_view=history_view,
                            state_view=state_view)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace:
            state_view if attribute == "postcard_count" else None
    )


def _svc(client, adapter, **kw):
    return RecallService(
        graphiti_client=client, graph_adapter=adapter, scoring_service=ScoringService(),
        scalar_view_authority_enabled=True, **kw)


async def _recall(client, adapter, query=QUERY, **kw):
    _setup_search_and_metadata(client, adapter)
    return await _svc(client, adapter, **kw).recall(query, namespace="proj")


def _ids(res):
    return {r.uuid for r in res.results}


# --------------------------------------------------------------------------- 1. flag off

@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_keeps_observation_and_counted_contributors(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if collapse acts without its flag.
    _wire(stub_memory_graph_adapter, state_view=_state_view("counted"))
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter, counting_enabled=True)
    assert "obs-postcards-1" in _ids(res)
    verdict = res.authority_layer[0]
    assert verdict.kind == "current"
    assert len(verdict.contributors) == 1 and verdict.contributors_total == 1
    assert verdict.contributors_truncated is False


# --------------------------------------------------------------------------- 2. observation drop

@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_drops_echo_observation_keeps_other_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the covered-slot join is missing (echo kept) or too broad (other slot dropped).
    _wire(stub_memory_graph_adapter, state_view=_state_view(), extra_hits=[_other_slot_hit()])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_collapse_enabled=True)
    assert res.authority_layer and res.authority_layer[0].kind == "current"
    assert "obs-postcards-1" not in _ids(res)
    assert "obs-other-1" in _ids(res)
    assert "sv-1" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_keeps_scalar_history_candidate_in_covered_slot(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if the drop touches anything but OBSERVATION candidates.
    _wire(stub_memory_graph_adapter, state_view=_state_view(),
          history_view=_postcard_history_view())
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        "what was my postcard count before",
                        scalar_view_collapse_enabled=True, scalar_history_enabled=True)
    assert "obs-postcards-1" not in _ids(res)
    assert "history-view-postcards" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_keeps_observation_whose_view_was_skipped(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Counted View with counting off is skipped -> no verdict -> the observation is the only carrier.
    _wire(stub_memory_graph_adapter, state_view=_state_view("counted"))
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_collapse_enabled=True, counting_enabled=False)
    assert not res.authority_layer
    assert "obs-postcards-1" in _ids(res)


# --------------------------------------------------------------------------- 3. counted verdict

@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_counted_verdict_has_no_inline_contributors(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _wire(stub_memory_graph_adapter, state_view=_state_view("counted"))
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_collapse_enabled=True, counting_enabled=True)
    verdict = res.authority_layer[0]
    assert verdict.contributors == ()
    assert verdict.contributors_total == 1
    assert verdict.contributors_truncated is True
    assert verdict.next_offset == 0
    assert verdict.view_uuid == "sv-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_stated_verdict_keeps_contributors(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _wire(stub_memory_graph_adapter, state_view=_state_view())
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_collapse_enabled=True, counting_enabled=True)
    verdict = res.authority_layer[0]
    assert len(verdict.contributors) == 1
    assert verdict.contributors_truncated is False


# --------------------------------------------------------------------------- 4. context builder

def _verdict(**kw):
    base = dict(
        kind="current", status="leads", subject_uuid="s", attribute="visits", scope="",
        value_kind="count", unit="", value=5, valid_at=None, view_uuid="view-5",
        has_foundation=True,
    )
    base.update(kw)
    return ScalarAuthorityVerdict(**base)


async def _context(verdict):
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0,
        authority_layer=(verdict,)))
    return (await ContextBuilderService(recall_service=recall).build_context(
        "q", max_tokens=2000)).context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_renders_drill_down_line_for_collapsed_verdict():
    text = await _context(_verdict(
        contributors=(), contributors_total=5, contributors_truncated=True, next_offset=0))
    assert "  - built from 5 items; drill down: get_scalar_view_items(view_uuid=view-5)" in text
    assert "provenance truncated" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_renders_normal_verdict_unchanged():
    contributor = ScalarAuthorityContributor(
        assertion_id="a", relation="CURRENT_ANCHOR", operation="absolute", value=5,
        stated_span="I went five times", valid_at="2026-01-01", evidence_tier="user",
        episode_uuid="e")
    text = await _context(_verdict(
        contributors=(contributor,), contributors_total=9, contributors_truncated=True,
        next_offset=1))
    assert "  - CURRENT_ANCHOR: I went five times (valid 2026-01-01)" in text
    assert "  - provenance truncated: 9 total; continue at offset 1 for View view-5" in text
    assert "drill down" not in text


# --------------------------------------------------------------------------- 5. helper

class _Adapter:
    def __init__(self, state_total=0, history_total=0):
        self.state_total, self.history_total = state_total, history_total
        self.state_calls, self.history_calls = [], []

    def fetch_scalar_authority_contributors(self, **kw):
        self.state_calls.append(kw)
        rows = [{"assertion_id": "a1", "relation": "CONTRIBUTED_TO", "operation": "delta",
                 "value": 1, "stated_span": "went today", "valid_at": "2026-01-01",
                 "evidence_tier": "user", "episode_uuid": "ep1"}] if self.state_total else []
        return {"contributors": rows, "total": self.state_total, "next_offset": 1}

    def list_scalar_history_entries(self, **kw):
        self.history_calls.append(kw)
        rows = [{"assertion_id": "h1", "operation": "delta", "value": 2, "stated_span": "again",
                 "valid_at": "2026-02-01", "evidence_tier": "user",
                 "episode_uuid": "ep2"}] if self.history_total else []
        return {"entries": rows, "total": self.history_total, "next_offset": None}


@pytest.mark.unit
def test_helper_scalar_state_path_pages_and_clamps_limit():
    adapter = _Adapter(state_total=3)
    out = fetch_scalar_view_items(adapter, view_uuid="v", namespace="proj", offset=2, limit=0)
    assert adapter.state_calls[0]["limit"] == 1 and adapter.state_calls[0]["offset"] == 2
    assert adapter.history_calls == []
    assert out["kind"] == "scalar_state" and out["total"] == 3
    assert out["offset"] == 2 and out["next_offset"] == 1
    assert out["items"][0] == {
        "assertion_id": "a1", "relation": "CONTRIBUTED_TO", "operation": "delta", "value": 1,
        "quote": "went today", "valid_at": "2026-01-01", "evidence_tier": "user",
        "episode_uuid": "ep1"}
    fetch_scalar_view_items(adapter, view_uuid="v", namespace="proj", offset=-4, limit=99)
    assert adapter.state_calls[1]["limit"] == 50 and adapter.state_calls[1]["offset"] == 0
    assert "include_occurrences" not in adapter.state_calls[0]


@pytest.mark.unit
def test_helper_falls_back_to_history_entries():
    adapter = _Adapter(state_total=0, history_total=1)
    out = fetch_scalar_view_items(adapter, view_uuid="v", namespace="proj")
    assert out["kind"] == "scalar_history" and out["total"] == 1
    assert out["items"][0]["relation"] == "HISTORY_ENTRY"
    assert out["items"][0]["quote"] == "again"
    assert "include_occurrences" not in adapter.history_calls[0]


@pytest.mark.unit
def test_helper_both_empty_and_default_silo_namespace():
    adapter = _Adapter()
    out = fetch_scalar_view_items(adapter, view_uuid="v", namespace=None)
    assert out["kind"] is None and out["total"] == 0 and out["items"] == []
    assert adapter.state_calls[0]["namespace"] is not None
    assert adapter.history_calls[0]["namespace"] == adapter.state_calls[0]["namespace"]


# --------------------------------------------------------------------------- 6. registration

@pytest.mark.unit
def test_mcp_tool_is_namespaced_readonly_and_agent_allowed():
    assert GetScalarViewItemsTool.name == "get_scalar_view_items"
    assert GetScalarViewItemsTool.scope == ToolScope.NAMESPACED
    assert GetScalarViewItemsTool.required_tier == "readonly"
    assert "get_scalar_view_items" in AGENT_ALLOWED_TOOLS


@pytest.mark.unit
def test_rest_route_exists_with_readonly_tier():
    matches = [r for r in routes.router.routes
               if getattr(r, "path", "") == "/api/scalar-views/{view_uuid}/items"]
    assert matches and "GET" in matches[0].methods
    assert '_require_tier("readonly")' in inspect.getsource(matches[0].endpoint)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_tool_calls_backend_with_none_for_empty_namespace():
    tool = GetScalarViewItemsTool()
    backend = AsyncMock()
    backend.get_scalar_view_items = AsyncMock(return_value={"view_uuid": "v", "items": []})
    tool.get_backend = lambda: backend
    await tool.endpoint(view_uuid="v", namespace="", offset=3, limit=7)
    backend.get_scalar_view_items.assert_awaited_once_with(
        view_uuid="v", namespace=None, offset=3, limit=7)
