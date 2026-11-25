"""Live: the visibility-only RETIRED state (retire_memory / restore_memory) against a real graph.

Safety property under test: retiring changes VISIBILITY only. A retired memory disappears from the
DEFAULT recall surfaces, stays on the HISTORICAL ones (timeline, recall_history) labelled
``retired``, is frozen against automatic lifecycle (decay, compression, TTL deletion, merging),
is still reachable by erasure, never disturbs a scalar View, and restore returns it to exactly its
prior visibility. Design: .agent/plans/menhir-retire-lifecycle-plan-2026-11-25.md, "Step 2 design".

Every assertion reads state back from the store (properties, edges, query results) rather than
trusting a return value. Each test pairs the "hidden" check with a premise check that the same
call DID return the memory before the retire, and a restore check that it returns afterwards, so
a surface that never returned the memory (or a retire that is a no-op) cannot pass.

Online: needs the test Neo4j instance (``--run-online``).
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_pipeline import _memory_admitted
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

from tests.test_retire_scalar_proof_live import _Case, _assert_unchanged

pytestmark = pytest.mark.online

MODEL = "text-embedding-3-small"
QUERY_VEC = [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------------- helpers

class _Embedder:
    model = MODEL


class _Graphiti:
    embedder_ref = _Embedder()

    async def embed_query(self, query: str) -> list[float]:
        return QUERY_VEC


def _svc(adapter: MemoryGraphAdapter) -> RecallService:
    return RecallService(graphiti_client=_Graphiti(), graph_adapter=adapter,
                         scoring_service=ScoringService())


def _norm(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # neo4j temporal types


class _Env:
    """One tagged namespace on the test instance, with small seeding and read-back helpers."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.tag = f"test-retire-live-{uuidlib.uuid4().hex}"
        self.ns = f"{self.tag}-ns"
        self.adapter = MemoryGraphAdapter(neo4j=repo)

    # -- seeding
    def entity(self, label: str, **props: Any) -> str:
        uid = f"{self.tag}-{label}-{uuidlib.uuid4().hex[:8]}"
        node = {"uuid": uid, "name": label, "group_id": self.ns, "namespace": self.ns,
                "test_tag": self.tag, "scope": "PERSISTENT", "freshness": "ACTIVE",
                "content": f"{label} content", **props}
        self.repo.execute("CREATE (n:Entity) SET n = $p, n.created_at = datetime()", {"p": node})
        return uid

    def pair(self, label: str, *, state: str = "READY", vec: list[float] | None = None,
             queue: dict[str, Any] | None = None, twin: dict[str, Any] | None = None,
             valid_at: str = "2026-03-01T10:00:00Z") -> tuple[str, str]:
        """A Menhir queue :Episodic and the Graphiti episode it resolves to (queue, twin)."""
        suffix = uuidlib.uuid4().hex[:8]
        q_uuid, g_uuid = f"{self.tag}-{label}-q-{suffix}", f"{self.tag}-{label}-g-{suffix}"
        g = {"uuid": g_uuid, "group_id": self.ns, "content": f"{label} text", "source": "message",
             "test_tag": self.tag, "name": f"{label}-episode", **(twin or {})}
        q = {"uuid": q_uuid, "namespace": self.ns, "resolved_episode_uuid": g_uuid,
             "processing_state": state, "content": f"{label} text", "source": "message",
             "content_embedding": vec or list(QUERY_VEC), "content_embedding_model": MODEL,
             "test_tag": self.tag, **(queue or {})}
        self.repo.execute(
            "CREATE (g:Episodic) SET g = $g, g.valid_at = datetime($t), g.created_at = datetime($t) "
            "CREATE (q:Episodic) SET q = $q, q.created_at = datetime($t)",
            {"g": g, "q": q, "t": valid_at},
        )
        return q_uuid, g_uuid

    def fact(self, a: str, b: str, text: str = "A relates to B") -> str:
        fact_uuid = f"{self.tag}-fact-{uuidlib.uuid4().hex[:8]}"
        self.repo.execute(
            "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
            "CREATE (a)-[r:RELATES_TO {uuid:$f, fact:$text, group_id:$ns, test_tag:$tag}]->(b) "
            "SET r.valid_at = datetime(), r.created_at = datetime()",
            {"a": a, "b": b, "f": fact_uuid, "text": text, "ns": self.ns, "tag": self.tag},
        )
        return fact_uuid

    def set(self, uid: str, assignments: str, **params: Any) -> None:
        self.repo.execute(
            f"MATCH (n) WHERE n.uuid = $u AND (n:Entity OR n:Episodic) SET {assignments}",
            {"u": uid, **params},
        )

    # -- read-back
    def marker(self, uid: str) -> dict[str, Any]:
        rows = self.repo.execute(
            "MATCH (n) WHERE n.uuid = $u AND (n:Entity OR n:Episodic) "
            "RETURN n.memory_retired AS retired, toString(n.memory_retired_at) AS at, "
            "n.memory_retired_reason AS reason, n.memory_retired_by AS by, "
            "toString(n.memory_restored_at) AS restored_at",
            {"u": uid},
        )
        assert len(rows) == 1, f"expected exactly one node {uid}, got {len(rows)}"
        return dict(rows[0])

    def exists(self, uid: str) -> bool:
        rows = self.repo.execute(
            "MATCH (n) WHERE n.uuid = $u AND (n:Entity OR n:Episodic) RETURN count(n) AS c",
            {"u": uid})
        return int(rows[0]["c"]) > 0

    def props(self, uid: str) -> dict[str, Any]:
        """All properties except the memory_* marker family, normalised for comparison."""
        rows = self.repo.execute(
            "MATCH (n) WHERE n.uuid = $u AND (n:Entity OR n:Episodic) RETURN properties(n) AS p",
            {"u": uid})
        return {k: _norm(v) for k, v in dict(rows[0]["p"]).items() if not k.startswith("memory_")}

    def edges(self, uid: str) -> list[dict[str, Any]]:
        rows = self.repo.execute(
            "MATCH (n {uuid: $u})-[r]-(m) "
            "RETURN type(r) AS t, startNode(r).uuid AS s, endNode(r).uuid AS e, properties(r) AS p "
            "ORDER BY t, s, e",
            {"u": uid})
        return [{"t": r["t"], "s": r["s"], "e": r["e"],
                 "p": {k: _norm(v) for k, v in dict(r["p"]).items()}} for r in rows]

    def marked_count(self) -> int:
        rows = self.repo.execute(
            "MATCH (n) WHERE n.test_tag = $t AND n.memory_retired IS NOT NULL RETURN count(n) AS c",
            {"t": self.tag})
        return int(rows[0]["c"])

    def retire(self, uid: str, reason: str = "test", by: str = "tester") -> dict[str, Any]:
        return self.adapter.retire_memory(uid, reason=reason, retired_by=by)

    def restore(self, uid: str) -> dict[str, Any]:
        return self.adapter.restore_memory(uid)


