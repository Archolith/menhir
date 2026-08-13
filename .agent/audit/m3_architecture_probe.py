#!/usr/bin/env python3
"""Static architecture probe for Menhir M3 (src/menhir/mcp).

Read-only, standard-library-only. It does not import Menhir.
Run: python .agent/audit/m3_architecture_probe.py --repo .
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

MCP = "menhir.mcp"
HUBS = (
    "menhir.mcp.contracts",
    "menhir.mcp.service_access",
    "menhir.mcp.formatters",
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
        parts += target.split(".")
    return ".".join(parts)


def edge_rows(src: Path, repo: Path, path: Path, tree: ast.AST) -> list[dict]:
    importer = modname(src, path)
    rows: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rows.append({
                    "importer": importer,
                    "path": path.relative_to(repo).as_posix(),
                    "line": node.lineno,
                    "base": alias.name,
                    "name": None,
                    "resolved": alias.name,
                    "private": any(p.startswith("_") for p in alias.name.split(".")),
                })
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(importer, path, node.level, node.module)
            for alias in node.names:
                rows.append({
                    "importer": importer,
                    "path": path.relative_to(repo).as_posix(),
                    "line": node.lineno,
                    "base": base,
                    "name": alias.name,
                    "resolved": f"{base}.{alias.name}" if base and alias.name != "*" else base,
                    "private": alias.name.startswith("_"),
                })
    return rows


def target_module(row: dict, modules: set[str]) -> str | None:
    for candidate in (row["resolved"], row["base"]):
        while candidate:
            if candidate in modules:
                return candidate
            candidate = candidate.rpartition(".")[0]
    return None


def scc(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on: set[str] = set()
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    out: list[list[str]] = []

    def visit(v: str) -> None:
        nonlocal index
        idx[v] = low[v] = index
        index += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            group: list[str] = []
            while True:
                w = stack.pop()
                on.remove(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                out.append(sorted(group))

    for v in sorted(graph):
        if v not in idx:
            visit(v)
    return sorted(out, key=lambda x: (-len(x), x))


def defs(path: Path, repo: Path, module: str, tree: ast.Module) -> list[dict]:
    out: list[dict] = []

    def walk(body: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                normalized = ast.dump(
                    ast.Module(body=node.body, type_ignores=[]),
                    include_attributes=False,
                )
                out.append({
                    "name": node.name,
                    "qualname": ".".join((*parents, node.name)),
                    "module": module,
                    "path": path.relative_to(repo).as_posix(),
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "kind": type(node).__name__,
                    "body_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
                })
                walk(node.body, (*parents, node.name))
    walk(tree.body)
    return out


def constants(path: Path, repo: Path, module: str, tree: ast.Module) -> list[dict]:
    out: list[dict] = []
    for node in tree.body:
        targets: list[ast.Name] = []
        if isinstance(node, ast.Assign):
            targets = [x for x in node.targets if isinstance(x, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if re.fullmatch(r"_?[A-Z][A-Z0-9_]*", target.id):
                out.append({
                    "name": target.id,
                    "module": module,
                    "path": path.relative_to(repo).as_posix(),
                    "line": node.lineno,
                })
    return out


def top_package(name: str) -> str:
    parts = name.split(".")
    return ".".join(parts[:2]) if parts and parts[0] == "menhir" and len(parts) > 1 else parts[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()
    repo = args.repo.resolve()
    src = repo / "src"
    root = src / "menhir" / "mcp"
    if not root.is_dir():
        print(f"ERROR: missing {root}", file=sys.stderr)
        return 2

    all_files = sorted((src / "menhir").rglob("*.py"))
    mcp_files = sorted(root.rglob("*.py"))
    parsed: dict[Path, tuple[str, ast.Module]] = {}
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        parsed[path] = (text, ast.parse(text, filename=str(path)))

    modules = {modname(src, p) for p in all_files}
    mcp_modules = {modname(src, p) for p in mcp_files}
    edges: list[dict] = []
    definitions: list[dict] = []
    consts: list[dict] = []
    loads: defaultdict[str, list[str]] = defaultdict(list)
    for path, (_, tree) in parsed.items():
        module = modname(src, path)
        edges += edge_rows(src, repo, path, tree)
        definitions += defs(path, repo, module, tree)
        consts += constants(path, repo, module, tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loads[node.id].append(f"{path.relative_to(repo).as_posix()}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                loads[node.attr].append(f"{path.relative_to(repo).as_posix()}:{node.lineno}")

    mcp_edges = [e for e in edges if e["importer"] in mcp_modules]
    graph = {m: set() for m in mcp_modules}
    sites: defaultdict[str, list[str]] = defaultdict(list)
    for row in mcp_edges:
        target = target_module(row, modules)
        if target in mcp_modules and target != row["importer"]:
            graph[row["importer"]].add(target)
            sites[f"{row['importer']} -> {target}"].append(f"{row['path']}:{row['line']}")

    importers: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            importers[target].add(source)

    layer_edges: defaultdict[str, list[dict]] = defaultdict(list)
    for row in mcp_edges:
        layer_edges[top_package(row["base"] or row["resolved"])].append(row)

    boundary_private = []
    for row in edges:
        inside_from = row["importer"] == MCP or row["importer"].startswith(MCP + ".")
        inside_to = row["base"] == MCP or row["base"].startswith(MCP + ".")
        if row["private"] and inside_from != inside_to and (row["base"].startswith("menhir") or row["resolved"].startswith("menhir")):
            boundary_private.append(row)

    by_name: defaultdict[str, list[dict]] = defaultdict(list)
    for row in definitions:
        if row["module"] in mcp_modules:
            by_name[row["name"]].append(row)
    duplicates = []
    for name, rows in sorted(by_name.items()):
        if len({r["path"] for r in rows}) > 1:
            duplicates.append({
                "name": name,
                "distinct_bodies": len({r["body_sha256"] for r in rows}),
                "definitions": rows,
            })

    mcp_consts = []
    for row in consts:
        if row["module"] in mcp_modules:
            row = dict(row)
            row["read_sites"] = sorted(set(loads.get(row["name"], [])))
            mcp_consts.append(row)

    file_rows = []
    for path in mcp_files:
        text = parsed[path][0]
        file_rows.append({
            "path": path.relative_to(repo).as_posix(),
            "physical_lines": len(text.splitlines()),
            "newline_count": text.count("\n"),
            "terminal_newline": text.endswith("\n"),
        })

    result = {
        "scope": {
            "file_count": len(mcp_files),
            "physical_lines": sum(x["physical_lines"] for x in file_rows),
            "newline_count": sum(x["newline_count"] for x in file_rows),
            "files": file_rows,
        },
        "layering": {
            k: {"edge_count": len(v), "file_count": len({x["path"] for x in v}), "edges": v}
            for k, v in sorted(layer_edges.items())
        },
        "import_graph": {
            "adjacency": {k: sorted(v) for k, v in sorted(graph.items())},
            "cycles": scc(graph),
            "edge_sites": dict(sorted(sites.items())),
            "indegree": {m: {"count": len(importers[m]), "importers": sorted(importers[m])} for m in sorted(mcp_modules)},
            "hubs": {m: {"count": len(importers[m]), "importers": sorted(importers[m])} for m in HUBS},
        },
        "cross_boundary_private_imports": boundary_private,
        "duplicate_definitions": duplicates,
        "unread_module_constants": [x for x in mcp_consts if not x["read_sites"]],
        "module_constants": mcp_consts,
    }

    print("M3 ARCHITECTURE PROBE")
    print(f"scope: {result['scope']['file_count']} files / {result['scope']['physical_lines']} physical lines")
    print("layering:")
    for name, row in result["layering"].items():
        print(f"  {name}: {row['edge_count']} edges from {row['file_count']} files")
    print("hub in-degree:")
    for name, row in result["import_graph"]["hubs"].items():
        print(f"  {name}: {row['count']}")
    print("cycles:", result["import_graph"]["cycles"] or "NONE")
    print("cross-boundary private imports:", len(boundary_private))
    print("duplicate names across files:", len(duplicates))
    for row in duplicates:
        label = "IDENTICAL" if row["distinct_bodies"] == 1 else "DIVERGENT"
        print(f"  {label} {row['name']} ({row['distinct_bodies']} bodies)")
    print("unread module constants:", len(result["unread_module_constants"]))
    for row in result["unread_module_constants"]:
        print(f"  {row['path']}:{row['line']} {row['name']}")
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print("--- JSON ---")
    print(encoded)
    if args.json_out:
        out = args.json_out if args.json_out.is_absolute() else repo / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
