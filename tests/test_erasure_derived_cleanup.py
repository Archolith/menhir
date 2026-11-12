"""Derived-fact / derived-entity cleanup on explicit memory erasure (#423).

Erasing a memory erases its queue node and its Graphiti twin T. Anything Graphiti derived from T
must then be handled: single-source facts/entities are deleted, shared ones are flagged for
re-derivation with a durable repair node, unknown-provenance entities are left alone and reported,
and views/artifacts/TEMPORAL/structure entities are never touched.

The graph is seeded directly with Cypher in the shape Graphiti writes (no LLM, no enrichment run),
modelled on tests/test_erasure_derived_facts_repro.py. All tests need a live Neo4j.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuidlib
from pathlib import Path
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    ERASED,
    ERASED_INCOMPLETE,
    ERASURE_KIND,
    PREPARE_FAILED,
    ErasureCoordinator,
)

SECRET = "The derived-cleanup probe's favourite colour is teal-9921"
US = "\x1f"  # repair_key separator


# ------------------------------------------------------------------ helpers
def _ids(tag: str, *, canonical: bool = False) -> dict[str, str]:
    # canonical=True: real uuids, as production mints them. The merge-audit scrub ignores any
    # needle that is not a canonical uuid (rebuild plan v2 §3.4).
    def node(suffix: str) -> str:
        return str(uuidlib.uuid4()) if canonical else f"{tag}-{suffix}"

    return {
        "tag": tag,
        "ns": f"{tag}-ns",
        "queue": node("queue"),
        "twin": node("graphiti"),
        "entity": node("entity"),
        "entity2": node("entity2"),
        "edge": node("edge"),
        "other": node("other"),
    }


def _seed_base(
    repo: Any, tag: str, edge_episodes: list[str] | None = None, *, canonical: bool = False
) -> dict[str, str]:
    """Queue node Q + twin T + entity E (mentioned by T) + fact edge E->E2 (E2 not mentioned)."""
    ids = _ids(tag, canonical=canonical)
    episodes = [ids["twin"]] if edge_episodes is None else edge_episodes
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, valid_at:datetime(), content:$text,
                            test_tag:$tag})
        CREATE (:Episodic {uuid:$queue, namespace:$ns, resolved_episode_uuid:$twin,
                           processing_state:'READY', content:$text, test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'derived probe', group_id:$ns,
                           summary:$text, created_at:datetime(), test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-9921', group_id:$ns,
                           summary:'a colour', created_at:datetime(), test_tag:$tag})
        CREATE (t)-[:MENTIONS {uuid:$mention, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$text,
                                  episodes:$episodes, group_id:$ns, created_at:datetime(),
                                  test_tag:$tag}]->(e2)
        """,
        params={**ids, "text": SECRET, "mention": f"{tag}-mention", "episodes": episodes},
    )
    return ids


def _make_other_twin(repo: Any, ids: dict[str, str]) -> None:
    """Another live :Episodic in the same group (a second source for derived data)."""
    repo.execute(
        "CREATE (:Episodic {uuid:$other, group_id:$ns, valid_at:datetime(), "
        "content:'another memory', test_tag:$tag})",
        params=ids,
    )


def _coordinator(repo: Any, tmp_path: Path):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(neo4j=repo)
    db = tmp_path / "derived-cleanup.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    journal = GraphOperationsJournal(db_path=db)
    coord = ErasureCoordinator(
        graph_adapter=adapter, journal=journal, subjects=ErasureSubjectStore(db_path=db)
    )
    return adapter, coord, journal, db


def _cleanup(repo: Any, tag: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
    repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t DETACH DELETE r",
        params={"t": tag},
    )


def _exists(repo: Any, node_uuid: str) -> bool:
    rows = repo.execute(
        "MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid}
    )
    return int(rows[0]["c"]) > 0


