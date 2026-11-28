"""Live: the report-only retire category judge against a real graph (retire plan, step 3).

Safety properties under test:
  * nomination applies the same guards `retire_memory` refuses on, so a node that is retired
    (stored or derived), a View, structural, CANDIDATE, flagged, retention-protected, erased or in
    flight is never nominated, and a node with no signal is never nominated;
  * each signal (idle, target_date_passed, secret_hit, stale_anchor, scalar_replaced) nominates;
  * nomination is namespace scoped;
  * nominate + gather_evidence + judge + build_report write NOTHING: every node and relationship
    property (including last_accessed) is byte-identical afterwards, counts are unchanged and
    pending_actions is empty.

Every guard case is built twice in the same namespace, once with the guard property off (the
control, which MUST be nominated) and once with it on (which must not), so an empty result can never
come from a node that was not idle to begin with. Online: needs the test Neo4j (``--run-online``).
No LLM is ever called; chat is a stub.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import retire_judge as svc

pytestmark = pytest.mark.online

OLD_DAYS = 400
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _norm(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # neo4j temporal types


class _Env:
    """One tagged namespace on the test instance, with seeding helpers."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.tag = f"test-retire-judge-{uuidlib.uuid4().hex}"
        self.ns = f"{self.tag}-ns"

    # -- seeding
    def entity(self, label: str, *, age_days: int = OLD_DAYS, accessed_days: int | None = None,
               ns: str | None = None, **props: Any) -> str:
        """An :Entity created ``age_days`` ago (idle by default: old, edge_count 0)."""
        ns = ns or self.ns
        uid = f"{self.tag}-{label}-{uuidlib.uuid4().hex[:8]}"
        node = {"uuid": uid, "name": label, "group_id": ns, "namespace": ns,
                "test_tag": self.tag, "scope": "PERSISTENT", "freshness": "ACTIVE",
                "content": f"{label} content", "edge_count": 0, **props}
        self.repo.execute(
            "CREATE (n:Entity) SET n = $p, n.created_at = datetime() - duration({days: $age}) "
            "FOREACH (_ IN CASE WHEN $acc IS NULL THEN [] ELSE [1] END | "
            "SET n.last_accessed = datetime() - duration({days: $acc}))",
            {"p": node, "age": age_days, "acc": accessed_days})
        return uid

    def pair(self, label: str, *, state: str = "READY", queue: dict[str, Any] | None = None,
             twin: dict[str, Any] | None = None, age_days: int = OLD_DAYS) -> tuple[str, str]:
        """A Menhir queue :Episodic and the Graphiti episode it resolves to (queue, twin)."""
        suffix = uuidlib.uuid4().hex[:8]
        q_uuid, g_uuid = f"{self.tag}-{label}-q-{suffix}", f"{self.tag}-{label}-g-{suffix}"
        g = {"uuid": g_uuid, "group_id": self.ns, "content": f"{label} text", "source": "message",
             "test_tag": self.tag, "name": f"{label}-episode", **(twin or {})}
        q = {"uuid": q_uuid, "namespace": self.ns, "resolved_episode_uuid": g_uuid,
             "processing_state": state, "content": f"{label} text", "source": "message",
             "test_tag": self.tag, **(queue or {})}
        self.repo.execute(
            "CREATE (g:Episodic) SET g = $g, g.created_at = datetime() - duration({days: $age}) "
            "CREATE (q:Episodic) SET q = $q, q.created_at = datetime() - duration({days: $age})",
            {"g": g, "q": q, "age": age_days})
        return q_uuid, g_uuid

    def fact(self, a: str, b: str, text: str = "A relates to B") -> None:
        self.repo.execute(
            "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
            "CREATE (a)-[r:RELATES_TO {uuid:$f, fact:$text, group_id:$ns, test_tag:$tag}]->(b) "
            "SET r.valid_at = datetime(), r.created_at = datetime()",
            {"a": a, "b": b, "f": f"{self.tag}-fact-{uuidlib.uuid4().hex[:8]}", "text": text,
             "ns": self.ns, "tag": self.tag})

    def set(self, uid: str, assignments: str, **params: Any) -> None:
        self.repo.execute(
            f"MATCH (n) WHERE n.uuid = $u AND (n:Entity OR n:Episodic) SET {assignments}",
            {"u": uid, **params})

    def nominated(self, ns: str | None = None, **kw: Any) -> dict[str, dict[str, Any]]:
        kw.setdefault("limit", 1000)
        rows = svc.nominate(self.repo.execute, ns or self.ns, **kw)
        return {r["uuid"]: r for r in rows}

    def delete_all(self) -> None:
        self.repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", {"t": self.tag})


