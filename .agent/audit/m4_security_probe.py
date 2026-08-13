#!/usr/bin/env python3
"""Standard-library-only probe for the Menhir M4 security audit.

The probe imports no Menhir package code. It reads the pinned checkout as text/AST,
executes selected source nodes in an isolated namespace for adversarial behavior tests,
and prints reproducible JSON or human-readable evidence.

Examples from a clean checkout::

    .venv/Scripts/python.exe .agent/audit/m4_security_probe.py --self-test-only
    .venv/Scripts/python.exe .agent/audit/m4_security_probe.py --root . --adversarial --json
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

SCOPE: tuple[str, ...] = (
    "src/menhir/core/backend_client_ops.py",
    "src/menhir/core/backend_protocol.py",
    "src/menhir/core/runtime.py",
    "src/menhir/core/backend_runtime_admin_ops.py",
    "src/menhir/core/backend_runtime_data_ops.py",
    "src/menhir/core/runtime_preflight.py",
    "src/menhir/core/bootstrap.py",
    "src/menhir/operator_diagnostics.py",
    "src/menhir/core/runtime_support.py",
    "src/menhir/privacy.py",
    "src/menhir/core/backend_shared.py",
    "src/menhir/core/backend_client.py",
    "src/menhir/core/request_context.py",
    "src/menhir/core/ingest_guard.py",
    "src/menhir/core/backend_runtime.py",
    "src/menhir/core/backend_impl.py",
    "src/menhir/core/__init__.py",
    "src/menhir/core/backend_config.py",
    "src/menhir/__init__.py",
    "src/menhir/main.py",
    "src/menhir/core/backend_runtime_ops.py",
    "src/menhir/core/reader_identity.py",
    "src/menhir/__main__.py",
)

EXPECTED_TOTAL = 5_097
BUILTIN_NAMES = frozenset(dir(builtins))
BACKEND_FAMILY_PREFIX = "src/menhir/core/backend_"


@dataclass(frozen=True)
class Definition:
    file: str
    parent: str
    name: str
    line: int
    end_line: int
    signature: str
    body_hash: str


@dataclass(frozen=True)
class CallableShape:
    file: str
    name: str
    line: int
    accepted: frozenset[str]
    has_var_keyword: bool


@dataclass
class FileResult:
    file: str
    logical_lines: int
    newline_bytes: int
    sha256: str
    parse_error: str | None
    duplicate_definitions: list[dict[str, object]]
    except_only_unbound_names: list[dict[str, object]]
    cancellation_candidates: list[dict[str, object]]
    timestamp_comparison_candidates: list[dict[str, object]]
    module_constants: list[dict[str, object]]


def source_line_count(text: str) -> tuple[int, int]:
    """Return logical line count and literal LF count."""
    return len(text.splitlines()), text.count("\n")


def _without_docstring(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    body = list(body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _body_dump(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    wrapper = ast.Module(body=_without_docstring(node.body), type_ignores=[])
    return ast.dump(wrapper, annotate_fields=True, include_attributes=False)


def _signature_dump(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(node.args, annotate_fields=True, include_attributes=False)


class DefinitionCollector(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.scope: list[str] = ["<module>"]
        self.definitions: list[Definition] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        body_dump = _body_dump(node)
        self.definitions.append(
            Definition(
                file=self.file,
                parent=".".join(self.scope),
                name=node.name,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=_signature_dump(node),
                body_hash=hashlib.sha256(body_dump.encode("utf-8")).hexdigest()[:16],
            )
        )


def duplicate_definitions(definitions: Sequence[Definition]) -> list[dict[str, object]]:
    """Find same-scope shadowing definitions and compare bodies, not signatures."""
    grouped: dict[tuple[str, str, str], list[Definition]] = defaultdict(list)
    for definition in definitions:
        grouped[(definition.file, definition.parent, definition.name)].append(definition)

    findings: list[dict[str, object]] = []
    for (_, parent, name), group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        findings.append(
            {
                "parent": parent,
                "name": name,
                "definitions": [
                    {
                        "line": item.line,
                        "end_line": item.end_line,
                        "body_hash": item.body_hash,
                    }
                    for item in group
                ],
                "body_relation": (
                    "same" if len({item.body_hash for item in group}) == 1 else "different"
                ),
                "signature_relation": (
                    "same" if len({item.signature for item in group}) == 1 else "different"
                ),
            }
        )
    return findings


def cross_file_backend_collisions(
    definitions: Sequence[Definition],
) -> list[dict[str, object]]:
    """Inventory same-named backend-family callables with differing bodies.

    These are candidates, not automatic findings; protocol declarations, clients, and
    runtime implementations legitimately share names. The output makes dispatch review
    explicit instead of allowing an empty same-file duplicate scan to imply absence.
    """
    grouped: dict[str, list[Definition]] = defaultdict(list)
    for item in definitions:
        if item.file.startswith(BACKEND_FAMILY_PREFIX):
            grouped[item.name].append(item)

    results: list[dict[str, object]] = []
    for name, group in sorted(grouped.items()):
        files = {item.file for item in group}
        if len(files) < 2 or len({item.body_hash for item in group}) < 2:
            continue
        results.append(
            {
                "name": name,
                "signature_relation": (
                    "same" if len({item.signature for item in group}) == 1 else "different"
                ),
                "definitions": [
                    {
                        "file": item.file,
                        "parent": item.parent,
                        "line": item.line,
                        "end_line": item.end_line,
                        "body_hash": item.body_hash,
                    }
                    for item in sorted(group, key=lambda d: (d.file, d.line))
                ],
            }
        )
    return results


def _module_bound_names(tree: ast.Module) -> set[str]:
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                    bound.add(inner.id)
    return bound


def _function_bound_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    bound: set[str] = set()
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    bound.update(arg.arg for arg in args)
    if node.args.vararg:
        bound.add(node.args.vararg.arg)
    if node.args.kwarg:
        bound.add(node.args.kwarg.arg)
    for inner in ast.walk(node):
        if inner is not node and isinstance(
            inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            bound.add(inner.name)
            continue
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Param)):
            bound.add(inner.id)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            for alias in inner.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(inner, ast.ExceptHandler) and inner.name:
            bound.add(inner.name)
    return bound


def except_only_unbound_names(tree: ast.Module) -> list[dict[str, object]]:
    """Find loads used in except handlers but unbound in the containing lexical scope."""
    module_bound = _module_bound_names(tree)
    results: list[dict[str, object]] = []

    def inspect_body(
        body: Sequence[ast.stmt],
        *,
        scope: str,
        bound: set[str],
    ) -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inspect_body(
                    statement.body,
                    scope=statement.name,
                    bound=_function_bound_names(statement),
                )
                continue
            if isinstance(statement, ast.ClassDef):
                inspect_body(statement.body, scope=statement.name, bound=set())
                continue
            for candidate in ast.walk(statement):
                if not isinstance(candidate, ast.ExceptHandler):
                    continue
                for inner in ast.walk(candidate):
                    if not isinstance(inner, ast.Name) or not isinstance(inner.ctx, ast.Load):
                        continue
                    if inner.id in BUILTIN_NAMES or inner.id in bound or inner.id in module_bound:
                        continue
                    results.append(
                        {"scope": scope, "name": inner.id, "line": inner.lineno}
                    )

    inspect_body(tree.body, scope="<module>", bound=module_bound)
    unique = {
        (item["scope"], item["name"], item["line"]): item for item in results
    }
    return [unique[key] for key in sorted(unique)]


def _handler_type_name(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare"
    return ast.unparse(handler.type) if hasattr(ast, "unparse") else ast.dump(handler.type)


def _contains_await(nodes: Sequence[ast.stmt]) -> bool:
    return any(isinstance(inner, ast.Await) for node in nodes for inner in ast.walk(node))


def cancellation_candidates(tree: ast.AST) -> list[dict[str, object]]:
    """List async try blocks that catch Exception but not CancelledError."""
    results: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not _contains_await(node.body):
            continue
        handlers = [_handler_type_name(handler) for handler in node.handlers]
        catches_exception = any(name in {"Exception", "builtins.Exception"} for name in handlers)
        if not catches_exception:
            continue
        results.append(
            {
                "line": node.lineno,
                "handlers": handlers,
                "explicit_cancelled_error": any("CancelledError" in name for name in handlers),
                "has_finally": bool(node.finalbody),
                "finally_lines": [
                    getattr(statement, "lineno", node.lineno) for statement in node.finalbody
                ],
            }
        )
    return results


TIMESTAMP_HINTS = (
    "time",
    "date",
    "stamp",
    "created",
    "updated",
    "expires",
    "expired",
    "valid",
    "since",
    "until",
    "now",
    "iso",
)


def _expr_text(node: ast.AST) -> str:
    return ast.unparse(node) if hasattr(ast, "unparse") else ast.dump(node)


def timestamp_comparison_candidates(tree: ast.AST) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        text = _expr_text(node)
        lowered = text.lower()
        if any(hint in lowered for hint in TIMESTAMP_HINTS):
            results.append({"line": node.lineno, "expression": text})
    return results


def module_constants(tree: ast.Module) -> list[dict[str, object]]:
    constants: list[dict[str, object]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                constants.append({"name": target.id, "line": target.lineno})
    return constants


def _callable_shapes(definitions_by_file: dict[str, ast.Module]) -> dict[str, list[CallableShape]]:
    shapes: dict[str, list[CallableShape]] = defaultdict(list)
    for file, tree in definitions_by_file.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = list(node.args.posonlyargs) + list(node.args.args)
            accepted = {arg.arg for arg in positional}
            accepted.update(arg.arg for arg in node.args.kwonlyargs)
            shapes[node.name].append(
                CallableShape(
                    file=file,
                    name=node.name,
                    line=node.lineno,
                    accepted=frozenset(accepted),
                    has_var_keyword=node.args.kwarg is not None,
                )
            )
    return shapes


def keyword_mismatch_candidates(
    trees: dict[str, ast.Module],
) -> list[dict[str, object]]:
    """Cross-file direct-call keyword mismatch candidates with unambiguous targets."""
    shapes = _callable_shapes(trees)
    results: list[dict[str, object]] = []
    for file, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            options = shapes.get(name, [])
            if len(options) != 1 or options[0].has_var_keyword:
                continue
            supplied = {kw.arg for kw in node.keywords if kw.arg is not None}
            unknown = sorted(supplied - options[0].accepted)
            if unknown:
                results.append(
                    {
                        "file": file,
                        "line": node.lineno,
                        "call": _expr_text(node),
                        "definition_file": options[0].file,
                        "definition_line": options[0].line,
                        "unknown_keywords": unknown,
                    }
                )
    return results


def analyze_file(root: Path, relative: str) -> tuple[FileResult, ast.Module | None, list[Definition]]:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    logical_lines, newline_bytes = source_line_count(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return (
            FileResult(
                file=relative,
                logical_lines=logical_lines,
                newline_bytes=newline_bytes,
                sha256=digest,
                parse_error=f"{exc.msg} at {exc.lineno}:{exc.offset}",
                duplicate_definitions=[],
                except_only_unbound_names=[],
                cancellation_candidates=[],
                timestamp_comparison_candidates=[],
                module_constants=[],
            ),
            None,
            [],
        )

    collector = DefinitionCollector(relative)
    collector.visit(tree)
    assert isinstance(tree, ast.Module)
    return (
        FileResult(
            file=relative,
            logical_lines=logical_lines,
            newline_bytes=newline_bytes,
            sha256=digest,
            parse_error=None,
            duplicate_definitions=duplicate_definitions(collector.definitions),
            except_only_unbound_names=except_only_unbound_names(tree),
            cancellation_candidates=cancellation_candidates(tree),
            timestamp_comparison_candidates=timestamp_comparison_candidates(tree),
            module_constants=module_constants(tree),
        ),
        tree,
        collector.definitions,
    )


def analyze(root: Path, scope: Iterable[str] = SCOPE) -> dict[str, object]:
    results: list[FileResult] = []
    missing: list[str] = []
    trees: dict[str, ast.Module] = {}
    definitions: list[Definition] = []
    global_loads: Counter[str] = Counter()

    for relative in scope:
        if not (root / relative).is_file():
            missing.append(relative)
            continue
        result, tree, file_definitions = analyze_file(root, relative)
        results.append(result)
        definitions.extend(file_definitions)
        if tree is not None:
            trees[relative] = tree
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    global_loads[node.id] += 1

    unused_constants: list[dict[str, object]] = []
    for result in results:
        for item in result.module_constants:
            if global_loads[item["name"]] == 0:
                unused_constants.append(
                    {"file": result.file, "name": item["name"], "line": item["line"]}
                )

    logical_total = sum(item.logical_lines for item in results)
    newline_total = sum(item.newline_bytes for item in results)
    return {
        "root": str(root.resolve()),
        "expected_total": EXPECTED_TOTAL,
        "logical_line_total": logical_total,
        "newline_total": newline_total,
        "logical_total_matches_expected": logical_total == EXPECTED_TOTAL,
        "newline_total_matches_expected": newline_total == EXPECTED_TOTAL,
        "missing_files": missing,
        "same_scope_duplicates": [
            {"file": item.file, **finding}
            for item in results
            for finding in item.duplicate_definitions
        ],
        "backend_cross_file_collisions": cross_file_backend_collisions(definitions),
        "except_only_unbound_names": [
            {"file": item.file, **finding}
            for item in results
            for finding in item.except_only_unbound_names
        ],
        "cancellation_candidates": [
            {"file": item.file, **finding}
            for item in results
            for finding in item.cancellation_candidates
        ],
        "timestamp_comparison_candidates": [
            {"file": item.file, **finding}
            for item in results
            for finding in item.timestamp_comparison_candidates
        ],
        "unused_module_constants": unused_constants,
        "keyword_mismatch_candidates": keyword_mismatch_candidates(trees),
        "files": [asdict(item) for item in results],
    }


def _extract_namespace(
    path: Path,
    *,
    imports: frozenset[str] = frozenset(),
    assignments: frozenset[str] = frozenset(),
    functions: frozenset[str] = frozenset(),
) -> tuple[dict[str, object], str]:
    """Compile selected exact AST nodes without importing the Menhir package."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {
                alias.name.split(".", 1)[0]
                for alias in node.names
            }
            if names & imports:
                selected.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: list[str] = []
            raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in raw_targets:
                if isinstance(target, ast.Name):
                    targets.append(target.id)
            if set(targets) & assignments:
                selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(path), "exec"), namespace, namespace)
    selected_hash = hashlib.sha256(
        ast.dump(module, include_attributes=False).encode("utf-8")
    ).hexdigest()
    return namespace, selected_hash


