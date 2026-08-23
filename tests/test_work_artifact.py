"""Unit tests for the WorkArtifact domain model and repository."""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

import pytest

from menhir.domain.work_artifact import (
    ARTIFACT_RELATIONS,
    ArtifactMedium,
    ArtifactSourceSpec,
    ArtifactStatus,
    ArtifactType,
    DeclarationKind,
    DeclarationStatus,
    QuestionStatus,
    can_transition,
    normalize_declarations,
    relation_is_legal,
    status_from_header,
    valid_statuses,
)
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_progresses_through_its_forward_path() -> None:
    p = ArtifactType.PLAN
    assert can_transition(p, ArtifactStatus.PROPOSED, ArtifactStatus.REVIEWED)
    assert can_transition(p, ArtifactStatus.REVIEWED, ArtifactStatus.APPROVED)
    assert can_transition(p, ArtifactStatus.APPROVED, ArtifactStatus.IMPLEMENTING)
    assert can_transition(p, ArtifactStatus.IMPLEMENTING, ArtifactStatus.IMPLEMENTED)


@pytest.mark.unit
def test_forward_path_cannot_be_skipped() -> None:
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.PROPOSED, ArtifactStatus.IMPLEMENTED)
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.PROPOSED, ArtifactStatus.APPROVED)


@pytest.mark.unit
def test_no_backward_transitions() -> None:
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.APPROVED, ArtifactStatus.PROPOSED)


@pytest.mark.unit
@pytest.mark.parametrize("terminal", [ArtifactStatus.SUPERSEDED, ArtifactStatus.DEFERRED])
def test_terminal_states_are_reachable_from_anywhere(terminal: str) -> None:
    for start in (ArtifactStatus.PROPOSED, ArtifactStatus.APPROVED, ArtifactStatus.IMPLEMENTED):
        assert can_transition(ArtifactType.PLAN, start, terminal)


@pytest.mark.unit
def test_deferred_is_distinct_from_superseded() -> None:
    """SUPERSEDED means a better answer exists; DEFERRED means not answered yet."""
    assert ArtifactStatus.DEFERRED in valid_statuses(ArtifactType.PLAN)
    assert ArtifactStatus.SUPERSEDED in valid_statuses(ArtifactType.PLAN)
    assert ArtifactStatus.DEFERRED != ArtifactStatus.SUPERSEDED
    # Neither leads anywhere, including to each other.
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.DEFERRED, ArtifactStatus.SUPERSEDED)


@pytest.mark.unit
def test_self_transition_is_refused() -> None:
    """A no-op would still stamp status_changed_at, faking movement."""
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.APPROVED, ArtifactStatus.APPROVED)


@pytest.mark.unit
def test_types_have_separate_lifecycles() -> None:
    assert can_transition(ArtifactType.IMPLEMENTATION_REPORT, ArtifactStatus.DRAFT, ArtifactStatus.COMPLETE)
    # DRAFT is not a plan state at all.
    assert ArtifactStatus.DRAFT not in valid_statuses(ArtifactType.PLAN)
    assert not can_transition(ArtifactType.PLAN, ArtifactStatus.DRAFT, ArtifactStatus.COMPLETE)


@pytest.mark.unit
def test_unknown_type_permits_nothing() -> None:
    assert not can_transition("orientation", ArtifactStatus.OPEN, ArtifactStatus.COMPLETE)


# ---------------------------------------------------------------------------
# Status header migration
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Proposal", ArtifactStatus.PROPOSED),
        ("IMPLEMENTED", ArtifactStatus.IMPLEMENTED),
        ("reviewer-approved", ArtifactStatus.REVIEWED),
        ("**SUPERSEDED**", ArtifactStatus.SUPERSEDED),
        ("DEFERRED", ArtifactStatus.DEFERRED),
        ("IN PROGRESS", ArtifactStatus.IMPLEMENTING),
    ],
)
def test_real_corpus_headers_map_to_typed_states(raw: str, expected: str) -> None:
    status, reason = status_from_header(raw, ArtifactType.PLAN)
    assert status == expected
    assert reason is None