@pytest.fixture
def env(test_neo4j_repo):
    e = _Env(test_neo4j_repo)
    yield e
    test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", {"t": e.tag})


# ============================================================================ 1. round trip

def test_retire_restore_round_trip_changes_only_the_marker(env):
    a = env.entity("subject", type="Preference", summary="a summary", sharpness=0.5, edge_count=2)
    b = env.entity("peer")
    env.fact(a, b)
    ep_uuid = f"{env.tag}-mentions-episode"
    env.repo.execute(
        "MATCH (a:Entity {uuid:$a}) "
        "CREATE (e:Episodic {uuid:$e, group_id:$ns, namespace:$ns, test_tag:$tag, "
        "content:'mentions subject', processing_state:'COMPLETED'}) CREATE (e)-[:MENTIONS]->(a)",
        {"a": a, "e": ep_uuid, "ns": env.ns, "tag": env.tag})

    props_before, edges_before = env.props(a), env.edges(a)
    assert len(edges_before) == 2  # premise: the fact edge and the MENTIONS edge
    assert env.marker(a)["retired"] is None

    first = env.retire(a, reason="stale preference", by="alice")
    assert first["status"] == "retired" and first["uuid"] == a
    m1 = env.marker(a)
    assert m1["retired"] is True and m1["at"]
    assert m1["reason"] == "stale preference" and m1["by"] == "alice"
    assert m1["restored_at"] is None
    assert (env.props(a), env.edges(a)) == (props_before, edges_before)

    second = env.retire(a, reason="a different reason", by="bob")
    assert second["status"] == "already_retired"
    m2 = env.marker(a)
    assert m2["retired"] is True
    assert m2["at"] == m1["at"], "a repeat retire must keep the first memory_retired_at"
    assert (env.props(a), env.edges(a)) == (props_before, edges_before)

    restored = env.restore(a)
    assert restored["status"] == "restored"
    m3 = env.marker(a)
    assert m3["retired"] is False
    assert m3["restored_at"]
    assert m3["reason"] is None and m3["by"] is None
    assert m3["at"] == m1["at"], "restore keeps memory_retired_at as history"
    # Identical to before the round trip: every non-memory_* property and every edge.
    assert (env.props(a), env.edges(a)) == (props_before, edges_before)

    again = env.restore(a)
    assert again["status"] == "not_retired"
    assert env.marker(a)["retired"] is False
    assert (env.props(a), env.edges(a)) == (props_before, edges_before)


