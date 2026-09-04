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

from types import SimpleNamespace

import pytest

from menhir.domain.self_identity import (
    declare_self_subject,
    self_context_for_pending_episode,
    self_uuid_for_namespace,
)
from menhir.infrastructure.graphiti_extraction_patches import (
    begin_extraction_receipt,
    clear_extraction_receipt,
    get_extraction_receipt,
)
from menhir.infrastructure.self_binding import bind_canonical_self
from menhir.infrastructure.self_binding import SelfBindMode


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
        ctx = _declared(episode_uuid="ep-1")
        receipt = begin_extraction_receipt("ep-1", "body", self_identity=ctx)
        nodes = [_node("rand-1", "I")]
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



def _declared(
    namespace: str = "default",
    episode_uuid: str = "ep",
    subject_node_uuid: str = "rand-1",
):
    """Promote trusted turn evidence onto the exact in-memory subject node."""
    return declare_self_subject(
        self_context_for_pending_episode(
            source="manual",
            namespace=namespace,
            source_kind="manual",
            episode_uuid=episode_uuid,
        ),
        subject_node_uuid=subject_node_uuid,
    )


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

    # A driver that reports the canonical node as absent: this is the first self episode in the
    # namespace. The resolver refuses to run without one, because an unreadable canonical node is
    # never evidence that it does not exist.
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    async def _get_by_uuid(driver, uuid):
        raise NodeNotFoundError(uuid)

    monkeypatch.setattr(EntityNode, "get_by_uuid", staticmethod(_get_by_uuid))

    resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
        SimpleNamespace(llm_client=object(), driver=object()), nodes
    )
    return searched, llm_calls, (resolved, uuid_map)


@pytest.mark.unit
async def test_proven_self_triggers_no_candidate_search_and_no_dedup_llm(monkeypatch):
    """The load-bearing assertion. Asserting only the resulting uuid would pass even while the
    fragmenting search still ran, so assert the calls themselves do not happen."""
    ctx = _declared()
    try:
        receipt = begin_extraction_receipt("ep", "body", self_identity=ctx)
        nodes = [_node("rand-1", "I"), _node("rand-2", "Rachel")]
        receipt.self_bind_result = bind_canonical_self(nodes, [], {}, ctx)
        assert receipt.self_bind_result.bound

        searched, llm_calls, (resolved, uuid_map) = await _resolve(monkeypatch, nodes)

        assert searched, "candidate collection was never invoked at all"
        assert "I" not in searched[0], "the proven self was submitted to candidate search"
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


@pytest.mark.unit
async def test_undeclared_node_cannot_reuse_canonical_self_through_ordinary_dedup(monkeypatch):
    """A retained name-shaped `user` node is not allowed to regain authority as a candidate."""
    import graphiti_core.utils.maintenance.node_operations as node_operations

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    canonical = self_uuid_for_namespace("default")
    canonical_candidate = _node(canonical, "user")
    canonical_candidate.attributes = {"is_self": True, "entity_role": "self"}
    ordinary_candidate = _node("ordinary-user", "user")
    seen_candidate_uuids: list[str] = []

    async def _collect(clients, extracted_nodes, existing_nodes_override=None):
        return [[canonical_candidate, ordinary_candidate] for _ in extracted_nodes]

    original_build_indexes = node_operations._build_candidate_indexes

    def _capture_indexes(candidates):
        seen_candidate_uuids.extend(str(candidate.uuid) for candidate in candidates)
        return original_build_indexes(candidates)

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
    monkeypatch.setattr(node_operations, "_build_candidate_indexes", _capture_indexes)
    _patch_graphiti_adaptive_dedupe()

    identity = self_context_for_pending_episode(
        source="manual", namespace="default", episode_uuid="ep"
    )
    try:
        begin_extraction_receipt(
            "ep", "body", self_identity=identity, self_bind_mode=SelfBindMode.ENFORCE
        )
        resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
            SimpleNamespace(llm_client=object(), driver=object()),
            [_node("extracted-user", "user")],
        )
    finally:
        clear_extraction_receipt()

    assert canonical not in seen_candidate_uuids
    assert "ordinary-user" in seen_candidate_uuids
    assert all(getattr(node, "uuid", None) != canonical for node in resolved if node is not None)
    assert canonical not in uuid_map.values()


