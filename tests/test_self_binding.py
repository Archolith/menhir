"""Phase 3: deterministic self binding before Graphiti dedup.

The counterexamples matter more than the happy path. A binding that rewrites the node but not
both edge directions, or not the episode index map, orphans the facts the episode carried -- a
worse outcome than the fork it replaces.
"""

from __future__ import annotations

import pytest

from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext,
    SpeakerRole,
    self_context_for_pending_episode,
    self_uuid_for_namespace,
)
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError,
    SelfBindMode,
    SelfBindOutcome,
    bind_canonical_self,
    resolve_bind_mode,
)


class _Node:
    """Duck-typed stand-in for graphiti's EntityNode (uuid + name is all binding touches)."""

    def __init__(self, uuid: str, name: str) -> None:
        self.uuid = uuid
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Node({self.uuid!r}, {self.name!r})"


class _Edge:
    def __init__(self, source: str, target: str, fact: str = "") -> None:
        self.source_node_uuid = source
        self.target_node_uuid = target
        self.fact = fact


def _trusted(namespace: str = "default") -> SelfIdentityContext:
    return self_context_for_pending_episode(
        source="user", namespace=namespace, episode_uuid="ep-1"
    )


def _declared(namespace: str = "default") -> SelfIdentityContext:
    """A trusted internal caller declaring that this episode's subject IS the owner.

    The only evidence that binds. No production producer emits it yet -- see
    `test_self_identity_producer_census`, which pins the production construction surface.
    """
    return SelfIdentityContext(
        namespace=namespace,
        speaker_role=SpeakerRole.USER,
        evidence_kind=SelfEvidenceKind.EXPLICIT_SELF_SUBJECT,
        source_kind="manual",
        episode_uuid="ep-1",
    )


def _untrusted(namespace: str = "default") -> SelfIdentityContext:
    return self_context_for_pending_episode(
        source="claude-code", namespace=namespace, episode_uuid="ep-1"
    )


# --------------------------------------------------------------------------- binding happens


@pytest.mark.unit
def test_proven_self_binds_to_the_deterministic_uuid():
    nodes = [_Node("random-1", "I"), _Node("random-2", "Rachel")]
    edges = [_Edge("random-1", "random-2", "I know Rachel")]
    index_map = {"random-1": [0], "random-2": [0]}

    result = bind_canonical_self(nodes, edges, index_map, _declared())

    assert result.outcome is SelfBindOutcome.BOUND
    assert result.self_uuid == self_uuid_for_namespace("default")
    assert nodes[0].uuid == self_uuid_for_namespace("default")
    assert nodes[1].uuid == "random-2"  # untouched


@pytest.mark.unit
def test_both_edge_directions_follow_the_rewrite():
    """An edge left pointing at the extractor's discarded UUID references a node that no longer
    exists -- the fact is silently lost."""
    canonical = self_uuid_for_namespace("default")
    nodes = [_Node("random-1", "I"), _Node("other", "Rachel")]
    edges = [
        _Edge("random-1", "other", "outgoing"),
        _Edge("other", "random-1", "incoming"),
    ]
    bind_canonical_self(nodes, edges, {}, _declared())

    assert edges[0].source_node_uuid == canonical
    assert edges[0].target_node_uuid == "other"
    assert edges[1].source_node_uuid == "other"
    assert edges[1].target_node_uuid == canonical


@pytest.mark.unit
def test_index_map_follows_with_no_lost_indices():
    canonical = self_uuid_for_namespace("default")
    nodes = [_Node("random-1", "I")]
    index_map = {"random-1": [0, 2], "unrelated": [1]}

    bind_canonical_self(nodes, [], index_map, _declared())

    assert index_map[canonical] == [0, 2]
    assert "random-1" not in index_map
    assert index_map["unrelated"] == [1]


