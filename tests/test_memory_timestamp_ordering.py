"""Chronological shared memory reads across native and legacy timestamp storage."""

from uuid import uuid4

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


def _read(adapter, reader, namespace, limit):
    if reader == "recent":
        return adapter.fetch_recent_memories(limit=limit, namespace=namespace)
    if reader == "flagged":
        return adapter.fetch_flagged_memories(limit=limit, namespace=namespace)
    if reader == "scope":
        return adapter.fetch_memories_by_scope("PERSISTENT", limit=limit, namespace=namespace)
    return adapter.fetch_memories_by_type("SEMANTIC", limit=limit, namespace=namespace)


@pytest.mark.online
@pytest.mark.asyncio
async def test_startup_context_selects_newest_before_bounded_limit(test_neo4j_repo):
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    from menhir.core.backend_runtime import RuntimeProvider
    from menhir.mcp.tools.recall.read_flagged_memories import ReadFlaggedMemoriesTool
    from menhir.mcp.tools.recall.recall_context_memories import RecallContextMemoriesTool

    neo4j = test_neo4j_repo
    tag = "issue145-" + uuid4().hex
    uuids = [tag + str(i) for i in range(5)]
    try:
        neo4j.execute("""
            UNWIND $uuids AS id
            CREATE (n:Entity {uuid:id, name:id, content:'Memory', namespace:$ns, group_id:$ns,
                type:'SEMANTIC', scope:'PERSISTENT', freshness:'ACTIVE', user_flagged:false,
                created_at:datetime('1999-01-01T00:00:00Z')})
            SET n.last_accessed = CASE WHEN id = $newest THEN datetime('2002-01-01T00:00:00Z')
                                      ELSE '1999-01-01T00:00:00Z' END
        """, {"uuids": uuids, "ns": tag, "newest": uuids[-1]})
        backend = RuntimeProvider(SimpleNamespace(graph_adapter=MemoryGraphAdapter(neo4j=neo4j)),
                                  SimpleNamespace(session_id=tag))
        flagged, context = ReadFlaggedMemoriesTool(), RecallContextMemoriesTool()
        with patch.object(type(flagged), "get_backend", return_value=backend), \
                patch.object(type(context), "get_backend", return_value=backend):
            await flagged.endpoint(reader_id=tag, namespace=tag, workspace="archolith")
            payload = json.loads(await context.endpoint(reader_id=tag, namespace=tag,
                                                       workspace="archolith", recent_limit=1))
        assert payload.get("ok") is not False, payload
        assert [row["uuid"] for row in payload["recent"]] == [uuids[-1]]
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": uuids})


@pytest.mark.online
def test_lifecycle_age_checks_legacy_native_and_unknown_dates(test_neo4j_repo):
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    neo4j = test_neo4j_repo
    tag = "issue145-" + uuid4().hex
    rows = [
        {"uuid": tag + "-old-text", "time": "1999-01-01T00:00:00Z", "native": False, "flagged": False},
        {"uuid": tag + "-old-native", "time": "2000-01-01T00:00:00Z", "native": True, "flagged": False},
        {"uuid": tag + "-recent", "time": "2099-01-01T00:00:00Z", "native": False, "flagged": False},
        {"uuid": tag + "-unknown", "time": "bad", "native": False, "flagged": False},
        {"uuid": tag + "-protected", "time": "1999-01-01T00:00:00Z", "native": False, "flagged": True},
    ]
    uuids = [row["uuid"] for row in rows]
    try:
        neo4j.execute("""
            UNWIND $rows AS row
            CREATE (n:Entity {uuid:row.uuid, name:row.uuid, content:'Memory', scope:'PERSISTENT',
                type:'SEMANTIC', freshness:'ACTIVE', edge_count:0, sharpness:0.1,
                session_id:$session, namespace:$session, user_flagged:row.flagged})
            SET n.created_at = CASE WHEN row.native THEN datetime(row.time) ELSE row.time END,
                n.last_accessed = n.created_at
        """, {"rows": rows, "session": tag})
        repository = ConsolidationRepository(neo4j)
        selected = repository.fetch_decay_candidates("ACTIVE", min_days_since_accessed=30,
                                                     max_edge_count=2, max_sharpness=0.5)
        assert [row["uuid"] for row in selected if row["uuid"] in uuids] == uuids[:2]
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids SET n.scope='SESSION'", {"uuids": uuids})
        selected = repository.fetch_session_entities(session_id=tag, max_age_hours=1)
        # Session consolidation also admits retained records; only unknown/recent ages are excluded.
        assert {row["uuid"] for row in selected} == {uuids[0], uuids[1], uuids[4]}
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": uuids})


