#!/usr/bin/env python3
"""Canonical mechanical probe for Menhir audit lanes.

Structural checks only. This tool answers "what shape is this code", never
"does this code do the right thing". It is intent-free by design: it does not
read docstrings for meaning, and it never trusts a comment.

Why it exists: the project's own test suite is confirmatory -- written by the
same authors, encoding the same assumptions -- so it passes while real defects
sit in the tree. These checks are orthogonal to that.

Usage:
    python menhir_audit_probe.py <module_dir> [--type a1|a2|a5|a6|a7] [--repo ROOT]

Every audit report section must map to a check emitted here. A report claim not
traceable to probe output must be labelled NOT MECHANICALLY VERIFIED.

Exit code is always 0: this is a measuring instrument, not a gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SEP = "=" * 78

# Source files contain non-ASCII (typographic minus, em dash, arrows). Echoing
# them raises UnicodeEncodeError on a cp1252 console, which killed a full run
# mid-report. Force a UTF-8 stdout where possible and sanitise every echoed
# source fragment regardless, so output is ASCII-safe on any terminal.
try:  # pragma: no cover - depends on stream type
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


_CORPUS_CACHE: dict[str, str] = {}


def repo_corpus(repo_root: Path, dirs: tuple = ("src", "tests", "scripts")) -> str:
    """Concatenated repo source, cached. Rebuilding it per check was the main
    cost on large modules and pushed full-module runs past a two-minute budget."""
    key = "%s|%s" % (repo_root, ",".join(dirs))
    if key not in _CORPUS_CACHE:
        parts = []
        for base in dirs:
            d = repo_root / base
            if d.exists():
                for f in iter_py(d):
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
        _CORPUS_CACHE[key] = "\n".join(parts)
    return _CORPUS_CACHE[key]


_TOKEN_CACHE: dict[str, dict] = {}


def corpus_tokens(repo_root: Path, dirs: tuple = ("src", "tests", "scripts")) -> dict:
    """Identifier frequency table for the repo, built in one pass.

    Replaces a per-symbol regex scan of the whole corpus. That was O(symbols x
    corpus) and pushed a 58-file module past a 60s budget; this is one pass plus
    O(1) lookups.
    """
    key = "%s|%s" % (repo_root, ",".join(dirs))
    if key not in _TOKEN_CACHE:
        # Counter over findall runs the counting loop in C. A per-token dict
        # increment in Python cost ~40s per call on this repo; this is ~1s.
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", repo_corpus(repo_root, dirs))
        _TOKEN_CACHE[key] = Counter(tokens)
    return _TOKEN_CACHE[key]


def ascii_safe(text: str) -> str:
    """Reduce arbitrary source text to printable ASCII."""
    return text.encode("ascii", "replace").decode("ascii")


def package_root(module: Path, repo_root: Path) -> tuple[str, Path]:
    """Infer (top-level package name, its directory) from the module path.

    Hardcoding the package name made every import-based check silently return
    zero on any other project -- the reader sees "0 violations" and concludes
    clean, when the check never ran. Silent zeros are the failure this tool
    exists to prevent, so the package is derived instead.
    """
    parts = module.resolve().relative_to(repo_root.resolve()).parts
    if "src" in parts:
        i = parts.index("src")
        if i + 1 < len(parts):
            return parts[i + 1], repo_root / "src" / parts[i + 1]
    # no src/ layout: treat the module's own top directory as the package
    return (parts[0], repo_root / parts[0]) if parts else (module.name, module)


def sibling_layers(pkg_dir: Path) -> tuple:
    """Immediate subpackages of the top-level package -- the 'layers'.

    Derived rather than listed, so the check applies to any project instead of
    only the one it was written against.
    """
    if not pkg_dir.is_dir():
        return ()
    # Any subdirectory holding .py files counts. Requiring __init__.py missed
    # PEP 420 namespace packages entirely and reported "0 subpackages" for a
    # project that plainly had them -- another silent zero.
    return tuple(sorted(
        d.name for d in pkg_dir.iterdir()
        if d.is_dir() and d.name != "__pycache__" and any(d.glob("*.py"))
    ))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def iter_py(root: Path, recursive: bool = True):
    """Yield .py files under root, skipping caches. Deterministic order."""
    globber = root.rglob if recursive else root.glob
    for p in sorted(globber("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def parse(path: Path):
    """Return (ast.Module, source_text) or (None, text) when unparsable."""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return ast.parse(text), text
    except SyntaxError:
        return None, text


def rel(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def body_digest(node: ast.AST) -> str:
    """Structural digest of a function body, ignoring docstring and formatting.

    Two functions with the same digest have the same executable body even if
    their docstrings, parameter names, or comments differ. This is the check
    signature comparison cannot make -- two confirmed defects in this codebase
    had compatible signatures and dispatched to different implementations.
    """
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if not body:
        return ""
    dumped = "\n".join(ast.dump(stmt, annotate_fields=False) for stmt in body)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def enclosing_async(tree: ast.Module) -> dict[int, str]:
    """Map every line inside an async def to that function's name."""
    spans: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                spans[line] = node.name
    return spans


