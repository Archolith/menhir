"""Tests for combined-extraction endpoint closure, payload sanitation, and collapse detection.

Covers the menhir-side remediation of the ScalarStateView ingestion/provenance defect:
Graphiti's combined extractor dropped edges whose endpoints were absent from the extracted
entity set (then orphan-pruned the survivors), and rejected a whole payload for one malformed
row. These patches close missing endpoints and drop only malformed rows BEFORE resolution,
and surface a genuine collapse as a retryable failure instead of a masked empty-extraction
success.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.utils.maintenance.combined_extraction as ce  # noqa: E402
import menhir.infrastructure.graphiti_extraction_patches as extraction_patches  # noqa: E402
import menhir.infrastructure.graphiti_model_patches as model_patches  # noqa: E402
import menhir.infrastructure.graphiti_patches as patches  # noqa: E402
import menhir.services.enrichment_steps as steps  # noqa: E402
from menhir.domain.self_identity import (  # noqa: E402
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    SelfEvidenceKind,
    declare_self_subject,
    self_context_for_pending_episode,
    self_subject_endpoint_for_claim,
    self_uuid_for_namespace,
)
from menhir.domain.self_authority import (  # noqa: E402
    SELF_ASSERTION_EDGE_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY,
    SelfAuthorizationDecision,
    canonical_json_bytes,
    make_self_assertion_proposal,
)
from menhir.infrastructure.self_binding import (  # noqa: E402
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
)
from menhir.services.enrichment_failures import classify_enrichment_failure  # noqa: E402


@pytest.fixture(autouse=True)
def _install_and_reset() -> None:
    patches._patch_graphiti_combined_extraction_models()
    patches.clear_extraction_receipt()
    yield
    patches.clear_extraction_receipt()


def _build(payload: dict) -> object:
    return ce.CombinedExtraction(**payload)


def _subject_endpoint_claim(**changes) -> dict[str, object]:
    claim: dict[str, object] = {
        "uuid": "projection-1",
        "content": "I own 25 postcards.",
        "source": "user",
        "namespace": "default",
        "diff": None,
        "subject_endpoint_eligible": True,
        "is_evidence_projection": True,
        "evidence_projection_of": "turn-1",
        "turn_evidence_count": 1,
        "turn_evidence_uuid": "turn-1",
        "turn_evidence_role": "user",
        "turn_evidence_declarant": "user",
        "turn_evidence_text": "I own 25 postcards.",
        "turn_evidence_namespace": "default",
    }
    claim.update(changes)
    return claim


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_edge_payload_and_temporal_scope_survive_graphiti_resolution() -> None:
    proposal = make_self_assertion_proposal(
        principal_id="owner-1",
        namespace="default",
        episode_uuid="episode-1",
        turn_evidence_uuid="turn-1",
        evidence_text="I own 25 postcards.",
        lane="graphiti_edge",
        direction="self_to_entity",
        polarity="affirmed",
        assertion={
            "counterpart": {"labels": [], "name": "postcards", "uuid": "postcards"},
            "fact": "user owns 25 postcards",
            "predicate": "OWNS",
        },
        temporal_scope={"valid_at": None, "invalid_at": None, "expired_at": None},
    )
    payload_json = canonical_json_bytes(proposal.confirmation_payload()).decode("utf-8")
    authority_attributes = {
        SELF_ASSERTION_EDGE_EPISODE_PROPERTY: "episode-1",
        SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: "graphiti-episode",
        SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: payload_json,
    }
    edge = SimpleNamespace(
        source_node_uuid=self_uuid_for_namespace("default"),
        target_node_uuid="postcards",
        name="OWNS",
        fact="user owns 25 postcards",
        group_id="",
        episodes=["graphiti-episode"],
        attributes=dict(authority_attributes),
        valid_at=None,
        invalid_at=None,
        expired_at=None,
    )
    receipt = patches.begin_extraction_receipt(
        "episode-1",
        "I own 25 postcards.",
        self_identity=SimpleNamespace(namespace="default"),
        self_bind_mode=SelfBindMode.ENFORCE,
        self_assertion_authorizer=_OwnerAuthorizer(),
    )
    receipt.self_assertion_authorized_edge_ids.add(id(edge))
    receipt.graphiti_episode_uuid = "graphiti-episode"
    receipt.self_assertion_counterpart_by_edge_id[id(edge)] = "postcards"
    receipt.resolved_node_identity_by_extracted_uuid["postcards"] = (
        "postcards",
        "postcards",
        (),
    )
    observed = {}

    async def mutating_resolver(
        llm_client, extracted_edge, related_edges, existing_edges, episode,
        edge_type_candidates=None,
    ):
        observed["related"] = related_edges
        observed["existing"] = existing_edges
        extracted_edge.attributes = {}
        extracted_edge.valid_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        extracted_edge.name = "BORROWS"
        extracted_edge.fact = "user borrows postcards"
        return extracted_edge, [SimpleNamespace(uuid="invalidated")], []

    wrapped = extraction_patches._wrap_self_authority_edge_resolver(mutating_resolver)
    resolved, invalidated, duplicates = await wrapped(
        object(),
        edge,
        [SimpleNamespace(source_node_uuid="x")],
        [SimpleNamespace(source_node_uuid="y")],
        SimpleNamespace(uuid="graphiti-episode"),
        None,
    )

    assert observed == {"related": [], "existing": []}
    assert resolved.attributes == authority_attributes
    assert resolved.episodes == ["graphiti-episode"]
    assert resolved.valid_at is None
    assert resolved.name == "OWNS"
    assert resolved.fact == "user owns 25 postcards"
    assert invalidated == [] and duplicates == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_edge_resolution_reuses_only_exact_existing_edge() -> None:
    proposal = make_self_assertion_proposal(
        principal_id="owner-1",
        namespace="default",
        episode_uuid="episode-1",
        turn_evidence_uuid="turn-1",
        evidence_text="I own 25 postcards.",
        lane="graphiti_edge",
        direction="self_to_entity",
        polarity="affirmed",
        assertion={
            "counterpart": {"labels": [], "name": "postcards", "uuid": "postcards"},
            "fact": "user owns 25 postcards",
            "predicate": "OWNS",
        },
        temporal_scope={"valid_at": None, "invalid_at": None, "expired_at": None},
    )
    payload_json = canonical_json_bytes(proposal.confirmation_payload()).decode("utf-8")
    authority_attributes = {
        SELF_ASSERTION_EDGE_EPISODE_PROPERTY: "episode-1",
        SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: "graphiti-episode",
        SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: payload_json,
    }
    edge = SimpleNamespace(
        source_node_uuid=self_uuid_for_namespace("default"),
        target_node_uuid="postcards",
        name="OWNS",
        fact="user owns 25 postcards",
        group_id="",
        episodes=["graphiti-episode"],
        attributes=dict(authority_attributes),
        valid_at=None,
        invalid_at=None,
        expired_at=None,
    )
    existing = SimpleNamespace(
        source_node_uuid=edge.source_node_uuid,
        target_node_uuid=edge.target_node_uuid,
        name=edge.name,
        fact=edge.fact,
        group_id=edge.group_id,
        attributes={"legacy": "unsafe"},
        valid_at=None,
        invalid_at=None,
        expired_at=None,
        episodes=[],
    )
    receipt = patches.begin_extraction_receipt(
        "episode-1",
        "I own 25 postcards.",
        self_identity=SimpleNamespace(namespace="default"),
        self_bind_mode=SelfBindMode.ENFORCE,
        self_assertion_authorizer=_OwnerAuthorizer(),
    )
    receipt.self_assertion_authorized_edge_ids.add(id(edge))
    receipt.graphiti_episode_uuid = "graphiti-episode"
    receipt.self_assertion_counterpart_by_edge_id[id(edge)] = "postcards"
    receipt.resolved_node_identity_by_extracted_uuid["postcards"] = (
        "postcards",
        "postcards",
        (),
    )

    async def should_not_run(*args, **kwargs):
        raise AssertionError("exact authorized replay should not invoke Graphiti edge resolution")

    wrapped = extraction_patches._wrap_self_authority_edge_resolver(should_not_run)
    resolved, invalidated, duplicates = await wrapped(
        object(), edge, [existing], [], SimpleNamespace(uuid="graphiti-episode"), None
    )

    assert resolved is existing
    assert existing.attributes == authority_attributes
    assert existing.episodes == ["graphiti-episode"]
    assert invalidated == [] and duplicates == []


class _OwnerAuthorizer:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized
        self.proposals = []

    def authorize(self, proposal):
        self.proposals.append(proposal)
        return SelfAuthorizationDecision(
            self.authorized,
            "owner_signature_verified" if self.authorized else "confirmation_no_exact_match",
            "ed25519:test-owner",
        )


def _begin_subject_endpoint_receipt(*, authorized: bool = True):
    claim = _subject_endpoint_claim()
    endpoint = self_subject_endpoint_for_claim(claim)
    assert endpoint is not None
    identity = self_context_for_pending_episode(
        source="user",
        namespace="default",
        episode_uuid="projection-1",
        turn_evidence_uuid="turn-1",
        principal_id="owner-1",
    )
    authorizer = _OwnerAuthorizer(authorized)
    patches.begin_extraction_receipt(
        "projection-1",
        str(claim["content"]),
        source_description="user",
        self_identity=identity,
        self_subject_endpoint=endpoint,
        self_bind_mode=SelfBindMode.ENFORCE,
        self_assertion_authorizer=authorizer,
    )
    return endpoint


def test_missing_source_endpoint_is_synthesized() -> None:
    """Acceptance #1: Alice's coins + Alice->OWNS->Alice's coins yields two nodes and one edge."""
    patches.begin_extraction_receipt("ep1", "user: Alice owns 37 coins.")
    obj = _build(
        {
            "extracted_entities": [{"name": "Alice's coins", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": "Alice",
                    "target_entity_name": "Alice's coins",
                    "relation_type": "OWNS",
                    "fact": "Alice owns 37 coins",
                    "episode_indices": [0],
                }
            ],
        }
    )
    names = sorted(e.name for e in obj.extracted_entities)
    assert names == ["Alice", "Alice's coins"]
    assert len(obj.edges) == 1
    receipt = patches.get_extraction_receipt()
    assert receipt.endpoints_synthesized == 1
    assert receipt.raw_entity_count == 1
    assert receipt.raw_edge_count == 1


