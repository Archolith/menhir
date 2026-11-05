"""The retired RANGE index on :Episodic(content) must be gone from every graph, not just new ones.

Neo4j cannot index a property value over ~8 KB, so `episodic_content_idx` made any longer
episode unwritable (an LME build died on a 9,838-byte episode). Dropping it inside
`bootstrap_phase_one` would not reach an existing install: startup skips that bootstrap once the
required index set is online. These pin that the drop runs on every writer start.
"""

from __future__ import annotations

import os
import re
import uuid
from types import SimpleNamespace

import pytest

from menhir.infrastructure.schema import (
    PHASE_ONE_REQUIRED_INDEXES,
    RETIRED_INDEXES,
    get_phase1_bootstrap_queries,
    get_retired_index_queries,
)

OLD_DDL = "CREATE INDEX episodic_content_idx IF NOT EXISTS FOR (n:Episodic) ON (n.content)"


@pytest.mark.unit
def test_bootstrap_never_creates_or_requires_a_retired_index() -> None:
    created = set(re.findall(r"CREATE (?:\w+ )*INDEX (\w+)", " ".join(get_phase1_bootstrap_queries())))
    assert "episodic_content_idx" in RETIRED_INDEXES
    assert created.isdisjoint(RETIRED_INDEXES)
    assert set(PHASE_ONE_REQUIRED_INDEXES).isdisjoint(RETIRED_INDEXES)


@pytest.mark.unit
def test_adapter_drops_each_retired_index_idempotently() -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    executed: list[str] = []
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter.neo4j = SimpleNamespace(execute=lambda query, params=None: executed.append(query) or [])

    assert adapter.retire_obsolete_indexes() == []
    assert executed == get_retired_index_queries()
    assert all(q.startswith("DROP INDEX ") and q.endswith(" IF EXISTS") for q in executed)


def _artifacts(*, schema_ready: bool, retire):
    calls: list[str] = []
    readiness = iter([schema_ready, True])

    def _retire():
        calls.append("retire")
        return retire()

    adapter = SimpleNamespace(
        retire_obsolete_indexes=_retire,
        phase_one_schema_ready=lambda **_: (calls.append("ready"), next(readiness))[1],
        bootstrap_phase_one=lambda: (calls.append("bootstrap"), SimpleNamespace(
            success=True, queries_executed=1, failures=[]))[1],
        sync_edge_counts=lambda: 0,
    )
    artifacts = SimpleNamespace(
        capabilities=SimpleNamespace(graphiti_ready=False),
        graph_adapter=adapter,
        graphiti_client=None,
    )
    return artifacts, calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_install_with_schema_ready_still_drops_the_retired_index() -> None:
    """The counterexample: every index online, so bootstrap is skipped -- the drop must not be."""
    from menhir.core.bootstrap import prepare_memory_runtime

    artifacts, calls = _artifacts(schema_ready=True, retire=lambda: [])
    result = await prepare_memory_runtime(artifacts)

    assert result["schema"]["skipped"] is True
    assert "bootstrap" not in calls
    assert calls[0] == "retire", "the drop runs before the readiness gate, whatever it decides"
    assert result["retired_index_failures"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_drop_is_reported_and_does_not_refuse_startup() -> None:
    """A failed drop leaves the pre-fix behaviour (long episodes fail); refusing to boot over it
    would turn a partial outage into a total one."""
    from menhir.core.bootstrap import prepare_memory_runtime

    artifacts, _ = _artifacts(schema_ready=True, retire=lambda: ["drop failed: transient"])
    result = await prepare_memory_runtime(artifacts)

    assert result["retired_index_failures"] == ["drop failed: transient"]
    assert result["schema"]["status"] == "ok"


@pytest.fixture
def neo4j_repo():
    from menhir.infrastructure import Neo4jRepository

    repo = Neo4jRepository(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "password"),
    )
    if not repo.ping():
        repo.close()
        pytest.skip("Neo4j is not reachable")
    yield repo
    repo.close()


@pytest.mark.online
def test_a_graph_carrying_the_old_index_accepts_a_long_episode_after_retirement(neo4j_repo) -> None:
    from menhir.infrastructure import MemoryGraphAdapter

    group = f"retired-content-idx-{uuid.uuid4().hex[:8]}"
    long_content = "x" * 10_000
    write = "CREATE (n:Episodic {uuid: $u, group_id: $g, content: $c})"
    try:
        neo4j_repo.execute(OLD_DDL)
        neo4j_repo.execute("CALL db.awaitIndexes(60)")
        # Reproduce first, or the pass below proves nothing.
        with pytest.raises(Exception, match="(?i)too large"):
            neo4j_repo.execute(write, params={"u": str(uuid.uuid4()), "g": group, "c": long_content})

        assert MemoryGraphAdapter(neo4j=neo4j_repo).retire_obsolete_indexes() == []

        names = {r["name"] for r in neo4j_repo.execute("SHOW INDEXES YIELD name RETURN name")}
        assert names.isdisjoint(RETIRED_INDEXES)
        neo4j_repo.execute(write, params={"u": str(uuid.uuid4()), "g": group, "c": long_content})
        rows = neo4j_repo.execute(
            "MATCH (n:Episodic {group_id: $g}) RETURN size(n.content) AS n", params={"g": group})
        assert [r["n"] for r in rows] == [10_000]
    finally:
        neo4j_repo.execute("MATCH (n:Episodic {group_id: $g}) DETACH DELETE n", params={"g": group})
        neo4j_repo.execute("DROP INDEX episodic_content_idx IF EXISTS")