def section(title: str) -> None:
    print("\n" + SEP)
    print(title)
    print(SEP)


# --------------------------------------------------------------------------
# core checks -- run for every module and every audit type
# --------------------------------------------------------------------------


def check_line_reconciliation(files: list[Path], repo: Path) -> None:
    section("CORE 1. LINE RECONCILIATION")
    total = 0
    for p in files:
        n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        total += n
        print("  %-52s %5d" % (rel(p, repo), n))
    print("  " + "-" * 58)
    print("  TOTAL: %d lines across %d files" % (total, len(files)))
    print("\n  Report this number verbatim. Do not restate a figure from the brief.")


def check_duplicate_bodies(files: list[Path], repo: Path) -> None:
    section("CORE 2. DUPLICATE DEFINITIONS (body-compare, not signature)")
    by_name: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)

    def collect(node: ast.AST, qual: str, path: Path) -> None:
        """Walk with class context so Foo.__init__ and Bar.__init__ stay distinct.

        A flat ast.walk cannot tell two same-named methods on different classes
        apart, and would falsely report 'later definition wins' for them. Only a
        redefinition within the SAME class body actually shadows.
        """
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                collect(child, "%s%s." % (qual, child.name), path)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in child.args.args]
                by_name["%s%s" % (qual, child.name)].append(
                    (rel(path, repo), child.lineno, body_digest(child), ",".join(args))
                )
                # nested defs are closures, not redefinitions -- do not descend

    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        collect(tree, "", p)

    hits = 0
    for name, defs in sorted(by_name.items()):
        if len(defs) < 2:
            continue
        same_file = defaultdict(list)
        for f, line, dig, args in defs:
            same_file[f].append((line, dig, args))
        for fname, entries in sorted(same_file.items()):
            if len(entries) < 2:
                continue
            hits += 1
            print("\n  [SAME FILE] %s defined %d times in %s" % (name, len(entries), fname))
            for line, dig, args in entries:
                print("      line %-5d body=%s args=(%s)" % (line, dig or "<empty>", args))
            digests = {d for _, d, _ in entries}
            print("      -> LATER DEFINITION WINS. bodies %s"
                  % ("IDENTICAL" if len(digests) == 1 else "DIFFER"))
        if len(same_file) > 1:
            hits += 1
            print("\n  [CROSS FILE] %s defined in %d files" % (name, len(same_file)))
            for f, entries in sorted(same_file.items()):
                for line, dig, args in entries:
                    print("      %-46s line %-5d body=%s" % (f, line, dig or "<empty>"))
            all_digests = {d for entries in same_file.values() for _, d, _ in entries}
            print("      -> bodies %s"
                  % ("IDENTICAL (DRY candidate)" if len(all_digests) == 1 else "DIFFER"))

    if hits == 0:
        print("  none found")
    print("\n  Duplicate name groups reported: %d" % hits)


def check_unread_constants(files: list[Path], repo: Path, repo_root: Path) -> None:
    section("CORE 3. MODULE CONSTANTS NEVER READ")
    consts: list[tuple[str, int, str]] = []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                if name.isupper() and len(name) > 2:
                    consts.append((rel(p, repo), node.lineno, name))

    counts = corpus_tokens(repo_root)

    unread = []
    for fname, line, name in consts:
        if counts.get(name, 0) <= 1:
            unread.append((fname, line, name))

    if unread:
        for fname, line, name in unread:
            print("  %s:%d  %s  -- 1 occurrence repo-wide (its own definition)" % (fname, line, name))
    else:
        print("  none found")
    print("\n  Constants scanned: %d   never read: %d" % (len(consts), len(unread)))


