"""#476 online: list a silo by erase ids, then erase a batch exactly as delete_memory would.

Counterexamples on a real Neo4j: every stored spelling of 'default' is listed; twins, structure
rows, evidence projections, erased tombstones and other silos never are (including a
namespace-less node in group 'other', which a coalesce(namespace, 'default') rule would misfile);
a real batch erase leaves the structure graph, other silos and shared entities in place.
Other tests share the default silo, so assertions are about the ids seeded here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from menhir.mcp.tools.ingest.delete_memories import DeleteMemoriesTool
from tests._erasure_canary import all_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import _adapter_and_coordinator, _u

pytestmark = pytest.mark.online


def _all_ids(adapter: Any, namespace: str, kind: str, limit: int = 2) -> list[str]:
    """Page to the end; fails on a repeated id (a broken keyset)."""
    seen: list[str] = []
    after = None
    while True:
        page = adapter.list_namespace_records(namespace, kind=kind, limit=limit, after_id=after)
        ids = [row["id"] for row in page]
        assert not set(ids) & set(seen), "keyset pagination repeated an id"
        seen.extend(ids)
        if len(page) < limit:
            return seen
        after = ids[-1]


def _seed_silos(repo: Any) -> dict[str, str]:
    ids = {k: _u() for k in (
        "q_default", "q_empty", "twin", "legacy", "structure", "projection", "tombstone",
        "other_groupless", "other_ns", "entity", "artifact",
    )}
    repo.execute(
        """
        CREATE (:Episodic {uuid:$q_default, namespace:'default', resolved_episode_uuid:$twin,
                           processing_state:'READY', content:'q default', created_at:datetime()})
        CREATE (:Episodic {uuid:$twin, group_id:'', valid_at:datetime(), content:'twin'})
        CREATE (:Episodic {uuid:$q_empty, namespace:'', processing_state:'READY',
                           content:'q empty', created_at:datetime()})
        CREATE (:Episodic {uuid:$legacy, group_id:'', valid_at:datetime(), content:'legacy'})
        CREATE (:Episodic {uuid:$structure, group_id:'', structure_role:'file', content:'f'})
        CREATE (:Episodic {uuid:$projection, namespace:'default', is_evidence_projection:true,
                           content:'raw turn'})
        CREATE (:Episodic {uuid:$tombstone, namespace:'default', erased_at:datetime(),
                           processing_state:'FAILED', processing_substage:'deleted_by_user'})
        CREATE (:Episodic {uuid:$other_groupless, group_id:'other-476', valid_at:datetime(),
                           content:'other silo, no namespace property'})
        CREATE (:Episodic {uuid:$other_ns, namespace:'other-476', processing_state:'READY',
                           content:'other silo'})
        CREATE (:Entity {uuid:$entity, group_id:'', namespace:'default', name:'e476'})
        CREATE (:Entity {uuid:$artifact, group_id:'', is_artifact:true, name:'a476'})
        """,
        params=ids,
    )
    return ids


@pytest.fixture
def silos(test_neo4j_repo):
    ids = _seed_silos(test_neo4j_repo)
    yield ids
    test_neo4j_repo.execute("MATCH (n) WHERE n.uuid IN $u DETACH DELETE n",
                            params={"u": list(ids.values())})


def test_default_listing_covers_every_spelling_and_nothing_else(
    test_neo4j_repo, tmp_path, silos
) -> None:
    """Fails if the listing misses a 'default', '' or namespace-less legacy memory, or lists a
    twin, structure row, projection, tombstone, entity, artifact or another silo's node."""
    adapter, _, _ = _adapter_and_coordinator(test_neo4j_repo, tmp_path)
    listed = set(_all_ids(adapter, "default", "memory"))

    assert {silos["q_default"], silos["q_empty"], silos["legacy"]} <= listed
    never = {"twin", "structure", "projection", "tombstone", "other_groupless", "other_ns",
             "entity", "artifact"}
    assert not {silos[k] for k in never} & listed

    other = set(_all_ids(adapter, "other-476", "memory"))
    assert other == {silos["other_groupless"], silos["other_ns"]}


