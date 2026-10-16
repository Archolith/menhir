"""Live proof of the counting-lane cursor Cypher (scalar counting plan, unit 3).

The counting lane reads user :TurnEvidence only AT OR BEFORE the scalar cursor (for the scalar
perceiver_version in effect) and AFTER its own :CountingConsolidationWatermark. A FakeNeo4j cannot
evaluate the (recorded_at, turn_id) comparisons, the version reset or the pause, so this is online
against the TEST instance (same fixture/marker as test_scalar_provenance_edges_live.py).
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_AT = "2026-07-20T00:00:00Z"


def _iso(i: int) -> str:
    return (_T0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cnt-{uuidlib.uuid4().hex[:10]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    yield test_neo4j_repo, adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n", {"ns": ns})


def _seed_turns(repo, ns, spec):
    """spec: list of (turn_id, minute offset). Text is non-empty, role/declarant user."""
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (:TurnEvidence {turn_id: r.id, namespace: $ns, role: 'user', declarant: 'user',
                               text: 'I went to the gym ' + r.id, recorded_at: datetime(r.at)})
        """,
        {"ns": ns, "rows": [{"id": tid, "at": _iso(m)} for tid, m in spec]},
    )


def _scalar_cursor(adapter, ns, turn_id, minute, pv="v1"):
    adapter.advance_scalar_cursor(
        ns, cursor_at=_iso(minute), cursor_uuid=turn_id, perceiver_version=pv, at=_AT)


def _load(adapter, ns, *, pv="v1", spv="v1", limit=500):
    return adapter.load_next_counting_batch(
        ns, perceiver_version=pv, scalar_perceiver_version=spv, limit=limit)


def _ids(rows):
    return [r["uuid"] for r in rows]


def _dirty(adapter, ns, *, pv="v1", spv="v1"):
    return ns in adapter.list_counting_dirty_namespaces(
        perceiver_version=pv, scalar_perceiver_version=spv, limit=100000)


@pytest.mark.online
def test_never_reads_past_the_scalar_cursor_and_follows_it(env):
    repo, adapter, ns = env
    _seed_turns(repo, ns, [(f"t{i}", i) for i in range(1, 6)])
    _scalar_cursor(adapter, ns, "t3", 3)

    rows = _load(adapter, ns)
    assert _ids(rows) == ["t1", "t2", "t3"]
    assert _dirty(adapter, ns)

    last = rows[-1]
    adapter.advance_counting_cursor(
        ns, cursor_at=last["cursor_at"], cursor_uuid=last["uuid"], perceiver_version="v1", at=_AT)
    assert _load(adapter, ns) == []
    assert not _dirty(adapter, ns)

    _scalar_cursor(adapter, ns, "t5", 5)
    assert _ids(_load(adapter, ns)) == ["t4", "t5"]
    assert _dirty(adapter, ns)


@pytest.mark.online
def test_scalar_version_mismatch_or_missing_scalar_cursor_pauses_counting(env):
    repo, adapter, ns = env
    _seed_turns(repo, ns, [(f"t{i}", i) for i in range(1, 4)])

    # no scalar watermark at all
    assert _load(adapter, ns) == []
    assert not _dirty(adapter, ns)

    # scalar watermark stamped by another version than the caller's
    _scalar_cursor(adapter, ns, "t3", 3, pv="v2")
    assert _load(adapter, ns, spv="v1") == []
    assert not _dirty(adapter, ns, spv="v1")
    # ...and it is visible once the caller's scalar version matches (proves the pause is the version)
    assert _ids(_load(adapter, ns, spv="v2")) == ["t1", "t2", "t3"]


@pytest.mark.online
def test_counting_version_change_resets_the_counting_cursor(env):
    repo, adapter, ns = env
    _seed_turns(repo, ns, [(f"t{i}", i) for i in range(1, 6)])
    _scalar_cursor(adapter, ns, "t3", 3)
    adapter.advance_counting_cursor(
        ns, cursor_at=_iso(3), cursor_uuid="t3", perceiver_version="v0", at=_AT)

    assert _ids(_load(adapter, ns, pv="v1")) == ["t1", "t2", "t3"]
    assert _dirty(adapter, ns, pv="v1")
    # same version as the stamp: fully caught up
    assert _load(adapter, ns, pv="v0") == [] and not _dirty(adapter, ns, pv="v0")


