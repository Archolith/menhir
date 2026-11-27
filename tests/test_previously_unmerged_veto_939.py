"""#939: a pair an operator unmerged is never re-merged automatically (PREVIOUSLY_UNMERGED).

Two independent records prove a split, and either one vetoes:
- the node stamp ``restored_from_merge`` that ``restore_merge_snapshot`` writes on the restored
  node, naming the survivor it came out of (read by the eligibility policy);
- the journal: a REVERSED ENTITY_MERGE of the pair, or a COMMITTED (LEGACY_)ENTITY_UNMERGE of it.
  The unmerge row counts on its own because the forward merge is marked REVERSED only best-effort
  (#920).

Counterexamples against 4794b41f: nothing read ``restored_from_merge`` and the coordinator never
consulted the journal's history, so every case below was ELIGIBLE / merged.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator, pair_key

pytestmark = pytest.mark.unit


def _node(uuid: str, *, restored_from_merge: str | None = None) -> me.NodeSignals:
    return me.NodeSignals(
        uuid=uuid, exists=True, ineligible_role=False, namespace="default",
        freshness="ACTIVE", scope="SESSION", user_flagged=False, conflict_status=None,
        restored_from_merge=restored_from_merge,
    )


# --------------------------------------------------------------------------- node stamp

@pytest.mark.parametrize(
    "survivor, absorbed",
    [
        (_node("S"), _node("B", restored_from_merge="S")),   # B was split out of S
        (_node("S", restored_from_merge="B"), _node("B")),   # the reverse direction
    ],
    ids=["absorbed-restored-from-survivor", "survivor-restored-from-absorbed"],
)
def test_a_restore_stamp_naming_the_other_node_vetoes(survivor, absorbed) -> None:
    decision = me.evaluate(survivor, absorbed)
    assert (decision.allowed, decision.reason_code) == (False, me.PREVIOUSLY_UNMERGED)


def test_a_restore_from_a_third_node_does_not_veto() -> None:
    decision = me.evaluate(_node("S"), _node("B", restored_from_merge="OTHER"))
    assert decision.reason_code == me.ELIGIBLE


def test_the_repository_reads_the_stamp() -> None:
    neo4j = MagicMock()
    base = {"ineligible_role": False, "namespace": "default", "freshness": "ACTIVE",
            "scope": "SESSION", "user_flagged": False, "memory_retired": False,
            "source_retention_protected": False, "conflict_status": None}
    neo4j.execute.return_value = [
        {"uuid": "S", **base, "restored_from_merge": None},
        {"uuid": "B", **base, "restored_from_merge": "S"},
    ]
    decision = CorrelationRepository(neo4j).evaluate_merge_eligibility("S", "B")
    assert decision.reason_code == me.PREVIOUSLY_UNMERGED
    assert "n.restored_from_merge AS restored_from_merge" in neo4j.execute.call_args.args[0]


# --------------------------------------------------------------------------- journal

def _insert(journal: GraphOperationsJournal, op_id: str, kind: str, state: str, key: str) -> None:
    journal._ensure_ready()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, target_key, request_json, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (op_id, kind, key, json.dumps({}), state, f"2026-11-26T00:00:0{len(op_id) % 10}Z",
             "2026-11-26T00:00:00Z"),
        )


@pytest.fixture
def journal(tmp_path) -> GraphOperationsJournal:
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


@pytest.mark.parametrize(
    "kind, state, found",
    [
        ("ENTITY_MERGE", "REVERSED", True),
        ("ENTITY_UNMERGE", "COMMITTED", True),         # forward row left COMMITTED (#920)
        ("LEGACY_ENTITY_UNMERGE", "COMMITTED", True),
        ("ENTITY_MERGE", "COMMITTED", False),          # merged, never split
        ("ENTITY_UNMERGE", "FAILED", False),           # the split did not happen
        ("ENTITY_UNMERGE", "NEEDS_REVIEW", False),
    ],
)
def test_journal_history_for_the_pair(journal, kind, state, found) -> None:
    key = pair_key("S", "B")
    _insert(journal, "op-1", kind, state, key)
    assert journal.pair_unmerge_op_ids(key) == (["op-1"] if found else [])
    # Pair keys are order-independent, and another pair's history is not this pair's.
    assert journal.pair_unmerge_op_ids(pair_key("B", "S")) == (["op-1"] if found else [])
    assert journal.pair_unmerge_op_ids(pair_key("S", "C")) == []


class _EligibleGraph:
    def __init__(self) -> None:
        self.snapshots = 0

    def evaluate_merge_eligibility(self, survivor_uuid, absorbed_uuid):
        return me.MergeEligibility(allowed=True, reason_code=me.ELIGIBLE)

    def capture_merge_snapshot(self, *args, **kwargs):
        self.snapshots += 1
        raise AssertionError("a previously unmerged pair must abstain before the snapshot")


def test_the_coordinator_abstains_on_a_journaled_split(journal) -> None:
    _insert(journal, "unmerge-1", "ENTITY_UNMERGE", "COMMITTED", pair_key("S", "B"))
    graph = _EligibleGraph()

    result = MergeCoordinator(graph_adapter=graph, journal=journal).merge(
        survivor_uuid="S", absorbed_uuid="B", similarity=0.97,
    )

    assert result["merged"] == 0 and result["reason"] == me.PREVIOUSLY_UNMERGED
    assert result["diagnostics"] == {"source": "journal", "op_ids": ["unmerge-1"]}
    assert graph.snapshots == 0
