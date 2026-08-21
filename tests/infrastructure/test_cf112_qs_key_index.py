"""CF-112: the View supersession lookup is a disjunction with only one branch indexed.

`ViewWriteRepositoryMixin._current_by_key` runs on EVERY View write:

    MATCH (n:{label}) WHERE (n.view_key = $k OR n.qs_key = $k)
      AND coalesce(n.view_current, n.qs_current, true) ...

`view_key` was indexed on both :Entity and :Metric. `qs_key` -- the backward-compatibility branch
that lets pre-View counter nodes supersede without a migration -- had no index anywhere. A
disjunction needs EVERY branch indexed or the planner cannot resolve it as a seek union.

The entry was filed **NOT MEASURED** for want of a live Neo4j. Measured 2026-08-20 against Neo4j
5.26.21 on the throwaway test instance, 20,500 :Entity nodes:

    BEFORE (qs_key unindexed)          61,005 dbHits
      Union
        NodeIndexSeek  n:Entity(view_key)        2 dbHits
        Filter                               40,500 dbHits
          NodeByLabelScan n:Entity           20,501 dbHits   <- the whole label, every write

    AFTER (qs_key indexed)                  5 dbHits
      Union
        NodeIndexSeek  n:Entity(view_key)        2 dbHits
        NodeIndexSeek  n:Entity(qs_key)          1 dbHits

Two details worth keeping, because they are not obvious from reading the query:

* `LIMIT 1` buys nothing. The scan branch is fully evaluated regardless of where the match sits --
  61,005 dbHits whether the key is the first row or the 19,999th. There is no early exit.
* The cost is linear in TOTAL :Entity count, not in the number of View nodes, so it grows with the
  whole graph on a hot write path.

The online test below profiles the query the repository ACTUALLY emits -- captured from a real
`ViewWriteRepositoryMixin` through a stub driver -- rather than a copy that could drift from it.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.schema import PHASE_ONE_REQUIRED_INDEXES, get_phase1_bootstrap_queries
from menhir.infrastructure.view_models import ViewClass
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin


class _CapturingNeo4j:
    """Captures the query `_current_by_key` emits, so the profile below is of the real thing."""

    def __init__(self) -> None:
        self.query: str | None = None
        self.params: dict[str, Any] | None = None

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        self.query = query
        self.params = params or {}
        return []


def _emitted_query(view_class: ViewClass) -> tuple[str, dict[str, Any]]:
    neo = _CapturingNeo4j()
    repo = ViewWriteRepositoryMixin(neo4j=neo)
    repo._current_by_key("cf112-probe", view_class=view_class)
    assert neo.query is not None
    return neo.query, dict(neo.params or {})


# ---------------------------------------------------------------------------
# Offline: the schema declares the indexes. Runs in the default lane.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ["entity_qs_key_idx", "metric_qs_key_idx"])
def test_the_qs_key_indexes_are_declared_and_required(name: str) -> None:
    """Required, not merely created: an existing install must report schema_not_ready until the
    index that makes the write path cheap actually exists. That is the same choice the repo already
    made for `entity_view_subject_uuid_idx`."""
    assert name in PHASE_ONE_REQUIRED_INDEXES

    queries = get_phase1_bootstrap_queries()
    assert any(name in q and "qs_key" in q for q in queries), f"{name} is required but never created"


@pytest.mark.unit
def test_both_branches_of_the_disjunction_are_indexed_for_both_labels() -> None:
    """The invariant, not the instance. A disjunction needs EVERY branch indexed; asserting on the
    pair keeps a future third branch from silently reintroducing the scan."""
    queries = " ".join(get_phase1_bootstrap_queries())
    for label, prop in (
        ("Entity", "view_key"), ("Entity", "qs_key"),
        ("Metric", "view_key"), ("Metric", "qs_key"),
    ):
        needle = f"FOR (n:{label}) ON (n.{prop})"
        assert needle in queries, f"no index backing {label}.{prop}"


@pytest.mark.unit
@pytest.mark.parametrize("view_class", list(ViewClass))
def test_the_supersession_query_still_queries_both_keys(view_class: ViewClass) -> None:
    """POSITIVE CONTROL on the capture harness the online test depends on: if `_current_by_key`
    stops emitting the disjunction, the profile assertions below would pass vacuously."""
    query, params = _emitted_query(view_class)

    assert "n.view_key = $k" in query
    assert "n.qs_key = $k" in query
    assert params == {"k": "cf112-probe"}


# ---------------------------------------------------------------------------
# Online: the plan itself. This is the assertion that would have caught the finding.
# ---------------------------------------------------------------------------


def _plan_operators(profile: Any) -> list[str]:
    """Operator names, with the `@database` suffix stripped.

    Neo4j reports `NodeIndexSeek@neo4j`, not `NodeIndexSeek`. Comparing against the bare name
    without stripping makes a `not in` assertion pass vacuously -- which is exactly what happened
    when this test was first written, and is why the NodeIndexSeek count below is not optional.
    """
    ops = [profile["operatorType"].split("@", 1)[0]]
    for child in profile.get("children", []):
        ops.extend(_plan_operators(child))
    return ops


def _profile(session: Any, query: str, params: dict[str, Any]) -> Any:
    result = session.run("PROFILE " + query, **params)
    list(result)
    return result.consume().profile


@pytest.mark.online
@pytest.mark.timing
def test_the_supersession_lookup_does_not_scan_the_label(test_neo4j_repo) -> None:
    """The finding, as a plan-shape assertion.

    Plan shape rather than a dbHits threshold: the threshold depends on how many nodes the fixture
    happens to create, but 'a NodeByLabelScan appears on the write path' is true or false
    regardless, and it is the actual defect.
    """
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "UNWIND range(1, 2000) AS i "
        "CREATE (n:Entity {uuid:'u'+toString(i), view_key:'vk-'+toString(i), view_current:true})"
    )
    for statement in get_phase1_bootstrap_queries():
        if "qs_key" in statement or "view_key" in statement:
            test_neo4j_repo.execute(statement)
    test_neo4j_repo.execute("CALL db.awaitIndexes(60)")

    query, params = _emitted_query(ViewClass.FACT)

    driver = test_neo4j_repo._get_driver()
    with driver.session(database=test_neo4j_repo.database) as session:
        operators = _plan_operators(_profile(session, query, params))

    assert "NodeByLabelScan" not in operators, (
        f"the View write path still scans the whole label: {operators}"
    )
    # POSITIVE CONTROL: it resolved through indexes rather than by matching nothing at all.
    assert operators.count("NodeIndexSeek") == 2, operators


@pytest.mark.online
@pytest.mark.timing
def test_both_branches_still_return_their_own_rows(test_neo4j_repo) -> None:
    """POSITIVE CONTROL: indexing must not change WHAT the disjunction finds. The qs_key branch is
    the backward-compatibility path for pre-View counter nodes -- if it stopped matching, old
    counters would silently stop superseding and this would be a correctness regression, not a
    speedup."""
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:'by-view-key', view_key:'k-view', view_current:true}) "
        "CREATE (:Entity {uuid:'by-qs-key', qs_key:'k-qs', qs_current:true})"
    )
    for statement in get_phase1_bootstrap_queries():
        if "qs_key" in statement or "view_key" in statement:
            test_neo4j_repo.execute(statement)
    test_neo4j_repo.execute("CALL db.awaitIndexes(60)")

    repo = ViewWriteRepositoryMixin(neo4j=test_neo4j_repo)

    assert (repo._current_by_key("k-view") or {}).get("uuid") == "by-view-key"
    assert (repo._current_by_key("k-qs") or {}).get("uuid") == "by-qs-key"
    assert repo._current_by_key("k-absent") is None
