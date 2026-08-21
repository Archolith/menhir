"""CF-176: two live paths that scanned the whole `:Entity` label.

(a) `resolve_structural_entities` matches `structure_path` as a DISJUNCTION -- `= candidate` OR
    `ENDS WITH '/' + candidate` -- filtered by `structure_role`.
(b) `list_verifiers` is `MATCH (v:Entity {is_verifier: true})` with no LIMIT.

Filed STRUCTURAL from a grep census, with no Neo4j available. Measured 2026-08-20 against Neo4j
5.26.21 on the test instance: 22,003 `:Entity` of which 2,000 structural and 3 verifiers.

    (a) BEFORE this fix, AFTER CF-203     4,006 dbHits
          Filter                          2,000
            NodeIndexSeek structure_role  2,004   <- pulls EVERY structural node, then filters
        AFTER                                 5 dbHits
          Union[ NodeIndexEndsWithScan TEXT(structure_path)
               , NodeIndexSeek        RANGE(structure_path) ]

    (b) BEFORE                           44,013 dbHits
          Filter                         22,003
            NodeByLabelScan v:Entity     22,004   <- the whole label, for 3 rows
        AFTER                                 7 dbHits
          NodeIndexSeek RANGE(is_verifier)

Two things worth recording, because neither is visible from the entry:

* **(a) was already HALF fixed by CF-203.** That entry added `entity_structure_role_idx`, which
  turned a `:Entity` label scan into a `structure_role` seek. This entry's stated cost --
  "O(|candidate_paths| x |:Entity|)" -- was therefore already stale; the real remaining cost was
  O(|candidate_paths| x |structural entities|). Re-measuring before fixing is what caught that.

* **The residue is the CF-112 shape again.** A disjunction needs EVERY branch indexed. `ENDS WITH`
  cannot use a RANGE index, so one branch stayed unindexable and the planner declined to seek on
  `structure_path` at all. A TEXT index on the same property closes it, and both index kinds
  coexist -- the planner picks per branch.

* **`awaitIndexes` is not enough to make a plan assertion deterministic.** It waits for ONLINE, not
  for planner statistics. Immediately after a bulk seed the planner still chose the old
  role-seek-then-filter plan WITH BOTH INDEXES ONLINE -- the identical query measured 4,005 dbHits
  right after seeding and 5 dbHits moments later, no schema change in between. `_seed` therefore
  ends with `db.prepareForReplanning()`. Without it this file fails intermittently and looks like
  the fix regressed. The 4,006-vs-5 figures above were both taken post-statistics, on the 22,003
  graph; the "AFTER CF-203" number is the genuine steady-state cost of the equality-only index.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.schema import get_phase1_bootstrap_queries

pytestmark_unit = pytest.mark.unit


# ---------------------------------------------------------------------------
# Offline: the declarations. Runs in the default lane.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_verifier_is_indexed() -> None:
    queries = get_phase1_bootstrap_queries()
    assert any("entity_is_verifier_idx" in q and "n.is_verifier" in q for q in queries)


@pytest.mark.unit
def test_structure_path_carries_both_a_range_and_a_text_index() -> None:
    """The invariant, not the instance: `structure_path` is queried by BOTH `=` and `ENDS WITH`,
    and each needs its own index kind. Dropping either reintroduces the scan for that branch."""
    queries = get_phase1_bootstrap_queries()
    path_indexes = [q for q in queries if "n.structure_path)" in q and ":Entity" in q]

    assert any(q.strip().startswith("CREATE TEXT INDEX") for q in path_indexes), (
        f"no TEXT index backs the ENDS WITH branch: {path_indexes}"
    )
    assert any("TEXT" not in q for q in path_indexes), (
        f"no RANGE index backs the equality branch: {path_indexes}"
    )


# ---------------------------------------------------------------------------
# Online: the plans. These are the assertions that would have caught the finding.
# ---------------------------------------------------------------------------

_STRUCTURAL_QUERY = """
UNWIND $paths AS candidate
MATCH (n:Entity)
WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
  AND (n.structure_path = candidate
       OR (NOT candidate CONTAINS '/'
           AND (n.structure_path ENDS WITH '/' + candidate
                OR n.structure_path = candidate)))
