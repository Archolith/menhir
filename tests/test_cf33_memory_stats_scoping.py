"""CF-33 / CF-36: `get_memory_stats` reported graph-wide cardinality to a pinned client.

OWNER RULING 2026-08-21. The tool's own comment already recorded the residue honestly -- the Graph
section summed entity/episode/flagged counts across every silo, so a pinned client learned roughly
how much data existed outside its own -- and deferred it as "a feature decision, not this fix".
That decision has now been made.

WHY THIS IS THE WHOLE OF CF-33, not a piece of it. The entry says the pin "cannot constrain 41 of
the 54 MCP tools". Measured by executing the catalog: **43 of 54 are pinnable**; the figure predates
the namespace threading in CF-220, CF-226, CF-221 and CF-237. The 11 that remain are declared
`global` deliberately -- scheduler control, client identity, cross-silo maintenance -- and
`get_memory_stats` was the only one of them carrying tenant data. There is no architecture left to
design.

THE SPLIT IS THE POINT. Only the Graph section is scoped. Operation latencies, failure counts,
enrichment rate, queue depth and circuit breakers come from the telemetry sidecar and process state;
they are deployment-wide by nature. Both halves are LABELLED, because a scoped count printed beside
an unscoped one with nothing saying which is which is how a reader draws a false ratio.

Measured against the production graph after this change:

    ns=None        entities= 50760  episodes= 2531  turns= 576
    ns=default     entities= 50331  episodes= 2334  turns=   0
    ns=archolith   entities=   235  episodes=   41  turns=  17
    ns=menhir      entities=    31  episodes=    4  turns= 186
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from menhir.domain.namespace import namespace_spellings
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.mcp.contracts import ToolScope, assert_tool_scopes_declared
from menhir.mcp.tools import ALL_TOOLS
from menhir.mcp.tools.ops.get_memory_stats import GetMemoryStatsTool

pytestmark = pytest.mark.unit


class _Neo4j:
    """Records the predicates and parameters each statement was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "ADMITTED_ON" in query:
            return [{"turn_evidence_count": 3, "admission_edge_count": 1}]
        return [{"total_memories": 9, "entity_count": 6, "episode_count": 3}]

    def _for(self, needle: str) -> tuple[str, Any]:
        return next(c for c in self.calls if (needle in c[0]) == (needle == "ADMITTED_ON"))


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


def test_a_namespace_scopes_the_entity_counts() -> None:
    """THE FINDING: a pinned client must not be counting the whole graph."""
    neo4j = _Neo4j()
    MemoryQueryRepository(neo4j).fetch_memory_overview("archolith")

    node_query, node_params = next(c for c in neo4j.calls if "ADMITTED_ON" not in c[0])
    assert "n.group_id IN $group_ids" in node_query
    assert node_params == {"group_ids": ["archolith"]}


def test_turn_evidence_is_scoped_on_namespace_not_group_id() -> None:
    """:TurnEvidence carries NO `group_id` at all -- verified on the live graph, 576 nodes with the
    property absent on every one. A group_id predicate here would match nothing and report a false
    `no_turns`, which is the failure mode this whole finding family is about: a scoping bug that
    reads as healthy."""
    neo4j = _Neo4j()
    MemoryQueryRepository(neo4j).fetch_memory_overview("archolith")

    admission_query, admission_params = next(c for c in neo4j.calls if "ADMITTED_ON" in c[0])
    assert "t.namespace IN $ns" in admission_query
    assert "group_id" not in admission_query
    assert admission_params == {"ns": ["archolith"]}


def test_the_admission_edge_count_is_scoped_too() -> None:
    """The edge count must be constrained by the same silo as the turn count, or a tenant with no
    turns could still be shown another tenant's edges."""
    neo4j = _Neo4j()
    MemoryQueryRepository(neo4j).fetch_memory_overview("archolith")

    admission_query, _ = next(c for c in neo4j.calls if "ADMITTED_ON" in c[0])
    assert "-[r:ADMITTED_ON]->(t:TurnEvidence) WHERE t.namespace IN $ns" in admission_query


# ---------------------------------------------------------------------------
# the owner ruling: '' and 'default' are one silo
# ---------------------------------------------------------------------------


def test_default_and_empty_string_are_the_same_silo() -> None:
    """OWNER RULING 2026-08-21. Both spellings are accepted on READ until the persisted values are
    migrated, because a read that accepted only the canonical one would go blind to every existing
    row the moment the ruling was adopted."""
    assert set(namespace_spellings("default")) == {"default", ""}
    assert set(namespace_spellings("")) == {"default", ""}


def test_a_real_tenant_gets_only_itself() -> None:
    """POSITIVE CONTROL: the equivalence is for the default silo alone. If it leaked into named
    tenants it would merge silos, which is the opposite of this fix."""
    assert namespace_spellings("archolith") == ["archolith"]


def test_none_means_no_filter() -> None:
    assert namespace_spellings(None) is None


def test_the_default_silo_query_carries_both_spellings() -> None:
    neo4j = _Neo4j()
    MemoryQueryRepository(neo4j).fetch_memory_overview("default")

    _, admission_params = next(c for c in neo4j.calls if "ADMITTED_ON" in c[0])
    assert set(admission_params["ns"]) == {"default", ""}


# ---------------------------------------------------------------------------
# positive controls
# ---------------------------------------------------------------------------


def test_no_namespace_still_counts_every_silo() -> None:
    """POSITIVE CONTROL, the one that matters most. Most callers of this are operational -- the
    scheduler's queue-health job, the metadata resource -- and want the whole deployment. A fix
    that scoped unconditionally would silently shrink their numbers."""
    neo4j = _Neo4j()
    MemoryQueryRepository(neo4j).fetch_memory_overview()

    for query, params in neo4j.calls:
        assert params is None, query
        assert "group_id IN" not in query
        assert "t.namespace IN" not in query


def test_the_tool_declares_the_scope_its_signature_supports() -> None:
    """`assert_tool_scopes_declared` refuses to start when a declaration contradicts a signature --
    NAMESPACED with no `namespace` parameter would read as pinned in the audit list while the pin
    could not actually reach it."""
    assert GetMemoryStatsTool.scope == ToolScope.NAMESPACED
    assert "namespace" in inspect.signature(GetMemoryStatsTool.endpoint).parameters
    assert_tool_scopes_declared(ALL_TOOLS)


def test_the_pin_can_now_reach_this_tool() -> None:
    """The mechanism, not just the declaration: `_apply_pinned_namespace` only rewrites tools whose
    endpoint accepts `namespace`, so this is what actually makes the pin bite."""
    tool = GetMemoryStatsTool()
    assert tool._accepts_namespace() is True


def test_the_module_facade_accepts_the_same_argument() -> None:
    """The plain-function facade is the documented entry point; if it dropped the parameter the
    tool and its facade would disagree about what a caller can ask for."""
    from menhir.mcp.tools.ops.get_memory_stats import get_memory_stats

    assert "namespace" in inspect.signature(get_memory_stats).parameters
