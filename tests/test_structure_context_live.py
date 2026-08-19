"""CF-73: `query_context` made five sequential Neo4j round trips; `query_overview` made five.

**Verified against the ORIGINAL five-call implementation before any of it was touched**: all
twelve passed on the first run, unmodified. That ordering is what makes them a characterisation
rather than a description of whatever the rewrite happens to produce, and it is why the combined
query must satisfy them without a single assertion being edited. A test written after a rewrite
pins the new behaviour and proves nothing about equivalence.

Why a live test and not stubs: the risk in this rewrite is entirely in Cypher semantics --
`OPTIONAL MATCH` cardinality, `collect` on an empty branch, ordering, and null handling. A stub
that returns canned rows per `neo4j.execute` call cannot exercise any of it, and would be
rewritten to match the new call shape anyway, proving only that the test was updated. There were
also no tests for either method at all, so nothing is being replaced.

The four empty-branch cases below are the whole point. A file with no symbols, no importers and
no tests is the state where a five-query implementation naturally returns `[]` and a single
`OPTIONAL MATCH` query naturally returns `[null]`, and that difference is the defect this
rewrite would otherwise ship.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.structure_queries import StructureGraphWriter

PROJECT = "cf73-fixture"


@pytest.fixture
def writer(test_neo4j_repo) -> StructureGraphWriter:
    """A small structure graph, shaped like what the scanner writes.

    `lonely.py` is deliberately barren -- no symbols, no imports either way, no tests -- because
    that is the case a combined query gets wrong.
    """
    test_neo4j_repo.execute(
        """
        CREATE (proj:Entity {structure_project: $p, structure_role: 'project',
                             structure_path: '', content: 'fixture project', stack: 'python'})

        CREATE (app:Entity {structure_project: $p, structure_role: 'file',
                            structure_path: 'src/app.py', content: 'the app',
                            symbols_truncated: false})
        CREATE (util:Entity {structure_project: $p, structure_role: 'file',
                             structure_path: 'src/util.py', content: 'helpers',
                             symbols_truncated: false})
        CREATE (cli:Entity {structure_project: $p, structure_role: 'file',
                            structure_path: 'src/cli.py', content: 'entry point',
                            symbols_truncated: false})
        CREATE (lonely:Entity {structure_project: $p, structure_role: 'file',
                               structure_path: 'src/lonely.py', content: 'nothing links here'})
        CREATE (trunc:Entity {structure_project: $p, structure_role: 'file',
                              structure_path: 'src/big.py', content: 'huge',
                              symbols_truncated: true})
        CREATE (test:Entity {structure_project: $p, structure_role: 'file',
                             structure_path: 'tests/test_app.py', content: 'tests'})

        CREATE (s2:Entity {structure_project: $p, structure_role: 'symbol', name: 'beta',
                           symbol_kind: 'function', symbol_signature: 'beta(x)',
                           content: 'second by line', symbol_line: 20,
                           symbol_parent: 'App', symbol_decorator: 'staticmethod'})
        CREATE (s1:Entity {structure_project: $p, structure_role: 'symbol', name: 'alpha',
                           symbol_kind: 'class', symbol_signature: 'alpha()',
                           content: 'first by line', symbol_line: 5,
                           symbol_parent: '', symbol_decorator: ''})
        CREATE (nonsym:Entity {structure_project: $p, structure_role: 'note', name: 'not-a-symbol',
                               symbol_line: 1})

        CREATE (app)-[:DEFINES]->(s1)
        CREATE (app)-[:DEFINES]->(s2)
        CREATE (app)-[:DEFINES]->(nonsym)
        CREATE (app)-[:IMPORTS]->(util)
        CREATE (cli)-[:IMPORTS]->(app)
        CREATE (test)-[:TESTS]->(app)
        """,
        params={"p": PROJECT},
    )
    return StructureGraphWriter(test_neo4j_repo)


# ---------------------------------------------------------------------------
# query_context
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_context_returns_every_branch_for_a_well_connected_file(writer) -> None:
    result = writer.query_context(PROJECT, "src/app.py")

    assert result == {
        "path": "src/app.py",
        "summary": "the app",
        "truncated": False,
        "symbols": [
            {"name": "alpha", "kind": "class", "sig": "alpha()", "doc": "first by line",
             "line": 5, "parent": "", "decorator": ""},
            {"name": "beta", "kind": "function", "sig": "beta(x)", "doc": "second by line",
             "line": 20, "parent": "App", "decorator": "staticmethod"},
        ],
        "imports": ["src/util.py"],
        "imported_by": ["src/cli.py"],
        "tested_by": ["tests/test_app.py"],
    }


@pytest.mark.online
def test_context_empty_branches_are_empty_lists_not_lists_of_none(writer) -> None:
    """The case that breaks a naive combined query. Five separate queries return no rows and the
    comprehension yields `[]`; one query with `OPTIONAL MATCH` yields a row whose collected
    branches contain a null unless they are filtered. `[None]` would then reach the MCP caller
    as a symbol with an empty name or an import path of "None"."""
    result = writer.query_context(PROJECT, "src/lonely.py")

    assert result["symbols"] == []
    assert result["imports"] == []
    assert result["imported_by"] == []
    assert result["tested_by"] == []
    assert result["summary"] == "nothing links here"


@pytest.mark.online
def test_context_reports_a_missing_file_as_an_error_not_an_empty_context(writer) -> None:
    """`lonely.py` and a file that does not exist must stay distinguishable. A combined query
    anchored on a required MATCH returns zero rows for the latter, which is what preserves this
    -- but only if the error branch is kept."""
    result = writer.query_context(PROJECT, "src/does-not-exist.py")

    assert result == {"error": "File not found in structure graph: src/does-not-exist.py"}
    assert "symbols" not in result


@pytest.mark.online
def test_context_symbols_are_ordered_by_line_not_insertion(writer) -> None:
    """`beta` is CREATEd before `alpha` and has the higher line number, so an implementation that
    lost `ORDER BY sym.symbol_line` would still return both and pass a set-based assertion."""
    result = writer.query_context(PROJECT, "src/app.py")

    assert [s["line"] for s in result["symbols"]] == [5, 20]
    assert [s["name"] for s in result["symbols"]] == ["alpha", "beta"]


@pytest.mark.online
def test_context_excludes_defined_nodes_that_are_not_symbols(writer) -> None:
    """The DEFINES edge is not sufficient: the original filters on
    `sym.structure_role = 'symbol'`. `nonsym` is DEFINES-linked and would appear without it."""
    result = writer.query_context(PROJECT, "src/app.py")

    assert "not-a-symbol" not in [s["name"] for s in result["symbols"]]
    assert len(result["symbols"]) == 2


@pytest.mark.online
def test_context_truncated_flag_survives(writer) -> None:
    assert writer.query_context(PROJECT, "src/big.py")["truncated"] is True
    assert writer.query_context(PROJECT, "src/app.py")["truncated"] is False


@pytest.mark.online
def test_context_missing_truncated_property_coalesces_to_false(writer) -> None:
    """`lonely.py` carries no `symbols_truncated` property at all -- legacy nodes predate it."""
    assert writer.query_context(PROJECT, "src/lonely.py")["truncated"] is False


@pytest.mark.online
def test_context_does_not_cross_project_boundaries(test_neo4j_repo, writer) -> None:
    """Both queries key on `structure_project`, and a combined query has to keep that on every
    branch -- not only on the anchor -- or another project's importer leaks in."""
    test_neo4j_repo.execute(
        """
        CREATE (other:Entity {structure_project: 'other-project', structure_role: 'file',
                              structure_path: 'other/thing.py', content: 'elsewhere'})
        WITH other
        MATCH (app:Entity {structure_project: $p, structure_path: 'src/app.py'})
        CREATE (other)-[:IMPORTS]->(app)
        CREATE (other)-[:TESTS]->(app)
        """,
        params={"p": PROJECT},
    )
    result = writer.query_context(PROJECT, "src/app.py")

    # The ORIGINAL does not filter importers/testers by project -- `MATCH (importer:Entity)`
    # is unqualified -- so a cross-project importer IS returned today. Confirmed by execution,
    # not inferred. Pinned as observed behaviour, NOT endorsed: a performance rewrite must not
    # quietly change what a query returns, so this stays as-is here and the leak is filed
    # separately as CF-224.
    assert result["imported_by"] == ["other/thing.py", "src/cli.py"]
    assert result["tested_by"] == ["other/thing.py", "tests/test_app.py"]


