"""Rendering of recall provenance: user/None byte-identical, non-user labeled (MCP, context, REST)."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.domain.recall import RecallResult, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.mcp.formatters import _compact_scored_item
from menhir.services.context_builder import ContextBuilderService

_BREAKDOWN = RelevanceBreakdown(
    semantic_similarity=0.9, adjacency_bonus=0.0, recency_bonus=0.0, prominence_bonus=0.0,
    conflict_bonus=0.0, type_boost=0.0, preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1,
    delta=0.0,
)


def _prov(said_by: str) -> dict:
    return {"said_by": said_by, "sources": ["x"], "first_seen_at": None, "turn_evidence_ids": []}


def _scored(uuid: str, name: str, provenance=None) -> ScoredMemory:
    return ScoredMemory(
        uuid=uuid, name=name, content=f"{name} body", scope="PERSISTENT",
        memory_type="SEMANTIC", final_score=0.8, breakdown=_BREAKDOWN, provenance=provenance,
    )


def _ns_item(provenance=None, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        uuid="u1", name="n1", scope="PERSISTENT", memory_type="SEMANTIC", final_score=0.81,
        summary="sum",
        breakdown=SimpleNamespace(semantic_similarity=0.7, adjacency_bonus=0.1,
                                  recency_bonus=0.01, prominence_bonus=0.001),
        provenance=provenance, **extra,
    )


# ---------------------------------------------------------------- MCP item


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_mcp_item_none_and_user_are_byte_identical_with_no_provenance_key(compact) -> None:
    absent = _compact_scored_item(SimpleNamespace(**{
        k: v for k, v in vars(_ns_item()).items() if k != "provenance"}), compact=compact)
    none = _compact_scored_item(_ns_item(None), compact=compact)
    user = _compact_scored_item(_ns_item(_prov("user")), compact=compact)

    assert json.dumps(none, sort_keys=True) == json.dumps(user, sort_keys=True)
    assert json.dumps(none, sort_keys=True) == json.dumps(absent, sort_keys=True)
    assert "provenance" not in none
    assert "provenance" not in user
    assert set(none) == set(user)


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("said_by", ["agent", "mixed", "unknown"])
def test_mcp_item_non_user_carries_provenance_dict_and_otherwise_unchanged(said_by, compact) -> None:
    base = _compact_scored_item(_ns_item(None), compact=compact)
    prov = _prov(said_by)
    item = _compact_scored_item(_ns_item(prov), compact=compact)

    assert item["provenance"] == prov
    assert isinstance(item["provenance"], dict)
    assert {k: v for k, v in item.items() if k != "provenance"} == base


# ---------------------------------------------------------------- context builder


def _service(memories: list[ScoredMemory]) -> ContextBuilderService:
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=RecallResult(
        query="q", preset="knowledge", results=memories,
        candidates_evaluated=len(memories), nodes_touched=len(memories),
    ))
    return ContextBuilderService(recall_service=recall)


async def _context(provenance) -> str:
    memories = [_scored("a", "Alpha", provenance), _scored("b", "Beta", provenance)]
    return (await _service(memories).build_context("q", max_tokens=2000)).context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_none_and_user_are_byte_identical_without_marker() -> None:
    none_ctx = await _context(None)
    user_ctx = await _context(_prov("user"))

    assert none_ctx == user_ctx
    assert "[Memory 1] Alpha: Alpha body" in none_ctx
    for label in ("agent-recorded", "user + agent", "provenance unknown"):
        assert label not in user_ctx


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said_by", "label"),
    [("agent", "agent-recorded"), ("mixed", "user + agent"), ("unknown", "provenance unknown")],
)
async def test_context_non_user_gets_label_marker(said_by, label) -> None:
    ctx = await _context(_prov(said_by))

    assert f"[Memory 1] Alpha ({label}): Alpha body" in ctx
    assert f"[Memory 2] Beta ({label}): Beta body" in ctx
    # removing the marker recovers the unlabeled output exactly
    assert ctx.replace(f" ({label})", "") == await _context(None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_marker_labels_only_the_non_user_item() -> None:
    memories = [_scored("a", "Alpha", _prov("user")), _scored("b", "Beta", _prov("agent"))]
    ctx = (await _service(memories).build_context("q", max_tokens=2000)).context

    assert "[Memory 1] Alpha: Alpha body" in ctx
    assert "[Memory 2] Beta (agent-recorded): Beta body" in ctx


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_marker_combines_with_other_markers() -> None:
    mem = replace(_scored("a", "Alpha", _prov("agent")), is_scalar_authority=True)
    ctx = (await _service([mem]).build_context("q", max_tokens=2000)).context

    assert "Alpha (current authority, agent-recorded)" in ctx


# ---------------------------------------------------------------- REST


@pytest.fixture
def rest_client():
    results = [
        {"uuid": "n-agent", "name": "Agent", "content": "c", "scope": "PERSISTENT",
         "memory_type": "SEMANTIC", "final_score": 0.9,
         "provenance": {"said_by": "agent", "sources": ["claude-code"],
                        "first_seen_at": "2026-01-01T00:00:00Z", "turn_evidence_ids": ["t1"]}},
        {"uuid": "n-none", "name": "Plain", "content": "c", "scope": "PERSISTENT",
         "memory_type": "SEMANTIC", "final_score": 0.8},
        {"uuid": "n-user", "name": "Said", "content": "c", "scope": "PERSISTENT",
         "memory_type": "SEMANTIC", "final_score": 0.7,
         "provenance": {"said_by": "user", "sources": ["user"],
                        "first_seen_at": None, "turn_evidence_ids": []}},
    ]
    backend = SimpleNamespace(recall=AsyncMock(return_value={
        "query": "q", "preset": "knowledge", "results": results, "candidates_evaluated": 2,
    }))
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=SimpleNamespace(), settings=SimpleNamespace()),
        session=SimpleNamespace(session_id="s", user_id="u"),
        capabilities=SimpleNamespace(startup_mode="full"),
    )
    with patch.object(api_routes, "_get_backend", return_value=backend):
        yield TestClient(app)


@pytest.mark.unit
def test_rest_recall_carries_provenance_for_agent_and_omits_it_for_none(rest_client) -> None:
    resp = rest_client.post("/api/recall", json={"query": "q"})

    assert resp.status_code == 200
    rows = {r["uuid"]: r for r in resp.json()["results"]}
    assert rows["n-agent"]["provenance"]["said_by"] == "agent"
    assert rows["n-agent"]["provenance"]["sources"] == ["claude-code"]
    assert rows["n-agent"]["provenance"]["turn_evidence_ids"] == ["t1"]
    # spec: a None provenance is omitted from the REST JSON
    assert "provenance" not in rows["n-none"]
    # user-stated output is unchanged: provenance is not emitted for user items either
    assert "provenance" not in rows["n-user"]