def _edge(repo: Any, edge_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() "
        "RETURN r.episodes AS episodes, r.rederive_pending AS pending, r.fact AS fact",
        params={"u": edge_uuid},
    )
    return dict(rows[0]) if rows else None


def _entity(repo: Any, entity_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH (e:Entity {uuid: $u}) RETURN e.rederive_pending AS pending, "
        "e.summary AS summary, e.merge_audit AS merge_audit, "
        "e.merge_irreversible AS irreversible",
        params={"u": entity_uuid},
    )
    return dict(rows[0]) if rows else None


def _repairs(repo: Any, tag: str) -> list[dict[str, Any]]:
    rows = repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t "
        "RETURN r.item_kind AS kind, r.item_uuid AS item, r.status AS status, "
        "r.repair_key AS key",
        params={"t": tag},
    )
    return [dict(r) for r in rows]


def _sql(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ tests
@pytest.mark.online
def test_single_source_entity_and_fact_are_deleted_and_recorded(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if the cascade stops deleting single-source derived items, miscounts them, records
    them as NODE_UUID, or drops fence_uuids from the journaled request."""
    repo = test_neo4j_repo
    tag = f"test-dcl-single-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        _adapter, coord, _journal, db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == ERASED
        assert outcome["derived"]["entities_deleted"] == 1
        assert outcome["derived"]["edges_deleted"] == 1
        assert _exists(repo, ids["queue"]) is False
        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, ids["entity"]) is False
        assert _edge(repo, ids["edge"]) is None
        # The other endpoint was never derived from T alone: it is not mentioned, so it stays.
        assert _exists(repo, ids["entity2"]) is True

        rows = _sql(
            db,
            "SELECT subject_type, subject_value FROM erasure_subjects WHERE op_id = ?",
            (outcome["op_id"],),
        )
        derived = {v for t, v in rows if t == "DERIVED_UUID"}
        node = {v for t, v in rows if t == "NODE_UUID"}
        assert {ids["entity"], ids["edge"]} <= derived
        assert node == {ids["queue"], ids["twin"]}

        [(kind, request_json)] = _sql(
            db,
            "SELECT operation_kind, request_json FROM graph_operations WHERE op_id = ?",
            (outcome["op_id"],),
        )
        assert kind == ERASURE_KIND
        assert json.loads(request_json)["fence_uuids"] == [ids["entity"]]
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_shared_fact_survives_stripped_and_flagged(test_neo4j_repo, tmp_path) -> None:
    """Breaks if an edge that another episode also supports is deleted, keeps the erased episode
    uuid, or is not flagged with a fact repair node."""
    repo = test_neo4j_repo
    tag = f"test-dcl-sharedfact-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_base(repo, tag, edge_episodes=[ids["twin"], ids["other"]])
        _make_other_twin(repo, ids)
        _adapter, coord, _journal, _db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == ERASED
        edge = _edge(repo, ids["edge"])
        assert edge is not None, "a fact still supported by a live episode must survive"
        assert list(edge["episodes"]) == [ids["other"]]
        assert edge["pending"] is True
        assert outcome["derived"]["edges_flagged"] == 1
        assert outcome["derived"]["edges_deleted"] == 0
        fact_repairs = [r for r in _repairs(repo, tag) if r["kind"] == "fact"]
        assert len(fact_repairs) == 1
        assert fact_repairs[0]["item"] == ids["edge"]
        assert fact_repairs[0]["status"] == "pending"
        target, edge_part = fact_repairs[0]["key"].split(US)
        assert target in (ids["queue"], ids["twin"])
        assert edge_part == ids["edge"]
        assert _exists(repo, ids["other"]) is True
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_shared_entity_survives_flagged(test_neo4j_repo, tmp_path) -> None:
    """Breaks if an entity also mentioned by a live episode is deleted, or is not flagged with an
    entity repair node, or has its summary rewritten by the cascade."""
    repo = test_neo4j_repo
    tag = f"test-dcl-sharedent-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        _make_other_twin(repo, ids)
        # E is also mentioned by the live other episode; drop the fact edge so only the mention
        # decides sharing.
        repo.execute(
            "MATCH (o:Episodic {uuid:$other}), (e:Entity {uuid:$entity}) "
            "CREATE (o)-[:MENTIONS {uuid:$m, group_id:$ns}]->(e)",
            params={**ids, "m": f"{tag}-mention2"},
        )
        repo.execute(
            "MATCH ()-[r:RELATES_TO {uuid:$edge}]->() DELETE r", params=ids
        )
        _adapter, coord, _journal, _db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == ERASED
        ent = _entity(repo, ids["entity"])
        assert ent is not None, "an entity still mentioned by a live episode must survive"
        assert ent["pending"] is True
        # Owner ruling 2026-11-09: scrub at erase; the rederive job rebuilds it from live sources.
        assert ent["summary"] == ""
        assert outcome["derived"]["entities_flagged"] == 1
        assert outcome["derived"]["entities_deleted"] == 0
        ent_repairs = [r for r in _repairs(repo, tag) if r["kind"] == "entity"]
        assert len(ent_repairs) == 1
        assert ent_repairs[0]["item"] == ids["entity"]
        assert ent_repairs[0]["status"] == "pending"
        assert ent_repairs[0]["key"].split(US)[1] == ids["entity"]
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_unknown_provenance_entity_and_edge_left_untouched(test_neo4j_repo, tmp_path) -> None:
    """Breaks if an entity whose incident fact edge has no episodes provenance is deleted or
    flagged, or if the outcome stops reporting it (reason must degrade to erased_incomplete)."""
    repo = test_neo4j_repo
    tag = f"test-dcl-unknown-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag, edge_episodes=[])
        _adapter, coord, _journal, _db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == ERASED_INCOMPLETE
        assert outcome["derived"]["unknown_provenance"] >= 1
        assert outcome["derived"]["entities_deleted"] == 0
        assert outcome["derived"]["edges_deleted"] == 0
        ent = _entity(repo, ids["entity"])
        assert ent is not None and ent["pending"] is None
        edge = _edge(repo, ids["edge"])
        assert edge is not None
        assert list(edge["episodes"]) == []
        assert edge["pending"] is None
        assert _repairs(repo, tag) == []
        # The memory's own two nodes are still erased.
        assert _exists(repo, ids["queue"]) is False
        assert _exists(repo, ids["twin"]) is False
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_merge_audit_entry_tombstoned_and_survivor_marked_irreversible(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if the scrub leaves the erased episode uuid or the copied property text in a
    survivor's merge_audit, touches unrelated entries, or omits merge_irreversible."""
    repo = test_neo4j_repo
    tag = f"test-dcl-audit-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag, canonical=True)
        survivor, absorbed = f"{tag}-survivor", f"{tag}-absorbed"
        unrelated_ep = f"{tag}-unrelated-ep"
        first = json.dumps(
            {
                "absorbed_uuid": absorbed,
                "survivor_uuid": survivor,
                "properties": {"summary": SECRET},
                "mentioned_by_episodes": [ids["twin"]],
            }
        )
        second = json.dumps(
            {
                "absorbed_uuid": f"{tag}-absorbed2",
                "survivor_uuid": survivor,
                "properties": {"summary": "harmless"},
                "mentioned_by_episodes": [unrelated_ep],
            }
        )
        repo.execute(
            "CREATE (:Entity {uuid:$s, name:'survivor', group_id:$ns, summary:'s', "
            "merge_audit:$audit, test_tag:$tag})",
            params={**ids, "s": survivor, "audit": [first, second]},
        )
        _adapter, coord, _journal, _db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["derived"]["merge_audits_tombstoned"] == 1
        surv = _entity(repo, survivor)
        assert surv is not None
        audit = list(surv["merge_audit"])
        assert len(audit) == 2
        assert SECRET not in audit[0]
        assert ids["twin"] not in audit[0]
        assert json.loads(audit[0])["erased"] is True
        assert audit[1] == second
        assert surv["irreversible"] is True
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_counterexample_merge_holding_the_entity_fails_prepare_and_deletes_nothing(
    test_neo4j_repo, tmp_path
) -> None:
    """Fence counterexample: a PREPARED ENTITY_MERGE already locks the single-source entity.
    Breaks if fence_uuids stop being participant locks (the erasure would then delete an entity
    mid-merge) or if PREPARE failure is allowed to proceed to graph deletes."""
    repo = test_neo4j_repo
    tag = f"test-dcl-fence-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        _adapter, coord, journal, db = _coordinator(repo, tmp_path)
        bystander = f"{tag}-bystander"
        journal.prepare(
            operation_kind="ENTITY_MERGE",
            request_json=json.dumps(
                {"survivor_uuid": bystander, "absorbed_uuid": ids["entity"]}, sort_keys=True
            ),
            target_uuid=ids["entity"],
            target_key=f"{bystander}|{ids['entity']}",
            op_id=uuidlib.uuid4().hex,
        )

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == PREPARE_FAILED
        for node_uuid in (ids["queue"], ids["twin"], ids["entity"], ids["entity2"]):
            assert _exists(repo, node_uuid) is True, node_uuid
        assert _edge(repo, ids["edge"]) is not None
        assert _sql(
            db,
            "SELECT count(*) FROM graph_operations WHERE operation_kind = ?",
            (ERASURE_KIND,),
        ) == [(0,)]
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_counterexample_entity_not_in_prepare_allowlist_is_flagged_not_deleted(
    test_neo4j_repo, tmp_path
) -> None:
    """Allowlist counterexample: an entity that became single-source after the preview was not
    fenced. Breaks if the cascade deletes any single-source entity regardless of the fenced
    allowlist (deletable_entity_uuids)."""
    repo = test_neo4j_repo
    tag = f"test-dcl-allowlist-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        adapter, coord, _journal, _db = _coordinator(repo, tmp_path)
        real_preview = adapter.erasure_derived_preview

        def stale_preview(erased_uuids, *, twin_found=None):
            out = dict(real_preview(erased_uuids, twin_found=twin_found))
            # Sanity: with a fresh preview the entity WOULD have been fenced for deletion.
            assert out["entities_delete"] == [ids["entity"]]
            out["entities_delete"] = []
            return out

        adapter.erasure_derived_preview = stale_preview  # type: ignore[method-assign]
        try:
            outcome = coord.erase_memory(ids["queue"])
        finally:
            del adapter.erasure_derived_preview  # restore the class method

        assert outcome["derived"]["entities_deleted"] == 0
        assert outcome["derived"]["entities_flagged"] == 1
        ent = _entity(repo, ids["entity"])
        assert ent is not None, "an unfenced entity must never be deleted"
        assert ent["pending"] is True
        assert [r["kind"] for r in _repairs(repo, tag) if r["item"] == ids["entity"]] == [
            "entity"
        ]
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_replay_flags_instead_of_deleting_an_entity_that_became_shared(
    test_neo4j_repo, tmp_path
) -> None:
    """Replay counterexample. The twin delete crashes, leaving the erasure PREPARED with the
    entity still fenced; meanwhile a live episode starts mentioning it. Replay must re-evaluate
    sharing in the graph (never delete by the recorded DERIVED_UUID): the entity survives flagged.
    Breaks if replay deletes recorded DERIVED_UUIDs directly or skips the sharing check."""
    repo = test_neo4j_repo
    tag = f"test-dcl-replay-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        adapter, coord, _journal, db = _coordinator(repo, tmp_path)
        real_delete = adapter.delete_memory_detailed
        state = {"armed": True}

        def flaky(node_uuid, **kwargs):
            if state["armed"] and node_uuid == ids["twin"]:
                state["armed"] = False
                raise RuntimeError("crash deleting twin")
            return real_delete(node_uuid, **kwargs)

        adapter.delete_memory_detailed = flaky  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="crash deleting twin"):
                coord.erase_memory(ids["queue"])
        finally:
            del adapter.delete_memory_detailed

        [(op_id, state_name, request_json)] = _sql(
            db,
            "SELECT op_id, state, request_json FROM graph_operations WHERE operation_kind = ?",
            (ERASURE_KIND,),
        )
        assert state_name == "PREPARED"
        assert _exists(repo, ids["queue"]) is False
        assert _exists(repo, ids["twin"]) is True
        assert _exists(repo, ids["entity"]) is True
        # While PREPARED the fenced entity is held by this erasure's participant locks.
        assert _sql(
            db,
            "SELECT op_id FROM graph_operation_locks WHERE entity_uuid = ?",
            (ids["entity"],),
        ) == [(op_id,)]
        assert ids["entity"] in json.loads(request_json)["fence_uuids"]

        # The entity is now shared: a new live episode mentions it.
        _make_other_twin(repo, ids)
        repo.execute(
            "MATCH (o:Episodic {uuid:$other}), (e:Entity {uuid:$entity}) "
            "CREATE (o)-[:MENTIONS {uuid:$m, group_id:$ns}]->(e)",
            params={**ids, "m": f"{tag}-mention2"},
        )

        outcome, _diag = coord.replay_prepared_row(
            {"op_id": op_id, "request_json": request_json}
        )

        assert outcome == "REPLAYED"
        assert _exists(repo, ids["twin"]) is False
        ent = _entity(repo, ids["entity"])
        assert ent is not None, "replay must not delete an entity that is no longer single-source"
        assert ent["pending"] is True
        assert [r["kind"] for r in _repairs(repo, tag) if r["item"] == ids["entity"]] == [
            "entity"
        ]
        assert _sql(db, "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)) == [
            ("COMMITTED",)
        ]
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_views_artifacts_temporal_and_structure_entities_are_never_touched(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if the exclusion predicates (view_kind, structure_role, is_artifact, TEMPORAL type)
    are dropped from the derived-entity step. The control entity proves the cascade did run."""
    repo = test_neo4j_repo
    tag = f"test-dcl-exclude-{uuidlib.uuid4()}"
    try:
        ids = _seed_base(repo, tag)
        excluded = {
            "view": {"view_kind": "profile"},
            "structure": {"structure_role": "file"},
            "artifact": {"is_artifact": True},
            "temporal": {"type": "TEMPORAL"},
        }
        for name, props in excluded.items():
            repo.execute(
                "MATCH (t:Episodic {uuid:$twin}) "
                "CREATE (e:Entity {uuid:$u, name:$n, group_id:$ns, summary:'x', test_tag:$tag}) "
                "SET e += $props "
                "CREATE (t)-[:MENTIONS {uuid:$m, group_id:$ns}]->(e)",
                params={
                    **ids,
                    "u": f"{tag}-{name}",
                    "n": name,
                    "props": props,
                    "m": f"{tag}-m-{name}",
                },
            )
        _adapter, coord, _journal, _db = _coordinator(repo, tmp_path)

        outcome = coord.erase_memory(ids["queue"])

        assert outcome["reason"] == ERASED
        # Control: the ordinary single-source entity went, so the derived step ran.
        assert _exists(repo, ids["entity"]) is False
        assert outcome["derived"]["entities_deleted"] == 1
        assert outcome["derived"]["entities_flagged"] == 0
        for name in excluded:
            ent = _entity(repo, f"{tag}-{name}")
            assert ent is not None, f"{name} entity was deleted"
            assert ent["pending"] is None, f"{name} entity was flagged"
        assert [r for r in _repairs(repo, tag) if r["kind"] == "entity"] == []
    finally:
        _cleanup(repo, tag)
