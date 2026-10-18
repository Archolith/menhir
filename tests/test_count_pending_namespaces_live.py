"""Live proof of the 4R pending-decision namespace listing: it lists exactly the namespaces whose
undecided or re-decide read is non-empty, so a quiet namespace is swept and nothing else is.

Runs online against the TEST instance; every test uses fresh namespaces and removes what it made.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.count_decide import REDECIDE_COOLDOWN_S
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
    # count_decided_at is set below: inside the cooldown, so not yet re-decided.
    "recent": ([{"id": "r", "status": "uncertain", "attempts": 1}], False),
    # ... and past it.
    "cooled": ([{"id": "r", "status": "uncertain", "attempts": 1}], True),
}
_DECIDED_AGO_S = {"recent": 60, "cooled": REDECIDE_COOLDOWN_S + 600}


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
    for case, ago in _DECIDED_AGO_S.items():
        repo.execute(
            "MATCH (a:TypedAssertion {namespace: $ns}) "
            "SET a.count_decided_at = datetime() - duration({seconds: $ago})",
            {"ns": names[case], "ago": ago})
    listed = set(adapter.pending_decision_namespaces(
        perceiver_version=_PV, max_attempts=3, limit=100000, cooldown_s=REDECIDE_COOLDOWN_S))
    mine = {ns for ns in listed if ns in names.values()}
    assert mine == {names[c] for c, (_, expected) in _CASES.items() if expected}
    for ns in names.values():
        reads = (adapter.undecided_count_occurrences(ns, perceiver_version=_PV, limit=10)
                 + adapter.redecide_count_occurrences(ns, perceiver_version=_PV, max_attempts=3,
                                                      limit=10, exclude_run_id="next-run",
                                                      cooldown_s=REDECIDE_COOLDOWN_S))
        assert (ns in mine) == bool(reads), ns
    # A dirty namespace's re-decide read has no cooldown: the recent row is still picked up.
    assert adapter.redecide_count_occurrences(
        names["recent"], perceiver_version=_PV, max_attempts=3, limit=10,
        exclude_run_id="next-run")
