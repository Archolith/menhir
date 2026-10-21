"""Two-tier self identity, offline half: the inferred (lower-trust) tier.

The inferred tier is the human an AGENT-authored episode refers to. It must never become the
anchored self, never be merged with it or with an ordinary node, and a malformed marker must cost
the marker's edges rather than leak marker-shaped text into the graph. These tests drive the real
production functions with duck-typed payload objects; no Neo4j, provider or network is touched.

Plan: .agent/plans/menhir-two-tier-self-identity-plan-2026-10-21.md (Phase 1, Phase 5).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.domain import merge_eligibility as me
from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    INFERRED_ENDPOINT_MARKER_PREFIX,
    INFERRED_SELF_DISPLAY_NAME,
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    InferredSubjectEndpointEnvelope,
    inferred_self_uuid_for_namespace,
    inferred_subject_endpoint_for_claim,
    self_subject_endpoint_for_claim,
    self_uuid_for_namespace,
)
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graphiti_extraction_policy import (
    CombinedExtractionReceipt,
    _first_person_endpoint,
    _inferred_endpoint_instructions,
    _record_inferred_binding,
    _sanitize_combined_payload,
    begin_extraction_receipt,
    clear_extraction_receipt,
)
from menhir.infrastructure.graphiti_resolution_policy import (
    MenhirCandidateFilterHook,
    MenhirNodePreResolutionHook,
)
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError,
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    SelfBindOutcome,
    bind_inferred_self,
    resolve_inferred_mode,
)

pytestmark = pytest.mark.unit

NS = "proj-a"


# --------------------------------------------------------------------------- shared fakes


class _Node:
    """Duck-typed EntityNode: binding touches uuid, name, group_id and attributes only."""

    def __init__(self, uuid, name, group_id=NS, attributes=None):
        self.uuid = uuid
        self.name = name
        self.group_id = group_id
        self.attributes = attributes if attributes is not None else {}


class _Edge:
    def __init__(self, source, target, fact="", name="", episodes=None):
        self.source_node_uuid = source
        self.target_node_uuid = target
        self.fact = fact
        self.name = name
        self.episodes = list(episodes or [])


def _graphiti_node(uuid, name, attributes=None, group_id=""):
    from graphiti_core.nodes import EntityNode

    return EntityNode(
        uuid=uuid, name=name, group_id=group_id, labels=["Entity"],
        created_at=datetime.now(timezone.utc), attributes=dict(attributes or {}),
    )


def _claim(**over):
    claim = {
        "uuid": "ep-1", "content": "The user prefers tabs.", "source": "claude-code",
        "namespace": NS,
    }
    claim.update(over)
    return claim


def _projection(source_kind, text="I prefer tabs.", namespace=NS, **over):
    """A graph-proven verbatim evidence projection, the anchored tier's input shape."""
    claim = {
        "uuid": "proj-1", "content": text, "source": "user", "namespace": namespace,
        "diff": None, "subject_endpoint_eligible": True, "is_evidence_projection": True,
        "evidence_projection_of": "turn-1", "turn_evidence_count": 1,
        "turn_evidence_uuid": "turn-1", "turn_evidence_role": "user",
        "turn_evidence_declarant": "user", "turn_evidence_text": text,
        "turn_evidence_namespace": namespace, "turn_evidence_source_kind": source_kind,
    }
    claim.update(over)
    return claim


def _envelope(episode="ep-1", namespace=NS):
    env = inferred_subject_endpoint_for_claim(
        _claim(uuid=episode, namespace=namespace), turn_role="unknown", anchored_endpoint=None
    )
    assert env is not None
    return env


@contextmanager
def _receipt(episode="ep-1", namespace=NS, **kwargs):
    env = _envelope(episode, namespace)
    try:
        receipt = begin_extraction_receipt(
            episode, "The user prefers tabs.",
            inferred_subject_endpoint=env, inferred_bind_mode=SelfBindMode.ENFORCE, **kwargs,
        )
        receipt.graphiti_episode_uuid = "g-ep"
        yield receipt
    finally:
        clear_extraction_receipt()


@pytest.fixture(autouse=True)
def _no_leaked_receipt():
    clear_extraction_receipt()
    yield
    clear_extraction_receipt()


@pytest.fixture
def telemetry(monkeypatch):
    events: list[dict] = []
    import menhir.infrastructure.telemetry.recorders as recorders

    monkeypatch.setattr(recorders, "record_lifecycle_event", lambda **kw: events.append(kw))
    return events


# --------------------------------------------------------------------------- 1. uuid


def test_inferred_uuid_is_deterministic_per_namespace_and_distinct_from_anchored():
    a1 = inferred_self_uuid_for_namespace("proj-a")
    assert a1 == inferred_self_uuid_for_namespace("proj-a")
    assert a1 != inferred_self_uuid_for_namespace("proj-b")
    # A shared formula prefix would make the two tiers one node.
    assert a1 != self_uuid_for_namespace("proj-a")
    assert inferred_self_uuid_for_namespace("default") != self_uuid_for_namespace("default")


def test_inferred_uuid_collapses_none_blank_and_default():
    default = inferred_self_uuid_for_namespace("default")
    assert inferred_self_uuid_for_namespace(None) == default
    assert inferred_self_uuid_for_namespace("") == default
    assert inferred_self_uuid_for_namespace("   ") == default


