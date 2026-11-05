"""#401: build_context's todo section honours the caller's namespace (live Neo4j)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from menhir.domain.namespace import DEFAULT_NAMESPACE
from menhir.domain.recall import RecallResult
from menhir.infrastructure.todo_repository import TodoRepository
from menhir.services.context_builder import ContextBuilderService

pytestmark = [pytest.mark.online, pytest.mark.timeout(60)]


def _seed(graph: Any) -> tuple[TodoRepository, str, dict[str, str]]:
    repo = TodoRepository(graph)
    keyword = f"zzkeyword{uuid4().hex[:8]}"
    uuids = {
        ns: repo.create_todo(content=f"{keyword} in {ns}", namespace=ns)["uuid"]
        for ns in ("ns-a-401", "ns-b-401", DEFAULT_NAMESPACE)
    }
    return repo, keyword, uuids


def test_search_by_query_scopes_to_namespace_plus_default(test_neo4j_repo: Any) -> None:
    repo, keyword, uuids = _seed(test_neo4j_repo)

    scoped = {r["uuid"] for r in repo.search_by_query(keyword, limit=10, namespace="ns-a-401")}
    assert scoped == {uuids["ns-a-401"], uuids[DEFAULT_NAMESPACE]}

    unrestricted = {r["uuid"] for r in repo.search_by_query(keyword, limit=10)}
    assert unrestricted == set(uuids.values())


@pytest.mark.asyncio
async def test_build_context_todo_section_never_leaks_other_namespace(
    test_neo4j_repo: Any,
) -> None:
    repo, keyword, _ = _seed(test_neo4j_repo)
    adapter = SimpleNamespace(
        list_todos_matching_query=lambda query, *, limit=3, namespace=None: (
            repo.search_by_query(query, limit=limit, namespace=namespace)
        )
    )
    recall = AsyncMock()
    recall.recall = AsyncMock(
        return_value=RecallResult(
            query="q", preset="knowledge", results=[], candidates_evaluated=0, nodes_touched=0
        )
    )
    svc = ContextBuilderService(recall_service=recall, graph_adapter=adapter)

    scoped = await svc.build_context(keyword, namespace="ns-a-401")
    assert f"{keyword} in ns-a-401" in scoped.context
    assert f"{keyword} in {DEFAULT_NAMESPACE}" in scoped.context
    assert "ns-b-401" not in scoped.context

    unrestricted = await svc.build_context(keyword)
    assert "ns-b-401" in unrestricted.context
