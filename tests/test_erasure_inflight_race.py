"""Erase racing in-flight enrichment (#476): compensating erase of the orphaned Graphiti twin.

An erase flips a PENDING/ENRICHING queue node Q to FAILED/deleted_by_user and stamps ``erased_at``.
A worker already inside ``add_episode`` then writes Graphiti twin T (with derived entities/facts)
and finds Q erased but ``Q.resolved_episode_uuid`` NULL, so the erase never saw T. The worker's
``_compensate_if_erased`` -> ``compensate_erased_enrichment`` links Q to T and erases T and its
single-source derivatives. If the worker crashes first, ``sweep_erased_twin_compensations`` is the
backstop (guarded by a 1h age window, a unique same-namespace name match, and an ambiguity stop).

The graph is seeded directly with Cypher (no LLM). All tests need a live Neo4j.
"""

from __future__ import annotations

import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator

SECRET = "The race probe's favourite colour is teal-4417"


# ------------------------------------------------------------------ helpers
def _ids(tag: str) -> dict[str, str]:
    return {
        "tag": tag,
        "ns": f"{tag}-ns",
        "other_ns": f"{tag}-other-ns",
        "queue": f"{tag}-queue",
        "twin": f"{tag}-graphiti",
        "twin2": f"{tag}-graphiti2",
        "entity": f"{tag}-entity",
        "entity2": f"{tag}-entity2",
        "edge": f"{tag}-edge",
        "other": f"{tag}-other",
        "name": f"{tag}-anchor-name",
    }


def _seed_queue(
    repo: Any,
    ids: dict[str, str],
    *,
    erased: bool = True,
    erased_age: str | None = "PT2H",
) -> None:
    """Queue node Q, unlinked (resolved_episode_uuid NULL).

    erased=True -> FAILED/deleted_by_user with erased_at = now - erased_age (or now if None).
    erased=False -> a live READY node.
    """
    if erased:
        repo.execute(
            """
            CREATE (:Episodic {uuid:$queue, namespace:$ns, name:$name, content:$text,
                               processing_state:'FAILED', processing_substage:'deleted_by_user',
                               processing_started_at: datetime() - duration('PT3H'),
                               erased_at: CASE WHEN $age IS NULL THEN datetime()
                                               ELSE datetime() - duration($age) END,
                               test_tag:$tag})
            """,
            params={**ids, "text": SECRET, "age": erased_age},
        )
    else:
        repo.execute(
            """
            CREATE (:Episodic {uuid:$queue, namespace:$ns, name:$name, content:$text,
                               processing_state:'READY', test_tag:$tag})
            """,
            params={**ids, "text": SECRET},
        )


