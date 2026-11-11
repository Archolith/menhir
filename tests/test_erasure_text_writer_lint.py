"""T14: every writer of an inventoried Neo4j text property is registered and gated (erasure R1.4).

An AST + Cypher scan over ``src/menhir`` and ``scripts/`` finds every function that SETs, MERGEs or
CREATEs a (label, property) the graph text inventory calls text
(``menhir.infrastructure.graph_text_inventory``). Each such function must have an entry in
``menhir.services.erasure_fence_registry.COPIERS``. The registry carries the classification (gate or
exempt-with-reason or UNFENCED-with-a-tag); this scan only supplies the census and gate EVIDENCE, so a
registry claim cannot be made up. UNFENCED entries are a ratchet: the count may only shrink.

Run ``python -m tests.test_erasure_text_writer_lint`` to print the census as markdown.

Known blind spots (the scanner is static): Cypher assembled from runtime data (``str.join`` over
variables, dict-driven clause lists), queries read from files, and writers reached only through
``getattr``. Writers whose variable cannot be bound to a label are reported with label ``?``.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import pytest

from menhir.infrastructure.graph_text_inventory import (
    inventoried_labels,
    inventoried_pairs,
    is_text_property,
)

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (REPO / "src" / "menhir", REPO / "scripts")

#: Recorded ceiling for UNFENCED registry entries (ratchet: lower it when R2 closes entries).
UNFENCED_ALLOWLIST_MAX = 11  # seeded at 15 by the R1 census; R2 closed CF-332, CF-327, CF-338

_PH = "__X__"


class Site(NamedTuple):
    path: str  # relative to the repo
    function: str  # qualname ("Class.method"), or "<module>"
    label: str  # inventoried label / rel type, or "?" when the variable could not be bound
    prop: str  # property name, or "*" for a whole-map write
    line: int
    gate_evidence: tuple[str, ...]

    @property
    def module(self) -> str:
        return _module_of_path(self.path)


def _module_of_path(path: str) -> str:
    parts = path.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --------------------------------------------------------------------------- module index
class _Mod:
    def __init__(self, name: str, path: Path, rel: str, tree: ast.Module, source: str) -> None:
        self.name = name
        self.path = path
        self.rel = rel
        self.tree = tree
        self.lines = source.splitlines()
        self.consts: dict[str, ast.expr] = {}
        self.class_consts: dict[tuple[str, str], ast.expr] = {}
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.docstrings: set[int] = set()


def _iter_py(root: Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _build_index(roots) -> dict[str, _Mod]:
    index: dict[str, _Mod] = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for p in _iter_py(root):
            try:
                source = p.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue
            relp = p.relative_to(root.parent).as_posix()
            try:
                rel = p.resolve().relative_to(REPO).as_posix()
            except ValueError:
                rel = relp
            name = _module_of_path(relp)
            mod = _Mod(name, p, rel, tree, source)
            _index_module(mod)
            index[name] = mod
    return index


def _index_module(mod: _Mod) -> None:
    pkg = mod.name.split(".")
    if not mod.path.name == "__init__.py":
        pkg = pkg[:-1]
    for node in ast.walk(mod.tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                mod.docstrings.add(id(body[0].value))
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else list(pkg)
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            for alias in node.names:
                mod.imports[alias.asname or alias.name] = (target, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    mod.imports[alias.asname] = (alias.name, None)

    def collect(body, cls_prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                collect(stmt.body, cls_prefix + stmt.name + ".")
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = stmt.value
                if value is None:
                    continue
                for t in targets:
                    if isinstance(t, ast.Name):
                        if cls_prefix:
                            mod.class_consts[(cls_prefix[:-1], t.id)] = value
                        else:
                            mod.consts[t.id] = value
            elif isinstance(stmt, (ast.If, ast.Try, ast.With)) and not cls_prefix:
                for field in ("body", "orelse", "finalbody"):
                    collect(getattr(stmt, field, []) or [], cls_prefix)
                for h in getattr(stmt, "handlers", []) or []:
                    collect(h.body, cls_prefix)

    collect(mod.tree.body, "")


# --------------------------------------------------------------------------- string evaluation
_HELPER = re.compile(r"(cypher$|^marker_|^fence_|^_?fence)")
_BUILDER_PREFIX = {
    "match": "MATCH",
    "optional_match": "OPTIONAL MATCH",
    "merge": "MERGE",
    "create": "CREATE",
    "set": "SET",
    "on_create_set": "ON CREATE SET",
    "on_match_set": "ON MATCH SET",
    "where": "WHERE",
    "with_clause": "WITH",
    "delete": "DELETE",
    "detach_delete": "DETACH DELETE",
    "return_fields": "RETURN",
    "return_raw": "RETURN",
    "unwind": "UNWIND",
    "raw": "",
}


class _Resolver:
    def __init__(self, index: dict[str, _Mod]) -> None:
        self.index = index
        self.used: set[tuple[str, str]] = set()
        self._memo: dict[tuple[str, str], str | None] = {}
        self._active: set[tuple[str, str]] = set()
        self.bind: dict[str, str] = {}

    def const(self, mod: _Mod, name: str, cls: str = "") -> str | None:
        node = None
        key_mod = mod
        if cls and (cls, name) in mod.class_consts:
            node = mod.class_consts[(cls, name)]
            key = (mod.name, f"{cls}.{name}")
        elif name in mod.consts:
            node = mod.consts[name]
            key = (mod.name, name)
        elif name in mod.imports:
            target, orig = mod.imports[name]
            other = self.index.get(target)
            if other is not None and orig and orig in other.consts:
                return self.const(other, orig)
            return None
        else:
            return None
        self.used.add(key)
        if key in self._memo:
            return self._memo[key]
        if key in self._active:
            return None
        self._active.add(key)
        try:
            value = self.eval(node, key_mod, cls)
        finally:
            self._active.discard(key)
        self._memo[key] = value
        return value

    def eval(self, node: ast.AST, mod: _Mod, cls: str = "") -> str | None:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    parts.append(self._interp(v.value, mod, cls))
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            left = self.eval(node.left, mod, cls)
            right = self.eval(node.right, mod, cls) if isinstance(node.op, ast.Add) else ""
            if left is None and right is None:
                return None
            if isinstance(node.op, ast.Mod):
                return left
            return (left if left is not None else _PH) + (right if right is not None else _PH)
        if isinstance(node, ast.Name):
            return self.const(mod, node.id, cls)
        if isinstance(node, ast.Attribute):
            return self._attribute(node, mod, cls)
        if isinstance(node, (ast.Tuple, ast.List)):
            vals = [self.eval(e, mod, cls) for e in node.elts]
            vals = [v for v in vals if v is not None]
            return ", ".join(vals) if vals else None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr == "join":
                    sep = self.eval(func.value, mod, cls)
                    if sep is not None and node.args and isinstance(
                        node.args[0], (ast.GeneratorExp, ast.ListComp)
                    ):
                        comp = node.args[0]
                        gen = comp.generators[0]
                        if isinstance(gen.target, ast.Name):
                            parts = []
                            for elt in self._elements(gen.iter, mod):
                                self.bind[gen.target.id] = elt
                                got = self.eval(comp.elt, mod, cls)
                                parts.append(got if got is not None else _PH)
                            self.bind.pop(gen.target.id, None)
                            if parts:
                                return sep.join(parts)
                        return None
                    if sep is not None and node.args:
                        items = self.eval(node.args[0], mod, cls)
                        if items is not None:
                            return items.replace(", ", sep) if sep != ", " else items
                    return None
                if func.attr in ("format", "strip", "lstrip", "rstrip", "replace", "lower", "upper"):
                    return self.eval(func.value, mod, cls)
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name and _HELPER.search(name):
                return f"__CALL_{name}__"
            return None
        if isinstance(node, ast.IfExp):
            a = self.eval(node.body, mod, cls)
            b = self.eval(node.orelse, mod, cls)
            if a is None and b is None:
                return None
            return (a if a is not None else _PH) + " " + (b if b is not None else _PH)
        return None

    def _attribute(self, node: ast.Attribute, mod: _Mod, cls: str) -> str | None:
        base = node.value
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls") and cls:
                return self.const(mod, node.attr, cls)
            if base.id in mod.imports:
                target, orig = mod.imports[base.id]
                dotted = f"{target}.{orig}" if orig else target
                for cand in (dotted, target):
                    other = self.index.get(cand)
                    if other is not None and node.attr in other.consts:
                        return self.const(other, node.attr)
            if (base.id, node.attr) in mod.class_consts:
                return self.const(mod, node.attr, base.id)
        return None

    def _elements(self, node: ast.AST, mod: _Mod) -> list[str]:
        if isinstance(node, ast.Name) and node.id in mod.imports and node.id not in mod.consts:
            target, orig = mod.imports[node.id]
            other = self.index.get(target)
            if other is not None and orig in other.consts:
                return self._elements(other.consts[orig], other)
        if isinstance(node, ast.Name) and node.id in mod.consts:
            return self._elements(mod.consts[node.id], mod)
        if isinstance(node, (ast.Tuple, ast.List)):
            return [str(e.value) for e in node.elts if isinstance(e, ast.Constant)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._elements(node.left, mod) + self._elements(node.right, mod)
        return []

    def _interp(self, value: ast.AST, mod: _Mod, cls: str) -> str:
        if isinstance(value, ast.Name) and value.id in self.bind:
            return self.bind[value.id]
        got = self.eval(value, mod, cls)
        return got if got is not None else _PH


# --------------------------------------------------------------------------- cypher analysis
_KW = re.compile(
    r"(?<![\w.$:])(ON\s+CREATE\s+SET|ON\s+MATCH\s+SET|OPTIONAL\s+MATCH|DETACH\s+DELETE|ORDER\s+BY|"
    r"SET|WHERE|MATCH|MERGE|CREATE|WITH|RETURN|REMOVE|DELETE|UNWIND|FOREACH|CALL|LIMIT|SKIP|UNION|"
    r"USING|YIELD)(?!\w)"
)
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_BIND = re.compile(r"[(\[]\s*(\w+)\s*((?::\s*[`\w]+)+)")
_BIND_DYN = re.compile(r"[(\[]\s*(\w+)\s*:\s*\$\(")
_WHERE_LABEL = re.compile(r"(?<![\w.$:])(\w+)\s*:\s*([A-Z]\w*)\b")
_NODE_MAP = re.compile(r"[(\[]\s*(\w*)\s*((?::\s*[`\w]+)*)\s*\{")
_ITEM_PROP = re.compile(r"^\s*(\w+)\s*\.\s*(\w+)\s*(\+=|=)(?!=)\s*(.*)$", re.S)
_ITEM_MAP = re.compile(r"^\s*(\w+)\s*(\+=|=)(?!=)\s*(.*)$", re.S)
_ITEM_DYN = re.compile(r"^\s*(\w+)\s*\[[^\]]*\]\s*(\+=|=)(?!=)\s*(.*)$", re.S)


_LINE_COMMENT = re.compile(r"(?m)(^|\s)//[^\n]*")


def _blank_strings(text: str) -> str:
    text = _LINE_COMMENT.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), text)
    return _QUOTED.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], text)


def _depths(text: str) -> list[int]:
    out: list[int] = []
    d = 0
    for ch in text:
        out.append(d)
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
    out.append(d)
    return out


def _labels(spec: str) -> set[str]:
    return {x.strip().strip("`") for x in spec.split(":") if x.strip().strip("`")}


def _norm_label(label: str) -> str:
    return "?" if label.startswith("__") or label.startswith("$") else label


def _regions(text: str, depth: list[int]):
    """``(keyword, start, end, depth)`` for every clause token; ``end`` closes the clause."""
    toks = [(m.start(), m.end(), re.sub(r"\s+", " ", m.group(1))) for m in _KW.finditer(text)]
    out = []
    for i, (s, e, kw) in enumerate(toks):
        d = depth[s]
        end = len(text)
        for s2, _e2, _k2 in toks[i + 1:]:
            if depth[s2] <= d:
                end = s2
                break
        for pos in range(e, end):
            if depth[pos + 1] < d:
                end = pos
                break
        out.append((kw, e, end, d))
    return out


def _bindings(text: str) -> dict[str, set[str]]:
    blank = _blank_strings(text)
    depth = _depths(blank)
    out: dict[str, set[str]] = defaultdict(set)
    for m in _BIND.finditer(blank):
        for lab in _labels(m.group(2)):
            out[m.group(1)].add(_norm_label(lab))
    for m in _BIND_DYN.finditer(blank):
        out[m.group(1)].add("?")
    for kw, s, e, _d in _regions(blank, depth):
        if kw == "WHERE":
            for m in _WHERE_LABEL.finditer(blank[s:e]):
                out[m.group(1)].add(_norm_label(m.group(2)))
    return out


def _split_top(text: str, base_depth: int, offset_depth: list[int], start: int) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    last = 0
    for i, ch in enumerate(text):
        if ch == "," and offset_depth[start + i] == base_depth:
            items.append((last, text[last:i]))
            last = i + 1
    items.append((last, text[last:]))
    return items


def _writes(text: str) -> list[tuple[str, str, str, int]]:
    """``(var, label_or_empty, prop, char_offset)`` for every text-capable write in ``text``."""
    blank = _blank_strings(text)
    depth = _depths(blank)
    out: list[tuple[str, str, str, int]] = []
    for kw, s, e, d in _regions(blank, depth):
        seg = blank[s:e]
        if "SET" in kw:
            for off, item in _split_top(seg, d, depth, s):
                m = _ITEM_PROP.match(item)
                if m:
                    var, prop, _op, rhs = m.groups()
                    if rhs.strip().lower() == "null":
                        continue
                    out.append((var, "", prop, s + off))
                    continue
                m = _ITEM_DYN.match(item)
                if m:
                    out.append((m.group(1), "", "*", s + off))
                    continue
                m = _ITEM_MAP.match(item)
                if m and "." not in m.group(1) and (
                    m.group(2) == "+=" or m.group(3).lstrip().startswith(("$", "{", "properties("))
                ):
                    out.append((m.group(1), "", "*", s + off))
        elif kw in ("MERGE", "CREATE"):
            for m in _NODE_MAP.finditer(seg):
                brace = s + m.end() - 1
                body_start = brace + 1
                close = body_start
                dd = depth[brace + 1]
                while close < len(blank) and not (blank[close] == "}" and depth[close] == dd):
                    close += 1
                body = blank[body_start:close]
                labels = _labels(m.group(2))
                for off, item in _split_top(body, dd, depth, body_start):
                    km = re.match(r"^\s*(\w+)\s*:", item)
                    if km:
                        if labels:
                            for lab in labels:
                                out.append((m.group(1), _norm_label(lab), km.group(1), body_start + off))
                        else:
                            out.append((m.group(1), "", km.group(1), body_start + off))
    return out


def _is_text(label: str, prop: str) -> bool:
    if label == "?":
        # Unbound variable: Entity is open-ended, so only names the inventory lists explicitly
        # count (otherwise every unbound ``r.weight`` would be reported).
        return prop == "*" or prop in {p for _l, p in inventoried_pairs()}
    if prop == "*":
        return label in inventoried_labels()
    return is_text_property(label, prop)


# --------------------------------------------------------------------------- scope scan
class _Scope(NamedTuple):
    qualname: str
    cls: str
    node: ast.AST | None  # None for the module-level scope
    stmts: list[ast.stmt]


def _scopes(mod: _Mod) -> list[_Scope]:
    scopes: list[_Scope] = []
    module_stmts: list[ast.stmt] = []

    def walk(body, prefix: str, top: bool) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append(_Scope(prefix + stmt.name, prefix[:-1], stmt, [stmt]))
            elif isinstance(stmt, ast.ClassDef):
                walk(stmt.body, prefix + stmt.name + ".", False)
            elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(stmt, field, []) or [], prefix, top)
                for h in getattr(stmt, "handlers", []) or []:
                    walk(h.body, prefix, top)
            else:
                module_stmts.append(stmt)

    walk(mod.tree.body, "", True)
    scopes.append(_Scope("<module>", "", None, module_stmts))
    return scopes


def _units(stmts: list[ast.stmt], mod: _Mod, res: _Resolver, cls: str) -> list[tuple[str, int]]:
    units: list[tuple[str, int]] = []
    # Local list variables later passed to a builder (``set_lines = [...]; set_lines.insert(0, ...)``).
    local_lists: dict[str, list[str]] = defaultdict(list)
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                got = res.eval(node.value, mod, cls)
                if got is not None:
                    local_lists[node.targets[0].id].append(got)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                got = res.eval(node.value, mod, cls)
                if got is not None:
                    local_lists[node.target.id].append(got)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.attr in ("append", "insert", "extend") and node.args:
                got = res.eval(node.args[-1], mod, cls)
                if got is not None:
                    local_lists[node.func.value.id].append(got)

    def emit(text: str | None, line: int) -> None:
        if text and text.strip():
            units.append((text, line))

    def visit(node: ast.AST) -> None:
        if id(node) in mod.docstrings:
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _BUILDER_PREFIX:
            attr = node.func.attr
            args = node.args[1:2] if attr == "where_if" else node.args
            vals = []
            for a in args:
                v = res.eval(a, mod, cls)
                if v is None and isinstance(a, ast.Name) and a.id in local_lists:
                    v = ", ".join(local_lists[a.id])
                if v is not None:
                    vals.append(v)
            if vals:
                joiner = " AND " if attr in ("where", "where_if") else ", "
                emit(f"{_BUILDER_PREFIX[attr]} {joiner.join(vals)}", node.lineno)
                visit(node.func.value)
                return
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                return
            got = res.eval(node, mod, cls)
            if got is not None:
                emit(got, getattr(node, "lineno", 0))
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in stmts:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if all(isinstance(t, ast.Name) for t in targets) and stmt.value is not None \
                    and not cls and any(t.id in mod.consts for t in targets):  # type: ignore[attr-defined]
                continue  # module constant: reported through its users or the residual pass
        visit(stmt)
    return units


def _evidence(source: str, text: str) -> tuple[str, ...]:
    both = source + "\n" + text
    ev = []
    if "execute_copy_fenced" in source:
        ev.append("copy_fenced")
    if "run_fenced" in source:
        ev.append("run_fenced")
    if "check_fence" in source or "_fence_check" in source:
        ev.append("check_fence")
    if "MenhirPublishFence" in source or "after_write_extras" in source or "with_extras" in source:
        ev.append("publish_extra")
    if any(t in both for t in ("ErasureMarker", "marker_absent", "EPISODE_NOT_MARKED")):
        ev.append("g_q")
    if ("fence_lock_cypher" in both or "EvidenceNamespaceFence" in both) and "created_epoch" in both:
        ev.append("g_create")
    return tuple(ev)


def _scan_unit_set(
    mod: _Mod, qualname: str, units: list[tuple[str, int]], source: str
) -> list[Site]:
    if not units:
        return []
    func_bind: dict[str, set[str]] = defaultdict(set)
    for text, _line in units:
        for var, labs in _bindings(text).items():
            func_bind[var] |= labs
    all_text = "\n".join(t for t, _ in units)
    evidence = _evidence(source, all_text)
    sites: list[Site] = []
    seen: set[tuple[str, str, int]] = set()
    for text, line in units:
        local = _bindings(text)
        for var, label, prop, off in _writes(text):
            labels = {label} if label else (local.get(var) or func_bind.get(var) or {"?"})
            for lab in sorted(labels):
                if not _is_text(lab, prop):
                    continue
                ln = line + text.count("\n", 0, off)
                key = (lab, prop, ln)
                if key in seen:
                    continue
                seen.add(key)
                sites.append(Site(mod.rel, qualname, lab, prop, ln, evidence))
    return sites


def collect_text_writers(roots=DEFAULT_ROOTS) -> list[Site]:
    """Every (function, label, property) that writes inventoried memory text in ``roots``."""
    index = _build_index(roots)
    res = _Resolver(index)
    sites: list[Site] = []
    for mod in index.values():
        for scope in _scopes(mod):
            if scope.node is None:
                continue
            units = _units(scope.stmts, mod, res, scope.cls)
            node = scope.node
            lo = min([node.lineno] + [d.lineno for d in node.decorator_list])  # type: ignore[attr-defined]
            source = "\n".join(mod.lines[lo - 1: node.end_lineno])  # type: ignore[attr-defined]
            sites.extend(_scan_unit_set(mod, scope.qualname, units, source))
    # Module level: statements outside any function, plus constants no function resolved.
    for mod in index.values():
        stmts = next(s.stmts for s in _scopes(mod) if s.node is None)
        units = _units(stmts, mod, res, "")
        for name, node in mod.consts.items():
            if (mod.name, name) in res.used:
                continue
            text = res.eval(node, mod, "")
            if text is not None:
                units.append((text, getattr(node, "lineno", 0)))
        for (cname, name), node in mod.class_consts.items():
            if (mod.name, f"{cname}.{name}") in res.used:
                continue
            text = res.eval(node, mod, cname)
            if text is not None:
                units.append((text, getattr(node, "lineno", 0)))
        src = "\n".join(mod.lines)
        sites.extend(_scan_unit_set(mod, "<module>", units, src if False else ""))
    return sorted(set(sites), key=lambda s: (s.path, s.function, s.line, s.label, s.prop))


# --------------------------------------------------------------------------- registry plumbing
def _registry():
    from menhir.services.erasure_fence_registry import COPIERS, CopierEntry, Gate

    return COPIERS, CopierEntry, Gate


def _site_keys(sites: list[Site]) -> set[tuple[str, str]]:
    return {(s.module, s.function) for s in sites}


_SITES_CACHE: list[Site] | None = None


def _sites() -> list[Site]:
    global _SITES_CACHE
    if _SITES_CACHE is None:
        _SITES_CACHE = collect_text_writers(DEFAULT_ROOTS)
    return _SITES_CACHE


# --------------------------------------------------------------------------- the lint
def test_every_text_writer_is_registered() -> None:
    copiers, _entry, _gate = _registry()
    registered = {(c.module, c.function) for c in copiers}
    missing = sorted(_site_keys(_sites()) - registered)
    assert not missing, (
        "text writers with no erasure_fence_registry.COPIERS entry (classify each, or fence it): "
        + "; ".join(f"{m}:{f}" for m, f in missing)
    )


def test_no_stale_registry_entries() -> None:
    copiers, _entry, _gate = _registry()
    found = _site_keys(_sites())
    stale = sorted((c.module, c.function) for c in copiers if (c.module, c.function) not in found)
    assert not stale, "registry entries that no longer match a text writer: " + "; ".join(
        f"{m}:{f}" for m, f in stale
    )


def test_registry_has_no_duplicate_entries() -> None:
    copiers, _entry, _gate = _registry()
    keys = [(c.module, c.function) for c in copiers]
    assert len(keys) == len(set(keys))


def test_unfenced_entries_are_tagged() -> None:
    copiers, _entry, gate = _registry()
    bad = [
        f"{c.module}:{c.function}: {c.note!r}"
        for c in copiers
        if c.gate is gate.UNFENCED
        and not c.note.startswith(("R2 ", "P5 ", "owner-accepted:", "UNTRIAGED:"))
    ]
    assert not bad, "UNFENCED entries without an R2/P5/owner-accepted/UNTRIAGED tag: " + "; ".join(bad)


def test_exempt_entries_carry_a_reason() -> None:
    copiers, _entry, gate = _registry()
    bad = [f"{c.module}:{c.function}" for c in copiers if c.gate is gate.EXEMPT and len(c.note) < 12]
    assert not bad, "EXEMPT entries need a reason in note: " + "; ".join(bad)


def test_unfenced_allowlist_only_shrinks() -> None:
    copiers, _entry, gate = _registry()
    count = sum(1 for c in copiers if c.gate is gate.UNFENCED)
    assert count <= UNFENCED_ALLOWLIST_MAX, (
        f"UNFENCED count {count} exceeds the recorded ceiling {UNFENCED_ALLOWLIST_MAX}: the "
        "allowlist is a ratchet, fix the writer (R2) or classify it honestly; never raise the ceiling"
    )


_EVIDENCE_FOR_GATE = {
    "G_COPY": {"copy_fenced"},
    "G_FENCED": {"run_fenced", "check_fence", "copy_fenced"},
    "G_Q": {"g_q"},
    "G_CREATE": {"g_create"},
}


def test_claimed_gates_have_evidence() -> None:
    copiers, _entry, _gate = _registry()
    by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for s in _sites():
        by_key[(s.module, s.function)].update(s.gate_evidence)
    bad = []
    for c in copiers:
        need = _EVIDENCE_FOR_GATE.get(c.gate.name)
        if need and not (by_key.get((c.module, c.function), set()) & need):
            bad.append(f"{c.module}:{c.function} claims {c.gate.name} but shows none of {sorted(need)}")
    assert not bad, "registry gate claims without evidence in the function: " + "; ".join(bad)


# --------------------------------------------------------------------------- T14 extras
def test_no_delete_of_evidence_namespace_fence() -> None:
    offenders = _fence_delete_offenders(DEFAULT_ROOTS)
    assert not offenders, "EvidenceNamespaceFence must never be deleted: " + "; ".join(offenders)


def test_fence_delete_check_detects_a_delete(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) DETACH DELETE f')\n"
        "def ok(neo):\n"
        "    neo.execute('MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) MATCH (q:Episodic) DELETE q')\n"
    )
    found = _fence_delete_offenders([_write_tree(tmp_path, body)])
    assert len(found) == 1 and ":w:" in found[0]


def _fence_delete_offenders(roots) -> list[str]:
    offenders = []
    index = _build_index(roots)
    res = _Resolver(index)
    for mod in index.values():
        for scope in _scopes(mod):
            units = _units(scope.stmts, mod, res, scope.cls)
            if not any("EvidenceNamespaceFence" in t for t, _ in units):
                continue
            fence_vars = {
                v for t, _ in units for v, labs in _bindings(t).items() if "EvidenceNamespaceFence" in labs
            }
            for text, line in units:
                blank = _blank_strings(text)
                for kw, s, e, _d in _regions(blank, _depths(blank)):
                    if kw in ("DELETE", "DETACH DELETE") and fence_vars & set(re.findall(r"\w+", blank[s:e])):
                        offenders.append(f"{mod.rel}:{scope.qualname}:{line}")
    return sorted(set(offenders))


def test_fence_gates_read_erasure_epoch_not_generation() -> None:
    from menhir.infrastructure import publish_fence

    for name in ("GATE_CYPHER", "RECHECK_CYPHER", "COPY_RECHECK_CYPHER"):
        text = getattr(publish_fence, name)
        assert "erasure_epoch" in text, name
        assert "generation" not in text, name


def test_sidecar_triggers_cover_content_tables_and_spare_the_journal(tmp_path) -> None:
    from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
    from menhir.infrastructure.graph_operations import GraphOperationsJournal
    from menhir.infrastructure.telemetry.erasure_denylist import (
        JOURNAL_TABLES,
        TRIGGER_PREFIX,
        denylist_plan,
    )
    from menhir.infrastructure.telemetry.erasure_inventory import CONTENT_COLUMNS
    from menhir.infrastructure.telemetry.store import McpTelemetryStore

    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    GraphOperationsJournal(db_path=db)._ensure_ready()
    ErasureSubjectStore(db_path=db)._ensure_ready()
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = [
            (r[0], r[1])
            for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'")
            if r[0].startswith(TRIGGER_PREFIX)
        ]
    finally:
        conn.close()
    per_table: dict[str, int] = defaultdict(int)
    for _name, tbl in triggers:
        per_table[tbl] += 1
    plan = denylist_plan()
    assert plan, "denylist plan is empty"
    # Every content table with a uuid subject column that exists in this database is triggered.
    expected = {t for t in plan if t in tables}
    assert expected, "no planned table exists in the fresh telemetry DB"
    for table in sorted(expected):
        assert per_table[table] >= 1, f"no denylist trigger on content table {table}"
    # Content tables the plan skips must be intentional: every CONTENT_COLUMNS table with a subject
    # column and a trigger-eligible shape appears in the plan.
    for journal in JOURNAL_TABLES:
        assert per_table[journal] == 0, f"denylist trigger on journal table {journal}"
        assert journal not in plan
    assert all(t not in JOURNAL_TABLES for t in plan)
    assert {e.table for e in CONTENT_COLUMNS} >= set(plan)


# --------------------------------------------------------------------------- instrument self-tests
def _write_tree(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mod.py").write_text(body, encoding="utf-8")
    return root


def _found(tmp_path: Path, body: str) -> set[tuple[str, str, str]]:
    sites = collect_text_writers([_write_tree(tmp_path, body)])
    return {(s.function, s.label, s.prop) for s in sites}


def test_lint_finds_literal_set(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('MATCH (n:Entity {uuid: $u}) SET n.content = $c')\n"
    )
    assert ("w", "Entity", "content") in _found(tmp_path, body)


def test_lint_finds_set_through_module_constant(tmp_path) -> None:
    body = (
        "Q = 'MATCH (n:Entity {uuid: $u}) SET n.content = $c, n.last_accessed = datetime()'\n"
        "def w(neo):\n"
        "    neo.execute(Q)\n"
    )
    found = _found(tmp_path, body)
    assert ("w", "Entity", "content") in found
    assert not any(f[0] == "<module>" for f in found)


def test_lint_finds_set_through_cypher_builder_chain(tmp_path) -> None:
    body = (
        "from menhir.infrastructure.cypher import Cypher\n"
        "def w():\n"
        "    return Cypher().match('(n:Entity {uuid: $u})').set(['n.summary = $s']).build()\n"
    )
    assert ("w", "Entity", "summary") in _found(tmp_path, body)


def test_lint_finds_relationship_fact_write(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.fact = $f')\n"
    )
    assert ("w", "RELATES_TO", "fact") in _found(tmp_path, body)


def test_lint_finds_map_write(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('MATCH (e:Entity {uuid: $u}) SET e += $props')\n"
    )
    assert ("w", "Entity", "*") in _found(tmp_path, body)


def test_lint_finds_create_property_map_and_foreach(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('CREATE (t:TurnEvidence {text: $t, turn_id: $i})')\n"
        "def v(neo):\n"
        "    neo.execute('MATCH (n:Episodic {uuid: $u}) FOREACH (_ IN [1] | SET n.content = $c)')\n"
    )
    found = _found(tmp_path, body)
    assert ("w", "TurnEvidence", "text") in found
    assert ("v", "Episodic", "content") in found


def test_lint_ignores_structural_entity_write_and_erasing_writes(tmp_path) -> None:
    body = (
        "def w(neo):\n"
        "    neo.execute('MATCH (n:Entity {uuid: $u}) SET n.last_accessed = datetime()')\n"
        "def e(neo):\n"
        "    neo.execute('MATCH (n:Entity {uuid: $u}) SET n.content = null REMOVE n.summary')\n"
        "def c(neo):\n"
        "    neo.execute('MATCH (n:Entity {uuid: $u}) WHERE n.content = $c RETURN n')\n"
    )
    assert _found(tmp_path, body) == set()


# --------------------------------------------------------------------------- census report
def _census_markdown() -> str:
    copiers, _entry, _gate = _registry()
    reg = {(c.module, c.function): c for c in copiers}
    sites = _sites()
    by_file: dict[str, list[Site]] = defaultdict(list)
    for s in sites:
        by_file[s.path].append(s)
    lines = [
        "| file | function | label.property | line | registry gate | evidence |",
        "|---|---|---|---|---|---|",
    ]
    for path in sorted(by_file):
        for s in by_file[path]:
            c = reg.get((s.module, s.function))
            gate = c.gate.value if c else "UNREGISTERED"
            lines.append(
                f"| {path} | {s.function} | {s.label}.{s.prop} | {s.line} | {gate} | "
                f"{','.join(s.gate_evidence)} |"
            )
    funcs = {(s.module, s.function) for s in sites}
    per_gate: dict[str, int] = defaultdict(int)
    for key in funcs:
        c = reg.get(key)
        per_gate[c.gate.value if c else "UNREGISTERED"] += 1
    lines += [
        "",
        f"sites: {len(sites)}; functions: {len(funcs)}; files: {len(by_file)}",
        "per gate (functions): " + ", ".join(f"{g}={n}" for g, n in sorted(per_gate.items())),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.write(_census_markdown() + "\n")