@pytest.fixture
def jenv(test_neo4j_repo):
    e = _Env(test_neo4j_repo)
    yield e
    e.delete_all()


# ============================================================================ 1. guards

def _flag(on: bool, **props: Any) -> dict[str, Any]:
    return props if on else {}


def _g_retired(env: _Env, on: bool) -> list[str]:
    return [env.entity("retired", **_flag(on, memory_retired=True))]


def _g_view(env: _Env, on: bool) -> list[str]:
    return [env.entity("view", **_flag(on, is_view=True, view_kind="scalar_state"))]


def _g_quantstate(env: _Env, on: bool) -> list[str]:
    return [env.entity("counter", **_flag(on, is_quantstate=True))]


def _g_structural(env: _Env, on: bool) -> list[str]:
    return [env.entity("struct", **_flag(on, structure_role="file"))]


def _g_candidate_scope(env: _Env, on: bool) -> list[str]:
    return [env.entity("cand", scope="CANDIDATE" if on else "PERSISTENT")]


def _g_flagged(env: _Env, on: bool) -> list[str]:
    return [env.entity("flag", **_flag(on, user_flagged=True))]


def _g_retention_protected(env: _Env, on: bool) -> list[str]:
    e = env.entity("kept")
    if on:
        env.repo.execute(
            "MATCH (e:Entity {uuid:$e}) "
            "CREATE (x:Episodic {uuid:$x, group_id:$ns, namespace:$ns, test_tag:$tag, "
            "user_flagged:true, processing_state:'READY'}) CREATE (x)-[:RETENTION_SOURCE]->(e)",
            {"e": e, "x": f"{env.tag}-retention-source-{uuidlib.uuid4().hex[:6]}", "ns": env.ns,
             "tag": env.tag})
    return [e]


def _g_entity_erased_at(env: _Env, on: bool) -> list[str]:
    return [env.entity("erased", **_flag(on, erased_at="2026-11-01T00:00:00Z"))]


def _g_entity_deleted_by_user(env: _Env, on: bool) -> list[str]:
    return [env.entity("dbu", **_flag(on, processing_substage="deleted_by_user"))]


def _g_entity_erase_marker(env: _Env, on: bool) -> list[str]:
    e = env.entity("marked")
    if on:
        env.repo.execute("CREATE (:ErasureMarker {subject_uuid: $u, test_tag: $tag})",
                         {"u": e, "tag": env.tag})
    return [e]


def _g_queue_erased_at(env: _Env, on: bool) -> list[str]:
    return list(env.pair("erq", queue=_flag(on, erased_at="2026-11-01T00:00:00Z")))


def _g_queue_deleted_by_user(env: _Env, on: bool) -> list[str]:
    return list(env.pair("dbuq", queue=_flag(on, processing_substage="deleted_by_user")))


def _g_twin_erase_marker(env: _Env, on: bool) -> list[str]:
    q, g = env.pair("erm")
    if on:
        env.repo.execute("CREATE (:ErasureMarker {subject_uuid: $g, test_tag: $tag})",
                         {"g": g, "tag": env.tag})
    return [q, g]


def _g_pending(env: _Env, on: bool) -> list[str]:
    return list(env.pair("pend", state="PENDING" if on else "READY"))


def _g_enriching(env: _Env, on: bool) -> list[str]:
    return list(env.pair("enr", state="ENRICHING" if on else "READY"))


def _g_twin_flagged(env: _Env, on: bool) -> list[str]:
    return list(env.pair("tfl", twin=_flag(on, user_flagged=True)))


def _g_queue_flagged(env: _Env, on: bool) -> list[str]:
    return list(env.pair("qfl", queue=_flag(on, user_flagged=True)))


def _g_twin_enriching(env: _Env, on: bool) -> list[str]:
    return list(env.pair("ten", twin={"processing_state": "ENRICHING" if on else "READY"}))


def _g_twin_candidate(env: _Env, on: bool) -> list[str]:
    return list(env.pair("tca", twin=_flag(on, scope="CANDIDATE")))