def test_listed_silo_matches_the_erase_scope_key(test_neo4j_repo, tmp_path, silos) -> None:
    """Fails if listing and the erase's CF-326 scope check disagree about a listed id's silo."""
    adapter, _, _ = _adapter_and_coordinator(test_neo4j_repo, tmp_path)
    queries = adapter._memory_queries
    for key in ("q_default", "q_empty", "legacy"):
        assert queries.erase_target_namespace_keys(silos[key]) == ["default"], key
    assert queries.erase_target_namespace_keys(silos["other_groupless"]) == ["other-476"]


def test_turn_evidence_and_assertions_are_listed_by_their_erase_ids(test_neo4j_repo, tmp_path) -> None:
    """Fails if a kind lists an id the erase does not accept, skips a null-namespace or
    null-time row, or leaks another silo's row."""
    repo = test_neo4j_repo
    t_null, t_empty, t_other, a_default, a_other = (_u() for _ in range(5))
    repo.execute(
        """
        CREATE (:TurnEvidence {turn_id:$t_null, text:'hi', role:'user'})
        CREATE (:TurnEvidence {turn_id:$t_empty, namespace:'', text:'hi', role:'user',
                               recorded_at:datetime()})
        CREATE (:TurnEvidence {turn_id:$t_other, namespace:'other-476', text:'hi', role:'user'})
        CREATE (:TypedAssertion {assertion_id:$a_default, namespace:'default', attribute:'age',
                                 value:3, recorded_at:datetime()})
        CREATE (:TypedAssertion {assertion_id:$a_other, namespace:'other-476', attribute:'age'})
        """,
        params={"t_null": t_null, "t_empty": t_empty, "t_other": t_other,
                "a_default": a_default, "a_other": a_other},
    )
    try:
        adapter, _, _ = _adapter_and_coordinator(repo, tmp_path)
        turns = set(_all_ids(adapter, "default", "turn_evidence"))
        assert {t_null, t_empty} <= turns and t_other not in turns
        claims = set(_all_ids(adapter, "default", "typed_assertion"))
        assert a_default in claims and a_other not in claims
        queries = adapter._memory_queries
        assert queries.erase_target_namespace_keys(t_null) == ["default"]
        assert queries.erase_target_namespace_keys(a_default) == ["default"]
    finally:
        repo.execute(
            "MATCH (n) WHERE n.turn_id IN $ids OR n.assertion_id IN $ids DETACH DELETE n",
            params={"ids": [t_null, t_empty, t_other, a_default, a_other]},
        )


def test_default_ownership_check_accepts_the_empty_spelling(test_neo4j_repo, tmp_path, silos) -> None:
    """Fails if fetch_memory_by_uuid(namespace='default') treats a node stored as '' as foreign
    (delete_memory would refuse it as 'outside namespace'), or starts matching another silo."""
    adapter, _, _ = _adapter_and_coordinator(test_neo4j_repo, tmp_path)
    assert adapter.fetch_memory_by_uuid(silos["q_empty"], namespace="default") is not None
    assert adapter.fetch_memory_by_uuid(silos["q_default"], namespace="default") is not None
    assert adapter.fetch_memory_by_uuid(silos["other_ns"], namespace="default") is None
    assert adapter.fetch_memory_by_uuid(silos["q_empty"], namespace="other-476") is None


class _Backend:
    """The two backend calls delete_memories makes, bound to the real adapter and saga."""

    def __init__(self, adapter: Any, coord: Any) -> None:
        self.adapter, self.coord = adapter, coord

    async def fetch_memory_by_uuid(self, node_uuid: str, namespace: str | None = None):
        return self.adapter.fetch_memory_by_uuid(node_uuid, namespace=namespace)

    async def erase_memory(self, node_uuid: str, namespace: str | None = None) -> dict:
        return self.coord.erase_memory(node_uuid, caller_namespace=namespace)