# --------------------------------------------------------------------------- 2. envelope


def test_envelope_rejects_a_marker_that_does_not_match_its_authority_tuple():
    good = _envelope("ep-1", NS)
    # Positive control: the genuine tuple reconstructs.
    InferredSubjectEndpointEnvelope(
        version=good.version, episode_uuid="ep-1", namespace=NS, marker=good.marker
    )
    with pytest.raises(ValueError, match="marker does not match"):
        InferredSubjectEndpointEnvelope(
            version=good.version, episode_uuid="ep-OTHER", namespace=NS, marker=good.marker
        )
    with pytest.raises(ValueError, match="marker does not match"):
        InferredSubjectEndpointEnvelope(
            version=good.version, episode_uuid="ep-1", namespace="proj-b", marker=good.marker
        )
    with pytest.raises(ValueError, match="marker does not match"):
        InferredSubjectEndpointEnvelope(
            version=good.version, episode_uuid="ep-1", namespace=NS,
            marker=f"{INFERRED_ENDPOINT_MARKER_PREFIX}forged",
        )
    with pytest.raises(ValueError, match="version"):
        InferredSubjectEndpointEnvelope(
            version="inferred-user-endpoint-v0", episode_uuid="ep-1", namespace=NS,
            marker=good.marker,
        )


# --------------------------------------------------------------------------- 3. offer matrix


def _anchored_envelope():
    env = self_subject_endpoint_for_claim(_projection("user_turn"))
    assert env is not None
    return env


def test_marker_is_offered_for_an_ordinary_agent_episode_with_unknown_role():
    claim = _claim()
    env = inferred_subject_endpoint_for_claim(claim, turn_role="unknown", anchored_endpoint=None)
    assert env is not None
    assert env.episode_uuid == "ep-1" and env.namespace == NS
    assert env.marker.startswith(INFERRED_ENDPOINT_MARKER_PREFIX)
    assert SUBJECT_ENDPOINT_MARKER_PREFIX not in env.marker
    assert env.inferred_uuid == inferred_self_uuid_for_namespace(NS)
    # Per-episode: another episode gets another marker.
    other = inferred_subject_endpoint_for_claim(
        _claim(uuid="ep-2"), turn_role="unknown", anchored_endpoint=None
    )
    assert other is not None and other.marker != env.marker


@pytest.mark.parametrize("role", ["user", "unknown", None, ""])
def test_marker_is_offered_for_non_refused_roles(role):
    assert inferred_subject_endpoint_for_claim(
        _claim(), turn_role=role, anchored_endpoint=None
    ) is not None


def test_no_marker_when_the_anchored_endpoint_was_built():
    anchored = _anchored_envelope()
    # Even a perfectly offerable agent claim gets nothing once the anchored marker exists.
    assert inferred_subject_endpoint_for_claim(
        _claim(), turn_role="user", anchored_endpoint=anchored
    ) is None
    assert inferred_subject_endpoint_for_claim(
        _projection("agent_quoted"), turn_role="user", anchored_endpoint=anchored
    ) is None


@pytest.mark.parametrize("kind", ["hook", "user_turn", "", "agent_quoted_not"])
def test_evidence_projection_of_a_non_agent_quoted_turn_is_not_offered(kind):
    assert inferred_subject_endpoint_for_claim(
        _projection(kind), turn_role="user", anchored_endpoint=None
    ) is None


@pytest.mark.parametrize("kind", ["agent_quoted", "Agent-Quoted", " AGENT_QUOTED "])
def test_evidence_projection_of_an_agent_quoted_turn_is_offered(kind):
    env = inferred_subject_endpoint_for_claim(
        _projection(kind), turn_role="user", anchored_endpoint=None
    )
    assert env is not None
    assert env.episode_uuid == "proj-1"


@pytest.mark.parametrize("role", ["assistant", "tool", "system", "Assistant", " TOOL "])
def test_refused_roles_get_no_marker(role):
    assert inferred_subject_endpoint_for_claim(
        _claim(), turn_role=role, anchored_endpoint=None
    ) is None


@pytest.mark.parametrize(
    "source", ["project-scan", "project_scan", "Project-Ingest", "structure-scan",
               "document_ingest", "document-ingest"],
)
def test_structurally_non_self_producers_get_no_marker(source):
    assert inferred_subject_endpoint_for_claim(
        _claim(source=source), turn_role="unknown", anchored_endpoint=None
    ) is None


@pytest.mark.parametrize(
    "text",
    [
        "note MenhirInferredUser_abc",
        "note menhirinferreduser_abc",
        "MENHIRINFERREDUSER_",
    ],
)
def test_prefix_in_content_withholds_the_marker_instead_of_raising(text):
    assert inferred_subject_endpoint_for_claim(
        _claim(content=text), turn_role="unknown", anchored_endpoint=None
    ) is None


def test_blank_episode_or_non_string_content_gets_no_marker():
    assert inferred_subject_endpoint_for_claim(
        _claim(uuid=""), turn_role="unknown", anchored_endpoint=None
    ) is None
    assert inferred_subject_endpoint_for_claim(
        _claim(content=None), turn_role="unknown", anchored_endpoint=None
    ) is None


# --------------------------------------------------------------------------- 12. counterexample


