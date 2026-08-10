"""A normal decay sweep must never delete a node for being isolated.

`cleanup_orphan_subgraphs()` was REMOVED on 2026-07-13. It inferred physical-deletion
eligibility from degree zero alone -- no check on freshness, memory type, age, sharpness,
or the deletion gate -- and its first real-database execution destroyed ~24 unrecoverable
production memories. An isolated node (e.g. a sole neighbour left after bridge_and_delete)
is benign; it is not authorization for deletion.

These tests pin the removal from both sides:
  1. the decay service reports zero orphan cleanup and calls no such path, and
  2. no global degree-zero :Entity deletion query survives anywhere in infrastructure.

See the workspace-root artifact .agent/plans/menhir-orphan-cleanup-removal.md (the split
merge/decay/delete lifecycle remediation; P0).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from menhir.config import MemorySettings
from menhir.services.lifecycle_service import LifecycleService

_SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "menhir"

# A degree-zero isolation predicate on some variable, e.g. `NOT EXISTS { MATCH (n)-[]-() }`,
# `NOT EXISTS { MATCH (x)--() }`, or `NOT EXISTS { MATCH (e)-[r]-() }`. Captures the variable so
# the same variable's :Entity binding and deletion can be checked -- name-agnostic by design.
_ISOLATION_PREDICATE = re.compile(
    r"NOT\s+EXISTS\s*\{\s*MATCH\s*\(\s*(\w+)\s*\)\s*-{1,2}(?:\[[^\]]*\])?-{1,2}\s*\(\s*\)\s*\}",
    re.IGNORECASE,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decay_never_runs_orphan_cleanup(
    stub_memory_graph_adapter, stub_graphiti_client
) -> None:
    """The decay sweep must not delete by isolation, and must report zero.

    The stub adapter deliberately has no cleanup_orphan_subgraphs attribute (removed);
    if the service tried to call it, this would raise AttributeError.
    """
    assert not hasattr(stub_memory_graph_adapter, "cleanup_orphan_subgraphs")

    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        settings=MemorySettings(),
    )
    result = await svc.apply_decay()

    assert result.orphan_subgraphs_cleaned == 0


@pytest.mark.unit
def test_no_degree_zero_entity_deletion_query_exists() -> None:
    """No query anywhere in src/menhir may delete an :Entity selected by degree zero.

    Guards against the shape that was removed, generalised so a re-introduction is caught
    regardless of naming: for each degree-zero isolation predicate on some variable, within
    the same query region the SAME variable must NOT be both bound to :Entity and deleted.
    Name-agnostic (any variable, not just `n`), tolerant of the isolation spelling
    (`-[]-`, `--`, `-[r]-`), and recursive across the whole package (not just top-level
    infrastructure files).

    UUID-scoped operator deletion (delete_memory) and the COMPRESSED->GONE bridge_and_delete
    path are unaffected -- neither selects for deletion by degree zero.
    """
    offenders: list[tuple[str, str]] = []
    for path in _SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _ISOLATION_PREDICATE.finditer(text):
            var = match.group(1)
            # Same query region: a window spanning the isolation predicate.
            window = text[max(0, match.start() - 800): match.end() + 800]
            bound_to_entity = re.search(rf"\(\s*{re.escape(var)}\s*:\s*Entity\b", window)
            deleted = re.search(
                rf"\b(?:DETACH\s+)?DELETE\b[^\n]*\b{re.escape(var)}\b", window, re.IGNORECASE
            )
            if bound_to_entity and deleted:
                rel = path.relative_to(_SRC_DIR).as_posix()
                offenders.append((rel, var))

    assert not offenders, (
        "degree-zero :Entity deletion reintroduced (file, variable): "
        f"{sorted(set(offenders))} -- isolation must never authorize deletion "
        "(see the workspace-root artifact .agent/plans/menhir-orphan-cleanup-removal.md)"
    )
