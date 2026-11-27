"""Online counterexamples for compress and rehydrate against a real graph (no LLM calls).

Compress replaces exactly the body it summarised, only while the node still qualifies;
rehydrate rebuilds from the body the last compression replaced and restores it on the recall path.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository, summary_sha256
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.lifecycle_service import LifecycleService
from tests.test_lifecycle_service import _FakeLLM

pytestmark = pytest.mark.online

_MIN_DAYS = 30.0


def _u() -> str:
    return str(uuid4())


def _props(repo: Any, uuid: str) -> dict[str, Any]:
    rows = repo.execute("MATCH (e:Entity {uuid:$u}) RETURN properties(e) AS p", params={"u": uuid})
    return dict(rows[0]["p"]) if rows else {}


def _seed(repo: Any, *, content: str = "the body", freshness: str = "ACTIVE") -> tuple[str, str]:
    ns, uid = f"decay-{_u()}", _u()
    repo.execute(
        """
        CREATE (:Entity {uuid:$u, name:'node', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:$fresh, content:$content, summary:'s',
                         created_at:datetime('2020-01-01'), last_accessed:datetime('2020-01-01')})
        """,
        params={"u": uid, "ns": ns, "fresh": freshness, "content": content},
    )
    return uid, ns


def _age(repo: Any, uid: str) -> None:
    repo.execute("MATCH (e:Entity {uuid:$u}) SET e.last_accessed = datetime('2020-01-01')",
                 params={"u": uid})


def _service(repo: Any, graphiti: Any, tmp_path: Any, llm: Any) -> LifecycleService:
    return LifecycleService(
        graph_adapter=MemoryGraphAdapter(neo4j=repo), graphiti_client=graphiti, llm=llm,
        pending_actions=PendingActionStore(db_path=tmp_path / "pending.db"),
    )


def _compress(repo: Any, uid: str, summary: str, read_content: str | None) -> bool:
    return ConsolidationRepository(repo).compress_node(
        uid, summary, read_content=read_content, min_days_since_accessed=_MIN_DAYS, fence_key=None,
    )


def test_compress_replaces_the_read_body_and_keeps_it_as_original(test_neo4j_repo) -> None:
    uid, _ = _seed(test_neo4j_repo)
    assert _compress(test_neo4j_repo, uid, "gist", "the body") is True
    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"], p["original_content"]) == ("COMPRESSED", "gist", "the body")


def test_a_recall_touch_between_the_read_and_the_compress_refuses_it(test_neo4j_repo) -> None:
    """Fails on main: compress never re-checks recency."""
    uid, _ = _seed(test_neo4j_repo)
    test_neo4j_repo.execute("MATCH (e:Entity {uuid:$u}) SET e.last_accessed = datetime()",
                            params={"u": uid})

    assert _compress(test_neo4j_repo, uid, "gist", "the body") is False

    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "the body")


def test_a_content_change_between_the_read_and_the_compress_refuses_it(test_neo4j_repo) -> None:
    """Fails on main: the summary of the old body overwrites the new one."""
    uid, _ = _seed(test_neo4j_repo)
    test_neo4j_repo.execute("MATCH (e:Entity {uuid:$u}) SET e.content = 'a newer body'",
                            params={"u": uid})

    assert _compress(test_neo4j_repo, uid, "gist", "the body") is False

    assert _props(test_neo4j_repo, uid)["content"] == "a newer body"


def test_compress_writes_the_marker_that_binds_the_base_to_the_summary(test_neo4j_repo) -> None:
    uid, _ = _seed(test_neo4j_repo)
    assert _compress(test_neo4j_repo, uid, "gist", "the body") is True
    assert _props(test_neo4j_repo, uid)["compressed_summary_sha256"] == summary_sha256("gist")


def test_compress_of_a_node_that_read_blank_content_summarises_the_summary(test_neo4j_repo) -> None:
    uid, ns = _u(), f"decay-{_u()}"
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:$u, name:'n', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'ACTIVE', summary:'only a summary', created_at:datetime('2020-01-01')})",
        params={"u": uid, "ns": ns},
    )
    repo = ConsolidationRepository(test_neo4j_repo)

    assert repo.compress_node(uid, "gist", read_content=None, read_summary="a stale summary",
                              min_days_since_accessed=_MIN_DAYS, fence_key=None) is False
    assert repo.compress_node(uid, "gist", read_content=None, read_summary="only a summary",
                              min_days_since_accessed=_MIN_DAYS, fence_key=None) is True

    assert _props(test_neo4j_repo, uid)["original_content"] == "only a summary"


@pytest.mark.asyncio
async def test_compress_rehydrate_x_compress_rehydrate_y_keeps_x(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    """Fails on main: the second rehydrate rebuilds from the oldest archived body (here the
    node has no archive at all, so main merges into the summary and loses the body)."""
    uid, _ = _seed(test_neo4j_repo, content="orig")
    llm = _FakeLLM(merge_return="orig + X")
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, llm)

    assert _compress(test_neo4j_repo, uid, "gist 1", "orig") is True
    assert await svc.rehydrate_node(uid, new_context="X", source_episode_uuid="ep-1") is True
    assert _props(test_neo4j_repo, uid)["content"] == "orig + X"

    _age(test_neo4j_repo, uid)
    assert _compress(test_neo4j_repo, uid, "gist 2", "orig + X") is True
    llm.merge_return = "orig + X + Y"
    assert await svc.rehydrate_node(uid, new_context="Y", source_episode_uuid="ep-2") is True

    assert llm.merge_calls == [("orig", "X"), ("orig + X", "Y")]
    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "orig + X + Y")


@pytest.mark.asyncio
async def test_a_recall_rehydrate_restores_the_original_body(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    """Fails on main: the node goes ACTIVE still holding the summary."""
    uid, _ = _seed(test_neo4j_repo, content="the full original body")
    assert _compress(test_neo4j_repo, uid, "gist", "the full original body") is True
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node(uid) is True

    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "the full original body")


@pytest.mark.asyncio
async def test_a_recall_rehydrate_without_a_stored_original_only_flips_state(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    uid, _ = _seed(test_neo4j_repo, content="gist", freshness="COMPRESSED")
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node(uid) is True

    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "gist")


def test_a_rederive_pending_node_is_not_restored(test_neo4j_repo) -> None:
    """The text-copying write refuses an erased (rederive_pending) node on both paths."""
    uid, _ = _seed(test_neo4j_repo, content="the full original body")
    assert _compress(test_neo4j_repo, uid, "gist", "the full original body") is True
    test_neo4j_repo.execute("MATCH (e:Entity {uuid:$u}) SET e.rederive_pending = true",
                            params={"u": uid})
    repo = ConsolidationRepository(test_neo4j_repo)

    assert repo.complete_rehydration(
        uid, "the full original body", read_summary="gist",
        read_base="the full original body") is False
    assert repo.complete_rehydration(
        uid, "merged text", read_summary="gist", read_base="the full original body") is False

    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("COMPRESSED", "gist")


@pytest.mark.asyncio
async def test_an_erased_node_is_not_restored_through_the_service(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    """The state read withholds a rederive_pending node's text even while original_content is
    still on the node, so no restore is attempted and the content stays the summary."""
    uid, _ = _seed(test_neo4j_repo, content="erased body")
    assert _compress(test_neo4j_repo, uid, "gist", "erased body") is True
    test_neo4j_repo.execute("MATCH (e:Entity {uuid:$u}) SET e.rederive_pending = true",
                            params={"u": uid})
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, _FakeLLM())

    await svc.rehydrate_node(uid)

    assert _props(test_neo4j_repo, uid)["content"] == "gist"


def test_rehydrate_refuses_when_the_summary_or_base_moved_since_the_read(test_neo4j_repo) -> None:
    uid, _ = _seed(test_neo4j_repo, content="body")
    assert _compress(test_neo4j_repo, uid, "gist", "body") is True
    repo = ConsolidationRepository(test_neo4j_repo)

    assert repo.complete_rehydration(uid, "merged", read_summary="stale", read_base="body") is False
    assert repo.complete_rehydration(uid, "merged", read_summary="gist", read_base="stale") is False
    assert repo.complete_rehydration(uid, "body", read_summary="stale", read_base="body") is False
    assert _props(test_neo4j_repo, uid)["freshness"] == "COMPRESSED"

    assert repo.complete_rehydration(uid, "merged", read_summary="gist", read_base="body") is True


def test_a_state_flip_does_not_compare_the_read_text(test_neo4j_repo) -> None:
    """No text is copied, so a moved summary or base does not refuse the flip."""
    uid, _ = _seed(test_neo4j_repo, content="body")
    assert _compress(test_neo4j_repo, uid, "gist", "body") is True

    assert ConsolidationRepository(test_neo4j_repo).complete_rehydration(
        uid, read_summary="stale", read_base="stale") is True

    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "gist")


def test_an_absorb_into_a_compressed_keep_extends_its_base_and_marker(test_neo4j_repo) -> None:
    """The keep's replaced body gets the absorbed block too, and the marker follows the new
    content, so a later rehydrate restores the block."""
    ns, keep, gone, group = f"decay-{_u()}", _u(), _u(), _u()
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:$k, name:'keep', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'COMPRESSED', content:'gist', original_content:'the body', "
        "compressed_summary_sha256:$sha, conflict_group_id:$cg, "
        "created_at:datetime('2020-01-01')}), "
        "(:Entity {uuid:$g, name:'gone', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'ACTIVE', content:'other text', conflict_group_id:$cg, "
        "created_at:datetime('2020-01-01')})",
        params={"k": keep, "g": gone, "ns": ns, "cg": group, "sha": summary_sha256("gist")},
    )

    ConsolidationRepository(test_neo4j_repo).resolve_conflict_group(
        group, "replace", keep_uuid=keep, remove_uuid=gone, namespace=ns,
    )

    p = _props(test_neo4j_repo, keep)
    assert p["freshness"] == "COMPRESSED"
    assert p["original_content"].startswith("the body") and "other text" in p["original_content"]
    assert p["compressed_summary_sha256"] == summary_sha256(p["content"])
    assert p["content"].startswith("gist") and "other text" in p["content"]


@pytest.mark.asyncio
async def test_a_marked_keep_restores_the_absorbed_block(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    keep, gone, group, ns = _u(), _u(), _u(), f"decay-{_u()}"
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:$k, name:'keep', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'COMPRESSED', content:'gist', original_content:'the body', "
        "compressed_summary_sha256:$sha, conflict_group_id:$cg, created_at:datetime('2020-01-01')}), "
        "(:Entity {uuid:$g, name:'gone', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'ACTIVE', content:'other text', conflict_group_id:$cg, "
        "created_at:datetime('2020-01-01')})",
        params={"k": keep, "g": gone, "ns": ns, "cg": group, "sha": summary_sha256("gist")},
    )
    ConsolidationRepository(test_neo4j_repo).resolve_conflict_group(
        group, "replace", keep_uuid=keep, remove_uuid=gone, namespace=ns,
    )
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node(keep) is True

    p = _props(test_neo4j_repo, keep)
    assert p["freshness"] == "ACTIVE"
    assert p["content"].startswith("the body") and "other text" in p["content"]


@pytest.mark.asyncio
async def test_an_unmarked_keep_absorbs_without_blessing_its_base(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    """A legacy, multi-cycle keep: its stored base is stale, so the absorb leaves the base alone,
    leaves the marker null, and a contextless rehydrate is a flip keeping summary plus block."""
    keep, gone, group, ns = _u(), _u(), _u(), f"decay-{_u()}"
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:$k, name:'keep', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'COMPRESSED', content:'gist', original_content:'stale first body', "
        "rehydration_count:1, conflict_group_id:$cg, created_at:datetime('2020-01-01')}), "
        "(:Entity {uuid:$g, name:'gone', group_id:$ns, namespace:$ns, scope:'PERSISTENT', "
        "freshness:'ACTIVE', content:'other text', conflict_group_id:$cg, "
        "created_at:datetime('2020-01-01')})",
        params={"k": keep, "g": gone, "ns": ns, "cg": group},
    )
    ConsolidationRepository(test_neo4j_repo).resolve_conflict_group(
        group, "replace", keep_uuid=keep, remove_uuid=gone, namespace=ns,
    )
    p = _props(test_neo4j_repo, keep)
    assert p["original_content"] == "stale first body"
    assert p.get("compressed_summary_sha256") is None
    merged = p["content"]
    svc = _service(test_neo4j_repo, stub_graphiti_client, tmp_path, _FakeLLM())

    assert await svc.rehydrate_node(keep) is True

    p = _props(test_neo4j_repo, keep)
    assert (p["freshness"], p["content"]) == ("ACTIVE", merged)
    assert "other text" in merged


def test_the_ratchet_never_passes_the_cap(test_neo4j_repo) -> None:
    """Fails on main: 4.95 -> 5.05 because the cap test ran before the step."""
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid:'ra'})-[:RELATES_TO {uuid:'er1', weight:4.95}]->(:Entity {uuid:'rb'}), "
        "(:Entity {uuid:'rc'})-[:RELATES_TO {uuid:'er2', weight:4.95}]->(:Entity {uuid:'rd'})"
    )
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.increment_edge_weight("er1")
    repo.increment_edge_weights(["er2"])

    rows = test_neo4j_repo.execute("MATCH ()-[r]->() RETURN r.weight AS w")
    assert rows and all(float(r["w"]) <= 5.0 for r in rows)


def test_a_gone_neighbour_is_not_counted_as_persistent(test_neo4j_repo) -> None:
    """Fails on main: count_persistent_edges counts every PERSISTENT/PROMOTED neighbour."""
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "CREATE (c:Entity {uuid:'pc', scope:'SESSION'}) "
        "CREATE (c)-[:RELATES_TO {uuid:'p1'}]->(:Entity {uuid:'live', scope:'PERSISTENT', freshness:'ACTIVE'}) "
        "CREATE (c)-[:RELATES_TO {uuid:'p2'}]->(:Entity {uuid:'gone', scope:'PERSISTENT', freshness:'GONE'})"
    )

    assert ConsolidationRepository(test_neo4j_repo).count_persistent_edges("pc") == 1


def _write_blocked_then_refused(repo: Any, uid: str, attempt: Any) -> bool:
    """Run ``attempt`` while a second session holds an uncommitted content write on the node.

    The attempt must be waiting on the node when the writer commits; returns what it returned.
    """
    import threading
    import time

    result: list[bool] = []
    driver = repo._get_driver()
    with driver.session(database=repo.database) as session:
        tx = session.begin_transaction()
        tx.run("MATCH (n:Entity {uuid:$u}) SET n.content = 'changed'", u=uid).consume()
        worker = threading.Thread(target=lambda: result.append(attempt()))
        worker.start()
        time.sleep(1.0)
        assert worker.is_alive(), "the write did not wait for the node lock"
        tx.commit()
    worker.join(timeout=30)
    assert not worker.is_alive()
    return result[0]


def test_a_compress_waiting_on_a_content_write_refuses_after_it_commits(test_neo4j_repo) -> None:
    """Neo4j does not re-check a WHERE after a write waits for a lock: the lock-then-recheck
    prefix is what makes the compress see the committed content."""
    uid, _ = _seed(test_neo4j_repo)

    refused = _write_blocked_then_refused(
        test_neo4j_repo, uid, lambda: _compress(test_neo4j_repo, uid, "gist", "the body"))

    assert refused is False
    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("ACTIVE", "changed")


def test_a_rehydrate_waiting_on_a_content_write_refuses_after_it_commits(test_neo4j_repo) -> None:
    uid, _ = _seed(test_neo4j_repo, content="the body")
    assert _compress(test_neo4j_repo, uid, "gist", "the body") is True

    refused = _write_blocked_then_refused(
        test_neo4j_repo, uid,
        lambda: ConsolidationRepository(test_neo4j_repo).complete_rehydration(
            uid, "the body", read_summary="gist", read_base="the body"))

    assert refused is False
    p = _props(test_neo4j_repo, uid)
    assert (p["freshness"], p["content"]) == ("COMPRESSED", "changed")