@pytest.mark.parametrize("build", [
    _g_retired, _g_view, _g_quantstate, _g_structural, _g_candidate_scope, _g_flagged,
    _g_retention_protected, _g_entity_erased_at, _g_entity_deleted_by_user,
    _g_entity_erase_marker, _g_queue_erased_at, _g_queue_deleted_by_user, _g_twin_erase_marker,
    _g_pending, _g_enriching, _g_twin_flagged, _g_queue_flagged, _g_twin_enriching,
    _g_twin_candidate,
], ids=lambda f: f.__name__.removeprefix("_g_"))
def test_each_retire_guard_excludes_an_otherwise_idle_node(jenv, build):
    control = build(jenv, False)
    guarded = build(jenv, True)

    found = jenv.nominated()

    # The control, identical but for the guard, IS nominated (so idleness is not the reason).
    assert set(control) & set(found), f"premise: the unguarded twin must be nominated: {control}"
    assert not set(guarded) & set(found), f"a guarded node was nominated: {guarded}"
    for uid in set(control) & set(found):
        assert "idle" in found[uid]["signals"]


def test_derived_retired_entity_is_excluded_once_every_mentioning_episode_is_retired(jenv):
    ent = jenv.entity("derived")
    ep = f"{jenv.tag}-mentions-episode"
    jenv.repo.execute(
        "MATCH (a:Entity {uuid:$a}) "
        "CREATE (e:Episodic {uuid:$e, group_id:$ns, namespace:$ns, test_tag:$tag, "
        "content:'mentions it', processing_state:'COMPLETED'}) CREATE (e)-[:MENTIONS]->(a)",
        {"a": ent, "e": ep, "ns": jenv.ns, "tag": jenv.tag})

    assert ent in jenv.nominated(), "premise: nominated while its source episode is live"

    jenv.set(ep, "n.memory_retired = true, n.memory_retired_at = datetime()")
    assert jenv.repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.memory_retired AS r", {"u": ent})[0]["r"] is None
    assert ent not in jenv.nominated(), "derived-retired entity was nominated"

    # A second, live source brings it back: derived retirement needs EVERY source retired.
    live = f"{jenv.tag}-live-episode"
    jenv.repo.execute(
        "MATCH (a:Entity {uuid:$a}) "
        "CREATE (e:Episodic {uuid:$e, group_id:$ns, namespace:$ns, test_tag:$tag, "
        "content:'second source', processing_state:'COMPLETED'}) CREATE (e)-[:MENTIONS]->(a)",
        {"a": ent, "e": live, "ns": jenv.ns, "tag": jenv.tag})
    assert ent in jenv.nominated()


# ============================================================================ 2. episode pairs

def test_episode_pair_is_nominated_once(jenv):
    q, g = jenv.pair("ep")
    found = jenv.nominated()
    assert len({q, g} & set(found)) == 1, f"pair must be represented exactly once: {set(found)}"
    only = found[next(iter({q, g} & set(found)))]
    assert only["label"] == "Episodic" and "idle" in only["signals"]


def test_pair_with_flagged_or_enriching_twin_nominates_neither_half(jenv):
    flagged = jenv.pair("tf", twin={"user_flagged": True})
    enriching = jenv.pair("te", twin={"processing_state": "ENRICHING"})
    control = jenv.pair("ok")
    found = jenv.nominated()
    assert len(set(control) & set(found)) == 1  # premise
    assert not (set(flagged) | set(enriching)) & set(found)


# ============================================================================ 3. signals

@pytest.mark.parametrize("edge_count,expect_idle", [(0, True), (2, True), (3, False), (9, False)])
def test_idle_signal_needs_old_recency_and_fewer_than_max_edges(jenv, edge_count, expect_idle):
    uid = jenv.entity("idle", edge_count=edge_count)
    found = jenv.nominated(idle_days=90, max_edge_count=3)
    assert (uid in found) is expect_idle
    if expect_idle:
        assert found[uid]["signals"] == ["idle"]


def test_recent_node_is_not_idle_and_a_recent_access_beats_an_old_creation(jenv):
    fresh = jenv.entity("fresh", age_days=2)
    touched = jenv.entity("touched", age_days=OLD_DAYS, accessed_days=3)
    stale_access = jenv.entity("stale-access", age_days=2, accessed_days=OLD_DAYS)
    found = jenv.nominated()
    assert fresh not in found
    assert touched not in found, "recency is last_accessed first, so a recent access means not idle"
    assert stale_access in found and found[stale_access]["signals"] == ["idle"]