def check_private_imports(files: list[Path], repo: Path, pkg: str = "menhir") -> None:
    section("CORE 4. CROSS-MODULE PRIVATE-SYMBOL IMPORTS  [package: %s]" % pkg)
    rows = []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        own_pkg = p.parent.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(pkg):
                continue
            src_pkg = node.module.split(".")[-2] if "." in node.module else ""
            for alias in node.names:
                if alias.name.startswith("_"):
                    scope = "same-package" if src_pkg == own_pkg else "CROSS-PACKAGE"
                    rows.append((rel(p, repo), node.lineno, node.module, alias.name, scope))

    for fname, line, mod, sym, scope in rows:
        print("  %-44s:%-5d %-14s %s <- %s" % (fname, line, scope, sym, mod))
    if not rows:
        print("  none found")
    cross = sum(1 for r in rows if r[4] == "CROSS-PACKAGE")
    print("\n  Private imports: %d total, %d cross-package" % (len(rows), cross))


def check_layering(files: list[Path], repo: Path, pkg: str = "menhir",
                   layers: tuple = ()) -> None:
    section("CORE 5. LAYERING EDGES  [package: %s]" % pkg)
    if not layers:
        print("  WARNING: no subpackages detected under %s -- this check cannot run." % pkg)
        print("  A zero below would mean 'not measured', not 'no violations'.")
        return
    print("  layers detected: %s\n" % ", ".join(layers))
    prefix = pkg + "."
    rows = []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        own = p.parent.name
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith(prefix):
                        mod = a.name
            if not mod or not mod.startswith(prefix):
                continue
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if target in layers and target != own:
                rows.append((rel(p, repo), node.lineno, own, target, mod))

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for fname, line, own, target, mod in rows:
        counts[(own, target)] += 1
        print("  %-44s:%-5d %s -> %-16s %s" % (fname, line, own, target, mod))
    if not rows:
        print("  none found")
    print("\n  Edge summary:")
    for (own, target), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("    %-12s -> %-16s %d" % (own, target, n))
    print("\n  Total cross-layer import statements: %d" % len(rows))
    print("  NOTE: an edge is not automatically a violation. Judge direction against")
    print("        the project's intended layering; report the judgement separately.")


def check_dead_symbols(files: list[Path], repo: Path, repo_root: Path) -> None:
    section("CORE 6. TOP-LEVEL SYMBOLS WITH NO REFERENCE OUTSIDE THEIR DEFINITION")
    defined: list[tuple[str, int, str]] = []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("__"):
                    defined.append((rel(p, repo), node.lineno, node.name))

    counts = corpus_tokens(repo_root)

    dead = []
    for fname, line, name in defined:
        if counts.get(name, 0) <= 1:
            dead.append((fname, line, name))

    for fname, line, name in dead:
        print("  %s:%d  %s" % (fname, line, name))
    if not dead:
        print("  none found")
    print("\n  Top-level symbols: %d   unreferenced: %d" % (len(defined), len(dead)))
    print("  NOTE: decorator-dispatched handlers (FastAPI routes, MCP tools) appear here")
    print("        and are NOT dead. Confirm the dispatch mechanism before reporting.")


def check_comment_claims(files: list[Path], repo: Path) -> None:
    section("CORE 7. CONTROL-ASSERTING COMMENTS (candidates for verification)")
    pattern = re.compile(
        r"(fail[- ]?clos|fail[- ]?open|constant[- ]?time|never|always|cannot|must not|"
        r"guarantee|ensures?|prevents?|only applies|runs at startup|is required|"
        r"single[- ]use|one[- ]shot|atomic)",
        re.IGNORECASE,
    )
    rows = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("#") or stripped.startswith('"""')
                    or stripped.startswith("*") or stripped.startswith("'''")):
                continue
            if pattern.search(stripped):
                rows.append((rel(p, repo), i, ascii_safe(stripped[:96])))

    for fname, line, text in rows:
        print("  %s:%d  %s" % (fname, line, text))
    if not rows:
        print("  none found")
    print("\n  Control-asserting comments: %d" % len(rows))
    print("  Each is a CLAIM TO VERIFY, not documentation. This codebase has nine")
    print("  confirmed cases where such a comment described a control the code lacks.")


