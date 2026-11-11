"""R2 CF-329 online counterexamples: pending_actions never keeps an erased episode's text.

Production writer: ``LifecycleService.rehydrate_node`` queues a pending ``rehydrate`` row when it
cannot merge. Graph seeded in Graphiti's shape; no LLM (a fake merge).
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.lifecycle_service import LifecycleService
from tests._erasure_canary import all_hits, new_canary, sqlite_hits
from tests.test_erasure_rebuild_p0_scrub_online import _adapter_and_coordinator, _u

pytestmark = pytest.mark.online


class _MergeLLM:
    async def merge_content(self, existing_content: str, new_context: str) -> str | None:
        return f"{existing_content} + {new_context}"


def _seed(repo: Any, ns: str, canary: str) -> dict[str, str]:
    ids = {"x": _u(), "ep": _u(), "ns": ns}
    repo.execute(
        """
        CREATE (:Entity {uuid:$x, name:'x', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:'COMPRESSED', content:'gist', original_content:'x body',
                         created_at:datetime()})
        CREATE (:Episodic {uuid:$ep, name:'e', group_id:$ns, namespace:$ns, content:$c,
                           source_description:'t', created_at:datetime(), valid_at:datetime()})
        """,
        params={**ids, "c": f"episode says {canary}"},
    )
    return ids


def _service(adapter: Any, db: Any, llm: Any = None) -> LifecycleService:
    return LifecycleService(
        graph_adapter=adapter, graphiti_client=MagicMock(), llm=llm,
        pending_actions=PendingActionStore(db_path=db),
    )


def _rows(db: Any) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM pending_actions")]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cf329_queued_rehydrate_names_the_episode_and_stores_no_text(
    test_neo4j_repo, tmp_path
) -> None:
    """Fails if rehydrate_node writes the episode text to ``pending_actions.context`` again."""
    repo = test_neo4j_repo
    ns, canary = f"r2-329q-{_u()}", new_canary("CF329Q")
    ids = _seed(repo, ns, canary)
    adapter, _, db = _adapter_and_coordinator(repo, tmp_path)

    ok = await _service(adapter, db).rehydrate_node(
        ids["x"], new_context=f"episode says {canary}", source_episode_uuid=ids["ep"]
    )

    assert ok is False
    rows = _rows(db)
    assert [(r["node_uuid"], r["source_uuid"], r["context"]) for r in rows] == [
        (ids["x"], ids["ep"], None)
    ], rows
    assert sqlite_hits(db, canary) == []


@pytest.mark.asyncio
async def test_cf329_legacy_context_is_purged_and_a_retry_after_erase_is_a_no_op(
    test_neo4j_repo, tmp_path
) -> None:
    """A legacy row holds the episode text. Erasing the episode clears it (the row stays), a
    stale writer cannot put it back, and a rehydrate with the erased episode's text changes
    nothing. Fails if pending_actions.context is not a registered content column, or if its
    denylist triggers are not installed when the store creates the table."""
    repo = test_neo4j_repo
    ns, canary = f"r2-329l-{_u()}", new_canary("CF329L")
    ids = _seed(repo, ns, canary)
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    store = PendingActionStore(db_path=db)
    store.upsert(ids["x"], "rehydrate", context=f"episode says {canary}",
                 source_uuid=ids["ep"], failure_reason="no_llm")

    out = coord.erase_memory(ids["ep"])

    assert out["reason"] == "erased", out
    assert all_hits(repo, db, canary) == []
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["context"] is None, rows

    # A stale writer (an old process) queues the text again.
    store.upsert(ids["x"], "rehydrate", context=f"episode says {canary}",
                 source_uuid=ids["ep"], failure_reason="no_llm")
    # The retry path: ingestion hands rehydrate the erased episode's text once more.
    ok = await _service(adapter, db, _MergeLLM()).rehydrate_node(
        ids["x"], new_context=f"episode says {canary}", source_episode_uuid=ids["ep"]
    )

    assert ok is False
    assert all_hits(repo, db, canary) == []
    rows = repo.execute("MATCH (e:Entity {uuid:$x}) RETURN e.freshness AS f", params=ids)
    assert rows[0]["f"] == "COMPRESSED"
