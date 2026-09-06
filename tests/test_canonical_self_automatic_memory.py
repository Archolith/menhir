"""Automatic memory: deterministic identity, fallible semantics, unchanged enrichment.

Production extraction/binding/resolution/hydration functions; only model and I/O outputs
are injected. These are offline regression tests, not live-provider accuracy evidence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel
import graphiti_core.graphiti as graphiti
import graphiti_core.utils.bulk_utils as bulk
import graphiti_core.utils.maintenance.combined_extraction as combined
import graphiti_core.utils.maintenance.node_operations as operations

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    SelfEvidenceKind, self_context_for_pending_episode,
    self_subject_endpoint_for_claim, self_uuid_for_namespace,
)
from menhir.infrastructure import graphiti_extraction_patches as extraction
from menhir.infrastructure import graphiti_model_patches as models
from menhir.infrastructure.self_binding import SelfBindMode, InvalidSelfSubjectDeclarationError

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_receipt():
    extraction.clear_extraction_receipt()
    yield
    extraction.clear_extraction_receipt()


def receipt_for(text, namespace="default"):
    endpoint = self_subject_endpoint_for_claim({
        "uuid": "projection", "content": text, "source": "user", "namespace": namespace,
        "diff": None, "subject_endpoint_eligible": True, "is_evidence_projection": True,
        "evidence_projection_of": "turn", "turn_evidence_count": 1,
        "turn_evidence_uuid": "turn", "turn_evidence_role": "user",
        "turn_evidence_declarant": "user", "turn_evidence_text": text,
        "turn_evidence_namespace": namespace,
    })
    assert endpoint is not None
    receipt = extraction.begin_extraction_receipt(
        "projection", text,
        self_identity=self_context_for_pending_episode(
            source="user", namespace=namespace, episode_uuid="projection", turn_evidence_uuid="turn"
        ),
        self_subject_endpoint=endpoint, self_bind_mode=SelfBindMode.ENFORCE,
    )
    receipt.graphiti_episode_uuid = "graphiti-episode"
    return receipt, endpoint


def node(uuid, name, group=""):
    return EntityNode(uuid=uuid, name=name, group_id=group, labels=["Entity"], created_at=NOW)


def edge(source, target, fact, group=""):
    return EntityEdge(source_node_uuid=source, target_node_uuid=target, name="RELATED_TO",
                      fact=fact, group_id=group, episodes=["graphiti-episode"], created_at=NOW)


@pytest.mark.parametrize("text", [
    "I own postcards.", "Yesterday I bought a bicycle.", "I do not own a car.",
    "Do I own a car?", "She said:\nI will handle the deployment.",
    'She said "I will handle the deployment."', "The application user has read access.",
])
def test_author_identity_is_declared_before_any_model_output_regardless_of_text(text):
    receipt, endpoint = receipt_for(text)
    assert receipt.self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    owned = receipt.self_subject_node
    assert receipt.self_identity.subject_node_uuid == owned.uuid
    assert owned.name == endpoint.marker
    assert extraction._active_subject_marker(receipt) == endpoint.marker
    assert receipt.self_bind_result is None  # No relationship has been inferred or persisted.


@pytest.mark.parametrize("namespace", ["default", "work"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_model_reference_uses_preallocated_node_not_model_identity(monkeypatch, namespace, reverse):
    receipt, endpoint = receipt_for("Yesterday I bought a bicycle.", namespace)
    group = namespace_to_group_id(namespace)
    owned = getattr(receipt, "self_subject_node", None)
    target = node("bicycle", "bicycle", group)
    carrier = node("model-chosen-uuid", endpoint.marker, group)
    carrier.summary = "Model-controlled identity properties must not replace Menhir's node."
    source, destination = (target.uuid, carrier.uuid) if reverse else (carrier.uuid, target.uuid)
    relation = edge(source, destination, f"{endpoint.marker} bought a bicycle.", group)

    async def extract(*args, **kwargs):
        assert receipt.self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
        assert receipt.self_subject_node is owned
        assert endpoint.marker in kwargs["custom_extraction_instructions"]
        return [carrier, target], [relation], {carrier.uuid: [0], target.uuid: [0]}

    monkeypatch.setattr(combined, "extract_nodes_and_edges", extract)
    nodes, edges, indices = await extraction._run_graphiti_combined_extraction(
        object(), SimpleNamespace(uuid="graphiti-episode"), [], None, None, None
    )
    canonical = self_uuid_for_namespace(namespace)
    assert nodes[0].uuid == canonical and nodes[0].name == "user"
    assert nodes[0].summary == ""
    assert edges[0].source_node_uuid == (target.uuid if reverse else canonical)
    assert edges[0].target_node_uuid == (canonical if reverse else target.uuid)
    assert edges[0].fact == "user bought a bicycle."
    assert indices[canonical] == [0] and carrier.uuid not in indices
    # The input carrier and edge were not mutated; failed preparation can never half-publish.
    assert carrier.uuid == "model-chosen-uuid"
    assert relation.fact.startswith(endpoint.marker)


@pytest.mark.parametrize("text", ["I own postcards. I own stamps.", "The collection is in the cabinet."])
async def test_mixed_payload_gets_one_correction_then_withholds_unresolved_alias(monkeypatch, text):
    receipt, endpoint = receipt_for(text)
    calls = []

    async def extract(*args, **kwargs):
        calls.append(kwargs)
        nodes = [node("marker", endpoint.marker), node("postcards", "postcards"),
                 node("fallback", "user"), node("stamps", "stamps")]
        return nodes, [edge("marker", "postcards", f"{endpoint.marker} owns postcards."),
                       edge("fallback", "stamps", "user owns stamps.")], {n.uuid: [0] for n in nodes}

    monkeypatch.setattr(combined, "extract_nodes_and_edges", extract)
    nodes, edges, indices = await extraction._run_graphiti_combined_extraction(
        object(), SimpleNamespace(uuid="graphiti-episode"), [], None, None, None
    )
    assert len(calls) == 2
    assert len(edges) == 1 and edges[0].source_node_uuid == self_uuid_for_namespace("default")
    assert {n.uuid for n in nodes} == {self_uuid_for_namespace("default"), "postcards"}
    assert "fallback" not in indices and "stamps" not in indices
    assert receipt.unresolved_author_edges_suppressed == 1


def test_unsigned_model_misattribution_is_not_a_new_identity_or_owner_confirmation():
    """Deliberately document option 1: an incorrect relationship remains an inference error."""
    receipt, endpoint = receipt_for("She said:\nI will handle the deployment.")
    nodes = [node("model", endpoint.marker), node("deployment", "deployment")]
    edges = [edge("model", "deployment", f"{endpoint.marker} will handle the deployment.")]
    result = extraction._record_self_binding(nodes, edges, {n.uuid: [0] for n in nodes}, receipt)
    assert result.bound and nodes[0].uuid == self_uuid_for_namespace("default")
    assert nodes[0].uuid != "model"
    assert edges[0].fact == "user will handle the deployment."
    assert not edges[0].attributes  # No signed/verified status is manufactured.


def test_payload_failure_is_atomic():
    receipt, endpoint = receipt_for("I own postcards.")
    nodes = [node("carrier", endpoint.marker), node("postcards", "postcards")]
    edges = [edge("carrier", "postcards", f"{endpoint.marker} owns postcards.")]
    indices = {n.uuid: [0] for n in nodes}
    edges[0].episodes = ["foreign-episode"]
    before = ([n.model_dump() for n in nodes], [e.model_dump() for e in edges], deepcopy(indices))
    with pytest.raises(InvalidSelfSubjectDeclarationError):
        extraction._record_self_binding(nodes, edges, indices, receipt)
    assert ([n.model_dump() for n in nodes], [e.model_dump() for e in edges], indices) == before


async def test_bound_author_bypasses_candidates_and_normal_enrichment_stays_live(monkeypatch):
    receipt, endpoint = receipt_for("I own a bicycle. The bicycle is blue.")
    nodes = [node("carrier", endpoint.marker), node("bike-new", "bicycle")]
    edges = [edge("carrier", "bike-new", f"{endpoint.marker} owns a bicycle.")]
    indices = {n.uuid: [0] for n in nodes}
    receipt.self_bind_result = extraction._record_self_binding(nodes, edges, indices, receipt)
    existing_bike = node("bike-persistent", "bicycle")
    stored_self = node(self_uuid_for_namespace("default"), "user")
    stored_self.attributes = {"is_self": True, "entity_role": "self", "user_flagged": True}
    collect = AsyncMock(return_value=[[existing_bike]])
    monkeypatch.setattr(operations, "_collect_candidate_nodes", collect)
    monkeypatch.setattr(models, "_existing_canonical_node", AsyncMock(return_value=stored_self))
    for module in (operations, graphiti, bulk):
        monkeypatch.setattr(module, "resolve_extracted_nodes", module.resolve_extracted_nodes)
    monkeypatch.setattr(operations, "_menhir_adaptive_dedupe_patched", False, raising=False)
    models._patch_graphiti_adaptive_dedupe()
    clients = SimpleNamespace(llm_client=AsyncMock(), embedder=SimpleNamespace(
        create_batch=AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]])
    ))
    resolved, uuid_map, _ = await operations.resolve_extracted_nodes(clients, nodes)
    assert [n.uuid for n in collect.await_args.args[1]] == ["bike-new"]
    assert uuid_map[stored_self.uuid] == stored_self.uuid
    clients.llm_client.generate_response.assert_not_awaited()  # No self LLM dedup.

    class Bicycle(BaseModel):
        color: str

    existing_bike.labels = ["Entity", "Bicycle"]
    existing_bike.summary = ""
    async def model(*args, **kwargs):
        if kwargs.get("attribute_extraction"):
            return {"color": "blue"}
        return {"summaries": [{"name": "bicycle", "summary": "A blue bicycle."},
                              {"name": "user", "summary": "Owns a bicycle."}]}

    clients.llm_client.generate_response.side_effect = model
    monkeypatch.setattr(operations, "_extract_entity_attributes", operations._extract_entity_attributes)
    monkeypatch.setattr(operations, "_menhir_untyped_attribute_preservation_patched", False, raising=False)
    models._patch_graphiti_untyped_attribute_preservation()
    hydrated = await graphiti.extract_attributes_from_nodes(
        clients, resolved, SimpleNamespace(content=receipt.episode_text, valid_at=NOW), [],
        entity_types={"Bicycle": Bicycle}, edges=[],
    )
    assert existing_bike in hydrated
    assert existing_bike.summary == "A blue bicycle."
    assert existing_bike.attributes["color"] == "blue"
    assert stored_self.attributes["user_flagged"] is True
    assert stored_self.summary == "Owns a bicycle."
    assert clients.llm_client.generate_response.await_count >= 2


@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE])
async def test_legacy_modes_keep_original_extraction_payload(monkeypatch, mode):
    extraction.begin_extraction_receipt("legacy", "I own a bicycle.", self_bind_mode=mode)
    n = node("legacy-user", "user")
    fake = AsyncMock(return_value=([n], [], {n.uuid: [0]}))
    monkeypatch.setattr(combined, "extract_nodes_and_edges", fake)
    result = await extraction._run_graphiti_combined_extraction(
        object(), SimpleNamespace(uuid="legacy"), [], None, None, None
    )
    assert result == ([n], [], {n.uuid: [0]})
    assert "STRUCTURAL CURRENT-MESSAGE" not in fake.await_args.kwargs["custom_extraction_instructions"]


async def test_concurrent_namespaces_do_not_share_author_nodes():
    async def allocate(namespace):
        receipt, _ = receipt_for("I own postcards.", namespace)
        await asyncio.sleep(0)
        assert extraction.get_extraction_receipt() is receipt
        return receipt.self_subject_node.uuid, receipt.self_identity.self_uuid
    a, b = await asyncio.gather(allocate("a"), allocate("b"))
    assert a[0] != b[0] and a[1] != b[1]


async def test_enforce_refuses_native_dispatch_when_bypass_patch_is_missing(monkeypatch):
    from menhir.infrastructure.graphiti_client import GraphitiClient
    receipt_for("I own postcards.")
    monkeypatch.setattr(graphiti, "extract_nodes", object())
    native = AsyncMock()
    client = GraphitiClient(client=native)
    with pytest.raises(RuntimeError, match="requires combined extraction"):
        await client.add_episode(name="test", episode_body="I own postcards.",
                                 source_description="user", reference_time=NOW)
    native.add_episode.assert_not_awaited()


@pytest.mark.parametrize("text,actor,fact", [
    ("I do not own a car.", "AUTHOR", "user does not own a car."),
    ("Do I own a car?", None, None),
    ('Mara said:\nI own a car.', "Mara", "Mara owns a car."),
    ("The application user has access.", "user", "The application user has access."),
])
async def test_semantic_dispositions_preserve_negation_and_ordinary_actors(monkeypatch, text, actor, fact):
    receipt, endpoint = receipt_for(text)
    if actor is None:
        payload = ([], [], {})
    else:
        name = endpoint.marker if actor == "AUTHOR" else actor
        nodes = [node("actor", name), node("object", "car" if "car" in text else "access")]
        model_fact = fact.replace("user", endpoint.marker) if actor == "AUTHOR" else fact
        payload = (nodes, [edge("actor", "object", model_fact)], {n.uuid: [0] for n in nodes})
    monkeypatch.setattr(combined, "extract_nodes_and_edges", AsyncMock(return_value=payload))
    nodes, edges, _ = await extraction._run_graphiti_combined_extraction(
        object(), SimpleNamespace(uuid="graphiti-episode"), [], None, None, None
    )
    assert [e.fact for e in edges] == ([fact] if fact else [])
    assert receipt.self_bind_result.bound is (actor == "AUTHOR")
    if actor not in (None, "AUTHOR"):
        assert nodes[0].uuid == "actor"


def test_failed_copy_cannot_partially_publish_transport(monkeypatch):
    receipt, endpoint = receipt_for("I own postcards.")
    nodes = [node("carrier", endpoint.marker), node("postcards", "postcards")]
    edges = [edge("carrier", "postcards", f"{endpoint.marker} owns postcards.")]
    indices = {n.uuid: [0] for n in nodes}
    before = ([n.model_dump() for n in nodes], [e.model_dump() for e in edges], deepcopy(indices))
    def fail_copy(value):
        raise RuntimeError("copy failure")
    monkeypatch.setattr(extraction, "deepcopy", fail_copy)
    with pytest.raises(RuntimeError, match="copy failure"):
        extraction._record_self_binding(nodes, edges, indices, receipt)
    assert ([n.model_dump() for n in nodes], [e.model_dump() for e in edges], indices) == before


@pytest.mark.parametrize("orphan", [False, True])
def test_pruned_marker_or_orphan_alias_never_becomes_an_author_declaration(orphan):
    receipt, endpoint = receipt_for("I know the user.")
    nodes = [node("carrier", endpoint.marker), node("alias", "user")]
    edges = [edge("carrier", "alias", f"{endpoint.marker} knows the user.")]
    if orphan:
        nodes.append(node("isolated", "I"))
    declared = receipt.self_identity
    indices = {n.uuid: [0] for n in nodes}
    result = extraction._record_self_binding(nodes, edges, indices, receipt)
    assert nodes == [] and edges == [] and indices == {}
    assert not result.bound
    assert receipt.self_identity is declared


def test_foreign_marker_cannot_hide_beside_valid_transport_in_fact_text():
    receipt, endpoint = receipt_for("I own postcards.")
    payload = extraction._sanitize_combined_payload({
        "extracted_entities": [{"name": endpoint.marker, "entity_type_id": 0},
                               {"name": "postcards", "entity_type_id": 0}],
        "edges": [{"source_entity_name": endpoint.marker, "target_entity_name": "postcards",
                   "relation_type": "OWNS", "episode_indices": [0],
                   "fact": f"{endpoint.marker} and MenhirCurrentSpeaker_stale own postcards."}],
    }, receipt, receipt.episode_text)
    assert payload["edges"] == []


def test_replayed_extractions_converge_on_one_canonical_identity():
    canonical_ids, declarations = set(), set()
    for attempt in range(3):
        receipt, endpoint = receipt_for("I own postcards.")
        declarations.add(receipt.self_identity.subject_node_uuid)
        nodes = [node(f"model-{attempt}", endpoint.marker), node("postcards", "postcards")]
        edges = [edge(nodes[0].uuid, "postcards", f"{endpoint.marker} owns postcards.")]
        indices = {n.uuid: [0] for n in nodes}
        result = extraction._record_self_binding(nodes, edges, indices, receipt)
        assert result.bound and indices[result.self_uuid] == [0]
        canonical_ids.add(edges[0].source_node_uuid)
    assert canonical_ids == {self_uuid_for_namespace("default")}
    assert len(declarations) == 3  # Distinct attempts don't share a mutable author node.
