"""Pure-Python Tree-sitter polyglot structural extractor (#1077).

Extracts deterministic IMPORTS, SYMBOLS (classes/interfaces/structs/functions/methods),
and intra-project CALLS edges for Tier-1 non-Python languages using MIT-licensed
`tree-sitter` and `tree-sitter-language-pack` (zero external LSP or compiler binaries):

  - TypeScript / TSX / JavaScript / JSX
  - Kotlin / Java
  - Go
  - Rust

Attribution:
  Polyglot Tree-sitter structural-extraction architecture and multi-language AST node
  mapping patterns adapted in part from DeusData/codebase-memory-mcp (MIT License,
  https://github.com/DeusData/codebase-memory-mcp), using `tree-sitter` (MIT) and
  `tree-sitter-language-pack` (MIT). See repository NOTICE for full license texts.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from menhir.infrastructure.project_scanner import (
    CallEdge,
    FileEntry,
    ImportEdge,
    SymbolEntry,
)

logger = logging.getLogger(__name__)

_MAX_SYMBOLS_PER_FILE = 60

#: File extension -> tree-sitter-language-pack language name.
EXT_TO_LANG: dict[str, str] = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}

_TS_JS_LANGS = frozenset({"typescript", "tsx", "javascript"})
_JVM_LANGS = frozenset({"kotlin", "java"})

_PARSERS: dict[str, Any] = {}
_TS_AVAILABLE: bool | None = None


def is_treesitter_available() -> bool:
    """True when `tree_sitter_language_pack` is importable in this environment."""
    global _TS_AVAILABLE
    if _TS_AVAILABLE is None:
        try:
            import tree_sitter_language_pack  # noqa: F401

            _TS_AVAILABLE = True
        except ImportError:
            _TS_AVAILABLE = False
    return _TS_AVAILABLE


def lang_for_path(rel_path: str) -> str | None:
    """Return the Tree-sitter language id for *rel_path*, or None if unsupported."""
    ext = os.path.splitext(rel_path)[1].lower()
    return EXT_TO_LANG.get(ext)


def _get_parser(lang: str) -> Any | None:
    if not is_treesitter_available():
        return None
    if lang in _PARSERS:
        return _PARSERS[lang]
    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(lang)
        _PARSERS[lang] = parser
        return parser
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("tree-sitter parser unavailable for %s: %s", lang, exc)
        _PARSERS[lang] = None
        return None


def _parse_tree(text: str, lang: str) -> tuple[Any | None, bytes]:
    parser = _get_parser(lang)
    if parser is None:
        return None, b""
    data = text.encode("utf-8", errors="replace")
    try:
        return parser.parse(data), data
    except Exception as exc:  # pragma: no cover
        logger.debug("tree-sitter parse failed for %s: %s", lang, exc)
        return None, b""


def _node_text(node: Any, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_line(node: Any, data: bytes) -> str:
    raw = _node_text(node, data)
    line = raw.splitlines()[0].strip() if raw else ""
    return line[:160]


def _child_text_by_field(node: Any, field: str, data: bytes) -> str:
    child = node.child_by_field_name(field)
    return _node_text(child, data).strip() if child is not None else ""


def _first_child_of_types(node: Any, types: tuple[str, ...]) -> Any | None:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _sym_path(sym: SymbolEntry) -> str:
    if sym.parent:
        return f"{sym.file_path}::{sym.parent}.{sym.name}"
    return f"{sym.file_path}::{sym.name}"


# ---------------------------------------------------------------------------
# Symbol Extraction
# ---------------------------------------------------------------------------


def extract_polyglot_symbols(
    text: str, rel_path: str, lang: str
) -> tuple[list[SymbolEntry], bool]:
    """Extract classes/structs/interfaces/functions/methods for *rel_path*."""
    tree, data = _parse_tree(text, lang)
    if tree is None:
        return [], False

    symbols: list[SymbolEntry] = []
    truncated = False

    def add_sym(
        name: str, kind: str, node: Any, parent: str = ""
    ) -> bool:
        nonlocal truncated
        if not name:
            return True
        if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
            truncated = True
            return False
        symbols.append(
            SymbolEntry(
                file_path=rel_path,
                name=name,
                kind=kind,
                line_no=node.start_point[0] + 1,
                signature=_first_line(node, data),
                docstring="",
                parent=parent,
            )
        )
        return True

    def walk(node: Any, class_ctx: str = "") -> None:
        if truncated:
            return
        nt = node.type

        if lang in _TS_JS_LANGS:
            if nt in ("class_declaration", "class", "interface_declaration", "type_alias_declaration", "enum_declaration"):
                name = _child_text_by_field(node, "name", data)
                if add_sym(name, "class", node, parent=class_ctx):
                    for ch in node.children:
                        walk(ch, class_ctx=name or class_ctx)
                return
            if nt in ("function_declaration", "generator_function_declaration"):
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "method" if class_ctx else "function", node, parent=class_ctx)
                return
            if nt == "method_definition":
                name = _child_text_by_field(node, "name", data)
                if name and name != "constructor":
                    add_sym(name, "method", node, parent=class_ctx)
                return
            if nt == "variable_declarator" and not class_ctx:
                val = node.child_by_field_name("value")
                if val is not None and val.type in ("arrow_function", "function_expression", "function"):
                    name = _child_text_by_field(node, "name", data)
                    add_sym(name, "function", node)
                    return

        elif lang == "kotlin":
            if nt in ("class_declaration", "object_declaration", "interface_declaration"):
                ident = _first_child_of_types(node, ("type_identifier", "simple_identifier"))
                name = _node_text(ident, data).strip() if ident is not None else ""
                if add_sym(name, "class", node, parent=class_ctx):
                    for ch in node.children:
                        walk(ch, class_ctx=name or class_ctx)
                return
            if nt == "function_declaration":
                ident = _first_child_of_types(node, ("simple_identifier",))
                name = _node_text(ident, data).strip() if ident is not None else ""
                add_sym(name, "method" if class_ctx else "function", node, parent=class_ctx)
                return

        elif lang == "java":
            if nt in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                name = _child_text_by_field(node, "name", data)
                if add_sym(name, "class", node, parent=class_ctx):
                    for ch in node.children:
                        walk(ch, class_ctx=name or class_ctx)
                return
            if nt == "method_declaration":
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "method", node, parent=class_ctx)
                return

        elif lang == "go":
            if nt == "type_spec":
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "class", node)
                return
            if nt == "function_declaration":
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "function", node)
                return
            if nt == "method_declaration":
                name = _child_text_by_field(node, "name", data)
                recv = node.child_by_field_name("receiver")
                parent = _extract_go_receiver_type(recv, data) if recv is not None else ""
                add_sym(name, "method", node, parent=parent)
                return

        elif lang == "rust":
            if nt in ("struct_item", "enum_item", "trait_item"):
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "class", node)
                return
            if nt == "impl_item":
                t_node = node.child_by_field_name("type")
                impl_type = _extract_rust_type_name(t_node, data) if t_node is not None else ""
                for ch in node.children:
                    walk(ch, class_ctx=impl_type or class_ctx)
                return
            if nt == "function_item":
                name = _child_text_by_field(node, "name", data)
                add_sym(name, "method" if class_ctx else "function", node, parent=class_ctx)
                return

        for ch in node.children:
            walk(ch, class_ctx=class_ctx)

    walk(tree.root_node)
    return symbols, truncated


def _extract_go_receiver_type(recv_node: Any, data: bytes) -> str:
    txt = _node_text(recv_node, data)
    m = re.search(r"\*?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[.*?\])?\s*\)", txt)
    return m.group(1) if m else ""


def _extract_rust_type_name(type_node: Any, data: bytes) -> str:
    txt = _node_text(type_node, data).strip()
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", txt)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Import Extraction & Resolution
# ---------------------------------------------------------------------------


def _read_go_module_path(root: Path) -> str:
    gomod = root / "go.mod"
    if not gomod.is_file():
        return ""
    try:
        for line in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*module\s+(\S+)", line)
            if m:
                return m.group(1).strip()
    except OSError:
        return ""
    return ""


def _resolve_ts_js_specifier(
    importer: str, specifier: str, file_set: set[str]
) -> str | None:
    if not specifier.startswith("."):
        return None
    base_dir = os.path.dirname(importer)
    joined = os.path.normpath(os.path.join(base_dir, specifier)).replace("\\", "/")
    if joined.startswith("./"):
        joined = joined[2:]
    candidates = [joined]
    # Strip .js/.mjs/.cjs mapped to .ts/.tsx in ESM TypeScript projects
    stem = re.sub(r"\.(mjs|cjs|js|jsx)$", "", joined)
    for ext in (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs"):
        candidates.append(joined + ext)
        if stem != joined:
            candidates.append(stem + ext)
    for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
        candidates.append(f"{joined}/{idx}")
    for cand in candidates:
        if cand in file_set and cand != importer:
            return cand
    return None


def _extract_ts_js_imports_from_tree(
    tree: Any, data: bytes
) -> list[tuple[str, list[str]]]:
    """Return list of (specifier, [local_imported_names])."""
    out: list[tuple[str, list[str]]] = []

    def walk(node: Any) -> None:
        nt = node.type
        if nt in ("import_statement", "export_statement"):
            src = node.child_by_field_name("source")
            if src is not None:
                spec = _node_text(src, data).strip().strip("'\"")
                names: list[str] = []
                _collect_ts_import_names(node, data, names)
                out.append((spec, names))
            return
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and _node_text(fn, data).strip() == "require":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for ch in args.children:
                        if ch.type == "string":
                            spec = _node_text(ch, data).strip().strip("'\"")
                            out.append((spec, []))
                            break
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return out


def _collect_ts_import_names(node: Any, data: bytes, names: list[str]) -> None:
    if node.type == "import_specifier":
        alias = node.child_by_field_name("alias")
        name = node.child_by_field_name("name")
        target = alias or name
        if target is not None:
            names.append(_node_text(target, data).strip())
        return
    if node.type == "identifier" and node.parent is not None and node.parent.type == "import_clause":
        names.append(_node_text(node, data).strip())
        return
    for ch in node.children:
        _collect_ts_import_names(ch, data, names)


def _extract_jvm_package_and_imports(
    tree: Any, data: bytes, lang: str
) -> tuple[str, list[str]]:
    package = ""
    imports: list[str] = []

    def walk(node: Any) -> None:
        nonlocal package
        nt = node.type
        if nt in ("package_header", "package_declaration"):
            txt = _node_text(node, data)
            m = re.search(r"package\s+([A-Za-z0-9_.]+)", txt)
            if m:
                package = m.group(1)
            return
        if nt in ("import_header", "import_declaration"):
            txt = _node_text(node, data)
            m = re.search(r"import\s+(?:static\s+)?([A-Za-z0-9_.*]+)", txt)
            if m:
                imports.append(m.group(1))
            return
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return package, imports


def _extract_go_imports(tree: Any, data: bytes) -> list[tuple[str, str]]:
    """Return list of (import_path, local_alias_or_pkg_name)."""
    out: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            name_node = node.child_by_field_name("name")
            if path_node is not None:
                imp_path = _node_text(path_node, data).strip().strip('"`')
                alias = _node_text(name_node, data).strip() if name_node is not None else ""
                if not alias:
                    alias = imp_path.rsplit("/", 1)[-1]
                out.append((imp_path, alias))
            return
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return out


def _extract_rust_uses(tree: Any, data: bytes) -> list[str]:
    uses: list[str] = []

    def walk(node: Any) -> None:
        if node.type == "use_declaration":
            txt = _node_text(node, data).strip()
            txt = re.sub(r"^(?:pub(?:\([^)]*\))?\s+)?use\s+", "", txt).rstrip(";").strip()
            if txt:
                uses.append(txt)
            return
        if node.type == "mod_item":
            # External module declaration `mod foo;` (no body block)
            if node.child_by_field_name("body") is None:
                name = _child_text_by_field(node, "name", data)
                if name:
                    uses.append(f"self::{name}")
            return
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return uses


def extract_polyglot_imports(
    root: Path,
    files: list[FileEntry],
    read_text_fn: Callable[[Path], str],
) -> tuple[list[ImportEdge], dict[str, dict[str, str]]]:
    """Extract intra-repo ImportEdges and local-name maps for all Tier-1 non-Python files."""
    if not is_treesitter_available():
        return [], {}

    file_set = {f.rel_path for f in files}
    edges: list[ImportEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    import_name_map: dict[str, dict[str, str]] = {}

    # Pre-index JVM files by (package, stem) and Go files by directory
    jvm_by_fqn: dict[str, str] = {}
    jvm_by_pkg: dict[str, list[str]] = {}
    go_by_dir: dict[str, list[str]] = {}
    parsed_cache: dict[str, tuple[str, Any, bytes]] = {}

    for fe in files:
        lang = lang_for_path(fe.rel_path)
        if lang is None:
            continue
        if lang == "go":
            d = os.path.dirname(fe.rel_path)
            if not fe.rel_path.endswith("_test.go"):
                go_by_dir.setdefault(d, []).append(fe.rel_path)
        try:
            text = read_text_fn(root / fe.rel_path)
        except OSError:
            continue
        tree, data = _parse_tree(text, lang)
        if tree is None:
            continue
        parsed_cache[fe.rel_path] = (lang, tree, data)
        if lang in _JVM_LANGS:
            pkg, _ = _extract_jvm_package_and_imports(tree, data, lang)
            stem = os.path.splitext(os.path.basename(fe.rel_path))[0]
            if pkg:
                jvm_by_fqn[f"{pkg}.{stem}"] = fe.rel_path
                jvm_by_pkg.setdefault(pkg, []).append(fe.rel_path)

    go_module = _read_go_module_path(root)

    def add_edge(src: str, tgt: str, local_names: list[str] | None = None) -> None:
        if not tgt or tgt == src:
            return
        key = (src, tgt)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append(ImportEdge(source_path=src, target_path=tgt))
        if local_names:
            nmap = import_name_map.setdefault(src, {})
            for nm in local_names:
                if nm and nm not in ("*", "_", "."):
                    nmap[nm] = tgt

    for rel_path, (lang, tree, data) in parsed_cache.items():
        if lang in _TS_JS_LANGS:
            for spec, names in _extract_ts_js_imports_from_tree(tree, data):
                tgt = _resolve_ts_js_specifier(rel_path, spec, file_set)
                if tgt:
                    add_edge(rel_path, tgt, names)

        elif lang in _JVM_LANGS:
            _, imps = _extract_jvm_package_and_imports(tree, data, lang)
            for imp in imps:
                if imp.endswith(".*"):
                    pkg = imp[:-2]
                    for tgt in jvm_by_pkg.get(pkg, ()):
                        add_edge(rel_path, tgt)
                elif imp in jvm_by_fqn:
                    add_edge(rel_path, jvm_by_fqn[imp], [imp.rsplit(".", 1)[-1]])
                else:
                    # Importing a symbol inside a class/file: com.example.AuthKt.verify -> com.example.AuthKt
                    parent_fqn = imp.rsplit(".", 1)[0] if "." in imp else ""
                    sym_name = imp.rsplit(".", 1)[-1]
                    if parent_fqn in jvm_by_fqn:
                        add_edge(rel_path, jvm_by_fqn[parent_fqn], [sym_name])
                    elif parent_fqn in jvm_by_pkg:
                        for tgt in jvm_by_pkg[parent_fqn]:
                            add_edge(rel_path, tgt, [sym_name])

        elif lang == "go":
            # Same-package tests (`foo_test.go` in same dir as `foo.go`) implicitly depend on pkg files
            if rel_path.endswith("_test.go"):
                d = os.path.dirname(rel_path)
                for tgt in go_by_dir.get(d, ()):
                    add_edge(rel_path, tgt)
            for imp_path, alias in _extract_go_imports(tree, data):
                rel_dir = ""
                if go_module and imp_path.startswith(go_module + "/"):
                    rel_dir = imp_path[len(go_module) + 1 :]
                elif go_module and imp_path == go_module:
                    rel_dir = ""
                else:
                    # Fallback: suffix match against repo directories
                    for cand_dir in go_by_dir:
                        if cand_dir and imp_path.endswith("/" + cand_dir):
                            rel_dir = cand_dir
                            break
                    else:
                        continue
                for tgt in go_by_dir.get(rel_dir, ()):
                    add_edge(rel_path, tgt, [alias])

        elif lang == "rust":
            for use_expr in _extract_rust_uses(tree, data):
                _resolve_rust_use(rel_path, use_expr, file_set, add_edge)

    return edges, import_name_map


def _resolve_rust_use(
    importer: str,
    use_expr: str,
    file_set: set[str],
    add_edge: Callable[[str, str, list[str] | None], None],
) -> None:
    # Expand simple brace groups `crate::foo::{a, b}`
    m = re.match(r"^([A-Za-z0-9_:]+)::\{([^}]+)\}$", use_expr)
    if m:
        prefix, inner = m.group(1), m.group(2)
        items = [x.strip() for x in inner.split(",") if x.strip()]
    else:
        prefix, items = use_expr, []

    parts = [p for p in prefix.split("::") if p]
    if not parts:
        return
    if parts[0] == "crate":
        base_parts = ["src"]
        rest = parts[1:]
    elif parts[0] == "self":
        d = os.path.dirname(importer)
        base_parts = d.split("/") if d else ["src"]
        rest = parts[1:]
    elif parts[0] == "super":
        d = os.path.dirname(os.path.dirname(importer))
        base_parts = d.split("/") if d else ["src"]
        rest = parts[1:]
    else:
        return

    # Try longest module prefix matching a file (`src/a/b.rs` or `src/a/b/mod.rs`)
    for cut in range(len(rest), 0, -1):
        mod_rel = "/".join(base_parts + rest[:cut])
        for cand in (f"{mod_rel}.rs", f"{mod_rel}/mod.rs"):
            if cand in file_set and cand != importer:
                local_names = items if items else ([rest[-1]] if cut < len(rest) else [])
                add_edge(importer, cand, local_names)
                return


# ---------------------------------------------------------------------------
# Call-Edge Extraction
# ---------------------------------------------------------------------------


def extract_polyglot_call_edges(
    text: str,
    rel_path: str,
    lang: str,
    file_import_names: dict[str, str],
    tl_lookup: dict[tuple[str, str], str],
    method_lookup: dict[tuple[str, str, str], str],
    imported_files: set[str] | None = None,
) -> list[CallEdge]:
    """Extract intra-project CallEdges for one non-Python file using Tree-sitter."""
    tree, data = _parse_tree(text, lang)
    if tree is None:
        return []

    edges: list[CallEdge] = []
    seen: set[tuple[str, str]] = set()

    # Build quick name -> symbol_path lookup across imported files
    imported_fn_lookup: dict[str, str] = {}
    for target_file in imported_files or ():
        for (fp, fn_name), sp in tl_lookup.items():
            if fp == target_file:
                imported_fn_lookup.setdefault(fn_name, sp)
        for (fp, _cls, m_name), sp in method_lookup.items():
            if fp == target_file:
                imported_fn_lookup.setdefault(m_name, sp)

    def add_edge(caller: str, callee: str) -> None:
        if caller and callee and caller != callee and (caller, callee) not in seen:
            seen.add((caller, callee))
            edges.append(CallEdge(caller_path=caller, callee_path=callee))

    def resolve_callee(callee_name: str, obj_name: str, class_ctx: str) -> str | None:
        # 1. Explicit imported symbol (`import { recordEntry } from './ledger'`)
        if callee_name in file_import_names:
            tgt_file = file_import_names[callee_name]
            if (tgt_file, callee_name) in tl_lookup:
                return tl_lookup[(tgt_file, callee_name)]
        # 2. Qualified module/alias call (`ledger.recordEntry` / `ledger::record_entry`)
        if obj_name and obj_name in file_import_names:
            tgt_file = file_import_names[obj_name]
            if (tgt_file, callee_name) in tl_lookup:
                return tl_lookup[(tgt_file, callee_name)]
            for (fp, _cls, m_name), sp in method_lookup.items():
                if fp == tgt_file and m_name == callee_name:
                    return sp
        # 3. Same-class method (`this.foo()` / `self.foo()`)
        if class_ctx and (rel_path, class_ctx, callee_name) in method_lookup:
            return method_lookup[(rel_path, class_ctx, callee_name)]
        # 4. Same-file top-level function
        if (rel_path, callee_name) in tl_lookup:
            return tl_lookup[(rel_path, callee_name)]
        # 5. Any function/method in an imported file
        if callee_name in imported_fn_lookup:
            return imported_fn_lookup[callee_name]
        return None

    def extract_call_target(call_node: Any) -> tuple[str, str]:
        """Return (callee_name, qualifier_obj_name) from a call AST node."""
        if lang == "java" and call_node.type == "method_invocation":
            name = _child_text_by_field(call_node, "name", data)
            obj = _child_text_by_field(call_node, "object", data)
            return name, obj
        fn_node = call_node.child_by_field_name("function")
        if fn_node is None and call_node.children:
            fn_node = call_node.children[0]
        if fn_node is None:
            return "", ""
        raw = _node_text(fn_node, data).strip()
        if "::" in raw:
            parts = raw.rsplit("::", 1)
            return parts[1].strip(), parts[0].rsplit("::", 1)[-1].strip()
        if "." in raw:
            parts = raw.rsplit(".", 1)
            return parts[1].strip(), parts[0].rsplit(".", 1)[-1].strip()
        return raw, ""

    def walk(node: Any, caller_sym: str = "", class_ctx: str = "") -> None:
        nt = node.type
        next_caller = caller_sym
        next_class = class_ctx

        # Enter class / impl scope
        if lang in _TS_JS_LANGS and nt in ("class_declaration", "class"):
            next_class = _child_text_by_field(node, "name", data) or class_ctx
        elif lang == "kotlin" and nt in ("class_declaration", "object_declaration"):
            ident = _first_child_of_types(node, ("type_identifier", "simple_identifier"))
            next_class = _node_text(ident, data).strip() if ident is not None else class_ctx
        elif lang == "java" and nt in ("class_declaration", "record_declaration"):
            next_class = _child_text_by_field(node, "name", data) or class_ctx
        elif lang == "rust" and nt == "impl_item":
            t_node = node.child_by_field_name("type")
            next_class = _extract_rust_type_name(t_node, data) if t_node is not None else class_ctx

        # Enter function / method scope
        fn_name = ""
        if lang in _TS_JS_LANGS and nt in ("function_declaration", "generator_function_declaration", "method_definition"):
            fn_name = _child_text_by_field(node, "name", data)
        elif lang in _TS_JS_LANGS and nt == "variable_declarator" and not class_ctx:
            val = node.child_by_field_name("value")
            if val is not None and val.type in ("arrow_function", "function_expression", "function"):
                fn_name = _child_text_by_field(node, "name", data)
        elif lang == "kotlin" and nt == "function_declaration":
            ident = _first_child_of_types(node, ("simple_identifier",))
            fn_name = _node_text(ident, data).strip() if ident is not None else ""
        elif lang == "java" and nt == "method_declaration":
            fn_name = _child_text_by_field(node, "name", data)
        elif lang == "go" and nt in ("function_declaration", "method_declaration"):
            fn_name = _child_text_by_field(node, "name", data)
            if nt == "method_declaration":
                recv = node.child_by_field_name("receiver")
                next_class = _extract_go_receiver_type(recv, data) if recv is not None else ""
        elif lang == "rust" and nt == "function_item":
            fn_name = _child_text_by_field(node, "name", data)

        if fn_name:
            if next_class and (rel_path, next_class, fn_name) in method_lookup:
                next_caller = method_lookup[(rel_path, next_class, fn_name)]
            elif (rel_path, fn_name) in tl_lookup:
                next_caller = tl_lookup[(rel_path, fn_name)]

        # Record call sites inside a known caller
        if next_caller and nt in ("call_expression", "method_invocation"):
            callee_name, obj_name = extract_call_target(node)
            if callee_name:
                target = resolve_callee(callee_name, obj_name, next_class)
                if target:
                    add_edge(next_caller, target)

        for ch in node.children:
            walk(ch, caller_sym=next_caller, class_ctx=next_class)

    walk(tree.root_node)
    return edges
