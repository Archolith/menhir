"""Symbol signatures must not carry credentials from default values into the structure graph.

Signatures were stored via `ast.unparse` with their defaults, and only document content was
scrubbed, so `def connect(password: str = "...")` was readable by every structure caller. The
response-level scrubber misses most such defaults (annotated parameters, bare hex keys, tuples),
so the scanner now elides text literals, and the writer scrubs signatures from other sources.
"""

from __future__ import annotations

import ast

import pytest

from menhir.infrastructure import structure_queries
from menhir.infrastructure.project_scanner import _build_signature

pytestmark = pytest.mark.unit

_SOURCE = '''
def connect(host="db", password: str = "Xk9#mP2qLw7z", *, key=b"9f86d081884c7d65",
            auth=("admin", "S3cretPass!"), token=f"Bearer {TOKEN}", limit: int = 20,
            ratio=0.5, flag=None, mode: Literal["a", "b"] = "a") -> "Conn":
    pass

async def fetch(self, url, retries=3): ...
'''


def _sig(name: str) -> str:
    tree = ast.parse(_SOURCE)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return _build_signature(node)


def test_text_defaults_are_elided() -> None:
    sig = _sig("connect")
    for secret in ("Xk9#mP2qLw7z", "9f86d081884c7d65", "S3cretPass!", "Bearer", "admin", '"db"'):
        assert secret not in sig, sig
    assert "password: str=..." in sig
    assert "auth=(..., ...)" in sig


def test_non_text_defaults_and_annotations_are_kept() -> None:
    sig = _sig("connect")
    assert "limit: int=20" in sig
    assert "ratio=0.5" in sig
    assert "flag=None" in sig
    # Annotations (including string forward references and Literal values) are not defaults.
    assert "Literal['a', 'b']" in sig
    assert "-> 'Conn'" in sig
    assert _sig("fetch") == "async def fetch(self, url, retries=3)"


def test_writer_scrubs_signatures_that_did_not_come_from_the_scanner(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)
    out = structure_queries._scrub_signature("def f(api_key='sk-proj-AbCdEfGhIjKlMnOpQrStUvWx')")
    assert "sk-proj" not in out
    assert "REDACTED" in out


def test_writer_skips_the_detector_when_no_literal_is_left(monkeypatch) -> None:
    def _boom(text: str):  # pragma: no cover - must not be called
        raise AssertionError("detector called for a signature with no string literal")

    monkeypatch.setattr(structure_queries, "redact", _boom)
    sig = "def f(password: str = ..., limit: int = 20)"
    assert structure_queries._scrub_signature(sig) == sig