@pytest.mark.online
@pytest.mark.parametrize("reader", ["recent", "flagged", "scope", "type"])
def test_shared_reader_offsets_ties_invalid_fallback_and_visibility(test_neo4j_repo, reader):
    neo4j = test_neo4j_repo
    namespace = "issue145-" + uuid4().hex
    data = [
        ("00-unknown", "bad", "bad", False, namespace, "ACTIVE"),
        ("10-legacy", "2002-01-01T02:00:00+02:00", None, False, namespace, "ACTIVE"),
        ("20-native", "2002-01-01T00:00:00Z", None, True, namespace, "ACTIVE"),
        ("30-subsecond", "2002-01-01T00:00:00.000000001Z", None, False, namespace, "ACTIVE"),
        ("40-fallback", "2001-02-29T00:00:00Z", "2001-01-01", False, namespace, "ACTIVE"),
        ("foreign", "2099-01-01", None, False, namespace + "-other", "ACTIVE"),
        ("gone", "2099-01-01", None, False, namespace, "GONE"),
    ]
    rows = [{"uuid": namespace + "-" + key, "accessed": accessed, "created": created,
             "native": native, "ns": ns, "freshness": freshness}
            for key, accessed, created, native, ns, freshness in data]
    try:
        neo4j.execute("""
            UNWIND $rows AS row
            CREATE (n:Entity {uuid: row.uuid, name: row.uuid, content: 'Memory',
                namespace: row.ns, group_id: row.ns, type: 'SEMANTIC', scope: 'PERSISTENT',
                freshness: row.freshness, user_flagged: true, bootstrap_scope: 'general', created_at: row.created})
            SET n.last_accessed = CASE WHEN row.native THEN datetime(row.accessed) ELSE row.accessed END
        """, {"rows": rows})
        adapter = MemoryGraphAdapter(neo4j=neo4j)
        expected = [namespace + "-" + key for key in
                    ["30-subsecond", "10-legacy", "20-native", "40-fallback", "00-unknown"]]
        assert [row["uuid"] for row in _read(adapter, reader, namespace, 10)] == expected
        assert [row["uuid"] for row in _read(adapter, reader, namespace, 2)] == expected[:2]
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n",
                      {"uuids": [row["uuid"] for row in rows]})