def run_adversarial(root: Path) -> dict[str, object]:
    privacy_path = root / "src/menhir/privacy.py"
    preflight_path = root / "src/menhir/core/runtime_preflight.py"
    missing = [str(path) for path in (privacy_path, preflight_path) if not path.is_file()]
    if missing:
        return {"missing_files": missing}

    privacy, privacy_ast_sha = _extract_namespace(
        privacy_path,
        imports=frozenset({"re"}),
        assignments=frozenset(
            {
                "MASK",
                "REDACTED_FIELDS",
                "_LOG_PREFIX",
                "_QUOTED",
                "_MIN_FREE_TEXT_LEN",
                "_IDENTIFIER_LIKE",
            }
        ),
        functions=frozenset(
            {"redact_text", "redact_mapping", "redact_rows", "_is_free_text", "redact_log_line"}
        ),
    )
    redact_mapping = privacy["redact_mapping"]
    redact_log_line = privacy["redact_log_line"]

    row = {
        "content": "Alice said \"launch Friday\" and it's confidential",
        "summary": {"secret": "nested dict value"},
        "preview": ["nested list value", {"token": "abc"}],
        "notes": 8675309,
        "name": None,
        "label": "",
        "summary_preview": "X" * 10_000,
        "Content": "case-variant secret",
        "SUMMARY": "upper-case secret",
        "uuid": "structural-uuid",
    }
    redacted = redact_mapping(row)
    log_inputs = [
        '2026-08-13 12:00:00,000 - menhir.test - INFO - content="Alice\'s confidential launch plan"',
        "2026-08-13 12:00:00,000 - menhir.test - INFO - content='Alice's confidential launch plan'",
        "2026-08-13 12:00:00,000 - menhir.test - INFO - content=Alice confidential launch plan",
        'malformed content="Alice confidential launch plan',
        '2026-08-13 12:00:00,000 - menhir.test - INFO - id="abc_def" content="a sufficiently long private phrase"',
    ]

    preflight, preflight_ast_sha = _extract_namespace(
        preflight_path,
        functions=frozenset({"_should_bypass_local_auth"}),
    )
    bypass = preflight["_should_bypass_local_auth"]
    urls = [
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://localhost.evil.example/v1",
        "http://127.0.0.1.evil.example/v1",
        "http://localhost@evil.example/v1",
        "https://localhost:8443",
        "http://[::1]:8080",
    ]

    return {
        "missing_files": [],
        "privacy_selected_ast_sha256": privacy_ast_sha,
        "preflight_selected_ast_sha256": preflight_ast_sha,
        "redaction": {
            "mapping_result": redacted,
            "oversized_input_len": 10_000,
            "oversized_output": redacted["summary_preview"],
            "reveal_returns_same_object": redact_mapping(row, reveal=True) is row,
            "log_results": [
                {"input": line, "output": redact_log_line(line)} for line in log_inputs
            ],
        },
        "local_auth_bypass": [
            {"url": url, "bypass": bool(bypass(url))} for url in urls
        ],
    }


