"""A first install with no AI provider must be able to start.

`prepare_memory_runtime` skips Graphiti's index build when there is no usable LLM/embedder, so
the server can "start against Neo4j alone (graph adapter works; Graphiti features degrade)
instead of crashing" -- its own comment. The readiness check then required the three indexes that
skipped call is the only creator of, so the branch raised at startup every time and could never
be taken. Found by standing up a container with no AI provider against an empty graph; every
existing test ran against a graph that already had them.

The rule these pin: relax the requirement ONLY for indexes Menhir does not create, and only when
Graphiti is actually absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.schema import (
    GRAPHITI_OWNED_INDEXES,
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
    get_phase1_bootstrap_queries,
)

pytestmark = pytest.mark.unit


class _Neo4j:
    """Answers SHOW INDEXES/CONSTRAINTS from a fixed set of online names."""

    def __init__(self, online: set[str], constraints: list[dict[str, Any]] | None = None) -> None:
        self.online = online
        # Default to a fully-satisfied constraint set: these tests are about the INDEX
        # requirement, and a fake that silently reports no constraints would fail them for an
        # unrelated reason -- as it did on the first run.
        self.constraints = constraints if constraints is not None else [
            {
                "name": name,
                "type": constraint_type,
                "entityType": entity_type,
                "labelsOrTypes": list(labels),
                "properties": list(properties),
            }
            for name, constraint_type, entity_type, labels, properties
            in PHASE_ONE_REQUIRED_CONSTRAINTS
        ]
        self.asked_for: list[str] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = params or {}
        if "SHOW INDEXES" in query:
            self.asked_for = list(params.get("names", []))
            return [{"names": [n for n in self.asked_for if n in self.online]}]
        if "SHOW CONSTRAINTS" in query:
            return self.constraints
        return []


def _adapter(neo4j: _Neo4j):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter.neo4j = neo4j
    return adapter


def test_the_three_relaxed_indexes_are_ones_menhir_never_creates() -> None:
    """The load-bearing fact. If Menhir's own DDL gained one of these, relaxing it would start
    hiding a real bootstrap failure instead of tolerating an absent component."""
    import re

    ddl = " ".join(get_phase1_bootstrap_queries())
    created = set(re.findall(r"CREATE (?:\w+ )*INDEX (\w+)", ddl))

    assert GRAPHITI_OWNED_INDEXES.isdisjoint(created)
    assert GRAPHITI_OWNED_INDEXES <= set(PHASE_ONE_REQUIRED_INDEXES)


def test_without_graphiti_the_schema_is_ready_once_menhirs_own_indexes_exist() -> None:
    """The bug: this returned False forever, so startup was refused on every attempt."""
    menhir_owned = set(PHASE_ONE_REQUIRED_INDEXES) - GRAPHITI_OWNED_INDEXES
    neo4j = _Neo4j(online=menhir_owned)

    assert _adapter(neo4j).phase_one_schema_ready(require_graphiti=False) is True
    assert not (set(neo4j.asked_for) & GRAPHITI_OWNED_INDEXES), (
        "the relaxed check must not even ask for indexes it has decided not to require"
    )


def test_with_graphiti_the_same_graph_is_not_ready() -> None:
    """Nothing is relaxed for an instance that HAS Graphiti: a missing index there means the
    build failed rather than never ran, and that must still refuse the writer."""
    menhir_owned = set(PHASE_ONE_REQUIRED_INDEXES) - GRAPHITI_OWNED_INDEXES
    neo4j = _Neo4j(online=menhir_owned)

    assert _adapter(neo4j).phase_one_schema_ready(require_graphiti=True) is False


def test_the_default_stays_strict() -> None:
    """An existing caller that passes nothing keeps the old, stricter behaviour."""
    menhir_owned = set(PHASE_ONE_REQUIRED_INDEXES) - GRAPHITI_OWNED_INDEXES
    neo4j = _Neo4j(online=menhir_owned)

    assert _adapter(neo4j).phase_one_schema_ready() is False


def test_a_missing_menhir_owned_index_still_fails_without_graphiti() -> None:
    """The relaxation is narrow: it drops three names, not the requirement."""
    menhir_owned = set(PHASE_ONE_REQUIRED_INDEXES) - GRAPHITI_OWNED_INDEXES
    incomplete = set(menhir_owned)
    incomplete.discard("entity_type_idx")
    neo4j = _Neo4j(online=incomplete)

    assert _adapter(neo4j).phase_one_schema_ready(require_graphiti=False) is False


def test_bootstrap_requires_graphiti_indexes_exactly_when_it_builds_them() -> None:
    """One decision used twice. Deriving the requirement separately is how they diverged."""
    from pathlib import Path

    import menhir.core.bootstrap as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "graphiti_in_play = bool(graphiti_ready and graphiti_client_available)" in source
    assert "if graphiti_in_play:" in source
    # Counted across the module rather than sliced to a region: the invariant is that EVERY
    # readiness call in the bootstrap path is pinned to the same decision as the build, and a
    # slice would quietly stop covering a third call added below it.
    assert source.count("phase_one_schema_ready") == source.count(
        "require_graphiti=graphiti_in_play"
    ), "every readiness call in bootstrap must be pinned to the build decision"