def test_assistant_turn_never_offered_inferred_marker():
    """The assistant's own "I fixed the bug" must not be able to name the human: the assistant's
    first person is the writer. Refused for an ordinary episode AND for an agent-quoted
    projection, where every other gate would admit it."""
    ordinary = _claim(content="I fixed the bug in the parser.")
    assert inferred_subject_endpoint_for_claim(
        ordinary, turn_role="assistant", anchored_endpoint=None
    ) is None
    quoted = _projection("agent_quoted", text="I fixed the bug in the parser.")
    assert inferred_subject_endpoint_for_claim(
        quoted, turn_role="assistant", anchored_endpoint=None
    ) is None
    # Control: the same claims are offered once the role is not a refused one.
    assert inferred_subject_endpoint_for_claim(
        ordinary, turn_role="unknown", anchored_endpoint=None
    ) is not None
    assert inferred_subject_endpoint_for_claim(
        quoted, turn_role="user", anchored_endpoint=None
    ) is not None


# --------------------------------------------------------------------------- 4. exclude_agent_quoted


def test_exclude_agent_quoted_refuses_an_otherwise_eligible_projection():
    quoted = _projection("agent_quoted")
    assert self_subject_endpoint_for_claim(quoted, exclude_agent_quoted=False) is not None
    assert self_subject_endpoint_for_claim(quoted) is not None  # default unchanged
    assert self_subject_endpoint_for_claim(quoted, exclude_agent_quoted=True) is None


def test_exclude_agent_quoted_leaves_genuine_user_turns_anchored():
    genuine = _projection("user_turn")
    assert self_subject_endpoint_for_claim(genuine, exclude_agent_quoted=True) is not None


# --------------------------------------------------------------------------- 5. bind_inferred_self


def _payload(marker, keeper_attrs=None):
    keeper = _Node("k1", marker, attributes=keeper_attrs)
    other = _Node("o1", "tabs")
    edges = [
        _Edge("k1", "o1", fact=f"{marker} prefers tabs", name="PREFERS"),
        _Edge("o1", "k1", fact=f"tabs are preferred by {marker.upper()}", name="PREFERRED_BY"),
        _Edge("o1", "o1", fact="tabs relate to tabs", name="SELF_REL"),
    ]
    return [keeper, other], edges, {"k1": [0], "o1": [0]}


def test_bind_rewrites_node_edges_text_and_index_map():
    marker = _envelope().marker
    nodes, edges, index_map = _payload(marker)
    inferred = inferred_self_uuid_for_namespace(NS)

    result = bind_inferred_self(
        nodes, edges, index_map, namespace=NS, keeper_uuid="k1", marker=marker
    )

    keeper = nodes[0]
    assert result.outcome is SelfBindOutcome.BOUND and result.bound
    assert result.tier == "inferred" and result.self_uuid == inferred
    assert keeper.uuid == inferred and keeper.name == INFERRED_SELF_DISPLAY_NAME == "user-inferred"
    assert keeper.attributes["entity_role"] == "inferred_self"
    assert keeper.attributes["self_tier"] == "inferred"
    assert keeper.attributes["namespace"] == NS
    # both edge directions followed
    assert edges[0].source_node_uuid == inferred and edges[0].target_node_uuid == "o1"
    assert edges[1].source_node_uuid == "o1" and edges[1].target_node_uuid == inferred
    assert result.edge_endpoints_rewritten == 2
    # marker scrubbed from prose, case-insensitively, nothing else touched
    assert edges[0].fact == "the user prefers tabs"
    assert edges[1].fact == "tabs are preferred by the user"
    assert edges[2].fact == "tabs relate to tabs"
    assert INFERRED_ENDPOINT_MARKER_PREFIX.lower() not in (edges[0].fact + edges[1].fact).lower()
    # index_map key moved, untouched key kept
    assert "k1" not in index_map and index_map[inferred] == [0] and index_map["o1"] == [0]
    assert result.index_map_keys_merged == 1
    assert result.rewritten_node_uuids == ("k1",)


def test_bound_inferred_node_never_carries_is_self_even_when_pre_stamped():
    marker = _envelope().marker
    nodes, edges, index_map = _payload(
        marker, keeper_attrs={"is_self": True, "entity_role": "self", "keep": "me"}
    )
    bind_inferred_self(nodes, edges, index_map, namespace=NS, keeper_uuid="k1", marker=marker)
    attrs = nodes[0].attributes
    assert "is_self" not in attrs
    assert attrs["entity_role"] == "inferred_self" and attrs["self_tier"] == "inferred"
    assert attrs["keep"] == "me"


def test_bind_refuses_a_foreign_physical_group():
    marker = _envelope().marker
    nodes, edges, index_map = _payload(marker)
    nodes[0].group_id = "other-group"
    with pytest.raises(InvalidSelfSubjectDeclarationError):
        bind_inferred_self(nodes, edges, index_map, namespace=NS, keeper_uuid="k1", marker=marker)
    assert nodes[0].uuid == "k1"  # refused before any mutation


def test_bind_refuses_default_namespace_node_in_a_named_namespace_and_vice_versa():
    marker = _envelope(namespace="default").marker
    nodes, edges, index_map = _payload(marker)
    nodes[0].group_id = "default"  # logical name, not the physical "" partition
    with pytest.raises(InvalidSelfSubjectDeclarationError):
        bind_inferred_self(
            nodes, edges, index_map, namespace="default", keeper_uuid="k1", marker=marker
        )


