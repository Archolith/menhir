"""#79 hotfix: `count_transient_requeue` must be VALID Cypher, proven against a live Neo4j.

The v0.2.1 refund query used ``greatest()``, which Neo4j Cypher does not have. Every call
raised ``Unknown function 'greatest'``, so attempts were never refunded and the transient
counter never moved — pre-fix behaviour, shipped as fixed. The unit suite could not see it:
the #79 tests run against conftest fakes that never compile Cypher (the same instrument trap
issue #69 documents).

This test is the missing instrument. It executes the real query against a real database and
asserts the refund semantics: attempts 1 -> 0, transient_retries 0 -> 1, clamped at 0.

Run with:  pytest --run-online -m online tests/test_transient_refund_online.py
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph(test_neo4j_repo):
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


def _seed_episode(repo, episode_uuid: str, *, attempts: int) -> None:
    repo.execute(
        """
        CREATE (n:Episodic {
            uuid: $uuid,
            name: $uuid,
            processing_state: 'PENDING',
            processing_attempts: $attempts
        })
        """,
        params={"uuid": episode_uuid, "attempts": attempts},
    )


def _row(repo, episode_uuid: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid: $uuid}) "
        "RETURN n.processing_attempts AS attempts, "
        "coalesce(toInteger(n.transient_retries), 0) AS transient",
        params={"uuid": episode_uuid},
    )
    assert rows, "seeded episode vanished"
    return rows[0]


@pytest.mark.online
def test_count_transient_requeue_refunds_against_live_neo4j(graph, test_neo4j_repo):
    _seed_episode(test_neo4j_repo, "ep-refund-online", attempts=1)

    assert graph.count_transient_requeue("ep-refund-online") is True
    row = _row(test_neo4j_repo, "ep-refund-online")
    assert int(row["attempts"]) == 0, "the claim's attempt was not refunded"
    assert int(row["transient"]) == 1, "the transient counter did not move"

    # A refund at zero clamps at zero — it must never drive attempts negative.
    assert graph.count_transient_requeue("ep-refund-online") is True
    row = _row(test_neo4j_repo, "ep-refund-online")
    assert int(row["attempts"]) == 0
    assert int(row["transient"]) == 2


@pytest.mark.online
def test_count_transient_requeue_counts_null_attempts_as_one_before_refund(
    graph, test_neo4j_repo
):
    # coalesce(attempts, 0) > 0 is False for a NULL field, so a NULL row clamps to 0
    # rather than treating the missing value as an already-refunded state invisibly.
    test_neo4j_repo.execute(
        "CREATE (n:Episodic {uuid: 'ep-refund-null', processing_state: 'PENDING'})"
    )

    assert graph.count_transient_requeue("ep-refund-null") is True
    row = _row(test_neo4j_repo, "ep-refund-null")
    assert int(row["attempts"]) == 0
    assert int(row["transient"]) == 1


@pytest.mark.online
def test_count_transient_requeue_missing_episode_returns_false(graph):
    assert graph.count_transient_requeue("no-such-episode-online") is False
