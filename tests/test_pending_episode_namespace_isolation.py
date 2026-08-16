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
