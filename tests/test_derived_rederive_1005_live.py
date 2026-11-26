"""#1005 against Neo4j: the failed-repair requeue, the embed_pending rewrite and re-embed CAS,
and erasure replay over requeued/embed_pending repairs."""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.erasure_coordinator import ErasureCoordinator

pytestmark = [pytest.mark.online]


def _repair(repo, tag: str, key: str) -> dict:
    rows = repo.execute(
        "MATCH (rr:DerivedRederiveRepair {repair_key: $k}) RETURN properties(rr) AS p",
        params={"k": key},
    )
    return dict(rows[0]["p"]) if rows else {}


@pytest.fixture
def tag(test_neo4j_repo):
    value = f"rr-1005-{uuidlib.uuid4().hex[:10]}"
    yield value
    test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})
    test_neo4j_repo.execute(
        "MATCH (rr:DerivedRederiveRepair) WHERE rr.repair_key CONTAINS $t DETACH DELETE rr",
        params={"t": value},
    )


def _failed(repo, key: str, days_ago: int) -> None:
    repo.execute(
        """
        CREATE (:DerivedRederiveRepair {repair_key: $k, item_uuid: $k + '-item', item_kind: 'fact',
                namespace: 'default', status: 'failed', attempts: 5, last_error: 'no_verdict',
                created_at: datetime() - duration({days: $d + 1}),
                last_attempt_at: datetime() - duration({days: $d})})
        """,
        params={"k": key, "d": days_ago},
    )


