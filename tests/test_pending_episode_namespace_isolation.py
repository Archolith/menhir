"""Cross-tenant read regression: pending-episode fetch must scope to ``n.namespace``.

This is a query-shape + simulated-filter test rather than a live-graph test: there is
no offline Neo4j harness for ``EpisodeLifecycleRepository`` in this suite, so a fake
``neo4j`` records the emitted Cypher and params and applies the tenant predicate to an
in-memory row set. The test proves both that the predicate is present (tenant-b yields
zero rows) and that the feature still works (tenant-a still yields the row) -- a
test that only asserted zero rows would also pass if the predicate broke the query
entirely, which is exactly the failure mode of the wrong ``n.group_id`` fix.
"""

from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository


class _RecordingNeo4j:
    """Stand-in for the neo4j driver: records queries/params and simulates the WHERE."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.executed_queries: list[str] = []
        self.executed_params: list[dict[str, object]] = []

    def execute(self, query: str, *, params: dict[str, object]) -> list[dict[str, object]]:
        self.executed_queries.append(query)
        self.executed_params.append(params)
        tokens = [str(t).lower() for t in params.get("tokens") or []]
        namespace = params.get("namespace")
        out: list[dict[str, object]] = []
        for row in self.rows:
            if str(row.get("processing_state") or "") not in {"PENDING", "ENRICHING"}:
                continue
            if namespace is not None and row.get("namespace") != namespace:
                continue
            content = str(row.get("content") or "").lower()
            name = str(row.get("name") or "").lower()
            if not any(token in content or token in name for token in tokens):
                continue
            out.append(dict(row))
        return out


def _pending_episode(uuid: str, namespace: str, content: str) -> dict[str, object]:
    return {
        "uuid": uuid,
        "name": f"{uuid} memory",
        "content": content,
        "namespace": namespace,
        "scope": "SESSION",
        "type": "EPISODIC",
        "processing_state": "PENDING",
    }


def test_pending_episode_fetch_isolates_foreign_namespace() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _RecordingNeo4j(
        [_pending_episode("ep-a", "tenant-a", "ZEPHYR launch planned")]
    )
    token = "zephyr"

    tenant_b_rows = repo.fetch_relevant_pending_episodes(token, namespace="tenant-b")
    assert tenant_b_rows == []

    tenant_a_rows = repo.fetch_relevant_pending_episodes(token, namespace="tenant-a")
    assert [r["uuid"] for r in tenant_a_rows] == ["ep-a"]

    cypher = repo.neo4j.executed_queries[-1]
    assert "($namespace IS NULL OR n.namespace = $namespace)" in cypher
    assert repo.neo4j.executed_params[-1]["namespace"] == "tenant-a"


def test_pending_episode_fetch_omitted_namespace_preserves_global_behavior() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _RecordingNeo4j(
        [
            _pending_episode("ep-a", "tenant-a", "ZEPHYR launch planned"),
            _pending_episode("ep-b", "tenant-b", "zephyr unrelated"),
        ]
    )

    rows = repo.fetch_relevant_pending_episodes("zephyr")

    assert {r["uuid"] for r in rows} == {"ep-a", "ep-b"}
    assert repo.neo4j.executed_params[-1]["namespace"] is None


@pytest.mark.online
@pytest.mark.asyncio
async def test_pending_query_filters_session_before_limit_and_flows_through_recall(
    test_neo4j_repo, stub_graphiti_client,
) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.recall_service import RecallService
    from menhir.services.scoring_service import ScoringService

    ns = f"test-pending-session-{uuidlib.uuid4()}"
    rows = [
        {"uuid": f"{ns}-{name}", "scope": scope, "session_id": owner,
         "namespace": namespace, "sequence": index}
        for index, (name, scope, owner, namespace) in enumerate([
            ("foreign-1", "SESSION", "B", ns), ("foreign-2", "SESSION", "B", ns),
            ("foreign-3", "SESSION", "B", ns), ("ownerless", "SESSION", None, ns),
            ("blank-scope", "", "B", ns), ("missing-scope", None, "B", ns),
            ("own", "SESSION", "A", ns), ("durable", "PERSISTENT", "B", ns),
            ("legacy-own", None, "A", ns),
            ("other-tenant", "SESSION", "A", f"{ns}-other"),
        ])
    ]
    test_neo4j_repo.execute(
        "UNWIND $rows AS row CREATE (n:Episodic) SET n = row, n.test_tag=$tag, "
        "n.content='zephyr pending fact', n.processing_state='PENDING', "
        "n.created_at=datetime('2026-01-01') + duration({seconds:row.sequence})",
        params={"rows": rows, "tag": ns},
    )
    try:
        adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
        selected = adapter.fetch_relevant_pending_episodes(
            "zephyr", limit=3, namespace=ns, include_session=True, session_id="A",
        )
        expected = {f"{ns}-own", f"{ns}-durable", f"{ns}-legacy-own"}
        assert {row["uuid"] for row in selected} == expected
        assert next(row for row in selected if row["scope"] == "SESSION")["session_id"] == "A"
        durable = adapter.fetch_relevant_pending_episodes(
            "zephyr", namespace=ns, include_session=False, session_id="A",
        )
        assert [row["uuid"] for row in durable] == [f"{ns}-durable"]
        assert len(adapter.fetch_relevant_pending_episodes("zephyr", namespace=ns)) == 3
        assert adapter.fetch_relevant_pending_episodes("zephyr", namespace=f"{ns}-missing") == []

        async def wait(episode_uuid, *, timeout_s):
            return adapter.fetch_episode_processing(episode_uuid)

        stub_graphiti_client.search_scored_results = []
        svc = RecallService(
            graphiti_client=stub_graphiti_client, graph_adapter=adapter,
            scoring_service=ScoringService(), ingest_service=SimpleNamespace(wait_for_episode_processing=wait),
        )
        result = await svc.recall(
            "zephyr", namespace=ns, include_session=True, session_id="A",
            wait_for_pending=True, pending_wait_timeout_s=0.01,
        )
        assert {row.uuid for row in result.results} == expected
        result = await svc.recall(
            "zephyr", namespace=ns, include_session=False, session_id="A",
            wait_for_pending=True, pending_wait_timeout_s=0.01,
        )
        assert [row.uuid for row in result.results] == [f"{ns}-durable"]
    finally:
        test_neo4j_repo.execute("MATCH (n {test_tag:$tag}) DETACH DELETE n", params={"tag": ns})