@pytest.mark.parametrize(
    "held", [inferred_self_uuid_for_namespace(NS), self_uuid_for_namespace(NS)],
    ids=["inferred-uuid", "anchored-uuid"],
)
def test_bind_refuses_when_another_node_holds_a_self_tier_uuid(held):
    marker = _envelope().marker
    nodes, edges, index_map = _payload(marker)
    nodes.append(_Node(held, "user"))
    with pytest.raises(AmbiguousSelfBindingError):
        bind_inferred_self(nodes, edges, index_map, namespace=NS, keeper_uuid="k1", marker=marker)
    assert nodes[0].uuid == "k1" and nodes[0].name == marker


@pytest.mark.parametrize("keeper_uuid", ["", "absent"])
def test_bind_refuses_a_missing_keeper_and_a_non_inferred_marker(keeper_uuid):
    marker = _envelope().marker
    nodes, edges, index_map = _payload(marker)
    with pytest.raises(InvalidSelfSubjectDeclarationError):
        bind_inferred_self(
            nodes, edges, index_map, namespace=NS, keeper_uuid=keeper_uuid, marker=marker
        )
    with pytest.raises(InvalidSelfSubjectDeclarationError):
        bind_inferred_self(
            nodes, edges, index_map, namespace=NS, keeper_uuid="k1",
            marker=f"{SUBJECT_ENDPOINT_MARKER_PREFIX}abc",
        )


def test_bind_rolls_back_every_structure_when_an_edge_setter_raises_midway():
    marker = _envelope().marker
    inferred = inferred_self_uuid_for_namespace(NS)

    class _ExplodingEdge(_Edge):
        """Accepts every write except the rewrite to the inferred uuid."""

        @property
        def target_node_uuid(self):
            return self._target

        @target_node_uuid.setter
        def target_node_uuid(self, value):
            if value == inferred:
                raise RuntimeError("boom")
            self._target = value

    keeper = _Node("k1", marker, attributes={"is_self": True, "keep": 1})
    other = _Node("o1", "tabs")
    good = _Edge("k1", "o1", fact=f"{marker} prefers tabs", name="PREFERS")
    bad = _ExplodingEdge("o1", "k1", fact=f"{marker} is liked", name="LIKED")
    nodes, edges = [keeper, other], [good, bad]
    index_map = {"k1": [0, 2], "o1": [0]}

    with pytest.raises(AmbiguousSelfBindingError, match="rolled back"):
        bind_inferred_self(nodes, edges, index_map, namespace=NS, keeper_uuid="k1", marker=marker)

    # The edge rewritten BEFORE the failure is restored too: a half-rewritten payload would orphan
    # the facts the episode carried.
    assert keeper.uuid == "k1" and keeper.name == marker
    assert keeper.attributes == {"is_self": True, "keep": 1}
    assert good.source_node_uuid == "k1" and good.target_node_uuid == "o1"
    assert good.fact == f"{marker} prefers tabs"
    assert bad.source_node_uuid == "o1" and bad.target_node_uuid == "k1"
    assert bad.fact == f"{marker} is liked"
    assert index_map == {"k1": [0, 2], "o1": [0]}


# --------------------------------------------------------------------------- 6. receipt


def test_receipt_refuses_both_endpoints_at_once():
    inferred = _envelope("proj-1")
    anchored = _anchored_envelope()
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="exclusive"):
        begin_extraction_receipt(
            "proj-1", "text", self_subject_endpoint=anchored, self_bind_mode=SelfBindMode.ENFORCE,
            inferred_subject_endpoint=inferred, inferred_bind_mode=SelfBindMode.ENFORCE,
        )


@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE])
def test_receipt_refuses_an_inferred_endpoint_outside_enforce_mode(mode):
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="enforce"):
        begin_extraction_receipt(
            "ep-1", "text", inferred_subject_endpoint=_envelope(), inferred_bind_mode=mode,
        )


def test_receipt_refuses_an_inferred_endpoint_for_another_episode():
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="scope does not match"):
        begin_extraction_receipt(
            "ep-OTHER", "text", inferred_subject_endpoint=_envelope("ep-1"),
            inferred_bind_mode=SelfBindMode.ENFORCE,
        )


def test_receipt_refuses_an_inferred_endpoint_for_another_namespace():
    from menhir.domain.self_identity import self_context_for_pending_episode

    identity = self_context_for_pending_episode(
        source="claude-code", namespace="proj-b", episode_uuid="ep-1"
    )
    with pytest.raises(InvalidSelfSubjectDeclarationError, match="scope does not match"):
        begin_extraction_receipt(
            "ep-1", "text", self_identity=identity,
            inferred_subject_endpoint=_envelope("ep-1", NS),
            inferred_bind_mode=SelfBindMode.ENFORCE,
        )


