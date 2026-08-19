"""Counterexample tests for HIGH remediation wave 2 (CF-126, CF-147, CF-64, CF-215).

Each test reproduces the scenario the register recorded, not the shape of the fix.

The contract these are judged against is `menhir.domain.namespace`: `group_id` is the
load-bearing isolation boundary, `namespace` on a node is a defense-in-depth stamp, and
isolation is OPT-IN -- an unspecified namespace must not filter.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


class _RecordingNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return list(self._rows)


# ---------------------------------------------------------------------------
# CF-126 -- query_blast_radius leaked cross-namespace memory previews
# ---------------------------------------------------------------------------


def _linked_memories_call(namespace: str | None) -> tuple[str, dict[str, Any]]:
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    neo4j = _RecordingNeo4j()
    writer = StructureGraphWriter(neo4j=neo4j)
    writer.query_linked_memories("proj", ["src/main.py"], limit=10, namespace=namespace)
    return neo4j.calls[0]


def test_cf126_a_named_namespace_constrains_the_memory_preview_fetch() -> None:
    """The leak: `sem.structure_role IS NULL` selects FOR real tenant memories, and the
    query had no tenancy predicate at all, at readonly tier."""
    query, params = _linked_memories_call("tenant-a")

    assert "sem.group_id IN $group_ids" in query
    assert params["group_ids"] == ["tenant-a"]


def test_cf126_the_default_namespace_maps_to_the_empty_group_id() -> None:
    """`namespace_to_group_ids("default")` is `[""]`, not `["default"]` -- the two value
    spaces are not interchangeable."""
    _query, params = _linked_memories_call("default")

    assert params["group_ids"] == [""]


def test_cf126_an_unspecified_namespace_does_not_filter() -> None:
    """Isolation is opt-in. A null-guarded predicate would be equivalent here, but the
    params must stay clean so an unscoped read is byte-identical to before."""
    query, params = _linked_memories_call(None)

    assert "group_id" not in query
    assert "group_ids" not in params


def test_cf126_blast_radius_threads_its_namespace_into_the_memory_fetch() -> None:
    """The parameter existed on query_blast_radius and reached exactly one of its four
    sub-queries; the memory fetch two lines above was exempt."""
    source = (_SRC / "infrastructure/structure_queries.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "query_blast_radius"
    )
    call = next(
        c
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and getattr(c.func, "attr", None) == "query_linked_memories"
    )
    assert "namespace" in {kw.arg for kw in call.keywords}


# ---------------------------------------------------------------------------
# CF-147 -- a group_id compared against a namespace NAME
# ---------------------------------------------------------------------------


def test_cf147_history_view_read_does_not_coalesce_group_id_to_the_name_default() -> None:
    """`coalesce(n.group_id, 'default') = $namespace` compares the two value spaces.
    coalesce replaces NULL, not '', so a view written with no namespace (group_id "")
    could never match any namespace name a caller can pass."""
    from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin

    class _Repo(ScalarViewRepositoryMixin):
        def __init__(self, neo4j: Any) -> None:
            self.neo4j = neo4j

    neo4j = _RecordingNeo4j()
    _Repo(neo4j).list_scalar_history_views_for_namespace(namespace="", limit=10)
    query, params = neo4j.calls[0]

    assert "coalesce(n.group_id, 'default')" not in query
    assert "n.group_id = $namespace" in query
    # The unspecified namespace must reach the "" group id the write path produces.
    assert params["namespace"] == ""


def test_cf147_history_read_matches_its_sibling_convention() -> None:
    """The sibling 20 lines above is the author's own convention and was already correct."""
    source = (_SRC / "infrastructure/scalar_view_repository.py").read_text(encoding="utf-8")
    assert source.count("coalesce(n.group_id, 'default')") == 0


# ---------------------------------------------------------------------------
# CF-64 -- the pin cannot reach a UUID-addressed tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path,class_name",
    [
        ("mcp/tools/ingest/delete_memory.py", "DeleteMemoryTool"),
        ("mcp/tools/ingest/flag_memory.py", "FlagMemoryTool"),
    ],
)
def test_cf64_uuid_addressed_tools_declare_namespace(module_path: str, class_name: str) -> None:
    """_apply_pinned_namespace is pure signature introspection: a tool whose endpoint does
    not name `namespace` can never receive the pin, however it is configured."""
    tree = ast.parse((_SRC / module_path).read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    endpoint = next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"
    )
    params = [a.arg for a in endpoint.args.args + endpoint.args.kwonlyargs]
    assert "namespace" in params


