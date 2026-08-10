"""Live proof that a decay sweep never deletes a node for being isolated.

`cleanup_orphan_subgraphs()` was removed 2026-07-13 (it deleted any degree-zero :Entity,
bypassing every lifecycle policy, and destroyed ~24 unrecoverable production nodes). This is
the behavioral acceptance for the workspace-root artifact
`.agent/plans/menhir-orphan-cleanup-removal.md`: a full
`apply_decay()` pass over a graph containing isolated ACTIVE/PERSISTENT, IDENTITY, TEMPORAL,
and null-freshness nodes must leave every one of them intact.

Runs only with `--run-online`, against the stood-up TEST instance (:7688). The autouse
`force_all_tests_onto_test_neo4j` fixture guarantees the target is never prod, and
`test_neo4j_repo` wipes the scratch DB afterwards.
"""

from __future__ import annotations

import uuid as uuidlib
from pathlib import Path

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.lifecycle_service import LifecycleService


def _create_isolated_entity(repo, *, uuid: str, freshness: str | None, node_type: str,
                            old_ts: str, extra: str = "") -> None:
    """Create a degree-zero :Entity (no relationships) on the test graph.

    freshness=None deliberately omits the property (the 44%-of-graph null-freshness case).
    An old last_accessed makes the ACTIVE node a genuine decay candidate, so the sweep
    actually processes it rather than skipping it.
    """
    freshness_clause = ", freshness: $freshness" if freshness is not None else ""
    repo.execute(
        f"""
        CREATE (n:Entity {{
            uuid: $uuid, name: $name, content: $content, summary: '',
            scope: 'PERSISTENT', type: $type, user_flagged: false,
            edge_count: 0, sharpness: 0.05, namespace: 'test-orphan-removal',
            group_id: 'test-orphan-removal',
            created_at: datetime($ts), last_accessed: datetime($ts)
            {freshness_clause}
            {extra}
        }})
        """,
        params={
            "uuid": uuid,
            "name": f"orphan-removal-{node_type}",
            "content": "x" * 240,
            "type": node_type,
            "ts": old_ts,
            "freshness": freshness,
        },
    )


@pytest.mark.online
@pytest.mark.asyncio
async def test_decay_sweep_never_deletes_isolated_nodes(
    test_neo4j_repo, stub_graphiti_client, tmp_path
) -> None:
    repo = test_neo4j_repo
    old_ts = "2026-01-01T00:00:00Z"  # well past every compression threshold

    fixtures = {
        "active_persistent": (uuidlib.uuid4().hex, "ACTIVE", "SEMANTIC", ""),
        "identity": (uuidlib.uuid4().hex, "ACTIVE", "IDENTITY", ""),
        "temporal": (uuidlib.uuid4().hex, "ACTIVE", "TEMPORAL",
                     ", target_date: datetime('2027-01-01T00:00:00Z'), status: 'open'"),
        "null_freshness": (uuidlib.uuid4().hex, None, "SEMANTIC", ""),
    }

    for label, (uuid, freshness, node_type, extra) in fixtures.items():
        _create_isolated_entity(
            repo, uuid=uuid, freshness=freshness, node_type=node_type, old_ts=old_ts, extra=extra
        )

    # Every fixture node is isolated (degree zero) -- the exact shape the removed cleanup
    # would have DETACH DELETEd on this sweep.
    for label, (uuid, *_rest) in fixtures.items():
        deg = repo.execute(
            "MATCH (n:Entity {uuid:$u}) RETURN size([(n)--() | 1]) AS degree", params={"u": uuid}
        )
        assert deg and deg[0]["degree"] == 0, f"{label} fixture is not isolated"

    # High similarity -> low sharpness -> the ACTIVE candidate reaches the compress decision;
    # with llm=None it is deferred (recorded, never deleted), exercising the real path.
    stub_graphiti_client.count_similar_by_cosine_results = 5

    adapter = MemoryGraphAdapter(neo4j=repo)
    svc = LifecycleService(
        graph_adapter=adapter,
        graphiti_client=stub_graphiti_client,
        llm=None,
        pending_actions=PendingActionStore(db_path=Path(tmp_path) / "pending.db"),
    )

    result = await svc.apply_decay()

    # The removed path is provably inert.
    assert result.orphan_subgraphs_cleaned == 0

    # Every isolated node survives the full sweep.
    for label, (uuid, *_rest) in fixtures.items():
        rows = repo.execute("MATCH (n:Entity {uuid:$u}) RETURN n.uuid AS uuid", params={"u": uuid})
        assert len(rows) == 1, f"decay sweep DELETED the isolated {label} node ({uuid})"

    # Any deletion in decay can only come from the COMPRESSED->GONE bridge_and_delete path,
    # never from isolation; none of these are COMPRESSED, so nothing was deleted.
    assert result.deleted == 0
