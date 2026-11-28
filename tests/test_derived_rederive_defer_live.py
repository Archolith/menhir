"""#874: a deferred rederive repair is listed behind the rest, and deferring spends no attempt."""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository

pytestmark = [pytest.mark.online]


@pytest.fixture
def repairs(test_neo4j_repo):
    tag = f"rr-defer-{uuidlib.uuid4().hex[:10]}"
    keys = [f"{tag}-{i}" for i in range(3)]
    test_neo4j_repo.execute(
        """
        UNWIND range(0, size($keys) - 1) AS i
        CREATE (:DerivedRederiveRepair {repair_key: $keys[i], item_uuid: 'u' + toString(i),
                item_kind: 'fact', namespace: 'default', status: 'pending', attempts: 0,
                test_tag: $tag, created_at: datetime() - duration({minutes: 10 - i})})
        """,
        params={"keys": keys, "tag": tag},
    )
    yield keys
    test_neo4j_repo.execute(
        "MATCH (rr:DerivedRederiveRepair {test_tag: $tag}) DETACH DELETE rr", params={"tag": tag})


def _order(queries: MemoryQueryRepository, keys: list[str]) -> list[str]:
    return [r["repair_key"] for r in queries.list_pending_derived_rederive(1000)
            if r["repair_key"] in keys]


def test_deferred_repair_goes_behind_the_rest_without_an_attempt(test_neo4j_repo, repairs):
    queries = MemoryQueryRepository(test_neo4j_repo)
    assert _order(queries, repairs) == repairs
    assert queries.defer_derived_rederive(repairs[0]) is True
    assert _order(queries, repairs) == [repairs[1], repairs[2], repairs[0]]
    row = [r for r in queries.list_pending_derived_rederive(1000) if r["repair_key"] == repairs[0]]
    assert row[0]["attempts"] == 0


def test_defer_ignores_a_repair_that_is_not_pending(test_neo4j_repo, repairs):
    queries = MemoryQueryRepository(test_neo4j_repo)
    queries.mark_derived_rederive_status(repairs[0], "done")
    assert queries.defer_derived_rederive(repairs[0]) is False
