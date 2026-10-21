"""Live proof of the counting-lane fence, retirement, flag-marker and sweep-paging Cypher (unit 3a).

A FakeNeo4j cannot evaluate lease comparisons, MERGE-then-WHERE claims, fenced single-query writes
or the keyset paging, so these run online against the TEST instance (same fixture and marker as
test_counting_cursor_live.py). Every test uses a fresh namespace and removes everything it created.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import CountingFenceLost

_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_AT = "2026-07-20T00:00:00Z"
_LEASE = 60


def _iso(i: int) -> str:
    return (_T0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cfence-{uuidlib.uuid4().hex[:10]}"
    other = f"{ns}-other"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    yield test_neo4j_repo, adapter, ns, other
    for names in ([ns, other],):
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.namespace IN $names OR n.group_id IN $names "
            "OR n.namespace_key IN $names DETACH DELETE n", {"names": names})
    test_neo4j_repo.execute(
        "MATCH (h:TypedAssertionHead) WHERE h.head_key STARTS WITH $p DETACH DELETE h", {"p": ns})


def _claim(adapter, ns, run_id, now_s, lease=_LEASE):
    return adapter.claim_counting_namespace(
        ns, run_id=run_id, lease_seconds=lease, now=_t(now_s))


def _advance(adapter, ns, claim, run_id, *, cursor_uuid="t1", minute=1, pv="v1"):
    adapter.advance_counting_cursor(
        ns, cursor_at=_iso(minute), cursor_uuid=cursor_uuid, perceiver_version=pv, at=_AT,
        run_id=run_id, incarnation=claim["incarnation"], epoch=claim["epoch"])


def _node(repo, ns):
    rows = repo.execute(
        "MATCH (w:CountingConsolidationWatermark {group_id: $ns}) "
        "RETURN w.owner AS owner, w.incarnation AS incarnation, w.epoch AS epoch, "
        "w.cursor_at AS cursor_at, w.perceiver_version AS pv", {"ns": ns})
    return dict(rows[0]) if rows else None


def _seed_turns(repo, ns, spec):
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


def _load(adapter, ns):
    return adapter.load_next_counting_batch(
        ns, perceiver_version="v1", scalar_perceiver_version="v1", limit=500)


def _dirty(adapter, ns):
    return ns in adapter.list_counting_dirty_namespaces(
        perceiver_version="v1", scalar_perceiver_version="v1", limit=100000)


def _seed_assertions(repo, ns, rows):
    """rows: (id, namespace, episode, claim_ordinal, operation, subject, perceiver_version).
    Each gets a head with a CURRENT edge. Ids and head keys are prefixed with `ns` so they are unique
    and removable."""
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (a:TypedAssertion {assertion_id: r.id, namespace: r.ns, episode_uuid: r.ep,
                                  claim_ordinal: r.ord, operation: r.op, subject_uuid: r.subj,
                                  perceiver_version: r.pv, attribute: 'gym_visit_count',
                                  subject_display: 'me', scope: '', unit: '', value_json: '1',
                                  stated_span: r.id, span_start: 0})
        CREATE (:TypedAssertionHead {head_key: r.id})-[:CURRENT]->(a)
        """,
        {"rows": [{"id": f"{ns}:{i}", "ns": n, "ep": e, "ord": o, "op": op, "subj": s, "pv": pv}
                  for i, n, e, o, op, s, pv in rows]},
    )


def _assertion_state(repo, ns, ids):
    rows = repo.execute(
        """
        MATCH (a:TypedAssertion) WHERE a.assertion_id IN $ids
        OPTIONAL MATCH (:TypedAssertionHead)-[cr:CURRENT]->(a)
        RETURN a.assertion_id AS id, a.superseded AS superseded, a.retired_reason AS reason,
               count(cr) AS current
        """,
        {"ids": [f"{ns}:{i}" for i in ids]},
    )
    return {r["id"].split(":", 1)[1]: (r["superseded"], r["reason"], int(r["current"]))
            for r in rows}


def _retire_rows(ns, other):
    return [
        ("v0-t1", ns, "t1", 1, "occurrence", "s1", "v0"),            # the only one to retire
        ("v1-t1", ns, "t1", 1, "occurrence", "s2", "v1"),            # current version
        ("v0-t9", ns, "t9", 1, "occurrence", "s3", "v0"),            # turn not requested
        ("v0-t1-other", other, "t1", 1, "occurrence", "s4", "v0"),   # other namespace
        ("abs-v0-t1", ns, "t1", 0, "absolute", "s5", "v0"),          # scalar lane claim
    ]


_UNTOUCHED = (None, None, 1)


# --------------------------------------------------------------------------- A: exclusivity + lease

