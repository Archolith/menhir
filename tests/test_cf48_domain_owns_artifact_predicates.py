"""CF-48 -- the aggregate rules the repository was still deciding for itself.

CF-48's first wave moved three of nine sites into `domain/work_artifact.py`. The remaining six
were deferred with a single stated reason: they are *"structural guards written directly into
Cypher, where the predicate and the mutation are one expression."*

**That reason is not true of all of them, which is why this second wave exists.**
`link_artifacts`' namespace check is plain Python operating on values a previous query already
returned -- no Cypher, no atomicity argument, and it was grouped with the expensive sites by a
rationale that does not describe it. The two question-state guards do sit in Cypher, but the thing
that belongs in the domain is not the compare-and-set (that must stay, or an answered question can
exist with no answering edge) -- it is *which statuses are answerable*, which was written down
nowhere except inside two WHERE clauses.

What is deliberately NOT done here, and why:

* `link_todo`'s guard compares two nodes inside one MATCH; only the emitted-fragment half moves.
* `link_subject`'s `e.scope = 'PERSISTENT' AND e.structure_role IS NULL` is left alone. The domain
  does expose `non_structural_memory_cypher`, and it is tempting -- but it is a DIFFERENT
  predicate: it also excludes evidence projections and legacy structure rows, and it does not
  check scope. Substituting it would tighten what may be a subject while wearing the costume of a
  refactor. Recorded as a behaviour question, not taken.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

import pytest

from menhir.domain import work_artifact as domain
from menhir.domain.work_artifact import (
    DEFAULT_ARTIFACT_NAMESPACE,
    QuestionStatus,
    namespace_compatibility_cypher,
    namespaces_are_compatible,
    question_can_transition,
    question_statuses_allowing,
    supersession_cypher,
)
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

_REPO_SOURCE = pathlib.Path(
    "src/menhir/infrastructure/work_artifact_repository.py"
).read_text(encoding="utf-8")


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
# The namespace rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_subordinate_in_the_owners_namespace_is_compatible() -> None:
    assert namespaces_are_compatible("tenant-a", "tenant-a")


@pytest.mark.unit
def test_a_subordinate_in_the_shared_namespace_is_compatible() -> None:
    assert namespaces_are_compatible("tenant-a", DEFAULT_ARTIFACT_NAMESPACE)


@pytest.mark.unit
def test_a_foreign_namespace_is_refused() -> None:
    assert not namespaces_are_compatible("tenant-a", "tenant-b")


@pytest.mark.unit
def test_the_rule_is_asymmetric_and_shared_cannot_reach_into_a_silo() -> None:
    """THE TENANCY PROPERTY. A namespaced artifact may reach shared records; a shared artifact
    reaching a tenant's records would be a cross-silo read granted by direction alone."""
    assert namespaces_are_compatible("tenant-a", DEFAULT_ARTIFACT_NAMESPACE)
    assert not namespaces_are_compatible(DEFAULT_ARTIFACT_NAMESPACE, "tenant-a")


@pytest.mark.unit
def test_link_artifacts_refuses_a_foreign_namespace_through_the_domain_rule() -> None:
    """The behaviour, not the spelling: the refusal survives the predicate moving."""
    neo4j = _StubNeo4j(
        responses=[[{
            "source_type": "review",
            "target_type": "plan",
            "source_ns": "tenant-a",
            "target_ns": "tenant-b",
        }]]
    )

    result = WorkArtifactRepository(neo4j).link_artifacts("s", "t", "reviews")

    assert result["linked"] is False
    assert result["reason"] == "namespace_incompatible", (
        "the relation and both types are legal here, so this must be the namespace refusal "
        "and not an earlier guard answering for it"
    )
    assert len(neo4j.calls) == 1, "refused link must not have written an edge"


@pytest.mark.unit
def test_the_emitted_fragment_puts_the_subordinate_on_the_left() -> None:
    """Direction is the whole rule. Reversed, this grants exactly the cross-silo reach the
    Python form refuses, and every caller of the emitter would inherit it at once."""
    assert (
        namespace_compatibility_cypher(owner="a", subordinate="t")
        == "t.namespace IN [a.namespace, $default_ns]"
    )


@pytest.mark.unit
def test_the_supersession_predicate_is_byte_identical_after_the_extraction() -> None:
    """NO BEHAVIOUR CHANGE, asserted rather than assumed. This is the shipped Cypher for the only
    path that creates SUPERSEDES; a refactor that moved the rule must not have moved the query."""
    assert supersession_cypher() == (
        "new.artifact_type = old.artifact_type\n"
        "              AND new.artifact_uuid <> old.artifact_uuid\n"
        "              AND NOT old.status IN $terminal\n"
        "              AND old.namespace IN [new.namespace, $default_ns]"
    )


