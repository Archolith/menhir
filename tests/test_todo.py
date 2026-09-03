"""Unit tests for TodoRepository, MCP tools, and hook output formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure.todo_repository import (
    TODO_AGE_DAYS_CYPHER,
    TODO_STALE_AFTER_DAYS,
    TodoRepository,
)
from menhir.cli.output import format_hook_output


# ---------------------------------------------------------------------------
# Stub Neo4j
# ---------------------------------------------------------------------------


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


# ---------------------------------------------------------------------------
# TodoRepository.create_todo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_todo_returns_dict_with_open_status() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Fix the bug", code_ref="src/api/routes.py:42", priority="high")

    assert result["content"] == "Fix the bug"
    assert result["code_ref"] == "src/api/routes.py:42"
    assert result["priority"] == "high"
    assert result["status"] == "open"
    assert result["closed_at"] is None
    assert "uuid" in result
    assert "created_at" in result


@pytest.mark.unit
def test_create_todo_coerces_invalid_priority_to_normal() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Task", priority="urgent")

    assert result["priority"] == "normal"


@pytest.mark.unit
def test_create_todo_issues_one_idempotent_write() -> None:
    """Renamed and re-pointed by CF-158: the single write is now a uuid MERGE rather than a
    CREATE. What the test is FOR -- one statement, no inferred edges, params carried -- is
    unchanged; only the verb it pinned moved."""
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Do something")

    # A todo with no code_ref is now a single write: no inferred edges remain.
    assert len(neo4j.calls) == 1
    assert "MERGE (n:Todo {uuid: $uuid})" in neo4j.calls[0]["query"]
    params = neo4j.calls[0]["params"]
    assert params["content"] == "Do something"
    assert params["priority"] == "normal"
    assert params["code_ref"] is None


@pytest.mark.unit
def test_create_todo_without_code_ref() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Refactor scorer")

    assert result["code_ref"] is None


# ---------------------------------------------------------------------------
# TodoRepository.close_todo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_close_todo_returns_true_when_updated() -> None:
    neo4j = _StubNeo4j(responses=[[{"updated": 1}]])
    repo = TodoRepository(neo4j)

    assert repo.close_todo("some-uuid") is True


@pytest.mark.unit
def test_close_todo_returns_false_when_not_found() -> None:
    neo4j = _StubNeo4j(responses=[[{"updated": 0}]])
    repo = TodoRepository(neo4j)

    assert repo.close_todo("missing-uuid") is False


@pytest.mark.unit
def test_close_todo_returns_false_on_empty_rows() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    assert repo.close_todo("no-rows") is False


@pytest.mark.unit
def test_close_todo_issues_match_set_cypher() -> None:
    neo4j = _StubNeo4j(responses=[[{"updated": 1}]])
    repo = TodoRepository(neo4j)

    repo.close_todo("abc-123")

    assert len(neo4j.calls) == 1
    q = neo4j.calls[0]["query"]
    assert "MATCH (n:Todo" in q
    assert "n.status = 'closed'" in q
    assert neo4j.calls[0]["params"]["uuid"] == "abc-123"


# ---------------------------------------------------------------------------
# TodoRepository.delete_todo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delete_todo_returns_true_when_deleted() -> None:
    neo4j = _StubNeo4j(responses=[[{"found": 1}]])
    repo = TodoRepository(neo4j)

    assert repo.delete_todo("some-uuid") is True


@pytest.mark.unit
def test_delete_todo_returns_false_when_not_found() -> None:
    neo4j = _StubNeo4j(responses=[[{"found": 0}]])
    repo = TodoRepository(neo4j)

    assert repo.delete_todo("missing") is False


# ---------------------------------------------------------------------------
# TodoRepository.list_todos
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_todos_passes_status_and_limit() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.list_todos(status="closed", limit=10)

    params = neo4j.calls[0]["params"]
    assert params["status"] == "closed"
    assert params["limit"] == 10


@pytest.mark.unit
def test_list_todos_clamps_limit_to_200() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.list_todos(limit=999)

    assert neo4j.calls[0]["params"]["limit"] == 200


@pytest.mark.unit
def test_list_todos_coerces_invalid_status_to_open() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.list_todos(status="pending")

    assert neo4j.calls[0]["params"]["status"] == "open"


@pytest.mark.unit
def test_list_todos_returns_rows() -> None:
    rows = [
        {"uuid": "a", "content": "First", "priority": "high", "status": "open", "code_ref": None},
        {"uuid": "b", "content": "Second", "priority": "normal", "status": "open", "code_ref": "src/x.py:1"},
    ]
    neo4j = _StubNeo4j(responses=[rows])
    repo = TodoRepository(neo4j)

    result = repo.list_todos()

    assert len(result) == 2
    assert result[0]["uuid"] == "a"
    assert result[1]["code_ref"] == "src/x.py:1"


# ---------------------------------------------------------------------------
# TodoRepository — new graph edge behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_todo_with_code_ref_issues_references_file_query() -> None:
    neo4j = _StubNeo4j()  # no responses needed; all default to []
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Fix handler", code_ref="src/api/routes.py:42")

    # calls: CREATE, REFERENCES_FILE, known-projects lookup, HAS_LOCATION.
    # The trailing location->file audit-edge write was removed in CF-143 -- it MERGEd
    # (l)-[:REFERENCES_FILE]->(f) from :TodoLocation and nothing ever traversed it. The
    # :Todo-level REFERENCES_FILE edge asserted below is a different edge and IS read.
    assert len(neo4j.calls) == 4
    file_query = neo4j.calls[1]["query"]
    assert "REFERENCES_FILE" in file_query
    assert neo4j.calls[1]["params"]["file_path"] == "src/api/routes.py"


@pytest.mark.unit
def test_create_todo_linked_file_path_populated_when_matched() -> None:
    # Responses in call order: CREATE, REFERENCES_FILE match, then location writes
    neo4j = _StubNeo4j(responses=[
        [],
        [{"linked_path": "src/api/routes.py"}],
        [],
    ])
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Fix handler", code_ref="src/api/routes.py:42")

    assert result["linked_file_path"] == "src/api/routes.py"


@pytest.mark.unit
def test_create_todo_linked_file_path_none_when_no_structural_match() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Fix handler", code_ref="src/api/routes.py:42")

    assert result["linked_file_path"] is None


@pytest.mark.unit
def test_create_todo_code_ref_without_line_number() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Fix this", code_ref="src/api/routes.py")

    assert neo4j.calls[1]["params"]["file_path"] == "src/api/routes.py"


@pytest.mark.unit
def test_create_todo_with_episode_uuid_issues_created_from_query() -> None:
    neo4j = _StubNeo4j()  # all calls default to []
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Track this", episode_uuid="ep-abc-123")

    # calls: CREATE, CREATED_FROM
    assert len(neo4j.calls) == 2
    ep_query = neo4j.calls[1]["query"]
    assert "CREATED_FROM" in ep_query
    assert neo4j.calls[1]["params"]["episode_uuid"] == "ep-abc-123"


@pytest.mark.unit
def test_create_todo_episode_uuid_in_result() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Task", episode_uuid="ep-abc")

    assert result["episode_uuid"] == "ep-abc"


@pytest.mark.unit
def test_create_todo_no_episode_uuid_skips_created_from() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Task")

    # Only CREATE, no CREATED_FROM
    assert all("CREATED_FROM" not in c["query"] for c in neo4j.calls)


@pytest.mark.unit
def test_create_todo_all_params_together() -> None:
    # Call order: CREATE, REFERENCES_FILE, CREATED_FROM, known-projects lookup, HAS_LOCATION.
    # The location->file audit-edge write that used to follow was removed in CF-143 (dead write).
    neo4j = _StubNeo4j(responses=[
        [],
        [{"linked_path": "src/api/routes.py"}],
        [],
        [{"p": "menhir"}],
        [],
    ])
    repo = TodoRepository(neo4j)

    result = repo.create_todo(
        content="Fix router error handling",
        code_ref="src/api/routes.py:10",
        episode_uuid="ep-123",
    )

    assert len(neo4j.calls) == 5
    assert result["linked_file_path"] == "src/api/routes.py"
    assert result["episode_uuid"] == "ep-123"
    assert [(l["path"], l["line_start"]) for l in result["locations"]] == [
        ("src/api/routes.py", 10)
    ]


# ---------------------------------------------------------------------------
# TodoRepository.search_by_query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_by_query_passes_query_and_words() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.search_by_query("fix auth middleware", limit=3)

    params = neo4j.calls[0]["params"]
    assert params["query"] == "fix auth middleware"
    assert "middleware" in params["words"]
    assert params["limit"] == 3


@pytest.mark.unit
def test_search_by_query_filters_short_words() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.search_by_query("fix the bug in auth")

    words = neo4j.calls[0]["params"]["words"]
    # "fix", "the", "bug", "in", "auth" are all < 5 chars — should be filtered out
    assert words == []


@pytest.mark.unit
def test_search_by_query_clamps_limit() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.search_by_query("anything", limit=999)

    assert neo4j.calls[0]["params"]["limit"] == 50


@pytest.mark.unit
def test_search_by_query_returns_rows() -> None:
    rows = [{"uuid": "a", "content": "Fix middleware", "priority": "high", "code_ref": None}]
    neo4j = _StubNeo4j(responses=[rows])
    repo = TodoRepository(neo4j)

    result = repo.search_by_query("middleware")

    assert len(result) == 1
    assert result[0]["uuid"] == "a"


# ---------------------------------------------------------------------------
# format_hook_output — todos rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_hook_output_with_todos() -> None:
    todos = [
        {"uuid": "abc", "content": "Fix error handling", "priority": "high", "code_ref": "src/api/routes.py:42"},
        {"uuid": "def", "content": "Refactor scoring", "priority": "normal", "code_ref": None},
    ]
    result = format_hook_output([], todos=todos)

    assert "### TODOs (2 open)" in result
    assert "[HIGH]" in result
    assert "src/api/routes.py:42" in result
    assert "Fix error handling" in result
    assert "[NORMAL]" in result
    assert "Refactor scoring" in result


@pytest.mark.unit
def test_format_hook_output_todos_before_pinned() -> None:
    todos = [{"uuid": "t1", "content": "A task", "priority": "normal", "code_ref": None}]
    flagged = [{"name": "rule", "content": "Always use conventional commits"}]

    result = format_hook_output(flagged, todos=todos)

    todo_pos = result.index("### TODOs")
    pinned_pos = result.index("### Pinned")
    assert todo_pos < pinned_pos


@pytest.mark.unit
def test_format_hook_output_no_todos_section_when_empty() -> None:
    result = format_hook_output([], todos=[])
    assert "TODOs" not in result


@pytest.mark.unit
def test_format_hook_output_no_todos_section_when_none() -> None:
    result = format_hook_output([], todos=None)
    assert "TODOs" not in result


@pytest.mark.unit
def test_format_hook_output_todo_content_truncated_at_80() -> None:
    long_content = "x" * 100
    todos = [{"uuid": "a", "content": long_content, "priority": "normal", "code_ref": None}]

    result = format_hook_output([], todos=todos)

    assert "x" * 80 in result
    assert "..." in result
    assert "x" * 81 not in result


@pytest.mark.unit
def test_format_hook_output_temporal_before_todos() -> None:
    todos = [{"uuid": "t1", "content": "Task", "priority": "low", "code_ref": None}]
    temporal = "_2026-03-24T10:00:00Z — 2.0h since last session_"

    result = format_hook_output([], temporal_line=temporal, todos=todos)

    temporal_pos = result.index(temporal)
    todo_pos = result.index("### TODOs")
    assert temporal_pos < todo_pos


# ---------------------------------------------------------------------------
# MCP tool smoke (no Neo4j) — just verifies tool wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_todo_tool_calls_backend() -> None:
    from menhir.mcp.tools.ops.add_todo import AddTodoTool

    tool = AddTodoTool()
    backend = MagicMock()
    backend.create_todo = AsyncMock(
        return_value={"uuid": "u1", "priority": "high", "code_ref": "src/x.py:1"}
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(text="Do the thing", code_ref="src/x.py:1", priority="high"))

    backend.create_todo.assert_called_once_with(
        content="Do the thing", code_ref="src/x.py:1", priority="high",
        episode_uuid=None, structure_project=None, due_date=None, namespace=None,
    )
    assert "u1" in result
    assert "HIGH" in result


@pytest.mark.unit
def test_close_todo_tool_returns_closed_message() -> None:
    from menhir.mcp.tools.ops.close_todo import CloseTodoTool

    tool = CloseTodoTool()
    backend = MagicMock()
    backend.close_todo = AsyncMock(return_value=True)
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(uuid="u1"))

    assert "Closed TODO u1" in result


@pytest.mark.unit
def test_close_todo_tool_not_found_message() -> None:
    from menhir.mcp.tools.ops.close_todo import CloseTodoTool

    tool = CloseTodoTool()
    backend = MagicMock()
    backend.close_todo = AsyncMock(return_value=False)
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(uuid="u1"))

    assert "not found or already closed" in result


# ---------------------------------------------------------------------------
# Inbound semantic links (Phase B slice 1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "relation,edge",
    [("mentions", "MENTIONS_TODO"), ("addresses", "ADDRESSES_TODO")],
)
def test_link_memory_to_todo_uses_a_distinct_edge_type(relation: str, edge: str) -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = TodoRepository(neo4j)

    result = repo.link_memory_to_todo("m1", "t1", relation)

    assert result["linked"] is True
    assert result["edge_type"] == edge
    assert f"MERGE (m)-[r:{edge}]->(t)" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_link_is_idempotent_via_merge() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = TodoRepository(neo4j)

    repo.link_memory_to_todo("m1", "t1", "mentions")

    assert "MERGE" in neo4j.calls[0]["query"]
    assert "CREATE (m)" not in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_lifecycle_relations_are_not_available_in_slice_one() -> None:
    """RESOLVES/REOPENS ship with the transaction in slice 2, never bare."""
    repo = TodoRepository(_StubNeo4j())

    for relation in ("resolves", "reopens"):
        result = repo.link_memory_to_todo("m1", "t1", relation)
        assert result["linked"] is False
        assert result["reason"] == "unsupported_relation"


@pytest.mark.unit
def test_unsupported_relation_never_reaches_a_query() -> None:
    """The whitelist is also the injection guard -- Cypher cannot parameterize a type."""
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.link_memory_to_todo("m1", "t1", "DELETE]->() DETACH DELETE t //")

    assert neo4j.calls == []


@pytest.mark.unit
def test_link_requires_a_durable_non_structural_entity() -> None:
    """Structural nodes are ineligible: a file does not address a todo."""
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = TodoRepository(neo4j)

    repo.link_memory_to_todo("m1", "t1", "mentions")

    query = neo4j.calls[0]["query"]
    assert "m.scope = 'PERSISTENT'" in query
    assert "m.structure_role IS NULL" in query


@pytest.mark.unit
def test_link_enforces_namespace_compatibility() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]])
    repo = TodoRepository(neo4j)

    repo.link_memory_to_todo("m1", "t1", "mentions")

    query = neo4j.calls[0]["query"]
    assert "t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]" in query
    assert neo4j.calls[0]["params"]["default_ns"] == "default"


@pytest.mark.unit
def test_link_reports_refusal_rather_than_raising() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 0}]])
    repo = TodoRepository(neo4j)

    result = repo.link_memory_to_todo("m1", "t1", "mentions")

    assert result["linked"] is False
    assert result["reason"] == "ineligible_or_not_found"


@pytest.mark.unit
def test_many_memories_may_link_to_one_todo_and_vice_versa() -> None:
    neo4j = _StubNeo4j(responses=[[{"linked": 1}]] * 4)
    repo = TodoRepository(neo4j)

    assert repo.link_memory_to_todo("m1", "t1", "mentions")["linked"]
    assert repo.link_memory_to_todo("m2", "t1", "addresses")["linked"]
    assert repo.link_memory_to_todo("m1", "t2", "mentions")["linked"]
    assert repo.link_memory_to_todo("m1", "t1", "addresses")["linked"]
    assert len(neo4j.calls) == 4


@pytest.mark.unit
def test_todo_inbound_links_filters_to_known_edge_types() -> None:
    neo4j = _StubNeo4j(responses=[[{"relation": "ADDRESSES_TODO", "memory_uuid": "m1"}]])
    repo = TodoRepository(neo4j)

    rows = repo.todo_inbound_links("t1")

    assert rows[0]["relation"] == "ADDRESSES_TODO"
    # Reads span link and lifecycle relations alike; only writes are restricted.
    assert "MENTIONS_TODO" in neo4j.calls[0]["params"]["edge_types"]
    assert "ADDRESSES_TODO" in neo4j.calls[0]["params"]["edge_types"]


@pytest.mark.unit
def test_get_todo_returns_inbound_links() -> None:
    neo4j = _StubNeo4j(responses=[
        [{"uuid": "t1", "content": "body"}],
        [{"relation": "ADDRESSES_TODO", "memory_uuid": "m1", "memory_name": "decision"}],
    ])
    repo = TodoRepository(neo4j)

    todo = repo.get_todo("t1")

    assert todo["inbound_links"][0]["memory_name"] == "decision"


# ---------------------------------------------------------------------------
# Lifecycle transactions (Phase B slice 2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_todo_creates_edge_and_closes_in_one_statement() -> None:
    """Atomicity comes from a single statement, not a transaction scope."""
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    result = repo.resolve_todo("t1", "m1")

    assert result["applied"] is True
    assert result["status"] == "closed"
    assert len(neo4j.calls) == 1
    query = neo4j.calls[0]["query"]
    assert "MERGE (m)-[:RESOLVES_TODO]->(t)" in query
    assert "SET t.status = $to_status" in query


@pytest.mark.unit
def test_reopen_todo_creates_edge_and_reopens_in_one_statement() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    result = repo.reopen_todo("t1", "m1")

    assert result["applied"] is True
    assert result["status"] == "open"
    assert len(neo4j.calls) == 1
    query = neo4j.calls[0]["query"]
    assert "MERGE (m)-[:REOPENS_TODO]->(t)" in query
    assert "t.closed_at = null" in query


@pytest.mark.unit
def test_resolve_requires_the_todo_to_be_open() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    repo.resolve_todo("t1", "m1")

    assert neo4j.calls[0]["params"]["from_status"] == "open"


@pytest.mark.unit
def test_reopen_requires_the_todo_to_be_closed() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    repo.reopen_todo("t1", "m1")

    assert neo4j.calls[0]["params"]["from_status"] == "closed"


@pytest.mark.unit
@pytest.mark.parametrize("method", ["resolve_todo", "reopen_todo"])
def test_lifecycle_refusal_reports_reason_without_raising(method: str) -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 0}]])
    repo = TodoRepository(neo4j)

    result = getattr(repo, method)("t1", "m1")

    assert result["applied"] is False
    assert "reason" in result


@pytest.mark.unit
@pytest.mark.parametrize("method", ["resolve_todo", "reopen_todo"])
def test_lifecycle_enforces_same_eligibility_as_slice_one(method: str) -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    getattr(repo, method)("t1", "m1")

    query = neo4j.calls[0]["query"]
    assert "m.scope = 'PERSISTENT'" in query
    assert "m.structure_role IS NULL" in query
    assert "t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]" in query


@pytest.mark.unit
def test_lifecycle_relations_remain_uncreatable_by_a_bare_link() -> None:
    """The edge may only arise from the transaction that also moves status."""
    repo = TodoRepository(_StubNeo4j())

    for relation in ("resolves", "reopens"):
        assert repo.link_memory_to_todo("m1", "t1", relation)["reason"] == "unsupported_relation"


@pytest.mark.unit
def test_inbound_reads_include_lifecycle_relations() -> None:
    neo4j = _StubNeo4j(responses=[[]])
    repo = TodoRepository(neo4j)

    repo.todo_inbound_links("t1")

    assert neo4j.calls[0]["params"]["edge_types"] == [
        "MENTIONS_TODO", "ADDRESSES_TODO", "RESOLVES_TODO", "REOPENS_TODO",
    ]


@pytest.mark.unit
def test_resolve_completes_a_linked_reminder() -> None:
    neo4j = _StubNeo4j(responses=[[{"applied": 1}]])
    repo = TodoRepository(neo4j)

    repo.resolve_todo("t1", "m1")

    assert neo4j.calls[0]["params"]["reminder_status"] == "completed"
    assert "HAS_REMINDER" in neo4j.calls[0]["query"]


# ---------------------------------------------------------------------------
# Namespace invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_todo_defaults_namespace_when_omitted() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Task")

    assert result["namespace"] == "default"
    assert neo4j.calls[0]["params"]["namespace"] == "default"


@pytest.mark.unit
def test_create_todo_persists_explicit_namespace() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Task", namespace="yawn.market")

    assert result["namespace"] == "yawn.market"
    assert neo4j.calls[0]["params"]["namespace"] == "yawn.market"


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_create_todo_never_stores_null_namespace(blank) -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    result = repo.create_todo(content="Task", namespace=blank)

    assert result["namespace"] == "default"


@pytest.mark.unit
def test_create_todo_writes_namespace_into_the_node() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Task")

    assert "$namespace" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_reminder_is_stamped_with_its_todos_namespace() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="x", due_date="2027-02-16", namespace="tenantA")

    # call 0 is the :Todo write, call 1 is the reminder write
    params = neo4j.calls[1]["params"]
    assert params["r_namespace"] == "tenantA"
    assert params["r_group_id"] == "tenantA"

    query = neo4j.calls[1]["query"]
    assert "$r_namespace" in query
    assert "r.group_id      = ''" not in query


@pytest.mark.unit
def test_reminder_defaults_to_the_default_namespace() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="x", due_date="2027-02-16")

    # call 0 is the :Todo write, call 1 is the reminder write
    params = neo4j.calls[1]["params"]
    assert params["r_namespace"] == "default"
    assert params["r_group_id"] == ""

    # Assert the QUERY consumes them. Checking only the params dict is vacuous: the values are
    # computed whether or not the Cypher binds them, so unstamping the node leaves this green.
    query = neo4j.calls[1]["query"]
    assert "$r_namespace" in query
    assert "$r_group_id" in query


@pytest.mark.unit
def test_list_todos_without_namespace_does_not_filter() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.list_todos()

    assert neo4j.calls[0]["params"]["namespaces"] is None


@pytest.mark.unit
def test_list_todos_with_namespace_includes_default_bucket() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.list_todos(namespace="yawn.market")

    assert neo4j.calls[0]["params"]["namespaces"] == ["yawn.market", "default"]


@pytest.mark.unit
def test_get_todo_without_namespace_does_not_filter() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.get_todo("u1")

    assert neo4j.calls[0]["params"]["namespaces"] is None


@pytest.mark.unit
def test_get_todo_with_namespace_includes_default_bucket() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.get_todo("u1", namespace="yawn.market")

    assert neo4j.calls[0]["params"]["namespaces"] == ["yawn.market", "default"]


@pytest.mark.unit
def test_add_todo_tool_passes_namespace_through() -> None:
    from menhir.mcp.tools.ops.add_todo import AddTodoTool

    tool = AddTodoTool()
    backend = MagicMock()
    backend.create_todo = AsyncMock(return_value={"uuid": "u1", "priority": "normal"})
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    asyncio.run(tool.endpoint(text="Task", namespace="yawn.seed"))

    assert backend.create_todo.await_args.kwargs["namespace"] == "yawn.seed"


@pytest.mark.unit
def test_add_todo_tool_sends_none_for_blank_namespace() -> None:
    from menhir.mcp.tools.ops.add_todo import AddTodoTool

    tool = AddTodoTool()
    backend = MagicMock()
    backend.create_todo = AsyncMock(return_value={"uuid": "u1", "priority": "normal"})
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    asyncio.run(tool.endpoint(text="Task"))

    assert backend.create_todo.await_args.kwargs["namespace"] is None


@pytest.mark.unit
def test_get_todo_tool_reports_namespace() -> None:
    from menhir.mcp.tools.ops.get_todo import GetTodoTool

    tool = GetTodoTool()
    backend = MagicMock()
    backend.get_todo = AsyncMock(
        return_value={"uuid": "u1", "content": "body", "priority": "high",
                      "status": "open", "namespace": "yawn.market"}
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(uuid="u1"))

    assert "namespace: yawn.market" in result


# ---------------------------------------------------------------------------
# TodoRepository.get_todo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_todo_returns_row_when_found() -> None:
    neo4j = _StubNeo4j(responses=[[{
        "uuid": "u1",
        "content": "A very long multi-part todo body",
        "status": "open",
        "linked_entities": ["yawn.rip"],
    }]])
    repo = TodoRepository(neo4j)

    result = repo.get_todo("u1")

    assert result is not None
    assert result["content"] == "A very long multi-part todo body"
    assert neo4j.calls[0]["params"] == {
        "uuid": "u1",
        "namespaces": None,
        "stale_after": TODO_STALE_AFTER_DAYS,
    }


@pytest.mark.unit
def test_get_todo_returns_none_when_missing() -> None:
    repo = TodoRepository(_StubNeo4j())

    assert repo.get_todo("nope") is None


@pytest.mark.unit
def test_get_todo_query_matches_todo_and_edges() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.get_todo("u1")

    query = neo4j.calls[0]["query"]
    assert "MATCH (n:Todo {uuid: $uuid})" in query
    assert "REFERENCES_FILE" in query
    assert "CREATED_FROM" in query
    assert "HAS_LOCATION" in query


@pytest.mark.unit
def test_get_todo_tool_returns_full_content_untruncated() -> None:
    from menhir.mcp.tools.ops.get_todo import GetTodoTool

    long_content = "X" * 500
    tool = GetTodoTool()
    backend = MagicMock()
    backend.get_todo = AsyncMock(
        return_value={
            "uuid": "abc",
            "content": long_content,
            "priority": "high",
            "status": "open",
            "code_ref": "src/api.py:10",
            "created_at": "2026-03-24T10:00:00+00:00",
            "closed_at": None,
            "age_days": 40,
            "stale": True,
            "linked_file_path": "src/api.py",
        }
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(uuid="abc"))

    assert long_content in result
    assert "..." not in result
    assert "[HIGH]" in result
    assert "src/api.py:10" in result
    assert "STALE" in result


@pytest.mark.unit
def test_get_todo_tool_not_found_message() -> None:
    from menhir.mcp.tools.ops.get_todo import GetTodoTool

    tool = GetTodoTool()
    backend = MagicMock()
    backend.get_todo = AsyncMock(return_value=None)
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(uuid="missing"))

    assert "TODO missing not found" in result


@pytest.mark.unit
def test_list_todos_tool_points_at_get_todo_when_truncating() -> None:
    from menhir.mcp.tools.ops.list_todos import ListTodosTool

    tool = ListTodosTool()
    backend = MagicMock()
    backend.list_todos = AsyncMock(
        return_value=[{"uuid": "abc", "content": "Y" * 200, "priority": "high"}]
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(status="open", limit=25))

    assert "get_todo(uuid)" in result


@pytest.mark.unit
def test_list_todos_tool_no_truncation_hint_for_short_content() -> None:
    from menhir.mcp.tools.ops.list_todos import ListTodosTool

    tool = ListTodosTool()
    backend = MagicMock()
    backend.list_todos = AsyncMock(
        return_value=[{"uuid": "abc", "content": "short", "priority": "high"}]
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(status="open", limit=25))

    assert "get_todo(uuid)" not in result


@pytest.mark.unit
def test_list_todos_tool_formats_output() -> None:
    from menhir.mcp.tools.ops.list_todos import ListTodosTool

    tool = ListTodosTool()
    backend = MagicMock()
    backend.list_todos = AsyncMock(
        return_value=[
            {
                "uuid": "abc",
                "content": "Fix the thing",
                "priority": "high",
                "code_ref": "src/api.py:10",
                "created_at": "2026-03-24T10:00:00+00:00",
                "closed_at": None,
            }
        ]
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(status="open", limit=25))

    assert "open (1)" in result
    assert "[HIGH]" in result
    assert "abc" in result
    assert "Fix the thing" in result
    assert "src/api.py:10" in result


@pytest.mark.unit
def test_list_todos_tool_empty() -> None:
    from menhir.mcp.tools.ops.list_todos import ListTodosTool

    tool = ListTodosTool()
    backend = MagicMock()
    backend.list_todos = AsyncMock(return_value=[])
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(status="open"))

    assert "open (0)" in result
    assert "(none)" in result


# ---------------------------------------------------------------------------
# Todo age SSOT (duration.inDays, not duration.between)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_age_ssot_uses_indays_not_between_on_every_age_read() -> None:
    """Every age-bearing query must route through TODO_AGE_DAYS_CYPHER.

    `duration.between` returns a structured duration: months are extracted
    first, so `.days` is only the sub-month remainder and a 3-month-old todo
    reports 5. That capped every displayed age at ~31, made the stale flag
    near-unreachable, and made close_stale_todos(older_than_days>=32) a
    permanent no-op. This pins the fix at every call site at once, so
    re-inlining a duration call in a new query fails here rather than
    silently under-reporting age again.
    """
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.list_todos()
    repo.get_todo("u1")
    repo.close_stale_todos(older_than_days=90, dry_run=True)

    assert len(neo4j.calls) >= 3
    for call in neo4j.calls:
        query = call["query"]
        assert "duration.between" not in query, (
            "duration.between under-reports age by extracting months first; "
            "use TODO_AGE_DAYS_CYPHER"
        )
        assert TODO_AGE_DAYS_CYPHER in query


@pytest.mark.unit
def test_stale_threshold_is_parameterized_not_inlined() -> None:
    """The staleness cutoff has one definition, passed as $stale_after."""
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.list_todos()
    repo.get_todo("u1")

    for call in neo4j.calls:
        assert "age_days > 30" not in call["query"]
        assert "$stale_after" in call["query"]
        assert call["params"]["stale_after"] == TODO_STALE_AFTER_DAYS


@pytest.mark.unit
def test_close_stale_todos_selects_on_true_total_days() -> None:
    """A 90-day cutoff must be expressible; the old expression could not match it.

    `.days` off a structured duration never exceeds ~31, so `>= 90` matched
    nothing regardless of how old a todo actually was -- and close_stale_todos
    defaults to older_than_days=60, so its default was a no-op too.
    """
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.close_stale_todos(older_than_days=90, dry_run=True)

    query = neo4j.calls[0]["query"]
    assert f"{TODO_AGE_DAYS_CYPHER} >= $days" in query
    assert neo4j.calls[0]["params"]["days"] == 90


@pytest.mark.unit
def test_stale_banner_reports_the_ssot_threshold_not_a_literal(monkeypatch) -> None:
    """The stale banner must render TODO_STALE_AFTER_DAYS, not a hardcoded 30.

    `list_todos` rendered "N todo(s) older than 30 days" from a literal while the
    `stale` flag it describes is computed server-side from TODO_STALE_AFTER_DAYS.
    That is a fourth, out-of-band encoding of the same threshold: change the
    constant and the banner silently lies about which todos it just flagged.

    The threshold is perturbed here deliberately. Asserting "older than 30 days"
    against the real constant proves nothing while the constant happens to be 30 --
    a hardcoded literal passes that assertion identically. Only a value the literal
    cannot produce distinguishes the two.
    """
    import menhir.mcp.tools.ops.list_todos as list_todos_mod
    from menhir.mcp.tools.ops.list_todos import ListTodosTool

    monkeypatch.setattr(list_todos_mod, "TODO_STALE_AFTER_DAYS", 45)

    tool = ListTodosTool()
    backend = MagicMock()
    backend.list_todos = AsyncMock(
        return_value=[
            {
                "uuid": "abc",
                "content": "Old thing",
                "priority": "normal",
                "created_at": "2026-05-28T10:00:00+00:00",
                "age_days": 98,
                "stale": True,
            }
        ]
    )
    tool.get_backend = MagicMock(return_value=backend)

    import asyncio
    result = asyncio.run(tool.endpoint(status="open", limit=25))

    assert "older than 45 days" in result
    assert "older than 30 days" not in result
    assert "age: 98d" in result
    assert "STALE" in result