@pytest.mark.online
def test_claim_is_exclusive_until_lease_expiry_then_a_takeover_fences_the_old_owner(env):
    # Fails if a second run can claim a live lease, if takeover keeps the same epoch (the old owner
    # would still pass the fence), if takeover changes the incarnation, or if the old owner's
    # release steals ownership back.
    repo, adapter, ns, _ = env
    a = _claim(adapter, ns, "run-A", 0)
    assert a is not None
    assert _claim(adapter, ns, "run-B", 1) is None                    # lease live
    assert _node(repo, ns)["owner"] == "run-A"

    b = _claim(adapter, ns, "run-B", _LEASE + 1)                      # lease expired
    assert b is not None
    assert b["epoch"] == a["epoch"] + 1
    assert b["incarnation"] == a["incarnation"]

    with pytest.raises(CountingFenceLost):
        _advance(adapter, ns, a, "run-A")
    assert adapter.renew_counting_claim(
        ns, run_id="run-A", incarnation=a["incarnation"], epoch=a["epoch"],
        lease_seconds=_LEASE, now=_t(_LEASE + 2)) is False
    adapter.release_counting_claim(ns, run_id="run-A", incarnation=a["incarnation"])
    assert _node(repo, ns)["owner"] == "run-B"
    # the new owner is not disturbed: it can still advance and renew
    _advance(adapter, ns, b, "run-B")
    assert adapter.renew_counting_claim(
        ns, run_id="run-B", incarnation=b["incarnation"], epoch=b["epoch"],
        lease_seconds=_LEASE, now=_t(_LEASE + 3)) is True


# --------------------------------------------------------------------------- B: wipe fences

@pytest.mark.online
def test_wiping_the_watermark_fences_a_stale_writer_and_resets_the_namespace(env):
    # Fails if a writer holding a pre-wipe claim can still advance/retire after the watermark node
    # was deleted (it would resurrect a cursor for a wiped namespace), or if the next claim reuses the
    # old incarnation, or if the namespace does not read as fully unprocessed afterwards.
    repo, adapter, ns, _ = env
    _seed_turns(repo, ns, [(f"t{i}", i) for i in range(1, 4)])
    _scalar_cursor(adapter, ns, "t3", 3)

    a = _claim(adapter, ns, "run-A", 0)
    _advance(adapter, ns, a, "run-A", cursor_uuid="t3", minute=3)
    assert _load(adapter, ns) == []                                   # caught up before the wipe

    repo.execute(
        "MATCH (w:CountingConsolidationWatermark {group_id: $ns}) DETACH DELETE w", {"ns": ns})

    with pytest.raises(CountingFenceLost):
        _advance(adapter, ns, a, "run-A", cursor_uuid="t3", minute=3)
    with pytest.raises(CountingFenceLost):
        adapter.retire_prior_version_occurrences(
            ns, ["t1"], perceiver_version="v1", run_id="run-A",
            incarnation=a["incarnation"], epoch=a["epoch"])
    assert _node(repo, ns) is None                                    # the stale write created nothing

    b = _claim(adapter, ns, "run-B", 5)
    assert b is not None and b["incarnation"] != a["incarnation"]
    assert [r["uuid"] for r in _load(adapter, ns)] == ["t1", "t2", "t3"]
    # the stale writer still cannot move the new incarnation's cursor
    with pytest.raises(CountingFenceLost):
        _advance(adapter, ns, a, "run-A", cursor_uuid="t3", minute=3)
    assert [r["uuid"] for r in _load(adapter, ns)] == ["t1", "t2", "t3"]


# --------------------------------------------------------------------------- C: claim leaves cursor

@pytest.mark.online
def test_fresh_claim_does_not_stamp_a_cursor_and_namespace_stays_dirty(env):
    # Fails if claiming sets cursor_at / perceiver_version (a claim alone would make the namespace
    # look processed and hide unread turns).
    repo, adapter, ns, _ = env
    _seed_turns(repo, ns, [(f"t{i}", i) for i in range(1, 4)])
    _scalar_cursor(adapter, ns, "t3", 3)
    assert _dirty(adapter, ns)

    claim = _claim(adapter, ns, "run-A", 0)
    assert claim is not None
    node = _node(repo, ns)
    assert node["cursor_at"] is None and node["pv"] is None
    assert node["owner"] == "run-A"
    assert _dirty(adapter, ns)
    assert [r["uuid"] for r in _load(adapter, ns)] == ["t1", "t2", "t3"]


# --------------------------------------------------------------------------- D: retirement

@pytest.mark.online
def test_retire_prior_version_occurrences_retires_only_stale_version_on_requested_turns(env):
    # Fails if retirement touches another version, another turn, another namespace, a scalar-lane
    # (claim_ordinal 0) claim, forgets to drop the CURRENT edge / mark it superseded with the
    # reason, or returns the wrong subjects (their Views would not be rebuilt).
    repo, adapter, ns, other = env
    _seed_assertions(repo, ns, _retire_rows(ns, other))
    claim = _claim(adapter, ns, "run-A", 0)

    subjects = adapter.retire_prior_version_occurrences(
        ns, ["t1"], perceiver_version="v1", run_id="run-A",
        incarnation=claim["incarnation"], epoch=claim["epoch"])

    assert subjects == ["s1"]
    state = _assertion_state(
        repo, ns, ["v0-t1", "v1-t1", "v0-t9", "v0-t1-other", "abs-v0-t1"])
    assert state["v0-t1"] == (True, "counting_reversion", 0)
    for untouched in ("v1-t1", "v0-t9", "v0-t1-other", "abs-v0-t1"):
        assert state[untouched] == _UNTOUCHED, untouched