@pytest.mark.parametrize("namespace", ["proj-a", "default"])
def test_receipt_preallocates_the_marker_node_in_the_right_physical_group(namespace):
    env = _envelope("ep-1", namespace)
    with _receipt("ep-1", namespace) as receipt:
        node = receipt.inferred_subject_node
        assert receipt.inferred_subject_endpoint is env or (
            receipt.inferred_subject_endpoint.marker == env.marker
        )
        assert node.name == env.marker
        assert node.group_id == namespace_to_group_id(namespace)
        # Menhir owns the allocation: it must not pre-take either deterministic self uuid.
        assert node.uuid not in {
            inferred_self_uuid_for_namespace(namespace), self_uuid_for_namespace(namespace),
        }
        assert receipt.inferred_bind_mode is SelfBindMode.ENFORCE
        assert receipt.self_subject_endpoint is None


def test_receipt_without_inferred_endpoint_allocates_nothing():
    try:
        receipt = begin_extraction_receipt("ep-1", "text")
        assert receipt.inferred_subject_endpoint is None
        assert receipt.inferred_subject_node is None
    finally:
        clear_extraction_receipt()


def test_resolve_inferred_mode_fails_safe_to_off():
    assert resolve_inferred_mode("enforce") is SelfBindMode.ENFORCE
    assert resolve_inferred_mode(" Observe ") is SelfBindMode.OBSERVE
    assert resolve_inferred_mode("enfroce") is SelfBindMode.OFF
    assert resolve_inferred_mode(None) is SelfBindMode.OFF


# --------------------------------------------------------------------------- 7. sanitizer


FOREIGN = f"{INFERRED_ENDPOINT_MARKER_PREFIX}deadbeefdeadbeefdeadbeef"


def _raw_payload(marker):
    return {
        "extracted_entities": [
            {"name": "Rachel", "entity_type_id": -1},
            {"name": FOREIGN, "entity_type_id": -1},
            {"name": f"see {FOREIGN}", "entity_type_id": -1},
        ],
        "edges": [
            # keeper: the active marker is an edge endpoint, but NOT listed as an entity
            {"source_entity_name": marker, "target_entity_name": "tabs",
             "relation_type": "PREFERS", "fact": f"{marker} prefers tabs",
             "episode_indices": [0]},
            # foreign marker as endpoint -> dropped
            {"source_entity_name": FOREIGN, "target_entity_name": "tabs",
             "relation_type": "PREFERS", "fact": "foreign prefers tabs", "episode_indices": [0]},
            # marker only in prose, neither endpoint uses it -> dropped
            {"source_entity_name": "Rachel", "target_entity_name": "tabs",
             "relation_type": "LIKES", "fact": f"Rachel likes tabs said {marker}",
             "episode_indices": [0]},
        ],
    }


def _names(result):
    return [e["name"] for e in result["extracted_entities"]]


def test_sanitizer_in_enforce_mode_drops_foreign_markers_and_materializes_the_active_one():
    env = _envelope()
    with _receipt() as receipt:
        out = _sanitize_combined_payload(
            _raw_payload(env.marker), receipt, "The user prefers tabs. Rachel likes tabs."
        )
        names = _names(out)
        # foreign inferred-prefixed entity names are gone, ordinary ones stay
        assert FOREIGN not in names and f"see {FOREIGN}" not in names
        assert "Rachel" in names
        # marker used only as an edge endpoint is materialized exactly once
        assert names.count(env.marker) == 1
        # only the active-marker edge survives
        assert [e["fact"] for e in out["edges"]] == [f"{env.marker} prefers tabs"]
        assert receipt.subject_marker_edges_suppressed == 2
        assert receipt.malformed_entities_dropped == 2
        assert receipt.malformed_edges_dropped == 2


def test_sanitizer_keeps_an_edge_that_uses_the_marker_as_the_target_too():
    env = _envelope()
    raw = {
        "extracted_entities": [{"name": "tabs", "entity_type_id": -1}],
        "edges": [{"source_entity_name": "tabs", "target_entity_name": env.marker,
                   "relation_type": "PREFERRED_BY", "fact": f"tabs preferred by {env.marker}",
                   "episode_indices": [0]}],
    }
    with _receipt() as receipt:
        out = _sanitize_combined_payload(raw, receipt, "The user prefers tabs.")
        assert len(out["edges"]) == 1
        assert _names(out).count(env.marker) == 1


def test_sanitizer_drops_an_edge_mixing_the_active_and_a_foreign_marker_in_prose():
    env = _envelope()
    raw = {
        "extracted_entities": [],
        "edges": [{"source_entity_name": env.marker, "target_entity_name": "tabs",
                   "relation_type": "PREFERS",
                   "fact": f"{env.marker} prefers tabs, unlike {FOREIGN}",
                   "episode_indices": [0]}],
    }
    with _receipt() as receipt:
        out = _sanitize_combined_payload(raw, receipt, "The user prefers tabs.")
        assert out["edges"] == []


@pytest.mark.parametrize("receipt_kind", ["off", "none"])
def test_sanitizer_with_inferred_mode_off_does_not_apply_the_inferred_rules(receipt_kind):
    env = _envelope()
    receipt = None
    if receipt_kind == "off":
        receipt = begin_extraction_receipt("ep-1", "The user prefers tabs.")
        assert receipt.inferred_bind_mode is SelfBindMode.OFF
    out = _sanitize_combined_payload(
        _raw_payload(env.marker), receipt, "The user prefers tabs. Rachel likes tabs."
    )
    names = _names(out)
    assert FOREIGN in names and f"see {FOREIGN}" in names and "Rachel" in names
    assert len(out["edges"]) == 3
    if receipt is not None:
        assert receipt.subject_marker_edges_suppressed == 0
        assert receipt.malformed_edges_dropped == 0