# --------------------------------------------------------------------------
# audit-type plugins
# --------------------------------------------------------------------------


def plugin_a1_correctness(files: list[Path], repo: Path) -> None:
    section("A1-1. except Exception WHERE CancelledError CAN ESCAPE")
    rows = []
    for p in files:
        tree, text = parse(p)
        if tree is None:
            continue
        spans = enclosing_async(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            has_finally = bool(node.finalbody)
            for handler in node.handlers:
                t = handler.type
                broad = (isinstance(t, ast.Name) and t.id == "Exception")
                if not broad:
                    continue
                names = [
                    h.type.attr if isinstance(h.type, ast.Attribute)
                    else (h.type.id if isinstance(h.type, ast.Name) else "")
                    for h in node.handlers
                ]
                if any("CancelledError" in n or n == "BaseException" for n in names):
                    continue
                in_async = spans.get(handler.lineno)
                rows.append((rel(p, repo), handler.lineno, in_async or "-", has_finally))

    for fname, line, fn, fin in rows:
        flag = "" if fin else "  <- NO finally: state set before try is not reset"
        print("  %-46s:%-5d async=%-28s finally=%s%s" % (fname, line, fn, fin, flag))
    if not rows:
        print("  none found")
    risky = [r for r in rows if r[2] != "-" and not r[3]]
    print("\n  Broad handlers: %d   inside async def with no finally: %d" % (len(rows), len(risky)))
    print("  CancelledError derives from BaseException and is NOT caught by except Exception.")

    section("A1-2. LEXICOGRAPHIC TIMESTAMP COMPARISON")
    iso_writers, sql_now = [], []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if "isoformat()" in line:
                iso_writers.append((rel(p, repo), i, ascii_safe(line.strip()[:80])))
            if "datetime('now'" in line or 'datetime("now"' in line:
                sql_now.append((rel(p, repo), i, ascii_safe(line.strip()[:80])))
    print("  isoformat() writers ('T' separator, 0x54):")
    for f, i, t in iso_writers or []:
        print("    %s:%d  %s" % (f, i, t))
    if not iso_writers:
        print("    none")
    print("\n  SQLite datetime('now') comparisons (space separator, 0x20):")
    for f, i, t in sql_now or []:
        print("    %s:%d  %s" % (f, i, t))
    if not sql_now:
        print("    none")
    if iso_writers and sql_now:
        print("\n  BOTH PRESENT -> TEXT comparison over-includes. 'T' (0x54) > ' ' (0x20).")
    else:
        print("\n  Both forms not present together in this module.")

    section("A1-3. CALLER/CALLEE KEYWORD MISMATCH (intra-module)")
    sigs: dict[str, tuple[str, int, set[str], bool]] = {}
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kw = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
                sigs[node.name] = (rel(p, repo), node.lineno, kw, node.args.kwarg is not None)

    hits = 0
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.keywords:
                continue
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if not fname or fname not in sigs:
                continue
            defn_file, defn_line, accepted, has_kwargs = sigs[fname]
            if has_kwargs:
                continue
            passed = {k.arg for k in node.keywords if k.arg}
            unknown = passed - accepted
            if unknown:
                hits += 1
                print("  %s:%d calls %s(%s) -- not accepted by %s:%d"
                      % (rel(p, repo), node.lineno, fname,
                         ",".join(sorted(unknown)), defn_file, defn_line))
    if hits == 0:
        print("  none found")
    print("\n  Keyword mismatches: %d" % hits)
    print("  NOTE: intra-module only. Cross-module and runtime-selected targets are")
    print("        NOT covered here -- a confirmed defect of this class lives across files.")


def plugin_a2_security(files: list[Path], repo: Path) -> None:
    section("A2-1. ROUTE / TIER COVERAGE")
    rows = []
    for p in files:
        tree, text = parse(p)
        if tree is None:
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = []
            for d in node.decorator_list:
                seg = d.func if isinstance(d, ast.Call) else d
                if isinstance(seg, ast.Attribute):
                    decs.append(seg.attr)
            if not any(m in decs for m in ("get", "post", "put", "delete", "patch")):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            span = "\n".join(lines[node.lineno - 1:end])
            m = re.search(r"_require_tier\(\s*[\"'](\w+)[\"']", span)
            path = ""
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                    path = str(d.args[0].value)
            rows.append((rel(p, repo), node.lineno, path or node.name, m.group(1) if m else "NONE"))

    for fname, line, path, tier in rows:
        flag = "  <- NO TIER CHECK IN BODY" if tier == "NONE" else ""
        print("  %-40s:%-5d %-34s tier=%-9s%s" % (fname, line, path, tier, flag))
    if not rows:
        print("  none found")
    missing = sum(1 for r in rows if r[3] == "NONE")
    print("\n  Routes: %d   without an inline _require_tier: %d" % (len(rows), missing))
    print("  NOTE: a route may be gated by middleware or a helper instead. Trace the")
    print("        ASGI chain before calling any of these a bypass.")

    section("A2-2. REQUEST DATA REACHING LOGS UNREDACTED")
    rows = []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        redacts = "redact" in text
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning", "error", "exception")):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in (
                    "body", "payload", "params", "data", "kwargs", "request", "content", "token"
                ):
                    rows.append((rel(p, repo), node.lineno, arg.id, redacts))

    for fname, line, var, red in rows:
        flag = "" if red else "  <- FILE HAS NO redact() CALL"
        print("  %-46s:%-5d logs %-10s redaction-in-file=%s%s" % (fname, line, var, red, flag))
    if not rows:
        print("  none found")
    print("\n  Log calls passing request-shaped data: %d" % len(rows))

    section("A2-3. SYNCHRONOUS I/O ON AN ASYNC PATH (direct and one hop)")

    def blocking_label(node: ast.AST):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return None
        attr = node.func.attr
        base = node.func.value
        base_id = base.id if isinstance(base, ast.Name) else ""
        if attr in ("connect", "execute", "executemany", "commit") and base_id in (
            "sqlite3", "conn", "cursor", "db"
        ):
            return "sqlite3.%s" % attr
        if attr in ("run", "check_output", "check_call") and base_id == "subprocess":
            return "subprocess.%s" % attr
        if attr in ("read_text", "write_text", "read_bytes", "write_bytes"):
            return "path.%s" % attr
        return None

    # Names that collide with builtin container/file methods. Resolving a hop by
    # bare method name would match scope.get(...) to a Store.get() and report a
    # dict lookup as blocking I/O -- an observed false positive. Excluded.
    AMBIGUOUS = {
        "get", "set", "run", "read", "write", "close", "update", "pop", "keys",
        "values", "items", "append", "extend", "copy", "clear", "send", "open",
        "add", "remove", "insert", "count", "index", "join", "split", "format",
    }

    # Pass 1: every SYNC function in the module that performs blocking I/O.
    # A sync helper that opens sqlite3 blocks the loop exactly as much as an
    # inline call does -- the confirmed defect of this class is one hop away.
    blocking_helpers: dict[str, tuple[str, int, str]] = {}
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue

        def scan_sync(node: ast.AST, cls: str, path: Path) -> None:
            for child in getattr(node, "body", []):
                if isinstance(child, ast.ClassDef):
                    scan_sync(child, child.name, path)
                elif isinstance(child, ast.FunctionDef):  # sync only
                    if child.name in AMBIGUOUS:
                        continue
                    for sub in ast.walk(child):
                        lbl = blocking_label(sub)
                        if lbl:
                            blocking_helpers[child.name] = (rel(path, repo), child.lineno, lbl)
                            break
        scan_sync(tree, "", p)

    direct, hops = [], []
    for p in files:
        tree, _ = parse(p)
        if tree is None:
            continue
        spans = enclosing_async(tree)
        text = p.read_text(encoding="utf-8", errors="replace")
        uses_tt = "to_thread" in text or "run_in_executor" in text
        for node in ast.walk(tree):
            if node.lineno not in spans if hasattr(node, "lineno") else True:
                continue
            lbl = blocking_label(node)
            if lbl:
                direct.append((rel(p, repo), node.lineno, spans[node.lineno], lbl, uses_tt))
                continue
            if isinstance(node, ast.Call):
                callee = None
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    callee = node.func.attr
                if callee and callee in blocking_helpers:
                    hfile, hline, hlbl = blocking_helpers[callee]
                    row = (rel(p, repo), node.lineno, spans[node.lineno],
                           callee, hfile, hline, hlbl, uses_tt)
                    if row not in hops:
                        hops.append(row)

    print("  Direct blocking calls inside async def:")
    for fname, line, fn, label, tt in direct:
        flag = "" if tt else "  <- FILE NEVER USES to_thread"
        print("    %-38s:%-5d in async %-24s %s%s" % (fname, line, fn, label, flag))
    if not direct:
        print("    none")

    print("\n  One hop: async caller -> sync helper that performs blocking I/O:")
    for fname, line, fn, callee, hfile, hline, hlbl, tt in hops:
        flag = "" if tt else "  <- CALLER FILE NEVER USES to_thread"
        print("    %-34s:%-5d async %-26s -> %s()%s" % (fname, line, fn, callee, flag))
        print("        helper %s:%d performs %s" % (hfile, hline, hlbl))
    if not hops:
        print("    none")

    print("\n  Blocking helpers defined in this module: %d" % len(blocking_helpers))
    print("  Direct: %d   one-hop: %d" % (len(direct), len(hops)))
    print("  NOTE: hop detection is intra-module and name-based. A helper imported")
    print("        from another package is NOT resolved here.")


