"""Architecture checks for scheduler task responsibility boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_scheduler_tasks_delegate_scalar_consolidation() -> None:
    scheduler_path = (
        Path(__file__).parents[1]
        / "src"
        / "menhir"
        / "services"
        / "scheduler_tasks.py"
    )
    source = scheduler_path.read_text(encoding="utf-8")

    assert "menhir.services.scalar_consolidation" in _imports_in(scheduler_path)
    for implementation_symbol in (
        "TypedScalarPerceptionService",
        "load_next_scalar_batch",
        "advance_scalar_cursor",
        "repair_pending_bindings",
        "repair_incomplete_reconciliations",
        "repair_orphaned_assertions",
    ):
        assert implementation_symbol not in source
