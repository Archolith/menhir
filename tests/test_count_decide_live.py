"""Live proof of the unit 4R decide-step Cypher (PR 2): the atomic earlier-date swap, the re-decide
selection, the brute-force cosine related pull and the stored key embedding.

Runs online against the TEST instance; every test uses a fresh namespace and removes what it made.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

_T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_PV = "v1+m"
_EV = "emb-test"


def _t(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cdcd-{uuidlib.uuid4().hex[:10]}"
    other = f"{ns}-other"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    claim = adapter.claim_counting_namespace(ns, run_id="run-a", lease_seconds=600, now=_t(0))
    yield test_neo4j_repo, adapter, ns, other, claim
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace IN $names OR n.group_id IN $names "
        "OR n.namespace_key IN $names DETACH DELETE n", {"names": [ns, other]})


def _seed(repo, ns, rows):
    payload = []
    for i, r in enumerate(rows):
        base = {"ns": ns, "subj": "self", "pv": _PV, "kind": "count", "superseded": False,
                "status": None, "target": None, "attempts": None, "run": None, "vec": None,
                "ev": None, "at": _t(60 * (i + 1))}
        base.update({k: v for k, v in r.items() if k != "id"})
        base["id"] = f"{base['ns']}:{r['id']}"
        if base["target"]:
            base["target"] = f"{base.get('target_ns') or base['ns']}:{base['target']}"
        payload.append(base)
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (a:TypedAssertion {assertion_id: r.id, namespace: r.ns, group_id: r.ns,
                                  claim_ordinal: 1, operation: 'occurrence', subject_uuid: r.subj,
                                  perceiver_version: r.pv, value_kind: r.kind, unit: '',
                                  attribute: 'model_kit_count', scope: '', value: 1,
                                  value_json: '1', count_object: 'model kit',
                                  superseded: r.superseded, binding_pending: false,
                                  valid_at: datetime(r.at), episode_uuid: r.id,
                                  stated_span: r.id, span_start: 0})
        SET a.count_status = r.status, a.count_target_id = r.target,
            a.count_attempts = r.attempts, a.count_run_id = r.run,
            a.count_key_embedding = r.vec, a.count_key_embed_version = r.ev
        """,
        {"rows": payload},
    )


def _get(repo, ns, rid):
    rows = repo.execute(
        "MATCH (a:TypedAssertion {assertion_id: $id}) RETURN a.count_status AS status, "
        "a.count_target_id AS target, a.count_reason AS reason, a.count_attempts AS attempts, "
        "a.count_key_embedding AS vec, a.count_key_embed_version AS ev",
        {"id": f"{ns}:{rid}"})
    return dict(rows[0])


def _swap(ns, cand, target, **kw):
    return {"candidate_id": f"{ns}:{cand}", "target_id": f"{ns}:{target}", "status": "repeat",
            "family": "model kit", "vote": "2/3", "reason": "swap_earlier", **kw}


def _record(adapter, ns, claim, decisions=(), swaps=(), run_id="run-a"):
    return adapter.record_count_decisions(
        ns, list(decisions), perceiver_version=_PV, run_id=run_id,
        incarnation=claim["incarnation"], epoch=claim["epoch"], swaps=list(swaps))


# --------------------------------------------------------------------------- swap

@pytest.mark.online
def test_swap_counts_the_earlier_and_demotes_the_later_in_one_write(env):
    repo, adapter, ns, _, claim = env
    _seed(repo, ns, [{"id": "early"}, {"id": "late", "status": "counted"}])
    out = _record(adapter, ns, claim, swaps=[_swap(ns, "early", "late")])
    assert out == {"fence_ok": True, "written": 0, "swapped": 1}
    early, late = _get(repo, ns, "early"), _get(repo, ns, "late")
    assert (early["status"], early["target"], early["attempts"]) == ("counted", None, 1)
    assert (late["status"], late["target"], late["reason"]) == (
        "repeat", f"{ns}:early", "swapped_later")


@pytest.mark.online
@pytest.mark.parametrize("target", [
    {"id": "late", "status": "uncertain"},           # no longer counted
    {"id": "late", "status": "counted", "subj": "x"},  # another subject
    {"id": "late", "status": "counted", "pv": "v1"},  # another counting version
    {"id": "late", "status": "counted", "superseded": True},
])
def test_swap_writes_neither_row_unless_the_target_is_a_current_counted_row_of_the_subject(
        env, target):
    repo, adapter, ns, _, claim = env
    _seed(repo, ns, [{"id": "early"}, target])
    out = _record(adapter, ns, claim, swaps=[_swap(ns, "early", "late")])
    assert out["swapped"] == 0
    assert _get(repo, ns, "early")["status"] is None
    assert _get(repo, ns, "late")["status"] == target["status"]


