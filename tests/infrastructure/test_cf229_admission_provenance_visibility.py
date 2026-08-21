"""CF-229: turn capture ran for 576 turns and drew 0 admission edges, and nothing said so.

The `ADMITTED_ON` join between a memory and the turn it was admitted on is written by
`ingest_intake` when a caller supplies `turn_evidence_uuid`. On this deployment no caller does:
turn capture and memory writes come from different clients with disjoint session ids, so the
producer captures turns and never reports the pairing.

The code half was closed at `ad760b2` -- the in-process pairing exists and is tested. What was left
is the part that let it sit unnoticed: **nothing reported the disconnection**. Every unit test
passed, the live E2E passed, and the endpoint that draws the edge was simply never called. The
finding was eventually caught by a residue audit returning a suspicious 60/60 "unevaluable", i.e.
by a human noticing a number looked like a broken join.

So the durable fix is observability, and it is the only part of this finding that IS a code change
in this repo. The wiring belongs to a client outside it, and the historical corpus must not be
reconstructed -- temporal or textual similarity would FABRICATE provenance, which is worse than
admitting the gap.

Measured against the operator's graph after this change:

    turn_evidence_count      576
    admission_edge_count     0
    admission_provenance     never_linked
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.memory_queries import (
    ADMISSION_LINKED,
    ADMISSION_NEVER_LINKED,
    ADMISSION_NO_TURNS,
    MemoryQueryRepository,
    admission_provenance_state,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------


def test_turns_captured_and_no_edges_is_flagged() -> None:
    """THE FINDING, as a state: capture is running and the join has never been drawn."""
    assert (
        admission_provenance_state(turn_evidence_count=576, admission_edge_count=0)
        == ADMISSION_NEVER_LINKED
    )


def test_one_edge_is_enough_to_clear_it() -> None:
    """POSITIVE CONTROL: the signal is zero-of-many, so a single edge proves the wiring works and
    the state must clear. A classifier that stayed red would be noise an operator learns to
    ignore."""
    assert (
        admission_provenance_state(turn_evidence_count=576, admission_edge_count=1)
        == ADMISSION_LINKED
    )


def test_no_turns_is_not_a_problem() -> None:
    """POSITIVE CONTROL: with no turns there is nothing to pair. Reporting a fault on a deployment
    that simply does not capture turns would be a false alarm on the majority case."""
    assert (
        admission_provenance_state(turn_evidence_count=0, admission_edge_count=0)
        == ADMISSION_NO_TURNS
    )


@pytest.mark.parametrize("edges", [1, 100, 575])
def test_a_partial_ratio_is_deliberately_not_flagged(edges: int) -> None:
    """THE RESTRAINT, pinned so it is not "improved" into a threshold later.

    Not every memory is admitted on a turn, so fewer edges than turns is the normal healthy shape.
    A ratio threshold would be a guess dressed as a diagnosis and would fire on correct
    deployments. Only zero-of-many carries its own proof."""
    assert (
        admission_provenance_state(turn_evidence_count=576, admission_edge_count=edges)
        == ADMISSION_LINKED
    )


# ---------------------------------------------------------------------------
# the overview reports it
# ---------------------------------------------------------------------------


class _Neo4j:
    """Returns the overview row for the aggregate query and the counts for the admission one."""

    def __init__(self, *, turns: int, edges: int) -> None:
        self._turns = turns
        self._edges = edges
        self.queries: list[str] = []

    def execute(self, query: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        if "ADMITTED_ON" in query:
            return [
                {"turn_evidence_count": self._turns, "admission_edge_count": self._edges}
            ]
        return [{"total_memories": 3, "entity_count": 2, "episode_count": 1}]


def test_the_overview_carries_both_counts_and_the_verdict() -> None:
    """The counts are reported side by side because neither is meaningful alone -- it is the pair
    that reveals a producer capturing turns and never reporting the pairing."""
    overview = MemoryQueryRepository(_Neo4j(turns=576, edges=0)).fetch_memory_overview()

    assert overview["turn_evidence_count"] == 576
    assert overview["admission_edge_count"] == 0
    assert overview["admission_provenance"] == ADMISSION_NEVER_LINKED


def test_the_existing_overview_fields_are_untouched() -> None:
    """POSITIVE CONTROL: this feeds the MCP metadata resource. Adding counts must not disturb what
    was already there, or a consumer breaks on a diagnostic."""
    overview = MemoryQueryRepository(_Neo4j(turns=0, edges=0)).fetch_memory_overview()

    assert overview["total_memories"] == 3
    assert overview["entity_count"] == 2
    assert overview["episode_count"] == 1


def test_a_healthy_deployment_reports_linked() -> None:
    overview = MemoryQueryRepository(_Neo4j(turns=10, edges=4)).fetch_memory_overview()

    assert overview["admission_provenance"] == ADMISSION_LINKED


def test_the_admission_counts_are_a_separate_statement() -> None:
    """:TurnEvidence is neither :Entity nor :Episodic, so it cannot be folded into the existing
    CASE aggregation. Pinned so a later "optimisation" that merges them -- and silently returns
    zero for both -- fails here rather than in production."""
    neo4j = _Neo4j(turns=5, edges=5)
    MemoryQueryRepository(neo4j).fetch_memory_overview()

    assert len(neo4j.queries) == 2
    assert any("ADMITTED_ON" in q for q in neo4j.queries)


def test_missing_admission_rows_degrade_to_zero_not_a_crash() -> None:
    """An empty result must not take down the metadata resource: this is a diagnostic, and a
    diagnostic that can crash the surface it reports on is worse than the gap it describes."""

    class _Empty(_Neo4j):
        def execute(self, query: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
            self.queries.append(query)
            return [] if "ADMITTED_ON" in query else [{"total_memories": 1}]

    overview = MemoryQueryRepository(_Empty(turns=0, edges=0)).fetch_memory_overview()

    assert overview["turn_evidence_count"] == 0
    assert overview["admission_edge_count"] == 0
    assert overview["admission_provenance"] == ADMISSION_NO_TURNS
