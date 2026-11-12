"""Sync fence-lock writers stay off the event loop (release PR #599 stdio E2E hang).

The live race is in test_fence_lock_off_event_loop_online.py. Here: the runtime guard logs only
on a loop thread, and an AST census refuses an async function that calls a fence-lock writer
directly instead of through ``asyncio.to_thread`` / ``_off_loop``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from menhir.infrastructure.erasure_fence import FENCE_LOCK_ON_EVENT_LOOP, warn_if_on_event_loop

pytestmark = [pytest.mark.unit]

SRC = Path(__file__).resolve().parents[1] / "src" / "menhir"

#: Every sync function whose transaction takes the EvidenceNamespaceFence write lock, plus the
#: enrichment wrappers around them.
FENCE_LOCK_WRITERS = frozenset({
    "seal_memory", "seal_namespace", "release_memory", "release_orphaned_erasing_ops",
    "run_fenced", "run_fenced_call", "execute_copy_fenced",
    "create_evidence_projection", "ensure_self_entity", "create_raw_capture_entity",
    "compress_node", "complete_rehydration", "resolve_conflict_group",
    "_fenced_write", "_record_retention_fenced",
})


def test_guard_logs_on_the_loop_only(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="menhir.infrastructure.erasure_fence")
    warn_if_on_event_loop("off_loop")
    assert FENCE_LOCK_ON_EVENT_LOOP not in caplog.text

    async def on_loop() -> None:
        warn_if_on_event_loop("on_loop")
        await asyncio.to_thread(warn_if_on_event_loop, "in_thread")

    asyncio.run(on_loop())
    assert f"{FENCE_LOCK_ON_EVENT_LOOP} op=on_loop" in caplog.text
    assert "op=in_thread" not in caplog.text


def _direct_calls(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Calls made in ``fn``'s own body, not inside nested defs or lambdas."""
    found: list[tuple[int, str]] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            # An awaited call is an async method (e.g. the backend's off-loop wrapper of the
            # same name); a sync writer cannot be awaited. Its arguments are still checked.
            stack.extend(node.value.args)
            stack.extend(kw.value for kw in node.value.keywords)
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in FENCE_LOCK_WRITERS:
                found.append((node.lineno, name))
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_no_async_function_calls_a_fence_lock_writer_on_the_loop() -> None:
    """Fails if an async function calls a sync fence-lock writer directly (it would wait on the loop)."""
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for line, name in _direct_calls(node):
                    violations.append(f"{path.relative_to(SRC)}:{line} {node.name} -> {name}")
    assert not violations, "\n".join(violations)
