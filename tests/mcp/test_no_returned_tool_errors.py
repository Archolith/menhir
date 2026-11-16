"""#669 guard: a tool fails by raising McpToolRefusal, never by returning an error it built.

A returned `{"ok": False, ...}` or `"Error: ..."` reaches the client with isError unset, which is
the inconsistency #669 removed. BaseTool.error_result is the one place that builds the envelope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import menhir.mcp.tools as tools_pkg

pytestmark = [pytest.mark.unit]

_TOOLS = Path(tools_pkg.__file__).parent


def _leading_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "ok"
                    and isinstance(value, ast.Constant)
                    and value.value is False
                ):
                    found.append(f"{path.name}:{node.lineno} builds an ok=False envelope")
        if isinstance(node, ast.Return) and node.value is not None:
            text = _leading_text(node.value)
            if text is not None and text.startswith("Error"):
                found.append(f"{path.name}:{node.lineno} returns an error string")
    return found


def test_no_tool_returns_an_error_it_built() -> None:
    files = sorted(_TOOLS.rglob("*.py"))
    assert len(files) > 50, "tool tree not found"
    violations = [v for f in files for v in _violations(f)]
    assert not violations, "raise McpToolRefusal instead:\n" + "\n".join(violations)


def test_the_guard_catches_both_shapes(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text(
        "def a():\n    return {'ok': False, 'error': {}}\n"
        "def b(e):\n    return f'Error: {e}'\n",
        encoding="utf-8",
    )
    assert len(_violations(sample)) == 2