@pytest.mark.unit
def test_supersession_is_emitted_from_the_shared_namespace_fragment() -> None:
    """Supersession and todo-linking enforced the same rule in two hand-written copies. They
    agreed, and nothing kept them agreeing -- the shape CF-150 already found once in this file."""
    assert namespace_compatibility_cypher(owner="new", subordinate="old") in supersession_cypher()


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda r: r.link_todo("a1", "t1"), id="link_todo"),
        pytest.param(lambda r: r.supersede_artifact("new1", "old1"), id="supersede_artifact"),
        pytest.param(lambda r: r._match_artifact("a1", "some plan"), id="_match_artifact"),
        pytest.param(lambda r: r._resolve_todo("a1", "abc123"), id="_resolve_todo"),
    ],
)
def test_every_site_emitting_the_fragment_binds_the_parameter_it_references(call) -> None:
    """The failure a stubbed unit test cannot see on its own.

    The emitted fragment references `$default_ns`. A call site that adopts the emitter but forgets
    the binding raises ParameterMissing from Neo4j at runtime and passes every offline test, since
    a stub driver never parses the query. So the parameter references are checked against the
    bound dict directly, for all four sites that emit it.
    """
    neo4j = _StubNeo4j(responses=[[]])

    call(WorkArtifactRepository(neo4j))

    assert neo4j.calls, "the site under test issued no query"
    for recorded in neo4j.calls:
        referenced = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", recorded["query"]))
        missing = referenced - set(recorded["params"])
        assert not missing, f"query references unbound parameter(s): {sorted(missing)}"


@pytest.mark.unit
def test_the_repository_no_longer_hand_writes_the_namespace_membership() -> None:
    """RATCHET. Four sites spelled this out by hand; a fifth would re-open the finding.

    The needle is built by concatenation so this file does not match itself.
    """
    needle = ".namespace IN " + "[" + "a.namespace"

    assert needle not in _REPO_SOURCE, (
        "a namespace predicate was hand-written into the repository again; "
        "emit it from domain.work_artifact.namespace_compatibility_cypher instead"
    )


# ---------------------------------------------------------------------------
# The OpenQuestion state machine
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("target", [QuestionStatus.ANSWERED, QuestionStatus.DEFERRED])
def test_only_an_open_question_may_leave_the_open_state(target: str) -> None:
    assert question_can_transition(QuestionStatus.OPEN, target)


@pytest.mark.unit
@pytest.mark.parametrize("terminal", [QuestionStatus.ANSWERED, QuestionStatus.DEFERRED])
@pytest.mark.parametrize("target", [QuestionStatus.ANSWERED, QuestionStatus.DEFERRED])
def test_both_non_open_states_are_terminal(terminal: str, target: str) -> None:
    """Re-answering would overwrite which artifact actually resolved the question, and that
    record is the entire reason answering requires an answering artifact."""
    assert not question_can_transition(terminal, target)


@pytest.mark.unit
def test_an_unknown_target_status_admits_nothing() -> None:
    """FAIL CLOSED. A future status must refuse until the domain grants it, rather than
    defaulting into the answerable set."""
    assert question_statuses_allowing("not-a-status") == frozenset()


@pytest.mark.unit
def test_answer_question_binds_the_domains_admissible_set() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])

    WorkArtifactRepository(neo4j).answer_question("q1", "a2")

    assert neo4j.calls[0]["params"]["answerable"] == [QuestionStatus.OPEN]


@pytest.mark.unit
def test_defer_question_binds_the_domains_admissible_set() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])

    WorkArtifactRepository(neo4j).defer_question("q1")

    assert neo4j.calls[0]["params"]["deferrable"] == [QuestionStatus.OPEN]


@pytest.mark.unit
def test_widening_the_domain_rule_reaches_both_call_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE FINDING, stated as the property it is about.

    Before this change, admitting a new status meant editing two WHERE clauses in infrastructure
    and nothing in the module that defines the statuses. This proves the direction is now
    reversed: one domain edit, both statements follow, no infrastructure edit at all.
    """
    monkeypatch.setitem(
        domain._QUESTION_FORWARD,
        "reopened",
        frozenset({QuestionStatus.ANSWERED, QuestionStatus.DEFERRED}),
    )

    answering = _StubNeo4j(responses=[[{"applied": 1}]])
    deferring = _StubNeo4j(responses=[[{"applied": 1}]])
    WorkArtifactRepository(answering).answer_question("q1", "a2")
    WorkArtifactRepository(deferring).defer_question("q1")

    assert answering.calls[0]["params"]["answerable"] == ["open", "reopened"]
    assert deferring.calls[0]["params"]["deferrable"] == ["open", "reopened"]


@pytest.mark.unit
def test_the_question_guard_is_no_longer_a_literal_status_comparison() -> None:
    """RATCHET. The guard must ask the domain, not restate the rule in Cypher."""
    needle = "q.status" + " = " + "$open"

    assert needle not in _REPO_SOURCE