def _seed_twin_with_derivatives(repo: Any, ids: dict[str, str]) -> None:
    """Twin T + single-source entity E (mentioned only by T) + fact E->E2 supported only by T."""
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, name:$name, valid_at:datetime(),
                            created_at: datetime() - duration('PT1H'),
                            content:$text, test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'race probe', group_id:$ns, summary:$text,
                           created_at:datetime(), test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-4417', group_id:$ns, summary:'a colour',
                           created_at:datetime(), test_tag:$tag})
        CREATE (t)-[:MENTIONS {uuid:$mention, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$text,
                                  episodes:[$twin], group_id:$ns, created_at:datetime(),
                                  test_tag:$tag}]->(e2)
        """,
        params={**ids, "text": SECRET, "mention": f"{ids['tag']}-mention"},
    )


def _seed_bare_twin(
    repo: Any, ids: dict[str, str], twin_uuid: str, *, group_id: str | None = None
) -> None:
    """A Graphiti-shaped twin with Q's anchor name, created after Q's claim, no derivatives."""
    repo.execute(
        """
        CREATE (:Episodic {uuid:$u, group_id:$gid, name:$name, valid_at:datetime(),
                           created_at: datetime() - duration('PT1H'),
                           content:$text, test_tag:$tag})
        """,
        params={**ids, "u": twin_uuid, "gid": group_id or ids["ns"], "text": SECRET},
    )


def _coordinator(repo: Any, tmp_path: Path):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(neo4j=repo)
    db = tmp_path / "inflight-race.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    journal = GraphOperationsJournal(db_path=db)
    coord = ErasureCoordinator(
        graph_adapter=adapter, journal=journal, subjects=ErasureSubjectStore(db_path=db)
    )
    return adapter, coord


class _ScopedAdapter:
    """Delegates everything to the real adapter, but the sweep listings only return this test's
    queue nodes, so a sweep on a shared test database never touches another test's data. The
    listing queries themselves (including the 1h age window) still run for real."""

    def __init__(self, inner: Any, only: set[str]) -> None:
        self._inner = inner
        self._only = only

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def list_unlinked_erased_queue_nodes(self, limit: int) -> list[str]:
        return [u for u in self._inner.list_unlinked_erased_queue_nodes(100000) if u in self._only]

    def list_erased_twin_compensations(self, limit: int) -> list[dict]:
        return [
            r for r in self._inner.list_erased_twin_compensations(100000) if r["uuid"] in self._only
        ]


def _cleanup(repo: Any, tag: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
    repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t DETACH DELETE r",
        params={"t": tag},
    )


def _exists(repo: Any, node_uuid: str) -> bool:
    rows = repo.execute("MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid})
    return int(rows[0]["c"]) > 0


def _edge_exists(repo: Any, edge_uuid: str) -> bool:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN count(r) AS c", params={"u": edge_uuid}
    )
    return int(rows[0]["c"]) > 0


def _queue_props(repo: Any, queue_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid:$u}) RETURN n.resolved_episode_uuid AS resolved, "
        "n.erasure_compensation_pending AS pending, "
        "n.erasure_compensation_checked AS checked",
        params={"u": queue_uuid},
    )
    return dict(rows[0]) if rows else None


def _ctx(adapter: Any, queue_uuid: str) -> Any:
    return SimpleNamespace(graph_adapter=adapter, episode_uuid=queue_uuid)


# ------------------------------------------------------------------ worker path
@pytest.mark.online
def test_worker_loses_race_twin_and_single_source_derivatives_are_erased(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if compensate_erased_enrichment stops linking Q to T before erasing (erase_memory
    would then find no twin and leave T), stops cascading to single-source derivatives, or leaves
    erasure_compensation_pending set after the twin is gone."""
    repo = test_neo4j_repo
    tag = f"test-race-worker-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age=None)
        _seed_twin_with_derivatives(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)
        assert _queue_props(repo, ids["queue"])["resolved"] is None  # the race precondition

        from menhir.services.erasure_compensation import compensate_erased_enrichment

        result = compensate_erased_enrichment(
            adapter, ids["queue"], ids["twin"], coordinator_factory=lambda _a: coord
        )

        assert result == {"linked": True, "erased": True}
        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, ids["entity"]) is False
        assert _edge_exists(repo, ids["edge"]) is False
        # E2 was never mentioned by T, so it was never derived from T alone.
        assert _exists(repo, ids["entity2"]) is True
        # Pending flag must not survive: either Q is gone or the flag is cleared, and the sweep
        # must not see it as outstanding.
        props = _queue_props(repo, ids["queue"])
        assert props is None or props["pending"] is None
        assert ids["queue"] not in {
            r["uuid"] for r in adapter.list_erased_twin_compensations(100000)
        }
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_worker_entry_point_compensate_if_erased_erases_twin(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """Same race through the worker's real hook `_compensate_if_erased(ctx, resolved_uuid)`.
    Breaks if the hook stops consulting episode_erased_by_user, stops calling
    compensate_erased_enrichment, or passes the wrong uuids."""
    repo = test_neo4j_repo
    tag = f"test-race-hook-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age=None)
        _seed_twin_with_derivatives(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)
        import menhir.services.erasure_compensation as comp

        monkeypatch.setattr(comp, "_default_coordinator", lambda _a: coord)
        from menhir.services.enrichment_steps import _compensate_if_erased

        _compensate_if_erased(_ctx(adapter, ids["queue"]), ids["twin"])

        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, ids["entity"]) is False
        assert _edge_exists(repo, ids["edge"]) is False
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_not_erased_queue_node_compensation_is_noop(test_neo4j_repo, tmp_path, monkeypatch) -> None:
    """Q is READY (not deleted_by_user): neither the hook nor the compensation entry point may
    touch T. Breaks if the erased guard (substage deleted_by_user) is dropped from
    episode_erased_by_user or link_erased_twin, which would erase a healthy memory's twin."""
    repo = test_neo4j_repo
    tag = f"test-race-live-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased=False)
        _seed_twin_with_derivatives(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)
        import menhir.services.erasure_compensation as comp

        monkeypatch.setattr(comp, "_default_coordinator", lambda _a: coord)
        from menhir.services.enrichment_steps import _compensate_if_erased
        from menhir.services.erasure_compensation import compensate_erased_enrichment

        _compensate_if_erased(_ctx(adapter, ids["queue"]), ids["twin"])
        direct = compensate_erased_enrichment(
            adapter, ids["queue"], ids["twin"], coordinator_factory=lambda _a: coord
        )

        assert direct == {"linked": False, "erased": False}
        for node_uuid in (ids["queue"], ids["twin"], ids["entity"], ids["entity2"]):
            assert _exists(repo, node_uuid) is True, node_uuid
        assert _edge_exists(repo, ids["edge"]) is True
        props = _queue_props(repo, ids["queue"])
        assert props["resolved"] is None and props["pending"] is None
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_shared_derivative_is_kept_and_flagged_rederive_pending(test_neo4j_repo, tmp_path) -> None:
    """T's entity is also mentioned by another live episode. Compensation must behave like
    erase_memory: keep it, flag rederive_pending. Control: an entity mentioned only by T is
    deleted, proving the cascade ran. Breaks if compensation bypasses the coordinator's
    sharing check (deleting by twin mention alone)."""
    repo = test_neo4j_repo
    tag = f"test-race-shared-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age=None)
        _seed_twin_with_derivatives(repo, ids)
        repo.execute(
            """
            MATCH (e:Entity {uuid:$entity})
            CREATE (o:Episodic {uuid:$other, group_id:$ns, valid_at:datetime(),
                                content:'another memory', test_tag:$tag})
            CREATE (o)-[:MENTIONS {uuid:$m, group_id:$ns}]->(e)
            """,
            params={**ids, "m": f"{tag}-mention-other"},
        )
        # Control: only T mentions this one. The fact edge is dropped so only mentions decide.
        repo.execute(
            "MATCH ()-[r:RELATES_TO {uuid:$edge}]->() DELETE r "
            "WITH count(r) AS dropped "
            "MATCH (t:Episodic {uuid:$twin}) "
            "CREATE (c:Entity {uuid:$ctl, name:'control', group_id:$ns, summary:'x', test_tag:$tag}) "
            "CREATE (t)-[:MENTIONS {uuid:$m2, group_id:$ns}]->(c)",
            params={**ids, "ctl": f"{tag}-control", "m2": f"{tag}-mention-ctl"},
        )
        adapter, coord = _coordinator(repo, tmp_path)

        from menhir.services.erasure_compensation import compensate_erased_enrichment

        result = compensate_erased_enrichment(
            adapter, ids["queue"], ids["twin"], coordinator_factory=lambda _a: coord
        )

        assert result == {"linked": True, "erased": True}
        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, f"{tag}-control") is False
        assert _exists(repo, ids["other"]) is True
        rows = repo.execute(
            "MATCH (e:Entity {uuid:$u}) RETURN e.rederive_pending AS pending", params={"u": ids["entity"]}
        )
        assert rows, "an entity still mentioned by a live episode must survive compensation"
        assert rows[0]["pending"] is True
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ sweep backstop
@pytest.mark.online
def test_sweep_unique_twin_is_linked_and_erased(test_neo4j_repo, tmp_path) -> None:
    """Crashed worker: Q erased 2h ago, unlinked, exactly one same-namespace twin with Q's anchor
    name created after the claim. Breaks if the sweep stops listing aged unlinked erased nodes,
    the name/namespace/created_at match regresses, or the match is not linked and erased."""
    repo = test_neo4j_repo
    tag = f"test-race-sweep-unique-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age="PT2H")
        _seed_twin_with_derivatives(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)

        from menhir.services.erasure_compensation import sweep_erased_twin_compensations

        counts = sweep_erased_twin_compensations(
            _ScopedAdapter(adapter, {ids["queue"]}), coordinator_factory=lambda _a: coord
        )

        assert counts["compensated"] == 1
        assert counts["errors"] == 0
        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, ids["entity"]) is False
        assert _edge_exists(repo, ids["edge"]) is False
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_sweep_ambiguous_twins_deletes_nothing_and_records_ambiguous(
    test_neo4j_repo, tmp_path
) -> None:
    """Two same-name same-namespace candidates: the sweep must not guess. Breaks if ambiguity is
    resolved by picking one (deleting a possibly-innocent memory) or the outcome is not recorded
    via mark_compensation_checked('ambiguous') / not reported in the counts."""
    repo = test_neo4j_repo
    tag = f"test-race-sweep-ambig-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age="PT2H")
        _seed_bare_twin(repo, ids, ids["twin"])
        _seed_bare_twin(repo, ids, ids["twin2"])
        adapter, coord = _coordinator(repo, tmp_path)
        assert adapter.find_unlinked_erased_twin(ids["queue"]) == {"ambiguous": 2}

        from menhir.services.erasure_compensation import sweep_erased_twin_compensations

        counts = sweep_erased_twin_compensations(
            _ScopedAdapter(adapter, {ids["queue"]}), coordinator_factory=lambda _a: coord
        )

        assert counts["ambiguous"] == 1
        assert counts["compensated"] == 0
        assert _exists(repo, ids["twin"]) is True
        assert _exists(repo, ids["twin2"]) is True
        props = _queue_props(repo, ids["queue"])
        assert props is not None
        assert props["checked"] == "ambiguous"
        assert props["resolved"] is None
        # Examined once; it is no longer re-listed on every sweep.
        assert ids["queue"] not in adapter.list_unlinked_erased_queue_nodes(100000)
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_sweep_ignores_recently_erased_queue_node(test_neo4j_repo, tmp_path) -> None:
    """Q erased just now: a worker may still be inside add_episode, so Q must not be listed and
    the sweep must do nothing. Breaks if the PT1H erased_at window is shortened or removed
    (the sweep would erase a twin the live worker is still about to compensate itself)."""
    repo = test_neo4j_repo
    tag = f"test-race-sweep-recent-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age=None)
        _seed_twin_with_derivatives(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)

        assert ids["queue"] not in adapter.list_unlinked_erased_queue_nodes(100000)

        from menhir.services.erasure_compensation import sweep_erased_twin_compensations

        counts = sweep_erased_twin_compensations(
            _ScopedAdapter(adapter, {ids["queue"]}), coordinator_factory=lambda _a: coord
        )

        assert counts == {"retried": 0, "compensated": 0, "ambiguous": 0, "none": 0, "errors": 0}
        assert _exists(repo, ids["twin"]) is True
        assert _exists(repo, ids["entity"]) is True
        props = _queue_props(repo, ids["queue"])
        assert props["checked"] is None and props["resolved"] is None
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_sweep_does_not_match_same_name_twin_in_another_namespace(
    test_neo4j_repo, tmp_path
) -> None:
    """A same-name Episodic in a DIFFERENT group is not Q's twin. Breaks if the group_id equality
    is dropped from find_unlinked_erased_twin (cross-tenant erase). Outcome is recorded 'none'."""
    repo = test_neo4j_repo
    tag = f"test-race-sweep-xns-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age="PT2H")
        _seed_bare_twin(repo, ids, ids["twin"], group_id=ids["other_ns"])
        adapter, coord = _coordinator(repo, tmp_path)
        assert adapter.find_unlinked_erased_twin(ids["queue"]) == {}

        from menhir.services.erasure_compensation import sweep_erased_twin_compensations

        counts = sweep_erased_twin_compensations(
            _ScopedAdapter(adapter, {ids["queue"]}), coordinator_factory=lambda _a: coord
        )

        assert counts["none"] == 1
        assert counts["compensated"] == 0 and counts["ambiguous"] == 0
        assert _exists(repo, ids["twin"]) is True
        props = _queue_props(repo, ids["queue"])
        assert props["checked"] == "none" and props["resolved"] is None
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_sweep_finds_default_namespace_twin(test_neo4j_repo) -> None:
    """Default silo: Q has namespace 'default' and Graphiti writes the twin with group_id ''.
    Breaks if the lookup goes back to an exact group_id match (default never matched, so the
    backstop silently did nothing for most memories)."""
    repo = test_neo4j_repo
    tag = f"test-race-default-ns-{uuidlib.uuid4()}"
    try:
        ids = {**_ids(tag), "ns": "default"}
        _seed_queue(repo, ids, erased_age="PT2H")
        _seed_bare_twin(repo, ids, ids["twin"], group_id="")
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        assert MemoryGraphAdapter(neo4j=repo).find_unlinked_erased_twin(ids["queue"]) == {
            "match": ids["twin"]
        }
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ erase vs. publish interleavings
WORKER = "worker-race-1"


