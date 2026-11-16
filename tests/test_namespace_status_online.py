"""#671 against a real Neo4j: which namespaces hold records, and which stored spellings a typo means.

Run with ``pytest --run-online``; conftest forces the test instance.
"""

from __future__ import annotations

import uuid

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository

pytestmark = [pytest.mark.online]


@pytest.fixture
def stamped(test_neo4j_repo):
    """One Episodic under namespace `ns`, one Todo under `ns_todo`, one Episodic by group_id only."""
    tag = uuid.uuid4().hex[:8]
    names = {"ns": f"pfcc{tag}", "todo": f"todos{tag}", "group": f"grp{tag}"}
    test_neo4j_repo.execute(
        "CREATE (:Episodic {uuid: $a, namespace: $ns, group_id: $ns}), "
        "(:Todo {uuid: $b, namespace: $todo}), "
        "(:Episodic {uuid: $c, group_id: $group})",
        {"a": f"a-{tag}", "b": f"b-{tag}", "c": f"c-{tag}", **names},
    )
    yield names
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.uuid IN $ids DETACH DELETE n",
        {"ids": [f"a-{tag}", f"b-{tag}", f"c-{tag}"]},
    )


def test_exact_namespaces_are_known_from_every_label(test_neo4j_repo, stamped) -> None:
    queries = MemoryQueryRepository(test_neo4j_repo)
    for name in stamped.values():
        assert queries.namespace_status(name, include_similar=True)["known"] is True, name


@pytest.mark.parametrize("namespace", ["", "default"])
def test_the_default_namespace_is_always_known(test_neo4j_repo, namespace) -> None:
    status = MemoryQueryRepository(test_neo4j_repo).namespace_status(namespace, include_similar=True)
    assert status["known"] is True


def test_case_and_whitespace_variants_are_unknown_and_point_at_the_stored_spelling(
    test_neo4j_repo, stamped
) -> None:
    queries = MemoryQueryRepository(test_neo4j_repo)
    stored = stamped["ns"]
    for variant in (stored.upper(), stored + " "):
        status = queries.namespace_status(variant, include_similar=True)
        assert status["known"] is False, variant
        assert status["similar"] == [stored], variant


def test_a_pinned_lookup_lists_no_other_namespaces(test_neo4j_repo, stamped) -> None:
    status = MemoryQueryRepository(test_neo4j_repo).namespace_status(
        stamped["ns"].upper(), include_similar=False
    )
    assert status == {"namespace": stamped["ns"].upper(), "known": False, "similar": []}
