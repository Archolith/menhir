"""L6 -- production caller composition probe (composition-boundary audit).

The composition audit's other five lanes have mechanical probes; L6 was hand-run, and its
own results file says so: *"L6 has no mechanical probe yet ... the highest-yield lane, but it
was run by hand here."* This is that probe.

**What L6 asks.** Not "is this helper correct?" but:

    Does any test enter through the production caller that CHOOSES this helper's arguments,
    or do the tests only construct ideal arguments by hand?

CF-230 is the canonical miss: `link_episode_admission` filtered correctly and had its own
green test; the production caller passed `namespace=None`, which means "do not filter". A
helper tested only with hand-built arguments cannot fail the way CF-230 failed.

**Scope is deliberately narrow.** Requiring an integration test per helper is not the claim.
The corpus is restricted to helpers that carry a consequence -- tenancy/ownership/namespace,
destructive mutation, provenance/admission/trust, scheduler/saga ownership, resource limits
and budgets, control-flow callbacks, ContextVar-derived policy, security normalization. A
formatting helper composed wrongly is a bug; a namespace helper composed wrongly is a tenant
boundary crossing.

**What this probe can and cannot decide.**

It is a recall instrument. Callee resolution is by NAME, so overloads across layers collapse
together and dynamic dispatch is invisible; test "coverage" is a name reference in a test
file, not an execution trace. It therefore over-reports, and it cannot prove a call is safe.
What it does is rank (helper, caller) pairs by how much of the L6 shape they carry, so the
hand-verification pass reads call sites in the order most likely to contain a defect.

Read the ranked pairs; decide by reading the call site. Every ARGUMENT_GAP row is a question,
not a finding: does a caller that HAS the value omit it, and is the param a filter (pinning is
right) or the target of a mutation (pinning is wrong)?

Usage:
    python scripts/audit/l6_composition_probe.py [--json OUT] [--category CAT] [--all]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "menhir"
TESTS = ROOT / "tests"

# --------------------------------------------------------------------------------------
# Corpus definition: what counts as a consequential helper.
#
# Each category is (name-or-param signal, body signal). A helper joins the corpus when its
# NAME/PARAMS match, or when its BODY match is strong enough to stand alone (Cypher guards,
# ContextVar reads, control-signal raises). Body-only matches are noisier and are marked.
# --------------------------------------------------------------------------------------

CATEGORIES: dict[str, dict[str, object]] = {
    "tenancy": {
        "params": {"namespace", "tenant", "client_id", "client_name", "group_id", "scope",
                   "project", "project_name"},
        "name": re.compile(r"namespace|tenant|scope", re.I),
        "body": re.compile(r"\$namespace|\$group_id|\$project|namespace\s*IS\s*NULL", re.I),
    },
    "ownership": {
        "params": {"worker_id", "owner", "processing_owner", "lease", "lease_id", "holder"},
        "name": re.compile(r"owner|ownership|lease|claim|heartbeat|takeover|revoke", re.I),
        "body": re.compile(r"\$worker_id|processing_owner|SagaOwnershipRevoked|_revocation", re.I),
    },
    "destructive": {
        "params": set(),
        "name": re.compile(r"^(delete|erase|purge|drop|remove|wipe|truncate|unmerge|"
                           r"rollback|restore|supersede)", re.I),
        "body": re.compile(r"DETACH\s+DELETE|\bDELETE\s+[a-z]|REMOVE\s+[a-z]", re.I),
    },
    "provenance": {
        "params": {"evidence", "admitted", "admission", "provenance", "trust", "tier"},
        "name": re.compile(r"provenance|admission|admitted|evidence|attest|trust|promote|"
                           r"demote|apex", re.I),
        "body": re.compile(r"ADMITTED_ON|EVIDENCED_BY|SUPPORTED_BY|provenance", re.I),
    },
    "saga": {
        "params": {"journal", "saga_id", "disposition"},
        "name": re.compile(r"saga|journal|prepare|commit|reconcile|compensat|quarantine", re.I),
        "body": re.compile(r"owned_mutation|mark_committed|journal\.", re.I),
    },
    "budget": {
        "params": {"budget", "max_calls", "max_tokens", "quota"},
        "name": re.compile(r"budget|quota|reserve|throttle|rate_?limit", re.I),
        "body": re.compile(r"LlmBudgetExceeded|LlmUsageControlSignal"),
    },
    "callback": {
        "params": {"callback", "on_event", "hook", "handler", "sink", "emitter"},
        "name": re.compile(r"callback|_emit|emit_|dispatch|notify|publish", re.I),
        "body": re.compile(r"callback\(|_callback\.get\(\)", re.I),
    },
    "contextvar": {
        "params": set(),
        "name": re.compile(r"^(get|current|resolve)_(request|session|tier|auth|context)", re.I),
        "body": re.compile(r"_var\.get\(\)|ContextVar|\.get\(\)\s*or\s+_default", re.I),
    },
    "normalization": {
        "params": {"raw", "user_input", "untrusted"},
        "name": re.compile(r"normali[sz]e|sanitiz|escape|redact|scrub|_safe\b|guard", re.I),
        # No body signal. `re.sub(`/`.replace(` matched 279 helpers, nearly all of them
        # formatting -- a body probe that broad buries the security-sensitive ones.
        "body": re.compile(r"(?!x)x"),
    },
}

#: Params whose ABSENCE or `None` can disable enforcement. `namespace=None` is the CF-230
#: shape exactly: the opt-in isolation contract working as designed, handed the one value
#: that turns it off.
WEAKENING_PARAMS = {
    "namespace", "tenant", "group_id", "project", "project_name", "scope",
    "worker_id", "owner", "processing_owner", "required_state", "expected_state",
    "client_id", "client_name", "budget", "limit", "callback", "revocation",
    "actor", "session", "tier", "auth_mode",
}

#: Deliberate control signals. A blanket `except Exception` between the raiser and the actor
#: turns a decision back into a fault -- CF-227, and CF-231 one frame further up.
CONTROL_SIGNALS = {"LlmUsageControlSignal", "LlmBudgetExceeded", "SagaOwnershipRevoked"}

#: Boundaries that do NOT copy the context. `asyncio.to_thread` and `create_task` do copy it;
#: a bare `threading.Thread` and a raw executor submit do not.
LOSSY_BOUNDARIES = re.compile(r"threading\.Thread\(|run_in_executor\(|"
                              r"ThreadPoolExecutor\(|\.submit\(", re.I)

SKIP_DIRS = {"__pycache__", "static", "templates"}


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


# --------------------------------------------------------------------------------------
# Pass 1b -- close the control-signal set over the call graph.
#
# "Can this callee deliver a control signal?" is a property of the call graph, not of one
# function's text. The signal originates in a callback, passes through the emitter, and the
# audit spec is explicit that every frame between the raiser and the actor must name it --
# "not just the first". A frame that shields a delivering callee behind a blanket
# `except Exception` STOPS the signal there, which is both why it does not propagate further
# and why that call site is the finding.
# --------------------------------------------------------------------------------------

def propagate_control_signals(defs: list[FuncDef]) -> None:
    by_name: dict[str, list[FuncDef]] = defaultdict(list)
    for fd in defs:
        by_name[fd.name].append(fd)

    # Propagate ONLY through names that resolve to a single definition. Callee resolution
    # here is by name, and an unbounded closure over that graph is worthless: the first run
    # of this probe attributed `SagaOwnershipRevoked` to the LLM usage emitter, because the
    # emitter calls `.get()` and one of the six unrelated `get` definitions reaches the Neo4j
    # driver. A speculative edge does not become evidence by being walked twice.
    #
    # Direct calls to an ambiguously-named raiser (`execute`) are NOT lost -- they are picked
    # up at the call site as AMBIGUOUS_CALLEE, and scored lower, because that is what they
    # are: a call that MIGHT be the raiser.
    unique = {name: fds[0] for name, fds in by_name.items() if len(fds) == 1}

    changed = True
    rounds = 0
    while changed and rounds < 20:  # the graph is shallow; the bound is a safety net
        changed = False
        rounds += 1
        for fd in defs:
            for callee in fd.callees - fd.shielded_callees:
                target = unique.get(callee)
                if target is None or target is fd:
                    continue
                gained = target.delivers - fd.delivers
                if gained:
                    fd.delivers |= gained
                    changed = True


# --------------------------------------------------------------------------------------
# Pass 2 -- classify the corpus.
# --------------------------------------------------------------------------------------

def classify(fd: FuncDef) -> None:
    # A frame that can deliver a control signal is consequential by construction, whatever
    # it is named: the whole point of the signal is to affect its caller.
    if fd.delivers:
        fd.categories.add("control_signal")
    params = set(fd.params) | set(fd.kwonly)
    for category, spec in CATEGORIES.items():
        by_param = bool(params & spec["params"])          # type: ignore[operator]
        by_name = bool(spec["name"].search(fd.name))      # type: ignore[union-attr]
        by_body = bool(spec["body"].search(fd.code_src))  # type: ignore[union-attr]
        if by_param or by_name:
            fd.categories.add(category)
        elif by_body:
            fd.categories.add(category)
            fd.body_only = True


# --------------------------------------------------------------------------------------
# Pass 3 -- what do the tests actually name?
#
# A name reference in a test file is a weak proxy for execution, and is treated as one: it
# can only ever DOWNGRADE a candidate (the tests do mention this caller, so look elsewhere
# first). It never promotes anything to safe.
# --------------------------------------------------------------------------------------

def index_tests() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = defaultdict(set)
    for path in iter_py(TESTS):
        rel = _rel(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs[node.id].add(rel)
            elif isinstance(node, ast.Attribute):
                refs[node.attr].add(rel)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # patch("...path.to.symbol") and monkeypatch string targets
                for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                    refs[part].add(rel)
    return refs


# --------------------------------------------------------------------------------------
# Pass 4 -- score (helper, caller) pairs against the L6 questions.
# --------------------------------------------------------------------------------------

@dataclass
class Pair:
    helper: FuncDef
    site: CallSite
    signals: list[str] = field(default_factory=list)
    score: int = 0
    helper_tests: set[str] = field(default_factory=set)
    caller_tests: set[str] = field(default_factory=set)


def analyse(defs: list[FuncDef], calls: list[CallSite],
            test_refs: dict[str, set[str]]) -> list[Pair]:
    by_name: dict[str, list[FuncDef]] = defaultdict(list)
    for fd in defs:
        by_name[fd.name].append(fd)

    calls_by_callee: dict[str, list[CallSite]] = defaultdict(list)
    for site in calls:
        calls_by_callee[site.callee].append(site)

    pairs: list[Pair] = []
    for name, candidates in by_name.items():
        corpus = [fd for fd in candidates if fd.categories]
        if not corpus:
            continue
        helper = max(corpus, key=lambda f: len(f.categories))
        # `link_episode_admission` has three definitions across three layers; a call to that
        # name might reach any of them. Say so rather than implying the probe resolved it.
        ambiguous = len(candidates) > 1
        weakening = ((set(helper.params) | set(helper.kwonly)) & WEAKENING_PARAMS)
        optional_weakening = weakening & helper.defaults_none

        helper_tests = test_refs.get(name, set())

        for site in calls_by_callee.get(name, []):
            # A recursive/self call inside the helper's own definition is not a caller.
            if site.file == helper.file and site.enclosing == helper.name:
                continue

            pair = Pair(helper=helper, site=site, helper_tests=helper_tests)
            pair.caller_tests = test_refs.get(site.enclosing, set())

            # Q1 -- does the caller choose an argument that can disable the helper?
            omitted = optional_weakening - site.kwargs
            # Positional args may supply the first N params; be conservative and drop them.
            if site.n_positional:
                omitted -= set(helper.params[:site.n_positional])
            has_value = {p for p in omitted if p in site.names_in_scope}
            if has_value:
                pair.signals.append(
                    f"ARGUMENT_GAP: caller omits {sorted(has_value)} but has the name in scope")
                pair.score += 4
            elif omitted:
                pair.signals.append(f"ARGUMENT_ABSENT: {sorted(omitted)} not passed")
                pair.score += 1
            if site.none_kwargs & weakening:
                pair.signals.append(
                    f"EXPLICIT_NONE: {sorted(site.none_kwargs & weakening)} passed as None")
                pair.score += 3
            if site.star_kwargs and weakening:
                pair.signals.append("STAR_KWARGS: forwarded **kwargs, not statically checkable")
                pair.score += 2

            # Q2 -- is a verdict returned and ignored?
            if site.discarded and helper.returns in {"bool", "bool | None", "Optional[bool]"}:
                pair.signals.append("DISCARDED_VERDICT: bool return dropped at a bare call")
                pair.score += 3

            # Q3 -- can a deliberate control signal cross a blanket handler here?
            if site.in_blanket_except and helper.delivers:
                if helper.seeded:
                    origin, weight = "raises/re-raises", 4
                else:
                    origin, weight = "propagates (transitive)", 2
                note = " AMBIGUOUS_CALLEE" if ambiguous else ""
                pair.signals.append(
                    f"SWALLOWED_SIGNAL:{note} callee {origin} {sorted(helper.delivers)}; "
                    f"call sits inside a blanket except Exception")
                pair.score += weight if not ambiguous else max(weight - 2, 1)

            # Q5 -- is the helper tested while the caller that composes it is not?
            if helper_tests and not pair.caller_tests:
                pair.signals.append(
                    f"CALLER_UNTESTED: helper named in {len(helper_tests)} test file(s); "
                    f"caller `{site.enclosing}` in none")
                pair.score += 3
            elif not helper_tests and not pair.caller_tests:
                pair.signals.append("BOTH_UNTESTED: neither helper nor caller named in tests")
                pair.score += 2

            if pair.signals:
                pairs.append(pair)

    pairs.sort(key=lambda p: (-p.score, p.helper.name, p.site.loc))
    return pairs


# --------------------------------------------------------------------------------------
# Pass 5 -- context boundaries (the ContextVar half of Q4, reported separately because it
# is a property of the BOUNDARY, not of any one pair).
# --------------------------------------------------------------------------------------

def context_boundaries(module_src: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for rel, source in module_src.items():
        for lineno, line in enumerate(source.splitlines(), start=1):
            if LOSSY_BOUNDARIES.search(line) and "to_thread" not in line:
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


# --------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=pathlib.Path, help="write full results as JSON")
    parser.add_argument("--category", action="append", help="restrict to these categories")
    parser.add_argument("--top", type=int, default=40, help="pairs to print (default 40)")
    parser.add_argument("--all", action="store_true", help="print every scored pair")
    args = parser.parse_args()

    defs, calls, module_src = index_source()
    propagate_control_signals(defs)
    for fd in defs:
        classify(fd)
    test_refs = index_tests()
    pairs = analyse(defs, calls, test_refs)

    if args.category:
        wanted = set(args.category)
        pairs = [p for p in pairs if p.helper.categories & wanted]

    corpus = [fd for fd in defs if fd.categories]
    print(f"L6 composition probe -- {len(defs)} defs, {len(calls)} call sites, "
          f"{len(corpus)} consequential helpers, {len(pairs)} scored pairs\n")

    counts: dict[str, int] = defaultdict(int)
    for fd in corpus:
        for category in fd.categories:
            counts[category] += 1
    print("corpus by category: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))

    signal_counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        for signal in pair.signals:
            signal_counts[signal.split(":")[0]] += 1
    print("signals: " + ", ".join(
        f"{k}={v}" for k, v in sorted(signal_counts.items(), key=lambda kv: -kv[1])) + "\n")

    shown = pairs if args.all else pairs[:args.top]
    for pair in shown:
        cats = ",".join(sorted(pair.helper.categories))
        print(f"[{pair.score:>2}] {pair.helper.name}  ({cats})")
        print(f"     callee {pair.helper.loc}")
        print(f"     caller {pair.site.loc} in `{pair.site.enclosing}`")
        for signal in pair.signals:
            print(f"       - {signal}")
        print()

    boundaries = context_boundaries(module_src)
    print(f"context boundaries that do NOT copy the context: {len(boundaries)}")
    for hit in boundaries:
        print(f"  {hit}")

    if args.json:
        payload = {
            "summary": {
                "defs": len(defs), "calls": len(calls),
                "corpus": len(corpus), "pairs": len(pairs),
                "by_category": dict(counts), "signals": dict(signal_counts),
            },
            "pairs": [
                {
                    "score": p.score,
                    "helper": p.helper.name,
                    "helper_loc": p.helper.loc,
                    "categories": sorted(p.helper.categories),
                    "body_only_match": p.helper.body_only,
                    "caller_loc": p.site.loc,
                    "caller_func": p.site.enclosing,
                    "signals": p.signals,
                    "helper_tests": sorted(p.helper_tests),
                    "caller_tests": sorted(p.caller_tests),
                }
                for p in pairs
            ],
            "lossy_context_boundaries": boundaries,
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