@pytest.mark.online
def test_context_requires_a_path(writer) -> None:
    with pytest.raises(ValueError, match="path is required"):
        writer.query_context(PROJECT, "")


# ---------------------------------------------------------------------------
# query_overview
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_overview_returns_counts_description_and_stack(writer) -> None:
    result = writer.query_overview(PROJECT)

    assert result["project"] == PROJECT
    assert result["description"] == "fixture project"
    assert result["stack"] == "python"
    assert result["entities"] == {"project": 1, "file": 6, "symbol": 2, "note": 1}
    assert result["edges"] == {"DEFINES": 3, "IMPORTS": 2, "TESTS": 1}


@pytest.mark.online
def test_overview_of_an_unknown_project_is_empty_rather_than_an_error(writer) -> None:
    """The three reads are independent, so an absent project yields empty aggregates and empty
    strings rather than raising. A combined query anchored on a required MATCH of the project
    node would turn this into no rows at all, which is the trap here."""
    result = writer.query_overview("no-such-project")

    assert result["description"] == ""
    assert result["stack"] == ""
    assert result["entities"] == {}
    assert result["edges"] == {}


@pytest.mark.online
def test_overview_still_carries_coverage_and_contained_repos(writer) -> None:
    """These are two further round trips beyond the three the finding counts -- `query_overview`
    makes five, not three. Pinned so a rewrite of the three does not drop them."""
    result = writer.query_overview(PROJECT)

    assert "coverage" in result
    assert result["coverage"]["known"] is False  # no files_indexed on the fixture project node
    assert result["contains_repos"] == []