def test_missing_target_endpoint_is_synthesized() -> None:
    """Acceptance #2: closure is symmetric for a missing target endpoint."""
    patches.begin_extraction_receipt("ep2", "user: Alice wrote the novel.")
    obj = _build(
        {
            "extracted_entities": [{"name": "Alice", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": "Alice",
                    "target_entity_name": "the novel",
                    "relation_type": "WROTE",
                    "fact": "Alice wrote the novel",
                    "episode_indices": [0],
                }
            ],
        }
    )
    assert sorted(e.name for e in obj.extracted_entities) == ["Alice", "the novel"]
    assert len(obj.edges) == 1
    assert patches.get_extraction_receipt().endpoints_synthesized == 1


def test_case_and_whitespace_equivalent_endpoint_does_not_duplicate() -> None:
    """Acceptance #3: a case/whitespace variant of an existing entity is not synthesized twice."""
    patches.begin_extraction_receipt("ep3", "user: alice knows Alice.")
    obj = _build(
        {
            "extracted_entities": [{"name": "Alice", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": "  alice ",
                    "target_entity_name": "Alice",
                    "relation_type": "KNOWS",
                    "fact": "alice knows Alice",
                    "episode_indices": [0],
                }
            ],
        }
    )
    assert len(obj.extracted_entities) == 1
    assert patches.get_extraction_receipt().endpoints_synthesized == 0


def test_synthesized_endpoint_gets_generic_entity_type() -> None:
    """Acceptance #4: synthesized endpoints use entity_type_id=-1 (generic Entity), not 0."""
    patches.begin_extraction_receipt("ep4", "user: Alice owns 37 coins.")
    obj = _build(
        {
            "extracted_entities": [{"name": "Alice's coins", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": "Alice",
                    "target_entity_name": "Alice's coins",
                    "relation_type": "OWNS",
                    "fact": "Alice owns 37 coins",
                    "episode_indices": [0],
                }
            ],
        }
    )
    alice = next(e for e in obj.extracted_entities if e.name == "Alice")
    assert alice.entity_type_id == -1


def _one_edge_from(pronoun: str, episode_text: str):
    patches.begin_extraction_receipt("ep5", episode_text)
    return _build(
        {
            "extracted_entities": [{"name": "coins", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": pronoun,
                    "target_entity_name": "coins",
                    "relation_type": "OWNS",
                    "fact": f"{pronoun} own coins",
                    "episode_indices": [0],
                }
            ],
        }
    )


@pytest.mark.parametrize("pronoun", ["you", "we", "they", "he", "she", "it"])
def test_ambiguous_pronoun_endpoints_are_not_synthesized(pronoun: str) -> None:
    """Acceptance #5 (unchanged): genuinely ambiguous pronouns are never minted as identities."""
    obj = _one_edge_from(pronoun, f"user: {pronoun} own 37 coins.")
    assert all(e.name.lower() != pronoun.lower() for e in obj.extracted_entities)
    assert patches.get_extraction_receipt().endpoints_synthesized == 0


@pytest.mark.parametrize("label", ["user", "the user", "I", "me", "my"])
def test_self_like_endpoints_are_retained_as_ordinary_user_entities(label: str) -> None:
    """Self-like labels survive endpoint closure but gain no canonical identity authority.

    This REPLACES the half of the old acceptance #5 that also refused these. Refusing them was
    destroying the user's own facts: gpt-4o-mini emits the speaker as the literal token `user` on
    every turn, so each such edge lost an endpoint, graphiti dropped it, and then orphan-pruned
    every node those edges would have connected -- collapsing the episode to zero
    (CombinedExtractionCollapsedError). Measured on the cc5ded98 smoke: 5 of 6 user turns.
    """
    obj = _one_edge_from(label, f"user: {label} own 37 coins.")
    names = {e.name.lower() for e in obj.extracted_entities}
    assert "user" in names, f"{label!r} should be retained as an ordinary user entity"
    edge = obj.edges[0]
    assert edge.source_entity_name.lower() == "user", "the edge endpoint must be rewritten too"


@pytest.mark.parametrize("label", ["I", "me", "my"])
def test_first_person_on_an_assistant_turn_is_not_bound_to_the_user(label: str) -> None:
    """On an assistant turn `I` is the MODEL, so binding it to the human would misattribute.

    No self-like endpoint is retained on an assistant turn; those edges are handled by the
    self-echo suppression policy below.
    """
    obj = _one_edge_from(label, f"assistant: {label} am an AI trained on data.")
    assert all(e.name.lower() != "user" for e in obj.extracted_entities)


@pytest.mark.parametrize("label", ["user", "the user", "I", "me", "my"])
def test_no_self_binding_on_assistant_turns(label: str) -> None:
    """`user -> X` on an assistant turn is ECHO of the user's own turn -- do not mint a duplicate.

    On cc5ded98 the same fact appeared twice: turn 8 (user, first-hand) and turn 9 (assistant,
    paraphrased). Binding on assistant turns stores the second-hand copy alongside the original.
    Entity-to-entity facts on assistant turns are unaffected -- only the self-anchored echo is
    dropped -- so `single-session-assistant` answers (a recommended restaurant, etc.) still land.
    """
    obj = _one_edge_from(label, f"assistant: {label} own 37 coins.")
    assert all(e.name.lower() != "user" for e in obj.extracted_entities)


@pytest.mark.parametrize(
    "raw,malformed,suppressed,expected",
    [
        (4, 0, 4, True),    # pure echo: every usable edge suppressed by policy
        (4, 0, 0, False),   # REAL collapse: nothing suppressed -- must stay retryable
        (4, 0, 2, False),   # partial echo: real edges were also lost -- still a collapse
        (0, 0, 0, False),   # genuinely empty extraction, handled by the existing path
        (4, 1, 3, True),    # echo accounts for every edge that survived malformed-dropping
        (4, 4, 0, False),   # all malformed, no echo -- not a policy decision
    ],
)
def test_policy_empty_never_masks_a_real_collapse(
    raw: int, malformed: int, suppressed: int, expected: bool
) -> None:
    """The success path must open ONLY when policy explains every lost edge.

    If this predicate is too permissive it silently converts real data loss into reported success --
    exactly the failure mode that hid this bug for a week (collapse counted as zero-extraction
    success). The `suppressed=0` row is the one that matters.
    """
    receipt = patches.CombinedExtractionReceipt(
        raw_edge_count=raw,
        malformed_edges_dropped=malformed,
        self_echo_edges_suppressed=suppressed,
    )
    assert patches.is_policy_empty_extraction(receipt) is expected


def test_assistant_turn_entity_to_entity_facts_still_survive() -> None:
    """The exclusion must be surgical: a recommendation on an assistant turn is still ingested."""
    patches.begin_extraction_receipt("ep9", "assistant: Roscioli is a great Italian restaurant.")
    obj = _build(
        {
            "extracted_entities": [
                {"name": "Roscioli", "entity_type_id": 0},
                {"name": "Italian restaurant", "entity_type_id": 0},
            ],
            "edges": [
                {
                    "source_entity_name": "Roscioli",
                    "target_entity_name": "Italian restaurant",
                    "relation_type": "IS_A",
                    "fact": "Roscioli is an Italian restaurant",
                    "episode_indices": [0],
                }
            ],
        }
    )
    names = {e.name.lower() for e in obj.extracted_entities}
    assert {"roscioli", "italian restaurant"} <= names
    assert len(obj.edges) == 1


@pytest.mark.parametrize(
    "name", ["Alice", "Bob", "Sarah", "my car", "my wife", "my dog", "Alice's coins"])
def test_named_and_owned_entities_are_never_folded_into_the_user(name: str) -> None:
    """Self-binding must match ONLY bare self tokens -- never a person or an owned object.

    Folding `Alice` or `my car` into the canonical `user` node would silently merge distinct
    identities and attribute their properties to the human, which is far worse than the collapse
    the binding fixes. Mirrors the typed-scalar prompt's own rule that `my car is red` has
    subject `my car`, NOT `user`.
    """
    obj = _one_edge_from(name, f"user: {name} owns 37 coins.")
    names = {e.name.lower() for e in obj.extracted_entities}
    assert name.lower() in names, f"{name!r} must survive as its own entity"
    assert obj.edges[0].source_entity_name.lower() == name.lower(), "endpoint must not be rewritten"


def test_endpoint_absent_from_episode_text_is_not_synthesized() -> None:
    """A non-pronoun name that does not appear in the episode text is not synthesized."""
    patches.begin_extraction_receipt("ep6", "user: Alice owns 37 coins.")
    obj = _build(
        {
            "extracted_entities": [{"name": "Alice's coins", "entity_type_id": 0}],
            "edges": [
                {
                    "source_entity_name": "Bob",  # not present in episode text
                    "target_entity_name": "Alice's coins",
                    "relation_type": "GAVE",
                    "fact": "Bob gave the coins",
                    "episode_indices": [0],
                }
            ],
        }
    )
    assert all(e.name != "Bob" for e in obj.extracted_entities)
    assert patches.get_extraction_receipt().endpoints_synthesized == 0


