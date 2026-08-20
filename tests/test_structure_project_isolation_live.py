"""CF-224: no query scoped to one structure project may return another project's nodes.

**Two projects, identical file paths, cross-project edges in every direction.** Identical paths
are deliberate: a leak that returns `src/app.py` from the wrong project is invisible in any test
where the two projects have different filenames, because the value LOOKS like the caller's own
file. Distinct uuids are what make the leak observable, which is why the fixture carries them.

**The measured scope was wider than the finding recorded.** CF-224 names `query_context`'s
`imported_by` and `tested_by` branches. Driven against a real graph before writing any fix, all
FOUR of its branches leak -- symbols and outgoing imports too -- and so do
`resolve_structural_neighbors` and its bulk sibling, which return raw UUIDs that flow into
`recall_support`. Enumerated by reading every `MATCH` in `structure_queries.py` that lacks a
`structure_project` predicate, rather than by trusting the entry.

**Why this is a fix and not just a test, when CF-73 deliberately left it alone.** That decision
was correct for a PERFORMANCE rewrite: changing what a query returns under cover of a latency
change is how a behavioural regression ships unnoticed. It does not survive re-examination as an
isolation defect on its own terms. The behaviour change is the point now, and it is the whole
content of the commit rather than a side effect of one.

**The nesting question this raises, and why the answer is "still refuse".** `CONTAINS_REPO` means
projects genuinely nest, so a parent-project importer can be a legitimate answer -- that is what
made this non-obvious enough to file rather than fix in passing. But an unqualified `MATCH` does
not implement nesting; it returns EVERY project, related or not. If nested visibility is wanted
it needs to be a traversal of `CONTAINS_REPO`, deliberately, not the absence of a predicate.
`test_a_contained_repo_is_still_not_silently_merged` pins that distinction.

Run with:  pytest --run-online -m online tests/test_structure_project_isolation_live.py
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = [pytest.mark.online]

#: Appears only on project B. Any occurrence in a project-A result is a leak.
B_SENTINEL = "B_SECRET_SYMBOL"


@pytest.fixture
def two_projects(test_neo4j_repo):
    """Two projects sharing file paths, wired across the boundary in every direction."""
    test_neo4j_repo.execute(
        """
        CREATE (pa:Entity {structure_project:'proj-A', structure_role:'project',
                           structure_path:'', content:'A', stack:'python'})
        CREATE (pb:Entity {structure_project:'proj-B', structure_role:'project',
                           structure_path:'', content:'B', stack:'python'})

        CREATE (a:Entity  {structure_project:'proj-A', structure_role:'file',
                           structure_path:'src/app.py', uuid:'A-app', content:'A app'})
        CREATE (au:Entity {structure_project:'proj-A', structure_role:'file',
                           structure_path:'src/util.py', uuid:'A-util'})
        CREATE (asym:Entity {structure_project:'proj-A', structure_role:'symbol',
                             name:'a_own_symbol', symbol_line:5, uuid:'A-sym'})

        // Same path as A's file, different project. A leak here looks like A's own file.
        CREATE (b:Entity  {structure_project:'proj-B', structure_role:'file',
                           structure_path:'src/app.py', uuid:'B-app', content:'B SECRET'})
        CREATE (bi:Entity {structure_project:'proj-B', structure_role:'file',
                           structure_path:'src/importer.py', uuid:'B-importer'})
        CREATE (bt:Entity {structure_project:'proj-B', structure_role:'file',
                           structure_path:'tests/test_b.py', uuid:'B-tester'})
        CREATE (bs:Entity {structure_project:'proj-B', structure_role:'symbol',
                           name:$sentinel, symbol_line:1, uuid:'B-sym'})

        CREATE (a)-[:IMPORTS]->(au)
        CREATE (a)-[:DEFINES]->(asym)

        // Every cross-project direction, one per leaking branch.
        CREATE (a)-[:IMPORTS]->(bi)
        CREATE (bi)-[:IMPORTS]->(a)
        CREATE (bt)-[:TESTS]->(a)
        CREATE (a)-[:DEFINES]->(bs)
        """,
        params={"sentinel": B_SENTINEL},
    )
    return StructureGraphWriter(test_neo4j_repo)


# ---------------------------------------------------------------------------
# query_context -- all four branches
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_context_returns_no_foreign_project_in_any_branch(two_projects) -> None:
    """The whole finding in one assertion. Each branch is checked separately so a failure names
    which one leaked rather than reporting a blob."""
    ctx = two_projects.query_context("proj-A", "src/app.py")

    assert [s["name"] for s in ctx["symbols"]] == ["a_own_symbol"], (
        f"a foreign project's symbol reached the caller: {ctx['symbols']}"
    )
    assert ctx["imports"] == ["src/util.py"], (
        f"outgoing imports crossed the project boundary: {ctx['imports']}"
    )
    assert ctx["imported_by"] == [], (
        f"importers from another project were returned: {ctx['imported_by']}"
    )
    assert ctx["tested_by"] == [], (
        f"testers from another project were returned: {ctx['tested_by']}"
    )


@pytest.mark.online
def test_the_B_sentinel_appears_nowhere_in_a_project_A_context(two_projects) -> None:
    """A blunt whole-result scan, the CF-165 discipline: assert on the far end rather than on a
    guard being called. It catches a leak into a branch this file does not enumerate."""
    import json

    blob = json.dumps(two_projects.query_context("proj-A", "src/app.py"), default=str)
    assert B_SENTINEL not in blob
    assert "B SECRET" not in blob
    assert "src/importer.py" not in blob
    assert "tests/test_b.py" not in blob


@pytest.mark.online
def test_the_same_path_in_the_other_project_gets_its_own_context(two_projects) -> None:
    """Isolation, not erasure. Both projects genuinely have `src/app.py`, and each must see its
    own -- a fix that returned nothing for B would pass every assertion above."""
    ctx_b = two_projects.query_context("proj-B", "src/app.py")

    assert ctx_b["summary"] == "B SECRET", "project B lost its own file content"

    # B's symbol and importer are both wired to A's file -- those ARE the cross-project edges
    # under test -- so B's own context correctly has neither. Two earlier versions of this test
    # asserted otherwise and were describing the FIXTURE rather than the code.
    assert ctx_b["symbols"] == []
    assert ctx_b["imported_by"] == []


# ---------------------------------------------------------------------------
# resolve_structural_neighbors -- the leak that reaches recall
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_structural_neighbors_returns_no_foreign_uuids(two_projects) -> None:
    """Sharper than the context leak, because these are raw UUIDs consumed by
    `recall_support`: a foreign uuid does not merely display, it selects a node."""
    uuids = two_projects.resolve_structural_neighbors("proj-A", "src/app.py")

    assert all(u.startswith("A-") for u in uuids), f"foreign uuids leaked into recall: {uuids}"
    assert "A-app" in uuids and "A-util" in uuids, "the caller's own neighbours went missing"


@pytest.mark.online
def test_bulk_structural_neighbors_is_scoped_too(two_projects) -> None:
    """The bulk sibling is a separate query with the same shape, so it needs its own assertion --
    fixing one and not the other is precisely how this cluster has repeatedly gone wrong."""
    # Signature is (projects, file_path) -> (matched_project, uuids) | None -- read from the
    # source rather than assumed; a first version had the arguments the other way round and got
    # None, which would have passed a laxer assertion while testing nothing.
    result = two_projects.resolve_structural_neighbors_bulk(["proj-A"], "src/app.py")

    assert result is not None, "the bulk lookup found nothing for a project that exists"
    matched_project, uuids = result
    assert matched_project == "proj-A"
    assert all(str(u).startswith("A-") for u in uuids), f"bulk leaked foreign uuids: {uuids}"
    assert "A-app" in uuids and "A-util" in uuids, "the caller's own neighbours went missing"


# ---------------------------------------------------------------------------
# Nesting: refusal is a decision, not an oversight
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_a_contained_repo_is_still_not_silently_merged(test_neo4j_repo) -> None:
    """`CONTAINS_REPO` means projects nest, which is what made this worth filing rather than
    fixing in passing: a parent-project importer CAN be a legitimate answer.

    The resolution is that an unqualified `MATCH` does not implement nesting -- it returns every
    project in the database, related or not. If nested visibility is wanted it must be an
    explicit `CONTAINS_REPO` traversal. This pins that a contained repo is still isolated, so
    the day someone implements nesting they change this test deliberately.
    """
    test_neo4j_repo.execute(
        """
        CREATE (parent:Entity {structure_project:'parent', structure_role:'project',
                               structure_path:''})
        CREATE (child:Entity  {structure_project:'child', structure_role:'project',
                               structure_path:''})
        CREATE (pf:Entity {structure_project:'parent', structure_role:'file',
                           structure_path:'main.py', uuid:'P-main'})
        CREATE (cf:Entity {structure_project:'child', structure_role:'file',
                           structure_path:'lib.py', uuid:'C-lib'})
        CREATE (parent)-[:CONTAINS_REPO {rel_path:'vendor/child'}]->(child)
        CREATE (cf)-[:IMPORTS]->(pf)
        """
    )
    writer = StructureGraphWriter(test_neo4j_repo)

    ctx = writer.query_context("parent", "main.py")
    assert ctx["imported_by"] == [], (
        "a contained repo's importer appeared in the parent's context without any explicit "
        "CONTAINS_REPO traversal -- that is leakage, not nesting"
    )
    # The containment relationship itself is still reported, through the query built for it.
    assert writer.query_contained_repos("parent") == [
        {"name": "child", "rel_path": "vendor/child"}
    ]
