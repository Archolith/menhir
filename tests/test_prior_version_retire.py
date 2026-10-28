"""#170 decision 1: scalar retires prior-version readings once a turn is re-read and
stored at the new version (counting's rule B), and a lower-version writer cannot make a reading
current on a turn a higher version has read.

Unit tests drive the scalar runner with fakes. Online tests run the Cypher against the TEST instance;
each uses a fresh namespace and turn ids and removes what it made.
"""

from __future__ import annotations

import os
import threading
import uuid as uuidlib
from typing import Any

import pytest

from menhir.domain.typed_assertion import TypedAssertion, perceiver_rank
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.reversion_repository import PerceptionReversionRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.services.scalar_consolidation import (
    ScalarConsolidationConfig,
    run_scalar_consolidation,
)

_OLD, _NEW, _NEWER = "v1.0", "v1.1", "v1.2"
_NOW = "2026-07-01T00:00:00+00:00"
_REVERSION_METHODS = (
    "scalar_prior_version_rows", "scalar_current_version_spans", "scalar_owed_subjects",
    "retire_scalar_prior_version")


@pytest.mark.unit
def test_the_production_adapter_has_every_retire_query():
    # The runners retire only when the graph offers these (test fakes do not). Fails if the
    # production adapter loses one, which would switch retirement off silently.
    for name in _REVERSION_METHODS:
        assert callable(getattr(MemoryGraphAdapter, name, None)), name
    assert perceiver_rank(_OLD) < perceiver_rank(_NEW) < perceiver_rank(_NEWER)


# ------------------------------------------------------------------------- scalar runner (unit)


class _Ep:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.content = "x"


class _ScalarGraph:
    """Scalar runner fake: one page, the four retire queries, recorded calls."""

    def __init__(self, *, prior: list[dict], owed: list[str] | None = None,
                 stored: list[dict] | None = None, retired_subjects: list[str] | None = None):
        self.pages = [[{"uuid": "t1", "valid_at": _NOW, "cursor_at": _NOW}]]
        self.prior, self.owed, self.stored = prior, list(owed or []), list(stored or [])
        self.retired_subjects = list(retired_subjects or [])
        self.retire_calls: list[dict] = []
        self.advances: list[str] = []
        self.marked: list[list[str]] = []
        self.state = _State()

    def scalar_state_service(self, *, scalar_history_enabled=False):
        return self.state

    def load_next_scalar_batch(self, namespace, *, perceiver_version, limit):
        return self.pages.pop(0) if self.pages else []

    def advance_scalar_cursor(self, namespace, *, cursor_at, cursor_uuid, perceiver_version, at):
        self.advances.append(cursor_uuid)

    def retire_counters_superseded_by_scalar(self, *, namespace):
        return 0

    def evidence_exists(self):
        return True

    def scalar_prior_version_rows(self, namespace, turn_ids, *, perceiver_rank):
        return list(self.prior)

    def scalar_owed_subjects(self, namespace, turn_ids, *, perceiver_rank):
        return list(self.owed)

    def scalar_current_version_spans(self, namespace, turn_ids, *, perceiver_version,
                                     include_ids=None):
        return list(self.stored)

    def retire_scalar_prior_version(self, namespace, turn_ids, *, perceiver_rank,
                                    perceiver_version, keep_ids=None):
        self.retire_calls.append({"turns": list(turn_ids), "rank": perceiver_rank,
                                  "keep": list(keep_ids or [])})
        return {"retired": len(self.retired_subjects), "subjects": list(self.retired_subjects)}

    def mark_projection_complete(self, ids):
        self.marked.append(list(ids))
        return len(ids)


class _State:
    def __init__(self) -> None:
        self.rebuilt: list[str] = []
        self.fail: set[str] = set()

    def rebuild_scalar_state(self, subject, namespace=None, **_):
        self.rebuilt.append(subject)
        return {"complete": subject not in self.fail}

    def activate_due_assertions(self, **_):
        return {"claimed": 0, "repaired": [], "failed": []}

    def repair_incomplete_reconciliations(self, **_):
        return {"repaired": 0}

    def repair_orphaned_assertions(self, **_):
        return {"repaired": [], "unresolved": []}