def _write_synthetic(root: Path) -> tuple[str, str]:
    first = "first.py"
    second = "second.py"
    (root / first).write_text(
        """\
UNUSED_INVARIANT = 3
USED_INVARIANT = 4


def collide(value):
    return value + USED_INVARIANT


def collide(value):
    return value - USED_INVARIANT


def target(*, expected):
    return expected


async def cleanup_candidate(task):
    try:
        await task()
    except Exception:
        ghost_logger.exception('boom')


def compare(created_at, expires_at):
    return created_at < expires_at
""",
        encoding="utf-8",
    )
    (root / second).write_text(
        """\
from first import target


def caller():
    return target(unexpected=1)
""",
        encoding="utf-8",
    )
    return first, second


def _write_adversarial_synthetic(root: Path) -> None:
    privacy = root / "src/menhir/privacy.py"
    preflight = root / "src/menhir/core/runtime_preflight.py"
    privacy.parent.mkdir(parents=True, exist_ok=True)
    preflight.parent.mkdir(parents=True, exist_ok=True)
    privacy.write_text(
        """\
import re
MASK = '[hidden]'
REDACTED_FIELDS = frozenset({'content','summary','preview','summary_preview','notes','name','label'})
_LOG_PREFIX = re.compile(r'^(?P<prefix>\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}[.,]\\d+\\s*-\\s*[\\w.]+\\s*-\\s*\\w+\\s*-\\s*)(?P<body>.*)$', re.DOTALL)
_QUOTED = re.compile(r\"(['\\\"])(.*?)\\1\", re.DOTALL)
_MIN_FREE_TEXT_LEN = 12
_IDENTIFIER_LIKE = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_.:/-]*|\\d+|[0-9a-fA-F-]{8,})$')
def redact_text(value, *, reveal=False):
    if reveal: return value
    if isinstance(value, str) and value.strip(): return MASK
    return value
def redact_mapping(row, *, reveal=False, fields=REDACTED_FIELDS):
    if reveal: return row
    out = dict(row)
    for key in list(out.keys()):
        if key in fields: out[key] = redact_text(out[key], reveal=False)
    return out
def redact_rows(rows, *, reveal=False, fields=REDACTED_FIELDS):
    if reveal: return rows
    return [redact_mapping(r, reveal=False, fields=fields) for r in rows]
def _is_free_text(value):
    stripped = value.strip()
    if len(stripped) < _MIN_FREE_TEXT_LEN: return False
    if _IDENTIFIER_LIKE.match(stripped): return False
    return ' ' in stripped
def redact_log_line(line, *, reveal=False):
    if reveal or not line: return line
    m = _LOG_PREFIX.match(line)
    if m: prefix, body = m.group('prefix'), m.group('body')
    else: prefix, body = '', line
    def _sub(mo):
        quote, value = mo.group(1), mo.group(2)
        if _is_free_text(value): return f'{quote}{MASK}{quote}'
        return mo.group(0)
    return prefix + _QUOTED.sub(_sub, body)
""",
        encoding="utf-8",
    )
    preflight.write_text(
        """\
def _should_bypass_local_auth(base_url):
    normalized = (base_url or '').strip().lower()
    return normalized.startswith('http://127.0.0.1') or normalized.startswith('http://localhost')
""",
        encoding="utf-8",
    )


