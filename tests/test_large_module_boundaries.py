"""Architecture checks for the large-module decomposition stack."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.mark.unit
def test_typed_scalar_facade_only_reexports_focused_owners() -> None:
    path = ROOT / "src/menhir/services/typed_scalar_perception.py"
    tree = _tree("src/menhir/services/typed_scalar_perception.py")

    assert len(path.read_text(encoding="utf-8").splitlines()) <= 80
    assert not [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    owners = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert owners == {
        "menhir.services.typed_scalar_persistence",
        "menhir.services.typed_scalar_rules",
        "menhir.services.typed_scalar_service",
    }


@pytest.mark.unit
def test_recall_service_is_a_thin_public_coordinator() -> None:
    path = ROOT / "src/menhir/services/recall_service.py"
    tree = _tree("src/menhir/services/recall_service.py")

    assert len(path.read_text(encoding="utf-8").splitlines()) <= 160
    service = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RecallService"
    )
    methods = {
        node.name
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods == {"recall"}

    owners = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert {
        "menhir.services.recall_pipeline",
        "menhir.services.recall_policies",
        "menhir.services.recall_support",
    } <= owners


@pytest.mark.unit
def test_telemetry_store_owns_only_connection_and_schema() -> None:
    path = ROOT / "src/menhir/infrastructure/telemetry/store.py"
    tree = _tree("src/menhir/infrastructure/telemetry/store.py")

    assert len(path.read_text(encoding="utf-8").splitlines()) <= 425
    store = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "McpTelemetryStore"
    )
    methods = {
        node.name
        for node in store.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods == {"_connect", "_ensure_ready"}


@pytest.mark.unit
def test_graphiti_patch_facade_has_no_patch_implementations() -> None:
    path = ROOT / "src/menhir/infrastructure/graphiti_patches.py"
    tree = _tree("src/menhir/infrastructure/graphiti_patches.py")

    assert len(path.read_text(encoding="utf-8").splitlines()) <= 60
    assert not [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    owners = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert owners == {
        "menhir.infrastructure.graphiti_extraction_patches",
        "menhir.infrastructure.graphiti_llm_patches",
        "menhir.infrastructure.graphiti_model_patches",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relative_path", "class_name", "max_lines"),
    [
        ("src/menhir/infrastructure/view_repository.py", "ViewRepository", 40),
        (
            "src/menhir/infrastructure/typed_assertion_repository.py",
            "TypedAssertionRepository",
            30,
        ),
    ],
)
def test_graph_repository_facades_only_compose_operation_families(
    relative_path: str, class_name: str, max_lines: int
) -> None:
    path = ROOT / relative_path
    tree = _tree(relative_path)

    assert len(path.read_text(encoding="utf-8").splitlines()) <= max_lines
    repository = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    assert not [
        node
        for node in repository.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert len(repository.bases) == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relative_path", "class_name", "max_lines"),
    [
        ("src/menhir/services/ingest_service.py", "IngestService", 140),
        ("src/menhir/services/lifecycle_service.py", "LifecycleService", 80),
    ],
)
def test_workflow_service_facades_only_compose_operation_families(
    relative_path: str, class_name: str, max_lines: int
) -> None:
    path = ROOT / relative_path
    tree = _tree(relative_path)

    assert len(path.read_text(encoding="utf-8").splitlines()) <= max_lines
    service = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    assert not [
        node
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert len(service.bases) == 3
