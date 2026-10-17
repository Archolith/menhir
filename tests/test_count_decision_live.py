"""Live proof of the unit 4R decision storage Cypher: the fenced decision write, the related-pull
and undecided reads (with a "user" fork entity present), and the match-aware flag marker.

A FakeNeo4j cannot evaluate the same-statement fence or the tenant predicate, so these run online
against the TEST instance. Every test uses a fresh namespace and removes everything it created.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.count_match import conservative_decisions, related_sets
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_LEASE = 60
_PV = "v1+m"


def _t(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cdec-{uuidlib.uuid4().hex[:10]}"
    other = f"{ns}-other"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    yield test_neo4j_repo, adapter, ns, other
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace IN $names OR n.group_id IN $names "
        "OR n.namespace_key IN $names DETACH DELETE n", {"names": [ns, other]})


def _seed(repo, ns, rows):
    """rows: dicts with id plus overrides. Ids are prefixed with `ns` so they are unique."""
    payload = []
    for i, r in enumerate(rows):
        base = {
            "id": f"{ns}:{r['id']}", "ns": ns, "ord": 1, "op": "occurrence", "subj": "self",
            "pv": _PV, "kind": "count", "obj": "model kit", "attr": "model_kit_count",
            "superseded": False, "pending": False, "status": None,
            "at": _t(60 * (i + 1)), "ep": f"turn-{i}",
        }
        base.update({k: v for k, v in r.items() if k != "id"})
        payload.append(base)
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (a:TypedAssertion {assertion_id: r.id, namespace: r.ns, group_id: r.ns,
                                  claim_ordinal: r.ord, operation: r.op, subject_uuid: r.subj,
                                  perceiver_version: r.pv, value_kind: r.kind, unit: '',
                                  attribute: r.attr, scope: '', value: 1, value_json: '1',
                                  count_object: r.obj, superseded: r.superseded,
                                  binding_pending: r.pending, valid_at: datetime(r.at),
                                  episode_uuid: r.ep, stated_span: r.id, span_start: 0})
        FOREACH (_ IN CASE WHEN r.status IS NULL THEN [] ELSE [1] END |
                 SET a.count_status = r.status)
        """,
        {"rows": payload},
    )


def _status(repo, ns, ids):
    rows = repo.execute(
        "MATCH (a:TypedAssertion) WHERE a.assertion_id IN $ids "
        "RETURN a.assertion_id AS id, a.count_status AS status, a.count_family AS family, "
        "a.count_reason AS reason, a.count_attempts AS attempts, a.count_run_id AS run_id",
        {"ids": [f"{ns}:{i}" for i in ids]},
    )
    return {r["id"].split(":", 1)[1]: dict(r) for r in rows}


def _decision(ns, rid, status="counted", **kw):
    return {"assertion_id": f"{ns}:{rid}", "status": status, "family": "model kit",
            "reason": "test", **kw}


def _record(adapter, ns, claim, run_id, decisions, pv=_PV):
    return adapter.record_count_decisions(
        ns, decisions, perceiver_version=pv, run_id=run_id,
        incarnation=claim["incarnation"], epoch=claim["epoch"])


# --------------------------------------------------------------------------- fenced decision write

@pytest.mark.online
def test_decision_write_requires_the_exact_fence_and_never_overwrites_without_redecide(env):
    repo, adapter, ns, _ = env
    _seed(repo, ns, [{"id": "a"}, {"id": "b"}])
    claim = adapter.claim_counting_namespace(ns, run_id="run-a", lease_seconds=_LEASE, now=_t(0))
    assert claim is not None

    stale = dict(claim, epoch=claim["epoch"] - 1)
    out = _record(adapter, ns, stale, "run-a", [_decision(ns, "a")])
    assert out == {"fence_ok": False, "written": 0}
    out = _record(adapter, ns, claim, "run-other", [_decision(ns, "a")])
    assert out == {"fence_ok": False, "written": 0}
    assert _status(repo, ns, ["a"])["a"]["status"] is None

    out = _record(adapter, ns, claim, "run-a",
                  [_decision(ns, "a"), _decision(ns, "b", status="uncertain")])
    assert out == {"fence_ok": True, "written": 2}
    st = _status(repo, ns, ["a", "b"])
    assert (st["a"]["status"], st["a"]["attempts"], st["a"]["run_id"]) == ("counted", 1, "run-a")
    assert st["b"]["status"] == "uncertain"

    # An existing decision is kept unless the write says redecide.
    out = _record(adapter, ns, claim, "run-a", [_decision(ns, "a", status="uncertain")])
    assert out == {"fence_ok": True, "written": 0}
    assert _status(repo, ns, ["a"])["a"]["status"] == "counted"
    out = _record(adapter, ns, claim, "run-a",
                  [_decision(ns, "b", status="counted", redecide=True)])
    assert out == {"fence_ok": True, "written": 1}
    st = _status(repo, ns, ["b"])["b"]
    assert (st["status"], st["attempts"]) == ("counted", 2)


