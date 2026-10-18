"""Live proof of the counting re-version Cypher: the prior-version read, the head- and keep-scoped
retire, and why a same-head replacement must retire before it writes (counting versions such as
"cv1+m" are not SemVer, so they rank 0 and can never supersede the old current row in place).

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


def _occ(ns, ep, start, end, pv, *, op="occurrence", ordinal=1, value=1):
    return TypedAssertion(
        subject_uuid="s1", subject_display="me", attribute="gym_visit_count", scope="",
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
    _retire(adapter, ns, claim, ["t1"], source_keys=[gone.source_key])

    rows = adapter.prior_version_occurrences(ns, ["t1"], perceiver_version=_NEW)
    assert [(r["assertion_id"], r["source_key"], r["episode_uuid"], r["span_start"],
             r["span_end"], r["subject_uuid"]) for r in rows] == [
        (ids["a"], old_a.source_key, "t1", 0, 5, "s1"),
        (ids["b"], old_b.source_key, "t1", 10, 15, "s1")]
    assert adapter.prior_version_occurrences(ns, [], perceiver_version=_NEW) == []


@pytest.mark.online
def test_same_head_write_is_lost_unless_the_old_row_is_retired_first(env):
    # The counterexample: written over a current old row, the new rank-0 row is stored non-current
    # (and a later retire would leave the head with no current row). Retiring the head first makes
    # the same write current; exactly one row is current afterwards.
    repo, adapter, ns, _, claim = env
    old = _occ(ns, "t1", 0, 5, _OLD)
    _record(adapter, old)
    new = _occ(ns, "t1", 0, 5, _NEW)
    _record(adapter, new)
    assert _heads(repo, old.source_key) == [(_OLD, False, None, 1), (_NEW, True, None, 0)]

    assert _retire(adapter, ns, claim, ["t1"], source_keys=[old.source_key]) == ["s1"]
    _record(adapter, new)                                             # the retried write
    assert _heads(repo, old.source_key) == [
        (_OLD, True, "counting_reversion", 0), (_NEW, False, None, 1)]


@pytest.mark.online
def test_retire_honours_head_scope_and_keep_ids_and_the_fence(env):
    # Fails if a head-scoped retire reaches another head, if a kept (carried) row is retired, or if
    # either form writes under a lost fence.
    repo, adapter, ns, _, claim = env
    a, b, c = (_occ(ns, "t1", s, s + 5, _OLD) for s in (0, 10, 20))
    keep = _record(adapter, a)
    _record(adapter, b)
    _record(adapter, c)

    with pytest.raises(CountingFenceLost):
        adapter.retire_prior_version_occurrences(
            ns, ["t1"], perceiver_version=_NEW, run_id="run-b",
            incarnation=claim["incarnation"], epoch=claim["epoch"], keep_ids=[keep])
    assert [_heads(repo, x.source_key) for x in (a, b, c)] == [[(_OLD, False, None, 1)]] * 3

    _retire(adapter, ns, claim, ["t1"], source_keys=[b.source_key])
    assert _heads(repo, a.source_key) == [(_OLD, False, None, 1)]
    assert _heads(repo, b.source_key) == [(_OLD, True, "counting_reversion", 0)]
    assert _heads(repo, c.source_key) == [(_OLD, False, None, 1)]

    _retire(adapter, ns, claim, ["t1"], keep_ids=[keep])
    assert _heads(repo, a.source_key) == [(_OLD, False, None, 1)]       # carried
    assert _heads(repo, c.source_key) == [(_OLD, True, "counting_reversion", 0)]
