"""CF-250 -- `update_edge_facts` counted every edge twice, and CF-75 walked past it.

CF-75 found that `increment_edge_weight` matched with an anonymous undirected pattern, which
yields each relationship once per assignment of its two free endpoints -- so the ratchet applied
twice per traversal. That was fixed, and the reason was written into a comment in
`consolidation_queries.py`. Seventy lines below that comment, `update_edge_facts` had the same
pattern and kept the defect.

The two differ in consequence, which is why this is filed separately rather than folded in:

    increment_edge_weight   the second visit MUTATED    weight moved 0.2 per traversal
    update_edge_facts       the second visit is a NO-OP  `SET` is idempotent; only the count lied

So no fact was ever corrupted. What was wrong is the returned count -- exactly double -- and the
work done, since the scan walked every relationship in the graph twice per call. The count is
latent today: both call sites in `enrichment_steps` discard the return value. It is a wrong number
waiting for a caller, not a live miscount, and it is graded that way.

Measured on Neo4j 5, not reasoned about: one real relationship, undirected reported `2`, directed
reported `1`. Self-loops are the one case where the two agree (both report 1), so the fix does not
change behaviour there either.

**The general shape, since that is what CF-75 got wrong.** Only a match whose endpoints are BOTH
anonymous double-yields. An anchored pattern binds one endpoint, so each incident relationship is
yielded once, and every other undirected match in this codebase is anchored, deduplicated with
`DISTINCT`, or deduplicated in Python with a comment saying so. The ratchet below encodes that
distinction so the next instance cannot be introduced silently.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest


def _seed(repo: Any, count: int = 5) -> None:
    repo.execute("MATCH (n) DETACH DELETE n")
    repo.execute(
        "UNWIND range(1,$n) AS i "
        "CREATE (a:Entity {uuid:'a'+toString(i)})"
        "-[:RELATES_TO {uuid:'e'+toString(i), fact:'original'}]->"
        "(b:Entity {uuid:'b'+toString(i)})",
        params={"n": count},
    )


def _fact(repo: Any, edge_uuid: str) -> tuple[str, str]:
    rows = repo.execute(
        "MATCH ()-[r]->() WHERE r.uuid = $u RETURN r.fact AS f, r.fact_source AS s",
        params={"u": edge_uuid},
    )
    return str(rows[0]["f"]), str(rows[0]["s"])


@pytest.mark.online
def test_the_returned_count_is_the_number_of_edges_not_twice_it(test_neo4j_repo) -> None:
    """THE FINDING. Three edges updated returned 6."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    updated = repo.update_edge_facts([
        {"uuid": "e1", "fact": "one", "fact_source": "original"},
        {"uuid": "e2", "fact": "two", "fact_source": "original"},
        {"uuid": "e3", "fact": "three", "fact_source": "original"},
    ])

    assert updated == 3, "the undirected match counted each edge once per direction"


@pytest.mark.online
def test_the_fact_and_its_provenance_actually_land(test_neo4j_repo) -> None:
    """POSITIVE CONTROL. A count of 3 is also what a query that updated nothing would report if
    the match were broken in the other direction, so assert the write itself."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.update_edge_facts([{"uuid": "e2", "fact": "repaired", "fact_source": "synthetic_fallback"}])

    assert _fact(test_neo4j_repo, "e2") == ("repaired", "synthetic_fallback")


@pytest.mark.online
def test_only_the_named_edges_are_touched(test_neo4j_repo) -> None:
    """NEGATIVE CONTROL. `WHERE r.uuid = update.uuid` is one edit away from matching everything,
    and a query that rewrote every fact in the graph would still return a plausible count."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.update_edge_facts([{"uuid": "e2", "fact": "repaired", "fact_source": "original"}])

    for untouched in ("e1", "e3", "e4", "e5"):
        assert _fact(test_neo4j_repo, untouched)[0] == "original", untouched


@pytest.mark.online
def test_a_self_loop_is_counted_once_by_both_forms(test_neo4j_repo) -> None:
    """The one case where the two forms agree. Pinned so the fix is not later "reverted" on the
    theory that it changed self-loop behaviour -- it does not."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "CREATE (a:Entity {uuid:'solo'})-[:RELATES_TO {uuid:'loop', fact:'original'}]->(a)"
    )
    repo = ConsolidationRepository(test_neo4j_repo)

    assert repo.update_edge_facts([
        {"uuid": "loop", "fact": "x", "fact_source": "original"}
    ]) == 1


class _RecordingNeo4j:
    """Records every query issued, so "did not touch the database" is an assertion."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
        return [{"updated": 0}]


