"""Definition/call-site indexing for the L6 composition probe (pass 1).

Moved verbatim from ``l6_composition_probe.py``; re-exported through the parts package.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

from .corpus import CONTROL_SIGNALS, SKIP_DIRS

# One level deeper than the facade module (this is a parts subpackage), hence parents[3]
# rather than the original parents[2]; both resolve to the repository root.
ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "menhir"
TESTS = ROOT / "tests"


@dataclass
class FuncDef:
    name: str
    qualname: str
    file: str
    lineno: int
    params: list[str]
    kwonly: list[str]
    defaults_none: set[str]
    returns: str | None
    code_src: str             # unparsed body, docstring stripped: comments cannot match
    categories: set[str] = field(default_factory=set)
    body_only: bool = False
    #: Control signals this frame can deliver to ITS caller. Seeded textually, then closed
    #: transitively over the call graph -- see `propagate_control_signals`.
    delivers: set[str] = field(default_factory=set)
    seeded: set[str] = field(default_factory=set)
    callees: set[str] = field(default_factory=set)
    #: Callee names invoked inside a blanket `except Exception` in this frame.
    shielded_callees: set[str] = field(default_factory=set)
    reads_contextvar: bool = False

    @property
    def loc(self) -> str:
        return f"{self.file}:{self.lineno}"


@dataclass
class CallSite:
    callee: str
    file: str
    lineno: int
    enclosing: str            # enclosing function qualname, or "<module>"
    enclosing_line: int
    kwargs: set[str]
    n_positional: int
    none_kwargs: set[str]
    star_kwargs: bool
    discarded: bool           # bare statement-expression: any return value is dropped
    in_blanket_except: bool   # inside a try whose handler catches Exception/BaseException
    names_in_scope: set[str]  # identifiers available in the enclosing function

    @property
    def loc(self) -> str:
        return f"{self.file}:{self.lineno}"


# --------------------------------------------------------------------------------------
# Pass 1 -- index every definition and every call site in src/menhir.
# --------------------------------------------------------------------------------------

def iter_py(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _annotation(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is total on 3.12 ASTs
        return None


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """Exception names an `except` clause catches, for both `except E:` and `except (A, B):`."""
    if handler.type is None:
        return set()
    targets = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names: set[str] = set()
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class ModuleIndex(ast.NodeVisitor):
    """Collects defs and call sites, tracking the enclosing function and try/except depth."""

    def __init__(self, path: pathlib.Path, source: str) -> None:
        self.path = path
        self.rel = _rel(path)
        self.lines = source.splitlines()
        self.defs: list[FuncDef] = []
        self.calls: list[CallSite] = []
        self._stack: list[tuple[str, int]] = []
        self._blanket_depth = 0
        self._scope_names: list[set[str]] = [set()]
        self._current_def: list[FuncDef] = []

    # -- scope helpers ------------------------------------------------------------------
    def _qual(self, name: str) -> str:
        return ".".join([n for n, _ in self._stack] + [name])

    def _enclosing(self) -> tuple[str, int]:
        return self._stack[-1] if self._stack else ("<module>", 0)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append((node.name, node.lineno))
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        positional = [a.arg for a in (*args.posonlyargs, *args.args)]
        kwonly = [a.arg for a in args.kwonlyargs]

        defaults_none: set[str] = set()
        pos_defaults = args.defaults
        if pos_defaults:
            for name, default in zip(positional[-len(pos_defaults):], pos_defaults):
                if isinstance(default, ast.Constant) and default.value is None:
                    defaults_none.add(name)
        for name, default in zip(kwonly, args.kw_defaults):
            if isinstance(default, ast.Constant) and default.value is None:
                defaults_none.add(name)

        # Classify against UNPARSED code, not raw source. This file's own motivating cases
        # live in modules whose comments discuss `namespace` and `LlmUsageControlSignal` at
        # length; matching raw text scores the prose.
        body_nodes = list(node.body)
        if (body_nodes and isinstance(body_nodes[0], ast.Expr)
                and isinstance(body_nodes[0].value, ast.Constant)
                and isinstance(body_nodes[0].value.value, str)):
            body_nodes = body_nodes[1:]
        try:
            code_src = "\n".join(ast.unparse(stmt) for stmt in body_nodes)
        except Exception:  # pragma: no cover - unparse is total on 3.12 ASTs
            code_src = ""

        fd = FuncDef(
            name=node.name,
            qualname=self._qual(node.name),
            file=self.rel,
            lineno=node.lineno,
            params=[p for p in positional if p not in {"self", "cls"}],
            kwonly=kwonly,
            defaults_none=defaults_none,
            returns=_annotation(node.returns),
            code_src=code_src,
        )
        fd.seeded = self._seed_control_signals(node)
        fd.delivers = set(fd.seeded)
        fd.reads_contextvar = bool(re.search(r"_var\.get\(\)|[a-z_]+\.get\(\)\s*or\s", code_src))
        self.defs.append(fd)
        self._current_def.append(fd)

        self._stack.append((node.name, node.lineno))
        self._scope_names.append(set(positional) | set(kwonly))
        saved_blanket, self._blanket_depth = self._blanket_depth, 0
        self.generic_visit(node)
        self._blanket_depth = saved_blanket
        self._scope_names.pop()
        self._stack.pop()
        self._current_def.pop()

    @staticmethod
    def _seed_control_signals(node: ast.AST) -> set[str]:
        """Signals this frame can deliver, read off its own text.

        Two spellings, and the second is the one that matters: `raise Signal(...)` is the
        origin, but a frame that NAMES the signal in an `except` and re-raises it -- bare
        `raise` included -- is declaring that it delivers that signal onward. CF-231's
        emitter is exactly that shape, and a probe that looked only for explicit raises
        could not see it.
        """
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Raise) and child.exc is not None:
                target = child.exc
                if isinstance(target, ast.Call):
                    target = target.func
                if isinstance(target, ast.Name) and target.id in CONTROL_SIGNALS:
                    found.add(target.id)
            elif isinstance(child, ast.ExceptHandler):
                names = _handler_names(child)
                if names & CONTROL_SIGNALS and any(
                    isinstance(stmt, ast.Raise) for stmt in ast.walk(child)
                ):
                    found |= names & CONTROL_SIGNALS
        return found

    # -- assignment tracking: what values does this caller HAVE to pass? -----------------
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind(node.target)
        self.generic_visit(node)

    def _bind(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._scope_names[-1].add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind(elt)

    # -- exception isolation depth ------------------------------------------------------
    def visit_Try(self, node: ast.Try) -> None:
        blanket = any(
            h.type is None or bool(_handler_names(h) & {"Exception", "BaseException"})
            for h in node.handlers
        )
        # A handler that re-raises a named control signal before the blanket one is the
        # documented fix shape; do not count that try as swallowing.
        reraises = any(
            _handler_names(h) & CONTROL_SIGNALS
            and any(isinstance(s, ast.Raise) for s in ast.walk(h))
            for h in node.handlers
        )
        if blanket and not reraises:
            self._blanket_depth += 1
            for stmt in node.body:
                self.visit(stmt)
            self._blanket_depth -= 1
            for handler in node.handlers:
                self.visit(handler)
            for stmt in (*node.orelse, *node.finalbody):
                self.visit(stmt)
        else:
            self.generic_visit(node)

    # -- call sites ---------------------------------------------------------------------
    def visit_Expr(self, node: ast.Expr) -> None:
        value = node.value
        inner = value.value if isinstance(value, ast.Await) else value
        if isinstance(inner, ast.Call):
            self._record_call(inner, discarded=True)
            for child in ast.iter_child_nodes(inner):
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._record_call(node, discarded=False)
        self.generic_visit(node)

    def _record_call(self, node: ast.Call, *, discarded: bool) -> None:
        callee = _callee_name(node)
        if callee is None:
            return
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        none_kwargs = {
            kw.arg for kw in node.keywords
            if kw.arg and isinstance(kw.value, ast.Constant) and kw.value.value is None
        }
        enclosing, enclosing_line = self._enclosing()
        if self._current_def:
            frame = self._current_def[-1]
            frame.callees.add(callee)
            if self._blanket_depth > 0:
                frame.shielded_callees.add(callee)
        self.calls.append(CallSite(
            callee=callee,
            file=self.rel,
            lineno=node.lineno,
            enclosing=enclosing,
            enclosing_line=enclosing_line,
            kwargs=kwargs,
            n_positional=len(node.args),
            none_kwargs=none_kwargs,
            star_kwargs=any(kw.arg is None for kw in node.keywords),
            discarded=discarded,
            in_blanket_except=self._blanket_depth > 0,
            names_in_scope=set(self._scope_names[-1]),
        ))


def index_source() -> tuple[list[FuncDef], list[CallSite], dict[str, str]]:
    defs: list[FuncDef] = []
    calls: list[CallSite] = []
    module_src: dict[str, str] = {}
    for path in iter_py(SRC):
        source = path.read_text(encoding="utf-8")
        module_src[_rel(path)] = source
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover - repo must parse
            print(f"skip (syntax): {path}: {exc}", file=sys.stderr)
            continue
        index = ModuleIndex(path, source)
        index.visit(tree)
        defs.extend(index.defs)
        calls.extend(index.calls)
    return defs, calls, module_src
