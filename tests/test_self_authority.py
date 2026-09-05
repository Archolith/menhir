"""Offline acceptance tests for exact, nondelegated canonical-self authorization."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from menhir.domain.self_authority import (
    SELF_ASSERTION_EDGE_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY,
    SelfAuthorizationDecision,
    UnconfirmedSelfAssertionError,
    canonical_json_bytes,
    canonical_temporal_value,
    make_self_assertion_proposal,
    proposal_from_confirmation_payload,
    proposal_matches_persisted_edge,
)
from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.domain.typed_assertion import TypedAssertion
from menhir.domain.event_history import TypedEventAssertion
from menhir.infrastructure.self_authority import (
    FileSelfAssertionAuthorizer,
    confirmation_filename,
)
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_extraction_patches import (
    _wrap_self_authority_edge_resolver,
    begin_extraction_receipt,
    clear_extraction_receipt,
)
from menhir.infrastructure.cypher import ENTITY_METADATA_FIELDS
from menhir.infrastructure.self_binding import (
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    resolve_bind_mode,
)
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.typed_event_repository import TypedEventAssertionRepository
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.view_repository import ViewRepository


def _proposal(**changes):
    values = {
        "principal_id": "owner-1",
        "namespace": "default",
        "episode_uuid": "episode-1",
        "turn_evidence_uuid": "turn-1",
        "evidence_text": "I live in Chicago.",
        "lane": "graphiti_edge",
        "direction": "self_to_entity",
        "polarity": "affirmed",
        "assertion": {
            "counterpart": {"labels": ["Entity"], "name": "Chicago"},
            "fact": "I live in Chicago.",
            "predicate": "LIVES_IN",
            "subject": {"kind": "canonical_self", "marker": "opaque-marker"},
        },
        "temporal_scope": {"expired_at": None, "invalid_at": None, "valid_at": None},
    }
    values.update(changes)
    return make_self_assertion_proposal(**values)


def _authority(tmp_path, proposal, *, fingerprint=None, signed_proposal=None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_path = tmp_path / "owner-public.pem"
    key_path.write_bytes(public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    payload = (signed_proposal or proposal).confirmation_payload()
    signature = private_key.sign(canonical_json_bytes(payload))
    confirmations = tmp_path / "confirmations"
    confirmations.mkdir()
    (confirmations / confirmation_filename(proposal.episode_uuid)).write_text(
        json.dumps({
            "confirmations": [{
                "payload": payload,
                "signature": base64.b64encode(signature).decode("ascii"),
            }]
        }),
        encoding="utf-8",
    )
    return FileSelfAssertionAuthorizer(
        public_key_path=str(key_path),
        public_key_sha256=fingerprint or sha256(public_raw).hexdigest(),
        confirmation_directory=str(confirmations),
    )


@pytest.mark.unit
def test_exact_owner_signature_authorizes(tmp_path) -> None:
    proposal = _proposal()
    decision = _authority(tmp_path, proposal).authorize(proposal)
    assert decision.authorized is True
    assert decision.reason == "owner_signature_verified"
    assert decision.authority_key_id.startswith("ed25519:")


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed",
    [
        {"principal_id": "owner-2"},
        {"namespace": "other"},
        {"turn_evidence_uuid": "turn-2"},
        {"direction": "entity_to_self"},
        {"polarity": "negated"},
        {"evidence_text": "I might live in Chicago."},
        {"claim_revision": 2},
        {"assertion": {"predicate": "LIVES_IN", "fact": "I live in Boston."}},
        {"temporal_scope": {"valid_at": "2027-01-01T00:00:00Z"}},
    ],
)
def test_confirmation_cannot_be_replayed_to_changed_claim(tmp_path, changed) -> None:
    original = _proposal()
    changed_proposal = _proposal(**changed)
    decision = _authority(
        tmp_path, changed_proposal, signed_proposal=original
    ).authorize(changed_proposal)
    assert decision.authorized is False
    assert decision.reason == "confirmation_no_exact_match"


@pytest.mark.unit
def test_public_key_path_without_pinned_fingerprint_is_not_authority(tmp_path) -> None:
    proposal = _proposal()
    decision = _authority(tmp_path, proposal, fingerprint="missing").authorize(proposal)
    assert decision.authorized is False
    assert decision.reason == "authority_fingerprint_not_configured"


@pytest.mark.unit
def test_confirmation_filename_never_contains_episode_path_material() -> None:
    filename = confirmation_filename("../../replace-owner-key")
    assert filename.endswith(".json")
    assert "/" not in filename and "\\" not in filename and ".." not in filename


@pytest.mark.unit
def test_proposal_payload_and_digest_are_order_stable() -> None:
    first = _proposal(assertion={"b": 2, "a": 1})
    second = _proposal(assertion={"a": 1, "b": 2})
    assert first.confirmation_payload() == second.confirmation_payload()
    assert first.claim_digest == second.claim_digest


@pytest.mark.unit
def test_persisted_confirmation_payload_rejects_digest_tampering() -> None:
    proposal = _proposal()
    payload = proposal.confirmation_payload()
    assert proposal_from_confirmation_payload(payload) == proposal
    payload["claim_digest"] = "0" * 64
    with pytest.raises(ValueError, match="not exact"):
        proposal_from_confirmation_payload(payload)


@pytest.mark.unit
@pytest.mark.parametrize("field", ["claim_revision", "schema_version"])
def test_confirmation_payload_rejects_boolean_integer_fields(field) -> None:
    payload = _proposal().confirmation_payload()
    payload[field] = True
    with pytest.raises(ValueError):
        proposal_from_confirmation_payload(payload)


@pytest.mark.unit
def test_confirmation_removal_revokes_the_next_authority_check(tmp_path) -> None:
    proposal = _proposal()
    authorizer = _authority(tmp_path, proposal)
    assert authorizer.authorize(proposal).authorized is True
    (tmp_path / "confirmations" / confirmation_filename(proposal.episode_uuid)).unlink()
    decision = authorizer.authorize(proposal)
    assert decision.authorized is False
    assert decision.reason == "confirmation_not_found"


@pytest.mark.unit
def test_persisted_edge_must_match_signed_direction_semantics_and_time() -> None:
    proposal = _proposal()
    canonical_uuid = self_uuid_for_namespace(proposal.namespace)
    exact = {
        "expected_self_uuid": canonical_uuid,
        "source_node_uuid": canonical_uuid,
        "target_node_uuid": "chicago",
        "counterpart_name": "Chicago",
        "counterpart_labels": ["Entity"],
        "group_id": namespace_to_group_id(proposal.namespace),
        "episode_uuids": ["graphiti-episode-1"],
        "authority_episode_uuid": proposal.episode_uuid,
        "authority_graphiti_episode_uuid": "graphiti-episode-1",
        "predicate": "LIVES_IN",
        "fact": "I live in Chicago.",
        "valid_at": None,
        "invalid_at": None,
        "expired_at": None,
    }
    assert proposal_matches_persisted_edge(proposal, **exact) is True
    for changed in (
        {"source_node_uuid": "chicago", "target_node_uuid": canonical_uuid},
        {"counterpart_name": "Boston"},
        {"counterpart_labels": ["Location"]},
        {"group_id": "other"},
        {"group_id": None},
        {"episode_uuids": ["other-graphiti-episode"]},
        {"authority_episode_uuid": "other-external-episode"},
        {"authority_graphiti_episode_uuid": "other-graphiti-episode"},
        {"predicate": "VISITED"},
        {"fact": "I live in Boston."},
        {"valid_at": "2026-09-05T12:00:00Z"},
    ):
        assert proposal_matches_persisted_edge(
            proposal, **{**exact, **changed}
        ) is False


@pytest.mark.unit
def test_temporal_equality_normalizes_utc_spellings() -> None:
    assert canonical_temporal_value("2026-09-05T12:00:00Z") == (
        canonical_temporal_value("2026-09-05T12:00:00+00:00")
    )


def _signed_edge(proposal):
    assertion = json.loads(proposal.assertion_json)
    temporal = json.loads(proposal.temporal_scope_json)
    return SimpleNamespace(
        uuid="edge-new",
        name=assertion["predicate"],
        fact=assertion["fact"],
        group_id=namespace_to_group_id(proposal.namespace),
        source_node_uuid=self_uuid_for_namespace(proposal.namespace),
        target_node_uuid="chicago",
        episodes=["graphiti-episode-1"],
        valid_at=temporal["valid_at"],
        invalid_at=temporal["invalid_at"],
        expired_at=temporal["expired_at"],
        attributes={
            SELF_ASSERTION_EDGE_EPISODE_PROPERTY: proposal.episode_uuid,
            SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: "graphiti-episode-1",
            SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: canonical_json_bytes(
                proposal.confirmation_payload()
            ).decode("utf-8")
        },
    )


class _AllowExactProposal:
    def authorize(self, proposal):
        return SelfAuthorizationDecision(True, "owner_signature_verified", "ed25519:test")


def _begin_authorized_resolver_receipt(proposal, edge, *, authorizer=None):
    receipt = begin_extraction_receipt(
        proposal.episode_uuid,
        "I live in Chicago.",
        self_identity=SimpleNamespace(namespace=proposal.namespace),
        self_bind_mode=SelfBindMode.ENFORCE,
        self_assertion_authorizer=authorizer or _AllowExactProposal(),
    )
    receipt.self_assertion_authorized_edge_ids.add(id(edge))
    receipt.graphiti_episode_uuid = edge.attributes[
        SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY
    ]
    receipt.self_assertion_counterpart_by_edge_id[id(edge)] = edge.target_node_uuid
    receipt.resolved_node_identity_by_extracted_uuid[edge.target_node_uuid] = (
        edge.target_node_uuid,
        json.loads(proposal.assertion_json)["counterpart"]["name"],
        tuple(json.loads(proposal.assertion_json)["counterpart"]["labels"]),
    )
    return receipt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_edge_payload_and_time_survive_mutating_graphiti_resolver() -> None:
    proposal = _proposal()
    edge = _signed_edge(proposal)
    original_payload = dict(edge.attributes)

    async def mutating_resolver(*args):
        resolved = args[1]
        resolved.attributes = {"model_authored": "discard me"}
        resolved.valid_at = "2030-01-01T00:00:00Z"
        resolved.name = "VISITED"
        resolved.fact = "I visited Boston."
        resolved.source_node_uuid = "mutated-source"
        resolved.target_node_uuid = "mutated-target"
        return resolved, [SimpleNamespace(uuid="invalidated")], []

    _begin_authorized_resolver_receipt(proposal, edge)
    try:
        resolved, invalidated, duplicates = await _wrap_self_authority_edge_resolver(
            mutating_resolver
        )(None, edge, [], [], SimpleNamespace(uuid="graphiti-episode-1"))
    finally:
        clear_extraction_receipt()

    assert resolved is edge
    assert resolved.attributes == original_payload
    assert resolved.valid_at is None
    assert resolved.name == "LIVES_IN"
    assert resolved.fact == "I live in Chicago."
    assert resolved.source_node_uuid == self_uuid_for_namespace(proposal.namespace)
    assert resolved.target_node_uuid == "chicago"
    assert invalidated == []
    assert duplicates == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_edge_exact_replay_reuses_existing_edge_without_model_resolution() -> None:
    proposal = _proposal()
    edge = _signed_edge(proposal)
    existing = _signed_edge(proposal)
    existing.uuid = "edge-existing"
    existing.attributes = {}
    existing.episodes = ["older-episode"]

    async def unexpected_resolver(*args):
        raise AssertionError("exact signed replay reached model edge resolution")

    _begin_authorized_resolver_receipt(proposal, edge)
    try:
        resolved, invalidated, duplicates = await _wrap_self_authority_edge_resolver(
            unexpected_resolver
        )(None, edge, [existing], [], SimpleNamespace(uuid="graphiti-episode-1"))
    finally:
        clear_extraction_receipt()

    assert resolved is existing
    assert resolved.attributes == edge.attributes
    assert resolved.episodes == ["older-episode", "graphiti-episode-1"]
    assert invalidated == []
    assert duplicates == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protected_edge_fails_closed_if_changed_after_authorization() -> None:
    proposal = _proposal()
    edge = _signed_edge(proposal)
    _begin_authorized_resolver_receipt(proposal, edge)
    edge.fact = "I live in Boston."
    try:
        with pytest.raises(InvalidSelfSubjectDeclarationError, match="changed"):
            await _wrap_self_authority_edge_resolver(lambda *args: None)(
                None, edge, [], [], SimpleNamespace(uuid="graphiti-episode-1")
            )
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protected_edge_rechecks_owner_confirmation_at_final_resolution() -> None:
    proposal = _proposal()
    edge = _signed_edge(proposal)

    class RevokedAuthority:
        def authorize(self, proposal):
            return SelfAuthorizationDecision(False, "confirmation_not_found")

    _begin_authorized_resolver_receipt(proposal, edge, authorizer=RevokedAuthority())
    try:
        with pytest.raises(InvalidSelfSubjectDeclarationError, match="absent"):
            await _wrap_self_authority_edge_resolver(lambda *args: None)(
                None, edge, [], [], SimpleNamespace(uuid="graphiti-episode-1")
            )
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_edge_recall_rechecks_confirmation_and_excludes_revoked_fact(
    tmp_path,
) -> None:
    proposal = _proposal()
    _authority(tmp_path, proposal)
    public_key = serialization.load_pem_public_key(
        (tmp_path / "owner-public.pem").read_bytes()
    )
    fingerprint = sha256(public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )).hexdigest()
    canonical_uuid = self_uuid_for_namespace(proposal.namespace)
    edge = SimpleNamespace(
        uuid="edge-1",
        name="LIVES_IN",
        fact="I live in Chicago.",
        group_id="",
        source_node_uuid=canonical_uuid,
        target_node_uuid="chicago",
        source_node_name="user",
        target_node_name="Chicago",
        source_node_labels=["Entity"],
        target_node_labels=["Entity"],
        created_at=None,
        valid_at=None,
        invalid_at=None,
        expired_at=None,
        episodes=["graphiti-episode-1"],
        attributes={
            SELF_ASSERTION_EDGE_EPISODE_PROPERTY: proposal.episode_uuid,
            SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: "graphiti-episode-1",
            SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: canonical_json_bytes(
                proposal.confirmation_payload()
            ).decode("utf-8")
        },
    )

    class SearchClient:
        async def search_(self, query, config, *, group_ids=None):
            return SimpleNamespace(edges=[edge], edge_reranker_scores=[0.9])

    settings = SimpleNamespace(
        canonical_self_confirmation_public_key_path=str(tmp_path / "owner-public.pem"),
        canonical_self_confirmation_public_key_sha256=fingerprint,
        canonical_self_confirmation_directory=str(tmp_path / "confirmations"),
    )
    client = GraphitiClient(client=SearchClient(), scheduler_settings=settings)
    first = await client.search_edges_scored(
        "where do I live",
        canonical_self_uuid=canonical_uuid,
        enforce_canonical_self_authority=True,
    )
    assert [row["uuid"] for row in first] == ["edge-1"]

    edge.target_node_name = "Boston"
    redirected = await client.search_edges_scored(
        "where do I live",
        canonical_self_uuid=canonical_uuid,
        enforce_canonical_self_authority=True,
    )
    assert redirected == []
    edge.target_node_name = "Chicago"

    edge.target_node_labels = ["Location"]
    relabeled = await client.search_edges_scored(
        "where do I live",
        canonical_self_uuid=canonical_uuid,
        enforce_canonical_self_authority=True,
    )
    assert relabeled == []
    edge.target_node_labels = ["Entity"]

    edge.fact = "I live in Boston."
    tampered = await client.search_edges_scored(
        "where do I live",
        canonical_self_uuid=canonical_uuid,
        enforce_canonical_self_authority=True,
    )
    assert tampered == []
    edge.fact = "I live in Chicago."

    (tmp_path / "confirmations" / confirmation_filename(proposal.episode_uuid)).unlink()
    second = await client.search_edges_scored(
        "where do I live",
        canonical_self_uuid=canonical_uuid,
        enforce_canonical_self_authority=True,
    )
    assert second == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unscoped_graphiti_edge_recall_derives_canonical_self_from_group(tmp_path) -> None:
    proposal = _proposal(namespace="tenant-a")
    _authority(tmp_path, proposal)
    public_key = serialization.load_pem_public_key(
        (tmp_path / "owner-public.pem").read_bytes()
    )
    fingerprint = sha256(public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )).hexdigest()
    edge = SimpleNamespace(
        uuid="edge-tenant-a",
        name="LIVES_IN",
        fact="I live in Chicago.",
        group_id="tenant-a",
        source_node_uuid=self_uuid_for_namespace("tenant-a"),
        target_node_uuid="chicago",
        source_node_name="user",
        target_node_name="Chicago",
        source_node_labels=["Entity"],
        target_node_labels=["Entity"],
        created_at=None,
        valid_at=None,
        invalid_at=None,
        expired_at=None,
        episodes=["graphiti-episode-tenant-a"],
        attributes={
            SELF_ASSERTION_EDGE_EPISODE_PROPERTY: proposal.episode_uuid,
            SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: "graphiti-episode-tenant-a",
            SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: canonical_json_bytes(
                proposal.confirmation_payload()
            ).decode("utf-8")
        },
    )

    class SearchClient:
        async def search_(self, query, config, *, group_ids=None):
            return SimpleNamespace(edges=[edge], edge_reranker_scores=[0.9])

    settings = SimpleNamespace(
        canonical_self_confirmation_public_key_path=str(tmp_path / "owner-public.pem"),
        canonical_self_confirmation_public_key_sha256=fingerprint,
        canonical_self_confirmation_directory=str(tmp_path / "confirmations"),
    )
    client = GraphitiClient(client=SearchClient(), scheduler_settings=settings)
    rows = await client.search_edges_scored(
        "where do I live", enforce_canonical_self_authority=True
    )
    assert [row["uuid"] for row in rows] == ["edge-tenant-a"]


class _NoWriteNeo4j:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("unconfirmed canonical-self assertion reached Neo4j")


@pytest.mark.unit
def test_scalar_repository_rejects_direct_canonical_self_even_with_claimed_metadata() -> None:
    neo4j = _NoWriteNeo4j()
    assertion = TypedAssertion(
        subject_uuid=self_uuid_for_namespace("ns-a"),
        subject_display="user",
        attribute="wake",
        scope="",
        value_kind="clock_time",
        unit="",
        operation="absolute",
        value="07:30",
        stated_span="I wake at 07:30",
        episode_uuid="turn-1",
        valid_at="2026-09-05T07:30:00Z",
        learned_at="2026-09-05T08:00:00Z",
        namespace="ns-a",
        metadata={"owner_confirmed": True, "confidence": 1.0},
    )
    repository = TypedAssertionRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")
    with pytest.raises(UnconfirmedSelfAssertionError, match="exact owner confirmation"):
        repository.record_assertion(assertion)
    assert neo4j.calls == []


@pytest.mark.unit
def test_event_repository_rejects_direct_canonical_self_even_with_claimed_metadata() -> None:
    neo4j = _NoWriteNeo4j()
    assertion = TypedEventAssertion(
        subject_uuid=self_uuid_for_namespace("ns-a"),
        subject_display="user",
        predicate="bought",
        object_key="notebook",
        object_display="notebook",
        valid_at="2026-09-05T07:30:00Z",
        learned_at="2026-09-05T08:00:00Z",
        stated_span="I bought a notebook",
        episode_uuid="turn-1",
        turn_evidence_uuid="turn-1",
        namespace="ns-a",
        metadata={"owner_confirmed": True, "second_model_confirmed": True},
    )
    repository = TypedEventAssertionRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")
    with pytest.raises(UnconfirmedSelfAssertionError, match="exact owner confirmation"):
        repository.record_event_assertion(assertion)
    assert neo4j.calls == []


@pytest.mark.unit
def test_view_repository_rejects_direct_canonical_self_projection() -> None:
    neo4j = _NoWriteNeo4j()
    repository = ViewRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    with pytest.raises(UnconfirmedSelfAssertionError, match="exact owner confirmation"):
        repository.record(
            "scalar_state",
            subject="user",
            subject_uuid=self_uuid_for_namespace("ns-a"),
            namespace="ns-a",
            authoritative=True,
        )
    assert neo4j.calls == []


@pytest.mark.unit
def test_counter_view_rejects_uuidless_self_alias_in_enforce_mode() -> None:
    neo4j = _NoWriteNeo4j()
    repository = ViewRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    with pytest.raises(UnconfirmedSelfAssertionError, match="resolved non-self subject UUID"):
        repository.record_counter(
            subject="user",
            counter="notebooks",
            value=4,
            namespace="ns-a",
        )
    assert neo4j.calls == []


class _AuthorityBoundaryNeo4j:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls = []

    def execute(self, query, params=None, **kwargs):
        self.calls.append((str(query), dict(params or {}), dict(kwargs)))
        return self.responses.pop(0) if self.responses else []


@pytest.mark.unit
def test_generic_context_reads_exclude_unverified_self_in_enforce_only() -> None:
    enforce_neo4j = _AuthorityBoundaryNeo4j()
    repository = MemoryQueryRepository(enforce_neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    repository.fetch_recent_memories(limit=5, namespace="ns-a")
    repository.fetch_flagged_memories(limit=5, namespace="ns-a")
    repository.fetch_flagged_memory_bootstrap_version(namespace="ns-a")

    expected_uuid = self_uuid_for_namespace("ns-a")
    for query, params, _kwargs in enforce_neo4j.calls:
        assert "n.uuid = $canonical_self_uuid" in query
        assert "n.view_subject_uuid" in query
        assert "n.view_subject" in query
        assert "coalesce(n.is_view, false)" in query
        assert "self_neighbor:Entity" in query
        assert "coalesce(self_neighbor.is_self, false)" in query
        assert params["canonical_self_uuid"] == expected_uuid
        alias_pattern = params["canonical_self_alias_pattern"]
        assert re.fullmatch(alias_pattern, "the   user")
        assert re.fullmatch(alias_pattern, "user")
        assert re.fullmatch(alias_pattern, "myself")
        assert re.fullmatch(alias_pattern, "database user") is None

    off_neo4j = _AuthorityBoundaryNeo4j()
    MemoryQueryRepository(off_neo4j).fetch_recent_memories(limit=5, namespace="ns-a")
    assert "canonical_self_uuid" not in off_neo4j.calls[0][0]
    assert "canonical_self_uuid" not in off_neo4j.calls[0][1]


@pytest.mark.unit
def test_view_repository_rejects_structural_self_despite_namespace_spoof() -> None:
    neo4j = _AuthorityBoundaryNeo4j([[{"is_self": True}]])
    repository = ViewRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    with pytest.raises(UnconfirmedSelfAssertionError, match="exact owner confirmation"):
        repository.record(
            "scalar_state",
            subject="user",
            subject_uuid=self_uuid_for_namespace("ns-a"),
            namespace="ns-b",
            authoritative=True,
        )

    assert len(neo4j.calls) == 1
    assert "coalesce(subject.is_self, false)" in neo4j.calls[0][0]
    assert "subject.entity_role" in neo4j.calls[0][0]


@pytest.mark.unit
def test_view_write_low_point_rechecks_structural_self_in_create_statement() -> None:
    neo4j = _AuthorityBoundaryNeo4j([[], [{"uuid": "view-1"}]])
    repository = ViewRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    result = repository._write_version(
        kind="admission_audit",
        key="ns::subject::audit",
        subject="subject",
        subject_uuid="ordinary-subject",
        name="audit",
        summary="audit",
        sig="sig-1",
        extra_props={},
        namespace="ns",
        valid_at="2026-09-05T00:00:00+00:00",
        source="test",
        source_confidence=0.6,
        episode_uuids=[],
        name_embedding=None,
    )

    assert result["created"] is True
    query, params, _kwargs = neo4j.calls[1]
    assert "subject_entity:Entity" in query
    assert "NOT (coalesce(subject_entity.is_self, false)" in query
    assert params["allow_canonical_self"] is False
    assert params["subject_uuid"] == "ordinary-subject"


@pytest.mark.unit
def test_correlation_edge_writer_blocks_structural_self_endpoints_in_enforce() -> None:
    neo4j = _AuthorityBoundaryNeo4j([[{"created": True}]])
    repository = CorrelationRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    assert repository.create_related_to_edge("a", "b", similarity=0.8) is True
    query, params, _kwargs = neo4j.calls[0]
    assert "coalesce(a.is_self, false)" in query
    assert "coalesce(b.is_self, false)" in query
    assert params["allow_canonical_self"] is False


@pytest.mark.unit
def test_merge_refuses_to_consume_canonical_self_adjacency_before_snapshot() -> None:
    eligibility_rows = [
        {
            "uuid": uuid,
            "ineligible_role": False,
            "namespace": "ns",
            "freshness": "ACTIVE",
            "scope": "PERSISTENT",
            "user_flagged": False,
            "conflict_status": None,
        }
        for uuid in ("survivor", "absorbed")
    ]
    neo4j = _AuthorityBoundaryNeo4j([eligibility_rows, [{"protected_count": 1}]])
    repository = CorrelationRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    result = repository.merge_entity("survivor", "absorbed", similarity=0.99)

    assert result["merged"] == 0
    assert result["reason"] == "CANONICAL_SELF_ADJACENCY_REQUIRES_AUTHORITY"
    assert len(neo4j.calls) == 2
    assert "EXISTS" in neo4j.calls[1][0]


@pytest.mark.unit
def test_merge_mutation_rechecks_self_adjacency_after_preflight() -> None:
    eligibility_rows = [
        {
            "uuid": uuid,
            "ineligible_role": False,
            "namespace": "ns",
            "freshness": "ACTIVE",
            "scope": "PERSISTENT",
            "user_flagged": False,
            "conflict_status": None,
        }
        for uuid in ("survivor", "absorbed")
    ]
    neo4j = _AuthorityBoundaryNeo4j([
        eligibility_rows,
        [{"protected_count": 0}],
        [{"uuid": "absorbed", "relationships": []}],
        [{"edges_bridged": 0, "episodes_rebound": 0, "deleted": 1}],
    ])
    repository = CorrelationRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    result = repository.merge_entity("survivor", "absorbed", similarity=0.99)

    assert result["merged"] == 1
    query, params, _kwargs = neo4j.calls[3]
    assert "survivor_self_neighbor:Entity" in query
    assert "absorbed_self_neighbor:Entity" in query
    assert "coalesce(neighbor.is_self, false)" in query
    assert params["allow_canonical_self"] is False


@pytest.mark.unit
def test_unmerge_restore_refuses_relationships_to_structural_self() -> None:
    neo4j = _AuthorityBoundaryNeo4j([[{"protected_count": 1}]])
    repository = CorrelationRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    result = repository.restore_merge_snapshot(
        survivor_uuid="survivor",
        absorbed_uuid="absorbed",
        absorbed_labels=["Entity"],
        absorbed_properties={"uuid": "absorbed"},
        out_rels=[{"peer_uuid": "self", "type": "RELATES_TO", "properties": {}}],
        in_rels=[],
        survivor_properties={},
        rebound_episodes=[],
        operation_id="restore-1",
    )

    assert result == {
        "restored": 0,
        "reason": "CANONICAL_SELF_RELATIONSHIP_RESTORE_REQUIRES_AUTHORITY",
    }
    assert len(neo4j.calls) == 1


@pytest.mark.unit
def test_consolidation_repair_and_lifecycle_low_points_guard_self_authority() -> None:
    neo4j = _AuthorityBoundaryNeo4j()
    repository = ConsolidationRepository(neo4j)
    repository.configure_canonical_self_binding_mode("enforce")

    repository.update_edge_facts([
        {"uuid": "edge-1", "fact": "synthetic", "fact_source": "synthetic_fallback"}
    ])
    repository.bridge_and_delete("node-1")
    repository.bridge_edges_for_node("node-1")
    repository.bridge_edges_for_nodes(["node-1"])
    repository.delete_session_nodes(["node-1"])
    repository.delete_entities_returning_uuids(["node-1"])

    assert len(neo4j.calls) == 6
    for query, params, _kwargs in neo4j.calls:
        assert params["allow_canonical_self"] is False
        assert "entity_role" in query
    repair_query = neo4j.calls[0][0]
    assert "menhir_self_authority_payload_json IS NULL" in repair_query
    for query, _params, _kwargs in neo4j.calls[1:]:
        assert "self_neighbor:Entity" in query


@pytest.mark.unit
def test_runtime_rejects_misspelled_self_authority_mode_and_wires_all_low_points() -> None:
    assert resolve_bind_mode("enfroce") is SelfBindMode.OFF
    with pytest.raises(ValueError, match="off, observe, enforce"):
        resolve_bind_mode("enfroce", strict=True)

    adapter = MemoryGraphAdapter(_AuthorityBoundaryNeo4j())
    with pytest.raises(ValueError, match="off, observe, enforce"):
        adapter.configure_canonical_self_binding_mode("enfroce")

    adapter.configure_canonical_self_binding_mode("enforce")
    assert adapter.canonical_self_binding_mode == "enforce"
    assert adapter._memory_queries._canonical_self_binding_mode is SelfBindMode.ENFORCE
    assert adapter._correlation._canonical_self_binding_mode is SelfBindMode.ENFORCE
    assert adapter._consolidation._canonical_self_binding_mode is SelfBindMode.ENFORCE


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative_path",
    [
        "api/server_support.py",
        "cli/bootstrap.py",
        "core/bootstrap.py",
        "core/runtime.py",
        "explorer/app.py",
    ],
)
def test_every_production_memory_adapter_construction_applies_authority_mode(
    relative_path,
) -> None:
    source = (
        Path(__file__).parents[1] / "src" / "menhir" / relative_path
    ).read_text(encoding="utf-8")
    assert source.count("MemoryGraphAdapter(") == 1
    direct_wiring = source.count(".configure_canonical_self_binding_mode(")
    compatibility_wiring = (
        (
            "configure_self_binding(self_binding_mode)" in source
            or "configure_self_binding(self_binding_mode.value)" in source
        )
        and '"configure_canonical_self_binding_mode"' in source
    )
    assert direct_wiring == 1 or compatibility_wiring


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failed_patch", "message"),
    [
        ("_patch_graphiti_combined_extraction", "combined extraction"),
        ("_patch_graphiti_self_authority_edge_resolution", "exact edge resolution"),
        ("_patch_graphiti_structural_candidate_isolation", "candidate isolation"),
        ("_patch_graphiti_adaptive_dedupe", "canonical dedupe"),
    ],
)
def test_enforce_startup_fails_when_any_authority_patch_is_unavailable(
    monkeypatch, failed_patch, message
) -> None:
    import menhir.infrastructure.graphiti_client as graphiti_client_module

    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    for patch_name in (
        "_patch_graphiti_prompt_json",
        "_patch_graphiti_combined_extraction_models",
        "_patch_graphiti_entity_extraction",
        "_patch_graphiti_dedupe_resolutions",
        "_patch_graphiti_dedup_prompt",
        "_patch_graphiti_dedup_identity_gate",
        "_patch_graphiti_untyped_attribute_preservation",
        "_patch_graphiti_dedup_branch_telemetry",
    ):
        monkeypatch.setattr(graphiti_client_module, patch_name, lambda: None)
    for patch_name in (
        "_patch_graphiti_combined_extraction",
        "_patch_graphiti_self_authority_edge_resolution",
        "_patch_graphiti_structural_candidate_isolation",
        "_patch_graphiti_adaptive_dedupe",
    ):
        monkeypatch.setattr(
            graphiti_client_module,
            patch_name,
            (lambda: False) if patch_name == failed_patch else (lambda: True),
        )

    with pytest.raises(RuntimeError, match=message):
        GraphitiClient.from_settings_with_capabilities(
            SimpleNamespace(canonical_self_binding_mode="enforce")
        )


@pytest.mark.unit
def test_recall_metadata_projects_view_subject_for_canonical_self_gate() -> None:
    assert "n.view_subject_uuid AS view_subject_uuid" in ENTITY_METADATA_FIELDS
    assert "n.view_subject AS view_subject" in ENTITY_METADATA_FIELDS
    assert "coalesce(n.is_self, false) AS is_self" in ENTITY_METADATA_FIELDS
    assert "n.entity_role AS entity_role" in ENTITY_METADATA_FIELDS
    assert "coalesce(n.namespace, n.group_id, 'default') AS namespace" in ENTITY_METADATA_FIELDS


@pytest.mark.unit
def test_service_builder_does_not_degrade_authority_patch_failure_in_enforce(
    monkeypatch,
) -> None:
    from menhir.config import MemorySettings
    from menhir.core.bootstrap import build_memory_services

    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: object())

    def _fail_authority_startup(*args, **kwargs):
        raise RuntimeError("canonical-self enforce mode requires Graphiti authority patches")

    monkeypatch.setattr(
        "menhir.core.bootstrap.GraphitiClient.from_settings_with_capabilities",
        _fail_authority_startup,
    )

    with pytest.raises(RuntimeError, match="requires Graphiti authority patches"):
        build_memory_services(MemorySettings(canonical_self_binding_mode="enforce"))


@pytest.mark.unit
def test_service_builder_refuses_degraded_graphiti_reads_in_enforce(
    monkeypatch,
) -> None:
    from menhir.config import MemorySettings
    from menhir.core.bootstrap import build_memory_services

    monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: object())

    with pytest.raises(RuntimeError, match="requires Graphiti-backed reads"):
        build_memory_services(
            MemorySettings(canonical_self_binding_mode="enforce"),
            capabilities=SimpleNamespace(reads_ready=False),
        )


@pytest.mark.unit
def test_typed_writer_queries_treat_every_structural_self_node_as_unbound() -> None:
    """Namespace spoofing cannot bypass the Python formula check at the database low point."""

    from menhir.infrastructure.typed_assertion_models import _RECORD_CYPHER
    from menhir.infrastructure.typed_event_repository import _RECORD_CYPHER as event_cypher

    for query in (_RECORD_CYPHER, event_cypher):
        assert "$allow_canonical_self OR" in query
        assert "NOT coalesce(n.is_self, false)" in query
        assert "coalesce(n.entity_role, '')" in query


@pytest.mark.unit
def test_proposal_receipt_write_is_lease_guarded_and_non_recallable() -> None:
    class CaptureNeo4j:
        def __init__(self) -> None:
            self.query = ""
            self.params = {}

        def execute(self, query, params=None):
            self.query = str(query)
            self.params = dict(params or {})
            return [{"updated": 1}]

    neo4j = CaptureNeo4j()
    proposal = _proposal()
    stored = EpisodeRepository(neo4j).record_self_assertion_proposals(
        "episode-1",
        worker_id="worker-1",
        proposals=[proposal.audit_record(
            # The durable receipt records only the verifier outcome, not signing material.
            SelfAuthorizationDecision(False, "confirmation_not_found")
        )],
        authorized_count=0,
        policy_version=proposal.policy_version,
    )
    assert stored is True
    assert "n.processing_state = 'ENRICHING'" in neo4j.query
    assert "n.processing_owner = $worker_id" in neo4j.query
    assert ":Entity" not in neo4j.query and "MERGE" not in neo4j.query
    assert neo4j.params["worker_id"] == "worker-1"
    assert "signature" not in neo4j.params["proposals_json"]


@pytest.mark.unit
def test_operator_reenrichment_only_reopens_ready_episode_with_pending_self_proposal() -> None:
    """The two-pass offline signing flow is usable without reopening arbitrary READY work."""

    class CaptureNeo4j:
        def __init__(self) -> None:
            self.query = ""

        def execute(self, query, params=None):
            self.query = str(query)
            return [{"reset": 1}]

    neo4j = CaptureNeo4j()
    assert EpisodeRepository(neo4j).force_reset_failed_episode("episode-1") is True
    assert "n.processing_state IN ['FAILED', 'PENDING']" in neo4j.query
    assert "n.processing_state = 'READY'" in neo4j.query
    assert "coalesce(n.self_assertion_proposal_count, 0) > 0" in neo4j.query
    assert (
        "coalesce(n.self_assertion_authorized_count, 0)"
        " < n.self_assertion_proposal_count"
    ) in neo4j.query