# ============================================================================ 2. twin pair

def test_episode_twin_pair_is_marked_and_cleared_together_from_either_end(env):
    q, g = env.pair("ep")
    assert env.marked_count() == 0

    # Retire by the queue node: both ends marked.
    assert env.retire(q)["status"] == "retired"
    for uid in (q, g):
        assert env.marker(uid)["retired"] is True and env.marker(uid)["at"], uid
        assert env.marker(uid)["reason"] == "test"
    # Restore by the TWIN uuid clears the queue node as well.
    assert env.restore(g)["status"] == "restored"
    for uid in (q, g):
        m = env.marker(uid)
        assert m["retired"] is False and m["restored_at"] and m["reason"] is None, uid

    # Retire by the twin uuid: both ends marked again (queue node found via resolved_episode_uuid).
    assert env.retire(g)["status"] == "retired"
    for uid in (q, g):
        assert env.marker(uid)["retired"] is True, uid
    # A repeat from the other end is idempotent, not a second retire.
    assert env.retire(q)["status"] == "already_retired"
    # Restore by the queue node clears both.
    assert env.restore(q)["status"] == "restored"
    for uid in (q, g):
        assert env.marker(uid)["retired"] is False, uid
    assert env.restore(q)["status"] == "not_retired"


# ============================================================================ 3. refusals

def _c_view(env):
    uid = env.entity("view", is_view=True, view_kind="scalar_state", view_class="FACT",
                     view_audience="RECALL")
    return uid, "is_view"


def _c_candidate(env):
    return env.entity("cand", scope="CANDIDATE"), "candidate_scope"


def _c_pending(env):
    return env.pair("pend", state="PENDING")[0], "episode_in_flight"


def _c_enriching(env):
    return env.pair("enr", state="ENRICHING")[0], "episode_in_flight"


def _c_flagged(env):
    return env.entity("flag", user_flagged=True), "user_flagged"


def _c_twin_flagged_via_queue(env):
    q, _g = env.pair("tf1", twin={"user_flagged": True})
    return q, "user_flagged"


def _c_twin_flagged_via_twin(env):
    q, g = env.pair("tf2", queue={"user_flagged": True})
    return g, "user_flagged"


def _c_retention_source(env):
    e = env.entity("kept")
    env.repo.execute(
        "MATCH (e:Entity {uuid:$e}) "
        "CREATE (x:Episodic {uuid:$x, group_id:$ns, namespace:$ns, test_tag:$tag, "
        "user_flagged:true, processing_state:'READY'}) CREATE (x)-[:RETENTION_SOURCE]->(e)",
        {"e": e, "x": f"{env.tag}-retention-source", "ns": env.ns, "tag": env.tag})
    return e, "retention_protected"


def _c_structural(env):
    return env.entity("struct", structure_role="file"), "structural"


def _c_queue_erased(env):
    q, _g = env.pair("erq", queue={"erased_at": "2026-11-01T00:00:00Z"})
    return q, "erased"


def _c_twin_erase_marker(env):
    # An erase in progress: the marker on the twin alone must stop a retire through the queue node.
    q, g = env.pair("erm")
    env.repo.execute("CREATE (:ErasureMarker {subject_uuid: $g, test_tag: $tag})",
                     {"g": g, "tag": env.tag})
    return q, "erased"


def _c_deleted_by_user(env):
    return env.pair("dbu", queue={"processing_substage": "deleted_by_user"})[0], "erased"


