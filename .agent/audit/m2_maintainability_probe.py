#!/usr/bin/env python3
"""Read-only mechanical probe for the Menhir M2 API maintainability audit.

Run from a clean checkout at eebf6d6dd83f15083167bf847b639d24b953fdc9:
    python .agent/audit/m2_maintainability_probe.py all --root .
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import tokenize

PINNED_COMMIT = "eebf6d6dd83f15083167bf847b639d24b953fdc9"
API = Path("src/menhir/api")
FILES = (
    "routes.py", "routes_support.py", "oauth_authorize.py", "auth.py",
    "routes_handlers.py", "oauth_preflight.py", "oauth.py",
    "client_token_store.py", "server_support.py", "oauth_as_register.py",
    "oauth_rate_limit.py", "mcp_remote.py", "jose_provider.py",
    "oauth_token.py", "auth_code_store.py", "server.py", "oauth_keys.py",
    "oauth_metadata.py", "request_context.py", "oauth_client_store.py",
    "oauth_as_metadata.py", "errors.py", "auth_mode.py", "__init__.py",
)
OAUTH = (
    "oauth.py", "oauth_as_metadata.py", "oauth_as_register.py",
    "oauth_authorize.py", "oauth_client_store.py", "oauth_keys.py",
    "oauth_metadata.py", "oauth_preflight.py", "oauth_rate_limit.py",
    "oauth_token.py",
)
CONST = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class Def:
    path: Path
    qualname: str
    name: str
    line: int
    end: int
    body_hash: str


def paths(root: Path) -> list[Path]:
    return [root / API / name for name in FILES]


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def pyfiles(root: Path) -> list[Path]:
    excluded = {".git", ".venv", "venv", "site-packages", "node_modules"}
    return sorted(
        p for p in root.rglob("*.py")
        if not any(part in excluded for part in p.parts)
    )


def defs(path: Path) -> list[Def]:
    out: list[Def] = []

    def walk(body: list[ast.stmt], prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join((*prefix, node.name))
                dump = ast.dump(
                    ast.Module(body=node.body, type_ignores=[]),
                    include_attributes=False,
                )
                out.append(Def(
                    path, qual, node.name, node.lineno,
                    node.end_lineno or node.lineno,
                    hashlib.sha256(dump.encode()).hexdigest()[:16],
                ))
                walk(node.body, (*prefix, node.name, "<locals>"))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, (*prefix, node.name))

    walk(tree(path).body, ())
    return out


def name_counts(path: Path) -> Counter[str]:
    out: Counter[str] = Counter()
    with path.open("rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.NAME:
                    out[tok.string] += 1
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
    return out


def constants(path: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in tree(path).body:
        targets: list[ast.Name] = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if CONST.match(target.id):
                out.append((target.id, target.lineno))
    return out


def module(path: Path, root: Path) -> str | None:
    try:
        parts = list(path.relative_to(root / "src").with_suffix("").parts)
    except ValueError:
        return None
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve_from(current: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = current.split(".")[:-1]
    base = package[:max(0, len(package) - node.level + 1)]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def scc(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    active: set[str] = set()
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    result: list[list[str]] = []

    def visit(v: str) -> None:
        nonlocal index
        idx[v] = low[v] = index
        index += 1
        stack.append(v)
        active.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in active:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                active.remove(w)
                component.append(w)
                if w == v:
                    break
            result.append(sorted(component))

    for v in sorted(graph):
        if v not in idx:
            visit(v)
    return result


def coverage(root: Path) -> None:
    print("== COVERAGE ==")
    total = 0
    for path in paths(root):
        count = len(path.read_text(encoding="utf-8").splitlines())
        total += count
        print(f"{rel(path, root)}\t{count}")
    print(f"FILES\t{len(FILES)}")
    print(f"TOTAL_LINES\t{total}")


def duplicates(root: Path) -> None:
    print("== DUPLICATE DEFINITIONS; BODIES COMPARED ==")
    groups: dict[tuple[str, str], list[Def]] = defaultdict(list)
    for path in paths(root):
        for item in defs(path):
            groups[(rel(path, root), item.qualname)].append(item)
    found = 0
    for (path, qual), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        found += 1
        kind = "SAME_BODY" if len({x.body_hash for x in items}) == 1 else "DIFFERENT_BODIES"
        print(f"{path}:{qual}\t{kind}\tcount={len(items)}")
        for item in items:
            print(f"  {item.line}-{item.end}\tbody_sha256={item.body_hash}")
    print(f"DUPLICATE_DEFINITION_GROUPS\t{found}")


def constant_sweep(root: Path) -> None:
    print("== MODULE CONSTANT CONSUMPTION ==")
    totals: Counter[str] = Counter()
    for path in pyfiles(root):
        totals.update(name_counts(path))
    rows: list[tuple[Path, str, int]] = []
    definition_count: Counter[str] = Counter()
    for path in paths(root):
        for name, line in constants(path):
            rows.append((path, name, line))
            definition_count[name] += 1
    unread = 0
    for path, name, line in sorted(rows):
        uses = totals[name] - definition_count[name]
        status = "UNREAD" if uses <= 0 else "READ"
        unread += status == "UNREAD"
        print(f"{rel(path, root)}:{line}\t{name}\t{status}\tuses={max(0, uses)}")
    print(f"CONSTANT_DEFINITIONS\t{len(rows)}")
    print(f"UNREAD_CONSTANTS\t{unread}")


def clone_sweep(root: Path) -> None:
    print("== OAUTH CLONES ==")
    oauth_paths = [root / API / name for name in OAUTH]
    body_groups: dict[str, list[Def]] = defaultdict(list)
    for path in oauth_paths:
        for item in defs(path):
            body_groups[item.body_hash].append(item)
    exact = 0
    for digest, items in sorted(body_groups.items()):
        if len({x.path for x in items}) < 2:
            continue
        exact += 1
        print(f"EXACT_FUNCTION_BODY\tsha256={digest}\tcount={len(items)}")
        for item in items:
            print(f"  {rel(item.path, root)}:{item.line}-{item.end}\t{item.qualname}")

    windows: dict[str, list[tuple[Path, int, int]]] = defaultdict(list)
    for path in oauth_paths:
        clean = [
            (n, re.sub(r"\s+", " ", line.strip()))
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for i in range(max(0, len(clean) - 3)):
            chunk = "\n".join(text for _, text in clean[i:i + 4])
            digest = hashlib.sha256(chunk.encode()).hexdigest()[:16]
            windows[digest].append((path, clean[i][0], clean[i + 3][0]))
    window_groups = 0
    for digest, items in sorted(windows.items()):
        if len({x[0] for x in items}) < 2:
            continue
        window_groups += 1
        print(f"EXACT_4_LINE_WINDOW\tsha256={digest}\tcount={len(items)}")
        for path, start, end in items:
            print(f"  {rel(path, root)}:{start}-{end}")
    print(f"EXACT_CROSS_FILE_FUNCTION_GROUPS\t{exact}")
    print(f"EXACT_CROSS_FILE_4_LINE_WINDOW_GROUPS\t{window_groups}")


def imports(root: Path) -> None:
    print("== IMPORT GRAPH / PRIVATE IMPORTS ==")
    scope = {module(path, root): path for path in paths(root)}
    scope = {name: path for name, path in scope.items() if name}
    graph = {name: set() for name in scope}
    private: list[tuple[str, int, str, str]] = []
    for current, path in sorted(scope.items()):
        for node in ast.walk(tree(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in scope:
                        graph[current].add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = resolve_from(current, node)
                if target in scope:
                    graph[current].add(target)
                for alias in node.names:
                    if alias.name.startswith("_"):
                        private.append((rel(path, root), node.lineno, target, alias.name))
    for source in sorted(graph):
        for target in sorted(graph[source]):
            print(f"EDGE\t{source}\t{target}")
    for path, line, target, name in sorted(private):
        print(f"PRIVATE_IMPORT\t{path}:{line}\tfrom {target} import {name}")
    cycles = [c for c in scc(graph) if len(c) > 1]
    for cycle in cycles:
        print("CYCLE\t" + " -> ".join(cycle))
    print(f"PRIVATE_IMPORTS\t{len(private)}")
    print(f"IMPORT_CYCLES\t{len(cycles)}")


def dead_code(root: Path) -> None:
    print("== LEXICAL REFERENCE CANDIDATES; NOT AUTOMATIC DEAD CODE ==")
    totals: Counter[str] = Counter()
    for path in pyfiles(root):
        totals.update(name_counts(path))
    count = 0
    for path in paths(root):
        for item in defs(path):
            if totals[item.name] <= 1:
                count += 1
                print(f"NO_LEXICAL_REFERENCE\t{rel(path, root)}:{item.line}-{item.end}\t{item.qualname}")
    print(f"NO_LEXICAL_REFERENCE_CANDIDATES\t{count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="all",
                        choices=("all", "coverage", "duplicates", "constants", "clones", "imports", "dead-code"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    missing = [p for p in paths(root) if not p.is_file()]
    print(f"ROOT\t{root}")
    print(f"PINNED_COMMIT_EXPECTED\t{PINNED_COMMIT}")
    if missing:
        for path in missing:
            print(f"MISSING\t{path}")
        return 2
    commands = {
        "coverage": coverage,
        "duplicates": duplicates,
        "constants": constant_sweep,
        "clones": clone_sweep,
        "imports": imports,
        "dead-code": dead_code,
    }
    selected = commands.values() if args.command == "all" else (commands[args.command],)
    for index, command in enumerate(selected):
        if index:
            print()
        command(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