@pytest.mark.unit
def test_header_with_trailing_commentary_still_maps() -> None:
    status, _ = status_from_header("IMPLEMENTED (Phases 1-3) — see commit abc", ArtifactType.PLAN)
    assert status == ArtifactStatus.IMPLEMENTED


@pytest.mark.unit
def test_unrecognized_header_is_unresolved_not_guessed() -> None:
    status, reason = status_from_header("mostly done ish", ArtifactType.PLAN)
    assert status is None
    assert reason == "unrecognized_status"


@pytest.mark.unit
def test_missing_header_is_reported() -> None:
    assert status_from_header(None, ArtifactType.PLAN)[1] == "no_status_header"
    assert status_from_header("  ", ArtifactType.PLAN)[1] == "no_status_header"


@pytest.mark.unit
def test_status_valid_for_another_type_is_refused_not_coerced() -> None:
    """DRAFT is a report state; coercing it onto a plan would invent history."""
    status, reason = status_from_header("DRAFT", ArtifactType.PLAN)
    assert status is None
    assert reason == "status_invalid_for_type"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_artifact_defaults_to_the_types_initial_status() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    result = repo.create_artifact(artifact_type=ArtifactType.PLAN, title="A plan")

    assert result["status"] == ArtifactStatus.PROPOSED
    assert result["namespace"] == "default"


@pytest.mark.unit
def test_create_artifact_rejects_unknown_type() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    with pytest.raises(ValueError, match="unknown artifact_type"):
        repo.create_artifact(artifact_type="rfc", title="x")


@pytest.mark.unit
def test_create_artifact_rejects_status_illegal_for_type() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    with pytest.raises(ValueError, match="not valid for"):
        repo.create_artifact(
            artifact_type=ArtifactType.PLAN, title="x", status=ArtifactStatus.DRAFT
        )


@pytest.mark.unit
def test_create_artifact_accepts_explicit_status_for_migration() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    result = repo.create_artifact(
        artifact_type=ArtifactType.PLAN, title="x", status=ArtifactStatus.IMPLEMENTED
    )

    assert result["status"] == ArtifactStatus.IMPLEMENTED


@pytest.mark.unit
def test_embodiment_flattens_medium_specific_locator_legs() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="x",
        source=ArtifactSourceSpec(
            medium=ArtifactMedium.MARKDOWN,
            locator={"repository": "menhir", "path": ".agent/plans/x.md"},
            version="abc123",
        ),
    )

    props = neo4j.calls[1]["params"]["props"]
    assert props["medium"] == "markdown"
    assert props["locator_repository"] == "menhir"
    assert props["locator_path"] == ".agent/plans/x.md"
    assert props["version"] == "abc123"


@pytest.mark.unit
def test_wiki_embodiment_needs_no_schema_change() -> None:
    """The source contract is medium-neutral, not Git-shaped."""
    spec = ArtifactSourceSpec(
        medium=ArtifactMedium.WIKI, locator={"page_id": "4711"}, version="rev-9"
    )
    props = spec.as_properties()
    assert props["locator_page_id"] == "4711"
    assert props["version"] == "rev-9"
    assert props["integrity"] is None


@pytest.mark.unit
def test_unknown_medium_is_rejected() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    with pytest.raises(ValueError, match="unknown medium"):
        repo.create_artifact(
            artifact_type=ArtifactType.PLAN,
            title="x",
            source=ArtifactSourceSpec(medium="papyrus", locator={}),
        )


