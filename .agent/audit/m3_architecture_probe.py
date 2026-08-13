#!/usr/bin/env python3
"""Read-only AST probe for the Menhir M3 MCP architecture audit.

The probe parses source without importing Menhir and writes nothing. Run it from
(or point it at) a clean checkout of the pinned audit commit:

    python .agent/audit/m3_architecture_probe.py --repo .

It prints a compact human summary followed by deterministic JSON covering the
scope manifest, package/import graph, direct and transitive in-degree, boundary
private-symbol references, duplicate-definition body hashes, duplicate tool
names, and module-level constant read candidates.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

EXPECTED_FILES = 70
EXPECTED_LINES = 7_222
MCP_PREFIX = "menhir.mcp"
HUBS = (
    "menhir.mcp.tools.base",
    "menhir.mcp.formatters",
    "menhir.mcp.service_access",
    "menhir.mcp.contracts",
    "menhir.mcp.resources",
)


def module_name(src: Path, path: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_name(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def resolve_from(module: str, path: Path, level: int, target: str | None) -> str:
    if level == 0:
        return target or ""
    parts = package_name(module, path).split(".")
    trim = level - 1
    parts = parts[: len(parts) - trim] if trim <= len(parts) else []
    if target:
        parts.extend(target.split("."))
    return ".".join(parts)


def top_package(name: str) -> str:
    parts = name.split(".")
    if parts and parts[0] == "menhir" and len(parts) > 1:
        return ".".join(parts[:2])
    return parts[0] if parts else ""


def strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def body_hash(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    normalized = ast.dump(
        ast.Module(body=strip_docstring(list(node.body)), type_ignores=[]),
        include_attributes=False,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Collector(ast.NodeVisitor):
    """Collect imports/definitions/loads while retaining import execution context."""

    def __init__(self, *, src: Path, repo: Path, path: Path, module: str) -> None:
        self.src = src
        self.repo = repo
        self.path = path
        self.module = module
        self.function_depth = 0
        self.type_checking_depth = 0
        self.class_stack: list[str] = []
        self.imports: list[dict[str, Any]] = []
        self.definitions: list[dict[str, Any]] = []
        self.constants: list[dict[str, Any]] = []
        self.name_loads: list[dict[str, Any]] = []
        self.tool_names: list[dict[str, Any]] = []
        self.module_bindings: dict[str, str] = {}
        self.attribute_uses: list[dict[str, Any]] = []

    @property
    def relpath(self) -> str:
        return self.path.relative_to(self.repo).as_posix()

    @property
    def scope(self) -> str:
        return "local" if self.function_depth else "module"

    @property
    def type_checking(self) -> bool:
        return self.type_checking_depth > 0

    def add_import(
        self,
        *,
        node: ast.Import | ast.ImportFrom,
        base: str,
        name: str | None,
        resolved: str,
        asname: str | None,
    ) -> None:
        self.imports.append(
            {
                "importer": self.module,
                "path": self.relpath,
                "line": node.lineno,
                "base": base,
                "name": name,
                "resolved": resolved,
                "asname": asname,
                "scope": self.scope,
                "type_checking": self.type_checking,
                "private_symbol": bool(name and name.startswith("_")),
            }
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.add_import(
                node=node,
                base=alias.name,
                name=None,
                resolved=alias.name,
                asname=alias.asname,
            )
            binding = alias.asname or alias.name.split(".")[0]
            target = alias.name if alias.asname else alias.name.split(".")[0]
            self.module_bindings[binding] = target

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = resolve_from(self.module, self.path, node.level, node.module)
        for alias in node.names:
            resolved = f"{base}.{alias.name}" if base and alias.name != "*" else base
            self.add_import(
                node=node,
                base=base,
                name=alias.name,
                resolved=resolved,
                asname=alias.asname,
            )
            if alias.name != "*":
                self.module_bindings[alias.asname or alias.name] = resolved

    def visit_If(self, node: ast.If) -> None:
        is_type_checking = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        if is_type_checking:
            self.type_checking_depth += 1
        for child in node.body:
            self.visit(child)
        if is_type_checking:
            self.type_checking_depth -= 1
        for child in node.orelse:
            self.visit(child)

    def record_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        parents = tuple(self.class_stack)
        self.definitions.append(
            {
                "name": node.name,
                "qualname": ".".join((*parents, node.name)),
                "module": self.module,
                "path": self.relpath,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "kind": type(node).__name__,
                "module_level": not parents and self.function_depth == 0,
                "body_sha256": body_hash(node),
            }
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.record_definition(node)
        self.function_depth += 1
        for child in node.body:
            self.visit(child)
        self.function_depth -= 1
        for decorator in node.decorator_list:
            self.visit(decorator)
        if node.returns:
            self.visit(node.returns)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.record_definition(node)
        self.class_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.class_stack.pop()
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def record_assignment(
        self, name: str, node: ast.Assign | ast.AnnAssign, value: ast.expr | None
    ) -> None:
        if self.class_stack and not self.function_depth:
            if (
                name == "name"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                self.tool_names.append(
                    {
                        "tool_name": value.value,
                        "class_name": self.class_stack[-1],
                        "module": self.module,
                        "path": self.relpath,
                        "line": node.lineno,
                    }
                )
            return
        if self.function_depth:
            return
        if re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name):
            self.constants.append(
                {
                    "name": name,
                    "module": self.module,
                    "path": self.relpath,
                    "line": node.lineno,
                }
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.record_assignment(target.id, node, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.record_assignment(node.target.id, node, node.value)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.name_loads.append(
                {
                    "module": self.module,
                    "path": self.relpath,
                    "line": node.lineno,
                    "name": node.id,
                }
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            binding = node.value.id
            target = self.module_bindings.get(binding)
            if target:
                self.attribute_uses.append(
                    {
                        "importer": self.module,
                        "path": self.relpath,
                        "line": node.lineno,
                        "binding": binding,
                        "target_candidate": target,
                        "name": node.attr,
                        "private_symbol": node.attr.startswith("_"),
                    }
                )
        self.generic_visit(node)


def resolve_module(candidate: str, modules: set[str]) -> str | None:
    current = candidate
    while current:
        if current in modules:
            return current
        current = current.rpartition(".")[0]
    return None


def target_module(row: dict[str, Any], modules: set[str]) -> str | None:
    for candidate in (row["resolved"], row["base"]):
        target = resolve_module(candidate, modules)
        if target:
            return target
    return None


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lows: dict[str, int] = {}
    result: list[list[str]] = []

    def visit(vertex: str) -> None:
        nonlocal index
        indexes[vertex] = lows[vertex] = index
        index += 1
        stack.append(vertex)
        on_stack.add(vertex)
        for neighbor in sorted(graph.get(vertex, ())):
            if neighbor not in indexes:
                visit(neighbor)
                lows[vertex] = min(lows[vertex], lows[neighbor])
            elif neighbor in on_stack:
                lows[vertex] = min(lows[vertex], indexes[neighbor])
        if lows[vertex] == indexes[vertex]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == vertex:
                    break
            if len(component) > 1 or vertex in graph.get(vertex, set()):
                result.append(sorted(component))

    for vertex in sorted(graph):
        if vertex not in indexes:
            visit(vertex)
    return sorted(result, key=lambda rows: (-len(rows), rows))


def build_graph(
    *,
    modules: set[str],
    selected_modules: set[str],
    imports: list[dict[str, Any]],
    eager_only: bool,
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    graph = {module: set() for module in selected_modules}
    sites: defaultdict[str, list[str]] = defaultdict(list)
    for row in imports:
        if row["importer"] not in selected_modules:
            continue
        if eager_only and (row["scope"] != "module" or row["type_checking"]):
            continue
        target = target_module(row, modules)
        if target in selected_modules and target != row["importer"]:
            graph[row["importer"]].add(target)
            sites[f"{row['importer']} -> {target}"].append(
                f"{row['path']}:{row['line']}"
            )
    return graph, dict(sorted(sites.items()))


def reverse_transitive_counts(graph: dict[str, set[str]]) -> dict[str, int]:
    reverse: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    result: dict[str, int] = {}
    for module in graph:
        seen: set[str] = set()
        pending = list(reverse[module])
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(reverse[current] - seen)
        result[module] = len(seen)
    return result


def direction(importer: str, target: str) -> str:
    source_inside = importer == MCP_PREFIX or importer.startswith(MCP_PREFIX + ".")
    target_inside = target == MCP_PREFIX or target.startswith(MCP_PREFIX + ".")
    if source_inside and target_inside:
        return "mcp_to_mcp"
    if source_inside and not target_inside:
        return "mcp_to_outside"
    if not source_inside and target_inside:
        return "outside_to_mcp"
    return "outside_other"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()

    repo = args.repo.resolve()
    src = repo / "src"
    menhir_root = src / "menhir"
    mcp_root = menhir_root / "mcp"
    if not mcp_root.is_dir():
        print(f"ERROR: missing {mcp_root}", file=sys.stderr)
        return 2

    all_files = sorted(menhir_root.rglob("*.py"))
    mcp_files = sorted(mcp_root.rglob("*.py"))
    parsed: dict[Path, tuple[str, ast.Module, Collector]] = {}
    parse_errors: list[dict[str, Any]] = []
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            parse_errors.append(
                {
                    "path": path.relative_to(repo).as_posix(),
                    "line": exc.lineno,
                    "error": str(exc),
                }
            )
            continue
        module = module_name(src, path)
        collector = Collector(src=src, repo=repo, path=path, module=module)
        collector.visit(tree)
        parsed[path] = (text, tree, collector)

    modules = {module_name(src, path) for path in parsed}
    mcp_modules = {module_name(src, path) for path in mcp_files if path in parsed}
    imports = [row for _, _, collector in parsed.values() for row in collector.imports]
    definitions = [row for _, _, collector in parsed.values() for row in collector.definitions]
    constants = [row for _, _, collector in parsed.values() for row in collector.constants]
    name_loads = [row for _, _, collector in parsed.values() for row in collector.name_loads]
    tool_names = [row for _, _, collector in parsed.values() for row in collector.tool_names]

    attribute_uses: list[dict[str, Any]] = []
    for _, _, collector in parsed.values():
        for row in collector.attribute_uses:
            target = resolve_module(row["target_candidate"], modules)
            if target:
                attribute_uses.append({**row, "target_module": target})

    full_graph, full_sites = build_graph(
        modules=modules,
        selected_modules=mcp_modules,
        imports=imports,
        eager_only=False,
    )
    eager_graph, eager_sites = build_graph(
        modules=modules,
        selected_modules=mcp_modules,
        imports=imports,
        eager_only=True,
    )

    importers: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in full_graph.items():
        for target in targets:
            importers[target].add(source)
    transitive = reverse_transitive_counts(full_graph)

    mcp_imports = [row for row in imports if row["importer"] in mcp_modules]
    package_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mcp_imports:
        package_rows[top_package(row["base"] or row["resolved"])].append(row)
    layering = {
        package: {
            "alias_edge_count": len(rows),
            "file_count": len({row["path"] for row in rows}),
            "files": sorted({row["path"] for row in rows}),
            "edges": sorted(rows, key=lambda row: (row["path"], row["line"], str(row["name"]))),
        }
        for package, rows in sorted(package_rows.items())
    }

    definition_sites: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in definitions:
        if row["module_level"]:
            definition_sites[(row["module"], row["name"])].append(
                f"{row['path']}:{row['line']}"
            )
    for path, (_, tree, _) in parsed.items():
        module = module_name(src, path)
        for node in tree.body:
            targets: list[ast.Name] = []
            if isinstance(node, ast.Assign):
                targets = [target for target in node.targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
            for target in targets:
                definition_sites[(module, target.id)].append(
                    f"{path.relative_to(repo).as_posix()}:{node.lineno}"
                )

    private_refs: list[dict[str, Any]] = []
    for row in imports:
        if not row["private_symbol"] or not row["name"]:
            continue
        target = target_module(row, modules)
        if not target or target == row["importer"]:
            continue
        private_refs.append(
            {
                "kind": "from_import",
                "direction": direction(row["importer"], target),
                "importer": row["importer"],
                "importer_site": f"{row['path']}:{row['line']}",
                "target_module": target,
                "symbol": row["name"],
                "definition_sites": sorted(
                    set(definition_sites.get((target, row["name"]), []))
                ),
                "scope": row["scope"],
                "type_checking": row["type_checking"],
            }
        )
    for row in attribute_uses:
        if not row["private_symbol"] or row["target_module"] == row["importer"]:
            continue
        private_refs.append(
            {
                "kind": "module_attribute",
                "direction": direction(row["importer"], row["target_module"]),
                "importer": row["importer"],
                "importer_site": f"{row['path']}:{row['line']}",
                "target_module": row["target_module"],
                "symbol": row["name"],
                "definition_sites": sorted(
                    set(
                        definition_sites.get(
                            (row["target_module"], row["name"]), []
                        )
                    )
                ),
            }
        )
    private_refs.sort(
        key=lambda row: (
            row["direction"],
            row["importer_site"],
            row["target_module"],
            row["symbol"],
        )
    )

    module_defs = [
        row for row in definitions if row["module"] in mcp_modules and row["module_level"]
    ]
    by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    by_body: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in module_defs:
        by_name[row["name"]].append(row)
        by_body[row["body_sha256"]].append(row)
    duplicate_module_definitions = [
        {
            "name": name,
            "distinct_bodies": len({row["body_sha256"] for row in rows}),
            "definitions": sorted(rows, key=lambda row: (row["path"], row["line"])),
        }
        for name, rows in sorted(by_name.items())
        if len({row["path"] for row in rows}) > 1
    ]
    identical_module_bodies = [
        {
            "body_sha256": digest,
            "definitions": sorted(rows, key=lambda row: (row["path"], row["line"])),
        }
        for digest, rows in sorted(by_body.items())
        if len({row["path"] for row in rows}) > 1
    ]

    methods = [
        row for row in definitions if row["module"] in mcp_modules and not row["module_level"]
    ]
    methods_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in methods:
        methods_by_name[row["name"]].append(row)
    duplicate_method_definitions = [
        {
            "name": name,
            "definition_count": len(rows),
            "distinct_bodies": len({row["body_sha256"] for row in rows}),
            "identical_body_groups": [
                sorted(
                    [item for item in rows if item["body_sha256"] == digest],
                    key=lambda item: (item["path"], item["line"]),
                )
                for digest in sorted({row["body_sha256"] for row in rows})
                if sum(1 for item in rows if item["body_sha256"] == digest) > 1
            ],
        }
        for name, rows in sorted(methods_by_name.items())
        if len({row["path"] for row in rows}) > 1
    ]

    tools_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in tool_names:
        if row["module"] in mcp_modules:
            tools_by_name[row["tool_name"]].append(row)
    duplicate_tool_names = [
        {
            "tool_name": name,
            "definitions": sorted(rows, key=lambda row: (row["path"], row["line"])),
        }
        for name, rows in sorted(tools_by_name.items())
        if len(rows) > 1
    ]

    same_module_loads: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in name_loads:
        same_module_loads[(row["module"], row["name"])].append(
            f"{row['path']}:{row['line']}"
        )
    imported_symbol_sites: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in imports:
        if not row["name"] or row["name"] == "*":
            continue
        target = target_module(row, modules)
        if target:
            imported_symbol_sites[(target, row["name"])].append(
                f"{row['path']}:{row['line']}"
            )
    attribute_symbol_sites: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in attribute_uses:
        attribute_symbol_sites[(row["target_module"], row["name"])].append(
            f"{row['path']}:{row['line']}"
        )

    mcp_constants: list[dict[str, Any]] = []
    for row in constants:
        if row["module"] not in mcp_modules:
            continue
        reads = sorted(
            set(
                same_module_loads.get((row["module"], row["name"]), [])
                + imported_symbol_sites.get((row["module"], row["name"]), [])
                + attribute_symbol_sites.get((row["module"], row["name"]), [])
            )
        )
        mcp_constants.append({**row, "read_sites": reads, "statically_unread": not reads})

    file_rows: list[dict[str, Any]] = []
    for path in mcp_files:
        text = parsed[path][0]
        file_rows.append(
            {
                "path": path.relative_to(repo).as_posix(),
                "physical_lines": len(text.splitlines()),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    scope = {
        "file_count": len(file_rows),
        "physical_lines": sum(row["physical_lines"] for row in file_rows),
        "files": file_rows,
    }

    hubs = {
        module: {
            "direct_in_degree": len(importers[module]),
            "direct_importers": sorted(importers[module]),
            "reverse_transitive_dependents": transitive.get(module, 0),
        }
        for module in HUBS
    }

    controls = {
        "scope_file_count_70": scope["file_count"] == EXPECTED_FILES,
        "scope_physical_lines_7222": scope["physical_lines"] == EXPECTED_LINES,
        "no_parse_errors": not parse_errors,
        "BaseTool_definition_visible": bool(
            definition_sites.get(("menhir.mcp.contracts", "BaseTool"))
        ),
        "tools_base_to_contracts_edge_visible": (
            "menhir.mcp.contracts" in full_graph.get("menhir.mcp.tools.base", set())
        ),
        "outside_private_control_visible": any(
            row["direction"] == "outside_to_mcp"
            and row["importer"] == "menhir.api.mcp_remote"
            and row["target_module"] == "menhir.mcp.contracts"
            and row["symbol"] == "_tier_allows"
            for row in private_refs
        ),
    }

    result = {
        "target": {
            "expected_files": EXPECTED_FILES,
            "expected_physical_lines": EXPECTED_LINES,
        },
        "controls": controls,
        "parse_errors": parse_errors,
        "scope": scope,
        "layering": layering,
        "import_graph": {
            "full_cycles": strongly_connected_components(full_graph),
            "eager_cycles": strongly_connected_components(eager_graph),
            "full_edge_sites": full_sites,
            "eager_edge_sites": eager_sites,
            "in_degree": {
                module: {
                    "direct_in_degree": len(importers[module]),
                    "direct_importers": sorted(importers[module]),
                    "reverse_transitive_dependents": transitive.get(module, 0),
                }
                for module in sorted(mcp_modules)
            },
            "hubs": hubs,
        },
        "private_symbol_references": private_refs,
        "cross_boundary_private_references": [
            row
            for row in private_refs
            if row["direction"] in {"mcp_to_outside", "outside_to_mcp"}
        ],
        "duplicate_module_definitions": duplicate_module_definitions,
        "identical_module_bodies": identical_module_bodies,
        "duplicate_method_definitions": duplicate_method_definitions,
        "duplicate_tool_names": duplicate_tool_names,
        "module_constants": mcp_constants,
        "unread_module_constants": [
            row for row in mcp_constants if row["statically_unread"]
        ],
    }

    print("M3 ARCHITECTURE PROBE")
    print(f"scope: {scope['file_count']} files / {scope['physical_lines']} physical lines")
    print("controls:")
    for name, passed in controls.items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    print("layering (alias edges / distinct files):")
    for package, row in layering.items():
        print(f"  {package}: {row['alias_edge_count']} / {row['file_count']}")
    print("hub in-degree (direct / reverse-transitive):")
    for module, row in hubs.items():
        print(
            f"  {module}: {row['direct_in_degree']} / "
            f"{row['reverse_transitive_dependents']}"
        )
    print("full MCP cycles:", result["import_graph"]["full_cycles"] or "NONE")
    print("eager MCP cycles:", result["import_graph"]["eager_cycles"] or "NONE")
    print(
        "cross-boundary private references:",
        len(result["cross_boundary_private_references"]),
    )
    print("duplicate module-level names:", len(duplicate_module_definitions))
    for row in duplicate_module_definitions:
        relation = "IDENTICAL" if row["distinct_bodies"] == 1 else "DIVERGENT"
        print(f"  {relation} {row['name']} ({row['distinct_bodies']} bodies)")
    print("duplicate MCP tool names:", len(duplicate_tool_names))
    print("unread module constants:", len(result["unread_module_constants"]))
    for row in result["unread_module_constants"]:
        print(f"  {row['path']}:{row['line']} {row['name']}")
    print("--- JSON ---")
    print(json.dumps(result, indent=2, sort_keys=True))

    failed = [name for name, passed in controls.items() if not passed]
    if failed:
        print("ERROR: control checks failed: " + ", ".join(failed), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
