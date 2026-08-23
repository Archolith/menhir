"""CF-158 -- why Menhir does NOT declare a uniqueness constraint on `:Entity.uuid`.

This file exists to stop the constraint being re-attempted. It was attempted on 2026-08-22 and
reverted, and the reason is a schema-ownership boundary rather than a data problem.

**The data was checked first and was clean.** Read-only against production, under an operator
go-ahead (ledger section B): **0 duplicate uuids across 50,760 `:Entity`, 0 NULL uuids**, and the
same for `:Episodic`. A UNIQUE constraint refuses to come online over existing duplicates -- which
is exactly what this finding predicts -- so that census was the right pre-flight for the risk
everyone expected.

**It was the wrong pre-flight for the risk that actually existed.** Neo4j refuses the constraint
outright:

    Neo.ClientError.Schema.IndexAlreadyExists
    There already exists an index (:Entity {uuid}).
    A constraint cannot be created until the index has been dropped.

That index is **graphiti's**, not Menhir's -- `graphiti_core/graph_queries.py:55` issues
`CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)`, and
`build_indices_and_constraints` re-issues it on every startup, a call Menhir itself makes. Dropping
it to make room would put the two in a loop, each restoring its own shape on the next boot.

Counting rows could never have revealed this. **Executing the DDL against a disposable instance
did**, on the first run of the online test below.

OWNER RULING 2026-08-22: reject schema enforcement, move to write-path idempotency. `MERGE` on the
stable uuid is the right layer -- it needs no schema authority at all -- but a mechanical
`CREATE -> MERGE` swap is NOT assumed safe until the surrounding `ON CREATE` / property-mutation
behaviour is traced. That tracing is separate work and is not done here.

**The counter half is explicitly not covered by any of this.** A retried
`SET n.hot_count = coalesce(n.hot_count, 0) + 1` creates no duplicate node, so no constraint and no
`MERGE` addresses it; see `test_a_counter_increment_applies_twice_on_retry`.
"""

from __future__ import annotations

import pytest

pytestmark_unit = pytest.mark.unit

_CONSTRAINT = (
    "CREATE CONSTRAINT entity_uuid_unique IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.uuid IS UNIQUE"
)
_GRAPHITI_INDEX = "CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)"


@pytest.mark.unit
def test_menhir_does_not_declare_the_constraint() -> None:
    """The rejection, pinned. Re-adding this line reintroduces a bootstrap error on any graph
    where graphiti has already created its index -- which is every real one."""
    from menhir.infrastructure.schema import get_phase1_bootstrap_queries

    offenders = [
        q for q in get_phase1_bootstrap_queries()
        if "entity_uuid_unique" in q or ("REQUIRE n.uuid IS UNIQUE" in q and ":Entity)" in q)
    ]
    assert offenders == [], (
        "Menhir cannot hold a uniqueness constraint on :Entity.uuid while graphiti owns a plain "
        f"index on the same property; see this module's docstring. Found: {offenders}"
    )


@pytest.mark.unit
def test_graphiti_still_owns_the_conflicting_index() -> None:
    """The rejection's premise, asserted against the vendored library rather than remembered. If a
    graphiti upgrade ever stops creating this index, the constraint becomes possible and this test
    is what tells you."""
    from graphiti_core import graph_queries

    import inspect

    source = inspect.getsource(graph_queries)
    assert "CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)" in source, (
        "graphiti no longer declares entity_uuid -- re-evaluate CF-158's rejected constraint"
    )


@pytest.mark.online
def test_the_constraint_is_refused_while_the_graphiti_index_exists(test_neo4j_repo) -> None:
    """THE EVIDENCE. Executed, because this is the check that counting rows could not make."""
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute("DROP CONSTRAINT entity_uuid_unique IF EXISTS")
    test_neo4j_repo.execute(_GRAPHITI_INDEX)
    test_neo4j_repo.execute("CALL db.awaitIndexes(60)")

    with pytest.raises(Exception) as excinfo:
        test_neo4j_repo.execute(_CONSTRAINT)

    assert "IndexAlreadyExists" in str(excinfo.value) or "already exists an index" in str(
        excinfo.value
    ), f"expected the index collision, got: {excinfo.value}"


@pytest.mark.online
def test_a_counter_increment_applies_twice_on_retry(test_neo4j_repo) -> None:
    """THE HALF NO SCHEMA CHANGE REACHES, kept so the rejection above is not read as closing it. A
    retried increment produces no duplicate node -- it writes a wrong number onto one node, and an
    overcounted row is indistinguishable from a busy one, which is why historical repair is not
    possible from current state."""
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute("CREATE (n:Entity {uuid: 'c-1', hot_count: 0})")

    for _ in range(2):  # one logical increment, retried after an ambiguous commit
        test_neo4j_repo.execute(
            "MATCH (n:Entity) WHERE n.uuid = 'c-1' "
            "SET n.hot_count = coalesce(n.hot_count, 0) + 1"
        )

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n.uuid = 'c-1' RETURN n.hot_count AS c"
    )
    assert rows[0]["c"] == 2, "one logical increment, counted twice -- CF-158's open half"