@pytest.mark.online
def test_a_takeover_fences_the_old_owners_decision_write(env):
    repo, adapter, ns, _ = env
    _seed(repo, ns, [{"id": "a"}])
    old = adapter.claim_counting_namespace(ns, run_id="run-a", lease_seconds=_LEASE, now=_t(0))
    # The old owner stalls past its lease; a peer claims (epoch moves on).
    new = adapter.claim_counting_namespace(ns, run_id="run-b", lease_seconds=_LEASE, now=_t(600))
    assert new is not None and new["epoch"] == old["epoch"] + 1

    out = _record(adapter, ns, old, "run-a", [_decision(ns, "a")])
    assert out == {"fence_ok": False, "written": 0}
    assert _status(repo, ns, ["a"])["a"]["status"] is None
    out = _record(adapter, ns, new, "run-b", [_decision(ns, "a", status="uncertain")])
    assert out == {"fence_ok": True, "written": 1}


@pytest.mark.online
def test_decision_write_touches_only_current_bound_occurrences_of_this_namespace_and_version(env):
    repo, adapter, ns, other = env
    _seed(repo, ns, [
        {"id": "ok"},
        {"id": "scalar", "ord": 0, "op": "absolute"},
        {"id": "superseded", "superseded": True},
        {"id": "pending", "pending": True},
        {"id": "oldver", "pv": "v1"},
    ])
    _seed(repo, other, [{"id": "foreign"}])
    claim = adapter.claim_counting_namespace(ns, run_id="run-a", lease_seconds=_LEASE, now=_t(0))
    decisions = [_decision(ns, i) for i in ("ok", "scalar", "superseded", "pending", "oldver")]
    decisions.append({"assertion_id": f"{other}:foreign", "status": "counted"})
    out = _record(adapter, ns, claim, "run-a", decisions)
    assert out == {"fence_ok": True, "written": 1}
    st = _status(repo, ns, ["ok", "scalar", "superseded", "pending", "oldver"])
    assert st["ok"]["status"] == "counted"
    assert all(st[i]["status"] is None for i in ("scalar", "superseded", "pending", "oldver"))
    assert _status(repo, other, ["foreign"])["foreign"]["status"] is None


# --------------------------------------------------------------------------- related pull + fork

@pytest.mark.online
def test_related_pull_sees_every_user_occurrence_with_a_user_fork_entity_present(env):
    """Self-fork prerequisite (plan 2026-10-17): the typed lane binds the user's happenings to the
    canonical self, never to the Graphiti "user" fork; the pull keys on that subject and still sees
    every counted user occurrence while the fork exists."""
    repo, adapter, ns, other = env
    self_uuid, fork_uuid = f"{ns}-self", f"{ns}-fork"
    repo.execute(
        "CREATE (:Entity {uuid: $s, name: 'Charlie', group_id: $ns, namespace: $ns, is_self: true}) "
        "CREATE (:Entity {uuid: $f, name: 'user', group_id: $ns, namespace: $ns, is_self: false})",
        {"s": self_uuid, "f": fork_uuid, "ns": ns},
    )
    _seed(repo, ns, [
        {"id": "c1", "subj": self_uuid, "status": "counted"},
        {"id": "c2", "subj": self_uuid, "status": "counted", "obj": "museum"},
        {"id": "c-old", "subj": self_uuid, "status": "counted", "pv": "v1"},
        {"id": "u1", "subj": self_uuid, "status": "uncertain"},
        {"id": "new", "subj": self_uuid},
        {"id": "gone", "subj": self_uuid, "status": "counted", "superseded": True},
    ])
    _seed(repo, other, [{"id": "foreign", "subj": self_uuid, "status": "counted"}])

    pool = adapter.counted_occurrences_for_subjects(ns, [self_uuid, fork_uuid], limit=100)
    ids = sorted(r["assertion_id"].split(":", 1)[1] for r in pool)
    # Any counting version, current only, counted only, this namespace only.
    assert ids == ["c-old", "c1", "c2"]

    undecided = adapter.undecided_count_occurrences(ns, perceiver_version=_PV, limit=100)
    assert [r["assertion_id"].split(":", 1)[1] for r in undecided] == ["new"]

    cands = adapter.count_occurrences_by_ids(
        ns, [f"{ns}:new", f"{ns}:gone", f"{other}:foreign"], perceiver_version=_PV)
    assert [r["assertion_id"].split(":", 1)[1] for r in cands] == ["new"]
    decision = conservative_decisions(related_sets(cands, pool))[0]
    assert (decision["status"], decision["reason"]) == ("uncertain", "related_undecided")


# --------------------------------------------------------------------------- flag marker + match

@pytest.mark.online
def test_flag_marker_records_the_match_mode_and_a_match_flip_sweeps(env):
    repo, adapter, ns, _ = env
    adapter.mark_counting_flag_state(ns, enabled=True, at=_t(0))
    assert ns in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10,
                                                     match=True)
    assert ns not in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)

    adapter.mark_counting_flag_state(ns, enabled=True, at=_t(1), match=True)
    assert ns not in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10,
                                                         match=True)
    assert ns in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)

    # A marker written before 4R has no `match` property: it reads as match off.
    repo.execute("MATCH (m:CountingFlagState {namespace_key: $ns}) REMOVE m.match", {"ns": ns})
    assert ns not in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)
    assert ns in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10,
                                                     match=True)