@pytest.mark.unit
def test_relocate_updates_the_locator_rather_than_adding_an_embodiment() -> None:
    neo4j = _StubNeo4j(responses=[[{"updated": 1}]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.relocate_source("a1", ArtifactMedium.MARKDOWN, {"path": "archive/x.md"})

    query = neo4j.calls[0]["query"]
    assert "SET s +=" in query
    assert "CREATE" not in query
    assert neo4j.calls[0]["params"]["props"]["locator_path"] == "archive/x.md"


@pytest.mark.unit
def test_code_references_reuse_the_todo_location_normalizer() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    result = repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="x",
        code_refs="src/a.py:12, src/b.py",
    )

    assert [(l["path"], l["line_start"]) for l in result["locations"]] == [
        ("src/a.py", 12),
        ("src/b.py", None),
    ]


@pytest.mark.unit
def test_transition_checks_against_stored_status_not_caller_assertion() -> None:
    neo4j = _StubNeo4j(responses=[[{"artifact_type": "plan", "status": "PROPOSED"}]])
    repo = WorkArtifactRepository(neo4j)

    result = repo.transition_status("a1", ArtifactStatus.IMPLEMENTED)

    assert result["applied"] is False
    assert result["reason"] == "illegal_transition"
    assert result["from_status"] == "PROPOSED"


@pytest.mark.unit
def test_legal_transition_applies() -> None:
    neo4j = _StubNeo4j(responses=[[{"artifact_type": "plan", "status": "PROPOSED"}], []])
    repo = WorkArtifactRepository(neo4j)

    result = repo.transition_status("a1", ArtifactStatus.REVIEWED)

    assert result["applied"] is True
    assert "SET a.status = $to_status" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_transition_on_missing_artifact_reports_reason() -> None:
    repo = WorkArtifactRepository(_StubNeo4j(responses=[[]]))

    assert repo.transition_status("nope", ArtifactStatus.REVIEWED)["reason"] == "artifact_not_found"


@pytest.mark.unit
def test_delete_cascades_to_owned_subordinates() -> None:
    neo4j = _StubNeo4j(responses=[[{"found": 1}]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.delete_artifact("a1") is True
    query = neo4j.calls[0]["query"]
    assert "EMBODIED_IN" in query
    assert "HAS_LOCATION" in query
    assert "DETACH DELETE a, s, l" in query


@pytest.mark.unit
def test_list_without_namespace_spans_every_silo() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.list_artifacts()

    assert neo4j.calls[0]["params"]["namespaces"] is None


@pytest.mark.unit
def test_list_with_namespace_includes_the_shared_bucket() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.list_artifacts(namespace="menhir")

    assert neo4j.calls[0]["params"]["namespaces"] == ["menhir", "default"]


# ---------------------------------------------------------------------------
# Declared relationships (slice 2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "relation,source,target,ok",
    [
        ("reviews", ArtifactType.REVIEW, ArtifactType.PLAN, True),
        ("implements", ArtifactType.IMPLEMENTATION_REPORT, ArtifactType.PLAN, True),
        ("informs", ArtifactType.INVESTIGATION, ArtifactType.PLAN, True),
        # A plan does not review another plan.
        ("reviews", ArtifactType.PLAN, ArtifactType.PLAN, False),
        # A plan is not an implementation report.
        ("implements", ArtifactType.PLAN, ArtifactType.PLAN, False),
        # A report is not implemented by anything.
        ("implements", ArtifactType.IMPLEMENTATION_REPORT, ArtifactType.REVIEW, False),
    ],
)
def test_relation_type_constraints(relation: str, source: str, target: str, ok: bool) -> None:
    assert relation_is_legal(relation, source, target)[0] is ok


@pytest.mark.unit
def test_unknown_relation_is_distinguished_from_wrong_types() -> None:
    assert relation_is_legal("blesses", ArtifactType.REVIEW, ArtifactType.PLAN)[1] == "unsupported_relation"
    assert relation_is_legal("implements", ArtifactType.PLAN, ArtifactType.PLAN)[1] == "illegal_source_type"
    assert relation_is_legal("implements", ArtifactType.IMPLEMENTATION_REPORT, ArtifactType.REVIEW)[1] == "illegal_target_type"


@pytest.mark.unit
def test_supersedes_is_not_a_declarable_relation() -> None:
    """It ships only with the lifecycle command that also moves status."""
    assert "supersedes" not in ARTIFACT_RELATIONS
    repo = WorkArtifactRepository(_StubNeo4j())
    assert repo.link_artifacts("a", "b", "supersedes")["reason"] == "unsupported_relation"


@pytest.mark.unit
def test_unsupported_relation_never_reaches_a_query() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    repo.link_artifacts("a", "b", "DELETE]->() DETACH DELETE a //")

    assert neo4j.calls == []


@pytest.mark.unit
def test_link_validates_against_stored_types() -> None:
    neo4j = _StubNeo4j(responses=[[{
        "source_type": "plan", "target_type": "plan",
        "source_ns": "default", "target_ns": "default",
    }]])
    repo = WorkArtifactRepository(neo4j)

    result = repo.link_artifacts("a", "b", "reviews")

    assert result["linked"] is False
    assert result["reason"] == "illegal_source_type"
    assert len(neo4j.calls) == 1  # no write issued


@pytest.mark.unit
def test_link_refuses_cross_namespace() -> None:
    neo4j = _StubNeo4j(responses=[[{
        "source_type": "review", "target_type": "plan",
        "source_ns": "menhir", "target_ns": "yawn.market",
    }]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.link_artifacts("a", "b", "reviews")["reason"] == "namespace_incompatible"


@pytest.mark.unit
def test_link_allows_shared_default_bucket() -> None:
    neo4j = _StubNeo4j(responses=[[{
        "source_type": "review", "target_type": "plan",
        "source_ns": "menhir", "target_ns": "default",
    }], []])
    repo = WorkArtifactRepository(neo4j)

    assert repo.link_artifacts("a", "b", "reviews")["linked"] is True


@pytest.mark.unit
def test_link_is_idempotent_via_merge() -> None:
    neo4j = _StubNeo4j(responses=[[{
        "source_type": "review", "target_type": "plan",
        "source_ns": "default", "target_ns": "default",
    }], []])
    repo = WorkArtifactRepository(neo4j)

    repo.link_artifacts("a", "b", "reviews")

    assert "MERGE (s)-[:REVIEWS]->(t)" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_supersede_moves_status_and_edge_in_one_statement() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = WorkArtifactRepository(neo4j)

    result = repo.supersede_artifact("new", "old")

    assert result["applied"] is True
    assert len(neo4j.calls) == 1
    query = neo4j.calls[0]["query"]
    assert "MERGE (new)-[:SUPERSEDES]->(old)" in query
    assert "SET old.status = $superseded" in query


@pytest.mark.unit
def test_supersede_requires_same_type_and_non_terminal() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = WorkArtifactRepository(neo4j)

    repo.supersede_artifact("new", "old")

    query = neo4j.calls[0]["query"]
    assert "new.artifact_type = old.artifact_type" in query
    assert "NOT old.status IN $terminal" in query
    assert "new.artifact_uuid <> old.artifact_uuid" in query


@pytest.mark.unit
def test_supersede_refusal_reports_reason() -> None:
    repo = WorkArtifactRepository(_StubNeo4j(responses=[[{"applied": 0}]]))

    assert repo.supersede_artifact("new", "old")["applied"] is False


@pytest.mark.unit
def test_subject_targets_an_existing_entity_not_a_new_label() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.link_subject("a", "e1")["edge_type"] == "ABOUT"
    query = neo4j.calls[0]["query"]
    assert "MATCH (e:Entity {uuid: $entity_uuid})" in query
    assert ":Subject" not in query


@pytest.mark.unit
def test_subject_excludes_structural_nodes() -> None:
    """A file is not a subject."""
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = WorkArtifactRepository(neo4j)

    repo.link_subject("a", "e1")

    query = neo4j.calls[0]["query"]
    assert "e.scope = 'PERSISTENT'" in query
    assert "e.structure_role IS NULL" in query


@pytest.mark.unit
def test_todo_reference_uses_a_distinct_edge_type() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.link_todo("a", "t1")["edge_type"] == "REFERENCES_TODO"


# ---------------------------------------------------------------------------
# Open questions (slice 3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_open_questions_preserve_author_ordering_and_get_addressable_uuids() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    rows = repo.add_open_questions("a1", ["First?", "Second?", "Third?"])

    assert [r["ordinal"] for r in rows] == [0, 1, 2]
    assert [r["text"] for r in rows] == ["First?", "Second?", "Third?"]
    assert all(r["question_uuid"] for r in rows)
    assert all(r["status"] == QuestionStatus.OPEN for r in rows)


@pytest.mark.unit
def test_blank_questions_are_skipped_without_shifting_ordinals() -> None:
    repo = WorkArtifactRepository(_StubNeo4j())

    rows = repo.add_open_questions("a1", ["Real?", "   ", "", "Also real?"])

    assert [r["ordinal"] for r in rows] == [0, 1]
    assert [r["text"] for r in rows] == ["Real?", "Also real?"]


@pytest.mark.unit
def test_no_write_when_every_question_is_blank() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    assert repo.add_open_questions("a1", ["  ", ""]) == []
    assert neo4j.calls == []


@pytest.mark.unit
def test_answering_records_evidence_and_status_in_one_statement() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = WorkArtifactRepository(neo4j)

    result = repo.answer_question("q1", "a2")

    assert result["applied"] is True
    assert len(neo4j.calls) == 1
    query = neo4j.calls[0]["query"]
    assert "MERGE (a)-[:ANSWERS_QUESTION]->(q)" in query
    assert "SET q.status = $answered" in query


@pytest.mark.unit
def test_only_an_open_question_may_be_answered() -> None:
    """Re-answering would overwrite which artifact actually resolved it.

    CF-48 moved WHICH statuses are answerable into `domain.work_artifact`, so the guard is now
    spelled `q.status IN $answerable` and the admissible set is supplied by the domain. This
    asserts the bound set rather than the clause text: the property under test is "only an open
    question", and pinning the literal Cypher tested the spelling instead -- which is why this
    test, and not the behaviour, broke when the rule moved.
    """
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = WorkArtifactRepository(neo4j)

    repo.answer_question("q1", "a2")

    assert neo4j.calls[0]["params"]["answerable"] == [QuestionStatus.OPEN]


@pytest.mark.unit
def test_answer_refusal_reports_reason() -> None:
    repo = WorkArtifactRepository(_StubNeo4j(responses=[[{"applied": 0}]]))

    result = repo.answer_question("q1", "a2")

    assert result["applied"] is False
    assert result["reason"] == "question_not_open_or_artifact_missing"


@pytest.mark.unit
def test_deferring_needs_no_answering_artifact() -> None:
    """Deferring is a decision, not an answer -- evidence of a non-event."""
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = WorkArtifactRepository(neo4j)

    assert repo.defer_question("q1")["status"] == QuestionStatus.DEFERRED
    assert "ANSWERS_QUESTION" not in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_open_questions_query_defaults_to_open_only() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.open_questions()

    assert neo4j.calls[0]["params"]["status"] == QuestionStatus.OPEN
    assert neo4j.calls[0]["params"]["artifact_uuid"] is None


@pytest.mark.unit
def test_open_questions_namespace_read_off_the_owning_artifact() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.open_questions(namespace="menhir")

    assert neo4j.calls[0]["params"]["namespaces"] == ["menhir", "default"]
    # The filter is on the artifact, not a namespace copied onto the question.
    assert "a.namespace IN $namespaces" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_delete_cascades_to_open_questions() -> None:
    neo4j = _StubNeo4j(responses=[[{"found": 1}]])
    repo = WorkArtifactRepository(neo4j)

    repo.delete_artifact("a1")

    query = neo4j.calls[0]["query"]
    assert "HAS_OPEN_QUESTION" in query
    assert "DETACH DELETE a, s, l, q, d" in query


# ---------------------------------------------------------------------------
# Wrapup lifecycle and status headers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_report_carries_the_documented_wrapup_path() -> None:
    """WRAPUP-TEMPLATE runs DRAFT -> READY FOR REVIEW -> REVIEWED."""
    r = ArtifactType.IMPLEMENTATION_REPORT
    assert can_transition(r, ArtifactStatus.DRAFT, ArtifactStatus.READY_FOR_REVIEW)
    assert can_transition(r, ArtifactStatus.READY_FOR_REVIEW, ArtifactStatus.REVIEWED)
    assert can_transition(r, ArtifactStatus.REVIEWED, ArtifactStatus.COMPLETE)


@pytest.mark.unit
def test_review_cannot_be_skipped_backwards_on_a_report() -> None:
    r = ArtifactType.IMPLEMENTATION_REPORT
    assert not can_transition(r, ArtifactStatus.REVIEWED, ArtifactStatus.READY_FOR_REVIEW)
    assert not can_transition(r, ArtifactStatus.COMPLETE, ArtifactStatus.REVIEWED)


@pytest.mark.unit
def test_ready_for_review_is_not_a_plan_state() -> None:
    assert ArtifactStatus.READY_FOR_REVIEW not in valid_statuses(ArtifactType.PLAN)
    assert ArtifactStatus.READY_FOR_REVIEW in valid_statuses(ArtifactType.IMPLEMENTATION_REPORT)


@pytest.mark.unit
@pytest.mark.parametrize(
    "header,expected",
    [
        ("**SUPERSEDED** by `menhir-todo-declared-links.md`", ArtifactStatus.SUPERSEDED),
        ("APPROVED as the basis for future implementation", ArtifactStatus.APPROVED),
        ("READY FOR REVIEW", ArtifactStatus.READY_FOR_REVIEW),
        ("READY FOR REVIEW (all checks pass)", ArtifactStatus.READY_FOR_REVIEW),
        ("REVIEWED WITH FINDINGS", ArtifactStatus.REVIEWED),
        ("PARTIAL - verification incomplete", ArtifactStatus.DRAFT),
    ],
)
def test_leading_token_is_the_state_and_the_rest_is_commentary(
    header: str, expected: str
) -> None:
    status, reason = status_from_header(header, ArtifactType.IMPLEMENTATION_REPORT
                                        if expected in (ArtifactStatus.READY_FOR_REVIEW,
                                                        ArtifactStatus.REVIEWED,
                                                        ArtifactStatus.DRAFT)
                                        else ArtifactType.PLAN)
    assert (status, reason) == (expected, None)


@pytest.mark.unit
def test_a_state_named_mid_sentence_is_discussed_not_declared() -> None:
    """Matching walks inward to the first word and stops -- never scans deeper."""
    status, reason = status_from_header(
        "Research / Prototype - not yet APPROVED", ArtifactType.PLAN
    )
    assert status is None
    assert reason == "unrecognized_status"


@pytest.mark.unit
def test_raw_status_is_stored_alongside_the_mapped_one() -> None:
    """An artifact in its initial state because nobody could read its header
    must be distinguishable from one that genuinely is in that state."""
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="t",
        status_raw="ACTIVE - supersedes the ad-hoc phase",
        status_unresolved_reason="unrecognized_status",
    )

    params = neo4j.calls[0]["params"]
    assert params["status"] == ArtifactStatus.PROPOSED
    assert params["status_raw"] == "ACTIVE - supersedes the ad-hoc phase"
    assert params["status_unresolved_reason"] == "unrecognized_status"


# ---------------------------------------------------------------------------
# Frontmatter declarations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_declarations_flatten_in_author_order_across_keys() -> None:
    refs, ignored = normalize_declarations({
        "implements": ["oauth-plan", "token-plan"],
        "about": ["OAuth"],
    })

    assert [(r.key, r.raw_target, r.ordinal) for r in refs] == [
        ("implements", "oauth-plan", 0),
        ("implements", "token-plan", 1),
        ("about", "OAuth", 2),
    ]
    assert ignored == []


@pytest.mark.unit
def test_scalar_value_is_accepted_as_a_single_target() -> None:
    refs, _ = normalize_declarations({"supersedes": "oauth-v1"})

    assert [r.raw_target for r in refs] == ["oauth-v1"]
    assert refs[0].kind == DeclarationKind.SUPERSESSION


@pytest.mark.unit
def test_unknown_frontmatter_keys_are_reported_not_silently_skipped() -> None:
    """Frontmatter carries unrelated metadata; a caller deserves to know."""
    refs, ignored = normalize_declarations({"author": "ctharvey", "about": ["OAuth"]})

    assert [r.key for r in refs] == ["about"]
    assert ignored == [{"key": "author", "reason": "unsupported_declaration_key"}]


@pytest.mark.unit
def test_blank_targets_are_dropped_without_consuming_an_ordinal() -> None:
    refs, _ = normalize_declarations({"implements": ["a", "", "   ", "b"]})

    assert [(r.raw_target, r.ordinal) for r in refs] == [("a", 0), ("b", 1)]


@pytest.mark.unit
def test_declaration_is_stored_before_resolution_is_attempted() -> None:
    """The declaration is the durable record; the edge is a consequence."""
    neo4j = _StubNeo4j(responses=[[], []])
    repo = WorkArtifactRepository(neo4j)

    repo.declare_from_frontmatter("a1", {"implements": ["oauth-plan"]})

    write = neo4j.calls[0]
    assert "MERGE (a)-[:DECLARES]->(d:ArtifactDeclaration {" in write["query"]
    assert write["params"]["rows"][0]["raw_target"] == "oauth-plan"


@pytest.mark.unit
def test_redeclaring_unchanged_frontmatter_does_not_duplicate() -> None:
    """MERGE on (owner, key, raw_target); ON CREATE guards the uuid."""
    neo4j = _StubNeo4j(responses=[[], []])
    repo = WorkArtifactRepository(neo4j)

    repo.declare_from_frontmatter("a1", {"about": ["OAuth"]})

    query = neo4j.calls[0]["query"]
    assert "key: row.key, raw_target: row.raw_target" in query
    assert "ON CREATE SET d.declaration_uuid" in query


@pytest.mark.unit
def test_unresolvable_declaration_is_retained_rather_than_dropped() -> None:
    """A rename must not silently delete a relationship."""
    neo4j = _StubNeo4j(responses=[
        [],  # write
        [{"declaration_uuid": "d1", "key": "implements",
          "kind": DeclarationKind.ARTIFACT_RELATION, "raw_target": "gone-plan"}],
        [],  # _match_artifact: no candidates
        [],  # stamp
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"implements": ["gone-plan"]})

    assert out["results"][0]["status"] == DeclarationStatus.UNRESOLVED
    assert out["results"][0]["reason"] == "target_not_found"
    stamp = neo4j.calls[-1]["params"]
    assert stamp["status"] == DeclarationStatus.UNRESOLVED
    assert stamp["target_uuid"] is None


@pytest.mark.unit
def test_ambiguous_target_refuses_rather_than_picking_one() -> None:
    neo4j = _StubNeo4j(responses=[
        [],
        [{"declaration_uuid": "d1", "key": "implements",
          "kind": DeclarationKind.ARTIFACT_RELATION, "raw_target": "plan"}],
        [{"artifact_uuid": "p1"}, {"artifact_uuid": "p2"}],
        [],
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"implements": ["plan"]})

    assert out["results"][0]["reason"] == "ambiguous_target"
    assert out["results"][0]["status"] == DeclarationStatus.UNRESOLVED


@pytest.mark.unit
def test_found_target_with_illegal_relation_is_rejected_not_unresolved() -> None:
    """'No such target' and 'illegal relationship' are different mistakes."""
    neo4j = _StubNeo4j(responses=[
        [],
        [{"declaration_uuid": "d1", "key": "reviews",
          "kind": DeclarationKind.ARTIFACT_RELATION, "raw_target": "other-plan"}],
        [{"artifact_uuid": "p1"}],
        # link_artifacts type lookup: a plan cannot review anything
        [{"source_type": ArtifactType.PLAN, "target_type": ArtifactType.PLAN,
          "source_ns": "menhir", "target_ns": "menhir"}],
        [],
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"reviews": ["other-plan"]})

    assert out["results"][0]["status"] == DeclarationStatus.REJECTED
    assert out["results"][0]["reason"] == "illegal_source_type"
    assert out["results"][0]["target_uuid"] == "p1"


@pytest.mark.unit
def test_supersedes_declaration_routes_through_the_lifecycle_command() -> None:
    """The declaring document is the authority for the old plan's status."""
    neo4j = _StubNeo4j(responses=[
        [],
        [{"declaration_uuid": "d1", "key": "supersedes",
          "kind": DeclarationKind.SUPERSESSION, "raw_target": "oauth-v1"}],
        [{"artifact_uuid": "old"}],
        [{"applied": 1}],
        [],
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"supersedes": ["oauth-v1"]})

    supersede_query = neo4j.calls[3]["query"]
    assert "SUPERSEDES" in supersede_query
    assert "SET old.status = $superseded" in supersede_query
    assert out["results"][0]["status"] == DeclarationStatus.RESOLVED


@pytest.mark.unit
def test_missing_subject_is_not_minted_as_an_unembedded_entity() -> None:
    """A silently unrecallable :Entity is worse than an honest unresolved."""
    neo4j = _StubNeo4j(responses=[
        [],
        [{"declaration_uuid": "d1", "key": "about",
          "kind": DeclarationKind.SUBJECT, "raw_target": "blast radius"}],
        [],  # no such entity
        [],
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"about": ["blast radius"]})

    assert out["results"][0]["reason"] == "subject_not_found"
    assert not any("CREATE (e:Entity" in c["query"] for c in neo4j.calls)


@pytest.mark.unit
def test_todo_declarations_match_on_uuid_never_on_content() -> None:
    neo4j = _StubNeo4j(responses=[
        [],
        [{"declaration_uuid": "d1", "key": "todos",
          "kind": DeclarationKind.TODO, "raw_target": "b65e906d"}],
        [{"todo_uuid": "b65e906d-full"}],
        [{"linked": 1}],
        [],
    ])
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"todos": ["b65e906d"]})

    match_query = neo4j.calls[2]["query"]
    assert "t.uuid STARTS WITH $needle" in match_query
    assert "content" not in match_query
    assert out["results"][0]["status"] == DeclarationStatus.RESOLVED


@pytest.mark.unit
def test_resolution_retries_only_the_unresolved() -> None:
    """A later pass resolves a target that had not been ingested yet."""
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.resolve_declarations("a1")

    query = neo4j.calls[0]["query"]
    assert "d.resolution_status <> $resolved" in query
    assert neo4j.calls[0]["params"]["resolved"] == DeclarationStatus.RESOLVED


@pytest.mark.unit
def test_no_declarations_writes_nothing() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    out = repo.declare_from_frontmatter("a1", {"author": "ctharvey"})

    assert out["declared"] == 0
    assert neo4j.calls == []


class _StubWorkArtifactRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        self.calls.append((new_uuid, old_uuid))
        return {"applied": True, "artifact_uuid": new_uuid}


@pytest.mark.unit
def test_supersede_artifact_routes_to_work_artifacts_not_shadowed_by_l4() -> None:
    """The L4 rename must leave the live supersede_artifact unshadowed.

    Previously a second definition shadowed this one; calling it wrote to the
    L4 Entity store and returned a bool instead of a dict. Now the adapter
    must forward (new_uuid, old_uuid) to the WorkArtifact repository and
    return its dict unchanged.
    """
    repo = _StubWorkArtifactRepo()
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._work_artifacts = repo

    result = adapter.supersede_artifact("new-uuid", "old-uuid")

    assert repo.calls == [("new-uuid", "old-uuid")]
    assert isinstance(result, dict)
    assert result["applied"] is True