def _c_queue_enriching_via_twin(env):
    # The write marks both halves, so the queue node's state must stop a retire by the twin uuid.
    _q, g = env.pair("qen", state="ENRICHING")
    return g, "episode_in_flight"


def _c_twin_pending_via_queue(env):
    q, _g = env.pair("tpe", twin={"processing_state": "PENDING"})
    return q, "episode_in_flight"


def _c_twin_candidate_via_queue(env):
    q, _g = env.pair("tca", twin={"scope": "CANDIDATE"})
    return q, "candidate_scope"


@pytest.mark.parametrize("build", [
    _c_view, _c_candidate, _c_pending, _c_enriching, _c_flagged, _c_twin_flagged_via_queue,
    _c_twin_flagged_via_twin, _c_retention_source, _c_structural, _c_queue_erased,
    _c_twin_erase_marker, _c_deleted_by_user, _c_queue_enriching_via_twin,
    _c_twin_pending_via_queue, _c_twin_candidate_via_queue,
], ids=lambda f: f.__name__.removeprefix("_c_"))
def test_retire_refusals_carry_a_code_and_write_nothing(env, build):
    uid, code = build(env)
    assert env.marked_count() == 0

    result = env.retire(uid, reason="should not stick")

    assert result["status"] == "refused", result
    assert result["code"] == code, result
    assert result.get("detail")
    # No write anywhere in the tag: not on the target, not on its twin, not on a source.
    assert env.marked_count() == 0
    assert env.marker(uid)["retired"] is None and env.marker(uid)["at"] is None
    # Restore of something never retired is a no-op, not an error.
    assert env.restore(uid)["status"] == "not_retired"


class _AfterFirstCall:
    """Repository proxy that runs ``side_effect`` once, right after the first query (the probe)."""

    def __init__(self, repo: Any, side_effect: Any) -> None:
        self._repo, self._side_effect, self._calls = repo, side_effect, 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        out = self._repo.execute(*args, **kwargs)
        self._calls += 1
        if self._calls == 1:
            self._side_effect()
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)


@pytest.mark.parametrize("change", ["queue_enriching", "twin_flagged", "twin_erase_marker"])
def test_pair_change_between_probe_and_write_is_refused_by_the_write(env, change):
    # The probe passes; the pair then changes before the write. The write's own guards must refuse
    # (code "changed") and mark neither half.
    q, g = env.pair(f"race-{change}")
    effects = {
        "queue_enriching": lambda: env.set(q, "n.processing_state = 'ENRICHING'"),
        "twin_flagged": lambda: env.set(g, "n.user_flagged = true"),
        "twin_erase_marker": lambda: env.repo.execute(
            "CREATE (:ErasureMarker {subject_uuid: $g, test_tag: $tag})", {"g": g, "tag": env.tag}),
    }
    racing = MemoryGraphAdapter(neo4j=_AfterFirstCall(env.repo, effects[change]))

    result = racing.retire_memory(g, reason="race", retired_by="tester")

    assert result["status"] == "refused" and result["code"] == "changed", result
    assert env.marked_count() == 0


def test_retire_unknown_uuid_is_not_found(env):
    missing = f"{env.tag}-does-not-exist"
    assert env.retire(missing) == {"status": "not_found", "uuid": missing}
    assert env.restore(missing) == {"status": "not_found", "uuid": missing}
    assert env.marked_count() == 0


# ============================================================================ 4. default surfaces

def _uuids(rows: list[dict[str, Any]], key: str = "uuid") -> set[str]:
    return {str(r[key]) for r in rows}