@pytest.mark.online
def test_second_page_continues_at_turn_501(env):
    repo, adapter, ns = env
    _seed_turns(repo, ns, [(f"t{i:04d}", i) for i in range(1, 601)])
    _scalar_cursor(adapter, ns, "t0600", 600)

    first = _load(adapter, ns, limit=500)
    assert len(first) == 500 and first[0]["uuid"] == "t0001" and first[-1]["uuid"] == "t0500"
    adapter.advance_counting_cursor(
        ns, cursor_at=first[-1]["cursor_at"], cursor_uuid=first[-1]["uuid"],
        perceiver_version="v1", at=_AT)

    second = _load(adapter, ns, limit=500)
    assert len(second) == 100
    assert second[0]["uuid"] == "t0501" and second[-1]["uuid"] == "t0600"


@pytest.mark.online
def test_identical_recorded_at_tie_breaks_on_turn_id(env):
    repo, adapter, ns = env
    _seed_turns(repo, ns, [("tie-a", 7), ("tie-b", 7)])
    _scalar_cursor(adapter, ns, "tie-a", 7)

    assert _ids(_load(adapter, ns)) == ["tie-a"]

    # moving the scalar cursor onto the higher id releases the second turn, after the first
    _scalar_cursor(adapter, ns, "tie-b", 7)
    assert _ids(_load(adapter, ns)) == ["tie-a", "tie-b"]


@pytest.mark.online
def test_namespace_delete_removes_the_counting_cursor(env):
    # The cascade's watermark-reset block must take the counting cursor with the namespace, or a
    # re-created namespace would look already counted. Another namespace's cursor survives.
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    repo, adapter, ns = env
    other = f"{ns}-keep"
    for name in (ns, other):
        adapter.advance_counting_cursor(
            name, cursor_at=_iso(3), cursor_uuid="t3", perceiver_version="v1", at=_AT)
    try:
        # A group_id that differs from the namespace, so only the reset block (keyed on the
        # namespace) can remove the cursor -- not the partition's `n.group_id = $group_id` match.
        MemoryQueryRepository(repo).delete_namespace_with_scalar_cascade(
            f"{ns}-gid", ns, operation_id=f"op-{ns}")
        left = repo.execute(
            "MATCH (w:CountingConsolidationWatermark) WHERE w.group_id IN [$a, $b] "
            "RETURN w.group_id AS g", {"a": ns, "b": other})
        assert [r["g"] for r in left] == [other]
    finally:
        repo.execute(
            "MATCH (n) WHERE n.group_id = $b OR n.namespace_key = $a DETACH DELETE n",
            {"a": ns, "b": other})


@pytest.mark.online
def test_scalar_claims_for_turns_returns_only_current_scalar_lane_claims(env):
    repo, adapter, ns = env
    other = f"{ns}-other"
    rows = [
        # id, namespace, episode, ordinal, operation, current?
        ("a-abs", ns, "t1", 0, "absolute", True),
        ("a-delta", ns, "t1", 0, "delta", True),
        ("a-expire", ns, "t2", 0, "expire", True),
        ("a-occ", ns, "t1", 1, "occurrence", True),         # counting lane: excluded
        ("a-ord1-abs", ns, "t1", 1, "absolute", True),      # ordinal 1: excluded
        ("a-notcur", ns, "t1", 0, "absolute", False),       # no CURRENT edge: excluded
        ("a-other-turn", ns, "t9", 0, "absolute", True),    # turn not requested: excluded
        ("a-other-ns", other, "t1", 0, "absolute", True),   # other namespace: excluded
    ]
    try:
        repo.execute(
            """
            UNWIND $rows AS r
            CREATE (a:TypedAssertion {assertion_id: r.id, namespace: r.ns, episode_uuid: r.ep,
                                      claim_ordinal: r.ord, operation: r.op, attribute: r.id,
                                      subject_display: 'me', scope: '', unit: '',
                                      value_json: '1', stated_span: r.id, span_start: 0})
            WITH a, r WHERE r.cur
            CREATE (:TypedAssertionHead {head_key: r.id})-[:CURRENT]->(a)
            """,
            {"rows": [{"id": i, "ns": n, "ep": e, "ord": o, "op": op, "cur": c}
                      for i, n, e, o, op, c in rows]},
        )
        got = adapter.scalar_claims_for_turns(ns, ["t1", "t2"], limit=50)
        assert sorted(c["attribute"] for c in got) == ["a-abs", "a-delta", "a-expire"]
        assert {c["episode_uuid"] for c in got} == {"t1", "t2"}
        assert adapter.scalar_claims_for_turns(ns, [], limit=50) == []
    finally:
        repo.execute(
            "MATCH (n) WHERE n.namespace IN [$a, $b] DETACH DELETE n", {"a": ns, "b": other})
        repo.execute(
            "MATCH (h:TypedAssertionHead) WHERE h.head_key IN $ids DETACH DELETE h",
            {"ids": [r[0] for r in rows]})
