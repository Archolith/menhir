#!/usr/bin/env python3
"""Standard-library-only static probe for the Menhir M4 security audit.

The probe imports no Menhir package code. It reads Python source as text/AST,
executes built-in synthetic control cases, and prints reproducible evidence.
Run from a clean checkout:

    python .agent/audit/m4_security_probe.py --self-test
    python .agent/audit/m4_security_probe.py --root .
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


@dataclass(frozen=True)
class Definition:
    file: str
    parent: str
    name: str
    qualname: str
    line: int
    end_line: int
    signature: str
    body_hash: str
    body_dump: str


@dataclass
class FileResult:
    file: str
    logical_lines: int
    newline_bytes: int
    parse_error: str | None
    duplicate_definitions: list[dict[str, object]]
    except_only_unbound_names: list[dict[str, object]]
    broad_exception_candidates: list[dict[str, object]]
    timestamp_comparison_candidates: list[dict[str, object]]
    unread_module_constants: list[dict[str, object]]
    keyword_mismatch_candidates: list[dict[str, object]]


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
        parent = ".".join(self.scope)
        qualname = f"{parent}.{node.name}"
        body_dump = _body_dump(node)
        self.definitions.append(
            Definition(
                file=self.file,
                parent=parent,
                name=node.name,
                qualname=qualname,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=_signature_dump(node),
                body_hash=hashlib.sha256(body_dump.encode("utf-8")).hexdigest()[:16],
                body_dump=body_dump,
            )
        )


def duplicate_definitions(definitions: Sequence[Definition]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[Definition]] = defaultdict(list)
    for definition in definitions:
        grouped[(definition.file, definition.parent, definition.name)].append(definition)

    findings: list[dict[str, object]] = []
    for (_, parent, name), group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        bodies = {item.body_hash for item in group}
        signatures = {item.signature for item in group}
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
                "body_relation": "same" if len(bodies) == 1 else "different",
                "signature_relation": "same" if len(signatures) == 1 else "different",
            }
        )
    return findings


def _bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = (
                    list(node.args.posonlyargs)
                    + list(node.args.args)
                    + list(node.args.kwonlyargs)
                )
                bound.update(arg.arg for arg in args)
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def except_only_unbound_names(tree: ast.AST) -> list[dict[str, object]]:
    all_bound = _bound_names(tree)
    all_load_lines: dict[str, list[int]] = defaultdict(list)
    except_load_lines: dict[str, list[int]] = defaultdict(list)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            all_load_lines[node.id].append(node.lineno)
        if isinstance(node, ast.ExceptHandler):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                    except_load_lines[inner.id].append(inner.lineno)

    results: list[dict[str, object]] = []
    for name, lines in sorted(except_load_lines.items()):
        if name in BUILTIN_NAMES or name in all_bound:
            continue
        outside_count = len(all_load_lines[name]) - len(lines)
        if outside_count == 0:
            results.append({"name": name, "lines": sorted(set(lines))})
    return results


def _handler_type_name(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare"
    return ast.unparse(handler.type) if hasattr(ast, "unparse") else ast.dump(handler.type)


def broad_exception_candidates(tree: ast.AST) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handler_names = [_handler_type_name(handler) for handler in node.handlers]
        catches_exception = any(
            name in {"Exception", "builtins.Exception"} for name in handler_names
        )
        if not catches_exception:
            continue
        explicit_cancel = any("CancelledError" in name for name in handler_names)
        cleanup_calls: list[str] = []
        for statement in list(node.finalbody) + list(node.orelse):
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call):
                    cleanup_calls.append(
                        ast.unparse(inner.func) if hasattr(ast, "unparse") else ast.dump(inner.func)
                    )
        results.append(
            {
                "line": node.lineno,
                "handlers": handler_names,
                "explicit_cancelled_error": explicit_cancel,
                "has_finally": bool(node.finalbody),
                "cleanup_calls_after_try": sorted(set(cleanup_calls)),
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


def unread_module_constants(tree: ast.Module) -> list[dict[str, object]]:
    constants: dict[str, int] = {}
    load_counts: Counter[str] = Counter()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                constants[target.id] = target.lineno
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            load_counts[node.id] += 1
    return [
        {"name": name, "line": line}
        for name, line in sorted(constants.items(), key=lambda item: item[1])
        if load_counts[name] == 0
    ]


@dataclass(frozen=True)
class CallableShape:
    name: str
    line: int
    accepted: frozenset[str]
    has_var_keyword: bool


def _callable_shapes(tree: ast.AST) -> dict[str, list[CallableShape]]:
    shapes: dict[str, list[CallableShape]] = defaultdict(list)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = list(node.args.posonlyargs) + list(node.args.args)
        accepted = {arg.arg for arg in positional}
        accepted.update(arg.arg for arg in node.args.kwonlyargs)
        shapes[node.name].append(
            CallableShape(
                name=node.name,
                line=node.lineno,
                accepted=frozenset(accepted),
                has_var_keyword=node.args.kwarg is not None,
            )
        )
    return shapes


def keyword_mismatch_candidates(tree: ast.AST) -> list[dict[str, object]]:
    shapes = _callable_shapes(tree)
    results: list[dict[str, object]] = []
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
                    "line": node.lineno,
                    "call": _expr_text(node),
                    "definition_line": options[0].line,
                    "unknown_keywords": unknown,
                }
            )
    return results


def analyze_file(root: Path, relative: str) -> FileResult:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    logical_lines, newline_bytes = source_line_count(text)
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return FileResult(
            file=relative,
            logical_lines=logical_lines,
            newline_bytes=newline_bytes,
            parse_error=f"{exc.msg} at {exc.lineno}:{exc.offset}",
            duplicate_definitions=[],
            except_only_unbound_names=[],
            broad_exception_candidates=[],
            timestamp_comparison_candidates=[],
            unread_module_constants=[],
            keyword_mismatch_candidates=[],
        )

    collector = DefinitionCollector(relative)
    collector.visit(tree)
    assert isinstance(tree, ast.Module)
    return FileResult(
        file=relative,
        logical_lines=logical_lines,
        newline_bytes=newline_bytes,
        parse_error=None,
        duplicate_definitions=duplicate_definitions(collector.definitions),
        except_only_unbound_names=except_only_unbound_names(tree),
        broad_exception_candidates=broad_exception_candidates(tree),
        timestamp_comparison_candidates=timestamp_comparison_candidates(tree),
        unread_module_constants=unread_module_constants(tree),
        keyword_mismatch_candidates=keyword_mismatch_candidates(tree),
    )


def analyze(root: Path, scope: Iterable[str] = SCOPE) -> dict[str, object]:
    results: list[FileResult] = []
    missing: list[str] = []
    for relative in scope:
        if not (root / relative).is_file():
            missing.append(relative)
            continue
        results.append(analyze_file(root, relative))

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
        "files": [asdict(item) for item in results],
    }


def _write_synthetic(root: Path) -> str:
    relative = "synthetic.py"
    (root / relative).write_text(
        """\
