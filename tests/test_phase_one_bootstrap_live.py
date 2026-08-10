"""Live integration test: run phase-1 bootstrap against real Neo4j.

Marked ``online`` — skipped unless Neo4j is reachable.
Run with: pytest -m online tests/test_phase_one_bootstrap_live.py
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from menhir.infrastructure import MemoryGraphAdapter, Neo4jRepository
from menhir.infrastructure.schema import (
    EDGE_LABELS,
    MEMORY_NODE_LABELS,
    get_phase1_bootstrap_queries,
)

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")


@pytest.fixture(scope="module")
def neo4j_repo():
    """Provide a real Neo4j repository, skip if unreachable."""
    repo = Neo4jRepository(
        uri=NEO4J_URI, database=NEO4J_DATABASE, user=NEO4J_USER, password=NEO4J_PASSWORD)
    if not repo.ping():
        repo.close()
        pytest.skip("Neo4j is not reachable — skipping online tests")
    yield repo
    repo.close()


@pytest.fixture(scope="module")
def bootstrap_result(neo4j_repo: Neo4jRepository):
    """Run bootstrap once for the whole module and return the result."""
    adapter = MemoryGraphAdapter(neo4j=neo4j_repo)
    return adapter.bootstrap_phase_one()


@pytest.mark.online
class TestPhaseOneBootstrapLive:
    """Verify phase-1 schema bootstrap against a live Neo4j instance."""

    def test_bootstrap_succeeds(self, bootstrap_result) -> None:
        assert bootstrap_result.success is True
        assert bootstrap_result.queries_executed == len(get_phase1_bootstrap_queries())
        assert bootstrap_result.failures == []

    def test_node_indexes_exist(self, neo4j_repo: Neo4jRepository) -> None:
        """Verify that indexes for all memory node labels were created."""
        rows = neo4j_repo.execute("SHOW INDEXES YIELD name, entityType, labelsOrTypes RETURN name, entityType, labelsOrTypes")
        index_names = {row["name"] for row in rows}

        for label in MEMORY_NODE_LABELS:
            suffix = label.lower()
            expected_indexes = [
                f"{suffix}_type_idx",
                f"{suffix}_scope_idx",
                f"{suffix}_freshness_idx",
                f"{suffix}_session_id_idx",
                f"{suffix}_user_id_idx",
                f"{suffix}_sharpness_idx",
                f"{suffix}_conflict_group_idx",
                f"{suffix}_conflict_status_idx",
            ]
            for idx_name in expected_indexes:
                assert idx_name in index_names, f"Missing index: {idx_name}"

    def test_edge_indexes_exist(self, neo4j_repo: Neo4jRepository) -> None:
        """Verify that indexes for all edge labels were created."""
        rows = neo4j_repo.execute("SHOW INDEXES YIELD name RETURN name")
        index_names = {row["name"] for row in rows}

        for label in EDGE_LABELS:
            suffix = label.lower()
            expected_indexes = [
                f"{suffix}_weight_idx",
                f"{suffix}_scope_idx",
                f"{suffix}_last_traversed_idx",
            ]
            for idx_name in expected_indexes:
                assert idx_name in index_names, f"Missing edge index: {idx_name}"

    def test_bootstrap_is_idempotent(self, neo4j_repo: Neo4jRepository) -> None:
        """Running bootstrap a second time should succeed without errors."""
        adapter = MemoryGraphAdapter(neo4j=neo4j_repo)
        second = adapter.bootstrap_phase_one()
        assert second.success is True
        assert second.failures == []

    def test_defaults_queries_are_valid_cypher(self, neo4j_repo: Neo4jRepository) -> None:
        """All defaults/backfill queries should execute without Cypher errors.

        On an empty DB these are no-ops, but they must parse and run cleanly.
        """
        from menhir.infrastructure.schema import _node_defaults_queries, _edge_defaults_queries

        for query in _node_defaults_queries() + _edge_defaults_queries():
            neo4j_repo.execute(query.strip())