@pytest.mark.asyncio
async def test_endpoint_grounded_in_previous_episode_context_is_synthesized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved pronoun antecedent may close an endpoint omitted from the entity list.

    Regression for the LongMemEval turn "She moved to Chicago.": the combined extractor used
    its previous-episode context to resolve "She" to Rachel, returned the valid Rachel->Chicago
    edge, but omitted Rachel from ``extracted_entities``. Refusing every name absent from the
    current sentence dropped the edge and then orphan-pruned Chicago, deterministically collapsing
    both the main episode and its evidence projection.
    """
    patches.begin_extraction_receipt("ep-context", "user: She moved to Chicago.")

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        obj = _build(
            {
                "extracted_entities": [{"name": "Chicago", "entity_type_id": 0}],
                "edges": [
                    {
                        "source_entity_name": "Rachel",
                        "target_entity_name": "Chicago",
                        "relation_type": "LIVES_IN",
                        "fact": "Rachel moved to Chicago.",
                        "episode_indices": [0],
                    }
                ],
            }
        )
        return obj.extracted_entities, obj.edges, {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-context"),
        previous_episodes=[
            SimpleNamespace(content="The user is planning to visit her friend Rachel.")
        ],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert sorted(node.name for node in nodes) == ["Chicago", "Rachel"]
    assert len(edges) == 1
    receipt = patches.get_extraction_receipt()
    assert receipt.previous_episode_texts == (
        "The user is planning to visit her friend Rachel.",
    )
    assert receipt.endpoints_synthesized == 1
    assert receipt.orphan_nodes_dropped == 0


@pytest.mark.asyncio
async def test_exact_app_turn_gets_one_relationless_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 5/5 gpt-4o-mini miss gets a focused repair instead of terminal attempt one.

    Exact failed LongMemEval evidence atom from namespace ``lme-d7c942c3``. The production model
    returned one entity (``new app``) and zero edges on five isolated temperature-zero trials even
    though the sentence explicitly states ``user USES new app``.
    """
    turn = "I'm actually using a new app I recently downloaded."
    patches.begin_extraction_receipt("ep-exact-app", turn, source_description="user")
    calls: list[str] = []
    edge = object()

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        receipt = patches.get_extraction_receipt()
        if len(calls) == 1:
            receipt.raw_entity_count = 1
            receipt.raw_edge_count = 0
            return [], [], {}
        receipt.raw_entity_count = 2
        receipt.raw_edge_count = 1
        return [SimpleNamespace(name="user"), SimpleNamespace(name="new app")], [edge], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-exact-app"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions="CALLER CONTRACT",
    )

    assert len(calls) == 2
    assert "CALLER CONTRACT" in calls[0]
    assert turn in calls[0]
    assert "CORRECTIVE RE-EXTRACTION" not in calls[0]
    assert "CORRECTIVE RE-EXTRACTION" in calls[1]
    assert [node.name for node in nodes] == ["user", "new app"]
    assert edges == [edge]
    receipt = patches.get_extraction_receipt()
    assert receipt.source_description == "user"
    assert receipt.relationless_repair_attempted is True
    assert receipt.relationless_repair_succeeded is True
    assert receipt.relationless_initial_entity_count == 1
    assert receipt.relationless_initial_edge_count == 0


