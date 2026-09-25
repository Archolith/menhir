"""Python symbol and call-graph extraction for the project scanner.

Moved verbatim from ``project_scanner.py``; the facade module re-exports every name here.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from pathlib import Path

from menhir.domain.utils import symbol_structure_path
from menhir.infrastructure.project_scanner_edges import _resolve_module
from menhir.infrastructure.project_scanner_models import (
    CallEdge,
    FileEntry,
    SymbolEntry,
)
from menhir.infrastructure.project_scanner_rules import _MAX_FILE_BYTES
from menhir.infrastructure.text_io import read_text_utf8

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

_SYMBOL_PER_FILE_CAP = 200
_MAX_CALLS_PER_SYMBOL = 50  # max resolved calls tracked per function/method


def _sym_path(sym: SymbolEntry) -> str:
    """Compute the fully qualified structure_path for a symbol.

    Delegates to the shared `domain.utils.symbol_structure_path` (SSOT-12) so
    this and `structure_queries._symbol_path` can never independently drift.
    """
    return symbol_structure_path(sym.file_path, sym.name, sym.parent)


def _build_import_name_map(
    root: Path,
    files: list[FileEntry],
    stack: str,
) -> dict[str, dict[str, tuple[str, str]]]:
    """Build per-file map of imported names to their source for cross-file call resolution.

    Returns {file_rel_path: {local_name: (target_rel_path, original_name)}}.
    Only populated for Python projects.
    """
    if stack != "python":
        return {}

    module_map: dict[str, str] = {}
    for f in files:
        if not f.rel_path.endswith(".py"):
            continue
        mod = f.rel_path.replace("/", ".").replace("\\", ".")
        if mod.endswith(".py"):
            mod = mod[:-3]
        if mod.startswith("src."):
            mod = mod[4:]
        if mod.endswith(".__init__"):
            module_map[mod[:-9]] = f.rel_path
        module_map[mod] = f.rel_path

    result: dict[str, dict[str, tuple[str, str]]] = {}
    for f in files:
        if not f.rel_path.endswith(".py"):
            continue
        full_path = root / f.rel_path
        if not full_path.is_file():
            continue
        try:
            text = read_text_utf8(full_path)
        except OSError:
            continue

        name_map: dict[str, tuple[str, str]] = {}
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"^from\s+([\w.]+)\s+import\s+(.+)$", stripped)
            if not m:
                continue
            mod_name = m.group(1)
            target = _resolve_module(mod_name, module_map)
            if not target or target == f.rel_path:
                continue
            names_str = m.group(2).strip().rstrip("\\").strip("()")
            for part in names_str.split(","):
                part = part.strip()
                if " as " in part:
                    original, local = [p.strip() for p in part.split(" as ", 1)]
                else:
                    original = local = part
                if original and local and original.isidentifier() and local.isidentifier():
                    name_map[local] = (target, original)

        if name_map:
            result[f.rel_path] = name_map

    return result


def _walk_no_inner_defs(stmts: list) -> list:
    """Yield AST nodes from stmts without descending into nested function/class definitions."""
    stack: list = list(stmts)
    out: list = []
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append(child)
    return out


def _extract_call_edges(
    abs_path: str,
    rel_path: str,
    tl_lookup: dict[tuple[str, str], str],
    method_lookup: dict[tuple[str, str, str], str],
    import_names: dict[str, tuple[str, str]],
) -> list[CallEdge]:
    """Extract function-level CALLS edges from a Python file.

    Resolves:
    - bare calls: foo() → same-file top-level function or imported function
    - self/cls calls: self.method() → same-class method

    Returns deduplicated CallEdge list.
    """
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            return []
        source = read_text_utf8(abs_path)
    except OSError:
        return []

    if source.count("\n") > 10_000:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    edges: list[CallEdge] = []
    seen: set[tuple[str, str]] = set()

    def add_edge(caller: str, callee: str) -> None:
        key = (caller, callee)
        if key not in seen and caller != callee:
            seen.add(key)
            edges.append(CallEdge(caller_path=caller, callee_path=callee))

    def resolve_name(name: str) -> str | None:
        if (rel_path, name) in tl_lookup:
            return tl_lookup[(rel_path, name)]
        if name in import_names:
            tgt_path, orig_name = import_names[name]
            if (tgt_path, orig_name) in tl_lookup:
                return tl_lookup[(tgt_path, orig_name)]
        return None

    def resolve_self(method_name: str, class_name: str) -> str | None:
        return method_lookup.get((rel_path, class_name, method_name))

    def walk_body(body: list, caller_sym_path: str, class_ctx: str | None) -> None:
        count = 0
        for node in _walk_no_inner_defs(body):
            if count >= _MAX_CALLS_PER_SYMBOL:
                break
            if not isinstance(node, ast.Call):
                continue
            callee = None
            func = node.func
            if isinstance(func, ast.Name):
                callee = resolve_name(func.id)
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("self", "cls")
                and class_ctx
            ):
                callee = resolve_self(func.attr, class_ctx)
            if callee:
                add_edge(caller_sym_path, callee)
                count += 1

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            caller = tl_lookup.get((rel_path, node.name))
            if caller:
                walk_body(node.body, caller, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    caller = method_lookup.get((rel_path, node.name, item.name))
                    if caller:
                        walk_body(item.body, caller, node.name)

    return edges


def _extract_module_docstring(abs_path: str) -> str:
    """Extract the module-level docstring or first block comment from a Python file."""
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            return ""
        source = read_text_utf8(abs_path)
    except OSError:
        return ""
    try:
        tree = ast.parse(source)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            doc = tree.body[0].value.value
            first_line = doc.splitlines()[0].strip() if doc else ""
            return first_line[:200] if first_line else ""
    except SyntaxError:
        pass
    # Fall back to leading # comment
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:200]
        break
    return ""


def _extract_symbols(abs_path: str, rel_path: str) -> tuple[list[SymbolEntry], bool]:
    """Extract function/class/method symbols from a Python file via stdlib ast.

    Returns (symbols, truncated) where truncated is True if the per-file cap was hit.
    Skips files >2 MB or >10k lines (generated code, data files) and files with syntax errors.
    """
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            logger.debug("Skipping symbol extraction for oversized file: %s", rel_path)
            return [], False
        source = read_text_utf8(abs_path)
    except OSError:
        return [], False

    if source.count("\n") > 10_000:
        logger.debug("Skipping symbol extraction for large file: %s", rel_path)
        return [], False

    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.debug("Skipping symbol extraction for unparseable file: %s", rel_path)
        return [], False

    symbols: list[SymbolEntry] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_make_symbol(node, rel_path, parent=""))
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            symbols.append(SymbolEntry(
                file_path=rel_path,
                name=node.name,
                kind="class",
                line_no=node.lineno,
                signature=f"class {node.name}",
                docstring=doc.splitlines()[0][:120] if doc else "",
                parent="",
            ))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_make_symbol(item, rel_path, parent=node.name))
        if len(symbols) >= _SYMBOL_PER_FILE_CAP:
            break

    truncated = len(symbols) >= _SYMBOL_PER_FILE_CAP
    return symbols, truncated


def _make_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    parent: str,
) -> SymbolEntry:
    sig = _build_signature(node)
    doc = ast.get_docstring(node) or ""
    decorator = _detect_decorator(node)
    return SymbolEntry(
        file_path=rel_path,
        name=node.name,
        kind="method" if parent else "function",
        line_no=node.lineno,
        signature=sig,
        docstring=doc.splitlines()[0][:120] if doc else "",
        parent=parent,
        decorator=decorator,
    )


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({args}){ret}"
    except Exception:
        return f"def {node.name}(...)"


def _detect_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for dec in node.decorator_list:
        name = ast.unparse(dec).split("(")[0]
        if name in ("property", "classmethod", "staticmethod"):
            return name
    return ""
