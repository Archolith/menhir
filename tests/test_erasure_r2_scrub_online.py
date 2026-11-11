"""R2 online counterexamples: the erase scrub against both text inventories (remediation plan R2).

Each test erases a memory and then searches every graph property and every sidecar text column for
a planted canary (``tests/_erasure_canary.py``). Graph seeded in Graphiti's shape; no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._erasure_canary import all_hits, graph_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import (
    _adapter_and_coordinator,
    _seed_memory,
    _u,
)

pytestmark = pytest.mark.online


def _entity(repo: Any, uuid: str) -> dict[str, Any]:
    rows = repo.execute("MATCH (e:Entity {uuid:$u}) RETURN properties(e) AS p", params={"u": uuid})
    return dict(rows[0]["p"]) if rows else {}


def _edge(repo: Any, uuid: str) -> dict[str, Any]:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid:$u}]->() RETURN properties(r) AS p", params={"u": uuid}
    )
    return dict(rows[0]["p"]) if rows else {}


def test_canary_helper_finds_an_unerased_plant(test_neo4j_repo, tmp_path) -> None:
    """Inverse of every R2 canary assertion: without an erase the helper must report the plant in
    a custom attribute, a string list and a sidecar column. Fails if the scan misses a holder shape."""
    import sqlite3

    repo = test_neo4j_repo
    ns, canary = f"r2c-{_u()}", new_canary("HELPER")
    repo.execute(
        "CREATE (:Entity {uuid:$u, group_id:$ns, namespace:$ns, name:'n', x_custom:$c}) "
        "CREATE (:Entity {uuid:$v, group_id:$ns, namespace:$ns, name:'m', tags:['a', $c]})",
        params={"u": _u(), "v": _u(), "ns": ns, "c": canary},
    )
    db = tmp_path / "helper.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER, payload TEXT)")
    con.execute("INSERT INTO t VALUES (1, ?)", (f'{{"x": "{canary}"}}',))
    con.commit()
    con.close()
    hits = all_hits(repo, db, canary)
    assert {h.prop for h in hits} == {"x_custom", "tags", "payload"}, hits


def test_cf330_scrub_removes_original_content_custom_attributes_and_time_expression(
    test_neo4j_repo, tmp_path
) -> None:
    """CF-330: a shared entity's original_content and custom attribute, and a live edge's
    time_expression, hold the erased memory's words. After the erase the canary is gone everywhere,
    while structural keys (identity, tier and kind flags) and the anchored-time contract's absence
    are as specified. Fails if the entity scrub goes back to summary/content only, if the edge scrub
    leaves time_expression, or if the scrub removes a structural key."""
    repo = test_neo4j_repo
    ns, canary = f"r2-330-{_u()}", new_canary("CF330")
    ids = _seed_memory(repo, ns, canary)
    repo.execute(
        "MATCH (s:Entity {uuid:$s}) SET s.original_content = $c, s.x_custom_attr = $c, "
        "s.scope = 'PERSISTENT', s.self_tier = 'user', s.importance = 0.7, s.is_view = false "
        "WITH s MATCH ()-[r:RELATES_TO {uuid:$e}]->() "
        "SET r.time_expression = 'next week, ' + $c, r.time_kind = 'relative', "
        "r.time_contract = 'v1'",
        params={"s": ids["shared"], "e": ids["edge"], "c": canary},
    )
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)

    out = coord.erase_memory(ids["queue"])

    assert out["reason"] == "erased", out
    assert all_hits(repo, db, canary) == []
    shared = _entity(repo, ids["shared"])
    assert shared["rederive_pending"] is True, shared
    for key in ("uuid", "name", "group_id", "namespace", "scope", "self_tier", "importance",
                "is_view", "created_at"):
        assert key in shared, (key, shared)
    assert "original_content" not in shared and "x_custom_attr" not in shared, shared
    edge = _edge(repo, ids["edge"])
    assert edge and edge["rederive_pending"] is True, edge
    assert not {"time_expression", "time_kind", "time_contract"} & set(edge), edge


def _record_conflict(repo: Any, *, kept_edge: str, cause_edge: str, group_id: str, fact: str,
                     cause_episode: str | None = None) -> None:
    import asyncio

    from menhir.infrastructure.graphiti_edge_invalidation import KeptEdge, record_kept_conflicts

    class _Driver:
        async def execute_query(self, cypher: str, **params: Any) -> None:
            repo.execute(cypher, params=params)

    asyncio.run(record_kept_conflicts(_Driver(), [KeptEdge(
        reason="newer_candidate", kept_edge_uuid=kept_edge, kept_group_id=group_id,
        kept_fact="kept side", cause_edge_uuid=cause_edge, cause_fact=fact, cause_source="agent",
    )], cause_episode_uuid=cause_episode))


def _conflicts(repo: Any, group_id: str) -> int:
    return repo.execute(
        "MATCH (c:EdgeInvalidationConflict {group_id:$g}) RETURN count(c) AS c",
        params={"g": group_id},
    )[0]["c"]


def test_cf331_conflict_caused_by_the_erased_episode_is_deleted_and_counted(
    test_neo4j_repo, tmp_path
) -> None:
    """CF-331: the cause edge never persisted (it was kept against), so only the recorded cause
    episode links the conflict to the erased memory. The cascade deletes it and the reply counts it.
    Fails if the cascade stops matching ``cause_episode_uuids``, or the writer stops recording it."""
    repo = test_neo4j_repo
    ns, canary = f"r2-331-{_u()}", new_canary("CF331")
    ids = _seed_memory(repo, ns, canary)
    other_ns = f"r2-331o-{_u()}"
    _record_conflict(repo, kept_edge=_u(), cause_edge=_u(), group_id=ns, fact=canary,
                     cause_episode=ids["twin"])
    # Another namespace's record naming the same episode uuid is out of this erase's scope.
    _record_conflict(repo, kept_edge=_u(), cause_edge=_u(), group_id=other_ns, fact="other ns",
                     cause_episode=ids["twin"])
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)

    out = coord.erase_memory(ids["queue"])

    assert out["reason"] == "erased", out
    assert out["derived"]["conflicts_deleted"] == 1, out["derived"]
    assert _conflicts(repo, ns) == 0
    assert _conflicts(repo, other_ns) == 1
    assert all_hits(repo, db, canary) == []


def test_cf331_conflict_on_a_scrubbed_live_edge_is_deleted(test_neo4j_repo, tmp_path) -> None:
    """The kept edge survives (another episode still supports it) but is scrubbed; a conflict
    quoting its old fact must go. Fails if the cascade matches only deleted edges."""
    repo = test_neo4j_repo
    ns, canary = f"r2-331b-{_u()}", new_canary("CF331B")
    ids = _seed_memory(repo, ns, canary)
    _record_conflict(repo, kept_edge=ids["edge"], cause_edge=_u(), group_id=ns, fact="cause")
    repo.execute(
        "MATCH (c:EdgeInvalidationConflict {kept_edge_uuid:$e}) SET c.kept_fact = $c",
        params={"e": ids["edge"], "c": canary},
    )
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)
    coord.erase_memory(ids["queue"])
    assert _edge(repo, ids["edge"]).get("rederive_pending") is True
    assert _conflicts(repo, ns) == 0
    assert all_hits(repo, db, canary) == []


def test_cf330_scrub_leaves_an_unrelated_entity_untouched(test_neo4j_repo, tmp_path) -> None:
    """The key removal is bound to the shared set: an entity the erased memory never mentioned
    keeps its custom attribute. Fails if the REMOVE loop escapes the ``shared`` list."""
    repo = test_neo4j_repo
    ns, canary = f"r2-330b-{_u()}", new_canary("CF330B")
    ids = _seed_memory(repo, ns, canary)
    repo.execute(
        "MATCH (o:Entity {uuid:$o}) SET o.x_custom_attr = 'keep me'", params={"o": ids["other"]}
    )
    _, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    coord.erase_memory(ids["queue"])
    assert _entity(repo, ids["other"]).get("x_custom_attr") == "keep me"
    assert graph_hits(repo, canary) == []
