"""A proven self must reach zero candidate search and zero dedup LLM calls.

This is the property the whole prevention half rests on. Cosine candidate acquisition is the
mechanism that fragmented the identity: the `user` window saturates with exact-name matches at
cosine 1.0, so Graphiti's deterministic single-match branch is arithmetically unreachable and
every extraction falls through to the LLM, which may mint yet another fork.

Asserting "the uuid is right" is not enough -- that can hold while the expensive, fragmenting
path still runs. These tests assert the calls do not happen.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.domain.self_identity import self_context_for_pending_episode, self_uuid_for_namespace
from menhir.infrastructure.graphiti_extraction_patches import (
    begin_extraction_receipt,
    clear_extraction_receipt,
    get_extraction_receipt,
)
from menhir.infrastructure.self_binding import bind_canonical_self


def _node(uuid: str, name: str):
    from graphiti_core.nodes import EntityNode

    return EntityNode(
        uuid=uuid, name=name, group_id="", labels=["Entity"],
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def receipt_with_bound_self():
    """An active receipt whose self binding already succeeded, as the resolver would find it."""
    try:
        ctx = self_context_for_pending_episode(
            source="user", namespace="default", episode_uuid="ep-1"
        )
        receipt = begin_extraction_receipt("ep-1", "body", self_identity=ctx)
        nodes = [_node("rand-1", "user")]
        receipt.self_bind_result = bind_canonical_self(nodes, [], {}, ctx)
        yield receipt, nodes
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
def test_bound_self_is_visible_to_the_resolver(receipt_with_bound_self):
    from menhir.infrastructure.graphiti_model_patches import _pre_resolved_self_uuid

    receipt, _ = receipt_with_bound_self
    assert receipt.self_bind_result.bound is True
    assert _pre_resolved_self_uuid() == self_uuid_for_namespace("default")


@pytest.mark.unit
def test_no_bound_self_means_no_pre_resolution():
    """Ordinary episodes must be entirely unaffected: nothing pre-resolved, nothing skipped."""
    from menhir.infrastructure.graphiti_model_patches import _pre_resolved_self_uuid

    try:
        begin_extraction_receipt("ep-2", "body")
        assert _pre_resolved_self_uuid() is None
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
def test_no_receipt_at_all_means_no_pre_resolution():
    """Paths that never begin a receipt (legacy ingest, direct graphiti use) must not break."""
    from menhir.infrastructure.graphiti_model_patches import _pre_resolved_self_uuid

    clear_extraction_receipt()
    assert _pre_resolved_self_uuid() is None


async def _resolve(monkeypatch, nodes) -> tuple[list[list[str]], list[object], object]:
    """Drive the patched resolver, spying on the two paths a proven self must never enter."""
    from types import SimpleNamespace

    import graphiti_core.utils.maintenance.node_operations as node_operations

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    _patch_graphiti_adaptive_dedupe()

    searched: list[list[str]] = []
    llm_calls: list[object] = []

    async def _collect(clients, extracted_nodes, existing_nodes_override=None):
        searched.append([n.name for n in extracted_nodes])
        return [[] for _ in extracted_nodes]

    async def _resolve_with_llm(llm_client, nodes_, indexes, state, *args, **kwargs):
        llm_calls.append(state.unresolved_indices)

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
    monkeypatch.setattr(node_operations, "_resolve_with_llm", _resolve_with_llm)

    resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
        SimpleNamespace(llm_client=object()), nodes
    )
    return searched, llm_calls, (resolved, uuid_map)


@pytest.mark.unit
async def test_proven_self_triggers_no_candidate_search_and_no_dedup_llm(monkeypatch):
    """The load-bearing assertion. Asserting only the resulting uuid would pass even while the
    fragmenting search still ran, so assert the calls themselves do not happen."""
    ctx = self_context_for_pending_episode(source="user", namespace="default", episode_uuid="ep")
    try:
        receipt = begin_extraction_receipt("ep", "body", self_identity=ctx)
        nodes = [_node("rand-1", "user"), _node("rand-2", "Rachel")]
        receipt.self_bind_result = bind_canonical_self(nodes, [], {}, ctx)
        assert receipt.self_bind_result.bound

        searched, llm_calls, (resolved, uuid_map) = await _resolve(monkeypatch, nodes)

        assert searched, "candidate collection was never invoked at all"
        assert "user" not in searched[0], "the proven self was submitted to candidate search"
        assert "Rachel" in searched[0], "the ordinary entity must still be searched"
        assert llm_calls == [], "an LLM was asked to decide the human's identity"

        canonical = self_uuid_for_namespace("default")
        assert uuid_map[canonical] == canonical
        assert any(getattr(n, "uuid", None) == canonical for n in resolved if n is not None)
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
async def test_ordinary_entities_still_reach_candidate_search(monkeypatch):
    """The bypass must be surgical: with no proven self, every node is searched as before."""
    try:
        begin_extraction_receipt("ep", "body")
        nodes = [_node("a", "user"), _node("b", "Rachel")]

        searched, _llm, _out = await _resolve(monkeypatch, nodes)

        assert sorted(searched[0]) == ["Rachel", "user"]
    finally:
        clear_extraction_receipt()
