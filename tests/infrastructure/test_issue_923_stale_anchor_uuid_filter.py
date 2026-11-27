"""#923: `stale_anchored_memories(memory_uuids=...)` returns the rows for those memories.

Recall used the 200 most recently dirtied anchors and labeled every other returned memory fresh.
Online: the filter must select by uuid past the `limit` window and keep the tenancy predicate.
"""

from __future__ import annotations

from typing import Any

import pytest


def _seed(repo: Any) -> None:
    """Three memories on one dirty file; tenant-b's memory shares the file."""
    repo.execute("MATCH (n) DETACH DELETE n")
    repo.execute(
        """
        CREATE (f:Entity {uuid:'f1', structure_role:'file', structure_project:'menhir',
                          structure_path:'src/x.py', structure_dirty:true,
                          dirty_at: datetime('2026-08-20T12:00:00Z'), last_event_op:'write'})
        CREATE (g:Entity {uuid:'f2', structure_role:'file', structure_project:'menhir',
                          structure_path:'src/y.py', structure_dirty:true,
                          dirty_at: datetime('2026-08-21T12:00:00Z'), last_event_op:'write'})
        CREATE (old:Entity {uuid:'mem-old', name:'OLDER DIRTY', namespace:'tenant-a'})
        CREATE (new:Entity {uuid:'mem-new', name:'NEWER DIRTY', namespace:'tenant-a'})
        CREATE (b:Entity {uuid:'mem-b', name:'TENANT B', namespace:'tenant-b'})
        CREATE (old)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        CREATE (new)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(g)
        CREATE (b)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        """
    )


def _uuids(rows: list[dict]) -> set[str]:
    return {str(r["memory_uuid"]) for r in rows}


@pytest.mark.online
def test_uuid_filter_reaches_a_row_outside_the_limit_window(test_neo4j_repo) -> None:
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    # Control: limit 1 by dirty_at DESC returns only the newer one -- the defect's window.
    assert _uuids(repo.stale_anchored_memories(namespace="tenant-a", limit=1)) == {"mem-new"}
    assert _uuids(repo.stale_anchored_memories(
        namespace="tenant-a", limit=1, memory_uuids=["mem-old"])) == {"mem-old"}


@pytest.mark.online
def test_uuid_filter_keeps_tenancy_and_project_scope(test_neo4j_repo) -> None:
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    assert _uuids(repo.stale_anchored_memories(
        namespace="tenant-a", memory_uuids=["mem-old", "mem-b"])) == {"mem-old"}
    assert repo.stale_anchored_memories(
        project="other", namespace="tenant-a", memory_uuids=["mem-old"]) == []
    assert repo.stale_anchored_memories(namespace="tenant-a", memory_uuids=[]) == []
    assert _uuids(repo.stale_anchored_memories(namespace="tenant-a")) == {"mem-old", "mem-new"}
