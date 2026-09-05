"""Phase 4: the canonical-self API must never delete or rewire a fork.

The old `_absorb_self_entity_forks` ran as a side effect of an ordinary write, bulk-rewired every
incident relationship, and ended in `DETACH DELETE`. Against the production-shaped population
(66 same-named nodes carrying 1,670 edges) that would have been an unreviewable, irreversible
mass mutation triggered by enabling a feature flag.

These tests assert the absence of writes, not the presence of a result.
"""

from __future__ import annotations

import pytest

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository


class _RecordingNeo4j:
    """Records every statement so a test can assert on what was *not* executed."""

    def __init__(self, fork_uuids: list[str] | None = None) -> None:
        self.statements: list[tuple[str, dict]] = []
        self._forks = fork_uuids or []

    def execute(self, query: str, params: dict | None = None):
        self.statements.append((query, params or {}))
        if "RETURN f.uuid AS uuid" in query:
            return [{"uuid": u} for u in self._forks]
        return []

    @property
    def mutating(self) -> list[str]:
        verbs = ("DELETE", "MERGE (c)", "CREATE (c)", "SET r2", "DETACH")
        return [q for q, _ in self.statements if any(v in q for v in verbs)]


def _repo(forks: list[str] | None = None) -> tuple[EpisodeLifecycleRepository, _RecordingNeo4j]:
    repo = EpisodeLifecycleRepository()
    neo4j = _RecordingNeo4j(forks)
    repo.neo4j = neo4j
    return repo, neo4j


@pytest.mark.unit
@pytest.mark.parametrize("fork_count", [0, 1, 2, 15, 70])
def test_ensure_never_deletes_or_rewires_however_many_forks_exist(fork_count):
    """0, 1, and the production-shaped 70. The write path's behavior must not depend on how many
    forks it finds -- finding more must never escalate into a bulk mutation."""
    forks = [f"fork-{i}" for i in range(fork_count)]
    repo, neo4j = _repo(forks)

    result = repo.ensure_self_entity("ns-1")

    assert result == self_uuid_for_namespace("ns-1")
    assert neo4j.mutating == [], f"canonical-self write mutated relationships: {neo4j.mutating}"
    assert not any("DETACH DELETE" in q for q, _ in neo4j.statements)


@pytest.mark.unit
def test_the_absorber_is_gone_entirely():
    """Not merely unreferenced -- absent. A dormant destructive method is one call site away from
    being live again, and the plan forbids reviving its Cypher as a migration template."""
    assert not hasattr(EpisodeLifecycleRepository, "_absorb_self_entity_forks")

    import ast
    import inspect

    import menhir.infrastructure.episode_lifecycle as mod

    # Check executable Cypher only: the module's prose deliberately names the forbidden predicate
    # so a future reader knows not to reintroduce it, and that mention must not trip the guard.
    tree = ast.parse(inspect.getsource(mod))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    cypher = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
        and ("MATCH" in n.value or "MERGE" in n.value or "DELETE" in n.value)
    ]
    offending = [
        q for q in cypher if "DETACH DELETE" in q or "m.uuid <> $self_uuid" in q
    ]
    assert not offending, f"lossy absorber Cypher is still present: {offending}"


@pytest.mark.unit
def test_detection_is_read_only():
    repo, neo4j = _repo(["fork-a", "fork-b"])

    forks = repo.detect_self_forks(namespace="ns-1", self_uuid=self_uuid_for_namespace("ns-1"))

    assert forks == ["fork-a", "fork-b"]
    assert len(neo4j.statements) == 1
    assert neo4j.statements[0][0].strip().startswith("MATCH")
    assert neo4j.mutating == []


@pytest.mark.unit
def test_detection_reads_both_physical_spellings():
    """The old writer stamped group_id = the logical name, so `default` forks live under group
    "default" while everything else lives under "". Detection must see both or it reports zero
    forks on exactly the population that has them."""
    repo, neo4j = _repo()

    repo.detect_self_forks(namespace="default", self_uuid=self_uuid_for_namespace("default"))

    params = neo4j.statements[0][1]
    assert "" in params["group_ids"]
    assert "default" in params["group_ids"]


@pytest.mark.unit
def test_canonical_write_targets_the_physical_group_not_the_logical_name():
    """The RCA's activation hazard: writing group_id = "default" would create the canonical node
    in a partition containing none of the production data."""
    repo, neo4j = _repo()

    repo.ensure_self_entity("default")

    merge = next(p for q, p in neo4j.statements if "MERGE (n:Entity" in q)
    assert merge["group_id"] == namespace_to_group_id("default") == ""
    assert merge["namespace"] == "default"


@pytest.mark.unit
def test_named_namespace_group_is_unchanged():
    repo, neo4j = _repo()
    repo.ensure_self_entity("proj-a")
    merge = next(p for q, p in neo4j.statements if "MERGE (n:Entity" in q)
    assert merge["group_id"] == "proj-a"


@pytest.mark.unit
def test_forks_are_reported_not_silently_swallowed(caplog):
    """An operator must be able to see that migration is required."""
    repo, _ = _repo([f"fork-{i}" for i in range(70)])

    with caplog.at_level("WARNING"):
        repo.ensure_self_entity("ns-1")

    assert "SELF_FORKS_REQUIRE_MIGRATION" in caplog.text
    assert "forks=70" in caplog.text


@pytest.mark.unit
def test_adapter_and_protocol_expose_the_non_destructive_contract():
    """The adapter is the surface every caller actually binds to, and the Protocol is what they
    type-check against; both have to carry the split or callers keep the old contract."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.event_consolidation import EventConsolidationGraph

    assert hasattr(MemoryGraphAdapter, "detect_self_forks")
    assert hasattr(MemoryGraphAdapter, "ensure_self_entity")
    assert hasattr(EventConsolidationGraph, "detect_self_forks")


@pytest.mark.unit
def test_adapter_fork_detection_performs_no_write():
    """REVIEW P2. detect_self_forks previously obtained the uuid by calling ensure_self_entity,
    which MERGEs and updates the canonical node -- so a census or pre-migration inventory mutated
    the graph it was inspecting. Discovery must be separable from consolidation."""
    from menhir.domain.self_identity import self_uuid_for_namespace
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    class _Episodes:
        def __init__(self) -> None:
            self.ensure_calls = 0
            self.detect_args: dict | None = None

        def ensure_self_entity(self, namespace):
            self.ensure_calls += 1
            return self_uuid_for_namespace(namespace)

        def detect_self_forks(self, *, namespace, self_uuid):
            self.detect_args = {"namespace": namespace, "self_uuid": self_uuid}
            return ["fork-a"]

    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    episodes = _Episodes()
    adapter._episodes = episodes

    forks = adapter.detect_self_forks("ns-1")

    assert forks == ["fork-a"]
    assert episodes.ensure_calls == 0, "read-only detection called the mutating ensure path"
    assert episodes.detect_args["self_uuid"] == self_uuid_for_namespace("ns-1")