@pytest.mark.unit
def test_two_first_person_nodes_fail_closed_rather_than_guessing():
    """Node-level authority still cannot say WHICH of two first-person nodes is the author, so
    the payload fails closed and writes nothing."""
    nodes = [_Node("a", "I"), _Node("b", "me"), _Node("c", "Rachel")]
    edges = [_Edge("a", "c"), _Edge("b", "c")]
    index_map = {"a": [0], "b": [1], "c": [0]}

    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, edges, index_map, _declared())

    # Nothing moved.
    assert [n.uuid for n in nodes] == ["a", "b", "c"]
    assert edges[0].source_node_uuid == "a"
    assert index_map == {"a": [0], "b": [1], "c": [0]}


@pytest.mark.unit
def test_a_trusted_turn_alone_binds_nothing_at_all():
    """REVIEW P1 (round 3). "I gave the user read access" -- neither node binds. Authorship is not
    subjecthood, and no NAME SHAPE supplies subjecthood: `the user` may be an RBAC role, and `I`
    may be reported speech. Both counts are recorded so the gap is measurable."""
    nodes = [_Node("speaker", "I"), _Node("rbac", "the user")]

    result = bind_canonical_self(nodes, [], {}, _trusted())

    assert result.outcome is SelfBindOutcome.SELF_LIKE_UNRESOLVED
    assert [n.uuid for n in nodes] == ["speaker", "rbac"]
    assert result.self_like_without_subject_authority == 2
    assert result.first_person_unresolved == 1


@pytest.mark.unit
def test_reported_speech_does_not_bind_the_quoted_speaker():
    """REVIEW P1 (round 3), the counterexample that removed first-person authority. A proven human
    turn reading `She told me, "I will handle it"` extracts an `I` that is someone else. By the
    time binding runs there is no quote boundary, span or attribution left to tell the two apart,
    so grammatical person cannot be authority -- it is a property of the string, not its origin."""
    nodes = [_Node("quoted", "I"), _Node("her", "Rachel")]

    result = bind_canonical_self(nodes, [], {}, _trusted())

    assert result.bound is False
    assert nodes[0].uuid == "quoted"
    assert result.first_person_unresolved == 1


@pytest.mark.unit
def test_pending_episode_factory_never_declares_a_self_subject():
    """The production factory never strengthens a persisted source into node-level authority.

    This is a behavior check over representative inputs, not the producer guard. The structural
    census in ``test_self_identity_producer_census`` separately fails on any new construction or
    factory call site.
    """
    from menhir.domain.self_identity import self_context_for_pending_episode

    for source in ("user", "manual", "claude-code", "agent_inference", "hook", ""):
        ctx = self_context_for_pending_episode(source=source, namespace="default")
        assert ctx.evidence_kind is not SelfEvidenceKind.EXPLICIT_SELF_SUBJECT


@pytest.mark.unit
def test_lone_generic_user_in_a_trusted_turn_does_not_bind():
    """REVIEW P1 (round 2). A single self-LIKE node is not thereby the author: a trusted turn
    saying "the user table has 3 rows" extracts one node named `user` that is not a person.
    Episode authorship never promotes it, and the outcome is recorded as an ordinary entity."""
    nodes = [_Node("rbac", "user")]
    edges = [_Edge("rbac", "other")]
    index_map = {"rbac": [0]}

    result = bind_canonical_self(nodes, edges, index_map, _trusted())

    assert result.outcome is SelfBindOutcome.SELF_LIKE_UNRESOLVED
    assert result.bound is False
    assert result.self_like_without_subject_authority == 1
    assert nodes[0].uuid == "rbac"
    assert edges[0].source_node_uuid == "rbac"
    assert index_map == {"rbac": [0]}


@pytest.mark.unit
def test_explicit_self_subject_admits_a_third_person_reference():
    """The declared extension point: a trusted internal caller vouching that the episode's
    subject IS the owner supplies the node-level authority a bare human turn cannot."""
    nodes = [_Node("n1", "the user")]
    result = bind_canonical_self(nodes, [], {}, _declared())
    assert result.outcome is SelfBindOutcome.BOUND
    assert nodes[0].uuid == self_uuid_for_namespace("default")


