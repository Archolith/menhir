"""Lint: retired state is read through ``memory_retired_cypher`` only (retire plan, step 2b).

"Retired" is the stored ``memory_retired`` marker OR, for an ordinary Entity, the derived rule that
every mentioning Episodic is retired. ``domain.recall_visibility.memory_retired_cypher`` is the
single definition. A raw ``x.memory_retired`` read in a query silently ignores the derived half, so
a new raw read is the failure mode this test designs out.

Scans the string constants of every module under ``src/menhir`` for ``<var>.memory_retired`` (not
``memory_retired_at/_reason/_by``) and fails on any read outside ``recall_visibility.py`` and the
allow-list below. Writes (``SET``/``REMOVE``) are ignored.

Offline.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_SRC = Path(__file__).resolve().parents[1] / "src" / "menhir"

_RAW_READ = re.compile(r"\b\w+\.memory_retired\b(?!_)")
_WRITE = re.compile(r"\b\w+\.memory_retired\s*=\s*(?:true|false|null)\b|REMOVE\s+\w+\.memory_retired\b")

_HELPER_MODULE = "domain/recall_visibility.py"

#: (module relative to src/menhir, function) -> why a raw stored-marker read is correct there.
ALLOWED_RAW_READS: dict[tuple[str, str], str] = {
    ("infrastructure/memory_queries.py", "retire_memory"):
        "manages the stored marker and its idempotency; derived state must not count as 'already'",
    ("infrastructure/memory_queries.py", "restore_memory"):
        "manages the stored marker and its idempotency; derived state must not count as 'retired'",
    ("infrastructure/memory_queries.py", "search_episode_embeddings"):
        "episode source-lane label (queue node + twin); episodes are never derived-retired",
    ("infrastructure/memory_queries.py", "_TIMELINE_RETURN"):
        "episode timeline label (queue node + twin); episodes are never derived-retired",
}


def _raw_reads(path: Path) -> list[tuple[str, int, str]]:
    """(function, line, snippet) for each raw read in a string constant of ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if func == "<module>" and isinstance(child, ast.Assign) and isinstance(
                    child.targets[0], ast.Name):
                name = child.targets[0].id  # module/class-level constant
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                text = _WRITE.sub("", child.value)
                for m in _RAW_READ.finditer(text):
                    hits.append((func, child.lineno, m.group(0)))
            visit(child, name)

    visit(tree, "<module>")
    return hits


def _violations() -> list[str]:
    out = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        if rel == _HELPER_MODULE:
            continue
        for func, line, snippet in _raw_reads(py):
            if (rel, func) not in ALLOWED_RAW_READS:
                out.append(f"{rel}:{line} in {func}(): raw read {snippet!r}")
    return out


def test_no_raw_memory_retired_reads_outside_the_helper():
    assert _violations() == [], (
        "read retired state through domain.recall_visibility.memory_retired_cypher "
        "(or add a justified entry to ALLOWED_RAW_READS)"
    )


def test_allow_list_entries_are_all_still_in_use():
    used = {(py.relative_to(_SRC).as_posix(), func)
            for py in _SRC.rglob("*.py") for func, _l, _s in _raw_reads(py)}
    stale = sorted(set(ALLOWED_RAW_READS) - used)
    assert stale == [], f"stale allow-list entries: {stale}"


def test_lint_catches_a_new_raw_read(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        'def f():\n    return "MATCH (n) WHERE NOT coalesce(n.memory_retired, false) RETURN n"\n'
        'def g():\n    return "MATCH (n) SET n.memory_retired = true, n.memory_retired_at = 1"\n'
        'def h():\n    return "MATCH (n) WHERE n.memory_retired_at IS NULL RETURN n"\n',
        encoding="utf-8")
    assert [(f, s) for f, _l, s in _raw_reads(bad)] == [("f", "n.memory_retired")]
