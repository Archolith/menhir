#!/usr/bin/env python3
"""Read-only AST probe for Menhir M3 (``src/menhir/mcp``).

It imports no Menhir code and writes nothing. Run from a clean checkout:

    python .agent/audit/m3_architecture_probe.py --repo .
    python .agent/audit/m3_architecture_probe.py --self-test
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
MCP = "menhir.mcp"
HUBS = (
    "menhir.mcp.tools.base",
    "menhir.mcp.formatters",
    "menhir.mcp.service_access",
    "menhir.mcp.contracts",
    "menhir.mcp.resources",
)


def modname(src: Path, path: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package(mod: str, path: Path) -> str:
    return mod if path.name == "__init__.py" else mod.rpartition(".")[0]


def resolve_from(mod: str, path: Path, level: int, target: str | None) -> str:
    if not level:
        return target or ""
    parts = package(mod, path).split(".")
    trim = level - 1
    parts = parts[: len(parts) - trim] if trim <= len(parts) else []
    if target:
        parts.extend(target.split("."))
    return ".".join(parts)


def top_package(name: str) -> str:
    parts = name.split(".")
    return ".".join(parts[:2]) if parts[:1] == ["menhir"] and len(parts) > 1 else (parts[0] if parts else "")


def body_hash(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    raw = ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class Collector(ast.NodeVisitor):
    def __init__(self, *, repo: Path, src: Path, path: Path, module: str) -> None:
        self.repo, self.src, self.path, self.module = repo, src, path, module
        self.function_depth = 0
        self.type_checking_depth = 0
        self.class_stack: list[str] = []
        self.imports: list[dict[str, Any]] = []
        self.attributes: list[dict[str, Any]] = []
        self.definitions: list[dict[str, Any]] = []
        self.constants: list[dict[str, Any]] = []
        self.loads: list[dict[str, Any]] = []
        self.tool_names: list[dict[str, Any]] = []
        self.bindings: dict[str, str] = {}

    @property
    def relpath(self) -> str:
        return self.path.relative_to(self.repo).as_posix()

    def _import(self, node: ast.AST, base: str, name: str | None, resolved: str, asname: str | None) -> None:
        self.imports.append(
            {
                "importer": self.module,
                "path": self.relpath,
                "line": node.lineno,
                "base": base,
                "name": name,
                "resolved": resolved,
                "asname": asname,
                "scope": "local" if self.function_depth else "module",
                "type_checking": bool(self.type_checking_depth),
                "private": bool(name and name.startswith("_")),
            }
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._import(node, alias.name, None, alias.name, alias.asname)
            binding = alias.asname or alias.name.split(".")[0]
            self.bindings[binding] = alias.name if alias.asname else alias.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = resolve_from(self.module, self.path, node.level, node.module)
        for alias in node.names:
            resolved = f"{base}.{alias.name}" if base and alias.name != "*" else base
            self._import(node, base, alias.name, resolved, alias.asname)
            if alias.name != "*":
                self.bindings[alias.asname or alias.name] = resolved

    def visit_If(self, node: ast.If) -> None:
        guarded = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        self.type_checking_depth += int(guarded)
        for child in node.body:
            self.visit(child)
        self.type_checking_depth -= int(guarded)
        for child in node.orelse:
            self.visit(child)

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
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
                "module_level": not parents and not self.function_depth,
                "body_sha256": body_hash(node),
            }
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node)
        self.function_depth += 1
        self.generic_visit(node)  # includes annotations and defaults
        self.function_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node)
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _assignment(self, name: str, node: ast.AST, value: ast.expr | None) -> None:
        if self.class_stack and not self.function_depth:
            if name == "name" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.tool_names.append(
                    {
                        "tool_name": value.value,
                        "class_name": self.class_stack[-1],
                        "module": self.module,
                        "path": self.relpath,
                        "line": node.lineno,
                    }
                )
        elif not self.function_depth and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name):
            self.constants.append(
                {"name": name, "module": self.module, "path": self.relpath, "line": node.lineno}
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._assignment(target.id, node, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._assignment(node.target.id, node, node.value)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loads.append(
                {"module": self.module, "path": self.relpath, "line": node.lineno, "name": node.id}
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            parts: list[str] = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and parts and cur.id in self.bindings:
                ordered = list(reversed(parts))
                prefix = ordered[:-1]
                target = ".".join((self.bindings[cur.id], *prefix)) if prefix else self.bindings[cur.id]
                self.attributes.append(
                    {
                        "importer": self.module,
                        "path": self.relpath,
                        "line": node.lineno,
                        "target_candidate": target,
                        "name": ordered[-1],
                        "private": ordered[-1].startswith("_"),
                    }
                )
        self.generic_visit(node)


def nearest_module(candidate: str, modules: set[str]) -> str | None:
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def import_target(row: dict[str, Any], modules: set[str]) -> str | None:
    return nearest_module(row["resolved"], modules) or nearest_module(row["base"], modules)


def scc(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on: set[str] = set()
    indexes: dict[str, int] = {}
    low: dict[str, int] = {}
    out: list[list[str]] = []

    def visit(v: str) -> None:
        nonlocal index
        indexes[v] = low[v] = index
        index += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in indexes:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], indexes[w])
        if low[v] == indexes[v]:
            group: list[str] = []
            while True:
                w = stack.pop()
                on.remove(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1 or v in graph.get(v, set()):
                out.append(sorted(group))

    for vertex in sorted(graph):
        if vertex not in indexes:
            visit(vertex)
    return sorted(out, key=lambda group: (-len(group), group))


def build_graph(
    imports: list[dict[str, Any]], modules: set[str], selected: set[str], *, eager: bool
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    graph = {module: set() for module in selected}
    sites: defaultdict[str, list[str]] = defaultdict(list)
    for row in imports:
        if row["importer"] not in selected:
            continue
        if eager and (row["scope"] != "module" or row["type_checking"]):
            continue
        target = import_target(row, modules)
        if target in selected and target != row["importer"]:
            graph[row["importer"]].add(target)
            sites[f"{row['importer']} -> {target}"].append(f"{row['path']}:{row['line']}")
    return graph, dict(sorted(sites.items()))


def transitive_dependents(graph: dict[str, set[str]]) -> dict[str, int]:
    reverse: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    counts: dict[str, int] = {}
    for module in graph:
        seen: set[str] = set()
        pending = list(reverse[module])
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(reverse[current] - seen)
        counts[module] = len(seen)
    return counts


def direction(source: str, target: str) -> str:
    source_mcp = source == MCP or source.startswith(MCP + ".")
    target_mcp = target == MCP or target.startswith(MCP + ".")
    if source_mcp and target_mcp:
        return "mcp_to_mcp"
    if source_mcp:
        return "mcp_to_outside"
    if target_mcp:
        return "outside_to_mcp"
    return "outside_other"


def duplicate_groups(rows: list[dict[str, Any]], *, module_level: bool) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["module_level"] == module_level:
            grouped[row["name"]].append(row)
    return [
        {
            "name": name,
            "definition_count": len(items),
            "distinct_bodies": len({item["body_sha256"] for item in items}),
            "definitions": sorted(items, key=lambda item: (item["path"], item["line"])),
            "identical_body_groups": [
                sorted(
                    [item for item in items if item["body_sha256"] == digest],
                    key=lambda item: (item["path"], item["line"]),
                )
                for digest in sorted({item["body_sha256"] for item in items})
                if sum(item["body_sha256"] == digest for item in items) > 1
            ],
        }
        for name, items in sorted(grouped.items())
        if len({item["path"] for item in items}) > 1
    ]


def run_self_test() -> int:
    repo = Path("/probe-self-test")
    src = repo / "src"
    path = src / "menhir/mcp/sample.py"
    tree = ast.parse(
        """from typing import TYPE_CHECKING
