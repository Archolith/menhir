"""Structural census for every production path that can create self-identity evidence.

This test is intentionally source-based. Runtime samples cannot prove that another producer did
not bypass the normal factories, while a direct context or endpoint construction could silently
create another authority producer outside the reviewed final-payload validator.
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
        self.endpoint_names = {"SelfSubjectEndpointEnvelope"}
        self.endpoint_factory_names = {"self_subject_endpoint_for_claim"}
        self.declaration_names = {"declare_self_subject"}
        self.context_calls: Counter[str] = Counter()
        self.factory_calls: Counter[str] = Counter()
        self.endpoint_calls: Counter[str] = Counter()
        self.endpoint_factory_calls: Counter[str] = Counter()
        self.declaration_calls: Counter[str] = Counter()
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
                elif imported.name == "SelfSubjectEndpointEnvelope":
                    self.endpoint_names.add(local_name)
                elif imported.name == "self_subject_endpoint_for_claim":
                    self.endpoint_factory_names.add(local_name)
                elif imported.name == "declare_self_subject":
                    self.declaration_names.add(local_name)
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
        if called in self.endpoint_names:
            self.endpoint_calls[self._site()] += 1
        if called in self.endpoint_factory_names:
            self.endpoint_factory_calls[self._site()] += 1
        if (
            called == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "declare_self_subject"
        ):
            self.declaration_calls[self._site()] += 1
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        # Count every executable reference, not just a direct call. This catches assigning an
        # imported helper to a differently named local and invoking that alias later.
        if isinstance(node.ctx, ast.Load) and node.id in self.declaration_names:
            self.declaration_calls[self._site()] += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load) and node.attr == "declare_self_subject":
            self.declaration_calls[self._site()] += 1
        if node.attr == "EXPLICIT_SELF_SUBJECT":
            self.explicit_authority_references[self._site()] += 1
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        # Catch enum-by-value/name and getattr-style constructions as well as direct attributes.
        if node.value in {"explicit_self_subject", "EXPLICIT_SELF_SUBJECT"}:
            self.explicit_authority_references[self._site()] += 1


def _production_identity_census() -> tuple[
    Counter[str], Counter[str], Counter[str], Counter[str], Counter[str], Counter[str]
]:
    constructors: Counter[str] = Counter()
    factories: Counter[str] = Counter()
    endpoint_constructors: Counter[str] = Counter()
    endpoint_factories: Counter[str] = Counter()
    declarations: Counter[str] = Counter()
    explicit_authority: Counter[str] = Counter()
    for root in _PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(_REPO).as_posix()
            visitor = _IdentityProducerVisitor(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            constructors.update(visitor.context_calls)
            factories.update(visitor.factory_calls)
            endpoint_constructors.update(visitor.endpoint_calls)
            endpoint_factories.update(visitor.endpoint_factory_calls)
            declarations.update(visitor.declaration_calls)
            explicit_authority.update(visitor.explicit_authority_references)
    return (
        constructors,
        factories,
        endpoint_constructors,
        endpoint_factories,
        declarations,
        explicit_authority,
    )


@pytest.mark.unit
def test_census_recognizes_import_aliases_and_enum_by_value():
    """The guard must not depend on the one spelling production happens to use today."""
    source = """
from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext as Context,
    SelfSubjectEndpointEnvelope as Endpoint,
    declare_self_subject as declare_subject,
    self_context_for_pending_episode as make_context,
    self_subject_endpoint_for_claim as make_endpoint,
)

def new_writer():
    ctx = make_context(source="user", namespace="default")
    make_endpoint({})
    Endpoint(version="v", episode_uuid="e", turn_evidence_uuid="t", namespace="default", marker="m")
    declare_subject(ctx, subject_node_uuid="node-1")
    return Context(
        namespace="default",
        evidence_kind=SelfEvidenceKind("explicit_self_subject"),
    )

import menhir.domain.self_identity as identity_module

def qualified_writer():
    ctx = identity_module.self_context_for_pending_episode(
        source="user", namespace="default"
    )
    identity_module.declare_self_subject(ctx, subject_node_uuid="node-2")
    return identity_module.SelfIdentityContext(
        namespace="default",
        evidence_kind=identity_module.SelfEvidenceKind.EXPLICIT_SELF_SUBJECT,
    )

def rebound_writer():
    declare = identity_module.declare_self_subject
    return declare(make_context(source="user", namespace="default"), subject_node_uuid="node-3")

def dynamic_writer():
    declare = getattr(identity_module, "declare_self_subject")
    return declare(make_context(source="user", namespace="default"), subject_node_uuid="node-4")
"""
    visitor = _IdentityProducerVisitor("src/menhir/services/new_writer.py")
    visitor.visit(ast.parse(source))

    site = "src/menhir/services/new_writer.py:new_writer"
    qualified_site = "src/menhir/services/new_writer.py:qualified_writer"
    rebound_site = "src/menhir/services/new_writer.py:rebound_writer"
    dynamic_site = "src/menhir/services/new_writer.py:dynamic_writer"
    assert visitor.context_calls == Counter({site: 1, qualified_site: 1})
    assert visitor.factory_calls == Counter(
        {site: 1, qualified_site: 1, rebound_site: 1, dynamic_site: 1}
    )
    assert visitor.endpoint_calls == Counter({site: 1})
    assert visitor.endpoint_factory_calls == Counter({site: 1})
    assert visitor.declaration_calls == Counter(
        {site: 1, qualified_site: 1, rebound_site: 1, dynamic_site: 1}
    )
    assert visitor.explicit_authority_references == Counter({site: 1, qualified_site: 1})


@pytest.mark.unit
def test_self_identity_producer_census_is_closed():
    """Any new constructor, factory caller, or explicit-authority reference requires review.

    The two context constructions are both inside the one production factory. The only factory
    caller is the Graphiti dispatch boundary. The one declaration producer is the final-payload
    validator: it can name only the receipt-owned marker node after repair and before resolution.
    Therefore no second production producer can activate binding without changing this census.
    """
    (
        constructors,
        factories,
        endpoint_constructors,
        endpoint_factories,
        declarations,
        explicit_authority,
    ) = _production_identity_census()

    assert constructors == Counter(
        {"src/menhir/domain/self_identity.py:self_context_for_pending_episode": 2}
    )
    assert factories == Counter(
        {"src/menhir/services/enrichment_steps.py:run_graphiti_extraction": 1}
    )
    assert endpoint_constructors == Counter(
        {"src/menhir/domain/self_identity.py:self_subject_endpoint_for_claim": 1}
    )
    assert endpoint_factories == Counter(
        {"src/menhir/services/enrichment_steps.py:run_graphiti_extraction": 1}
    )
    assert declarations == Counter(
        {
            "src/menhir/infrastructure/graphiti_extraction_patches.py:_declare_subject_endpoint": 1
        }
    )
    assert explicit_authority == Counter(
        {
            "src/menhir/domain/self_identity.py:SelfEvidenceKind": 1,
            "src/menhir/domain/self_identity.py:declare_self_subject": 1,
            "src/menhir/domain/self_identity.py:proves_self_subject": 1,
            "src/menhir/infrastructure/graphiti_extraction_patches.py:_record_self_binding": 1,
            "src/menhir/infrastructure/self_binding.py:bind_canonical_self": 1,
            "src/menhir/services/enrichment_steps.py:add_episode_with_timeout": 1,
        }
    )