@pytest.mark.asyncio
async def test_exact_parental_leave_interest_turn_gets_relationless_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stopped LME turn explicitly states informational intent toward two companies."""
    turn = (
        "I'd like to know more about the parental leave policies at these companies. "
        "Can you tell me more about the policies at Goldman Sachs and Accenture?"
    )
    patches.begin_extraction_receipt("ep-parental-leave-interest", turn, source_description="user")
    calls: list[str] = []

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        if len(calls) == 1:
            obj = _build(
                {
                    "extracted_entities": [
                        {"name": "user", "entity_type_id": 0},
                        {"name": "Goldman Sachs", "entity_type_id": 0},
                        {"name": "Accenture", "entity_type_id": 0},
                    ],
                    "edges": [],
                }
            )
            return obj.extracted_entities, obj.edges, {}
        obj = _build(
            {
                "extracted_entities": [
                    {"name": "user", "entity_type_id": 0},
                    {"name": "Goldman Sachs", "entity_type_id": 0},
                    {"name": "Accenture", "entity_type_id": 0},
                ],
                "edges": [
                    {
                        "source_entity_name": "user",
                        "target_entity_name": "Goldman Sachs",
                        "relation_type": "WANTS_TO_KNOW_MORE_ABOUT",
                        "fact": "The user wants to know more about Goldman Sachs parental leave policies.",
                        "episode_indices": [0],
                    },
                    {
                        "source_entity_name": "user",
                        "target_entity_name": "Accenture",
                        "relation_type": "WANTS_TO_KNOW_MORE_ABOUT",
                        "fact": "The user wants to know more about Accenture parental leave policies.",
                        "episode_indices": [0],
                    },
                ],
            }
        )
        return obj.extracted_entities, obj.edges, {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-parental-leave-interest"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(calls) == 2
    for instructions in calls:
        normalized = " ".join(instructions.split())
        assert "I'd like to know more about X" in normalized
        assert "WANTS_TO_KNOW_MORE_ABOUT" in normalized
        assert "Can you tell me about X?" in normalized
    assert sorted(node.name for node in nodes) == ["Accenture", "Goldman Sachs", "user"]
    assert [edge.relation_type for edge in edges] == [
        "WANTS_TO_KNOW_MORE_ABOUT",
        "WANTS_TO_KNOW_MORE_ABOUT",
    ]
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_attempted is True
    assert receipt.relationless_repair_succeeded is True
    assert receipt.relationless_initial_entity_count == 3
    assert receipt.relationless_initial_edge_count == 0


@pytest.mark.asyncio
async def test_bare_information_question_contract_does_not_infer_interest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question without an explicit first-person interest remains an intentional empty result."""
    turn = "Can you tell me about parental leave policies at Accenture?"
    patches.begin_extraction_receipt("ep-bare-information-question", turn, source_description="user")
    calls: list[str] = []

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        obj = _build({"extracted_entities": [], "edges": []})
        return obj.extracted_entities, obj.edges, {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-bare-information-question"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(calls) == 1
    normalized = " ".join(calls[0].split())
    assert "Can you tell me about X?" in normalized
    assert "does not by itself assert durable interest" in normalized
    assert nodes == []
    assert edges == []
    assert patches.get_extraction_receipt().relationless_repair_attempted is False


@pytest.mark.asyncio
async def test_bare_numeric_reply_repairs_with_adjacent_transcript_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the stopped LongMemEval business-card episode.

    The current turn carries a selection but not its noun. Graphiti's ordinary previous episodes
    omit the context-only assistant turn, so the first pass returned ``user`` + ``100`` and no edge.
    Only the already-budgeted corrective pass may load the adjacent raw transcript, and endpoints
    emitted from that context must pass the same grounding guard as ordinary previous episodes.
    """

    current = "user: I think 100 is a good starting point."
    context_loads = 0

    def load_context() -> tuple[str, ...]:
        nonlocal context_loads
        context_loads += 1
        return (
            "user: I need to order more business cards. Do you think 100 or 200 is a good amount?",
            "assistant: Starting with 100 business cards sounds reasonable.",
        )

    patches.begin_extraction_receipt(
        "24bd4f72-0b8e-4b03-abeb-074b72250b8b",
        current,
        source_description="user",
        relationless_repair_context_loader=load_context,
    )
    calls: list[str] = []
    previous_contexts: list[list[str]] = []

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        previous_contexts.append([item.content for item in args[2]])
        if len(calls) == 1:
            obj = _build(
                {
                    "extracted_entities": [
                        {"name": "user", "entity_type_id": 0},
                        {"name": "100", "entity_type_id": 0},
                    ],
                    "edges": [],
                }
            )
            return obj.extracted_entities, obj.edges, {}
        obj = _build(
            {
                "extracted_entities": [{"name": "user", "entity_type_id": 0}],
                "edges": [
                    {
                        "source_entity_name": "user",
                        "target_entity_name": "business cards",
                        "relation_type": "PLANS_ORDER_QUANTITY",
                        "fact": "The user selected 100 as the starting business-card order quantity.",
                        "episode_indices": [0],
                    }
                ],
            }
        )
        return obj.extracted_entities, obj.edges, {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="24bd4f72-0b8e-4b03-abeb-074b72250b8b"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(calls) == 2
    assert context_loads == 1
    assert "ADJACENT TRANSCRIPT CONTEXT" not in calls[0]
    assert previous_contexts[0] == []
    assert previous_contexts[1] == [
        "user: I need to order more business cards. Do you think 100 or 200 is a good amount?",
        "assistant: Starting with 100 business cards sounds reasonable.",
    ]
    assert "I need to order more business cards" not in calls[1]
    assert "Starting with 100 business cards" not in calls[1]
    assert "ADJACENT TRANSCRIPT CONTEXT" in calls[1]
    assert (
        "Emit relationships only for claims or choices made in CURRENT MESSAGES"
        in " ".join(calls[1].split())
    )
    assert sorted(node.name for node in nodes) == ["business cards", "user"]
    assert len(edges) == 1
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_succeeded is True
    assert receipt.relationless_repair_context_texts == (
        "user: I need to order more business cards. Do you think 100 or 200 is a good amount?",
        "assistant: Starting with 100 business cards sounds reasonable.",
    )
    assert receipt.endpoints_synthesized == 1


@pytest.mark.asyncio
async def test_context_assisted_repair_suppresses_a_preceding_claim_copied_into_thanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native context must resolve the current turn, never become the current turn's content."""

    current = "user: Thanks for the advice."
    patches.begin_extraction_receipt(
        "ep-context-copy-control",
        current,
        source_description="user",
        relationless_repair_context_loader=lambda: (
            "user: I need to order more business cards.",
            "assistant: Starting with 100 business cards sounds reasonable.",
        ),
    )
    calls = 0

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            obj = _build(
                {
                    "extracted_entities": [{"name": "user", "entity_type_id": 0}],
                    "edges": [],
                }
            )
            return obj.extracted_entities, obj.edges, {}
        obj = _build(
            {
                "extracted_entities": [
                    {"name": "user", "entity_type_id": 0},
                    {"name": "business cards", "entity_type_id": 0},
                ],
                "edges": [
                    {
                        "source_entity_name": "user",
                        "target_entity_name": "business cards",
                        "relation_type": "NEEDS_TO_ORDER",
                        "fact": "The user needs to order more business cards.",
                        "episode_indices": [0],
                    }
                ],
            }
        )
        # Graphiti drops orphan entities after the sanitizer removes the only edge.
        return [], obj.edges, {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-context-copy-control"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert calls == 2
    assert nodes == []
    assert edges == []
    receipt = patches.get_extraction_receipt()
    assert receipt.context_unsupported_edges_suppressed == 1
    assert receipt.relationless_repair_succeeded is False
    assert receipt.initial_self_only_entities is True
    assert patches.is_policy_empty_extraction(receipt) is True


@pytest.mark.asyncio
async def test_relationless_repair_is_bounded_to_one_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair returning empty stays visibly failed; it never loops or erases the first underflow."""
    patches.begin_extraction_receipt(
        "ep-still-relationless",
        "A phrase with one entity but no grounded relationship.",
    )
    calls = 0

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        calls += 1
        receipt = patches.get_extraction_receipt()
        receipt.raw_entity_count = 1 if calls == 1 else 0
        receipt.raw_edge_count = 0
        return [], [], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-still-relationless"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert calls == 2
    assert nodes == []
    assert edges == []
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_attempted is True
    assert receipt.relationless_repair_succeeded is False
    assert receipt.raw_entity_count == 1
    assert receipt.raw_edge_count == 0


@pytest.mark.asyncio
async def test_relationship_bearing_first_pass_does_not_pay_for_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common successful path stays one call; repair cost is paid only on underflow."""
    context_loads = 0

    def load_context() -> tuple[str, ...]:
        nonlocal context_loads
        context_loads += 1
        return ("assistant: irrelevant",)

    patches.begin_extraction_receipt(
        "ep-complete",
        "user: I use a grocery list app.",
        relationless_repair_context_loader=load_context,
    )
    calls = 0
    edge = object()

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        calls += 1
        receipt = patches.get_extraction_receipt()
        receipt.raw_entity_count = 2
        receipt.raw_edge_count = 1
        return [SimpleNamespace(name="user"), SimpleNamespace(name="grocery list app")], [edge], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    _, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-complete"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert calls == 1
    assert context_loads == 0
    assert edges == [edge]
    assert patches.get_extraction_receipt().relationless_repair_attempted is False


@pytest.mark.asyncio
async def test_assistant_self_only_relationless_is_policy_empty_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic assistant boilerplate must not fail a namespace or pay for a futile repair."""
    turn = (
        "assistant: I'd be happy to help you prioritize your work tasks. "
        "Can you please share the tasks you need to prioritize?"
    )
    patches.begin_extraction_receipt("ep-assistant-boilerplate", turn)
    calls = 0

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        calls += 1
        receipt = patches.get_extraction_receipt()
        receipt.raw_entity_count = 1
        receipt.raw_edge_count = 0
        receipt.assistant_self_only_relationless = True
        return [], [], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="ep-assistant-boilerplate"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert calls == 1
    assert nodes == []
    assert edges == []
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_attempted is False
    assert patches.is_policy_empty_extraction(receipt) is True


def test_assistant_self_only_payload_sets_policy_receipt() -> None:
    """The sanitizer recognizes the exact live response shape before the wrapper decides."""
    receipt = patches.begin_extraction_receipt(
        "ep-assistant-boilerplate-payload",
        "assistant: Can you please share the tasks you need to prioritize?",
    )
    _build(
        {
            "extracted_entities": [{"name": "user", "entity_type_id": 0}],
            "edges": [],
        }
    )

    assert receipt.assistant_self_only_relationless is True
    assert patches.is_policy_empty_extraction(receipt) is True


def test_assistant_self_only_policy_does_not_mask_nonself_relationless_content() -> None:
    """Only canonical self labels qualify; a named entity with a missed relation still fails."""
    receipt = patches.begin_extraction_receipt(
        "ep-assistant-recommendation",
        "assistant: You should try Roscioli.",
    )
    _build(
        {
            "extracted_entities": [{"name": "Roscioli", "entity_type_id": 0}],
            "edges": [],
        }
    )

    assert receipt.assistant_self_only_relationless is False
    assert patches.is_policy_empty_extraction(receipt) is False


def test_assistant_self_only_policy_never_applies_to_user_turn() -> None:
    """A first-hand user turn with a missed predicate must still take the repair/failure path."""
    receipt = patches.begin_extraction_receipt(
        "ep-user-self-only",
        "user: I'm actually using a new app I recently downloaded.",
    )
    _build(
        {
            "extracted_entities": [{"name": "user", "entity_type_id": 0}],
            "edges": [],
        }
    )

    assert receipt.assistant_self_only_relationless is False
    assert patches.is_policy_empty_extraction(receipt) is False


async def _run_two_pass_extraction(
    monkeypatch: pytest.MonkeyPatch,
    episode_key: str,
    payloads: list[dict],
) -> int:
    """Drive the real wrapper + sanitizer over a scripted sequence of raw model payloads.

    Each payload goes through `CombinedExtraction`, so the receipt's per-pass self-only flags are
    written by production code rather than set by the test -- the point under test is precisely
    WHICH pass each flag records.
    """
    calls = 0

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        _build(payloads[min(calls, len(payloads) - 1)])
        calls += 1
        return [], [], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid=episode_key),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    return calls


@pytest.mark.asyncio
async def test_self_only_repair_does_not_mask_a_content_bearing_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair that degrades to `user` must not convert the first pass's content into success.

    The first pass extracted `Seattle` -- real content that resolution then persisted nowhere. If
    the policy-empty guard reads only the LAST pass's shape, the repair's `{"name": "user"}`
    silently relabels that loss as an intentional no-op and the fact is gone with no failure to
    retry or investigate. Both passes must independently be self-only.
    """
    patches.begin_extraction_receipt("ep-initial-content", "user: I moved to Seattle last month.")
    calls = await _run_two_pass_extraction(
        monkeypatch,
        "ep-initial-content",
        [
            {"extracted_entities": [{"name": "Seattle", "entity_type_id": 0}], "edges": []},
            {"extracted_entities": [{"name": "user", "entity_type_id": 0}], "edges": []},
        ],
    )

    assert calls == 2, "the content-bearing relationless first pass still earns its one repair"
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_attempted is True
    assert receipt.relationless_repair_succeeded is False
    assert patches.is_policy_empty_extraction(receipt) is False
    assert receipt.initial_self_only_entities is False
    assert receipt.repair_self_only_entities is True


@pytest.mark.asyncio
async def test_both_passes_self_only_is_an_intentional_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two passes agreeing on `user` with zero edges is the real no-op this guard exists for.

    An evidence projection of "Thanks again for your help!" bypasses the adaptive segmenter and
    correctly extracts only the canonical self label. Failing it would burn the retry budget on an
    outcome that is right and cannot change.
    """
    patches.begin_extraction_receipt("ep-thanks", "user: Thanks again for your help!")
    self_only = {"extracted_entities": [{"name": "user", "entity_type_id": 0}], "edges": []}
    calls = await _run_two_pass_extraction(monkeypatch, "ep-thanks", [self_only, self_only])

    assert calls == 2
    receipt = patches.get_extraction_receipt()
    assert receipt.relationless_repair_attempted is True
    assert receipt.relationless_repair_succeeded is False
    assert patches.is_policy_empty_extraction(receipt) is True
    assert receipt.initial_self_only_entities is True
    assert receipt.repair_self_only_entities is True


def test_malformed_edge_dropped_valid_sibling_survives() -> None:
    """Acceptance #6: one edge missing target is dropped; the valid sibling still builds."""
    patches.begin_extraction_receipt("ep7", "user: Bob works at Acme and owns a bike.")
    obj = _build(
        {
            "extracted_entities": [
                {"name": "Bob", "entity_type_id": 0},
                {"name": "Acme", "entity_type_id": 0},
                {"name": "bike", "entity_type_id": 0},
            ],
            "edges": [
                {
                    "source_entity_name": "Bob",
                    "relation_type": "WORKS_AT",
                    "fact": "Bob works at Acme",
                    "episode_indices": [0],
                },  # missing target_entity_name -> drop this row only
                {
                    "source_entity_name": "Bob",
                    "target_entity_name": "bike",
                    "relation_type": "OWNS",
                    "fact": "Bob owns a bike",
                    "episode_indices": [0],
                },
            ],
        }
    )
    assert len(obj.edges) == 1
    assert patches.get_extraction_receipt().malformed_edges_dropped == 1


def test_no_active_receipt_passes_payload_through_unchanged() -> None:
    """Scoping: with no active receipt the validator must not alter other callers' payloads.

    A malformed edge (missing target) still raises upstream ValidationError, proving the
    hardening did not run outside Menhir's forced single-episode path.
    """
    patches.clear_extraction_receipt()
    with pytest.raises(Exception):
        _build(
            {
                "extracted_entities": [{"name": "X", "entity_type_id": 0}],
                "edges": [{"source_entity_name": "A", "relation_type": "R", "fact": "f"}],
            }
        )


@pytest.mark.asyncio
async def test_receipts_are_isolated_between_concurrent_tasks() -> None:
    """Acceptance #9: concurrent episode tasks do not share receipt state."""

    async def run_one(key: str, entity: str) -> tuple[str, int]:
        patches.begin_extraction_receipt(key, f"user: {entity} owns coins.")
        await asyncio.sleep(0)
        _build(
            {
                "extracted_entities": [{"name": f"{entity}'s coins", "entity_type_id": 0}],
                "edges": [
                    {
                        "source_entity_name": entity,
                        "target_entity_name": f"{entity}'s coins",
                        "relation_type": "OWNS",
                        "fact": f"{entity} owns coins",
                        "episode_indices": [0],
                    }
                ],
            }
        )
        await asyncio.sleep(0)
        receipt = patches.get_extraction_receipt()
        return receipt.episode_key, receipt.endpoints_synthesized

    (key_a, synth_a), (key_b, synth_b) = await asyncio.gather(
        run_one("alice", "Alice"), run_one("bob", "Bob")
    )
    assert key_a == "alice" and key_b == "bob"
    assert synth_a == 1 and synth_b == 1


# ---------------------------------------------------------------------------
# Collapse detection in stamp_and_finalize (raw payload nonempty, final zero)
# ---------------------------------------------------------------------------


def test_collapse_error_is_classified_retryable() -> None:
    """Acceptance #7: a collapse is an explicit retryable failure, not empty success."""
    exc = steps.CombinedExtractionCollapsedError(
        "combined_extraction_collapsed episode_id=x raw_entities=1 raw_edges=1 "
        "resolved_nodes=0 resolved_edges=0"
    )
    assert classify_enrichment_failure(exc, error_type=type(exc).__name__) == "retryable"


def _fake_ctx() -> SimpleNamespace:
    adapter = MagicMock()
    adapter.mark_episode_ready.return_value = False  # short-circuit the empty-success path
    return SimpleNamespace(
        graph_adapter=adapter,
        episode_uuid="ep-collapse",
        worker_id="worker-1",
        processing_steps_total=3,
        started=0.0,
        claimed={},
    )


def _empty_graphiti_result() -> SimpleNamespace:
    return SimpleNamespace(
        episode=SimpleNamespace(uuid="ep-collapse"),
        nodes=[],
        edges=[],
        episodic_edges=[],
    )


@pytest.mark.asyncio
async def test_stamp_and_finalize_raises_collapse_when_raw_nonempty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw extraction non-empty but zero persisted -> CombinedExtractionCollapsedError."""
    monkeypatch.setattr(steps, "still_owns_episode", lambda *a, **k: True)
    ctx = _fake_ctx()
    receipt = patches.begin_extraction_receipt("ep-collapse", "user: Alice owns 37 coins.")
    receipt.raw_entity_count = 1
    receipt.raw_edge_count = 1

    with pytest.raises(steps.CombinedExtractionCollapsedError):
        await steps.stamp_and_finalize(ctx, _empty_graphiti_result())

    # Receipt is consumed exactly once.
    assert patches.get_extraction_receipt() is None
    ctx.graph_adapter.mark_episode_ready.assert_not_called()


@pytest.mark.asyncio
async def test_stamp_and_finalize_treats_genuinely_empty_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance #8: raw extraction empty stays an empty-extraction success (no collapse)."""
    monkeypatch.setattr(steps, "still_owns_episode", lambda *a, **k: True)
    ctx = _fake_ctx()
    patches.begin_extraction_receipt("ep-collapse", "user: ok thanks")  # raw counts stay 0

    # mark_episode_ready returns False -> the empty-success branch returns early without
    # raising. The point is simply that no collapse error is raised for a truly empty payload.
    await steps.stamp_and_finalize(ctx, _empty_graphiti_result())
    ctx.graph_adapter.mark_episode_ready.assert_called_once()


# ---------------------------------------------------------------------------
# Real add_episode_with_timeout task boundary (asyncio.wait_for copies context)
# ---------------------------------------------------------------------------


def _add_episode_kwargs() -> dict:
    return dict(
        name="ep",
        episode_body="user: Alice owns 37 coins.",
        source_description="claude-code",
        reference_time=datetime.now(timezone.utc),
        episode_uuid="ep-boundary",
    )


@pytest.mark.asyncio
async def test_receipt_survives_the_waitfor_task_boundary() -> None:
    """P1 regression: receipt begun in the PARENT is visible after asyncio.wait_for.

    add_episode_with_timeout runs graphiti_client.add_episode inside asyncio.wait_for,
    which schedules it as a separate Task with a COPIED context. The receipt must be
    created in the parent task (in add_episode_with_timeout, before wait_for) so the
    child inherits and mutates the SAME object and the parent reads the mutation back.
    """
    patches.clear_extraction_receipt()

    class FakeClient:
        async def add_episode(self, **kwargs) -> str:
            # Simulates the sanitizer running inside Graphiti's child task: the child
            # must see the parent's receipt object and mutate it in place.
            receipt = patches.get_extraction_receipt()
            assert receipt is not None, "child task did not inherit the parent receipt"
            receipt.raw_entity_count = 3
            receipt.raw_edge_count = 2
            return "graphiti-result"

    result = await steps.add_episode_with_timeout(
        FakeClient(), timeout_s=5.0, **_add_episode_kwargs()
    )
    assert result == "graphiti-result"

    # Parent task (this one) must observe the child's mutation.
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    assert receipt.raw_entity_count == 3
    assert receipt.raw_edge_count == 2
    patches.clear_extraction_receipt()


@pytest.mark.asyncio
async def test_declared_subject_requires_external_episode_uuid_not_name_fallback() -> None:
    """A display name cannot scope node authority to the active extraction request."""
    called = False

    class FakeClient:
        async def add_episode(self, **kwargs) -> str:
            nonlocal called
            called = True
            return "graphiti-result"

    kwargs = _add_episode_kwargs()
    kwargs["episode_uuid"] = None
    kwargs["self_bind_mode"] = SelfBindMode.ENFORCE
    kwargs["self_identity"] = declare_self_subject(
        self_context_for_pending_episode(
            source="manual", namespace="default", episode_uuid="ep"
        ),
        subject_node_uuid="node-1",
    )

    with pytest.raises(InvalidSelfSubjectDeclarationError, match="episode name is not identity"):
        await steps.add_episode_with_timeout(FakeClient(), timeout_s=5.0, **kwargs)

    assert called is False
    assert patches.get_extraction_receipt() is None


@pytest.mark.asyncio
async def test_receipt_cleared_when_add_episode_raises() -> None:
    """Failure cleanup: a raising add_episode leaves no stale receipt in a reused task."""
    patches.clear_extraction_receipt()

    class FailingClient:
        async def add_episode(self, **kwargs) -> str:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await steps.add_episode_with_timeout(
            FailingClient(), timeout_s=5.0, **_add_episode_kwargs()
        )
    assert patches.get_extraction_receipt() is None


@pytest.mark.asyncio
async def test_receipt_cleared_on_timeout() -> None:
    """Failure cleanup: a timed-out add_episode leaves no stale receipt."""
    patches.clear_extraction_receipt()

    class SlowClient:
        async def add_episode(self, **kwargs) -> str:
            await asyncio.sleep(1.0)
            return "never"

    with pytest.raises(TimeoutError):
        await steps.add_episode_with_timeout(
            SlowClient(), timeout_s=0.05, **_add_episode_kwargs()
        )
    assert patches.get_extraction_receipt() is None


@pytest.mark.asyncio
async def test_relationless_extraction_after_bounded_repair_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the in-call repair is exhausted, scheduler retries would only repeat paid work.

    The first entity-without-edge response is no longer assumed correct or deterministic. The
    combined-extraction wrapper repairs it once. Only a second relationless response reaches this
    terminal visibility path.
    """
    monkeypatch.setattr(steps, "still_owns_episode", lambda *a, **k: True)
    ctx = _fake_ctx()
    receipt = patches.begin_extraction_receipt("ep-collapse", "user: some names")
    receipt.raw_entity_count = 7
    receipt.raw_edge_count = 0
    receipt.orphan_nodes_dropped = 7
    receipt.relationless_repair_attempted = True

    with pytest.raises(steps.CombinedExtractionCollapsedError) as exc:
        await steps.stamp_and_finalize(ctx, _empty_graphiti_result())

    message = str(exc.value)
    assert message.startswith("relationless_extraction ")
    assert "retryable=false" in message
    assert "raw_entities=7" in message
    assert "relationless_repair_attempted=true" in message
    ctx.graph_adapter.mark_episode_ready.assert_not_called()


@pytest.mark.asyncio
async def test_a_real_linkage_collapse_is_still_reported_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse shape (edges present, endpoints lost) IS a linkage failure and must keep its
    original classification -- the relationless carve-out must not swallow it."""
    monkeypatch.setattr(steps, "still_owns_episode", lambda *a, **k: True)
    ctx = _fake_ctx()
    receipt = patches.begin_extraction_receipt("ep-collapse", "user: something")
    receipt.raw_entity_count = 0
    receipt.raw_edge_count = 1

    with pytest.raises(steps.CombinedExtractionCollapsedError) as exc:
        await steps.stamp_and_finalize(ctx, _empty_graphiti_result())

    message = str(exc.value)
    assert message.startswith("combined_extraction_collapsed ")
    assert "retryable=true" in message


@pytest.mark.asyncio
async def test_receipt_owned_subject_endpoint_binds_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    instructions: list[str] = []
    marker_node = SimpleNamespace(
        uuid="marker-node", name=endpoint.marker, group_id=""
    )
    postcards = SimpleNamespace(uuid="postcards-node", name="postcards", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="marker-node",
        target_node_uuid="postcards-node",
        fact="The current speaker owns 25 postcards.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        instructions.append(kwargs["custom_extraction_instructions"])
        return [marker_node, postcards], [edge], {
            "marker-node": [0],
            "postcards-node": [0],
        }

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, index_map = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions="CALLER CONTRACT",
    )

    canonical_uuid = self_uuid_for_namespace("default")
    assert endpoint.marker in instructions[0]
    assert nodes[0].name == "user"
    assert nodes[0].uuid == canonical_uuid
    assert edges[0].source_node_uuid == canonical_uuid
    assert index_map[canonical_uuid] == [0]
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    assert receipt.self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    assert receipt.self_bind_result.bound is True
    receipt.resolved_node_identity_by_extracted_uuid["postcards-node"] = (
        "postcards-persistent",
        "postcards",
        (),
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["postcards-node"] = True
    pruned = extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    )
    assert pruned == set()
    assert receipt.self_assertions_authorized == 1
    assert receipt.self_assertion_proposals[0]["authorization"]["authorized"] is True
    persisted_payload = json.loads(
        edges[0].attributes[SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY]
    )
    assert persisted_payload["claim_digest"] == receipt.self_assertion_proposals[0]["claim_digest"]


@pytest.mark.asyncio
async def test_unconfirmed_subject_edge_is_proposal_only_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=False)
    marker_node = SimpleNamespace(
        uuid="marker-node", name=endpoint.marker, group_id="", labels=[]
    )
    postcards = SimpleNamespace(
        uuid="postcards-node", name="postcards", group_id="", labels=[]
    )
    edge = SimpleNamespace(
        source_node_uuid="marker-node",
        target_node_uuid="postcards-node",
        name="OWNS",
        fact="The current speaker owns 25 postcards.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [marker_node, postcards], [edge], {
            "marker-node": [0],
            "postcards-node": [0],
        }

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, index_map = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    receipt = patches.get_extraction_receipt()
    assert [node.uuid for node in nodes] == [self_uuid_for_namespace("default"), "postcards-node"]
    assert len(edges) == 1
    assert self_uuid_for_namespace("default") in index_map
    assert receipt.self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    assert receipt.self_bind_result.bound is True
    receipt.resolved_node_identity_by_extracted_uuid["postcards-node"] = (
        "postcards-persistent",
        "postcards",
        (),
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["postcards-node"] = True
    pruned = extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    )
    assert pruned == {"postcards-node"}
    assert edges == []
    assert receipt.self_assertions_authorized == 0
    assert receipt.self_assertion_proposals[0]["authorization"] == {
        "authorized": False,
        "authority_key_id": "ed25519:test-owner",
        "reason": "confirmation_no_exact_match",
    }


@pytest.mark.asyncio
async def test_new_extraction_uuid_cannot_become_owner_approved_counterpart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=True)
    marker = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="", labels=[])
    alex = SimpleNamespace(uuid="alex-extracted", name="Alex", group_id="", labels=[])
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="alex-extracted",
        name="KNOWS",
        fact=f"{endpoint.marker} knows Alex.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [marker, alex], [edge], {"marker": [0], "alex-extracted": [0]}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    _, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.resolved_node_identity_by_extracted_uuid["alex-extracted"] = (
        "alex-extracted", "Alex", ()
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["alex-extracted"] = False

    assert extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    ) == {"alex-extracted"}
    assert edges == []
    assert receipt.self_assertion_proposals[0]["authorization"]["reason"] == (
        "counterpart_identity_not_persistent"
    )
    assert receipt.self_assertion_authorizer.proposals == []