def plugin_a3_architecture(files: list[Path], repo: Path, pkg: str = "menhir",
                           pkg_dir: Path | None = None) -> None:
    """Architecture lane: cycles, blast radius, responsibility proxy.

    The architecture audit's Lane A asks for circular dependencies, the most-
    imported files, and god modules. The first two are mechanical; the third is
    a judgement call the probe can only supply inputs for.
    """
    section("A3-1. IMPORT CYCLES (whole repo, not just this module)")
    graph: dict[str, set] = defaultdict(set)
    if not pkg_dir.is_dir():
        print("  WARNING: package dir %s not found -- cannot build the import graph." % pkg_dir)
        print("  A zero here would mean 'not measured', not 'no cycles'.")
        return
    for p in iter_py(pkg_dir):
        tree, _ = parse(p)
        if tree is None:
            continue
        mod = pkg + "." + p.relative_to(pkg_dir).with_suffix("").as_posix().replace("/", ".")
        mod = mod.replace(".__init__", "")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(pkg):
                graph[mod].add(node.module)

    cycles, seen, stack = [], set(), []

    def dfs(u: str) -> None:
        if u in stack:
            cycles.append(stack[stack.index(u):] + [u])
            return
        if u in seen:
            return
        seen.add(u)
        stack.append(u)
        for v in sorted(graph.get(u, ())):
            dfs(v)
        stack.pop()

    for m in sorted(graph):
        dfs(m)

    if cycles:
        for c in cycles[:20]:
            print("  " + " -> ".join(x.replace(pkg + ".", "") for x in c))
    else:
        print("  none found")
    print("\n  Import cycles: %d" % len(cycles))

    section("A3-2. BLAST RADIUS (most-imported modules repo-wide)")
    indeg: dict[str, int] = defaultdict(int)
    for _, targets in graph.items():
        for t in targets:
            indeg[t] += 1
    ranked = sorted(indeg.items(), key=lambda kv: -kv[1])[:15]
    for mod, n in ranked:
        print("  %-52s imported by %d modules" % (mod.replace(pkg + ".", ""), n))
    if not ranked:
        print("  none found")
    print("\n  A signature change in the top entries touches that many modules.")

    section("A3-3. RESPONSIBILITY PROXY (inputs for a god-module judgement)")
    print("  %-44s %6s %7s %8s %9s" % ("file", "lines", "publics", "classes", "decorated"))
    for p in files:
        tree, text = parse(p)
        if tree is None:
            continue
        publics = sum(
            1 for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
        )
        classes = sum(1 for n in tree.body if isinstance(n, ast.ClassDef))
        decorated = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.decorator_list
        )
        lines = len(text.splitlines())
        flag = "  <-" if lines > 500 else ""
        print("  %-44s %6d %7d %8d %9d%s"
              % (rel(p, repo), lines, publics, classes, decorated, flag))
    print("\n  A file is not a god module because it is long. These are INPUTS to that")
    print("  judgement -- count distinct responsibilities by reading, and cite line ranges.")