# --------------------------------------------------------------------------- 8. prompt


def test_instructions_name_the_marker_and_are_absent_without_an_endpoint():
    env = _envelope()
    assert _inferred_endpoint_instructions(None, "user") is None
    text = _inferred_endpoint_instructions(env, "user")
    assert env.marker in text


def test_user_turn_instruction_binds_the_writers_first_person_to_the_marker():
    env = _envelope()
    text = _inferred_endpoint_instructions(env, "user")
    assert f"I/me/my also denote `{env.marker}`" in text
    assert "is NOT the human" not in text


@pytest.mark.parametrize("role", ["unknown", "tool", "assistant", "agent"])
def test_non_user_instruction_says_the_writers_first_person_is_not_the_human(role):
    env = _envelope()
    text = _inferred_endpoint_instructions(env, role)
    assert env.marker in text
    assert "is NOT the human" in text
    assert "also denote" not in text


def test_first_person_endpoint_prefers_anchored_then_inferred_only_for_user_turns():
    anchored = _anchored_envelope()
    inferred = _envelope()
    both = CombinedExtractionReceipt(
        turn_role="user", self_subject_endpoint=anchored, inferred_subject_endpoint=inferred
    )
    assert _first_person_endpoint(both) is anchored
    assert _first_person_endpoint(
        CombinedExtractionReceipt(turn_role="user", inferred_subject_endpoint=inferred)
    ) is inferred
    for role in ("unknown", "assistant", "tool", "system"):
        assert _first_person_endpoint(
            CombinedExtractionReceipt(turn_role=role, inferred_subject_endpoint=inferred)
        ) is None
    assert _first_person_endpoint(CombinedExtractionReceipt(turn_role="user")) is None


# --------------------------------------------------------------------------- 9. _record_inferred_binding


def _marker_payload(marker, *, second_marker_name=None, attribution=True, index=(0,)):
    nodes = [_Node("m1", marker), _Node("t1", "tabs")]
    edges = [
        _Edge("m1", "t1", fact=f"{marker} prefers tabs", name="PREFERS",
              episodes=["g-ep"] if attribution else []),
        _Edge("t1", "t2", fact="tabs relate to spaces", name="RELATES_TO", episodes=["g-ep"]),
    ]
    nodes.append(_Node("t2", "spaces"))
    index_map = {"m1": list(index), "t1": [0], "t2": [0]}
    if second_marker_name is not None:
        nodes.append(_Node("m2", second_marker_name))
        edges.append(_Edge("m2", "t1", fact="another", name="PREFERS", episodes=["g-ep"]))
        index_map["m2"] = [0]
    return nodes, edges, index_map


def _assert_no_marker_shape_left(nodes, edges):
    prefix = INFERRED_ENDPOINT_MARKER_PREFIX.lower()
    assert not [n for n in nodes if str(n.name).lower().startswith(prefix)]
    assert not [e for e in edges if prefix in f"{e.fact} {e.name}".lower()]


def test_single_marker_with_a_current_edge_binds_the_inferred_tier(telemetry):
    with _receipt() as receipt:
        marker = receipt.inferred_subject_endpoint.marker
        nodes, edges, index_map = _marker_payload(marker)

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        inferred = inferred_self_uuid_for_namespace(NS)
        assert result is not None and result.bound and result.tier == "inferred"
        assert result.self_uuid == inferred
        assert [n.uuid for n in nodes if n.uuid == inferred] == [inferred]
        assert "m1" not in [n.uuid for n in nodes]
        assert edges[0].source_node_uuid == inferred and edges[0].target_node_uuid == "t1"
        assert edges[0].fact == "the user prefers tabs"
        assert edges[1].source_node_uuid == "t1"  # unrelated edge untouched
        assert index_map[inferred] == [0] and "m1" not in index_map
        assert receipt.inferred_marker_items_suppressed == 0
        _assert_no_marker_shape_left(nodes, edges)
        assert [(e["event"], e["state"]) for e in telemetry] == [
            ("inferred_self_decision", "bound")
        ]


@pytest.mark.parametrize("second_name", [None, "forged"], ids=["duplicate-marker", "foreign-marker"])
def test_two_marker_nodes_are_ambiguous_and_both_quarantined(second_name, telemetry):
    with _receipt() as receipt:
        marker = receipt.inferred_subject_endpoint.marker
        second = marker if second_name is None else f"{INFERRED_ENDPOINT_MARKER_PREFIX}forged"
        nodes, edges, index_map = _marker_payload(marker, second_marker_name=second)

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        assert result.outcome is SelfBindOutcome.AMBIGUOUS and result.tier == "inferred"
        assert not result.bound
        # every marker node and each edge touching one is gone; everything else is kept
        assert sorted(n.uuid for n in nodes) == ["t1", "t2"]
        assert [(e.source_node_uuid, e.target_node_uuid) for e in edges] == [("t1", "t2")]
        assert set(index_map) == {"t1", "t2"}
        assert receipt.inferred_marker_items_suppressed == 2 + 2  # 2 nodes + 2 edges
        _assert_no_marker_shape_left(nodes, edges)
        assert [e["state"] for e in telemetry] == ["ambiguous"]


