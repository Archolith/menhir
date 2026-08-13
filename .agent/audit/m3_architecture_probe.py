#!/usr/bin/env python3
"""Read-only AST probe for the Menhir M3 MCP architecture audit.

Prints deterministic JSON; it never writes to the checkout.  Architectural
judgements stay in the companion report.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

EXPECTED_FILES = 70
SCOPE = Path("src/menhir/mcp")


def module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root / "src").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_name(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def imports(module: str, path: Path, tree: ast.AST) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rows.append({"target": alias.name, "symbol": None, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package_name(module, path).split(".")
                trim = node.level - 1
                parts = parts[:-trim] if trim else parts
                if node.module:
                    parts += node.module.split(".")
                target = ".".join(parts)
            else:
                target = node.module or ""
            for alias in node.names:
                rows.append({"target": target, "symbol": alias.name, "line": node.lineno})
    return rows


def internal_target(row: dict[str, Any], modules: set[str]) -> str | None:
    candidates = [row["target"]]
    if row["symbol"] and row["symbol"] != "*":
        candidates.append(f'{row["target"]}.{row["symbol"]}')
    return next((c for c in sorted(candidates, key=len, reverse=True) if c in modules), None)


def digest(node: ast.AST) -> str:
    raw = ast.dump(node, include_attributes=False).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def body_digest(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return digest(ast.Module(body=body, type_ignores=[]))


def definitions(path: str, module: str, tree: ast.Module) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(stmts: list[ast.stmt], prefix: str = "") -> None:
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                out.append({
                    "file": path, "module": module, "name": node.name,
                    "qualname": qual, "kind": kind, "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno),
                    "body_hash": body_digest(node), "node_hash": digest(node),
                })
                if isinstance(node, ast.ClassDef):
                    visit(node.body, qual)
    visit(tree.body)
    return out


def class_scalar(node: ast.ClassDef, wanted: str) -> Any:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == wanted for t in stmt.targets):
            try:
                return ast.literal_eval(stmt.value)
            except Exception:
                return ast.unparse(stmt.value)
    return None


def calls(tree: ast.AST) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        cur: ast.AST = node.func
        parts: list[str] = []
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        found[".".join(reversed(parts)) or "<dynamic>"] += 1
    return found


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    result: list[list[str]] = []

    def visit(v: str) -> None:
        nonlocal index
        idx[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph[v]):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            group: list[str] = []
            while True:
                w = stack.pop(); on_stack.remove(w); group.append(w)
                if w == v:
                    break
            if len(group) > 1 or v in graph[v]:
                result.append(sorted(group))

    for v in sorted(graph):
        if v not in idx:
            visit(v)
    return sorted(result, key=lambda x: (-len(x), x))


def find_root(explicit: Path | None) -> Path:
    if explicit:
        root = explicit.resolve()
        if not (root / SCOPE).is_dir():
            raise SystemExit(f"missing {root / SCOPE}")
        return root
    for p in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        if (p / SCOPE).is_dir():
            return p
    raise SystemExit("repository root not found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path)
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    root = find_root(args.root)
    scope = sorted((root / SCOPE).rglob("*.py"))
    all_src = sorted((root / "src/menhir").rglob("*.py"))
    trees: dict[Path, ast.Module] = {}
    modules: dict[Path, str] = {}
    parse_errors: list[dict[str, Any]] = []
    for path in all_src:
        modules[path] = module_name(root, path)
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            parse_errors.append({"file": str(path.relative_to(root)), "line": exc.lineno, "error": str(exc)})

    scope_modules = {modules[p] for p in scope}
    by_module = {modules[p]: p for p in trees}
    graph = {m: set() for m in scope_modules}
    edges: list[dict[str, Any]] = []
    external: defaultdict[str, set[str]] = defaultdict(set)
    private_within: list[dict[str, Any]] = []
    private_in: list[dict[str, Any]] = []
    private_out: list[dict[str, Any]] = []
    outward: list[dict[str, Any]] = []

    for path, tree in trees.items():
        source = modules[path]
        in_scope = path in scope
        for row in imports(source, path, tree):
            target = internal_target(row, scope_modules) if in_scope else None
            record = {"file": str(path.relative_to(root)), "source": source, **row}
            if target:
                graph[source].add(target)
                edges.append({**record, "target": target})
            elif in_scope and row["target"]:
                external[source].add(row["target"])
            if not in_scope and (row["target"] == "menhir.mcp" or row["target"].startswith("menhir.mcp.")):
                outward.append(record)
            if row["symbol"] and row["symbol"].startswith("_"):
                src_mcp = source.startswith("menhir.mcp")
                dst_mcp = row["target"].startswith("menhir.mcp")
                (private_within if src_mcp and dst_mcp else private_in if (not src_mcp and dst_mcp) else private_out if (src_mcp and not dst_mcp) else []).append(record)

    indegree = Counter(e["target"] for e in edges)
    defs = [d for p in scope if p in trees for d in definitions(str(p.relative_to(root)), modules[p], trees[p])]
    by_name: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in defs:
        by_name[(d["kind"], d["name"])].append(d)
    duplicates = []
    for (kind, name), rows in sorted(by_name.items()):
        if len({r["file"] for r in rows}) < 2:
            continue
        hashes: defaultdict[str, list[str]] = defaultdict(list)
        for r in rows:
            hashes[r["body_hash"]].append(f'{r["file"]}:{r["line"]}-{r["end"]} {r["qualname"]}')
        duplicates.append({"kind": kind, "name": name, "locations": rows,
                           "identical_body_groups": {h: locs for h, locs in hashes.items() if len(locs) > 1}})

    constant_defs: list[dict[str, Any]] = []
    for p in scope:
        if p not in trees:
            continue
        tree = trees[p]
        loads = Counter(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load))
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constant_defs.append({"file": str(p.relative_to(root)), "module": modules[p],
                                          "name": target.id, "line": node.lineno,
                                          "local_loads": loads[target.id]})
    imported_constants = Counter((r["target"], r["symbol"]) for p, t in trees.items() for r in imports(modules[p], p, t) if r["symbol"])
    for c in constant_defs:
        c["import_sites"] = imported_constants[(c["module"], c["name"])]
        c["unread_candidate"] = c["local_loads"] == 0 and c["import_sites"] == 0

    tools = []
    for p in scope:
        if p not in trees:
            continue
        for node in trees[p].body:
            if isinstance(node, ast.ClassDef) and {ast.unparse(b) for b in node.bases} & {"BaseTool", "BaseTextTool", "BaseJsonTool"}:
                endpoint = next((n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"), None)
                tools.append({"file": str(p.relative_to(root)), "class": node.name,
                              "name": class_scalar(node, "name"),
                              "tier": class_scalar(node, "required_tier") or "agent (inherited)",
                              "class_line": node.lineno,
                              "endpoint_line": endpoint.lineno if endpoint else None})

    fanout = []
    for p in scope:
        if p not in trees:
            continue
        count = calls(trees[p])
        fanout.append({"file": str(p.relative_to(root)), "module": modules[p],
                       "internal_out_degree": len(graph[modules[p]]),
                       "external_import_count": len(external[modules[p]]),
                       "call_count": sum(count.values()), "unique_calls": len(count),
                       "top_calls": count.most_common(12)})

    qpath = root / SCOPE / "tools/recall/query_structure.py"
    qoutline = []
    if qpath in trees:
        for node in trees[qpath].body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                c = calls(node)
                qoutline.append({"name": node.name, "kind": type(node).__name__, "line": node.lineno,
                                 "end": getattr(node, "end_lineno", node.lineno),
                                 "span": getattr(node, "end_lineno", node.lineno) - node.lineno + 1,
                                 "call_count": sum(c.values()), "unique_calls": len(c)})

    file_rows = []
    for p in scope:
        text = p.read_text(encoding="utf-8")
        file_rows.append({"file": str(p.relative_to(root)), "lines": len(text.splitlines()),
                          "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()})

    result = {
        "probe": {"python": sys.version, "root": str(root), "expected_files": EXPECTED_FILES},
        "scope": {"files": len(scope), "matches_expected": len(scope) == EXPECTED_FILES,
                  "lines": sum(r["lines"] for r in file_rows), "parse_errors": parse_errors,
                  "file_rows": file_rows},
        "imports": {"edges": sorted(edges, key=lambda x: (x["source"], x["target"], x["line"])),
                    "cycles": cycles(graph),
                    "hubs": [{"module": m, "file": str(by_module[m].relative_to(root)),
                              "in_degree": indegree[m], "out_degree": len(graph[m])}
                             for m in sorted(scope_modules, key=lambda x: (-indegree[x], -len(graph[x]), x))],
                    "outward_coupling": sorted(outward, key=lambda x: (x["file"], x["line"])),
                    "private_within_mcp": private_within,
                    "private_imported_from_mcp": private_in,
                    "private_imported_by_mcp": private_out},
        "duplicates": duplicates,
        "constants": {"all": constant_defs,
                      "unread_candidates": [c for c in constant_defs if c["unread_candidate"]],
                      "note": "Zero-use is a conservative AST candidate; check reflection, strings and non-Python consumers."},
        "dispatch": sorted(tools, key=lambda x: (str(x["name"]), x["file"])),
        "fanout": sorted(fanout, key=lambda x: (-x["internal_out_degree"], -x["external_import_count"], -x["unique_calls"], x["file"])),
        "query_structure": qoutline,
    }
    json.dump(result, sys.stdout, indent=2 if args.pretty else None, sort_keys=True)
    print()
    return 1 if args.strict and (len(scope) != EXPECTED_FILES or parse_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