@pytest.mark.unit
def test_an_empty_update_list_does_not_reach_the_database() -> None:
    """The early return exists to skip a pointless round trip, and only a call count can show it.

    An earlier version of this test asserted `update_edge_facts([]) == 0` against a real graph and
    was VACUOUS: `UNWIND []` produces no rows, and an aggregation over no rows still returns a
    single row holding 0 -- so deleting the guard entirely left the return value identical. It
    survived the mutation that removed the thing it was meant to protect.
    """
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    neo4j = _RecordingNeo4j()
    repo = ConsolidationRepository(neo4j)

    assert repo.update_edge_facts([]) == 0
    assert neo4j.queries == [], "an empty update list still issued a query"


# ---------------------------------------------------------------------------
# Offline ratchet: the shape, not this one instance.
# ---------------------------------------------------------------------------

#: Built by concatenation rather than written out, because this file is itself scanned by the
#: check below and a literal would make the ratchet fail on its own source. Same reason the
#: docstrings above describe the pattern instead of quoting it.
_ANON = "(" + ")" + "-["
_UNDIRECTED_TAIL = "]-" + "("


def _anonymous_undirected_matches(text: str) -> list[str]:
    """Find relationship patterns whose endpoints are BOTH anonymous and whose match is
    undirected -- the only form that yields each relationship twice.

    Anchored to a preceding MATCH keyword, which is what makes this a check rather than a
    substring hunt. Two legitimate uses of the same character sequence exist in this codebase
    and would otherwise be flagged:

      * relationship-index DDL -- `CREATE INDEX ... FOR (<anon>)-[r:TYPE]-(<anon>) ON (r.prop)`.
        Undirected is the REQUIRED syntax there and no traversal happens at all.
      * prose quoting the defect, including the CF-75 comment that explains this very hazard.
        A guard that fires on its own documentation gets deleted rather than obeyed.
    """
    pattern = re.compile(
        r"(?:OPTIONAL\s+)?MATCH\s*"
        + re.escape(_ANON) + r"[A-Za-z_][A-Za-z0-9_]*(?::[^\]]*)?"
        + re.escape(_UNDIRECTED_TAIL) + r"\)"
    )
    return pattern.findall(text)


@pytest.mark.unit
def test_no_query_in_the_codebase_matches_relationships_undirected_from_both_ends() -> None:
    """THE RATCHET. CF-75 fixed one instance and left its sibling seventy lines away, because
    nothing looked for the shape. Zero tolerance, no allowlist: every legitimate undirected match
    in this codebase binds at least one endpoint, so this class has no valid instances to exempt.
    """
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        found = _anonymous_undirected_matches(path.read_text(encoding="utf-8"))
        offenders.extend(f"{path.relative_to(src)}: {hit}" for hit in found)

    assert not offenders, (
        "a relationship match with two anonymous endpoints yields every relationship twice; "
        "bind an endpoint or make it directed:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.unit
def test_the_ratchet_can_actually_see_the_shape_it_forbids() -> None:
    """Guards the guard. A regex that silently matched nothing would make the ratchet above pass
    forever, which is the failure mode a source scan is most prone to."""
    bad = "MATCH " + _ANON + "r" + _UNDIRECTED_TAIL + ")\nWHERE r.uuid = $u"
    assert _anonymous_undirected_matches(bad), "the ratchet cannot see its own counterexample"

    for benign in (
        "MATCH (n)-[r]-(peer)",          # anchored on n
        "OPTIONAL MATCH (n)-[r]-()",     # anchored on n
        "MATCH " + _ANON + "r]->()",     # directed
        # The two real shapes that tripped the first version of this ratchet, kept as fixtures
        # so a future loosening of the regex fails here rather than silently in the sweep.
        "CREATE INDEX x IF NOT EXISTS FOR " + _ANON + "r:RELATES_TO" + _UNDIRECTED_TAIL
        + ") ON (r.type)",
        "#: This match was `" + _ANON + "r" + _UNDIRECTED_TAIL + ")`, which yields it TWICE",
    ):
        assert not _anonymous_undirected_matches(benign), benign