def _seed_enriching_queue(repo: Any, ids: dict[str, str]) -> Any:
    """Queue node claimed by WORKER (ENRICHING, unlinked). Returns the claim instant."""
    repo.execute(
        """
        CREATE (:Episodic {uuid:$queue, namespace:$ns, name:$name, content:$text,
                           processing_state:'ENRICHING', processing_owner:$worker,
                           processing_started_at: datetime() - duration('PT1M'),
                           processing_lease_expires_at: datetime() + duration('PT10M'),
                           test_tag:$tag})
        """,
        params={**ids, "text": SECRET, "worker": WORKER},
    )
    rows = repo.execute(
        "MATCH (n:Episodic {uuid:$queue}) RETURN n.processing_started_at AS claim",
        params=ids,
    )
    return rows[0]["claim"]


def _publish_twin_and_fact(repo: Any, ids: dict[str, str], *, created_age: str | None) -> None:
    """What Graphiti's add_episode leaves behind: twin T (processing_state NULL, same silo, the
    queue node's anchor name), two entities, and a single-source fact listing only T. The fact
    carries the phrase; the entities do not, so the phrase's carriers are only T and the fact."""
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, name:$name, valid_at:datetime(),
                            created_at: CASE WHEN $age IS NULL THEN datetime()
                                             ELSE datetime() - duration($age) END,
                            content:$text, test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'race probe', group_id:$ns, summary:'a probe',
                           created_at:datetime(), test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-4417', group_id:$ns, summary:'a colour',
                           created_at:datetime(), test_tag:$tag})
        CREATE (t)-[:MENTIONS {uuid:$mention, group_id:$ns, created_at:datetime(),
                               test_tag:$tag}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$text,
                                  episodes:[$twin], group_id:$ns, created_at:datetime(),
                                  test_tag:$tag}]->(e2)
        """,
        params={**ids, "text": SECRET, "age": created_age, "mention": f"{ids['tag']}-mention"},
    )


def _phrase_carriers(repo: Any, tag: str) -> list[str]:
    """uuids of every node/relationship of this test that still has a property containing SECRET."""
    nodes = repo.execute(
        "MATCH (n) WHERE n.test_tag = $t AND any(k IN keys(n) WHERE n[k] CONTAINS $p) "
        "RETURN coalesce(n.uuid, '?') AS uuid",
        params={"t": tag, "p": SECRET},
    )
    rels = repo.execute(
        "MATCH ()-[r]->() WHERE r.test_tag = $t AND any(k IN keys(r) WHERE r[k] CONTAINS $p) "
        "RETURN coalesce(r.uuid, '?') AS uuid",
        params={"t": tag, "p": SECRET},
    )
    return sorted([f"node:{r['uuid']}" for r in nodes] + [f"rel:{r['uuid']}" for r in rels])


class _InterleavingAdapter:
    """Delegating adapter that runs ``hook`` once, immediately before the first graph delete (after
    the coordinator's twin lookup and derived preview) -- the window in which a worker finishes."""

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook
        self._fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def delete_memory_detailed(self, *args: Any, **kwargs: Any) -> Any:
        if not self._fired:
            self._fired = True
            self._hook()
        return self._inner.delete_memory_detailed(*args, **kwargs)


@pytest.mark.online
@pytest.mark.xfail(
    strict=True,
    reason="P5: legacy_verify verifier must erase an unfenced late twin (rebuild plan v2 P5)",
)
def test_twin_published_between_twin_lookup_and_graph_delete_does_not_survive_the_erase(
    test_neo4j_repo, tmp_path
) -> None:
    """B-1. Q is ENRICHING. erase_memory looks up Q's twins (none: Q is unlinked), then the worker
    finishes -- twin T and a single-source fact appear and mark_episode_ready links Q to T under
    the worker's valid claim -- and only then does the graph delete run. Q is deleted outright, so
    nothing references T and the sweep (which needs an erased queue node) can never find it.
    Fails if the node delete removes a PENDING/ENRICHING-at-lookup queue node whose link was
    written after the lookup, instead of keeping a tombstone / compensating for the late twin."""
    repo = test_neo4j_repo
    tag = f"test-race-b1-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        claim = _seed_enriching_queue(repo, ids)
        adapter, coord = _coordinator(repo, tmp_path)
        assert adapter.episodic_twin_uuids(ids["queue"]) == []  # the lookup will see nothing
        published: dict[str, Any] = {}

        def worker_finishes() -> None:
            _publish_twin_and_fact(repo, ids, created_age=None)
            published["ready"] = adapter.mark_episode_ready(
                ids["queue"],
                worker_id=WORKER,
                resolved_episode_uuid=ids["twin"],
                nodes_touched=2,
                edges_touched=1,
                claim_started_at=claim,
            )

        coord.graph_adapter = _InterleavingAdapter(adapter, worker_finishes)

        out = coord.erase_memory(ids["queue"])
        # P4: the late link lands after the seal, so the marker (G-q) refuses it; and this claim
        # predates publish_protocol 2, so the erase must not claim success (unfenced_inflight).
        assert published.get("ready") is False, "a link written after the seal must be refused"
        assert out.get("reason") == "erased_incomplete", out
        assert "unfenced_inflight" in out.get("incomplete_reasons", []), out

        from menhir.services.erasure_compensation import sweep_erased_twin_compensations

        sweep_erased_twin_compensations(
            _ScopedAdapter(adapter, {ids["queue"]}), coordinator_factory=lambda _a: coord
        )

        # The unfenced (pre-protocol) twin is removed by the P5 legacy_verify verifier; until
        # then the honest reply above is the guarantee (plan v2 section 2.6) and these fail (xfail).
        assert _exists(repo, ids["twin"]) is False
        assert _edge_exists(repo, ids["edge"]) is False
    finally:
        _cleanup(repo, tag)



@pytest.mark.online
def test_sweep_never_takes_a_live_queue_node_for_the_twin(test_neo4j_repo) -> None:
    """A live Menhir memory with the same anchor name, same namespace and a later created_at must
    not be a candidate, or the backstop would erase a memory the user kept. Breaks if the
    `processing_state IS NULL` guard is dropped (the result becomes ambiguous or the wrong node)."""
    repo = test_neo4j_repo
    tag = f"test-race-live-queue-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_queue(repo, ids, erased_age="PT2H")
        _seed_bare_twin(repo, ids, ids["twin"])
        repo.execute(
            """
            CREATE (:Episodic {uuid:$u, namespace:$ns, group_id:$ns, name:$name,
                               processing_state:'READY', created_at: datetime() - duration('PT1H'),
                               content:'kept memory', test_tag:$tag})
            """,
            params={**ids, "u": ids["other"]},
        )
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        assert MemoryGraphAdapter(neo4j=repo).find_unlinked_erased_twin(ids["queue"]) == {
            "match": ids["twin"]
        }
    finally:
        _cleanup(repo, tag)