RETURN DISTINCT n.uuid AS uuid
"""

_VERIFIER_QUERY = "MATCH (v:Entity {is_verifier: true}) RETURN v.uuid AS uuid"


def _plan_operators(profile: Any) -> list[str]:
    """Operator names with the `@database` suffix stripped -- Neo4j reports `NodeByLabelScan@neo4j`,
    and comparing against the bare name without stripping makes `not in` pass vacuously."""
    ops = [profile["operatorType"].split("@", 1)[0]]
    for child in profile.get("children", []):
        ops.extend(_plan_operators(child))
    return ops


def _profile(repo: Any, query: str, params: dict[str, Any]) -> tuple[list[str], int]:
    driver = repo._get_driver()
    with driver.session(database=repo.database) as session:
        result = session.run("PROFILE " + query, **params)
        list(result)
        plan = result.consume().profile

    def total(node: Any) -> int:
        return node.get("dbHits", 0) + sum(total(c) for c in node.get("children", []))

    return _plan_operators(plan), total(plan)


def _seed(repo: Any) -> None:
    repo.execute("MATCH (n) DETACH DELETE n")
    repo.execute("UNWIND range(1,4000) AS i CREATE (:Entity {uuid:'e'+toString(i)})")
    repo.execute(
        "UNWIND range(1,500) AS i "
        "CREATE (:Entity {uuid:'s'+toString(i), structure_role:'file', structure_project:'menhir',"
        " structure_path:'src/menhir/mod'+toString(i)+'.py'})"
    )
    repo.execute("UNWIND range(1,3) AS i CREATE (:Entity {uuid:'v'+toString(i), is_verifier:true})")
    for statement in get_phase1_bootstrap_queries():
        if "structure_path" in statement or "structure_role" in statement or "is_verifier" in statement:
            repo.execute(statement)
    repo.execute("CALL db.awaitIndexes(60)")
    # `awaitIndexes` waits for the index to come ONLINE. It does NOT wait for the planner's
    # statistics to catch up with a bulk load, and the planner picks between the seek-union and a
    # role-seek-then-filter using those statistics. Immediately after seeding it will choose the
    # scan-ish plan even with both indexes online -- measured, not assumed: the same query returned
    # 4,005 dbHits right after seeding and 5 dbHits moments later with no schema change.
    # `prepareForReplanning` refreshes statistics and blocks until they are current, which is what
    # makes a plan-shape assertion deterministic here.
    repo.execute("CALL db.prepareForReplanning()")


@pytest.mark.online
@pytest.mark.timing
def test_list_verifiers_does_not_scan_the_entity_label(test_neo4j_repo) -> None:
    """(b). The finding: a handful of verifiers cost a full scan of the highest-cardinality label."""
    _seed(test_neo4j_repo)

    operators, db_hits = _profile(test_neo4j_repo, _VERIFIER_QUERY, {})

    assert "NodeByLabelScan" not in operators, operators
    assert "NodeIndexSeek" in operators, operators
    # The scan cost ~2x the node count; a seek is bounded by the 3 matching rows.
    assert db_hits < 100, f"{db_hits} dbHits suggests it is still scanning: {operators}"


@pytest.mark.online
@pytest.mark.timing
@pytest.mark.parametrize(
    ("label", "candidate"),
    [("equality branch", "src/menhir/mod250.py"), ("ENDS WITH branch", "mod250.py")],
)
def test_structural_resolution_seeks_on_both_disjunction_branches(
    test_neo4j_repo, label: str, candidate: str
) -> None:
    """(a). Both spellings must resolve through an index. The bare-name case is the one that needs
    the TEXT index; without it the planner declines to seek on structure_path at all and falls back
    to pulling every structural node."""
    _seed(test_neo4j_repo)

    operators, db_hits = _profile(test_neo4j_repo, _STRUCTURAL_QUERY, {"paths": [candidate]})

    assert "NodeByLabelScan" not in operators, f"{label}: {operators}"
    assert "NodeIndexEndsWithScan" in operators, (
        f"{label}: the TEXT index is not being used, so the ENDS WITH branch is unindexed: {operators}"
    )
    assert db_hits < 100, f"{label}: {db_hits} dbHits, plan={operators}"


@pytest.mark.online
@pytest.mark.timing
def test_both_queries_still_return_their_rows(test_neo4j_repo) -> None:
    """POSITIVE CONTROL. Indexing must not change WHAT either query finds. Without this, a query
    that matched nothing would satisfy every dbHits assertion above."""
    _seed(test_neo4j_repo)

    verifiers = test_neo4j_repo.execute(_VERIFIER_QUERY)
    assert sorted(r["uuid"] for r in verifiers) == ["v1", "v2", "v3"]

    by_full = test_neo4j_repo.execute(_STRUCTURAL_QUERY, params={"paths": ["src/menhir/mod250.py"]})
    by_bare = test_neo4j_repo.execute(_STRUCTURAL_QUERY, params={"paths": ["mod250.py"]})
    assert [r["uuid"] for r in by_full] == ["s250"]
    assert [r["uuid"] for r in by_bare] == ["s250"]

    assert test_neo4j_repo.execute(_STRUCTURAL_QUERY, params={"paths": ["nope.py"]}) == []