def test_retired_memory_is_hidden_from_default_surfaces_and_restore_brings_it_back(env):
    ent = env.entity("subject", type="Preference")
    peer = env.entity("peer")
    env.fact(ent, peer)
    q, g = env.pair("ep", queue={"scope": "PERSISTENT"})
    adapter, ns = env.adapter, env.ns
    svc = _svc(adapter)

    def surfaces() -> dict[str, bool]:
        meta = adapter.fetch_candidate_metadata([ent])[0]
        return {
            "recent_entity": ent in _uuids(adapter.fetch_recent_memories(limit=50, namespace=ns)),
            "recent_episode": q in _uuids(adapter.fetch_recent_memories(limit=50, namespace=ns)),
            "by_scope_entity": ent in _uuids(adapter.fetch_memories_by_scope(
                "PERSISTENT", limit=50, namespace=ns)),
            "by_scope_episode": q in _uuids(adapter.fetch_memories_by_scope(
                "PERSISTENT", limit=50, namespace=ns)),
            "by_type": ent in _uuids(adapter.fetch_memories_by_type(
                "Preference", limit=50, namespace=ns)),
            "source_lane": q in _uuids(adapter.search_episode_embeddings(
                QUERY_VEC, limit=50, namespace=ns, model=MODEL)),
            "source_lane_resolved_only": q in _uuids(adapter.search_episode_embeddings(
                QUERY_VEC, limit=50, namespace=ns, model=MODEL, resolved_only=True)),
            "admitted": _memory_admitted(
                svc, meta, include_session=True, session_id=None, namespace=ns),
        }

    everything = set(surfaces())
    before = surfaces()
    assert before == {k: True for k in everything}, f"premise: all visible before retire: {before}"
    assert adapter.fetch_candidate_metadata([ent])[0]["memory_retired"] is False
    assert len(adapter.fetch_temporal_facts([ent])) == 1

    assert env.retire(ent)["status"] == "retired"
    assert env.retire(q)["status"] == "retired"
    after = surfaces()
    # Each entity surface hides the entity, each episode surface hides the episode.
    for key in ("recent_entity", "by_scope_entity", "by_type", "admitted"):
        assert after[key] is False, f"{key} still shows the retired entity"
    for key in ("recent_episode", "by_scope_episode", "source_lane", "source_lane_resolved_only"):
        assert after[key] is False, f"{key} still shows the retired episode"
    meta = adapter.fetch_candidate_metadata([ent])[0]
    assert meta["memory_retired"] is True  # projected, so admission can reject it
    assert meta["is_view"] is False

    # The fact is dropped when its OWN node is retired ...
    assert adapter.fetch_temporal_facts([ent]) == []
    assert adapter.fetch_candidate_fact_edges([ent]) == []

    assert env.restore(ent)["status"] == "restored"
    assert env.restore(q)["status"] == "restored"
    assert surfaces() == before
    assert adapter.fetch_candidate_metadata([ent])[0]["memory_retired"] is False
    assert len(adapter.fetch_temporal_facts([ent])) == 1


def test_fact_whose_peer_is_retired_is_dropped_from_temporal_facts(env):
    ent = env.entity("subject")
    peer = env.entity("peer")
    fact_uuid = env.fact(ent, peer)
    adapter = env.adapter

    rows = adapter.fetch_temporal_facts([ent])
    assert [r["node_uuid"] for r in rows] == [ent]
    assert [r["fact_uuid"] for r in adapter.fetch_candidate_fact_edges([ent])] == [fact_uuid]

    assert env.retire(peer)["status"] == "retired"
    assert env.marker(ent)["retired"] is None  # only the peer is retired
    assert adapter.fetch_temporal_facts([ent]) == []
    assert adapter.fetch_candidate_fact_edges([ent]) == []
    # The edge itself is untouched: visibility only.
    assert len(env.edges(ent)) == 1

    assert env.restore(peer)["status"] == "restored"
    assert [r["node_uuid"] for r in adapter.fetch_temporal_facts([ent])] == [ent]
    assert [r["fact_uuid"] for r in adapter.fetch_candidate_fact_edges([ent])] == [fact_uuid]


def test_retired_memory_is_not_a_startup_pin_until_restored(env):
    # Retire refuses flagged memories and flag refuses retired ones (either half of an episode
    # pair), so "flagged and retired" (a hidden pin) is unreachable. Restore, then flag, pins.
    ent = env.entity("pinned")
    adapter, ns = env.adapter, env.ns
    assert env.retire(ent)["status"] == "retired"
    with pytest.raises(ValueError, match="retired"):
        adapter.flag_memory(ent, bootstrap_scope="general")
    assert env.props(ent).get("user_flagged") is not True

    q, g = env.pair("pinned-episode")
    assert env.retire(q)["status"] == "retired"
    for uid in (q, g):
        with pytest.raises(ValueError, match="retired"):
            adapter.flag_memory(uid, bootstrap_scope="general")
        assert env.props(uid).get("user_flagged") is not True

    assert env.restore(ent)["status"] == "restored"
    assert adapter.flag_memory(ent, bootstrap_scope="general") is True
    assert ent in _uuids(adapter.fetch_flagged_memories(limit=50, namespace=ns))


