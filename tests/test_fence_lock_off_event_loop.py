"""Sync fence-lock writers stay off the event loop (release PR #599 stdio E2E hang; #602).

The live race is in test_fence_lock_off_event_loop_online.py. Here: the runtime guard logs only
on a loop thread, the sync Neo4j wrapper runs it for every fence-locking statement, and an AST
census refuses an async function that reaches a fence-lock writer without ``asyncio.to_thread``.

The writer set is derived from the Cypher, not listed by hand: a function is a writer when a
string it builds (or a module constant it uses) writes an ``EvidenceNamespaceFence`` per
``is_fence_lock_query``, and so is every sync function that calls a writer, transitively.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from menhir.infrastructure.erasure_fence import (
    FENCE_LOCK_ON_EVENT_LOOP,
    fence_lock_cypher,
    is_fence_lock_query,
    warn_if_on_event_loop,
)
from menhir.infrastructure.neo4j import Neo4jTransaction

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (ROOT / "src" / "menhir", ROOT / "scripts")

#: The hand list #600 shipped. The derived census must still contain every one of them.
LEGACY_FENCE_LOCK_WRITERS = frozenset({
    "seal_memory", "seal_namespace", "release_memory", "release_orphaned_erasing_ops",
    "run_fenced", "run_fenced_call", "execute_copy_fenced",
    "create_evidence_projection", "ensure_self_entity", "create_raw_capture_entity",
    "compress_node", "complete_rehydration", "resolve_conflict_group",
    "_fenced_write", "_record_retention_fenced",
})

#: Writers #602 found missing from that list.
ISSUE_602_WRITERS = frozenset({
    "record_turn_evidence", "purge_namespace", "_write_version", "_refresh_fact_provenance",
    "_replace_fact_provenance", "_delete_memory_with_scalar_cascade_in_namespace",
    "delete_namespace_with_scalar_cascade", "begin", "finalize_remote_outcome",
    "record_admission_audit", "record_counter", "record_scalar_state", "record_timeline",
})

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
#: Build the lock idiom as a string; they execute nothing.
_STRING_BUILDERS = frozenset({"fence_lock_cypher"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _string_text(expr: ast.AST) -> str:
    """Every string literal in ``expr``, joined; a ``fence_lock_cypher`` call contributes its idiom."""
    parts: list[str] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)
        elif isinstance(node, ast.Call) and _call_name(node) == "fence_lock_cypher":
            parts.append(fence_lock_cypher("$k"))
    return " ".join(parts)


def _expr_locks(expr: ast.AST, flagged_names: set[str]) -> bool:
    if is_fence_lock_query(_string_text(expr)):
        return True
    return any(_ref_name(n) in flagged_names for n in ast.walk(expr))


class _Corpus:
    def __init__(self) -> None:
        self.trees: dict[Path, ast.Module] = {}
        for base in SCAN_DIRS:
            for path in sorted(base.rglob("*.py")):
                self.trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        self.flagged_constants = self._flag_constants()
        self.writers = self._derive_writers()

    def _module_assignments(self) -> list[tuple[str, ast.AST]]:
        out: list[tuple[str, ast.AST]] = []
        for tree in self.trees.values():
            for stmt in tree.body:
                targets: list[ast.AST] = []
                value: ast.AST | None = None
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                for target in targets:
                    if isinstance(target, ast.Name) and value is not None:
                        out.append((target.id, value))
        return out

    def _flag_constants(self) -> set[str]:
        assignments = self._module_assignments()
        flagged: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, value in assignments:
                if name not in flagged and _expr_locks(value, flagged):
                    flagged.add(name)
                    changed = True
        return flagged

    def functions(self) -> list[tuple[Path, str | None, ast.FunctionDef | ast.AsyncFunctionDef]]:
        """Module-level functions and methods (nested defs belong to their enclosing function)."""
        out = []
        for path, tree in self.trees.items():
            for stmt in tree.body:
                if isinstance(stmt, _FUNCS):
                    out.append((path, None, stmt))
                elif isinstance(stmt, ast.ClassDef):
                    out.extend((path, stmt.name, s) for s in stmt.body if isinstance(s, _FUNCS))
        return out

    def _index(self) -> None:
        self.by_id = {(p, c, f.name): f for p, c, f in self.functions()}
        self.by_name: dict[str, list[tuple[Path, str | None, str]]] = {}
        for ident in self.by_id:
            self.by_name.setdefault(ident[2], []).append(ident)
        self.classes: dict[str, list[tuple[Path, ast.ClassDef]]] = {}
        #: (path, class) -> {attribute: class name} from ``self.attr = ClassName(...)``.
        self.attr_types: dict[tuple[Path, str], dict[str, str]] = {}
        #: attribute name -> class names named in a class-body annotation of that attribute.
        self.annotated: dict[str, set[str]] = {}
        for path, tree in self.trees.items():
            for stmt in tree.body:
                if not isinstance(stmt, ast.ClassDef):
                    continue
                self.classes.setdefault(stmt.name, []).append((path, stmt))
                types = self.attr_types.setdefault((path, stmt.name), {})
                for field in stmt.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        for ref in ast.walk(field.annotation):
                            ann = _ref_name(ref) or (
                                ref.value if isinstance(ref, ast.Constant)
                                and isinstance(ref.value, str) else None)
                            for part in str(ann or "").replace("|", " ").split():
                                self.annotated.setdefault(field.target.id, set()).add(part)
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                        ctor = _call_name(node.value)
                        for target in node.targets:
                            if (isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self" and ctor):
                                types[target.attr] = ctor

    def _method(self, cls_name: str, name: str, depth: int = 0) -> list[tuple]:
        """``cls_name.name`` looked up through same-named classes and their bases."""
        found: list[tuple] = []
        for path, node in self.classes.get(cls_name, []):
            if (path, node.name, name) in self.by_id:
                found.append((path, node.name, name))
            elif depth < 8:
                for base in node.bases:
                    base_name = _ref_name(base)
                    if base_name:
                        found.extend(self._method(base_name, name, depth + 1))
        return found

    def _subclasses(self, cls_name: str) -> list[str]:
        out: list[str] = []
        frontier = [cls_name]
        while frontier:
            parent = frontier.pop()
            for name, defs in self.classes.items():
                if name in out or name == cls_name:
                    continue
                if any(_ref_name(b) == parent for _, node in defs for b in node.bases):
                    out.append(name)
                    frontier.append(name)
        return out

    def resolve(self, call: ast.Call, path: Path, cls: str | None) -> list[tuple]:
        """Definitions ``call`` can reach. Unresolvable calls return [] (the runtime guard covers them)."""
        name = _call_name(call)
        if not name or (name.startswith("__") and name.endswith("__")):
            return []
        func = call.func
        if isinstance(func, ast.Attribute) and cls is not None:
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
                own = self._method(cls, name)
                if not own:
                    # A mixin calling a sibling mixin's method: look it up in every class
                    # that combines this one.
                    for sub in self._subclasses(cls):
                        own.extend(self._method(sub, name))
                if own:
                    return own
            if (isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "self"):
                typed = self.attr_types.get((path, cls), {}).get(receiver.attr)
                if typed:
                    hits = self._method(typed, name)
                    if hits:
                        return hits
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            # ``<obj>.attr.name()`` where some class annotates ``attr`` with a known class.
            hits = [h for typed in sorted(self.annotated.get(func.value.attr, ()))
                    if typed in self.classes for h in self._method(typed, name)]
            if hits:
                return hits
        if isinstance(func, ast.Name) and (path, None, name) in self.by_id:
            return [(path, None, name)]
        return list(self.by_name.get(name, []))

    def _calls_writer(self, call: ast.Call, path: Path, cls: str | None, writers: set) -> bool:
        """A call reaches a writer when every definition it can resolve to is one.

        ``self.x()`` and ``self.attr.x()`` resolve through the class (and the attribute's
        constructor); other calls fall back to every same-named definition, which counts only
        when all of them are writers. What stays unresolved is covered by the runtime guard.
        """
        targets = self.resolve(call, path, cls)
        return bool(targets) and all(t in writers for t in targets)

    def _derive_writers(self) -> set[str]:
        self._index()
        sync = [(p, c, f) for p, c, f in self.functions()
                if isinstance(f, ast.FunctionDef) and f.name not in _STRING_BUILDERS]
        writers = {
            (p, c, f.name) for p, c, f in sync
            if any(_expr_locks(n, self.flagged_constants) for n in ast.walk(f)
                   if isinstance(n, ast.expr))
        }
        changed = True
        while changed:
            changed = False
            for p, c, f in sync:
                ident = (p, c, f.name)
                if ident in writers:
                    continue
                if any(isinstance(n, ast.Call) and self._calls_writer(n, p, c, writers)
                       for n in ast.walk(f)):
                    writers.add(ident)
                    changed = True
        self.writer_ids = writers
        return {ident[2] for ident in writers}


@pytest.fixture(scope="module")
def corpus() -> _Corpus:
    return _Corpus()


def _on_loop_calls(
    fn: ast.AsyncFunctionDef, corpus: "_Corpus", path: Path, cls: str | None
) -> list[tuple[int, str]]:
    """Writer calls that run on the loop: in ``fn``'s own body, or in a local sync def it calls."""
    local_defs = {
        n.name: n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn
    }
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPES):
            continue
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            # An awaited call is async (e.g. the backend's off-loop wrapper of the same name).
            # Its arguments are still checked.
            stack.extend(node.value.args)
            stack.extend(kw.value for kw in node.value.keywords)
            continue
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if corpus._calls_writer(node, path, cls, corpus.writer_ids):
                found.append((node.lineno, str(name)))
            elif isinstance(node.func, ast.Name) and name in local_defs and name not in seen:
                seen.add(name)
                stack.extend(local_defs[name].body)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _async_functions(tree: ast.Module) -> list[tuple[str | None, ast.AsyncFunctionDef]]:
    """Every async def, nested ones included, with its enclosing class name."""
    out: list[tuple[str | None, ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, cls: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.AsyncFunctionDef):
                out.append((cls, child))
            visit(child, cls)

    visit(tree, None)
    return out


def test_guard_logs_on_the_loop_only(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="menhir.infrastructure.erasure_fence")
    warn_if_on_event_loop("off_loop")
    assert FENCE_LOCK_ON_EVENT_LOOP not in caplog.text

    async def on_loop() -> None:
        warn_if_on_event_loop("on_loop")
        await asyncio.to_thread(warn_if_on_event_loop, "in_thread")

    asyncio.run(on_loop())
    assert f"{FENCE_LOCK_ON_EVENT_LOOP} op=on_loop" in caplog.text
    assert "op=in_thread" not in caplog.text


@pytest.mark.parametrize(
    ("query", "locks"),
    [
        (fence_lock_cypher("$key"), True),
        ("MERGE (f:EvidenceNamespaceFence {namespace_key: $k}) RETURN f", True),
        (("MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) "
         "SET f.lock_nonce = $n RETURN f"), True),
        (("MATCH (f:EvidenceNamespaceFence) WHERE $op IN f.erasing_ops "
         "SET f.erasing_ops = [o IN f.erasing_ops WHERE o <> $op]"), True),
        (("OPTIONAL MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) "
         "RETURN coalesce(f.erasure_epoch, 0) AS epoch"), False),
        ("MATCH (n:Episodic) SET n.lock_nonce = $n", False),
    ],
)
def test_fence_lock_query_predicate(query: str, locks: bool) -> None:
    assert is_fence_lock_query(query) is locks


def test_sync_transaction_execute_guards_fence_locks(caplog) -> None:
    """A fence-locking statement through the sync wrapper logs on the loop, whoever issues it."""
    caplog.set_level(logging.ERROR, logger="menhir.infrastructure.erasure_fence")

    class _Tx:
        def run(self, query, **params):
            return []

    tx = Neo4jTransaction(_Tx())

    async def on_loop() -> None:
        tx.execute("OPTIONAL MATCH (f:EvidenceNamespaceFence) RETURN f.erasure_epoch")
        assert FENCE_LOCK_ON_EVENT_LOOP not in caplog.text
        tx.execute(fence_lock_cypher("$key"), {"key": "k", "fence_nonce": "n"})

    asyncio.run(on_loop())
    assert f"{FENCE_LOCK_ON_EVENT_LOOP} op=neo4j_execute_fence_lock" in caplog.text


def test_census_derives_every_known_writer(corpus: _Corpus) -> None:
    """The derived set covers #600's hand list and every writer #602 found missing."""
    missing = sorted((LEGACY_FENCE_LOCK_WRITERS | ISSUE_602_WRITERS) - corpus.writers)
    assert not missing, missing


def test_census_follows_writers_transitively() -> None:
    src = (
        "Q = 'MERGE (f:EvidenceNamespaceFence {namespace_key: $k})'\n"
        "def inner(neo4j):\n    neo4j.execute(Q)\n"
        "def outer(neo4j):\n    inner(neo4j)\n"
        "async def handler(neo4j):\n"
        "    def work():\n        outer(neo4j)\n"
        "    work()\n"
    )
    corpus = _Corpus.__new__(_Corpus)
    corpus.trees = {Path("fixture.py"): ast.parse(src)}
    corpus.flagged_constants = corpus._flag_constants()
    corpus.writers = corpus._derive_writers()
    assert {"inner", "outer"} <= corpus.writers
    handler = corpus.trees[Path("fixture.py")].body[-1]
    calls = _on_loop_calls(handler, corpus, Path("fixture.py"), None)
    assert [name for _, name in calls] == ["outer"]


def test_census_catches_the_602_on_loop_publication_intent(corpus: _Corpus) -> None:
    """Moving ``begin`` back onto the loop (the #602 example) must fail the census."""
    path = ROOT / "src" / "menhir" / "services" / "enrichment_steps.py"
    fixed = path.read_text(encoding="utf-8")
    on_loop = fixed.replace(
        "await asyncio.to_thread(\n                    ctx.evidence_publication_intents.begin,",
        "ctx.evidence_publication_intents.begin(",
    )
    assert on_loop != fixed, "fixture text drifted; update the replacement"
    tree = ast.parse(on_loop)
    hits = [
        name
        for cls, fn in _async_functions(tree)
        for _, name in _on_loop_calls(fn, corpus, path, cls)
    ]
    assert "begin" in hits


def test_no_async_function_reaches_a_fence_lock_writer_on_the_loop(corpus: _Corpus) -> None:
    """Fails if an async function calls a sync fence-lock writer on the loop thread."""
    violations = []
    for path, tree in corpus.trees.items():
        for cls, node in _async_functions(tree):
            for line, name in _on_loop_calls(node, corpus, path, cls):
                violations.append(f"{path.relative_to(ROOT)}:{line} {node.name} -> {name}")
    assert not violations, "\n".join(sorted(violations))