def _scalar_run(monkeypatch, graph, perception: dict, *, pv: str = _NEW) -> dict:
    import menhir.services.typed_scalar_perception as tsp

    calls: list[dict] = []

    class _Service:
        def __init__(self, *a, **k):
            pass

        def ensure_activated(self):
            pass

        def perceive_and_persist(self, episodes, llm, **kw):
            calls.append(kw)
            return dict(perception)

        def repair_pending_bindings(self, **_):
            return {"repaired": 0, "still_pending": 0}

    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Service)

    class _Llm:
        calls = 0

    out = run_scalar_consolidation(
        graph, scalar_targets=["ns"], namespaces=None, counting_llm=_Llm(),
        build_episodes=lambda rows: [_Ep(str(r["uuid"])) for r in rows], k=3, threshold=2 / 3,
        call_budget=None, embed=None,
        config=ScalarConsolidationConfig(perceiver_version=pv, batch_size=10))
    out["_persist_kwargs"] = calls
    return out


def _prior(aid="old-1", subject="s-old", start=0, end=5):
    return {"assertion_id": aid, "episode_uuid": "t1", "span_start": start, "span_end": end,
            "subject_uuid": subject, "binding_pending": False}


@pytest.mark.unit
def test_scalar_an_abstaining_re_read_retires_the_old_reading_and_rebuilds_once(monkeypatch):
    # Every sample read t1 in full and proposed nothing: the old reading is retired, its subject
    # rebuilt after the retire, then the cursor moves. Fails if the old reading is kept, the
    # persist is not deferred (a View would briefly hold both), or the rebuild is skipped.
    graph = _ScalarGraph(prior=[_prior()], retired_subjects=["s-old"])
    out = _scalar_run(monkeypatch, graph, {
        "results": [], "spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}})
    assert out["_persist_kwargs"][0].get("defer_rebuild") is True
    assert graph.retire_calls == [{"turns": ["t1"], "rank": perceiver_rank(_NEW), "keep": []}]
    assert graph.state.rebuilt == ["s-old"]
    assert graph.advances == ["t1"]
    assert out["scalar_prior_version_retired"] == 1 and out["scalar_prior_version_carried"] == 0


@pytest.mark.unit
def test_scalar_a_gate_veto_keeps_the_old_reading(monkeypatch):
    # The samples proposed something over the old span but the gate stored nothing: carried.
    graph = _ScalarGraph(prior=[_prior()])
    out = _scalar_run(monkeypatch, graph, {"results": [], "spans": {
        "clean_turns": ["t1"], "proposed": [("t1", 0, 5)], "committed": []}})
    assert graph.retire_calls[0]["keep"] == ["old-1"]
    assert out["scalar_prior_version_carried"] == 1


@pytest.mark.unit
def test_scalar_an_unread_turn_keeps_the_old_reading(monkeypatch):
    # A sample could not read t1 in full: no evidence the reading is gone, so it is carried.
    graph = _ScalarGraph(prior=[_prior()])
    _scalar_run(monkeypatch, graph, {"results": [], "spans": {
        "clean_turns": [], "proposed": [], "committed": []}})
    assert graph.retire_calls[0]["keep"] == ["old-1"]


@pytest.mark.unit
def test_scalar_new_rows_are_marked_complete_only_after_the_rebuild(monkeypatch):
    graph = _ScalarGraph(prior=[_prior()], stored=[
        {"assertion_id": "new-1", "episode_uuid": "t1", "span_start": 0, "span_end": 5}],
        retired_subjects=[])
    _scalar_run(monkeypatch, graph, {
        "results": [{"bound": True, "assertion_id": "new-1", "subject_uuid": "s-new"}],
        "spans": {"clean_turns": ["t1"], "proposed": [("t1", 0, 5)], "committed": []}})
    assert graph.retire_calls[0]["keep"] == []      # replaced by the stored new reading
    assert graph.state.rebuilt == ["s-new"] and graph.marked == [["new-1"]]


@pytest.mark.unit
def test_scalar_a_failed_rebuild_of_a_retired_subject_holds_the_cursor(monkeypatch):
    # Crash-equivalent: the View of a retired reading did not rebuild. Fails if the cursor moves
    # past the turn (nothing would rebuild it) -- the next run reads it as owed.
    graph = _ScalarGraph(prior=[_prior()], retired_subjects=["s-old"])
    graph.state.fail = {"s-old"}
    _scalar_run(monkeypatch, graph, {
        "results": [], "spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}})
    assert graph.advances == []

    again = _ScalarGraph(prior=[], owed=["s-old"])
    _scalar_run(monkeypatch, again, {"results": []})
    assert again.state.rebuilt == ["s-old"] and again.advances == ["t1"]


@pytest.mark.unit
def test_scalar_a_non_semver_version_retires_nothing(monkeypatch):
    # Rank 0 never supersedes, so it must never retire either.
    graph = _ScalarGraph(prior=[_prior()])
    out = _scalar_run(monkeypatch, graph, {"results": []}, pv="cv1")
    assert graph.retire_calls == [] and "defer_rebuild" not in out["_persist_kwargs"][0]
    assert graph.advances == ["t1"]


# -------------------------------------------------------------------------------- online


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"d1-{uuidlib.uuid4().hex[:10]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    test_neo4j_repo.execute(
        "UNWIND $s AS s CREATE (:Entity {uuid: s, namespace: $ns, group_id: $ns}) "
        "WITH count(*) AS _ UNWIND $t AS t "
        "CREATE (:TurnEvidence {turn_id: t, namespace: $ns, group_id: $ns})",
        {"s": [f"{ns}:s1", f"{ns}:s2"], "t": [f"{ns}:t1", f"{ns}:t2"], "ns": ns})
    yield test_neo4j_repo, adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace IN [$ns, toLower($ns)] OR n.group_id = $ns "
        "OR n.turn_id STARTS WITH $ns OR n.source_key IN $keys DETACH DELETE n",
        {"ns": ns, "keys": []})
    test_neo4j_repo.execute(
        "MATCH (h) WHERE h:TypedAssertionHead "
        "AND NOT (h)-[:HAS_VERSION]->() DETACH DELETE h")


def _scalar(ns, turn, start, pv, *, value=1, subject="s1", tier="agent"):
    return TypedAssertion(
        subject_uuid=f"{ns}:{subject}", subject_display="me", attribute="coin_count", scope="",
        value_kind="count", unit="", operation="absolute", value=value,
        stated_span="x" * 5, span_start=start, span_end=start + 5, claim_ordinal=0,
        episode_uuid=f"{ns}:{turn}", valid_at=_NOW, learned_at=_NOW, evidence_tier=tier,
        perceiver_version=pv, namespace=ns)


def _state(repo, label, source_key):
    """[(perceiver_version, current?, retired_reason)] for every version on a head."""
    rows = repo.execute(
        f"MATCH (h:{label}Head {{source_key: $sk}})-[:HAS_VERSION]->(a:{label}) "
        "OPTIONAL MATCH (h)-[cr:CURRENT]->(a) "
        "RETURN a.perceiver_version AS pv, count(cr) AS cur, a.retired_reason AS why, "
        "a.superseded AS sup ORDER BY pv",
        {"sk": source_key})
    return [(r["pv"], int(r["cur"]), r["why"]) for r in rows]


def _rank(pv):
    return perceiver_rank(pv)


def _retire_scalar(adapter, ns, turns, pv, keep=None):
    return adapter.retire_scalar_prior_version(
        ns, [f"{ns}:{t}" for t in turns], perceiver_rank=_rank(pv), perceiver_version=pv,
        keep_ids=keep)


@pytest.mark.online
def test_scalar_retire_takes_only_current_lower_rank_agent_readings_on_the_turns(env):
    # Fails if the retire touches the version in effect, a higher version, a kept row, a
    # user-confirmed reading, another turn, or a counting (ordinal 1) row.
    repo, adapter, ns = env
    old = _scalar(ns, "t1", 0, _OLD)
    kept = _scalar(ns, "t1", 10, _OLD, value=2)
    same = _scalar(ns, "t1", 20, _NEW, value=3)
    user = _scalar(ns, "t1", 30, _OLD, value=4, tier="user")
    other_turn = _scalar(ns, "t2", 0, _OLD, value=5)
    ids = {k: str(adapter.record_typed_assertion(a)["assertion_id"])
           for k, a in {"old": old, "kept": kept, "same": same, "user": user,
                        "other": other_turn}.items()}

    prior = adapter.scalar_prior_version_rows(ns, [f"{ns}:t1"], perceiver_rank=_rank(_NEW))
    assert sorted(r["assertion_id"] for r in prior) == sorted([ids["old"], ids["kept"]])

    out = _retire_scalar(adapter, ns, ["t1"], _NEW, keep=[ids["kept"]])
    assert out == {"retired": 1, "subjects": [f"{ns}:s1"]}
    assert _state(repo, "TypedAssertion", old.source_key) == [(_OLD, 0, "scalar_reversion")]
    for a in (kept, same, user, other_turn):
        assert _state(repo, "TypedAssertion", a.source_key)[0][1] == 1
    assert adapter.scalar_owed_subjects(ns, [f"{ns}:t1"], perceiver_rank=_rank(_NEW)) == [
        f"{ns}:s1"]


@pytest.mark.online
def test_a_stale_lower_version_writer_cannot_make_a_reading_current_on_a_re_read_turn(env):
    # The resurrection counterexample: v1.1 re-read t1 and retired v1.0's reading. A slow v1.0
    # runner then writes (the same reading again, and a new span). Fails if either becomes
    # current. Equal or higher versions, user-tier writes and other turns are unaffected.
    repo, adapter, ns = env
    old = _scalar(ns, "t1", 0, _OLD)
    adapter.record_typed_assertion(old)
    _retire_scalar(adapter, ns, ["t1"], _NEW)

    adapter.record_typed_assertion(old)                                  # exact replay
    late = _scalar(ns, "t1", 40, _OLD, value=9)
    adapter.record_typed_assertion(late)                                 # a fresh head
    assert _state(repo, "TypedAssertion", old.source_key) == [(_OLD, 0, "scalar_reversion")]
    assert _state(repo, "TypedAssertion", late.source_key) == [(_OLD, 0, None)]

    for a in (_scalar(ns, "t1", 50, _NEW, value=7), _scalar(ns, "t1", 60, _OLD, tier="user"),
              _scalar(ns, "t2", 0, _OLD)):
        adapter.record_typed_assertion(a)
        assert _state(repo, "TypedAssertion", a.source_key)[0][1] == 1, a


@pytest.mark.online
def test_mixed_versions_converge_on_the_highest_and_a_downgrade_retires_nothing(env):
    # v1.2 re-reads t1; a v1.1 runner then re-reads it too. Fails if v1.1 lowers the turn's
    # marker (a v1.0 writer could become current again), if v1.1 retires the v1.2 reading, or if
    # v1.1's own late reading becomes current over v1.2's.
    repo, adapter, ns = env
    adapter.record_typed_assertion(_scalar(ns, "t1", 0, _OLD))
    newer = _scalar(ns, "t1", 10, _NEWER, value=2)
    adapter.record_typed_assertion(newer)
    _retire_scalar(adapter, ns, ["t1"], _NEWER)

    assert _retire_scalar(adapter, ns, ["t1"], _NEW)["retired"] == 0     # downgrade: nothing
    assert _state(repo, "TypedAssertion", newer.source_key) == [(_NEWER, 1, None)]
    mid = _scalar(ns, "t1", 20, _NEW, value=3)
    adapter.record_typed_assertion(mid)
    assert _state(repo, "TypedAssertion", mid.source_key) == [(_NEW, 0, None)]
    marker = repo.execute("MATCH (m:TypedReadMarker {marker_key: $k}) RETURN m.rank AS r",
                          {"k": f"scalar:{ns}:t1"})
    assert [r["r"] for r in marker] == [_rank(_NEWER)]


@pytest.mark.online
def test_a_new_version_reading_on_the_same_head_still_supersedes_in_place(env):
    # The retire must leave the strict-rank supersede alone: the v1.1 reading of the same span is
    # already current, and the retire neither retires it nor revives v1.0.
    repo, adapter, ns = env
    old = _scalar(ns, "t1", 0, _OLD)
    adapter.record_typed_assertion(old)
    adapter.record_typed_assertion(_scalar(ns, "t1", 0, _NEW, value=2))
    assert _retire_scalar(adapter, ns, ["t1"], _NEW)["retired"] == 0
    assert _state(repo, "TypedAssertion", old.source_key) == [(_OLD, 0, None), (_NEW, 1, None)]


# ---- the two orders of a concurrent record and retire (the marker lock) ---------------------


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None, **_):
        self.calls.append((query, dict(params or {})))
        return []


def _driver():
    neo4j = pytest.importorskip("neo4j")
    return neo4j.GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")))


def _cypher_of(fn) -> tuple[str, dict]:
    cap = _Capture()
    fn(cap)
    return cap.calls[-1]


def _in_thread(fn) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run():
        try:
            box["out"] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced by the test
            box["err"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _existing_marker(adapter, ns, existing):
    # An earlier write on the turn creates its marker (rank -1): the lock must not depend on the
    # write being the one that creates it.
    if existing:
        adapter.record_typed_assertion(_scalar(ns, "t1", 100, _OLD, value=8, subject="s2"))


@pytest.mark.online
@pytest.mark.parametrize("existing", [False, True], ids=["new-marker", "existing-marker"])
def test_a_record_in_flight_when_the_retire_runs_is_retired(env, existing):
    # v1.0's write has run but not committed when v1.1's retire starts. Fails if the retire does
    # not wait for it (the write would commit current after the retire, and stay current).
    repo, adapter, ns = env
    _existing_marker(adapter, ns, existing)
    late = _scalar(ns, "t1", 0, _OLD)
    q, p = _cypher_of(lambda cap: TypedAssertionRepository(cap).record_assertion(late))
    driver = _driver()
    try:
        with driver.session() as s:
            tx = s.begin_transaction()
            tx.run(q, p).consume()
            t, box = _in_thread(lambda: _retire_scalar(adapter, ns, ["t1"], _NEW))
            t.join(1.5)
            assert t.is_alive(), "the retire must wait on the turn marker the write holds"
            tx.commit()
        t.join(30)
        assert box.get("out", {}).get("retired") == (2 if existing else 1), box
        assert _state(repo, "TypedAssertion", late.source_key) == [(_OLD, 0, "scalar_reversion")]
    finally:
        driver.close()


@pytest.mark.online
@pytest.mark.parametrize("existing", [False, True], ids=["new-marker", "existing-marker"])
def test_a_record_that_waits_on_the_retire_sees_the_new_marker(env, existing):
    # The other order: v1.1's retire holds the marker uncommitted; v1.0's write waits, then must
    # read the raised marker. Fails if it read the marker before the lock (current again).
    repo, adapter, ns = env
    _existing_marker(adapter, ns, existing)
    late = _scalar(ns, "t1", 0, _OLD)
    q, p = _cypher_of(lambda cap: PerceptionReversionRepository(cap).retire_scalar_prior_version(
        ns, [f"{ns}:t1"], perceiver_rank=_rank(_NEW), perceiver_version=_NEW))
    driver = _driver()
    try:
        with driver.session() as s:
            tx = s.begin_transaction()
            tx.run(q, p).consume()
            t, box = _in_thread(lambda: adapter.record_typed_assertion(late))
            t.join(1.5)
            assert t.is_alive(), "the write must wait on the marker the retire holds"
            tx.commit()
        t.join(30)
        assert "err" not in box, box
        assert _state(repo, "TypedAssertion", late.source_key) == [(_OLD, 0, None)]
    finally:
        driver.close()


@pytest.mark.online
def test_erasing_the_evidence_deletes_its_read_markers(env):
    # Markers are keyed by turn id; erasure must not leave them behind (#170 item 5).
    repo, adapter, ns = env
    adapter.record_typed_assertion(_scalar(ns, "t1", 0, _OLD))
    # A marker left by the archived event lane (same shape it wrote); erasure must still remove it.
    repo.execute(
        "CREATE (:TypedReadMarker {marker_key: 'event:' + $tid, turn_id: $tid, rank: 0, "
        "perceiver_version: $pv, read_at: datetime()})", {"tid": f"{ns}:t2", "pv": _OLD})
    _retire_scalar(adapter, ns, ["t1"], _NEW)

    def markers():
        return repo.execute(
            "MATCH (m:TypedReadMarker) WHERE m.turn_id IN $t RETURN m.marker_key AS k ORDER BY k",
            {"t": [f"{ns}:t1", f"{ns}:t2"]})

    assert [r["k"] for r in markers()] == [f"event:{ns}:t2", f"scalar:{ns}:t1"]
    adapter.purge_turn_evidence(ns)
    assert markers() == []
