"""No test module may load `.env` at import time.

A module-level `load_dotenv()` runs during collection and leaves `.env` in `os.environ` for the
whole run, so provider settings from the checkout's `.env` failed unrelated tests whenever that
module was collected. Live modules use the `live_dotenv` fixture, which restores the environment.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _module_level_dotenv_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "load_dotenv":
                    lines.append(sub.lineno)
    return lines


def test_no_test_module_calls_load_dotenv_at_import() -> None:
    offenders = {
        str(path.relative_to(TESTS_DIR)): lines
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if (lines := _module_level_dotenv_calls(path))
    }
    assert offenders == {}, f"module-level load_dotenv() leaks .env into later tests: {offenders}"


def test_the_check_sees_a_module_level_call(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from dotenv import load_dotenv\nload_dotenv()\n\ndef f():\n    load_dotenv()\n",
        encoding="utf-8",
    )
    assert _module_level_dotenv_calls(sample) == [2]
