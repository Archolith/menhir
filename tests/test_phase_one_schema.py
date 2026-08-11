import pytest

from menhir.infrastructure import (
    EDGE_LABELS,
    MEMORY_NODE_LABELS,
    MemoryGraphAdapter,
    PHASE_ONE_REQUIRED_INDEXES,
    get_phase1_bootstrap_queries,
)


class _StubGraphRepo:
    """In-memory Neo4j stand-in for adapter unit tests."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, str]]:
        self.executed.append(query)
        return []


class _StubIndexRepo(_StubGraphRepo):
    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.executed.append(query)
        if "SHOW INDEXES" in query:
            return [{"names": self.names}]
        return []


def test_phase1_schema_includes_required_node_defaults() -> None:
    query_blob = "\n".join(get_phase1_bootstrap_queries())
    assert "MATCH (n:Entity)" in query_blob
    assert "MATCH (n:Episodic)" in query_blob
    assert "toFloat(n.source_confidence)" in query_blob
    assert "WHEN coalesce(n.scope, 'SESSION') = 'SESSION' THEN n.freshness" in query_blob
    assert "user_flagged" in query_blob
    assert "bootstrap_scope" in query_blob
    assert "toInteger(n.edge_count)" in query_blob
    assert "conflict_group_id" in query_blob
    assert "unresolved" in query_blob
    assert "conflict_created_at" in query_blob
    assert "session_id" in query_blob
    assert "user_id" in query_blob
    assert "processing_state" in query_blob
    assert "processing_attempts" in query_blob
    assert "processing_llm_tasks_attempt" in query_blob
    assert "processing_llm_tasks_total" in query_blob
    assert "processing_llm_last_task_at" in query_blob
    assert "processing_owner" in query_blob
    assert "processing_lease_expires_at" in query_blob
    assert "queued_at" in query_blob
    assert "resolved_episode_uuid" in query_blob


def test_phase1_schema_targets_expected_labels() -> None:
    query_blob = " ".join(get_phase1_bootstrap_queries())
    for label in MEMORY_NODE_LABELS:
        assert f":{label}" in query_blob
    for label in EDGE_LABELS:
        assert f"-{label}-" in query_blob or f":{label}" in query_blob


def test_work_artifact_uuid_plain_index_is_retired_before_unique_constraint() -> None:
    queries = get_phase1_bootstrap_queries()
    drop_index = "DROP INDEX work_artifact_uuid_idx IF EXISTS"
    create_constraint = (
        "CREATE CONSTRAINT work_artifact_uuid_unique IF NOT EXISTS "
        "FOR (n:WorkArtifact) REQUIRE n.artifact_uuid IS UNIQUE"
    )

    assert drop_index in queries
    assert create_constraint in queries
    assert queries.index(drop_index) < queries.index(create_constraint)
    assert not any(query.startswith("CREATE INDEX work_artifact_uuid_idx") for query in queries)


@pytest.mark.unit
def test_adapter_bootstrap_runs_all_queries() -> None:
    repo = _StubGraphRepo()
    adapter = MemoryGraphAdapter(neo4j=repo)
    result = adapter.bootstrap_phase_one()

    assert result.success is True
    assert result.queries_executed == len(get_phase1_bootstrap_queries())
    assert repo.executed == get_phase1_bootstrap_queries()


@pytest.mark.unit
def test_adapter_phase_one_schema_ready_when_required_indexes_online() -> None:
    repo = _StubIndexRepo(list(PHASE_ONE_REQUIRED_INDEXES))
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert adapter.phase_one_schema_ready() is True


@pytest.mark.unit
def test_adapter_phase_one_schema_ready_when_indexes_missing() -> None:
    repo = _StubIndexRepo(list(PHASE_ONE_REQUIRED_INDEXES[:-1]))
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert adapter.phase_one_schema_ready() is False