@pytest.mark.online
def test_view_refresh_and_supersession_keep_native_dates(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    tag = "issue145-" + uuid4().hex
    key = f"{tag}::user::reads"
    try:
        for value in [1.0, 1.0, 2.0]:
            adapter.record_counter(subject="user", counter="reads", value=value, namespace=tag)
        rows = test_neo4j_repo.execute(
            "MATCH (n:Entity {view_key:$key}) RETURN n.created_at AS created, n.last_accessed AS accessed",
            {"key": key},
        )
        assert len(rows) == 2
        assert all(hasattr(row["created"], "to_native") and hasattr(row["accessed"], "to_native")
                   for row in rows)
    finally:
        test_neo4j_repo.execute("MATCH (n:Entity {view_key:$key}) DETACH DELETE n", {"key": key})


@pytest.mark.online
def test_actual_writers_store_native_memory_dates_and_preserve_create_receipts(test_neo4j_repo):
    from datetime import date
    from menhir.infrastructure.artifact_repository import ArtifactRepository
    from menhir.infrastructure.candidate_repository import CandidateRepository
    from menhir.infrastructure.temporal_repository import TemporalRepository
    from menhir.infrastructure.todo_repository import TodoRepository

    neo4j = test_neo4j_repo
    tag = "issue145-" + uuid4().hex
    uuids = []
    artifact_ids = [tag + "-old", tag + "-new"]
    try:
        temporal = TemporalRepository(neo4j)
        reminder = temporal.create_temporal(content=tag, target_date=date.today().isoformat(), namespace=tag)
        uuids.append(reminder["uuid"])
        assert isinstance(reminder["created_at"], str)  # Public receipt remains ISO text.
        assert temporal.complete_temporal(reminder["uuid"])
        candidates = CandidateRepository(neo4j)
        kwargs = {"content": tag, "source": "codex", "cluster_id": tag, "label": tag, "namespace": tag}
        candidate = candidates.create_candidate(**kwargs)
        uuids.append(candidate["uuid"])
        assert candidate["created"] is True
        assert candidates.create_candidate(**kwargs)["created"] is False
        assert candidates.promote_candidate(candidate["uuid"])
        artifacts = ArtifactRepository(neo4j)
        for artifact_id in artifact_ids:
            kwargs = {"artifact_id": artifact_id, "artifact_type": "decision", "summary": artifact_id,
                      "source": "human", "status": "trusted", "evidence": [{"kind": "test", "ref": artifact_id}]}
            artifact = artifacts.create_artifact(**kwargs)
            uuids.append(artifact["uuid"])
            assert artifact["created"] is True
            assert artifacts.create_artifact(**kwargs)["created"] is False
        assert artifacts.supersede_artifact(*artifact_ids)
        todos = TodoRepository(neo4j)
        todo = todos.create_todo(content=tag, due_date=date.today().isoformat(), namespace=tag)
        uuids.append(todo["uuid"])
        mirror = neo4j.execute("MATCH (:Todo {uuid:$uuid})-[:HAS_REMINDER]->(r) RETURN r.uuid AS uuid",
                               {"uuid": todo["uuid"]})[0]["uuid"]
        uuids.append(mirror)
        assert todos.close_todo(todo["uuid"])
        for node_uuid in [reminder["uuid"], candidate["uuid"], *uuids[2:4], mirror]:
            row = neo4j.execute("""
                MATCH (n:Entity {uuid:$uuid}) RETURN n.created_at AS created, n.last_accessed AS accessed
            """, {"uuid": node_uuid})[0]
            assert hasattr(row["created"], "to_native")
            assert hasattr(row["accessed"], "to_native")
    finally:
        neo4j.execute("MATCH (n) WHERE n.uuid IN $uuids OR n.artifact_id IN $ids DETACH DELETE n",
                      {"uuids": uuids, "ids": artifact_ids})


@pytest.mark.online
@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("not a date", None), (123, None), (True, None), (["2026-01-01"], None),
    ("2026-02-29T00:00:00Z", None), ("1900-02-29", None), ("2026-04-31", None),
    ("2026-01-00", None), ("2026-13-01", None), ("0000-01-01", None),
    ("2026-01-01T24:00:00Z", None), ("2026-01-01T00:60:00Z", None),
    ("2026-01-01T00:00:60Z", None), ("2026-01-01T00:00:00+18:01", None),
    ("2026-01-01T00:00:00.1234567890Z", None), ("2026-01-01T00:00:00Z[UTC", None),
    ("2026-01-01T00:00:00[Europe/Paris]", None),
    ("2000-02-29", "2000-02-29T00:00:00"),
    ("2024-02-29T05:30:00+05:30", "2024-02-29T00:00:00"),
    ("2026-01-01T00:00:00Z[UTC]", "2026-01-01T00:00:00"),
    ("2026-01-01T01:00:00+01:00[Europe/Paris]", "2026-01-01T00:00:00"),
    ("2026-01-01 00:00:00", "2026-01-01T00:00:00"),
    ("2026-01-01T00:00:00.123456789Z", "2026-01-01T00:00:00.123456789"),
])
def test_safe_legacy_timestamp_conversion(test_neo4j_repo, value, expected):
    from menhir.infrastructure.cypher import memory_timestamp_cypher
    result = test_neo4j_repo.execute(
        f"RETURN {memory_timestamp_cypher('$value')} AS normalized", {"value": value},
    )[0]["normalized"]
    if expected is None:
        assert result is None
    else:
        assert result.iso_format().startswith(expected)


