"""Live proof of the counting re-version Cypher: the prior-version read, the stored-span read, and
the keep-scoped retire that hands a head to its new row in the same statement (counting versions
such as "cv1+m" are not SemVer, so they rank 0 and a re-version is stored non-current next to the
old current row until then).

Runs online against the TEST instance; every test uses fresh namespaces and removes what it made.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion, perceiver_rank
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import CountingFenceLost

_OLD = "cv1"
_NEW = "cv1+m"
_NOW = "2026-07-01T00:00:00+00:00"


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"crev-{uuidlib.uuid4().hex[:10]}"
    other = f"{ns}-other"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    claim = adapter.claim_counting_namespace(ns, run_id="run-a", lease_seconds=600, now=_NOW)
    yield test_neo4j_repo, adapter, ns, other, claim
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace IN $names OR n.group_id IN $names "
        "OR n.namespace_key IN $names DETACH DELETE n", {"names": [ns, other]})


def _occ(ns, ep, start, end, pv, *, op="occurrence", ordinal=1, value=1, subject="s1"):
    return TypedAssertion(
        subject_uuid=subject, subject_display="me", attribute="gym_visit_count", scope="",
        value_kind="count", unit="", operation=op, value=value, stated_span="x" * (end - start),
        span_start=start, span_end=end, claim_ordinal=ordinal, episode_uuid=ep,
        valid_at=_NOW, learned_at=_NOW, evidence_tier="agent", perceiver_version=pv,
        namespace=ns, importance="tracked" if op == "occurrence" else "")


def _record(adapter, assertion):
    return str(adapter.record_typed_assertion(assertion)["assertion_id"])


def _heads(repo, source_key):
    rows = repo.execute(
        """
        MATCH (h:TypedAssertionHead {source_key: $sk})-[:HAS_VERSION]->(a:TypedAssertion)
        OPTIONAL MATCH (h)-[cr:CURRENT]->(a)
        RETURN a.perceiver_version AS pv, a.superseded AS superseded,
               a.retired_reason AS reason, count(cr) AS current
        ORDER BY pv
        """,
        {"sk": source_key})
    return [(r["pv"], r["superseded"], r["reason"], int(r["current"])) for r in rows]


def _retire(adapter, ns, claim, turns, **kw):
    return adapter.retire_prior_version_occurrences(
        ns, turns, perceiver_version=_NEW, run_id="run-a",
        incarnation=claim["incarnation"], epoch=claim["epoch"], **kw)


@pytest.mark.unit
def test_counting_versions_rank_zero():
    # The premise of the retire-before-write order; fails if the ranking ever changes.
    assert perceiver_rank(_OLD) == perceiver_rank(_NEW) == perceiver_rank("v1+x2+m") == 0


@pytest.mark.online
def test_prior_read_returns_only_current_other_version_occurrences_on_the_turns(env):
    # Fails if the read misses an old row (it would never be retired or carried), or returns a
    # current-version row, a scalar claim, another turn, another namespace or a retired row.
    repo, adapter, ns, other, claim = env
    old_a = _occ(ns, "t1", 0, 5, _OLD)
    old_b = _occ(ns, "t1", 10, 15, _OLD)
    ids = {"a": _record(adapter, old_a), "b": _record(adapter, old_b)}
    _record(adapter, _occ(ns, "t1", 20, 25, _NEW))                    # current version
    _record(adapter, _occ(ns, "t1", 30, 35, _OLD, op="absolute", ordinal=0))  # scalar lane
    _record(adapter, _occ(ns, "t9", 0, 5, _OLD))                      # other turn
    _record(adapter, _occ(other, "t1", 0, 5, _OLD))                   # other namespace
    gone = _occ(ns, "t1", 40, 45, _OLD)
    _record(adapter, gone)
    _retire(adapter, ns, claim, ["t1"], keep_ids=[ids["a"], ids["b"]])

    rows = adapter.prior_version_occurrences(ns, ["t1"], perceiver_version=_NEW)
    assert [(r["assertion_id"], r["source_key"], r["episode_uuid"], r["span_start"],
             r["span_end"], r["subject_uuid"]) for r in rows] == [
        (ids["a"], old_a.source_key, "t1", 0, 5, "s1"),
        (ids["b"], old_b.source_key, "t1", 10, 15, "s1")]
    assert adapter.prior_version_occurrences(ns, [], perceiver_version=_NEW) == []


def _bind(repo, ns, subjects, turns):
    """Entity and TurnEvidence nodes so records bind (an unbound row is never promoted or read as
    stored). Ids are namespaced: the record looks them up globally."""
    repo.execute(
        "UNWIND $s AS s CREATE (:Entity {uuid: s, namespace: $ns, group_id: $ns}) "
        "WITH count(*) AS _ UNWIND $t AS t "
        "CREATE (:TurnEvidence {turn_id: t, namespace: $ns, group_id: $ns})",
        {"s": [f"{ns}:{s}" for s in subjects], "t": [f"{ns}:{t}" for t in turns], "ns": ns})
    return ({s: f"{ns}:{s}" for s in subjects}, {t: f"{ns}:{t}" for t in turns})


def _rebuild_list(repo, ns):
    rows = repo.execute(
        "MATCH (w:CountingConsolidationWatermark {group_id: $ns}) "
        "RETURN coalesce(w.rebuild_subjects, []) AS s", {"ns": ns})
    return list(rows[0]["s"])


@pytest.mark.online
def test_the_retire_hands_the_head_to_its_stored_new_row_in_the_same_statement(env):
    # F-03: written over a current old row, the rank-0 new row is stored non-current, so the old
    # row is still counted if the run stops here. The retire then moves CURRENT to it: exactly one
    # row is current afterwards and the head is never empty. Fails if the head ends with no current
    # row, two current rows, or if the subject is not recorded as owed a rebuild.
    repo, adapter, ns, _, claim = env
    (s,), (t,) = (list(m.values()) for m in _bind(repo, ns, ["s1"], ["t1"]))
    old = _occ(ns, t, 0, 5, _OLD, subject=s)
    _record(adapter, old)
    new = _occ(ns, t, 0, 5, _NEW, subject=s)
    new_id = _record(adapter, new)
    assert _heads(repo, old.source_key) == [(_OLD, False, None, 1), (_NEW, True, None, 0)]

    assert _retire(adapter, ns, claim, [t], promote_ids=[new_id]) == [s]
    assert _heads(repo, old.source_key) == [
        (_OLD, True, "counting_reversion", 0), (_NEW, False, None, 1)]
    assert [dict(r) for r in repo.execute(
        "MATCH (n:TypedAssertion {assertion_id: $id})-[:SUPERSEDES]->(o:TypedAssertion) "
        "RETURN o.perceiver_version AS pv, o.superseded_by AS by", {"id": new_id})] == [
        {"pv": _OLD, "by": new_id}]
    assert _rebuild_list(repo, ns) == [s]
    _record(adapter, new)                                             # an idempotent re-write
    assert _heads(repo, old.source_key) == [
        (_OLD, True, "counting_reversion", 0), (_NEW, False, None, 1)]


@pytest.mark.online
def test_promotion_needs_the_old_row_retired_on_that_head(env):
    # Fails if a kept (carried) head or another head's id is promoted (two current rows on a head).
    repo, adapter, ns, _, claim = env
    (s,), (t,) = (list(m.values()) for m in _bind(repo, ns, ["s1"], ["t1"]))
    a, b = _occ(ns, t, 0, 5, _OLD, subject=s), _occ(ns, t, 10, 15, _OLD, subject=s)
    keep = _record(adapter, a)
    _record(adapter, b)
    a_new = _record(adapter, _occ(ns, t, 0, 5, _NEW, subject=s))
    _retire(adapter, ns, claim, [t], keep_ids=[keep], promote_ids=[a_new])
    assert _heads(repo, a.source_key) == [(_OLD, False, None, 1), (_NEW, True, None, 0)]
    assert _heads(repo, b.source_key) == [(_OLD, True, "counting_reversion", 0)]


@pytest.mark.online
def test_rebuild_list_is_returned_by_the_claim_and_cleared_only_by_its_owner(env):
    # F-02: fails if a lost owner's list is dropped, if a stale owner can clear it, or if a clear
    # removes subjects it did not rebuild.
    repo, adapter, ns, _, claim = env
    _record(adapter, _occ(ns, "t1", 0, 5, _OLD))
    _record(adapter, _occ(ns, "t2", 0, 5, _OLD, subject="s2"))
    _retire(adapter, ns, claim, ["t1", "t2"])
    assert sorted(_rebuild_list(repo, ns)) == ["s1", "s2"]
    adapter.release_counting_claim(ns, run_id="run-stale", incarnation=claim["incarnation"],
                                   rebuilt_subjects=["s1", "s2"])
    assert sorted(_rebuild_list(repo, ns)) == ["s1", "s2"]
    adapter.release_counting_claim(ns, run_id="run-a", incarnation=claim["incarnation"],
                                   rebuilt_subjects=["s1"])
    assert _rebuild_list(repo, ns) == ["s2"]
    again = adapter.claim_counting_namespace(ns, run_id="run-c", lease_seconds=600, now=_NOW)
    assert again["rebuild_subjects"] == ["s2"]


@pytest.mark.online
def test_stored_spans_are_bound_current_version_rows_or_the_included_ids(env):
    # Rule B's "what was stored" read. Fails if it misses a current row (from any run), returns a
    # non-current row it was not asked for, or returns another version.
    repo, adapter, ns, _, claim = env
    (s,), (t,) = (list(m.values()) for m in _bind(repo, ns, ["s1"], ["t1"]))
    _record(adapter, _occ(ns, t, 0, 5, _OLD, subject=s))
    waiting = _record(adapter, _occ(ns, t, 0, 5, _NEW, subject=s))   # non-current: old holds it
    current = _record(adapter, _occ(ns, t, 10, 15, _NEW, subject=s))
    _record(adapter, _occ(ns, t, 20, 25, _OLD, subject=s))
    _record(adapter, _occ(ns, t, 30, 35, _NEW, subject="s-unbound"))  # binding_pending

    def spans(include):
        rows = adapter.current_version_occurrence_spans(
            ns, [t], perceiver_version=_NEW, include_ids=include)
        return sorted((r["assertion_id"], r["span_start"]) for r in rows)

    assert spans([]) == [(current, 10)]
    assert spans([waiting]) == sorted([(waiting, 0), (current, 10)])


@pytest.mark.online
def test_retire_honours_keep_ids_and_the_fence(env):
    # Fails if a kept (carried) row is retired, or if the retire writes under a lost fence.
    repo, adapter, ns, _, claim = env
    a, c = (_occ(ns, "t1", s, s + 5, _OLD) for s in (0, 20))
    keep = _record(adapter, a)
    _record(adapter, c)

    with pytest.raises(CountingFenceLost):
        adapter.retire_prior_version_occurrences(
            ns, ["t1"], perceiver_version=_NEW, run_id="run-b",
            incarnation=claim["incarnation"], epoch=claim["epoch"], keep_ids=[keep])
    assert [_heads(repo, x.source_key) for x in (a, c)] == [[(_OLD, False, None, 1)]] * 2
    assert _rebuild_list(repo, ns) == []

    _retire(adapter, ns, claim, ["t1"], keep_ids=[keep])
    assert _heads(repo, a.source_key) == [(_OLD, False, None, 1)]       # carried
    assert _heads(repo, c.source_key) == [(_OLD, True, "counting_reversion", 0)]


@pytest.mark.online
def test_a_carried_pre_match_row_is_a_counted_target_for_decide(test_neo4j_repo):
    # F-05: an undecided row stamped by a pre-match version was counted under that version. Fails
    # if the decide pool leaves it out (a later retelling would count twice), or if a repeat of it
    # is treated as having a dead target and re-decided.
    from tests.test_count_decide_live import _PV, _seed

    ns = f"crev-{uuidlib.uuid4().hex[:10]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    try:
        _seed(test_neo4j_repo, ns, [
            {"id": "legacy", "pv": "v1"},
            {"id": "undecided-match"},
            {"id": "r", "status": "repeat", "target": "legacy", "attempts": 1},
        ])
        pool = adapter.counted_occurrences_for_subjects(ns, ["self"], limit=10)
        assert [p["assertion_id"] for p in pool] == [f"{ns}:legacy"]
        assert adapter.redecide_count_occurrences(
            ns, perceiver_version=_PV, max_attempts=3, limit=10, exclude_run_id="x") == []
    finally:
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n", {"ns": ns})