@pytest.mark.asyncio
async def test_missing_counterpart_resolution_retains_a_refusal_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=True)
    marker = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="", labels=[])
    bicycle = SimpleNamespace(uuid="bicycle-extracted", name="bicycle", group_id="", labels=[])
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="bicycle-extracted",
        name="OWNS",
        fact=f"{endpoint.marker} owns a bicycle.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [marker, bicycle], [edge], {
            "marker": [0], "bicycle-extracted": [0]
        }

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    _, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    receipt = patches.get_extraction_receipt()
    assert receipt is not None

    assert extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    ) == {"bicycle-extracted"}
    assert edges == []
    assert receipt.self_assertion_proposals == [{
        "authorization": {
            "authorized": False,
            "authority_key_id": "",
            "reason": "counterpart_identity_not_resolved",
        },
        "episode_uuid": "projection-1",
        "evidence_sha256": receipt.self_assertion_proposals[0]["evidence_sha256"],
        "extracted_counterpart_uuid": "bicycle-extracted",
        "kind": "unresolved_self_assertion",
        "policy_version": receipt.self_assertion_proposals[0]["policy_version"],
    }]
    assert receipt.self_assertion_authorizer.proposals == []


@pytest.mark.asyncio
async def test_changed_same_name_candidate_cannot_reuse_counterpart_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=True)
    marker = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="", labels=[])
    alex = SimpleNamespace(uuid="alex-extracted", name="Alex", group_id="", labels=["Person"])
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="alex-extracted",
        name="KNOWS",
        fact=f"{endpoint.marker} knows Alex.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [marker, alex], [edge], {"marker": [0], "alex-extracted": [0]}

    class ApprovesOnlyAlexA:
        def __init__(self) -> None:
            self.proposals = []

        def authorize(self, proposal):
            self.proposals.append(proposal)
            counterpart = json.loads(proposal.assertion_json)["counterpart"]
            authorized = counterpart["uuid"] == "alex-A"
            return SelfAuthorizationDecision(
                authorized,
                "owner_signature_verified" if authorized else "confirmation_no_exact_match",
                "ed25519:test-owner",
            )

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    _, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    authorizer = ApprovesOnlyAlexA()
    receipt.self_assertion_authorizer = authorizer
    receipt.resolved_node_identity_by_extracted_uuid["alex-extracted"] = (
        "alex-B", "Alex", ("Person",)
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["alex-extracted"] = True

    assert extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    ) == {"alex-extracted"}
    assert edges == []
    assert len(authorizer.proposals) == 1
    assert json.loads(authorizer.proposals[0].assertion_json)["counterpart"] == {
        "labels": ["Person"], "name": "Alex", "uuid": "alex-B"
    }
    assert receipt.self_assertion_proposals[0]["authorization"]["reason"] == (
        "confirmation_no_exact_match"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "episode_text",
    ["Yesterday I bought a bicycle.", "I do not own a car."],
)
async def test_unmarked_author_fallback_never_creates_an_ordinary_self(
    monkeypatch: pytest.MonkeyPatch, episode_text: str
) -> None:
    _begin_subject_endpoint_receipt(authorized=False)
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = episode_text
    ordinary_user = SimpleNamespace(uuid="ordinary-user", name="user", group_id="")
    counterpart = SimpleNamespace(uuid="counterpart", name="counterpart", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="ordinary-user",
        target_node_uuid="counterpart",
        name="OWNS",
        fact="user owns the counterpart",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [ordinary_user, counterpart], [edge], {
            "ordinary-user": [0], "counterpart": [0]
        }

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert nodes == []
    assert edges == []
    assert receipt.self_assertion_proposals[0]["kind"] == "unresolved_author_reference"
    assert receipt.self_assertions_authorized == 0


@pytest.mark.asyncio
async def test_self_proposal_episode_cannot_hydrate_unsigned_node_summary() -> None:
    receipt = patches.begin_extraction_receipt(
        "projection-1", "I own 37 postcards.", self_bind_mode=SelfBindMode.ENFORCE
    )
    receipt.suppress_node_semantic_hydration = True
    node = SimpleNamespace(uuid="postcards", summary="existing", attributes={"safe": True})
    embedded = []

    async def unexpected_hydration(*args, **kwargs):
        raise AssertionError("unsigned episode reached free-form node hydration")

    async def embed_nodes(embedder, nodes):
        embedded.extend(nodes)

    wrapped = model_patches._wrap_self_authority_node_hydration(
        unexpected_hydration, embed_nodes
    )
    result = await wrapped(SimpleNamespace(embedder=object()), [node], object())

    assert result == [node]
    assert embedded == [node]
    assert node.summary == "existing"
    assert node.attributes == {"safe": True}


def test_multiline_reported_speech_cannot_gain_authority_from_marker_or_model() -> None:
    """Regression: the legacy scanner accepts the second line, so the signature gate must hold."""

    endpoint = _begin_subject_endpoint_receipt(authorized=False)
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = "She said:\nI will handle the deployment."
    receipt.graphiti_episode_uuid = "projection-1"
    marker_node = SimpleNamespace(
        uuid="marker", name=endpoint.marker, group_id="", labels=[]
    )
    deployment = SimpleNamespace(
        uuid="deployment", name="deployment", group_id="", labels=[]
    )
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="deployment",
        name="WILL_HANDLE",
        fact=f"{endpoint.marker} will handle the deployment.",
        episodes=["projection-1"],
    )
    nodes = [marker_node, deployment]
    edges = [edge]
    index_map = {"marker": [0], "deployment": [0]}

    assert extraction_patches._requires_declared_author_endpoint(receipt.episode_text) is True
    receipt.self_bind_result = extraction_patches._record_self_binding(
        nodes, edges, index_map, receipt
    )
    receipt.resolved_node_identity_by_extracted_uuid["deployment"] = (
        "deployment-persistent",
        "deployment",
        (),
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["deployment"] = True
    assert extraction_patches.finalize_self_assertion_authority_after_node_resolution(
        receipt
    ) == {"deployment"}

    assert nodes[0].uuid == self_uuid_for_namespace("default")
    assert edges == []
    assert receipt.self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    assert receipt.self_assertion_proposals[0]["assertion"]["fact"] == (
        "user will handle the deployment."
    )


@pytest.mark.asyncio
async def test_subject_endpoint_corrective_retry_replaces_model_user_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    instructions: list[str] = []
    target = SimpleNamespace(uuid="postcards", name="postcards", group_id="")

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        instructions.append(kwargs["custom_extraction_instructions"])
        if len(instructions) == 1:
            ordinary_user = SimpleNamespace(uuid="ordinary-user", name="user", group_id="")
            edge = SimpleNamespace(
                source_node_uuid="ordinary-user",
                target_node_uuid="postcards",
                fact="The current speaker owns postcards.",
                episodes=["projection-1"],
            )
            return [ordinary_user, target], [edge], {
                "ordinary-user": [0],
                "postcards": [0],
            }
        marker = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
        edge = SimpleNamespace(
            source_node_uuid="marker",
            target_node_uuid="postcards",
            fact="The current speaker owns postcards.",
            episodes=["projection-1"],
        )
        return [marker, target], [edge], {"marker": [0], "postcards": [0]}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(instructions) == 2
    assert "generic speaker label" in instructions[0]
    assert "INVALID AUTHOR-ENDPOINT CORRECTION" in instructions[1]
    assert instructions[1].rfind(endpoint.marker) > instructions[1].rfind("`user`")
    assert nodes[0].uuid == self_uuid_for_namespace("default")
    assert edges[0].source_node_uuid == nodes[0].uuid


@pytest.mark.asyncio
async def test_marked_first_person_projection_quarantines_missing_marker_after_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _begin_subject_endpoint_receipt()
    ordinary = SimpleNamespace(uuid="ordinary-user", name="user", group_id="")
    target = SimpleNamespace(uuid="role", name="admin role", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="ordinary-user",
        target_node_uuid="role",
        fact="A software user has an admin role.",
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [ordinary, target], [edge], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    receipt = patches.get_extraction_receipt()
    assert nodes == []
    assert edges == []
    assert receipt is not None
    assert receipt.self_assertion_proposals[0]["authorization"]["reason"] == (
        "unmarked_author_reference_quarantined"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "episode_text,node_name,fact",
    [
        (
            "The application user has an admin role.",
            "user",
            "The application user has an admin role.",
        ),
        (
            'Mara said, "I own 25 postcards."',
            "I",
            "Mara said that I own 25 postcards.",
        ),
    ],
)
async def test_third_person_and_quoted_self_like_nodes_do_not_require_author_marker(
    monkeypatch: pytest.MonkeyPatch, episode_text: str, node_name: str, fact: str
) -> None:
    _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = episode_text
    calls = 0
    self_like = SimpleNamespace(uuid="ordinary", name=node_name, group_id="")
    target = SimpleNamespace(uuid="target", name="admin role", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="ordinary", target_node_uuid="target", fact=fact,
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [self_like, target], [edge], {"ordinary": [0], "target": [0]}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, _, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert calls == 1
    assert nodes[0].uuid == "ordinary"
    assert receipt.self_bind_result is not None
    assert receipt.self_bind_result.bound is False


@pytest.mark.asyncio
async def test_subject_endpoint_refuses_reserved_prefix_in_previous_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _begin_subject_endpoint_receipt()
    called = False

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        nonlocal called
        called = True
        return [], [], {}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="context"):
        await extraction_patches._run_graphiti_combined_extraction(
            clients=object(),
            episode=SimpleNamespace(uuid="projection-1"),
            previous_episodes=[
                SimpleNamespace(content=f"stale {SUBJECT_ENDPOINT_MARKER_PREFIX}token")
            ],
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions=None,
        )
    assert called is False


def test_sanitizer_materializes_only_the_receipt_owned_subject_endpoint() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    valid = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [{"name": "postcards", "entity_type_id": 0}],
            "edges": [{
                "source_entity_name": endpoint.marker,
                "target_entity_name": "postcards",
                "relation_type": "OWNS",
                "fact": "The current speaker owns postcards.",
                "episode_indices": [0],
            }],
        },
        patches.get_extraction_receipt(),
        "I own 25 postcards.",
    )
    assert endpoint.marker in {row["name"] for row in valid["extracted_entities"]}

    stale = f"{SUBJECT_ENDPOINT_MARKER_PREFIX}stale"
    invalid = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [
                {"name": stale, "entity_type_id": 0},
                {"name": "postcards", "entity_type_id": 0},
            ],
            "edges": [{
                "source_entity_name": stale,
                "target_entity_name": "postcards",
                "relation_type": "OWNS",
                "fact": "The current speaker owns postcards.",
                "episode_indices": [0],
            }],
        },
        patches.get_extraction_receipt(),
        "I own 25 postcards.",
    )
    assert stale not in {row["name"] for row in invalid["extracted_entities"]}
    assert invalid["edges"] == []