# ============================================================================ 5. historical surfaces

def _mark_queue_only(env, q, g):
    env.set(q, "n.memory_retired = true, n.memory_retired_at = datetime()")


def _mark_twin_only(env, q, g):
    env.set(g, "n.memory_retired = true, n.memory_retired_at = datetime()")


def _mark_via_tool(env, q, g):
    assert env.retire(q)["status"] == "retired"


@pytest.mark.parametrize("mark", [_mark_via_tool, _mark_queue_only, _mark_twin_only],
                         ids=["via_tool_both", "queue_only", "twin_only"])
def test_historical_surfaces_include_retired_memory_labelled(env, mark):
    q, g = env.pair("ep")
    env.set(g, "n.content = 'timeline text'")
    adapter, ns = env.adapter, env.ns

    def label(rows: list[dict[str, Any]], uid: str) -> Any:
        hit = [r for r in rows if r["uuid"] == uid]
        assert len(hit) == 1, f"{uid} not returned: {[r['uuid'] for r in rows]}"
        return hit[0]["memory_retired"]

    def read() -> dict[str, Any]:
        anchor_q = adapter.timeline_anchor(uuid=q, namespace=ns)
        anchor_g = adapter.timeline_anchor(uuid=g, namespace=ns)
        assert anchor_q is not None and anchor_g is not None
        return {
            "search": label(adapter.search_episode_embeddings(
                QUERY_VEC, limit=50, namespace=ns, model=MODEL, include_retired=True), q),
            "search_resolved": label(adapter.search_episode_embeddings(
                QUERY_VEC, limit=50, namespace=ns, model=MODEL, include_retired=True,
                resolved_only=True), q),
            "page": label(adapter.timeline_page(namespace=ns, limit=50), g),
            "anchor_by_receipt": anchor_q["memory_retired"],
            "anchor_by_episode": anchor_g["memory_retired"],
        }

    assert read() == {k: False for k in read()}  # premise: reachable and unlabelled

    mark(env, q, g)

    assert read() == {k: True for k in read()}


def test_resolve_timeline_subject_still_finds_a_retired_entity(env):
    ent = env.entity("Rachel Retired")
    assert [r["uuid"] for r in env.adapter.resolve_timeline_subject(
        subject="Rachel Retired", namespace=env.ns)] == [ent]
    assert env.retire(ent)["status"] == "retired"
    assert [r["uuid"] for r in env.adapter.resolve_timeline_subject(
        subject="Rachel Retired", namespace=env.ns)] == [ent]


@pytest.mark.asyncio
async def test_service_level_history_and_timeline_label_retired(env):
    q, g = env.pair("ep")
    svc, ns = _svc(env.adapter), env.ns

    async def read():
        history = await svc.recall_history("anything", namespace=ns, limit=10)
        around = await svc.recall_timeline(namespace=ns, around=q, limit=3)
        by_query = await svc.recall_timeline(namespace=ns, query="anything", limit=3)
        return (
            {m.uuid: m.retired for m in history.memories},
            {e.uuid: e.retired for e in around.entries},
            {e.uuid: e.retired for e in by_query.entries},
        )

    hist, around, by_query = await read()
    assert hist == {q: False} and around == {g: False} and by_query == {g: False}

    assert env.retire(q)["status"] == "retired"
    hist, around, by_query = await read()
    assert hist == {q: True}
    assert around == {g: True}
    assert by_query == {g: True}

    assert env.restore(q)["status"] == "restored"
    assert await read() == ({q: False}, {g: False}, {g: False})


# ============================================================================ 6. lifecycle freeze