@pytest.mark.unit
def test_named_namespace_binds_to_its_own_identity():
    nodes = [_Node("random-1", "I")]
    bind_canonical_self(nodes, [], {}, _declared("proj-a"))
    assert nodes[0].uuid == self_uuid_for_namespace("proj-a")
    assert nodes[0].uuid != self_uuid_for_namespace("default")


# --------------------------------------------------------------------------- binding must NOT happen


@pytest.mark.unit
def test_untrusted_user_entity_is_left_alone():
    """The whole point: an agent-authored episode mentioning `user` is an ordinary entity and
    stays on the ordinary Graphiti path."""
    nodes = [_Node("random-1", "user")]
    edges = [_Edge("random-1", "other")]
    index_map = {"random-1": [0]}

    result = bind_canonical_self(nodes, edges, index_map, _untrusted())

    assert result.outcome is SelfBindOutcome.NOT_ELIGIBLE
    assert nodes[0].uuid == "random-1"
    assert edges[0].source_node_uuid == "random-1"
    assert index_map == {"random-1": [0]}


@pytest.mark.unit
def test_project_scan_narrative_about_users_never_binds():
    ctx = SelfIdentityContext(
        namespace="default",
        speaker_role=SpeakerRole.USER,
        evidence_kind=SelfEvidenceKind.TRUSTED_USER_TURN,
        source_kind="project-scan",
        episode_uuid="ep-1",
    )
    nodes = [_Node("random-1", "user")]
    result = bind_canonical_self(nodes, [], {}, ctx)
    assert result.outcome is SelfBindOutcome.NOT_ELIGIBLE
    assert nodes[0].uuid == "random-1"


@pytest.mark.unit
def test_assistant_turn_self_echo_does_not_bind():
    ctx = SelfIdentityContext(
        namespace="default",
        speaker_role=SpeakerRole.ASSISTANT,
        evidence_kind=SelfEvidenceKind.TRUSTED_USER_TURN,
        source_kind="assistant",
    )
    nodes = [_Node("random-1", "user")]
    assert bind_canonical_self(nodes, [], {}, ctx).outcome is SelfBindOutcome.NOT_ELIGIBLE
    assert nodes[0].uuid == "random-1"


@pytest.mark.unit
def test_missing_identity_fails_closed():
    nodes = [_Node("random-1", "user")]
    assert bind_canonical_self(nodes, [], {}, None).outcome is SelfBindOutcome.NOT_ELIGIBLE
    assert nodes[0].uuid == "random-1"


@pytest.mark.unit
def test_trusted_turn_without_a_self_alias_binds_nothing():
    """A human turn about only third parties has no self node to bind."""
    nodes = [_Node("a", "Rachel"), _Node("b", "Chicago")]
    result = bind_canonical_self(nodes, [], {}, _trusted())
    assert result.outcome is SelfBindOutcome.NO_SELF_CANDIDATE
    assert [n.uuid for n in nodes] == ["a", "b"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["users", "user account", "admin", "the users"])
def test_lookalike_names_are_not_self(name):
    nodes = [_Node("random-1", name)]
    result = bind_canonical_self(nodes, [], {}, _trusted())
    assert result.outcome is SelfBindOutcome.NO_SELF_CANDIDATE
    assert nodes[0].uuid == "random-1"


# --------------------------------------------------------------------------- fail closed, visibly


@pytest.mark.unit
def test_canonical_uuid_held_by_a_non_self_entity_is_refused():
    """Rewriting here would silently absorb an unrelated entity into the human. Fail visibly and
    leave the episode retryable instead of guessing."""
    canonical = self_uuid_for_namespace("default")
    nodes = [_Node("random-1", "I"), _Node(canonical, "Rachel")]

    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, [], {}, _declared())