def test_marker_text_without_marker_endpoint_is_dropped_before_persistence() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    payload = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [
                {"name": "postcards", "entity_type_id": 0},
                {"name": "collection", "entity_type_id": 0},
            ],
            "edges": [{
                "source_entity_name": "postcards",
                "target_entity_name": "collection",
                "relation_type": "BELONGS_TO",
                "fact": f"{endpoint.marker} owns the collection.",
                "episode_indices": [0],
            }],
        },
        receipt,
        "I own 25 postcards.",
    )

    assert payload["edges"] == []
    assert receipt is not None
    assert receipt.subject_marker_edges_suppressed == 1


def test_marker_edge_is_only_a_proposal_without_owner_authority() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    payload = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [{"name": "Kubernetes", "entity_type_id": 0}],
            "edges": [{
                "source_entity_name": endpoint.marker,
                "target_entity_name": "Kubernetes",
                "relation_type": "OWNS",
                "fact": f"{endpoint.marker} owns Kubernetes.",
                "episode_indices": [0],
            }],
        },
        receipt,
        "Can you tell me about Kubernetes?",
    )

    assert len(payload["edges"]) == 1
    assert receipt is not None
    assert receipt.self_assertions_authorized == 0


@pytest.mark.parametrize(
    "episode_text",
    [
        "Do I own Kubernetes?",
        "I do not own Kubernetes.",
        'Mara said, "I own Kubernetes."',
        'Mara said, "I own\nKubernetes."',
        "Mara said, 'The quoted claim continues:\nI own Kubernetes.'",
        "Mara said, 'I don't own this, but the quote is unfinished:\nI own Kubernetes.",
        "Mara said:\n```text\nI own Kubernetes.\n```",
    ],
)
def test_structural_marker_transport_is_independent_of_grammar(
    episode_text: str,
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = episode_text
    payload = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [
                {"name": endpoint.marker, "entity_type_id": 0},
                {"name": "Kubernetes", "entity_type_id": 0},
            ],
            "edges": [{
                "source_entity_name": endpoint.marker,
                "target_entity_name": "Kubernetes",
                "relation_type": "OWNS",
                "fact": f"{endpoint.marker} owns Kubernetes.",
                "episode_indices": [0],
            }],
        },
        receipt,
        episode_text,
    )

    assert endpoint.marker in {row["name"] for row in payload["extracted_entities"]}
    assert len(payload["edges"]) == 1
    assert receipt.self_assertions_authorized == 0