def test_marker_node_without_any_edge_is_quarantined():
    with _receipt() as receipt:
        marker = receipt.inferred_subject_endpoint.marker
        nodes = [_Node("m1", marker), _Node("t1", "tabs"), _Node("t2", "spaces")]
        edges = [_Edge("t1", "t2", fact="tabs relate to spaces", episodes=["g-ep"])]
        index_map = {"m1": [0], "t1": [0], "t2": [0]}

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        assert result.outcome is SelfBindOutcome.AMBIGUOUS
        assert sorted(n.uuid for n in nodes) == ["t1", "t2"]
        assert len(edges) == 1 and "m1" not in index_map
        assert receipt.inferred_marker_items_suppressed == 1
        _assert_no_marker_shape_left(nodes, edges)


@pytest.mark.parametrize(
    "kwargs",
    [{"attribution": False}, {"index": (1,)}],
    ids=["edge-from-another-episode", "marker-index-without-current-message"],
)
def test_marker_without_current_episode_attribution_is_quarantined(kwargs):
    with _receipt() as receipt:
        marker = receipt.inferred_subject_endpoint.marker
        nodes, edges, index_map = _marker_payload(marker, **kwargs)

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        assert result.outcome is SelfBindOutcome.AMBIGUOUS
        assert sorted(n.uuid for n in nodes) == ["t1", "t2"]
        assert [(e.source_node_uuid, e.target_node_uuid) for e in edges] == [("t1", "t2")]
        _assert_no_marker_shape_left(nodes, edges)


def test_stray_marker_with_no_inferred_endpoint_is_quarantined_and_returns_none():
    try:
        receipt = begin_extraction_receipt("ep-1", "The user prefers tabs.")
        marker = f"{INFERRED_ENDPOINT_MARKER_PREFIX}strayfromprompt"
        nodes, edges, index_map = _marker_payload(marker)

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        assert result is None
        assert sorted(n.uuid for n in nodes) == ["t1", "t2"]
        assert [(e.source_node_uuid, e.target_node_uuid) for e in edges] == [("t1", "t2")]
        assert "m1" not in index_map
        assert receipt.inferred_marker_items_suppressed == 2
        _assert_no_marker_shape_left(nodes, edges)
    finally:
        clear_extraction_receipt()


def test_payload_without_a_marker_is_left_alone_by_an_active_endpoint():
    with _receipt() as receipt:
        nodes = [_Node("t1", "tabs"), _Node("t2", "spaces")]
        edges = [_Edge("t1", "t2", fact="tabs relate to spaces", episodes=["g-ep"])]
        index_map = {"t1": [0], "t2": [0]}

        result = _record_inferred_binding(nodes, edges, index_map, receipt)

        assert result.outcome is SelfBindOutcome.NO_SELF_CANDIDATE and not result.bound
        assert [n.uuid for n in nodes] == ["t1", "t2"] and len(edges) == 1


# --------------------------------------------------------------------------- 10. resolution


@pytest.mark.parametrize(
    "attributes",
    [
        {"entity_role": "inferred_self"},
        {"entity_role": " Inferred_Self "},
        {"self_tier": "inferred"},
    ],
    ids=["role", "role-folded", "tier-only"],
)
async def test_candidate_filter_always_excludes_inferred_self(attributes):
    from graphiti_core.candidate_filter import CandidateFilterDecision

    clear_extraction_receipt()  # no receipt at all: the protection is unconditional
    hook = MenhirCandidateFilterHook()
    context = SimpleNamespace(
        extracted_node=_graphiti_node("x1", "user"),
        candidate_node=_graphiti_node("c1", "user-inferred", attributes),
    )
    assert await hook.filter_candidate(context) is CandidateFilterDecision.EXCLUDE


async def test_candidate_filter_still_includes_an_ordinary_candidate():
    from graphiti_core.candidate_filter import CandidateFilterDecision

    clear_extraction_receipt()
    context = SimpleNamespace(
        extracted_node=_graphiti_node("x1", "user"),
        candidate_node=_graphiti_node("c1", "user", {"entity_role": "customer"}),
    )
    assert await MenhirCandidateFilterHook().filter_candidate(context) is CandidateFilterDecision.INCLUDE


async def test_pre_resolution_refuses_an_unbound_node_holding_the_inferred_uuid():
    with _receipt() as receipt:
        assert receipt.inferred_bind_result is None  # active but never bound
        node = _graphiti_node(inferred_self_uuid_for_namespace(NS), "user", group_id=NS)
        context = SimpleNamespace(extracted_node=node, clients=SimpleNamespace(driver=object()))
        with pytest.raises(RuntimeError, match="inferred-self identity"):
            await MenhirNodePreResolutionHook().pre_resolve_node(context)


async def test_pre_resolution_refuses_an_extracted_node_carrying_inferred_markers():
    with _receipt():
        node = _graphiti_node("rand-1", "user", {"self_tier": "inferred"}, group_id=NS)
        context = SimpleNamespace(extracted_node=node, clients=SimpleNamespace(driver=object()))
        with pytest.raises(RuntimeError, match="inferred-self identity"):
            await MenhirNodePreResolutionHook().pre_resolve_node(context)