@pytest.mark.online
@pytest.mark.parametrize("expression", [
    "datetime('2026-01-01T00:00:00Z')", "datetime('2026-01-01T01:00:00+01:00[Europe/Paris]')",
    "localdatetime('2026-01-01T00:00:00')", "date('2026-01-01')",
])
def test_native_timestamp_conversion(test_neo4j_repo, expression):
    from menhir.infrastructure.cypher import memory_timestamp_cypher
    result = test_neo4j_repo.execute(
        f"WITH {expression} AS value RETURN {memory_timestamp_cypher('value')} AS normalized",
    )[0]["normalized"]
    assert result.iso_format().startswith("2026-01-01T00:00:00")


@pytest.mark.online
@pytest.mark.parametrize("reader", ["recent", "flagged", "scope", "type"])
def test_shared_reader_orders_legacy_and_native_timestamps_before_limit(test_neo4j_repo, reader):
    neo4j = test_neo4j_repo
    namespace = "issue145-" + uuid4().hex
    uuids = [namespace + "-legacy", namespace + "-native", namespace + "-fallback"]
    try:
        neo4j.execute("""
            UNWIND $rows AS row
            CREATE (n:Entity {uuid: row.uuid, name: row.uuid, content: 'Memory',
                namespace: $ns, group_id: $ns, type: 'SEMANTIC', scope: 'PERSISTENT',
                freshness: 'ACTIVE', user_flagged: true, bootstrap_scope: 'general',
                created_at: datetime(row.created)})
            SET n.last_accessed = CASE row.kind WHEN 'native' THEN datetime(row.accessed)
                                     ELSE row.accessed END
        """, {"ns": namespace, "rows": [
            {"uuid": uuids[0], "kind": "legacy", "created": "1999-01-01T00:00:00Z",
             "accessed": "1999-01-01T00:00:00+00:00"},
            {"uuid": uuids[1], "kind": "native", "created": "1999-01-01T00:00:00Z",
             "accessed": "2002-01-01T00:00:00Z"},
            {"uuid": uuids[2], "kind": "legacy", "created": "2001-01-01T00:00:00Z", "accessed": None},
        ]})
        adapter = MemoryGraphAdapter(neo4j=neo4j)

        def read(limit):
            if reader == "recent":
                return adapter.fetch_recent_memories(limit=limit, namespace=namespace)
            if reader == "flagged":
                return adapter.fetch_flagged_memories(limit=limit, namespace=namespace)
            if reader == "scope":
                return adapter.fetch_memories_by_scope("PERSISTENT", limit=limit, namespace=namespace)
            return adapter.fetch_memories_by_type("SEMANTIC", limit=limit, namespace=namespace)

        assert [row["uuid"] for row in read(3)] == [uuids[1], uuids[2], uuids[0]]
        assert read(1)[0]["uuid"] == uuids[1]
        # The actual retrieval update changes a legacy string into a native datetime.
        adapter.touch_retrieved_nodes([uuids[0]])
        assert read(1)[0]["uuid"] == uuids[0]
    finally:
        neo4j.execute("MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": uuids})
