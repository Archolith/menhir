"""Regression tests for pytest-owned temporary-directory lifecycle."""

from __future__ import annotations

import ast
import stat
from pathlib import Path

from tests._temp_cleanup import remove_test_dir

TESTS_ROOT = Path(__file__).resolve().parent


def _temp_bypasses(path: Path) -> list[str]:
    """Return unmanaged scratch writers and hidden cleanup failures in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tempfile_modules: set[str] = set()
    mkdtemp_names: set[str] = set()
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tempfile":
                    tempfile_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            for alias in node.names:
                if alias.name == "mkdtemp":
                    mkdtemp_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and path.name == "conftest.py"
            and node.name == "tmp_path"
        ):
            violations.append(f"{path.name}:{node.lineno}: custom tmp_path fixture")
        elif isinstance(node, ast.Call):
            if any(
                keyword.arg == "ignore_errors"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                violations.append(
                    f"{path.name}:{node.lineno}: cleanup suppresses errors"
                )
            if isinstance(node.func, ast.Name) and node.func.id in mkdtemp_names:
                violations.append(f"{path.name}:{node.lineno}: direct mkdtemp call")
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "mkdtemp"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in tempfile_modules
            ):
                violations.append(
                    f"{path.name}:{node.lineno}: direct tempfile.mkdtemp call"
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "tempdir"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in tempfile_modules
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: global tempfile.tempdir write"
                    )

    return violations


def test_all_test_temp_directories_are_pytest_owned() -> None:
    """Scratch writers must be pytest-owned or use cleanup that reports failures."""
    violations = [
        violation
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        for violation in _temp_bypasses(path)
    ]

    assert violations == [], "test temp lifecycle bypasses:\n" + "\n".join(violations)


def test_readonly_scratch_file_is_removed(tmp_path: Path) -> None:
    """Git-style read-only objects must not strand a pytest session on Windows."""

    scratch = tmp_path / "git-objects"
    scratch.mkdir()
    git_object = scratch / "object"
    git_object.write_bytes(b"object")
    git_object.chmod(stat.S_IREAD)

    remove_test_dir(scratch)

    assert not scratch.exists()