@pytest.mark.unit
@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE])
async def test_non_enforce_modes_do_not_filter_canonical_candidates(monkeypatch, mode):
    """OFF preserves old resolution and OBSERVE measures without changing ingest."""
    import graphiti_core.utils.maintenance.node_operations as node_operations

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    canonical = self_uuid_for_namespace("default")
    canonical_candidate = _node(canonical, "user")
    canonical_candidate.attributes = {"is_self": True, "entity_role": "self"}
    seen_candidate_uuids: list[str] = []

    async def _collect(clients, extracted_nodes, existing_nodes_override=None):
        return [[canonical_candidate] for _ in extracted_nodes]

    original_build_indexes = node_operations._build_candidate_indexes

    def _capture_indexes(candidates):
        seen_candidate_uuids.extend(str(candidate.uuid) for candidate in candidates)
        return original_build_indexes(candidates)

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
    monkeypatch.setattr(node_operations, "_build_candidate_indexes", _capture_indexes)
    _patch_graphiti_adaptive_dedupe()

    identity = self_context_for_pending_episode(
        source="manual", namespace="default", episode_uuid="ep"
    )
    try:
        begin_extraction_receipt("ep", "body", self_identity=identity, self_bind_mode=mode)
        await node_operations.resolve_extracted_nodes(
            SimpleNamespace(llm_client=object(), driver=object()),
            [_node("extracted-user", "user")],
        )
    finally:
        clear_extraction_receipt()

    assert canonical in seen_candidate_uuids


@pytest.mark.unit
async def test_enforce_refuses_undeclared_extracted_node_with_canonical_identity(monkeypatch):
    """A producer cannot bypass the declaration contract by pre-stamping the node itself."""
    import graphiti_core.utils.maintenance.node_operations as node_operations

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    canonical = self_uuid_for_namespace("default")
    extracted = _node(canonical, "user")
    extracted.attributes = {"is_self": True, "entity_role": "self"}
    candidate_search_called = False

    async def _collect(clients, extracted_nodes, existing_nodes_override=None):
        nonlocal candidate_search_called
        candidate_search_called = True
        return [[] for _ in extracted_nodes]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
    _patch_graphiti_adaptive_dedupe()

    identity = self_context_for_pending_episode(
        source="manual", namespace="default", episode_uuid="ep"
    )
    try:
        begin_extraction_receipt(
            "ep", "body", self_identity=identity, self_bind_mode=SelfBindMode.ENFORCE
        )
        with pytest.raises(RuntimeError, match="undeclared extracted node"):
            await node_operations.resolve_extracted_nodes(
                SimpleNamespace(llm_client=object(), driver=object()),
                [extracted],
            )
    finally:
        clear_extraction_receipt()

    assert candidate_search_called is False


@pytest.mark.unit
async def test_existing_canonical_node_is_preserved_not_overwritten(monkeypatch):
    """Graphiti persists a resolved node with `SET n = $entity_data`, which REPLACES the property
    map. Committing the freshly extracted object would wipe the canonical node's is_self,
    entity_role, namespace, user_flagged, provenance and accumulated summary on every subsequent
    self episode -- silently destroying the identity this change exists to protect.

    The ordinary graphiti path avoids this because _promote_resolved_node returns the hydrated
    database node. The bypass must do the same.
    """
    from types import SimpleNamespace

    import graphiti_core.utils.maintenance.node_operations as node_operations
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    ctx = _declared()
    canonical = self_uuid_for_namespace("default")
    try:
        receipt = begin_extraction_receipt("ep", "body", self_identity=ctx)
        nodes = [_node("rand-1", "I")]
        receipt.self_bind_result = bind_canonical_self(nodes, [], {}, ctx)

        stored = _node(canonical, "I")
        stored.summary = "accumulated summary the extraction does not have"
        stored.attributes = {"is_self": True, "entity_role": "self", "user_flagged": True}

        async def _get_by_uuid(driver, uuid):
            assert uuid == canonical
            return stored

        monkeypatch.setattr(EntityNode, "get_by_uuid", _get_by_uuid)

        async def _collect(clients, extracted_nodes, existing_nodes_override=None):
            return [[] for _ in extracted_nodes]

        monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
        _patch_graphiti_adaptive_dedupe()

        resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
            SimpleNamespace(llm_client=object(), driver=object()), nodes
        )

        committed = resolved[0]
        assert committed is stored, "the bypass committed the extraction over the stored node"
        assert committed.attributes["is_self"] is True
        assert committed.attributes["user_flagged"] is True
        assert committed.summary.startswith("accumulated")
        assert uuid_map[canonical] == canonical
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
async def test_first_self_episode_creates_the_canonical_node(monkeypatch):
    """With nothing stored yet, the extracted node IS the canonical node and must be created."""
    from types import SimpleNamespace

    import graphiti_core.utils.maintenance.node_operations as node_operations
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _patch_graphiti_adaptive_dedupe

    ctx = _declared()
    try:
        receipt = begin_extraction_receipt("ep", "body", self_identity=ctx)
        nodes = [_node("rand-1", "I")]
        receipt.self_bind_result = bind_canonical_self(nodes, [], {}, ctx)

        from graphiti_core.errors import NodeNotFoundError

        async def _missing(driver, uuid):
            raise NodeNotFoundError(uuid)

        monkeypatch.setattr(EntityNode, "get_by_uuid", _missing)

        async def _collect(clients, extracted_nodes, existing_nodes_override=None):
            return [[] for _ in extracted_nodes]

        monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect)
        _patch_graphiti_adaptive_dedupe()

        resolved, _uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
            SimpleNamespace(llm_client=object(), driver=object()), nodes
        )
        assert resolved[0] is nodes[0]
        assert resolved[0].uuid == self_uuid_for_namespace("default")
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
async def test_transient_read_failure_does_not_degrade_to_a_sparse_overwrite(monkeypatch):
    """REVIEW P1. Graphiti replaces the stored property map on save, so treating a driver or
    database failure as "node absent" would let a later successful write erase the canonical
    node's markers, provenance, flags and summary. An operational failure must surface -- the
    episode is retryable; a silent overwrite is not recoverable.
    """
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    async def _boom(driver, uuid):
        raise ConnectionError("neo4j unavailable")

    monkeypatch.setattr(EntityNode, "get_by_uuid", _boom)

    with pytest.raises(ConnectionError):
        await _existing_canonical_node(
            SimpleNamespace(driver=object()), _node("x", "user"), None
        )