def test_idle_days_threshold_is_honoured(jenv):
    uid = jenv.entity("mid", age_days=100)
    assert uid in jenv.nominated(idle_days=90)
    assert uid not in jenv.nominated(idle_days=200)


def test_node_with_no_signal_is_not_nominated(jenv):
    recent = jenv.entity("recent", age_days=1)
    busy = jenv.entity("busy", edge_count=10)  # old but well connected
    future = jenv.entity("future", age_days=1, target_date="2999-01-01")
    plain_pair = jenv.pair("recent-pair", age_days=1)
    idle = jenv.entity("idle")
    found = jenv.nominated()
    assert idle in found  # premise: the query returns candidates at all
    assert not {recent, busy, future, *plain_pair} & set(found)


def test_target_date_passed_signal(jenv):
    past = jenv.entity("past", age_days=1, target_date="2026-01-15")
    future = jenv.entity("future", age_days=1, target_date="2999-01-01")
    found = jenv.nominated()
    assert found[past]["signals"] == ["target_date_passed"]
    assert future not in found


def test_secret_hit_signal_records_kinds_but_the_candidate_is_not_judged_or_given_evidence(jenv):
    uid = jenv.entity("leak", age_days=1, content=f"deploy key is {AWS_KEY} ok")
    found = jenv.nominated()
    cand = found[uid]
    assert cand["signals"] == ["secret_hit"] and cand["secret_kinds"] == ["AWS Access Key"]

    svc.gather_evidence([cand], MemoryQueryRepository(jenv.repo))
    assert cand["evidence"] == [] and cand["refs"] == {}  # never sent to a model

    calls: list[int] = []
    out = svc.judge([cand], lambda s, u: calls.append(1) or "{}", k=3)
    assert calls == [] and out["llm_calls"] == 0
    assert out["verdicts"][uid].category == "must_not_keep"


def _seed_anchor(env: _Env, memory: str, *, anchored_days_ago: int, dirty: bool = True) -> str:
    path = f"src/{uuidlib.uuid4().hex[:6]}.py"
    env.repo.execute(
        "MATCH (m:Entity {uuid:$m}) "
        "CREATE (f:Entity {uuid:$f, name:$path, structure_role:'file', structure_project:$proj, "
        "structure_path:$path, structure_dirty:$dirty, last_event_op:'modify', test_tag:$tag}) "
        "SET f.dirty_at = datetime() "
        "CREATE (m)-[:ANCHORED_TO {created_at: datetime() - duration({days: $days})}]->(f)",
        {"m": memory, "f": f"{env.tag}-file-{uuidlib.uuid4().hex[:6]}", "path": path,
         "proj": f"{env.tag}-proj", "dirty": dirty, "days": anchored_days_ago, "tag": env.tag})
    return path


def test_stale_anchor_signal_carries_the_changed_file(jenv):
    stale = jenv.entity("anchored", age_days=1)
    path = _seed_anchor(jenv, stale, anchored_days_ago=5)  # file changed AFTER the anchor
    fresh = jenv.entity("anchored-fresh", age_days=1)
    _seed_anchor(jenv, fresh, anchored_days_ago=-5)         # anchor made AFTER the file changed
    clean = jenv.entity("anchored-clean", age_days=1)
    _seed_anchor(jenv, clean, anchored_days_ago=5, dirty=False)
    found = jenv.nominated()
    assert found[stale]["signals"] == ["stale_anchor"]
    assert found[stale]["anchor"]["path"] == path and found[stale]["anchor"]["operation"] == "modify"
    assert fresh not in found and clean not in found

    svc.gather_evidence([found[stale]], MemoryQueryRepository(jenv.repo))
    (ev,) = [e for e in found[stale]["evidence"] if e["label"] == "anchor"]
    assert path in ev["text"] and ev["handle"] == "E1"


