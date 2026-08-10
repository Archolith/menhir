"""Architecture checks for the one-way backend/MCP dependency boundary."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from menhir.core.backend_config import resolve_backend_auth_key
from menhir.infrastructure.project_scanner import (
    CrossProjectRef,
    DirEntry,
    EndpointEntry,
    FileEntry,
    ProjectScanResult,
    TestEdge as ProjectTestEdge,
)
from menhir.mcp.service_access import resolve_mcp_backend_auth_key
from menhir.services.project_ingest import build_project_narrative


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _module_name(source_root: Path, path: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _internal_import_graph(source_root: Path) -> dict[str, set[str]]:
    paths = list((source_root / "menhir").rglob("*.py"))
    modules = {_module_name(source_root, path): path for path in paths}
    graph = {module: set() for module in modules}

    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            candidates: set[str] = set()
            if isinstance(node, ast.Import):
                candidates.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if node.level:
                    imported = importlib.util.resolve_name(
                        "." * node.level + imported, package
                    )
                if imported:
                    candidates.add(imported)
                    candidates.update(
                        f"{imported}.{alias.name}" for alias in node.names
                    )
            graph[module].update(
                candidate for candidate in candidates if candidate in modules
            )
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for target in graph[current] - seen:
            seen.add(target)
            pending.append(target)
    return seen


def test_core_imports_neither_mcp_nor_its_framework() -> None:
    core_root = Path(__file__).parents[1] / "src" / "menhir" / "core"
    forbidden_roots = {
        "archolith_mcp_framework",
        "cth_mcp_framework",
        "fastmcp",
        "mcp",
    }
    violations = {
        str(path.relative_to(core_root)): sorted(
            module
            for module in _imports_in(path)
            if module == "menhir.mcp"
            or module.startswith("menhir.mcp.")
            or module.split(".", 1)[0] in forbidden_roots
        )
        for path in core_root.rglob("*.py")
    }
    assert not {path: modules for path, modules in violations.items() if modules}


def test_no_import_cycle_spans_core_and_mcp() -> None:
    source_root = Path(__file__).parents[1] / "src"
    graph = _internal_import_graph(source_root)
    core_modules = {module for module in graph if module.startswith("menhir.core")}
    mcp_modules = {module for module in graph if module.startswith("menhir.mcp")}
    violations: list[tuple[str, str]] = []

    for core_module in core_modules:
        core_reachable = _reachable(graph, core_module)
        for mcp_module in core_reachable & mcp_modules:
            if core_module in _reachable(graph, mcp_module):
                violations.append((core_module, mcp_module))

    assert not violations


def test_mcp_backend_auth_helper_remains_a_compatibility_wrapper() -> None:
    assert resolve_mcp_backend_auth_key.__module__ == "menhir.mcp.service_access"
    assert resolve_backend_auth_key.__module__ == "menhir.core.backend_config"


def test_project_narrative_is_owned_by_the_application_service() -> None:
    scan = ProjectScanResult(
        name="menhir",
        root_path="/workspace/menhir",
        stack="Python",
        description="Graph memory service",
        directories=[DirEntry("src", "source")],
        files=[
            FileEntry("src/menhir/main.py", role="entrypoint", description="CLI"),
            FileEntry("src/menhir/core.py"),
        ],
        dependencies=["fastmcp", "neo4j"],
        test_edges=[ProjectTestEdge("tests/test_core.py", "src/menhir/core.py")],
        endpoints=[EndpointEntry("recall_memories", "mcp/recall.py", "mcp_tool")],
        cross_project_refs=[CrossProjectRef("bench", "import", "fixture")],
    )

    narrative = build_project_narrative(scan)

    assert narrative.startswith(
        'Project "menhir" is a Python project at /workspace/menhir.'
    )
    assert "- src/menhir/main.py — CLI" in narrative
    assert "- tests/test_core.py tests src/menhir/core.py" in narrative
    assert "Dependencies: fastmcp, neo4j" in narrative
    assert "menhir → bench via import (fixture)" in narrative


def test_project_ingest_tool_keeps_workflow_dependencies_outside_mcp() -> None:
    tool_path = (
        Path(__file__).parents[1]
        / "src"
        / "menhir"
        / "mcp"
        / "tools"
        / "ingest"
        / "ingest_project.py"
    )
    imports = _imports_in(tool_path)

    assert "menhir.services.project_ingest" in imports
    assert "asyncio" not in imports
    assert "os" not in imports