async def test_pre_resolution_defers_an_ordinary_node_while_an_endpoint_is_active():
    from graphiti_core.node_pre_resolution import PreResolutionDecision

    with _receipt():
        context = SimpleNamespace(
            extracted_node=_graphiti_node("rand-1", "Rachel", group_id=NS),
            clients=SimpleNamespace(driver=object()),
        )
        result = await MenhirNodePreResolutionHook().pre_resolve_node(context)
        assert result.decision is PreResolutionDecision.DEFER


def _absent_from_graph(monkeypatch):
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    async def _get_by_uuid(driver, uuid):
        raise NodeNotFoundError(uuid)

    monkeypatch.setattr(EntityNode, "get_by_uuid", staticmethod(_get_by_uuid))


async def test_pre_resolution_resolves_the_bound_inferred_node_with_inferred_markers(monkeypatch):
    from graphiti_core.node_pre_resolution import PreResolutionDecision

    _absent_from_graph(monkeypatch)
    inferred = inferred_self_uuid_for_namespace(NS)
    with _receipt() as receipt:
        marker = receipt.inferred_subject_endpoint.marker
        nodes, edges, index_map = _marker_payload(marker)
        receipt.inferred_bind_result = _record_inferred_binding(nodes, edges, index_map, receipt)
        assert receipt.inferred_bind_result.bound
        bound_node = next(n for n in nodes if n.uuid == inferred)
        context = SimpleNamespace(
            extracted_node=bound_node, clients=SimpleNamespace(driver=object())
        )

        result = await MenhirNodePreResolutionHook().pre_resolve_node(context)

        assert result.decision is PreResolutionDecision.RESOLVE
        resolved = result.resolved_node
        assert resolved.uuid == inferred
        assert resolved.attributes["entity_role"] == "inferred_self"
        assert resolved.attributes["self_tier"] == "inferred"
        assert "is_self" not in resolved.attributes
        # an unrelated ordinary node in the same payload is not swept in
        other = SimpleNamespace(
            extracted_node=next(n for n in nodes if n.uuid == "t1"),
            clients=SimpleNamespace(driver=object()),
        )
        assert (await MenhirNodePreResolutionHook().pre_resolve_node(other)).decision is (
            PreResolutionDecision.DEFER
        )


async def test_pre_resolution_stamps_a_bare_bound_node_when_nothing_is_stored(monkeypatch):
    """The resolver itself stamps the markers, so a node reaching it without them (first episode
    in the namespace) still persists as the inferred tier and never as an is_self node."""
    from graphiti_core.node_pre_resolution import PreResolutionDecision
    from menhir.infrastructure.self_binding import SelfBindResult

    _absent_from_graph(monkeypatch)
    inferred = inferred_self_uuid_for_namespace(NS)
    with _receipt() as receipt:
        receipt.inferred_bind_result = SelfBindResult(
            SelfBindOutcome.BOUND, mode=SelfBindMode.ENFORCE, self_uuid=inferred, tier="inferred"
        )
        bare = _graphiti_node(inferred, INFERRED_SELF_DISPLAY_NAME, group_id=NS)
        context = SimpleNamespace(extracted_node=bare, clients=SimpleNamespace(driver=object()))

        result = await MenhirNodePreResolutionHook().pre_resolve_node(context)

        assert result.decision is PreResolutionDecision.RESOLVE
        attrs = result.resolved_node.attributes
        assert attrs["entity_role"] == "inferred_self" and attrs["self_tier"] == "inferred"
        assert attrs["namespace"] == NS and "is_self" not in attrs


async def test_pre_resolution_refuses_to_substitute_when_no_driver_is_available():
    from menhir.infrastructure.self_binding import SelfBindResult

    inferred = inferred_self_uuid_for_namespace(NS)
    with _receipt() as receipt:
        receipt.inferred_bind_result = SelfBindResult(
            SelfBindOutcome.BOUND, mode=SelfBindMode.ENFORCE, self_uuid=inferred, tier="inferred"
        )
        context = SimpleNamespace(
            extracted_node=_graphiti_node(inferred, "user-inferred", group_id=NS),
            clients=SimpleNamespace(driver=None),
        )
        with pytest.raises(RuntimeError, match="requires a graph driver"):
            await MenhirNodePreResolutionHook().pre_resolve_node(context)


# --------------------------------------------------------------------------- 11. predicates


def test_correlation_ineligible_role_predicate_covers_the_inferred_tier():
    predicate = CorrelationRepository._INELIGIBLE_ROLE_PREDICATE
    assert "'inferred_self'" in predicate
    assert "self_tier IS NOT NULL" in predicate
    # anchored protection survives alongside it
    assert "coalesce(n.is_self, false)" in predicate and "= 'self'" in predicate


def test_mutation_time_precondition_protects_both_participants_from_the_inferred_tier():
    cypher = me.mutable_eligibility_cypher()
    for var in ("survivor", "absorbed"):
        assert f"toLower(trim(coalesce({var}.entity_role, ''))) <> 'inferred_self'" in cypher
        assert f"{var}.self_tier IS NULL" in cypher
    assert cypher.count("self_tier IS NULL") == 2
    assert cypher.count("<> 'inferred_self'") == 2
    renamed = me.mutable_eligibility_cypher("s", "a")
    assert "s.self_tier IS NULL" in renamed and "a.self_tier IS NULL" in renamed