def self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="m4-security-probe-") as temp:
        root = Path(temp)
        first, second = _write_synthetic(root)
        report = analyze(root, (first, second))
        _write_adversarial_synthetic(root)
        adversarial = run_adversarial(root)
        checks = {
            "line_counter": report["logical_line_total"] == 30,
            "duplicate_body_difference": (
                len(report["same_scope_duplicates"]) == 1
                and report["same_scope_duplicates"][0]["body_relation"] == "different"
            ),
            "except_only_unbound": any(
                item["name"] == "ghost_logger"
                for item in report["except_only_unbound_names"]
            ),
            "cancelled_error_candidate": len(report["cancellation_candidates"]) == 1,
            "timestamp_comparison": len(report["timestamp_comparison_candidates"]) == 1,
            "unread_constant": any(
                item["name"] == "UNUSED_INVARIANT"
                for item in report["unused_module_constants"]
            ),
            "keyword_mismatch": any(
                "unexpected" in item["unknown_keywords"]
                for item in report["keyword_mismatch_candidates"]
            ),
            "nested_redaction_leak": (
                adversarial["redaction"]["mapping_result"]["summary"]
                == {"secret": "nested dict value"}
            ),
            "host_prefix_bypass": any(
                item["url"] == "http://localhost.evil.example/v1" and item["bypass"]
                for item in adversarial["local_auth_bypass"]
            ),
        }
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "sample": {
                "same_scope_duplicates": report["same_scope_duplicates"],
                "except_only_unbound_names": report["except_only_unbound_names"],
                "cancellation_candidates": report["cancellation_candidates"],
                "timestamp_comparison_candidates": report["timestamp_comparison_candidates"],
                "unused_module_constants": report["unused_module_constants"],
                "keyword_mismatch_candidates": report["keyword_mismatch_candidates"],
                "adversarial": adversarial,
            },
        }


