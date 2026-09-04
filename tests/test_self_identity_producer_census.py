"""Structural census for every production path that can create self-identity evidence.

This test is intentionally source-based. Runtime samples cannot prove that another producer did
not bypass the normal factory, while a direct ``SelfIdentityContext`` construction carrying
``EXPLICIT_SELF_SUBJECT`` would silently turn the currently inert identity rewrite on.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS = (_REPO / "src", _REPO / "scripts")


class _IdentityProducerVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.context_names = {"SelfIdentityContext"}
        self.factory_names = {"self_context_for_pending_episode"}
        self.context_calls: Counter[str] = Counter()
        self.factory_calls: Counter[str] = Counter()
        self.explicit_authority_references: Counter[str] = Counter()

    def _site(self) -> str:
        return f"{self.relative_path}:{'.'.join(self.scope) or '<module>'}"

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module == "menhir.domain.self_identity":
            for imported in node.names:
                local_name = imported.asname or imported.name
                if imported.name == "SelfIdentityContext":
                    self.context_names.add(local_name)
                elif imported.name == "self_context_for_pending_episode":
                    self.factory_names.add(local_name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        called = ""
        if isinstance(node.func, ast.Name):
            called = node.func.id
        elif isinstance(node.func, ast.Attribute):
            called = node.func.attr
        if called in self.context_names:
            self.context_calls[self._site()] += 1
        if called in self.factory_names:
            self.factory_calls[self._site()] += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr == "EXPLICIT_SELF_SUBJECT":
            self.explicit_authority_references[self._site()] += 1
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        # Catch enum-by-value/name and getattr-style constructions as well as direct attributes.
        if node.value in {"explicit_self_subject", "EXPLICIT_SELF_SUBJECT"}:
            self.explicit_authority_references[self._site()] += 1


def _production_identity_census() -> tuple[Counter[str], Counter[str], Counter[str]]:
    constructors: Counter[str] = Counter()
    factories: Counter[str] = Counter()
    explicit_authority: Counter[str] = Counter()
    for root in _PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(_REPO).as_posix()
            visitor = _IdentityProducerVisitor(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            constructors.update(visitor.context_calls)
            factories.update(visitor.factory_calls)
            explicit_authority.update(visitor.explicit_authority_references)
    return constructors, factories, explicit_authority


@pytest.mark.unit
def test_census_recognizes_import_aliases_and_enum_by_value():
    """The guard must not depend on the one spelling production happens to use today."""
    source = """
from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext as Context,
    self_context_for_pending_episode as make_context,
)

def new_writer():
    make_context(source="user", namespace="default")
    return Context(
        namespace="default",
        evidence_kind=SelfEvidenceKind("explicit_self_subject"),
    )
"""
    visitor = _IdentityProducerVisitor("src/menhir/services/new_writer.py")
    visitor.visit(ast.parse(source))

    site = "src/menhir/services/new_writer.py:new_writer"
    assert visitor.context_calls == Counter({site: 1})
    assert visitor.factory_calls == Counter({site: 1})
    assert visitor.explicit_authority_references == Counter({site: 1})


@pytest.mark.unit
def test_self_identity_producer_census_is_closed():
    """Any new constructor, factory caller, or explicit-authority reference requires review.

    The two context constructions are both inside the one production factory. The only factory
    caller is the Graphiti dispatch boundary. ``EXPLICIT_SELF_SUBJECT`` appears executably only in
    its enum declaration and in the binding predicate that rejects every other evidence kind.
    Therefore no production producer can activate binding without changing this census.
    """
    constructors, factories, explicit_authority = _production_identity_census()

    assert constructors == Counter(
        {"src/menhir/domain/self_identity.py:self_context_for_pending_episode": 2}
    )
    assert factories == Counter(
        {"src/menhir/services/enrichment_steps.py:run_graphiti_extraction": 1}
    )
    assert explicit_authority == Counter(
        {
            "src/menhir/domain/self_identity.py:SelfEvidenceKind": 1,
            "src/menhir/domain/self_identity.py:proves_self_subject": 1,
        }
    )