def test_retired_persistent_entity_is_frozen_against_decay_compression_and_bridge_delete(env):
    old = "n.last_accessed = datetime() - duration({days: 400}), " \
          "n.created_at = datetime() - duration({days: 400})"
    squash = env.entity("to-compress", edge_count=0, sharpness=0.1)
    bridge = env.entity("to-bridge", edge_count=0, sharpness=0.1)
    env.set(squash, old)
    env.set(bridge, old)
    adapter = env.adapter

    def candidates() -> set[str]:
        rows = adapter.fetch_decay_candidates(
            "ACTIVE", min_days_since_accessed=30, max_edge_count=5, max_sharpness=None)
        return {r["uuid"] for r in rows}

    assert {squash, bridge} <= candidates(), "premise: both are decay candidates"
    props_squash, props_bridge = env.props(squash), env.props(bridge)

    assert env.retire(squash)["status"] == "retired"
    assert env.retire(bridge)["status"] == "retired"

    assert not ({squash, bridge} & candidates())
    assert adapter.compress_node(squash, "SUMMARY") is False
    assert adapter.bridge_and_delete(bridge) == {"edges_bridged": 0, "deleted": 0}
    # Nothing moved: content, freshness and existence are unchanged.
    assert env.props(squash) == props_squash
    assert env.props(bridge) == props_bridge
    assert env.exists(bridge)

    # Restore: the SAME nodes are selected and mutable again (the marker, not anything else, froze them).
    assert env.restore(squash)["status"] == "restored"
    assert env.restore(bridge)["status"] == "restored"
    assert {squash, bridge} <= candidates()
    assert adapter.compress_node(squash, "SUMMARY") is True
    row = env.repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.freshness AS f, n.content AS c", {"u": squash})[0]
    assert (row["f"], row["c"]) == ("COMPRESSED", "SUMMARY")
    assert adapter.bridge_and_delete(bridge)["deleted"] == 1
    assert not env.exists(bridge)


def test_retired_session_entity_with_expired_ttl_survives_the_ttl_sweep(env):
    sid = f"{env.tag}-session"
    ent = env.entity("expiring", scope="SESSION", session_id=sid)
    env.set(ent, "n.ttl_expires = datetime() - duration({days: 1})")
    adapter = env.adapter

    def expired() -> set[str]:
        return {r["uuid"] for r in adapter.fetch_ttl_expired_session_uuids(sid)}

    assert expired() == {ent}, "premise: expired and selected"

    assert env.retire(ent)["status"] == "retired"
    assert expired() == set()
    deleted = adapter.delete_entities_returning_uuids(
        [ent], protect_retention=True, require_expired_ttl=True)
    assert deleted == [] and env.exists(ent)

    assert env.restore(ent)["status"] == "restored"
    assert expired() == {ent}
    assert adapter.delete_entities_returning_uuids(
        [ent], protect_retention=True, require_expired_ttl=True) == [ent]
    assert not env.exists(ent)


def test_retired_entity_is_ineligible_for_merge_until_restored(env):
    survivor = env.entity("merge survivor")
    absorbed = env.entity("merge absorbed")
    repo = env.repo
    correlation = CorrelationRepository(repo)

    def mutation_predicate_allows() -> bool:
        rows = repo.execute(
            "MATCH (survivor:Entity {uuid:$s}), (absorbed:Entity {uuid:$a}) "
            f"WHERE {me.mutable_eligibility_cypher()} RETURN count(*) AS c",
            {"s": survivor, "a": absorbed, **me.MUTABLE_PREDICATE_PARAMS})
        return int(rows[0]["c"]) == 1

    ok = correlation.evaluate_merge_eligibility(survivor, absorbed)
    assert (ok.allowed, ok.reason_code) == (True, me.ELIGIBLE), "premise: the pair is eligible"
    assert mutation_predicate_allows()

    for retired_one in (absorbed, survivor):
        assert env.retire(retired_one)["status"] == "retired"
        verdict = correlation.evaluate_merge_eligibility(survivor, absorbed)
        assert (verdict.allowed, verdict.reason_code) == (False, me.MEMORY_RETIRED), retired_one
        assert verdict.diagnostics["retired"] == [retired_one]
        # The statement-level guard (the form the mutation repeats) refuses it too.
        assert not mutation_predicate_allows()
        # And the real merge abstains without touching either node.
        result = correlation.merge_entity(survivor, absorbed, similarity=0.99)
        assert result["merged"] == 0 and result.get("reason") == me.MEMORY_RETIRED, result
        assert env.exists(survivor) and env.exists(absorbed)
        assert env.restore(retired_one)["status"] == "restored"

    # Restored: the same pair is eligible and merges (proves the marker caused the veto).
    again = correlation.evaluate_merge_eligibility(survivor, absorbed)
    assert (again.allowed, again.reason_code) == (True, me.ELIGIBLE)
    assert mutation_predicate_allows()
    assert correlation.merge_entity(survivor, absorbed, similarity=0.99)["merged"] == 1
    assert env.exists(survivor) and not env.exists(absorbed)