from menhir.mcp.contracts import BaseTool
import menhir.mcp.contracts
if TYPE_CHECKING:
    from fastmcp import FastMCP
DEFAULT_LIMIT = 5
def helper(limit: int = DEFAULT_LIMIT):
    from menhir.mcp.tools import ALL_TOOLS
    return menhir.mcp.contracts._tier_allows('agent', 'readonly')
class DemoTool(BaseTool):
    name = 'demo'
    async def endpoint(self):
        return 'ok'
"""
    )
    c = Collector(repo=repo, src=src, path=path, module="menhir.mcp.sample")
    c.visit(tree)
    assert any(row["name"] == "FastMCP" and row["type_checking"] for row in c.imports)
    assert any(row["name"] == "ALL_TOOLS" and row["scope"] == "local" for row in c.imports)
    assert any(row["name"] == "DEFAULT_LIMIT" for row in c.loads)
    assert any(
        row["target_candidate"] == "menhir.mcp.contracts"
        and row["name"] == "_tier_allows"
        and row["private"]
        for row in c.attributes
    )
    assert [row["tool_name"] for row in c.tool_names] == ["demo"]
    a = ast.parse("def f():\n 'one'\n return 1\n").body[0]
    b = ast.parse("def f():\n 'two'\n return 1\n").body[0]
    d = ast.parse("def f():\n return 2\n").body[0]
    assert isinstance(a, ast.FunctionDef) and isinstance(b, ast.FunctionDef) and isinstance(d, ast.FunctionDef)
    assert body_hash(a) == body_hash(b) and body_hash(a) != body_hash(d)
    assert scc({"a": {"b"}, "b": {"a"}, "c": set()}) == [["a", "b"]]
    assert resolve_from("menhir.mcp.tools", src / "menhir/mcp/tools/__init__.py", 1, "recall") == "menhir.mcp.tools.recall"
    assert direction("menhir.api.mcp_remote", "menhir.mcp.contracts") == "outside_to_mcp"
    print(
        "SELF-TEST PASS: relative imports; local/TYPE_CHECKING context; "
        "function-default loads; dotted private module attributes; body hashes; SCCs"
    )
    return 0


def analyze(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    src = repo / "src"
    root = src / "menhir"
    mcp_root = root / "mcp"
    if not mcp_root.is_dir():
        raise FileNotFoundError(f"missing {mcp_root}")

    parsed: dict[Path, tuple[str, Collector]] = {}
    parse_errors: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            parse_errors.append(
                {"path": path.relative_to(repo).as_posix(), "line": exc.lineno, "error": str(exc)}
            )
            continue
        collector = Collector(repo=repo, src=src, path=path, module=modname(src, path))
        collector.visit(tree)
        parsed[path] = (text, collector)

    modules = {modname(src, path) for path in parsed}
    mcp_files = sorted(mcp_root.rglob("*.py"))
    mcp_modules = {modname(src, path) for path in mcp_files if path in parsed}
    imports = [row for _, c in parsed.values() for row in c.imports]
    definitions = [row for _, c in parsed.values() for row in c.definitions]
    constants = [row for _, c in parsed.values() for row in c.constants]
    loads = [row for _, c in parsed.values() for row in c.loads]
    tool_names = [row for _, c in parsed.values() for row in c.tool_names]

    attributes: list[dict[str, Any]] = []
    for _, collector in parsed.values():
        for row in collector.attributes:
            # Exact module match avoids treating ``ImportedClass._x`` as a module-private symbol.
            if row["target_candidate"] in modules:
                attributes.append({**row, "target_module": row["target_candidate"]})

    full_graph, full_sites = build_graph(imports, modules, mcp_modules, eager=False)
    eager_graph, eager_sites = build_graph(imports, modules, mcp_modules, eager=True)
    reverse_counts = transitive_dependents(full_graph)
    importers: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in full_graph.items():
        for target in targets:
            importers[target].add(source)

    mcp_imports = [row for row in imports if row["importer"] in mcp_modules]
    packages: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mcp_imports:
        packages[top_package(row["base"] or row["resolved"])].append(row)
    layering = {}
    for name, rows in sorted(packages.items()):
        eager_rows = [row for row in rows if row["scope"] == "module" and not row["type_checking"]]
        layering[name] = {
            "alias_edge_count": len(rows),
            "file_count": len({row["path"] for row in rows}),
            "files": sorted({row["path"] for row in rows}),
            "eager_alias_edge_count": len(eager_rows),
            "eager_file_count": len({row["path"] for row in eager_rows}),
            "edges": sorted(rows, key=lambda row: (row["path"], row["line"], str(row["name"]))),
        }

    definition_sites: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in definitions:
        if row["module_level"]:
            definition_sites[(row["module"], row["name"])].append(f"{row['path']}:{row['line']}")
    for row in constants:
        definition_sites[(row["module"], row["name"])].append(f"{row['path']}:{row['line']}")

    private: list[dict[str, Any]] = []
    for row in imports:
        if not row["private"] or not row["name"]:
            continue
        target = import_target(row, modules)
        if target and target != row["importer"]:
            private.append(
                {
                    "kind": "from_import",
                    "direction": direction(row["importer"], target),
                    "importer": row["importer"],
                    "importer_site": f"{row['path']}:{row['line']}",
                    "target_module": target,
                    "symbol": row["name"],
                    "definition_sites": sorted(set(definition_sites[(target, row["name"])])),
                    "scope": row["scope"],
                    "type_checking": row["type_checking"],
                }
            )
    for row in attributes:
        if row["private"] and row["target_module"] != row["importer"]:
            private.append(
                {
                    "kind": "module_attribute",
                    "direction": direction(row["importer"], row["target_module"]),
                    "importer": row["importer"],
                    "importer_site": f"{row['path']}:{row['line']}",
                    "target_module": row["target_module"],
                    "symbol": row["name"],
                    "definition_sites": sorted(
                        set(definition_sites[(row["target_module"], row["name"])] )
                    ),
                }
            )
    private.sort(key=lambda row: (row["direction"], row["importer_site"], row["symbol"]))

    scoped_defs = [row for row in definitions if row["module"] in mcp_modules]
    module_dupes = duplicate_groups(scoped_defs, module_level=True)
    method_dupes = duplicate_groups(scoped_defs, module_level=False)
    by_body: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scoped_defs:
        if row["module_level"]:
            by_body[row["body_sha256"]].append(row)
    identical_bodies = [
        {"body_sha256": digest, "definitions": sorted(rows, key=lambda row: (row["path"], row["line"]))}
        for digest, rows in sorted(by_body.items())
        if len({row["path"] for row in rows}) > 1
    ]

    by_tool: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in tool_names:
        if row["module"].startswith("menhir.mcp.tools."):
            by_tool[row["tool_name"]].append(row)
    duplicate_tools = [
        {"tool_name": name, "definitions": sorted(rows, key=lambda row: (row["path"], row["line"]))}
        for name, rows in sorted(by_tool.items())
        if len(rows) > 1
    ]

    local_loads: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    imported_reads: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    attribute_reads: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in loads:
        local_loads[(row["module"], row["name"])].append(f"{row['path']}:{row['line']}")
    for row in imports:
        if row["name"] and row["name"] != "*":
            target = import_target(row, modules)
            if target:
                imported_reads[(target, row["name"])].append(f"{row['path']}:{row['line']}")
    for row in attributes:
        attribute_reads[(row["target_module"], row["name"])].append(f"{row['path']}:{row['line']}")
    scoped_constants = []
    for row in constants:
        if row["module"] in mcp_modules:
            reads = sorted(
                set(
                    local_loads[(row["module"], row["name"])]
                    + imported_reads[(row["module"], row["name"])]
                    + attribute_reads[(row["module"], row["name"])]
                )
            )
            scoped_constants.append({**row, "read_sites": reads, "statically_unread": not reads})

    file_rows = [
        {
            "path": path.relative_to(repo).as_posix(),
            "physical_lines": len(parsed[path][0].splitlines()),
            "sha256": hashlib.sha256(parsed[path][0].encode()).hexdigest(),
        }
        for path in mcp_files
    ]
    scope = {
        "file_count": len(file_rows),
        "physical_lines": sum(row["physical_lines"] for row in file_rows),
        "files": file_rows,
    }
    hubs = {
        module: {
            "direct_in_degree": len(importers[module]),
            "direct_importers": sorted(importers[module]),
            "reverse_transitive_dependents": reverse_counts.get(module, 0),
        }
        for module in HUBS
    }
    controls = {
        "scope_file_count_70": scope["file_count"] == EXPECTED_FILES,
        "scope_physical_lines_7222": scope["physical_lines"] == EXPECTED_LINES,
        "no_parse_errors": not parse_errors,
        "BaseTool_definition_visible": bool(definition_sites[("menhir.mcp.contracts", "BaseTool")]),
        "tools_base_to_contracts_edge_visible": "menhir.mcp.contracts" in full_graph.get("menhir.mcp.tools.base", set()),
        "outside_private_control_visible": any(
            row["direction"] == "outside_to_mcp"
            and row["importer"] == "menhir.api.mcp_remote"
            and row["target_module"] == "menhir.mcp.contracts"
            and row["symbol"] == "_tier_allows"
            for row in private
        ),
    }
    return {
        "target": {"expected_files": EXPECTED_FILES, "expected_physical_lines": EXPECTED_LINES},
        "controls": controls,
        "parse_errors": parse_errors,
        "scope": scope,
        "layering": layering,
        "import_graph": {
            "full_cycles": scc(full_graph),
            "eager_cycles": scc(eager_graph),
            "full_edge_sites": full_sites,
            "eager_edge_sites": eager_sites,
            "hubs": hubs,
            "in_degree": {
                module: {
                    "direct_in_degree": len(importers[module]),
                    "direct_importers": sorted(importers[module]),
                    "reverse_transitive_dependents": reverse_counts.get(module, 0),
                }
                for module in sorted(mcp_modules)
            },
        },
        "private_symbol_references": private,
        "cross_boundary_private_references": [
            row for row in private if row["direction"] in {"mcp_to_outside", "outside_to_mcp"}
        ],
        "duplicate_module_definitions": module_dupes,
        "identical_module_bodies": identical_bodies,
        "duplicate_method_definitions": method_dupes,
        "duplicate_tool_names": duplicate_tools,
        "module_constants": scoped_constants,
        "unread_module_constants": [row for row in scoped_constants if row["statically_unread"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    try:
        result = analyze(args.repo)
    except (FileNotFoundError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    scope = result["scope"]
    print("M3 ARCHITECTURE PROBE")
    print(f"scope: {scope['file_count']} files / {scope['physical_lines']} physical lines")
    print("controls:")
    for name, passed in result["controls"].items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    print("layering (all aliases/files; eager aliases/files):")
    for name, row in result["layering"].items():
        print(
            f"  {name}: {row['alias_edge_count']}/{row['file_count']}; "
            f"eager={row['eager_alias_edge_count']}/{row['eager_file_count']}"
        )
    print("hub direct/reverse-transitive in-degree:")
    for name, row in result["import_graph"]["hubs"].items():
        print(f"  {name}: {row['direct_in_degree']}/{row['reverse_transitive_dependents']}")
    print("full cycles:", result["import_graph"]["full_cycles"] or "NONE")
    print("eager cycles:", result["import_graph"]["eager_cycles"] or "NONE")
    print("cross-boundary private references:", len(result["cross_boundary_private_references"]))
    print("duplicate module-level names:", len(result["duplicate_module_definitions"]))
    print("duplicate tool names:", len(result["duplicate_tool_names"]))
    print("unread module constants:", len(result["unread_module_constants"]))
    for row in result["unread_module_constants"]:
        print(f"  {row['path']}:{row['line']} {row['name']}")
    print("--- JSON ---")
    print(json.dumps(result, indent=2, sort_keys=True))

    failed = [name for name, passed in result["controls"].items() if not passed]
    if failed:
        print("ERROR: control checks failed: " + ", ".join(failed), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
