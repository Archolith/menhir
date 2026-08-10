"""No test may resolve its Neo4j connection to the production graph.

Online tests build services via ``MemorySettings.from_env()`` (reads ``NEO4J_URI``). With
``.env`` sourced or ``NEO4J_URI`` exported, that would otherwise resolve to prod
(the operator's real graph) and run destructive, unscoped decay/consolidation/ingest against the
operator's live memories -- the 2026-07-12 incident.

``conftest.force_all_tests_onto_test_neo4j`` (autouse, session) forces every ``from_env()``
resolution onto the dedicated test instance for the whole run. These tests pin that invariant
so it cannot silently regress.
"""

from __future__ import annotations

import os

import pytest

from menhir.config import MemorySettings


@pytest.mark.unit
def test_from_env_never_resolves_to_prod_neo4j() -> None:
    """Under the autouse redirect, from_env() must point at the test instance, not prod."""
    resolved = MemorySettings.from_env().neo4j_uri
    expected_test_uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")

    assert resolved == expected_test_uri, (
        f"from_env() resolved to {resolved!r}, not the test instance {expected_test_uri!r} -- "
        "the test->test-instance redirect has regressed"
    )

    prod_snapshot = os.getenv("MENHIR_PROD_NEO4J_URI_SNAPSHOT")
    assert prod_snapshot, "real prod URI was not snapshotted for the equality guard"
    assert resolved != prod_snapshot, (
        f"from_env() still resolves to the production graph ({prod_snapshot!r})"
    )