def _seed_default_memory(repo: Any, canary: str) -> dict[str, str]:
    """A default-silo memory stored as '' (queue + twin), a live default episode sharing an
    entity with it, a structure row and another silo's memory in the same graph."""
    ids = {k: _u() for k in ("queue", "twin", "live", "shared", "m1", "m2", "structure",
                             "other_q")}
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:'', valid_at:datetime(), content:$canary})
        CREATE (:Episodic {uuid:$queue, namespace:'', resolved_episode_uuid:$twin,
                           processing_state:'READY', content:$canary, created_at:datetime()})
        CREATE (l:Episodic {uuid:$live, group_id:'', valid_at:datetime(), content:'live text'})
        CREATE (s:Entity {uuid:$shared, name:'shared 476', group_id:'', namespace:'default',
                          summary:'shared summary', created_at:datetime()})
        CREATE (t)-[:MENTIONS {uuid:$m1, group_id:''}]->(s)
        CREATE (l)-[:MENTIONS {uuid:$m2, group_id:''}]->(s)
        CREATE (:Entity {uuid:$structure, group_id:'', structure_role:'file', name:'f476.py'})
        CREATE (:Episodic {uuid:$other_q, namespace:'other-476', processing_state:'READY',
                           content:'other silo memory'})
        """,
        params={**ids, "canary": canary},
    )
    return ids


def _exists(repo: Any, uuid: str) -> bool:
    return bool(repo.execute("MATCH (n {uuid:$u}) RETURN n.uuid AS u", params={"u": uuid}))


def test_batch_erases_the_listed_default_memory_and_nothing_else(test_neo4j_repo, tmp_path) -> None:
    """End to end on the real saga: list default, erase the seeded memory through
    delete_memories with namespace='default'. Fails if the '' memory is refused as foreign, if
    its twin or canary survives, or if the structure row, the other silo or the shared entity
    (still supported by a live episode) is deleted. A re-run must delete nothing more."""
    repo = test_neo4j_repo
    canary = new_canary("D476")
    ids = _seed_default_memory(repo, canary)
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    try:
        listed = set(_all_ids(adapter, "default", "memory", limit=100))
        assert ids["queue"] in listed and ids["twin"] not in listed
        assert ids["structure"] not in listed and ids["other_q"] not in listed

        tool = DeleteMemoriesTool()
        tool.get_backend = lambda: _Backend(adapter, coord)  # type: ignore[method-assign]
        preview = json.loads(asyncio.run(tool.endpoint([ids["queue"]], namespace="default")))
        assert preview["totals"] == {"would_erase": 1} and _exists(repo, ids["queue"])

        result = json.loads(asyncio.run(
            tool.endpoint([ids["queue"]], namespace="default", dry_run=False)))
        assert result["totals"] == {"erased": 1}, result

        assert not _exists(repo, ids["queue"]) and not _exists(repo, ids["twin"])
        assert all_hits(repo, db, canary) == []
        for survivor in ("live", "shared", "structure", "other_q"):
            assert _exists(repo, ids[survivor]), survivor

        again = json.loads(asyncio.run(
            tool.endpoint([ids["queue"]], namespace="default", dry_run=False)))
        assert set(again["totals"]) <= {"nothing_to_erase", "graph_already_absent"}, again
        for survivor in ("live", "shared", "structure", "other_q"):
            assert _exists(repo, ids[survivor]), survivor
    finally:
        repo.execute("MATCH (n) WHERE n.uuid IN $u DETACH DELETE n",
                     params={"u": list(ids.values())})


def test_batch_refuses_another_silos_memory_under_default(test_neo4j_repo, tmp_path) -> None:
    """Fails if delete_memories(namespace='default') erases a memory proven to be in another silo."""
    repo = test_neo4j_repo
    other = _u()
    repo.execute("CREATE (:Episodic {uuid:$u, namespace:'other-476', processing_state:'READY',"
                 " content:'keep me'})", params={"u": other})
    try:
        adapter, coord, _ = _adapter_and_coordinator(repo, tmp_path)
        tool = DeleteMemoriesTool()
        tool.get_backend = lambda: _Backend(adapter, coord)  # type: ignore[method-assign]
        result = json.loads(asyncio.run(
            tool.endpoint([other], namespace="default", dry_run=False)))
        assert result["totals"] == {"outside_namespace": 1}
        assert _exists(repo, other)
    finally:
        repo.execute("MATCH (n {uuid:$u}) DETACH DELETE n", params={"u": other})