@pytest.mark.online
def test_retire_under_a_lost_fence_raises_and_changes_nothing(env):
    # Fails if the retire write is not gated on the fence in the same query (a stale run would
    # retire heads it no longer owns).
    repo, adapter, ns, other = env
    _seed_assertions(repo, ns, _retire_rows(ns, other))
    claim = _claim(adapter, ns, "run-A", 0)

    with pytest.raises(CountingFenceLost):
        adapter.retire_prior_version_occurrences(
            ns, ["t1"], perceiver_version="v1", run_id="run-A",
            incarnation=claim["incarnation"], epoch=claim["epoch"] + 1)
    with pytest.raises(CountingFenceLost):
        adapter.retire_prior_version_occurrences(
            ns, ["t1"], perceiver_version="v1", run_id="run-B",
            incarnation=claim["incarnation"], epoch=claim["epoch"])

    state = _assertion_state(
        repo, ns, ["v0-t1", "v1-t1", "v0-t9", "v0-t1-other", "abs-v0-t1"])
    assert state == {k: _UNTOUCHED for k in
                     ("v0-t1", "v1-t1", "v0-t9", "v0-t1-other", "abs-v0-t1")}


# --------------------------------------------------------------------------- E: flag marker

@pytest.mark.online
def test_flag_marker_create_only_never_flips_and_sweep_targets_follow_the_marker(env):
    # Fails if create_only overwrites an existing marker (a failed re-enable sweep would be lost),
    # if a plain mark does not flip it, or if sweep targets ignore the enabled comparison.
    repo, adapter, ns, _ = env

    def _enabled():
        rows = repo.execute(
            "MATCH (m:CountingFlagState {namespace_key: $ns}) RETURN m.enabled AS e", {"ns": ns})
        return [r["e"] for r in rows]

    def _targets(flag):
        return adapter.counting_flag_sweep_targets(enabled=flag, namespaces=[ns], limit=10)

    assert _enabled() == [] and _targets(True) == [] and _targets(False) == []

    adapter.mark_counting_flag_state(ns, enabled=True, at=_AT, create_only=True)
    assert _enabled() == [True]
    assert _targets(True) == [] and _targets(False) == [ns]

    adapter.mark_counting_flag_state(ns, enabled=False, at=_AT, create_only=True)
    assert _enabled() == [True]                                       # create_only left it alone
    assert _targets(True) == [] and _targets(False) == [ns]

    adapter.mark_counting_flag_state(ns, enabled=False, at=_AT)
    assert _enabled() == [False]                                      # a plain mark flips it
    assert _targets(True) == [ns] and _targets(False) == []


# --------------------------------------------------------------------------- F: sweep subjects

@pytest.mark.online
def test_counting_sweep_subjects_keyset_pages_occurrence_and_counted_view_subjects(env):
    # Fails if paging repeats or skips subjects, if `after` is not exclusive, if the counted View
    # source is dropped from the union, or if non-counted / superseded Views, other namespaces or
    # scalar-lane claims leak in.
    repo, adapter, ns, other = env
    _seed_assertions(repo, ns, [
        ("o1", ns, "t1", 1, "occurrence", "s1", "v1"),
        ("o2", ns, "t2", 1, "occurrence", "s2", "v1"),
        ("o2b", ns, "t3", 1, "occurrence", "s2", "v1"),               # same subject twice -> once
        ("o3", ns, "t4", 1, "occurrence", "s3", "v1"),
        ("o-other", other, "t1", 1, "occurrence", "s0", "v1"),        # other namespace
        ("o-scalar", ns, "t1", 0, "occurrence", "s9", "v1"),          # not the counting lane
    ])
    repo.execute(
        """
        UNWIND $views AS v
        CREATE (:Entity {view_kind: 'scalar_state', group_id: $ns, view_subject_uuid: v.subj,
                         scalar_basis: v.basis, view_current: v.cur, name: 'sweep-view'})
        """,
        {"ns": ns, "views": [
            {"subj": "s4", "basis": "counted", "cur": True},          # returned
            {"subj": "s5", "basis": "stated", "cur": True},           # not counted
            {"subj": "s6", "basis": "counted", "cur": False},         # superseded
        ]},
    )

    first = adapter.counting_sweep_subjects(ns, limit=2)
    second = adapter.counting_sweep_subjects(ns, limit=2, after="s2")
    third = adapter.counting_sweep_subjects(ns, limit=2, after="s4")
    assert first == ["s1", "s2"]
    assert second == ["s3", "s4"]
    assert third == []
    assert adapter.counting_sweep_subjects(ns, limit=50) == ["s1", "s2", "s3", "s4"]