# ============================================================================ 7. erasure

def test_erasure_paths_still_reach_a_retired_memory(env):
    adapter = env.adapter
    swept = env.entity("swept", scope="SESSION")
    deleted_by_id = env.entity("deleted")
    q, g = env.pair("ep", twin={"content": "erase me"}, queue={"content": "erase me"})
    for uid in (swept, deleted_by_id, q):
        assert env.retire(uid)["status"] == "retired", uid
        assert env.marker(uid)["retired"] is True

    # The operator delete: no retention protection, no TTL requirement.
    assert adapter.delete_entities_returning_uuids([swept], protect_retention=False) == [swept]
    assert not env.exists(swept)

    # The delete_memory (erase) path on a retired entity ...
    assert adapter.delete_memory(deleted_by_id) is True
    assert not env.exists(deleted_by_id)

    # ... and on a retired episode: its text must not survive (the node may remain as a tombstone).
    assert adapter.delete_memory(q) is True
    rows = env.repo.execute(
        "MATCH (n:Episodic {uuid:$u}) RETURN n.content AS content", {"u": q})
    assert all(r["content"] is None for r in rows), rows


# ============================================================================ 8. scalar View

@pytest.fixture
def scalar_case(test_neo4j_repo):
    namespace = f"test-retire-live-scalar-{uuidlib.uuid4().hex}"
    c = _Case(test_neo4j_repo, namespace)
    yield c
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $ns OR n.namespace = $ns OR n.group_id = $ns "
        "OR n.namespace_key = $ns OR n.subject_uuid = $subject DETACH DELETE n",
        {"ns": namespace, "subject": c.subject})


def test_retiring_scalar_sources_through_the_tool_leaves_the_view_unchanged(scalar_case):
    from tests.test_retire_scalar_proof_live import _T1, _T2, _visible

    c = scalar_case
    t1 = c.turn("I have 30 coins")
    t2 = c.turn("Actually I have 37 coins now")
    c.absolute(t1, "I have 30 coins", "I have 30 coins", 30, _T1)
    c.absolute(t2, "Actually I have 37 coins now", "Actually I have 37 coins", 37, _T2,
               semantics="correction")
    c.build()

    before = c.snapshot()
    (view_uuid,) = [u for u, v in before["views"].items()
                    if v["props"].get("ss_attribute") == "owned"]
    assert str(before["views"][view_uuid]["props"]["ss_value"]) == "37"
    assert before["views"][view_uuid]["visible"] is True

    # Retire every memory-shaped source through the production tool path. (TurnEvidence is not an
    # Entity or Episodic, so retire_memory cannot and need not reach it.)
    sources = [*c.episodes, c.subject, c.value_entity]
    for uid in sources:
        out = c.adapter.retire_memory(uid, reason="scalar proof", retired_by="test")
        assert out["status"] == "retired", (uid, out)
    # The View itself is derived state: the tool refuses it and writes nothing to it.
    refused = c.adapter.retire_memory(view_uuid, reason="no", retired_by="test")
    assert (refused["status"], refused["code"]) == ("refused", "is_view")

    marked = c.repo.execute(
        "MATCH (n) WHERE n.group_id = $ns AND n.memory_retired = true RETURN collect(n.uuid) AS u",
        {"ns": c.ns})[0]["u"]
    assert sorted(marked) == sorted(sources), "the marker landed on exactly the sources"
    assert c.repo.execute(
        "MATCH (n) WHERE n.view_kind IS NOT NULL AND n.memory_retired IS NOT NULL "
        "RETURN count(n) AS c")[0]["c"] == 0

    c.refold_all_paths()

    after = c.snapshot()
    _assert_unchanged(before, after)
    assert after["views"][view_uuid]["visible"] is True
    assert _visible(c.repo, view_uuid) is True

    # Restoring the sources is equally a no-op for the View.
    for uid in sources:
        assert c.adapter.restore_memory(uid)["status"] == "restored", uid
    c.refold_all_paths()
    _assert_unchanged(before, c.snapshot())