def _print_human(report: dict[str, object]) -> None:
    print(f"root: {report['root']}")
    print(f"expected_total: {report['expected_total']}")
    print(f"logical_line_total: {report['logical_line_total']}")
    print(f"newline_total: {report['newline_total']}")
    print(f"missing_files: {len(report['missing_files'])}")
    for missing in report["missing_files"]:
        print(f"  MISSING {missing}")
    for key in (
        "same_scope_duplicates",
        "backend_cross_file_collisions",
        "except_only_unbound_names",
        "cancellation_candidates",
        "timestamp_comparison_candidates",
        "unused_module_constants",
        "keyword_mismatch_candidates",
    ):
        values = report[key]
        print(f"{key}: {len(values)}")
        for value in values:
            print(f"  {json.dumps(value, sort_keys=True)}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--self-test", action="store_true", help="run synthetic controls first")
    parser.add_argument(
        "--self-test-only",
        action="store_true",
        help="run synthetic controls and exit without reading the checkout",
    )
    parser.add_argument(
        "--adversarial",
        action="store_true",
        help="execute selected exact privacy/preflight source nodes against adversarial inputs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test or args.self_test_only:
        controls = self_test()
        print(json.dumps({"self_test": controls}, indent=2, sort_keys=True))
        if not controls["passed"]:
            return 2
        if args.self_test_only:
            return 0

    report = analyze(args.root)
    if args.adversarial:
        report["adversarial"] = run_adversarial(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
        if args.adversarial:
            print("adversarial:")
            print(json.dumps(report["adversarial"], indent=2, sort_keys=True))
    return 1 if report["missing_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