@pytest.mark.online
def test_swap_under_a_lost_fence_writes_nothing(env):
    repo, adapter, ns, _, claim = env
    _seed(repo, ns, [{"id": "early"}, {"id": "late", "status": "counted"}])
    stale = dict(claim, epoch=claim["epoch"] - 1)
    out = _record(adapter, ns, stale, swaps=[_swap(ns, "early", "late")])
    assert out == {"fence_ok": False, "written": 0, "swapped": 0}
    assert (_get(repo, ns, "early")["status"], _get(repo, ns, "late")["status"]) == (
        None, "counted")


# --------------------------------------------------------------------------- re-decide selection

@pytest.mark.online
def test_redecide_selects_uncertain_and_dead_target_rows_only(env):
    repo, adapter, ns, other, _ = env
    _seed(repo, other, [{"id": "foreign-target", "status": "counted"}])
    _seed(repo, ns, [
        {"id": "root", "status": "counted"},
        {"id": "uncertain", "status": "uncertain"},
        {"id": "live-repeat", "status": "repeat", "target": "root"},
        {"id": "gone", "status": "counted", "superseded": True},
        {"id": "dead-repeat", "status": "same_event", "target": "gone"},
        {"id": "missing-repeat", "status": "repeat", "target": "nope"},
        {"id": "foreign-repeat", "status": "repeat", "target": "foreign-target",
         "target_ns": other},
        {"id": "spent", "status": "uncertain", "attempts": 3},
        {"id": "this-run", "status": "uncertain", "run": "run-a"},
        {"id": "old-version", "status": "uncertain", "pv": "v1"},
        {"id": "undecided"},
    ])
    rows = adapter.redecide_count_occurrences(
        ns, perceiver_version=_PV, max_attempts=3, limit=100, exclude_run_id="run-a")
    assert [r["assertion_id"].split(":", 1)[1] for r in rows] == [
        "uncertain", "dead-repeat", "missing-repeat", "foreign-repeat"]


# --------------------------------------------------------------------------- cosine + embedding

@pytest.mark.online
def test_cosine_related_pull_is_subject_kind_version_and_threshold_scoped(env):
    repo, adapter, ns, _, _ = env
    _seed(repo, ns, [
        {"id": "near", "status": "counted", "vec": [1.0, 0.0], "ev": _EV},
        {"id": "close", "status": "counted", "vec": [0.95, 0.05], "ev": _EV},
        {"id": "far", "status": "counted", "vec": [0.0, 1.0], "ev": _EV},
        {"id": "other-ev", "status": "counted", "vec": [1.0, 0.0], "ev": "old"},
        {"id": "money", "status": "counted", "vec": [1.0, 0.0], "ev": _EV, "kind": "money"},
        {"id": "other-subj", "status": "counted", "vec": [1.0, 0.0], "ev": _EV, "subj": "x"},
        {"id": "uncertain", "status": "uncertain", "vec": [1.0, 0.0], "ev": _EV},
        {"id": "probe", "status": "counted", "vec": [1.0, 0.0], "ev": _EV},
    ])
    probe = {"id": f"{ns}:probe", "subject_uuid": "self", "value_kind": "count",
             "vector": [1.0, 0.0]}
    hits = adapter.cosine_related_counted(ns, [probe], embed_version=_EV, min_cosine=0.85,
                                          per_probe=10)
    assert [h["assertion_id"].split(":", 1)[1] for h in hits] == ["near", "close"]
    assert all(h["probe_id"] == f"{ns}:probe" for h in hits)
    assert hits[0]["cosine"] == pytest.approx(1.0)
    capped = adapter.cosine_related_counted(ns, [probe], embed_version=_EV, min_cosine=0.85,
                                            per_probe=1)
    assert [h["assertion_id"].split(":", 1)[1] for h in capped] == ["near"]


@pytest.mark.online
def test_decision_stores_the_key_embedding_and_a_later_redecide_keeps_it(env):
    repo, adapter, ns, _, claim = env
    _seed(repo, ns, [{"id": "a"}])
    first = {"assertion_id": f"{ns}:a", "status": "uncertain", "reason": "no_agreement",
             "key_embedding": [0.6, 0.8], "embed_version": _EV}
    assert _record(adapter, ns, claim, decisions=[first])["written"] == 1
    again = {"assertion_id": f"{ns}:a", "status": "counted", "reason": "decided_new",
             "redecide": True}
    assert _record(adapter, ns, claim, decisions=[again])["written"] == 1
    row = _get(repo, ns, "a")
    assert (row["status"], row["attempts"], row["ev"]) == ("counted", 2, _EV)
    assert row["vec"] == pytest.approx([0.6, 0.8])