class _Backend:
    """Mirrors fetch_memory_by_uuid's real namespace semantics: the row is returned only
    when the node's own namespace matches the one asked for."""

    def __init__(self, node_namespace: str | None) -> None:
        self.node_namespace = node_namespace
        self.erased: list[str] = []
        self.flagged: list[str] = []

    async def fetch_memory_by_uuid(self, node_uuid: str, *, namespace: str | None = None):
        if self.node_namespace is None:
            return None
        if namespace is not None and namespace != self.node_namespace:
            return None
        return {"uuid": node_uuid}

    async def erase_memory(self, node_uuid: str):
        self.erased.append(node_uuid)
        return {"reason": "erased", "purged": {"x": 1}}

    async def flag_memory(self, node_uuid: str, *, bootstrap_scope=None):
        self.flagged.append(node_uuid)
        return True


def _tool_with_backend(tool, backend):
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


@pytest.mark.asyncio
async def test_cf64_pinned_caller_cannot_erase_another_silos_node() -> None:
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    backend = _Backend(node_namespace="tenant-b")
    tool = _tool_with_backend(DeleteMemoryTool(), backend)

    result = await tool.endpoint("uuid-1", namespace="tenant-a")

    assert "Refused" in result
    assert backend.erased == []


@pytest.mark.asyncio
async def test_cf64_pinned_caller_can_erase_its_own_node() -> None:
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    backend = _Backend(node_namespace="tenant-a")
    tool = _tool_with_backend(DeleteMemoryTool(), backend)

    await tool.endpoint("uuid-1", namespace="tenant-a")

    assert backend.erased == ["uuid-1"]


@pytest.mark.asyncio
async def test_cf64_guard_does_not_block_erasing_residual_content_of_an_absent_node() -> None:
    """`graph_already_absent` is a SUPPORTED erasure outcome -- it is how a merge leaves the
    node it absorbed, whose stored content must still be erasable. A guard that refused
    whenever the node was not found in this namespace would break that path."""
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    backend = _Backend(node_namespace=None)  # not in the graph at all
    tool = _tool_with_backend(DeleteMemoryTool(), backend)

    await tool.endpoint("uuid-gone", namespace="tenant-a")

    assert backend.erased == ["uuid-gone"]


@pytest.mark.asyncio
async def test_cf64_unpinned_caller_is_unaffected() -> None:
    from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

    backend = _Backend(node_namespace="tenant-b")
    tool = _tool_with_backend(DeleteMemoryTool(), backend)

    await tool.endpoint("uuid-1")

    assert backend.erased == ["uuid-1"]


@pytest.mark.asyncio
async def test_cf64_pinned_caller_cannot_flag_another_silos_node() -> None:
    from menhir.mcp.tools.ingest.flag_memory import FlagMemoryTool

    backend = _Backend(node_namespace="tenant-b")
    tool = _tool_with_backend(FlagMemoryTool(), backend)

    result = await tool.endpoint("uuid-1", namespace="tenant-a")

    assert "Refused" in result
    assert backend.flagged == []


# ---------------------------------------------------------------------------
# CF-215 -- the one :Entity writer that omitted the tenancy property
# ---------------------------------------------------------------------------


def test_cf215_raw_captures_are_stamped_with_group_id() -> None:
    """Raw captures exist to make a terminally-failed episode's text reachable by recall.
    Namespace-scoped recall predicates on group_id, so a capture written without one was
    invisible to exactly the reads it exists for."""
    from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository

    neo4j = _RecordingNeo4j(rows=[{"entity_uuid": "cap-1"}])
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = neo4j
    repo.create_raw_capture_entity(
        episode_uuid="ep-1",
        name="n",
        content="c",
        namespace="tenant-a",
        session_id="s",
        user_id="u",
        source="claude-code",
    )

    query, params = neo4j.calls[0]
    assert "n.group_id = $group_id" in query
    assert params["group_id"] == "tenant-a"
    assert params["namespace"] == "tenant-a"


def test_cf215_default_namespace_capture_gets_the_empty_group_id() -> None:
    from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository

    neo4j = _RecordingNeo4j(rows=[{"entity_uuid": "cap-1"}])
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = neo4j
    repo.create_raw_capture_entity(
        episode_uuid="ep-1",
        name="n",
        content="c",
        namespace="default",
        session_id="s",
        user_id="u",
        source="claude-code",
    )

    _query, params = neo4j.calls[0]
    assert params["group_id"] == ""


def test_cf215_every_entity_writer_stamps_group_id() -> None:
    """The invariant, not the instance: seven of eight writers already did this, which is
    what made the eighth a defect rather than a design."""
    import re

    pattern = re.compile(
        r"""(?:MERGE|CREATE)\s*\(\s*\w*\s*:Entity"""
        r"""|\.(?:merge|create)\(\s*\n?\s*["'(]+\s*\(?\w*:Entity""",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not pattern.search(text):
            continue
        if not re.search(r"group_id\s*[:=]", text):
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == []
