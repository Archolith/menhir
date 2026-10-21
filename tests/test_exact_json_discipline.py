"""Code-smell guard: Decimal-to-JSON encoding lives only in menhir.domain.exact_json.

A function that both checks for ``Decimal`` and calls ``json.dumps`` is hand-rolling the
encoding that ``dumps_exact`` owns; use ``dumps_exact`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[1] / "src" / "menhir"
_API_FILE = "domain/exact_json.py"


def _is_decimal_isinstance(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return False
    if node.func.id != "isinstance" or len(node.args) != 2:
        return False
    return any(
        isinstance(n, ast.Name) and n.id == "Decimal"
        or isinstance(n, ast.Attribute) and n.attr == "Decimal"
        for n in ast.walk(node.args[1])
    )


def _is_json_dumps(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
    )


def hand_rolled_encoders(tree: ast.Module) -> list[str]:
    """Functions (nested ones included) that both test for Decimal and call json.dumps."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = list(ast.walk(node))
            if any(map(_is_decimal_isinstance, inner)) and any(map(_is_json_dumps, inner)):
                found.append(f"{node.name}:{node.lineno}")
    return found


def test_decimal_json_encoding_goes_through_dumps_exact() -> None:
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == _API_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if hits := hand_rolled_encoders(tree):
            offenders[rel] = hits
    assert not offenders, (
        f"Encode Decimal values with menhir.domain.exact_json.dumps_exact: {offenders}"
    )


def test_guard_catches_a_planted_encoder() -> None:
    tree = ast.parse(
        "import json\n"
        "from decimal import Decimal\n"
        "def enc(v):\n"
        "    if isinstance(v, Decimal):\n"
        "        return format(v, 'f')\n"
        "    return json.dumps(v)\n"
    )
    assert hand_rolled_encoders(tree) == ["enc:3"]
