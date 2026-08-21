"""CF-114: `evidence_stats` made six sequential full-label scans of `:TurnEvidence`.

Six `execute` calls, each its own round trip, each opening with an unfiltered
`MATCH (t:TurnEvidence)`. No namespace predicate, no time bound, no `LIMIT` on the scan itself.

The entry makes a point worth keeping: **there is no loop here**, which is why no AST or probe pass
found it. Probes find repetition, not redundancy.

**The obvious fix is a regression, and it was measured before being rejected.** Collapsing to ONE
pass that `collect()`s the grouped fields and counts them in Python trades scan count for unbounded
transfer -- on a label the entry itself says grows without bound. Neo4j 5.26.21, test instance:

    n=50,000   6 queries (before)    400,007 dbHits     20 rows over wire    105 ms
               1 query  (collect)    300,001 dbHits    250,000 values        303 ms
               3 queries (shipped)   400,003 dbHits     15 rows over wire    100 ms

The single-pass version is 3x slower at 50k and degrades further with N. Fewer dbHits, far worse
wall clock -- a reminder that dbHits is not a proxy for cost once payload size enters.

The shipped shape keeps every aggregation server-side, so the payload is bounded by DISTINCT-value
cardinality rather than row count:

* `role` + `triage_version` fold into one composite grouping; the marginals, `total` and `latest`
  are derived from it. Correct because summing a partition gives the whole, and max-of-maxes is the
  max.
* `source_kind` keeps its own query so its `LIMIT 10` stays server-side.
* `triage_reason` keeps its own because `UNWIND` on a list property multiplies cardinality and
  would corrupt every other aggregate sharing the pass.

The win is round trips (6 -> 3) with no transfer penalty, which is smaller than "collapse to one
pass" but is the one that survives contact with a large table.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

pytestmark = pytest.mark.unit

#: What the six-query version returned, reproduced here from the ORIGINAL code so the expectation
#: is not derived from the new implementation.
_ROWS = [
    {"role": "user", "version": "v1", "count": 5},
    {"role": "user", "version": "v2", "count": 3},
    {"role": "assistant", "version": "v1", "count": 4},
    {"role": "tool", "version": "v2", "count": 1},
]


class _StubNeo4j:
    """Answers each of the three queries by matching on a distinctive fragment."""

    def __init__(self, *, rows=_ROWS, sources=None, reasons=None) -> None:
        self._rows = rows
        self._sources = sources if sources is not None else [
            {"sk": "claude_code_hook", "c": 7}, {"sk": "mcp", "c": 6},
        ]
        self._reasons = reasons if reasons is not None else [
            {"reason": "short", "c": 9}, {"reason": "noise", "c": 2},
        ]
        self.queries: list[str] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list[dict]:
        self.queries.append(query)
        if "t.source_kind AS sk" in query:
            return list(self._sources)
        if "UNWIND t.triage_reason" in query:
            return list(self._reasons)
        return [
            {"role": r["role"], "version": r["version"], "c": r["count"],
             "latest": f"2026-08-2{i}T00:00:00Z"}
            for i, r in enumerate(self._rows)
        ]


def _stats(neo: _StubNeo4j) -> dict[str, Any]:
    repo = TurnEvidenceRepository.__new__(TurnEvidenceRepository)
    repo._neo4j = neo
    return repo.evidence_stats()


def test_the_round_trip_count_dropped_from_six_to_three() -> None:
    """The finding. Asserted exactly, so a regression to six -- or a drift to four -- fails."""
    neo = _StubNeo4j()
    _stats(neo)

    assert len(neo.queries) == 3, neo.queries


def test_no_query_collects_a_per_row_field() -> None:
    """The rejected design, pinned. `collect(t.role)` and friends ship one value PER NODE; that is
    the 3x-slower shape. Aggregation must stay server-side."""
    neo = _StubNeo4j()
    _stats(neo)

    for query in neo.queries:
        for field in ("collect(t.role", "collect(t.source_kind", "collect(t.triage_version",
                      "collect(t.triage_reason"):
            assert field not in query, f"per-row collect reintroduced: {query}"


def test_marginals_are_derived_correctly_from_the_composite_grouping() -> None:
    """The correctness risk of folding two groupings into one: the marginals must still be right."""
    stats = _stats(_StubNeo4j())

    # user 5+3=8, assistant 4, tool 1
    assert stats["by_role"] == {"user": 8, "assistant": 4, "tool": 1}
    # v1 5+4=9, v2 3+1=4
    assert stats["triage_version_counts"] == {"v1": 9, "v2": 4}
    assert stats["total_turn_evidence"] == 13
    assert stats["user_evidence"] == 8


def test_latest_is_the_max_across_groups() -> None:
    """max-of-maxes. A fold that took the first group's value would pass the counts above."""
    stats = _stats(_StubNeo4j())

    assert stats["latest_recorded_at"] == "2026-08-23T00:00:00Z"


def test_ordering_is_count_descending() -> None:
    stats = _stats(_StubNeo4j())

    assert list(stats["by_role"]) == ["user", "assistant", "tool"]
    assert list(stats["triage_version_counts"]) == ["v1", "v2"]


def test_source_kind_limit_stays_server_side() -> None:
    """The `LIMIT 10` must remain in Cypher -- moving it to Python would ship every distinct
    source_kind, which is the same unbounded-transfer mistake in miniature."""
    neo = _StubNeo4j()
    _stats(neo)

    source_query = next(q for q in neo.queries if "t.source_kind AS sk" in q)
    assert "LIMIT 10" in source_query
    assert "ORDER BY c DESC" in source_query


def test_the_full_returned_shape_is_unchanged() -> None:
    """POSITIVE CONTROL: every key the six-query version produced, with its value."""
    stats = _stats(_StubNeo4j())

    assert stats == {
        "turn_evidence_table_exists": True,
        "total_turn_evidence": 13,
        "by_role": {"user": 8, "assistant": 4, "tool": 1},
        "by_source_kind": {"claude_code_hook": 7, "mcp": 6},
        "claude_code_hook_evidence": 7,
        "triage_version_counts": {"v1": 9, "v2": 4},
        "triage_reason_counts": {"short": 9, "noise": 2},
        "user_evidence": 8,
        "latest_recorded_at": "2026-08-23T00:00:00Z",
    }


def test_an_empty_graph_returns_the_same_zero_shape() -> None:
    """POSITIVE CONTROL: no rows must not raise and must not change the key set."""
    stats = _stats(_StubNeo4j(rows=[], sources=[], reasons=[]))

    assert stats["turn_evidence_table_exists"] is False
    assert stats["total_turn_evidence"] == 0
    assert stats["by_role"] == {}
    assert stats["latest_recorded_at"] is None
    assert stats["claude_code_hook_evidence"] == 0