@pytest.mark.unit
def test_refusal_leaves_the_payload_unwritten():
    """The failure path must not half-apply: no graph write comes from a raised bind."""
    canonical = self_uuid_for_namespace("default")
    nodes = [_Node(canonical, "Rachel"), _Node("random-1", "I")]
    edges = [_Edge("random-1", canonical)]
    index_map = {"random-1": [0]}

    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, edges, index_map, _declared())

    assert edges[0].source_node_uuid == "random-1"
    assert index_map == {"random-1": [0]}


@pytest.mark.unit
def test_a_failure_midway_rolls_the_whole_payload_back():
    """The rewrite spans nodes, both edge directions and the index map, and is only correct as a
    unit. If an endpoint assignment fails after the node was rewritten, a half-applied payload
    would orphan the episode's facts -- strictly worse than the fork being fixed.
    """

    class _FrozenTargetEdge:
        """Endpoint that refuses assignment, standing in for a validated model rejecting a write."""

        def __init__(self, source: str, target: str) -> None:
            self.source_node_uuid = source
            self._target = target

        @property
        def target_node_uuid(self) -> str:
            return self._target

        @target_node_uuid.setter
        def target_node_uuid(self, value: str) -> None:
            raise ValueError("immutable endpoint")

    edge = _FrozenTargetEdge("other", "random-1")
    nodes = [_Node("random-1", "I"), _Node("keep", "Rachel")]
    index_map = {"random-1": [0]}

    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, [edge], index_map, _declared())

    # Everything restored: node uuid, node list, index map.
    assert nodes[0].uuid == "random-1"
    assert [n.uuid for n in nodes] == ["random-1", "keep"]
    assert index_map == {"random-1": [0]}


@pytest.mark.unit
def test_binding_works_against_real_graphiti_models():
    """The duck-typed fixtures above prove the logic; this proves the logic survives contact with
    the actual pydantic models the seam receives, which validate on assignment."""
    from datetime import datetime, timezone

    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    now = datetime.now(timezone.utc)
    canonical = self_uuid_for_namespace("default")
    human = EntityNode(uuid="rand-1", name="I", group_id="", labels=["Entity"], created_at=now)
    other = EntityNode(uuid="rand-2", name="Rachel", group_id="", labels=["Entity"], created_at=now)
    edge = EntityEdge(
        uuid="e1",
        source_node_uuid="rand-1",
        target_node_uuid="rand-2",
        name="knows",
        fact="I know Rachel",
        group_id="",
        created_at=now,
        episodes=[],
    )
    nodes = [human, other]
    index_map = {"rand-1": [0], "rand-2": [0]}

    result = bind_canonical_self(nodes, [edge], index_map, _declared())

    assert result.outcome is SelfBindOutcome.BOUND
    assert human.uuid == canonical
    assert edge.source_node_uuid == canonical
    assert edge.target_node_uuid == "rand-2"
    assert other.uuid == "rand-2"
    assert index_map == {canonical: [0], "rand-2": [0]}


@pytest.mark.unit
def test_result_carries_only_structured_fields():
    """Instrumentation must not leak memory content or arbitrary entity names."""
    nodes = [_Node("random-1", "I")]
    result = bind_canonical_self(nodes, [_Edge("random-1", "x")], {"random-1": [0]}, _declared())
    rendered = repr(result)
    assert "user" not in rendered.replace("self_uuid", "")
    assert result.rewritten_node_uuids == ("random-1",)
    assert result.edge_endpoints_rewritten == 1


# --------------------------------------------------------------------------- rollout control


@pytest.mark.unit
def test_default_mode_is_off():
    """A durable-write-semantics change must not activate merely by deploying."""
    from menhir.config.settings_model import MemorySettings

    assert MemorySettings().canonical_self_binding_mode == "off"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        ("off", SelfBindMode.OFF),
        ("observe", SelfBindMode.OBSERVE),
        ("enforce", SelfBindMode.ENFORCE),
        ("  ENFORCE  ", SelfBindMode.ENFORCE),
        ("typo", SelfBindMode.OFF),
        ("", SelfBindMode.OFF),
        (None, SelfBindMode.OFF),
    ],
)
def test_mode_parsing_fails_safe(value, expected):
    """A configuration typo must not silently enable binding."""
    assert resolve_bind_mode(value) is expected


