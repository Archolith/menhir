"""P3b: copies of one stored edge saved by one add_episode agree (pinned fork's reconcile).

Two mentions of a stored, ended trip resolve in one batch, each against its own fetched copy,
through Graphiti's real ``resolve_extracted_edges`` with ``MenhirEdgeExpiryHook``. One mention
is contradicted inside the trip's window (truncate and expire); the other restates it
(``WORLD_END`` keeps it live). The save is a full replace per row, so the copies must agree
before it, or the live copy can overwrite the supersession (Codex P3b-1). A no-reconcile
control proves the counterexample is real.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.utils.maintenance.edge_operations as edge_ops  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.nodes import EntityNode  # noqa: E402
from graphiti_core.search.search_config import SearchResults  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time import AnchoredTimeReport  # noqa: E402
from tests.test_anchored_time_expiry import (  # noqa: E402
    OWNER,
    TRIP_END,
    TURN,
    _edge,
    _episode,
    _result,
)

pytestmark = pytest.mark.unit

TRIP_START = datetime(2024, 3, 27, tzinfo=timezone.utc)
MILAN_START = datetime(2024, 3, 28, tzinfo=timezone.utc)
STORED_FACT = "The user visited Rome."
STORED_ATTRIBUTES = {"time_kind": "point_event", "time_expiry": "world_end"}


def _stored() -> EntityEdge:
    edge = _edge(STORED_FACT, "stored", valid_at=TRIP_START, invalid_at=TRIP_END)
    edge.target_node_uuid = "n-rome"
    edge.attributes = dict(STORED_ATTRIBUTES)
    return edge


def _mention(fact: str, uuid: str) -> EntityEdge:
    edge = _edge(fact, uuid, valid_at=TRIP_START, invalid_at=TRIP_END)
    edge.target_node_uuid = "n-rome"
    return edge


async def _resolve_batch(monkeypatch, mentions, responses):
    """Each search returns a fresh copy of the stored trip; Milan is the invalidation candidate."""
    milan = _edge("The user was in Milan instead.", "milan", valid_at=MILAN_START)

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    async def fake_search(clients, query, group_ids, config, search_filter, query_vector=None):
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[_stored()])
        return SearchResults(edges=[_stored(), milan])

    async def generate_response(messages, **kwargs):
        text = str(messages)
        return next(resp for fact, resp in responses.items() if fact in text)

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, "semaphore_gather", immediate_gather)
    monkeypatch.setattr(edge_ops, "create_entity_edge_embeddings", AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, "_extract_edge_timestamps", AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, "get_between_nodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, "search", fake_search)

    clients = SimpleNamespace(driver=MagicMock(), llm_client=llm, embedder=MagicMock(),
                              cross_encoder=MagicMock())
    nodes = [EntityNode(uuid="n-user", name="user", group_id="ns", labels=["Entity"]),
             EntityNode(uuid="n-rome", name="Rome", group_id="ns", labels=["Entity"])]

    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    binding = policy.anchored_time_owner.set(OWNER)
    try:
        receipt.anchored_time = AnchoredTimeReport(
            status="ok", model="m", owner=OWNER, expiry=True,
            results=tuple(_result(edge.uuid) for edge in mentions))
        resolved, invalidated, _ = await edge_ops.resolve_extracted_edges(
            clients, mentions, _episode(), nodes, {}, {},
            edge_expiry_hook=policy.MenhirEdgeExpiryHook())
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()
    return [edge for edge in resolved + invalidated if edge.uuid == "stored"]


async def _contradicted_and_restated(monkeypatch, *, contradicting_first: bool):
    contradicting = _mention("The user was in Rome until moving to Milan.", "m-contra")
    restating = _mention("The user had a Rome trip in late March.", "m-restate")
    mentions = [contradicting, restating] if contradicting_first else [restating, contradicting]
    responses = {
        contradicting.fact: {"duplicate_facts": [0], "contradicted_facts": [1]},
        restating.fact: {"duplicate_facts": [0], "contradicted_facts": []},
    }
    return await _resolve_batch(monkeypatch, mentions, responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("contradicting_first", [True, False])
async def test_restated_copy_never_overwrites_the_supersession(monkeypatch, contradicting_first):
    copies = await _contradicted_and_restated(monkeypatch, contradicting_first=contradicting_first)

    assert len(copies) == 2 and len({id(edge) for edge in copies}) == 2
    for edge in copies:
        assert edge.expired_at is not None and edge.invalid_at == MILAN_START
        assert edge.episodes == ["ep-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("contradicting_first", [True, False])
async def test_control_without_reconcile_the_copies_disagree(monkeypatch, contradicting_first):
    monkeypatch.setattr(edge_ops, "reconcile_edge_copies", lambda resolved, invalidated: None)
    copies = await _contradicted_and_restated(monkeypatch, contradicting_first=contradicting_first)

    assert sorted((edge.expired_at is None, edge.invalid_at) for edge in copies) == [
        (False, MILAN_START), (True, TRIP_END)]


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_first", [True, False])
async def test_exact_and_paraphrased_copies_save_one_attribute_set(monkeypatch, exact_first):
    """The exact mention keeps the stored attributes (fast path); the paraphrase clears them
    (Menhir passes no edge schema). The cleared set wins in either order (P3b-R2-1)."""
    exact = _mention(STORED_FACT, "m-exact")
    paraphrase = _mention("The user had a Rome trip in late March.", "m-para")
    mentions = [exact, paraphrase] if exact_first else [paraphrase, exact]
    responses = {paraphrase.fact: {"duplicate_facts": [0], "contradicted_facts": []}}

    copies = await _resolve_batch(monkeypatch, mentions, responses)

    assert len(copies) == 2
    assert [edge.attributes for edge in copies] == [{}, {}]
    assert all(edge.expired_at is None and edge.invalid_at == TRIP_END for edge in copies)
