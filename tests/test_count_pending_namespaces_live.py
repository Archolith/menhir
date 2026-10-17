"""Live proof of the 4R pending-decision namespace listing: it lists exactly the namespaces whose
undecided or re-decide read is non-empty, so a quiet namespace is swept and nothing else is.

Runs online against the TEST instance; every test uses fresh namespaces and removes what it made.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from tests.test_count_decide_live import _PV, _seed

_CASES = {
    "undecided": ([{"id": "r"}], True),
    "uncertain": ([{"id": "r", "status": "uncertain", "attempts": 1}], True),
    "spent": ([{"id": "r", "status": "uncertain", "attempts": 3}], False),
    "settled": ([{"id": "root", "status": "counted"},
                 {"id": "r", "status": "repeat", "target": "root"}], False),
    "dead": ([{"id": "gone", "status": "counted", "superseded": True},
              {"id": "r", "status": "same_event", "target": "gone"}], True),
    "foreign": ([{"id": "r", "status": "repeat", "target": "root", "target_ns": "settled"}], True),
    "old-version": ([{"id": "r", "pv": "v1"}], False),
    "superseded": ([{"id": "r", "superseded": True}], False),
}


@pytest.fixture
def env(test_neo4j_repo):
    base = f"cpns-{uuidlib.uuid4().hex[:10]}"
    names = {case: f"{base}-{case}" for case in _CASES}
    yield test_neo4j_repo, MemoryGraphAdapter(neo4j=test_neo4j_repo), names
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace IN $names OR n.group_id IN $names DETACH DELETE n",
        {"names": list(names.values())})


@pytest.mark.online
def test_listing_matches_the_per_namespace_sweep_reads(env):
    repo, adapter, names = env
    for case, (rows, _) in _CASES.items():
        _seed(repo, names[case], [
            dict(r, target_ns=names[r["target_ns"]]) if r.get("target_ns") else r for r in rows])
    listed = set(adapter.pending_decision_namespaces(
        perceiver_version=_PV, max_attempts=3, limit=100000))
    mine = {ns for ns in listed if ns in names.values()}
    assert mine == {names[c] for c, (_, expected) in _CASES.items() if expected}
    for ns in names.values():
        reads = (adapter.undecided_count_occurrences(ns, perceiver_version=_PV, limit=10)
                 + adapter.redecide_count_occurrences(ns, perceiver_version=_PV, max_attempts=3,
                                                      limit=10, exclude_run_id="next-run"))
        assert (ns in mine) == bool(reads), ns