#: Audit-type plugins. Both the charter code (a1) and the readable name
#: (correctness) select the same checks -- the code matches the column headings
#: in MODULE-MAP.md, the name is what anyone reading the command will expect.
PLUGINS = {
    "a1": plugin_a1_correctness,
    "correctness": plugin_a1_correctness,
    "a2": plugin_a2_security,
    "security": plugin_a2_security,
    "a3": plugin_a3_architecture,
    "architecture": plugin_a3_architecture,
}

#: Audit types with no plugin, and why. Naming them explicitly stops a lane
#: assuming a check ran when none exists for that type.
NO_PLUGIN = {
    "a4 / maintainability": "fully covered by the core checks; no plugin needed",
    "a5 / performance": "needs a profiler this project does not configure; see AUDIT-FIT.md",
    "a6 / test-coverage": "coverage data is regenerated out of band; see AUDIT-FIT.md",
    "a7 / llm-ai": "prompt-construction review is judgement, not mechanical",
}


# --------------------------------------------------------------------------


def main() -> int:
    epilog = (
        "audit-type plugins (code or name, both work):\n"
        "  a1 | correctness    CancelledError escaping except Exception; mixed\n"
        "                      timestamp formats compared as TEXT; kwarg mismatches\n"
        "  a2 | security       route/tier coverage; request data reaching logs\n"
        "                      unredacted; sync I/O on the async path (direct + 1 hop)\n"
        "  a3 | architecture   import cycles; blast radius by in-degree; per-file\n"
        "                      responsibility inputs\n"
        "\nno plugin (the core checks still run):\n"
        + "".join("  %-22s %s\n" % (k, v) for k, v in NO_PLUGIN.items())
        + "\nThe core checks always run. See .agent/audit/PROBE-PROTOCOL.md."
    )
    ap = argparse.ArgumentParser(
        description="Menhir mechanical audit probe -- structural checks only.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("module", help="module directory, e.g. src/menhir/api")
    ap.add_argument("--type", dest="atype", default=None, choices=sorted(PLUGINS),
                    metavar="TYPE",
                    help="audit-type plugin to add to the core checks (see below)")
    ap.add_argument("--repo", default=None, help="repository root (default: inferred)")
    ap.add_argument("--no-recurse", action="store_true",
                    help="only files directly in the module dir")
    args = ap.parse_args()

    module = Path(args.module).resolve()
    if not module.is_dir():
        print("ERROR: not a directory: %s" % module, file=sys.stderr)
        return 0

    if args.repo:
        repo_root = Path(args.repo).resolve()
    else:
        repo_root = module
        while repo_root.parent != repo_root and not (repo_root / "pyproject.toml").exists():
            repo_root = repo_root.parent

    files = list(iter_py(module, recursive=not args.no_recurse))
    pkg, pkg_dir = package_root(module, repo_root)
    layers = sibling_layers(pkg_dir)

    print(SEP)
    print("MECHANICAL AUDIT PROBE")
    print(SEP)
    print("  module    : %s" % rel(module, repo_root))
    print("  repo root : %s" % repo_root)
    print("  package   : %s  (%d subpackages)" % (pkg, len(layers)))
    print("  files     : %d" % len(files))
    print("  plugin    : %s" % (args.atype or "core only"))
    print("\n  Structural checks only. Nothing here judges intent or correctness.")
    print("  Any report claim not traceable to this output must be labelled")
    print("  NOT MECHANICALLY VERIFIED.")

    check_line_reconciliation(files, repo_root)
    check_duplicate_bodies(files, repo_root)
    check_unread_constants(files, repo_root, repo_root)
    check_private_imports(files, repo_root, pkg)
    check_layering(files, repo_root, pkg, layers)
    check_dead_symbols(files, repo_root, repo_root)
    check_comment_claims(files, repo_root)

    if args.atype:
        fn = PLUGINS[args.atype]
        if fn is plugin_a3_architecture:
            fn(files, repo_root, pkg, pkg_dir)
        else:
            fn(files, repo_root)

    print("\n" + SEP)
    print("END OF PROBE OUTPUT")
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