@pytest.mark.parametrize(
    "episode_text",
    [
        "Yes, I own 37 postcards.",
        "Today; I own 37 postcards.",
        "- I own 37 postcards.",
    ],
)
def test_prefixed_first_person_assertions_require_declared_endpoint(
    episode_text: str,
) -> None:
    assert extraction_patches._requires_declared_author_endpoint(episode_text) is True


def test_marker_fact_reaches_only_the_post_resolution_signature_gate() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = 'I said, "I own Kubernetes."'
    receipt.graphiti_episode_uuid = "projection-1"
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    target = SimpleNamespace(uuid="k8s", name="Kubernetes", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="k8s",
        fact=f"{endpoint.marker} owns Kubernetes.",
        episodes=["projection-1"],
    )

    nodes = [marker_node, target]
    edges = [edge]
    extraction_patches._declare_subject_endpoint(
        nodes, edges, {"marker": [0]}, receipt
    )
    assert edges == [edge]
    assert receipt.self_assertion_pending_edges == [edge]
    assert receipt.self_assertions_authorized == 0


@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE])
def test_reserved_prefix_has_no_new_semantics_outside_enforce(mode) -> None:
    receipt = patches.begin_extraction_receipt(
        "ordinary",
        f"The literal identifier is {SUBJECT_ENDPOINT_MARKER_PREFIX}ordinary.",
        self_bind_mode=mode,
    )
    literal = f"{SUBJECT_ENDPOINT_MARKER_PREFIX}ordinary"
    payload = extraction_patches._sanitize_combined_payload(
        {
            "extracted_entities": [
                {"name": literal, "entity_type_id": 0},
                {"name": "documentation", "entity_type_id": 0},
            ],
            "edges": [{
                "source_entity_name": literal,
                "target_entity_name": "documentation",
                "relation_type": "APPEARS_IN",
                "fact": f"{literal} appears in documentation.",
                "episode_indices": [0],
            }],
        },
        receipt,
        receipt.episode_text,
    )

    assert {row["name"] for row in payload["extracted_entities"]} == {
        literal,
        "documentation",
    }
    assert len(payload["edges"]) == 1


@pytest.mark.asyncio
async def test_subject_endpoint_is_reused_by_relationless_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    instructions: list[str] = []
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    target = SimpleNamespace(uuid="postcards", name="postcards", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="postcards",
        fact="The current speaker owns postcards.",
        episodes=["projection-1"],
    )

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        instructions.append(kwargs["custom_extraction_instructions"])
        receipt = patches.get_extraction_receipt()
        assert receipt is not None
        if len(instructions) == 1:
            receipt.raw_entity_count = 1
            receipt.raw_edge_count = 0
            return [target], [], {"postcards": [0]}
        receipt.raw_entity_count = 2
        receipt.raw_edge_count = 1
        return [marker_node, target], [edge], {"marker": [0], "postcards": [0]}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, _, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(instructions) == 2
    assert all(endpoint.marker in instruction for instruction in instructions)
    assert "quoted or reported speech" in instructions[0]
    assert "generic speaker label" in instructions[1]
    assert nodes[0].uuid == self_uuid_for_namespace("default")


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_name", ["application user", "user"])
async def test_mixed_rbac_user_is_preserved_unless_it_is_an_ambiguous_author_alias(
    monkeypatch: pytest.MonkeyPatch, ordinary_name: str,
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = "I grant read access to the application user."
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    ordinary_user = SimpleNamespace(uuid="rbac-user", name=ordinary_name, group_id="")
    role = SimpleNamespace(uuid="role", name="read access", group_id="")
    edges = [
        SimpleNamespace(
            source_node_uuid="marker",
            target_node_uuid="role",
            fact="The current speaker grants read access.",
            episodes=["projection-1"],
        ),
        SimpleNamespace(
            source_node_uuid="rbac-user",
            target_node_uuid="role",
            fact="The application user receives read access.",
            episodes=["projection-1"],
        ),
    ]

    async def fake_extract_nodes_and_edges(*args, **kwargs):
        return [marker_node, ordinary_user, role], edges, {
            "marker": [0],
            "rbac-user": [0],
            "role": [0],
        }

    monkeypatch.setattr(ce, "extract_nodes_and_edges", fake_extract_nodes_and_edges)
    nodes, final_edges, _ = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(),
        episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[],
        entity_types=None,
        excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert nodes[0].name == "user"
    assert nodes[0].uuid == self_uuid_for_namespace("default")
    if ordinary_name == "application user":
        assert nodes[1].name == ordinary_name
        assert nodes[1].uuid == "rbac-user"
        assert final_edges[1].source_node_uuid == "rbac-user"
    else:
        # Bare aliases have no independent proof of RBAC identity. Letting edge prose exempt
        # this alias would reopen the same mixed-payload bypass this test now guards.
        assert all(node.uuid != "rbac-user" for node in nodes)
        assert len(final_edges) == 1
        assert receipt.self_assertion_proposals[0]["kind"] == "unresolved_author_reference"


@pytest.mark.parametrize(
    "edges,index_map,message",
    [
        ([], {"marker": [0]}, "not an endpoint"),
        (
            [SimpleNamespace(
                source_node_uuid="marker",
                target_node_uuid="other",
                episodes=["projection-1"],
            )],
            {"marker": [1]},
            "index attribution",
        ),
    ],
)
def test_subject_marker_requires_edge_and_current_episode_attribution(
    edges, index_map, message
) -> None:
    endpoint = _begin_subject_endpoint_receipt()
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.graphiti_episode_uuid = "projection-1"

    with pytest.raises(InvalidSelfSubjectDeclarationError, match=message):
        extraction_patches._declare_subject_endpoint(
            [marker_node], edges, index_map, receipt
        )


def test_duplicate_subject_marker_nodes_are_refused() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    nodes = [
        SimpleNamespace(uuid="marker-a", name=endpoint.marker, group_id=""),
        SimpleNamespace(uuid="marker-b", name=endpoint.marker, group_id=""),
    ]
    receipt = patches.get_extraction_receipt()
    assert receipt is not None

    with pytest.raises(InvalidSelfSubjectDeclarationError, match="more than one"):
        extraction_patches._declare_subject_endpoint(nodes, [], {}, receipt)


def test_context_only_marker_edge_cannot_authorize_current_subject() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="other",
        episodes=["previous-graphiti-episode"],
    )
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.graphiti_episode_uuid = "current-graphiti-episode"

    with pytest.raises(InvalidSelfSubjectDeclarationError, match="current Graphiti episode"):
        extraction_patches._declare_subject_endpoint(
            [marker_node], [edge], {"marker": [0]}, receipt
        )


def test_current_episode_marker_edge_still_requires_owner_authority() -> None:
    endpoint = _begin_subject_endpoint_receipt()
    marker_node = SimpleNamespace(uuid="marker", name=endpoint.marker, group_id="")
    target = SimpleNamespace(uuid="k8s", name="Kubernetes", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="marker",
        target_node_uuid="k8s",
        fact=f"{endpoint.marker} owns Kubernetes.",
        episodes=["projection-1"],
    )
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.episode_text = "Can you tell me about Kubernetes?"
    receipt.graphiti_episode_uuid = "projection-1"

    extraction_patches._declare_subject_endpoint(
        [marker_node, target], [edge], {"marker": [0]}, receipt
    )
    assert receipt.self_assertion_pending_edges == [edge]
    assert receipt.self_assertions_authorized == 0