@pytest.mark.unit
def test_off_mode_reproduces_pre_change_behavior():
    nodes = [_Node("random-1", "I")]
    edges = [_Edge("random-1", "other")]
    index_map = {"random-1": [0]}

    result = bind_canonical_self(nodes, edges, index_map, _declared(), SelfBindMode.OFF)

    assert result.bound is False
    assert nodes[0].uuid == "random-1"
    assert edges[0].source_node_uuid == "random-1"
    assert index_map == {"random-1": [0]}


@pytest.mark.unit
def test_observe_mode_reports_without_mutating():
    """Observe must be able to answer "what would have happened" with zero payload change."""
    nodes = [_Node("random-1", "I")]
    edges = [_Edge("random-1", "other")]
    index_map = {"random-1": [0]}

    result = bind_canonical_self(nodes, edges, index_map, _declared(), SelfBindMode.OBSERVE)

    assert result.outcome is SelfBindOutcome.BOUND
    assert result.self_uuid == self_uuid_for_namespace("default")
    # ...and nothing moved.
    assert result.bound is False
    assert [n.uuid for n in nodes] == ["random-1"]
    assert edges[0].source_node_uuid == "random-1"
    assert index_map == {"random-1": [0]}


@pytest.mark.unit
def test_observe_surfaces_the_multi_alias_refusal_too():
    """Observe exists to find these before enforce can act on them."""
    nodes = [_Node("a", "me"), _Node("b", "I")]
    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, [], {}, _declared(), SelfBindMode.OBSERVE)


@pytest.mark.unit
def test_observe_does_not_trigger_the_resolver_bypass():
    """`bound` gates the dedup bypass. An observe-mode node still carries the extractor's uuid,
    so bypassing candidate search for it would strand it with no candidates and no resolution."""
    nodes = [_Node("random-1", "I")]
    result = bind_canonical_self(nodes, [], {}, _declared(), SelfBindMode.OBSERVE)
    assert result.outcome is SelfBindOutcome.BOUND
    assert result.bound is False


@pytest.mark.unit
def test_observe_still_surfaces_the_ambiguous_case():
    """Observe exists to find problems before enforce can cause them."""
    canonical = self_uuid_for_namespace("default")
    nodes = [_Node("random-1", "I"), _Node(canonical, "Rachel")]
    with pytest.raises(AmbiguousSelfBindingError):
        bind_canonical_self(nodes, [], {}, _declared(), SelfBindMode.OBSERVE)


# --------------------------------------------------------------------------- observability


@pytest.mark.unit
def test_telemetry_carries_no_content_or_entity_names():
    """Instrumentation must never leak memory text or arbitrary entity names -- the names in
    question are exactly the ones a user typed. Enums, counts, UUIDs and namespace only."""
    nodes = [_Node("random-1", "I"), _Node("secret", "Rachel's divorce lawyer")]
    ctx = _declared()
    result = bind_canonical_self(nodes, [], {}, ctx)

    details = result.telemetry_details(ctx)
    blob = repr(details)

    assert "Rachel" not in blob
    assert "divorce" not in blob
    assert details["outcome"] == "bound"
    assert details["namespace"] == "default"
    assert details["evidence_kind"] == "explicit_self_subject"
    assert details["rewritten_node_count"] == 1
    # The bound uuid is deterministic and derivable from the namespace, so it discloses nothing.
    assert details["self_uuid"] == self_uuid_for_namespace("default")


@pytest.mark.unit
def test_unclassified_self_like_emissions_are_counted():
    """Activation requires knowing whether self-like entities still arrive from untrusted
    producers -- a persistently non-zero count means recall is still being fragmented."""
    nodes = [_Node("a", "user"), _Node("b", "I"), _Node("c", "Rachel")]
    result = bind_canonical_self(nodes, [], {}, _untrusted())

    assert result.outcome is SelfBindOutcome.NOT_ELIGIBLE
    assert result.self_like_without_subject_authority == 2
    assert [n.uuid for n in nodes] == ["a", "b", "c"]


