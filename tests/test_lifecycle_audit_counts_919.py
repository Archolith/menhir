"""#919: the conflict scan's ``done`` and consolidation's audit rows match what was actually done.

Counterexamples against 4794b41f:
- ``scan_for_conflicts(limit=5000)`` capped the page at 2000 but compared with 5000, so ``done`` was
  False even on the last page;
- a demote row was written for every requested uuid, including nodes already in their grace window;
- a SESSION -> PERSISTENT revision was written for every requested uuid, including a node absorbed
  by a merge or skipped by the promotion filter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.services import lifecycle_consolidation
from menhir.services.lifecycle_conflicts import LifecycleConflictMixin
from menhir.services.lifecycle_service import LifecycleService

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- scan done

class _ScanHost(LifecycleConflictMixin):
    def __init__(self, rows) -> None:
        self.graph_adapter = MagicMock()
        self.graph_adapter.neo4j.execute.return_value = rows
        self._check_contradictions_batch = AsyncMock(return_value=0)


@pytest.mark.asyncio
async def test_a_short_page_is_done_even_when_the_limit_is_above_the_cap() -> None:
    host = _ScanHost([{"uuid": "a", "name": "a", "content": "", "namespace": "default"}])
    result = await host.scan_for_conflicts(limit=5000)
    assert result["done"] is True
    assert host.graph_adapter.neo4j.execute.call_args.kwargs["params"]["limit"] == 2000


@pytest.mark.asyncio
async def test_a_full_capped_page_is_not_done() -> None:
    rows = [{"uuid": f"u{i:04d}", "name": "n", "content": "", "namespace": "default"}
            for i in range(2000)]
    result = await _ScanHost(rows).scan_for_conflicts(limit=5000)
    assert result["done"] is False and result["next_cursor"] == "u1999"


# --------------------------------------------------------------------------- repository returns

def _repo(rows):
    neo4j = MagicMock()
    neo4j.execute.return_value = rows
    return ConsolidationRepository(neo4j), neo4j


def test_promote_returns_the_uuids_it_promoted() -> None:
    repo, neo4j = _repo([{"promoted_uuids": ["a"]}])
    assert repo.promote_to_persistent(["a", "absorbed"]) == ["a"]
    assert "collect(n.uuid) AS promoted_uuids" in neo4j.execute.call_args.args[0]
    assert repo.promote_to_persistent([]) == []


def test_demote_returns_only_the_newly_demoted_uuids() -> None:
    repo, neo4j = _repo([{"newly_demoted_uuids": ["new"]}])
    assert repo.set_demote_ttl(["old", "new"], 14) == ["new"]
    cypher = neo4j.execute.call_args.args[0]
    assert "n.ttl_expires IS NULL" in cypher and "collect(n.uuid) AS newly_demoted_uuids" in cypher
    assert repo.set_demote_ttl([], 14) == []


# --------------------------------------------------------------------------- consolidation audit

@pytest.mark.asyncio
async def test_audit_rows_follow_the_mutation_not_the_request(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
) -> None:
    adapter = stub_memory_graph_adapter
    adapter.session_entities = [
        {"uuid": "p-real", "name": "p-real", "user_flagged": True, "session_id": "s1"},
        {"uuid": "p-gone", "name": "p-gone", "user_flagged": True, "session_id": "s1"},
        {"uuid": "d-old", "name": "d-old", "user_flagged": False, "session_id": "s1"},
        {"uuid": "d-new", "name": "d-new", "user_flagged": False, "session_id": "s1"},
    ]
    adapter.demoted_uuids = ["d-old"]  # already in its grace window
    # p-gone was absorbed by a merge (or filtered), so the mutation does not promote it.
    monkeypatch.setattr(
        adapter, "promote_to_persistent", lambda uuids: [u for u in uuids if u != "p-gone"],
    )
    revisions: list[str] = []
    actions: list[tuple[str, str]] = []
    monkeypatch.setattr(
        lifecycle_consolidation, "record_memory_revision",
        lambda **kw: revisions.append(kw["node_uuid"]),
    )
    monkeypatch.setattr(
        lifecycle_consolidation, "record_lifecycle_action",
        lambda **kw: actions.append((kw["action"], kw["node_uuid"])),
    )
    svc = LifecycleService(graph_adapter=adapter, graphiti_client=stub_graphiti_client)
    monkeypatch.setattr(svc, "_count_similar_nodes", AsyncMock(return_value=5))  # low sharpness

    result = await svc.consolidate_session("s1")

    assert revisions == ["p-real"]
    assert [u for a, u in actions if a == "demote"] == ["d-new"]
    assert (result.promoted, result.demoted) == (1, 1)