@pytest.mark.unit
async def test_absent_node_still_falls_back_to_creating_it(monkeypatch):
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    async def _missing(driver, uuid):
        raise NodeNotFoundError(uuid)

    monkeypatch.setattr(EntityNode, "get_by_uuid", _missing)

    extracted = _node("x", "user")
    got = await _existing_canonical_node(SimpleNamespace(driver=object()), extracted, None)
    assert got is extracted


@pytest.mark.unit
async def test_first_canonical_node_carries_its_markers(monkeypatch):
    """REVIEW P2. The generic ingest metadata stamp supplies neither marker, so without this the
    FIRST canonical node in a namespace is created without is_self/entity_role -- invisible to
    every reader that identifies the human structurally (fork detection, census, migration)."""
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    async def _missing(driver, uuid):
        raise NodeNotFoundError(uuid)

    monkeypatch.setattr(EntityNode, "get_by_uuid", _missing)

    ctx = _declared("proj-a", "e")
    extracted = _node("x", "user")
    got = await _existing_canonical_node(SimpleNamespace(driver=object()), extracted, ctx)

    assert got.attributes["is_self"] is True
    assert got.attributes["entity_role"] == "self"
    assert got.attributes["namespace"] == "proj-a"


@pytest.mark.unit
async def test_existing_node_is_not_restamped(monkeypatch):
    """A stored node already carries its markers; the bypass must return it untouched."""
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    stored = _node(self_uuid_for_namespace("default"), "user")
    stored.attributes = {"is_self": True, "entity_role": "self", "user_flagged": True}

    async def _found(driver, uuid):
        return stored

    monkeypatch.setattr(EntityNode, "get_by_uuid", _found)

    got = await _existing_canonical_node(SimpleNamespace(driver=object()), _node("x", "user"), None)
    assert got is stored
    assert got.attributes["user_flagged"] is True


@pytest.mark.unit
async def test_existing_canonical_node_from_another_group_is_refused(monkeypatch):
    from graphiti_core.nodes import EntityNode

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    stored = _node(self_uuid_for_namespace("proj-a"), "user")
    stored.group_id = "proj-b"

    async def _found(driver, uuid):
        return stored

    monkeypatch.setattr(EntityNode, "get_by_uuid", _found)

    with pytest.raises(RuntimeError, match="cross-namespace resolution"):
        await _existing_canonical_node(
            SimpleNamespace(driver=object()), stored, _declared("proj-a", "e")
        )


@pytest.mark.unit
async def test_a_missing_driver_is_not_evidence_that_the_node_is_absent():
    """REVIEW P2. An absent driver is an operational invariant failure. Substituting the sparse
    extracted node would let graphiti's replacing save erase the stored canonical node -- the same
    defect as swallowing a transient read error, reached through a different door."""
    from types import SimpleNamespace

    from menhir.infrastructure.graphiti_model_patches import _existing_canonical_node

    with pytest.raises(RuntimeError):
        await _existing_canonical_node(SimpleNamespace(driver=None), _node("x", "I"), None)