@pytest.mark.unit
def test_no_self_like_emission_counts_zero():
    nodes = [_Node("a", "Rachel")]
    result = bind_canonical_self(nodes, [], {}, _untrusted())
    assert result.self_like_without_subject_authority == 0


@pytest.mark.unit
def test_telemetry_without_identity_still_renders():
    """A None identity must not make the recorder raise inside an ingest."""
    result = bind_canonical_self([_Node("a", "user")], [], {}, None)
    details = result.telemetry_details(None)
    assert details["outcome"] == "not_eligible"
    assert "namespace" not in details


# --------------------------------------------------------------------------- refusal is recorded


def _recording_binder(monkeypatch, nodes, mode, identity=None):
    """Drive the production wrapper and capture what it recorded."""
    import menhir.infrastructure.telemetry.recorders as recorders
    from menhir.infrastructure.graphiti_extraction_patches import (
        _record_self_binding,
        begin_extraction_receipt,
        clear_extraction_receipt,
    )

    recorded: list[dict] = []
    monkeypatch.setattr(
        recorders,
        "record_lifecycle_event",
        lambda **kw: recorded.append(kw),
    )
    try:
        receipt = begin_extraction_receipt(
            "ep", "body", self_identity=identity or _declared(), self_bind_mode=mode
        )
        return _record_self_binding(nodes, [], {}, receipt), recorded
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
def test_an_ambiguous_refusal_is_recorded_before_it_raises(monkeypatch):
    """REVIEW P2. A refusal is a DECISION. Raising before recording makes the one outcome an
    operator most needs during an observation window the only invisible one."""
    nodes = [_Node("a", "I"), _Node("b", "me")]
    recorded: list[dict] = []

    import menhir.infrastructure.telemetry.recorders as recorders
    from menhir.infrastructure.graphiti_extraction_patches import (
        _record_self_binding,
        begin_extraction_receipt,
        clear_extraction_receipt,
    )

    monkeypatch.setattr(recorders, "record_lifecycle_event", lambda **kw: recorded.append(kw))
    try:
        receipt = begin_extraction_receipt(
            "ep", "body", self_identity=_declared(), self_bind_mode=SelfBindMode.ENFORCE
        )
        with pytest.raises(AmbiguousSelfBindingError):
            _record_self_binding(nodes, [], {}, receipt)
    finally:
        clear_extraction_receipt()

    assert recorded, "the refusal raised without recording the decision"
    assert recorded[0]["details"]["outcome"] == "ambiguous"


@pytest.mark.unit
def test_observe_records_the_refusal_and_does_not_fail_the_episode(monkeypatch):
    """Observe exists to measure what enforce would do WITHOUT changing behavior. Propagating the
    refusal there would make merely observing a durable change in ingest success."""
    nodes = [_Node("a", "I"), _Node("b", "me")]

    result, recorded = _recording_binder(monkeypatch, nodes, SelfBindMode.OBSERVE)

    assert result.outcome is SelfBindOutcome.AMBIGUOUS
    assert [n.uuid for n in nodes] == ["a", "b"]
    assert recorded, "the refusal produced no telemetry"
    details = recorded[0]["details"]
    assert details["outcome"] == "ambiguous"
    assert details["self_like_without_subject_authority"] == 2


@pytest.mark.unit
def test_self_like_unresolved_is_recorded_as_its_own_outcome(monkeypatch):
    """The count that says how often a trusted turn mentions a `user` binding declines to claim."""
    result, recorded = _recording_binder(
        monkeypatch, [_Node("rbac", "user")], SelfBindMode.ENFORCE, identity=_trusted()
    )

    assert result.outcome is SelfBindOutcome.SELF_LIKE_UNRESOLVED
    assert recorded[0]["details"]["outcome"] == "self_like_unresolved"