_SCALAR_SEED = """
CREATE (self:Entity {uuid:$subj, group_id:$ns, namespace:$ns, name:'user', test_tag:$tag})
CREATE (staleEp:Episodic {uuid:$stale_ep, group_id:$ns, source:'chat_message', test_tag:$tag})
CREATE (currEp:Episodic  {uuid:$curr_ep,  group_id:$ns, source:'chat_message', test_tag:$tag})
CREATE (staleMem:Entity {uuid:$stale_mem, group_id:$ns, namespace:$ns, test_tag:$tag,
    name:'user owns 20 rare coins', content:'user owns 20 rare coins'})
CREATE (currMem:Entity  {uuid:$curr_mem,  group_id:$ns, namespace:$ns, test_tag:$tag,
    name:'user owns 37 rare coins', content:'user owns 37 rare coins'})
CREATE (staleEp)-[:MENTIONS]->(staleMem)
CREATE (currEp)-[:MENTIONS]->(currMem)
CREATE (staleA:TypedAssertion {namespace:$ns, binding_pending:false, operation:'absolute',
    episode_uuid:$stale_ep, subject_uuid:$subj, attribute:'owned', scope:'',
    value_kind:'count', unit:'', value_json:'20', evidence_tier:'user',
    valid_at: datetime('2026-07-01T00:00:00Z'), superseded:false, test_tag:$tag})
CREATE (currA:TypedAssertion {namespace:$ns, binding_pending:false, operation:'absolute',
    episode_uuid:$curr_ep, subject_uuid:$subj, attribute:'owned', scope:'',
    value_kind:'count', unit:'', value_json:'37', evidence_tier:'user',
    valid_at: datetime('2026-07-15T00:00:00Z'), superseded:false, test_tag:$tag})
CREATE (v:Entity {view_kind:'scalar_state', view_subject_uuid:$subj, group_id:$ns, namespace:$ns,
    ss_attribute:'owned', ss_scope:'', ss_kind:'count', ss_unit:'', view_current:true,
    test_tag:$tag})
"""


def _seed_scalar(env: _Env) -> dict[str, str]:
    ids = {"subj": f"{env.tag}-subj", "stale_ep": f"{env.tag}-ep-stale",
           "curr_ep": f"{env.tag}-ep-curr", "stale_mem": f"{env.tag}-mem-stale",
           "curr_mem": f"{env.tag}-mem-curr"}
    env.repo.execute(_SCALAR_SEED, {**ids, "ns": env.ns, "tag": env.tag})
    return ids


def test_scalar_replaced_signal_flags_the_stale_value_memory_only(jenv):
    ids = _seed_scalar(jenv)
    found = jenv.nominated()
    stale = found[ids["stale_mem"]]
    assert stale["signals"] == ["scalar_replaced"]
    assert str(stale["scalar"]["stale_value"]) == "20" and str(stale["scalar"]["view_value"]) == "37"
    # The memory whose source IS the latest assertion carries the same value as the View: no signal.
    assert ids["curr_mem"] not in found
    # The scalar View itself is derived state and never a candidate.
    assert len(found) == 1,f"only the stale-value memory is a candidate here: {sorted(found)}"

    svc.gather_evidence([stale], MemoryQueryRepository(jenv.repo))
    labels = [e["label"] for e in stale["evidence"]]
    assert labels[0] == "replacing_value" and "source_episode" in labels
    assert "37" in stale["evidence"][0]["text"] and "20" in stale["evidence"][0]["text"]


# ============================================================================ 4. isolation and ordering

def test_namespace_isolation(jenv):
    mine = jenv.entity("mine")
    other_ns = f"{jenv.tag}-other"
    theirs = jenv.entity("theirs", ns=other_ns)
    assert mine in jenv.nominated()
    assert theirs not in jenv.nominated()
    assert mine not in jenv.nominated(ns=other_ns)
    assert theirs in jenv.nominated(ns=other_ns)


def test_limit_keeps_the_candidate_with_the_most_signals(jenv):
    two = jenv.entity("two", target_date="2026-01-15")      # idle + target_date_passed
    one = jenv.entity("one")                                 # idle only
    rows = svc.nominate(jenv.repo.execute, jenv.ns, limit=1)
    assert [r["uuid"] for r in rows] == [two]
    assert rows[0]["signals"] == ["idle", "target_date_passed"]
    assert {r["uuid"] for r in svc.nominate(jenv.repo.execute, jenv.ns, limit=5)} == {two, one}


# ============================================================================ 5. read-only property