@pytest.mark.asyncio
async def test_subject_endpoint_cannot_change_observe_mode_prompt() -> None:
    endpoint = self_subject_endpoint_for_claim(_subject_endpoint_claim())
    assert endpoint is not None
    identity = self_context_for_pending_episode(
        source="user",
        namespace="default",
        episode_uuid="projection-1",
        turn_evidence_uuid="turn-1",
    )
    called = False

    class FakeClient:
        async def add_episode(self, **kwargs):
            nonlocal called
            called = True
            return "unused"

    kwargs = _add_episode_kwargs()
    kwargs.update(
        episode_uuid="projection-1",
        self_identity=identity,
        self_subject_endpoint=endpoint,
        self_bind_mode=SelfBindMode.OBSERVE,
    )
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="only in enforce"):
        await steps.add_episode_with_timeout(FakeClient(), timeout_s=5.0, **kwargs)
    assert called is False


def test_foreign_turn_endpoint_cannot_enter_receipt() -> None:
    endpoint = self_subject_endpoint_for_claim(
        _subject_endpoint_claim(
            evidence_projection_of="turn-foreign",
            turn_evidence_uuid="turn-foreign",
        )
    )
    assert endpoint is not None
    identity = self_context_for_pending_episode(
        source="user",
        namespace="default",
        episode_uuid="projection-1",
        turn_evidence_uuid="turn-1",
    )

    with pytest.raises(InvalidSelfSubjectDeclarationError, match="scope"):
        patches.begin_extraction_receipt(
            "projection-1",
            "I own 25 postcards.",
            self_identity=identity,
            self_subject_endpoint=endpoint,
            self_bind_mode=SelfBindMode.ENFORCE,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_fallback", [False, True])
@pytest.mark.parametrize("author_mentioned", [False, True])
async def test_mixed_marker_payload_quarantines_unmarked_author_before_resolution(
    monkeypatch: pytest.MonkeyPatch, reverse_fallback: bool, author_mentioned: bool,
) -> None:
    """One compliant marker edge must not vouch for an unmarked second self reference."""
    endpoint = _begin_subject_endpoint_receipt(authorized=False)
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    episode_text = (
        "I own postcards. I also own stamps. The postcards are blue." if author_mentioned
        else "The postcards and stamps are in the cabinet. The postcards are blue."
    )
    receipt.episode_text = episode_text
    calls = []

    async def extract(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        # Each extraction gets a fresh payload, just as the actual corrective call does.
        nodes = [
            SimpleNamespace(uuid=uuid, name=name, group_id="", labels=[])
            for uuid, name in (
                ("marker", endpoint.marker), ("ordinary-user", "user"),
                ("postcards", "postcards"), ("stamps", "stamps"), ("blue", "blue"),
            )
        ]
        source, target = (
            ("stamps", "ordinary-user") if reverse_fallback else ("ordinary-user", "stamps")
        )
        edges = [
            SimpleNamespace(
                source_node_uuid="marker", target_node_uuid="postcards",
                name="OWNS", fact=f"{endpoint.marker} owns postcards.", episodes=["projection-1"],
            ),
            SimpleNamespace(
                source_node_uuid=source, target_node_uuid=target,
                name="OWNS", fact="user owns stamps.", episodes=["projection-1"],
            ),
            SimpleNamespace(
                source_node_uuid="postcards", target_node_uuid="blue",
                name="COLOR", fact="The postcards are blue.", episodes=["projection-1"],
            ),
        ]
        return nodes, edges, {node.uuid: [0] for node in nodes}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", extract)
    nodes, edges, index_map = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )

    assert len(calls) == 2  # Exactly one correction, even with a valid marker already present.
    assert "MENHIR INVALID AUTHOR-ENDPOINT CORRECTION" in calls[1]
    canonical = self_uuid_for_namespace("default")
    assert {node.uuid for node in nodes} == {canonical, "postcards", "blue"}
    assert set(index_map) == {canonical, "postcards", "blue"}
    assert all("ordinary-user" not in (edge.source_node_uuid, edge.target_node_uuid) for edge in edges)
    assert receipt.self_assertion_proposals[0]["kind"] == "unresolved_author_reference"

    receipt.resolved_node_identity_by_extracted_uuid["postcards"] = (
        "persistent-postcards", "postcards", ()
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["postcards"] = True
    assert extraction_patches.finalize_self_assertion_authority_after_node_resolution(receipt) == set()
    assert [edge.fact for edge in edges] == ["The postcards are blue."]
    assert receipt.self_assertions_authorized == 0
    assert receipt.episode_text == episode_text


def test_mixed_marker_payload_prunes_orphan_author_alias() -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=False)
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.graphiti_episode_uuid = "projection-1"
    nodes = [
        SimpleNamespace(uuid=uuid, name=name, group_id="")
        for uuid, name in (("marker", endpoint.marker), ("alias", "I"), ("postcards", "postcards"))
    ]
    edges = [SimpleNamespace(
        source_node_uuid="marker", target_node_uuid="postcards", fact="I own postcards.",
        episodes=["projection-1"],
    )]
    index_map = {node.uuid: [0] for node in nodes}

    extraction_patches._declare_subject_endpoint(nodes, edges, index_map, receipt)

    assert {node.uuid for node in nodes} == {"marker", "postcards"}
    assert set(index_map) == {"marker", "postcards"}
    assert receipt.self_assertion_proposals[0]["kind"] == "unresolved_author_reference"


def test_quarantining_alias_does_not_declare_a_pruned_marker() -> None:
    endpoint = _begin_subject_endpoint_receipt(authorized=False)
    receipt = patches.get_extraction_receipt()
    assert receipt is not None
    receipt.graphiti_episode_uuid = "projection-1"
    nodes = [
        SimpleNamespace(uuid="marker", name=endpoint.marker, group_id=""),
        SimpleNamespace(uuid="alias", name="user", group_id=""),
    ]
    edges = [SimpleNamespace(
        source_node_uuid="marker", target_node_uuid="alias", fact="I know the user.",
        episodes=["projection-1"],
    )]
    index_map = {node.uuid: [0] for node in nodes}

    extraction_patches._declare_subject_endpoint(nodes, edges, index_map, receipt)

    assert nodes == [] and edges == [] and index_map == {}
    assert receipt.self_identity.evidence_kind is SelfEvidenceKind.TRUSTED_USER_TURN
    assert receipt.self_assertion_pending_edges == []


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_as_keyword", [False, True])
async def test_later_ordinary_turn_cannot_hydrate_from_rejected_self_context(
    previous_as_keyword: bool,
) -> None:
    """A fresh receipt cannot certify earlier evidence, including summaries/attributes."""
    first = patches.begin_extraction_receipt(
        "first", "I own 37 postcards.", self_bind_mode=SelfBindMode.ENFORCE
    )
    first.suppress_node_semantic_hydration = True
    first_episode = SimpleNamespace(uuid="first", content=first.episode_text)
    node = SimpleNamespace(uuid="postcards", summary="color:blue", attributes={"color": "blue"})
    hydrated, embedded = [], []

    async def repeat_rejected_claim(clients, nodes, *args, **kwargs):
        hydrated.append(nodes)
        nodes[0].summary = "user owns 37 postcards"
        nodes[0].attributes["owner"] = "user"
        return nodes

    async def embed_nodes(embedder, nodes):
        embedded.append(list(nodes))

    wrapped = model_patches._wrap_self_authority_node_hydration(repeat_rejected_claim, embed_nodes)
    clients = SimpleNamespace(embedder=object())
    await wrapped(clients, [node], first_episode)
    later = patches.begin_extraction_receipt(
        "later", "The postcards are blue.", self_bind_mode=SelfBindMode.ENFORCE
    )
    assert not later.suppress_node_semantic_hydration
    later_episode = SimpleNamespace(uuid="later", content=later.episode_text)
    if previous_as_keyword:
        result = await wrapped(clients, [node], episode=later_episode, previous_episodes=[first_episode])
    else:
        result = await wrapped(clients, [node], later_episode, [first_episode])

    assert hydrated == []
    assert embedded == [[node], [node]]
    assert result == [node]
    assert node.summary == "color:blue"
    assert node.attributes == {"color": "blue"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, SelfBindMode.OFF, SelfBindMode.OBSERVE])
async def test_hydration_guard_preserves_legacy_modes(mode) -> None:
    if mode is not None:
        patches.begin_extraction_receipt("legacy", "ordinary text", self_bind_mode=mode)
    calls = []
    clients, nodes, episode, previous = object(), [object()], object(), [object()]
    expected = [object()]

    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    async def no_embeddings(*args, **kwargs):
        raise AssertionError("legacy hydration unexpectedly bypassed")

    wrapped = model_patches._wrap_self_authority_node_hydration(original, no_embeddings)
    assert await wrapped(clients, nodes, episode, previous_episodes=previous) is expected
    assert calls == [((clients, nodes, episode), {"previous_episodes": previous})]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE, SelfBindMode.ENFORCE])
async def test_bare_user_in_rbac_only_turn_remains_ordinary(monkeypatch, mode) -> None:
    if mode is SelfBindMode.ENFORCE:
        _begin_subject_endpoint_receipt(authorized=False)
        receipt = patches.get_extraction_receipt()
    else:
        receipt = patches.begin_extraction_receipt("projection-1", "", self_bind_mode=mode)
    assert receipt is not None
    receipt.episode_text = "The user role grants read access to the database."
    ordinary = SimpleNamespace(uuid="rbac-user", name="user", group_id="")
    database = SimpleNamespace(uuid="database", name="database", group_id="")
    edge = SimpleNamespace(
        source_node_uuid="rbac-user", target_node_uuid="database", name="READS",
        fact="The user role can read the database.", episodes=["projection-1"],
    )
    calls = []

    async def extract(*args, **kwargs):
        calls.append(True)
        return [ordinary, database], [edge], {"rbac-user": [0], "database": [0]}

    monkeypatch.setattr(ce, "extract_nodes_and_edges", extract)
    nodes, edges, index_map = await extraction_patches._run_graphiti_combined_extraction(
        clients=object(), episode=SimpleNamespace(uuid="projection-1"),
        previous_episodes=[], entity_types=None, excluded_entity_types=None,
        custom_extraction_instructions=None,
    )
    assert len(calls) == 1
    assert nodes == [ordinary, database] and edges == [edge]
    assert set(index_map) == {"rbac-user", "database"}
    assert not receipt.self_assertion_proposals