# --------------------------------------------------------------------------- requeue
def test_requeue_moves_only_failed_repairs_past_the_cooldown(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    old, fresh = f"{tag}-old", f"{tag}-fresh"
    _failed(test_neo4j_repo, old, 8)
    _failed(test_neo4j_repo, fresh, 6)

    rows = q.requeue_failed_derived_rederive(7, 1000, 3)

    mine = {r["repair_key"]: r for r in rows if tag in r["repair_key"]}
    assert set(mine) == {old}
    assert mine[old]["item_kind"] == "fact" and mine[old]["last_error"] == "no_verdict"
    assert mine[old]["requeue_count"] == 1
    requeued = _repair(test_neo4j_repo, tag, old)
    assert requeued["status"] == "pending" and requeued["attempts"] == 0
    assert requeued["deferred_at"] is not None and requeued["requeued_at"] is not None
    assert requeued["last_error"] == "no_verdict"  # kept for audit
    assert _repair(test_neo4j_repo, tag, fresh)["status"] == "failed"
    # Listed for processing again, behind repairs that were already pending.
    assert old in [r["repair_key"] for r in q.list_pending_derived_rederive(100000)]


def test_requeue_is_bounded_and_oldest_first(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    keys = [f"{tag}-{i}" for i in range(3)]
    for i, key in enumerate(keys):
        _failed(test_neo4j_repo, key, 36500 - i)  # keys[0] is the oldest anywhere

    rows = q.requeue_failed_derived_rederive(7, 1, 3)

    assert [r["repair_key"] for r in rows] == [keys[0]]
    assert [_repair(test_neo4j_repo, tag, k)["status"] for k in keys] == [
        "pending", "failed", "failed",
    ]


def test_requeue_ignores_repairs_that_are_not_failed(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    key = f"{tag}-done"
    _failed(test_neo4j_repo, key, 30)
    test_neo4j_repo.execute(
        "MATCH (rr:DerivedRederiveRepair {repair_key: $k}) SET rr.status = 'done'",
        params={"k": key},
    )
    rows = q.requeue_failed_derived_rederive(7, 1000, 3)
    assert key not in [r["repair_key"] for r in rows]
    assert _repair(test_neo4j_repo, tag, key)["status"] == "done"


def test_requeue_skips_repairs_at_the_requeue_cap(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    capped, below = f"{tag}-capped", f"{tag}-below"
    _failed(test_neo4j_repo, capped, 30)
    _failed(test_neo4j_repo, below, 30)
    test_neo4j_repo.execute(
        "MATCH (rr:DerivedRederiveRepair) WHERE rr.repair_key IN [$c, $b] "
        "SET rr.requeue_count = CASE rr.repair_key WHEN $c THEN 3 ELSE 2 END",
        params={"c": capped, "b": below},
    )
    rows = q.requeue_failed_derived_rederive(7, 100000, 3)
    mine = {r["repair_key"]: r for r in rows if tag in r["repair_key"]}
    assert set(mine) == {below} and mine[below]["requeue_count"] == 3
    assert _repair(test_neo4j_repo, tag, capped)["status"] == "failed"
    assert _repair(test_neo4j_repo, tag, capped)["requeue_count"] == 3


# --------------------------------------------------------------------------- embed_pending
def _flagged_fact(repo, tag: str) -> dict:
    ids = {
        "a": f"{tag}-a", "b": f"{tag}-b", "edge": f"{tag}-edge", "key": f"ep\x1f{tag}-edge",
    }
    repo.execute(
        """
        CREATE (a:Entity {uuid: $a, name: 'a', group_id: '', test_tag: $tag})
        CREATE (b:Entity {uuid: $b, name: 'b', group_id: '', test_tag: $tag})
        CREATE (a)-[:RELATES_TO {uuid: $edge, name: 'R', fact: '', episodes: ['e1'],
                                 group_id: '', rederive_pending: true,
                                 rederive_reason: 'source_erased',
                                 rederive_flagged_at: datetime(), test_tag: $tag}]->(b)
        CREATE (:DerivedRederiveRepair {repair_key: $key, item_uuid: $edge, item_kind: 'fact',
                namespace: 'default', status: 'pending', created_at: datetime(),
                test_tag: $tag})
        """,
        params={**ids, "tag": tag},
    )
    return ids


def _edge(repo, edge_uuid: str) -> dict:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.fact AS fact, "
        "r.fact_embedding AS embedding, r.rederive_pending AS pending",
        params={"u": edge_uuid},
    )
    return dict(rows[0])


def _rewrite(q, ids, **kw) -> bool:
    item = q.load_derived_rederive_item("fact", ids["edge"])
    return q.apply_derived_rederive(
        repair_key=ids["key"], item_kind="fact", item_uuid=ids["edge"],
        flagged_at=item["flagged_at"], episodes=item["episodes_raw"], new_text="new fact", **kw,
    )


def _reembed(q, ids, text="new fact", vector=(0.5, 0.25)) -> bool:
    return q.apply_derived_rederive(
        repair_key=ids["key"], item_kind="fact", item_uuid=ids["edge"], flagged_at=None,
        episodes=[], new_text=text, embedding=list(vector), reembed=True,
    )


def test_rewrite_without_embedding_is_searchable_and_embed_pending(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    ids = _flagged_fact(test_neo4j_repo, tag)

    assert _rewrite(q, ids, embedding_missing=True) is True

    edge = _edge(test_neo4j_repo, ids["edge"])
    assert edge["fact"] == "new fact" and edge["pending"] is None and edge["embedding"] is None
    repair = _repair(test_neo4j_repo, tag, ids["key"])
    assert repair["status"] == "embed_pending" and repair.get("finished_at") is None
    assert ids["key"] in [r["repair_key"] for r in q.list_embed_pending_derived_rederive(100000)]
    assert ids["key"] not in [r["repair_key"] for r in q.list_pending_derived_rederive(100000)]
    assert q.defer_derived_rederive(ids["key"]) is True


def test_rewrite_with_embedding_still_finishes_done(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    ids = _flagged_fact(test_neo4j_repo, tag)
    assert _rewrite(q, ids, embedding=[0.1, 0.2]) is True
    assert _repair(test_neo4j_repo, tag, ids["key"])["status"] == "done"
    assert _edge(test_neo4j_repo, ids["edge"])["embedding"] == [0.1, 0.2]


def test_reembed_cas_writes_only_the_current_unflagged_fact(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    ids = _flagged_fact(test_neo4j_repo, tag)
    _rewrite(q, ids, embedding_missing=True)

    assert _reembed(q, ids, text="stale text") is False
    assert _edge(test_neo4j_repo, ids["edge"])["embedding"] is None

    assert _reembed(q, ids) is True
    assert _edge(test_neo4j_repo, ids["edge"])["embedding"] == [0.5, 0.25]
    repair = _repair(test_neo4j_repo, tag, ids["key"])
    assert repair["status"] == "done" and repair["finished_at"] is not None
    assert _reembed(q, ids) is False  # no longer embed_pending


def test_reembed_refuses_a_reflagged_fact_and_obsolete_applies(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    ids = _flagged_fact(test_neo4j_repo, tag)
    _rewrite(q, ids, embedding_missing=True)
    # A later erasure scrubs and re-flags the edge.
    test_neo4j_repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.fact = '', r.rederive_pending = true",
        params={"u": ids["edge"]},
    )
    assert _reembed(q, ids, text="") is False
    assert _edge(test_neo4j_repo, ids["edge"])["embedding"] is None
    assert q.mark_derived_rederive_status(ids["key"], "obsolete") is True
    assert _repair(test_neo4j_repo, tag, ids["key"])["status"] == "obsolete"


def test_embed_failure_counts_on_embed_pending_and_ends_embed_failed(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    ids = _flagged_fact(test_neo4j_repo, tag)
    # Not embed_pending yet ('pending'): the CAS does not match.
    assert q.record_derived_rederive_embed_failure(ids["key"], "embedding_failed", 3) is None
    assert _repair(test_neo4j_repo, tag, ids["key"]).get("embed_attempts") is None
    _rewrite(q, ids, embedding_missing=True)

    assert q.record_derived_rederive_embed_failure(ids["key"], "embedding_failed", 3) == (
        "embed_pending"
    )
    repair = _repair(test_neo4j_repo, tag, ids["key"])
    assert repair["embed_attempts"] == 1 and repair["deferred_at"] is not None
    assert repair.get("finished_at") is None and repair.get("attempts") in (None, 0)
    assert q.record_derived_rederive_embed_failure(ids["key"], "RuntimeError", 3) == "embed_pending"
    assert q.record_derived_rederive_embed_failure(ids["key"], "RuntimeError", 3) == "embed_failed"
    repair = _repair(test_neo4j_repo, tag, ids["key"])
    assert repair["status"] == "embed_failed" and repair["embed_attempts"] == 3
    assert repair["finished_at"] is not None and repair["last_error"] == "RuntimeError"
    # Terminal: no longer listed, counted, re-embedded or requeued; the rewrite stays.
    assert ids["key"] not in [r["repair_key"] for r in q.list_embed_pending_derived_rederive(100000)]
    assert q.record_derived_rederive_embed_failure(ids["key"], "RuntimeError", 3) is None
    assert _reembed(q, ids) is False
    assert ids["key"] not in [
        r["repair_key"] for r in q.requeue_failed_derived_rederive(0, 100000, 3)
    ]
    edge = _edge(test_neo4j_repo, ids["edge"])
    assert edge["fact"] == "new fact" and edge["embedding"] is None and edge["pending"] is None


# --------------------------------------------------------------------------- erasure replay
def test_erasure_replay_finds_requeued_and_embed_pending_repairs(test_neo4j_repo, tag):
    q = MemoryQueryRepository(test_neo4j_repo)
    erased = f"{tag}-erased"
    failed_key = f"{erased}\x1f{tag}-item-failed"
    _failed(test_neo4j_repo, failed_key, 30)
    ids = _flagged_fact(test_neo4j_repo, tag)
    embed_key = f"{erased}\x1f{ids['edge']}"
    test_neo4j_repo.execute(
        "MATCH (rr:DerivedRederiveRepair {repair_key: $old}) SET rr.repair_key = $new",
        params={"old": ids["key"], "new": embed_key},
    )
    ids["key"] = embed_key
    _rewrite(q, ids, embedding_missing=True)
    assert [r["repair_key"] for r in q.requeue_failed_derived_rederive(7, 100000, 3)
            if r["repair_key"] == failed_key] == [failed_key]

    class _Self:
        def _fence_neo4j(self):
            return test_neo4j_repo

    found = ErasureCoordinator._rederive_flagged_uuids(_Self(), {erased})
    assert found == {failed_key + "-item", ids["edge"]}