def _snapshot(env: _Env) -> dict[str, Any]:
    nodes = env.repo.execute(
        "MATCH (n) WHERE n.test_tag = $t RETURN elementId(n) AS id, labels(n) AS l, "
        "properties(n) AS p", {"t": env.tag})
    rels = env.repo.execute(
        "MATCH (a)-[r]->(b) WHERE a.test_tag = $t OR b.test_tag = $t "
        "RETURN elementId(r) AS id, type(r) AS t, elementId(a) AS s, elementId(b) AS e, "
        "properties(r) AS p", {"t": env.tag})
    return {
        "nodes": {r["id"]: (sorted(r["l"]), {k: _norm(v) for k, v in dict(r["p"]).items()})
                  for r in nodes},
        "rels": {r["id"]: (r["t"], r["s"], r["e"],
                           {k: _norm(v) for k, v in dict(r["p"]).items()}) for r in rels},
    }


def _seed_everything(env: _Env) -> dict[str, str]:
    """One graph holding a candidate for every signal, each with an explicit old last_accessed."""
    ids = _seed_scalar(env)
    idle = env.entity("idle", accessed_days=OLD_DAYS, content="user likes tea")
    peer = env.entity("peer", age_days=1)
    env.fact(idle, peer)
    leak = env.entity("leak", age_days=1, accessed_days=OLD_DAYS, content=f"key {AWS_KEY}")
    anchored = env.entity("anchored", age_days=1, accessed_days=OLD_DAYS)
    _seed_anchor(env, anchored, anchored_days_ago=5)
    target = env.entity("target", age_days=1, accessed_days=OLD_DAYS, target_date="2026-01-15")
    q, g = env.pair("ep")
    env.repo.execute(
        "MATCH (m:Entity {uuid:$m}) SET m.last_accessed = datetime() - duration({days: 400})",
        {"m": ids["stale_mem"]})
    return {**ids, "idle": idle, "leak": leak, "anchored": anchored, "target": target,
            "q": q, "g": g}


class _StubChat:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        return json.dumps({"category": "superseded", "evidence": ["E1"], "kind": "other",
                           "reason": "a later value exists"})


def test_snapshot_would_detect_a_last_accessed_touch(jenv):
    # Sensitivity guard: the read-only assertion below CAN fail.
    ids = _seed_everything(jenv)
    before = _snapshot(jenv)
    jenv.set(ids["idle"], "n.last_accessed = datetime()")
    assert _snapshot(jenv) != before


def test_nominate_gather_judge_and_report_write_nothing(jenv, isolated_telemetry_db):
    ids = _seed_everything(jenv)
    before = _snapshot(jenv)
    assert before["nodes"] and before["rels"]

    candidates = svc.nominate(jenv.repo.execute, jenv.ns, limit=1000)
    by_uuid = {c["uuid"]: c for c in candidates}
    signals = {s for c in candidates for s in c["signals"]}
    assert signals == {"idle", "target_date_passed", "secret_hit", "stale_anchor",
                       "scalar_replaced"}, "premise: every signal path was exercised"

    svc.gather_evidence(candidates, MemoryQueryRepository(jenv.repo))
    assert any(c["evidence"] for c in candidates), "premise: evidence reads ran"
    assert by_uuid[ids["stale_mem"]]["evidence"], "premise: receipts/facts were fetched"

    chat = _StubChat()
    judged = svc.judge(candidates, chat, k=3, call_budget=60)
    assert chat.calls > 0 and judged["stop_reason"] == ""
    report = svc.build_report(jenv.ns, candidates, judged)
    assert len(report["items"]) == len(candidates)
    assert AWS_KEY not in json.dumps(report)

    after = _snapshot(jenv)
    assert after["nodes"].keys() == before["nodes"].keys()
    assert after["rels"].keys() == before["rels"].keys()
    assert len(after["nodes"]) == len(before["nodes"])
    assert len(after["rels"]) == len(before["rels"])
    assert after == before, "the judge run changed graph state"

    # No pending action was proposed for any of those memories (the telemetry DB is the
    # per-test isolated one from the autouse fixture, so an empty store is meaningful).
    store = PendingActionStore(db_path=isolated_telemetry_db)
    assert store.count() == 0

    # And the safety net the property rests on: the production retire tool still sees every
    # candidate as unretired (nothing was marked).
    adapter = MemoryGraphAdapter(neo4j=jenv.repo)
    assert adapter.fetch_candidate_metadata([ids["idle"]])[0]["memory_retired"] is False