UNUSED_INVARIANT = 3
USED_INVARIANT = 4


def collide(value):
    return value + USED_INVARIANT


def collide(value):
    return value - USED_INVARIANT


def target(*, expected):
    return expected


def caller():
    try:
        target(unexpected=1)
    except Exception:
        ghost_logger.exception('boom')


def compare(created_at, expires_at):
    return created_at < expires_at
""",
        encoding="utf-8",
    )
    return relative


def self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="m4-security-probe-") as temp:
        root = Path(temp)
        relative = _write_synthetic(root)
        result = analyze(root, (relative,))
        file_result = result["files"][0]

        checks = {
            "line_counter": file_result["logical_lines"] == 25,
            "duplicate_body_difference": (
                len(file_result["duplicate_definitions"]) == 1
                and file_result["duplicate_definitions"][0]["body_relation"] == "different"
            ),
            "except_only_unbound": any(
                item["name"] == "ghost_logger"
                for item in file_result["except_only_unbound_names"]
            ),
            "broad_exception": len(file_result["broad_exception_candidates"]) == 1,
            "timestamp_comparison": len(file_result["timestamp_comparison_candidates"]) == 1,
            "unread_constant": any(
                item["name"] == "UNUSED_INVARIANT"
                for item in file_result["unread_module_constants"]
            ),
            "keyword_mismatch": any(
                "unexpected" in item["unknown_keywords"]
                for item in file_result["keyword_mismatch_candidates"]
            ),
        }
        return {"passed": all(checks.values()), "checks": checks, "sample": file_result}


def _print_human(report: dict[str, object]) -> None:
    print(f"root: {report['root']}")
    print(f"expected_total: {report['expected_total']}")
    print(f"logical_line_total: {report['logical_line_total']}")
    print(f"newline_total: {report['newline_total']}")
    print(f"missing_files: {len(report['missing_files'])}")
    for missing in report["missing_files"]:
        print(f"  MISSING {missing}")
    for item in report["files"]:
        print(f"\n[{item['file']}] lines={item['logical_lines']} parse_error={item['parse_error']}")
        for key in (
            "duplicate_definitions",
            "except_only_unbound_names",
            "broad_exception_candidates",
            "timestamp_comparison_candidates",
            "unread_module_constants",
            "keyword_mismatch_candidates",
        ):
            values = item[key]
            print(f"  {key}: {len(values)}")
            for value in values:
                print(f"    {json.dumps(value, sort_keys=True)}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--self-test", action="store_true", help="run synthetic controls first")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        controls = self_test()
        print(json.dumps({"self_test": controls}, indent=2, sort_keys=True))
        if not controls["passed"]:
            return 2
    report = analyze(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 1 if report["missing_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