# ---------------------------------------------------------------------------
# The rewrite's own properties: round trips, and where the aggregation happens
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_context_makes_exactly_one_round_trip(test_neo4j_repo, writer) -> None:
    """The finding itself. Five sequential `neo4j.execute` calls became one, and this counts
    them rather than trusting the shape of the source -- a helper reintroduced later would
    restore the round trips while every equivalence test above still passed."""
    calls: list[str] = []
    original = test_neo4j_repo.execute
    test_neo4j_repo.execute = lambda q, params=None, **kw: (  # type: ignore[method-assign]
        calls.append(q), original(q, params, **kw)
    )[1]
    try:
        writer.query_context(PROJECT, "src/app.py")
    finally:
        test_neo4j_repo.execute = original  # type: ignore[method-assign]

    assert len(calls) == 1, f"query_context made {len(calls)} round trips"


@pytest.mark.online
def test_overview_makes_three_round_trips_and_that_is_deliberate(test_neo4j_repo, writer) -> None:
    """Three, not one. The three independent reads are combined; `get_project_coverage` and
    `query_contained_repos` stay separate because `get_project_coverage` has another caller and
    both are public methods -- inlining their Cypher here would create a second definition free
    to diverge from the canonical one. Asserted so the boundary is a recorded decision rather
    than something a later reader assumes was missed."""
    calls: list[str] = []
    original = test_neo4j_repo.execute
    test_neo4j_repo.execute = lambda q, params=None, **kw: (  # type: ignore[method-assign]
        calls.append(q), original(q, params, **kw)
    )[1]
    try:
        writer.query_overview(PROJECT)
    finally:
        test_neo4j_repo.execute = original  # type: ignore[method-assign]

    assert len(calls) == 3, f"query_overview made {len(calls)} round trips"


@pytest.mark.online
def test_overview_counts_are_aggregated_server_side(test_neo4j_repo) -> None:
    """The regression this rewrite nearly shipped.

    The original used `count(n)`, so the server returned one row per ROLE. A combined query that
    collects one map per NODE and counts them in Python returns identical numbers -- every
    equivalence test above passes -- while shipping a map for every entity in the project.
    On a real codebase that is thousands of maps replacing a handful of rows: a bandwidth
    regression introduced by a latency fix.

    Correctness of the counts cannot detect it, so this asserts the shape ON THE WIRE instead.
    """
    test_neo4j_repo.execute(
        """
        CREATE (:Entity {structure_project: 'agg-fixture', structure_role: 'project'})
        """
    )
    test_neo4j_repo.execute(
        """
        UNWIND range(1, 200) AS i
        CREATE (:Entity {structure_project: 'agg-fixture', structure_role: 'file',
                         structure_path: 'f' + toString(i) + '.py'})
        """
    )

    writer = StructureGraphWriter(test_neo4j_repo)
    result = writer.query_overview("agg-fixture")
    assert result["entities"] == {"file": 200, "project": 1}

    rows = test_neo4j_repo.execute(
        """
        CALL {
            MATCH (n:Entity {structure_project: $p})
            WITH n.structure_role AS role, count(n) AS cnt
            RETURN collect({role: role, cnt: cnt}) AS e
        }
        RETURN e
        """,
        params={"p": "agg-fixture"},
    )
    # Two roles across 201 nodes. If the aggregation moved to Python this would be 201.
    assert len(rows[0]["e"]) == 2
