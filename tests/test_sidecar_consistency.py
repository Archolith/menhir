"""M7 Phase 4D — SQLite sidecar consistency spot-check.

Validates that the graph (Neo4j) and SQLite telemetry sidecar have consistent state:
- READY episodes in Neo4j should have corresponding lifecycle_events in SQLite
- lifecycle_actions rows should reference memory UUIDs that exist in the graph

This is a spot-check, not a full reconciliation. Run with:
    pytest --run-online -m online tests/test_sidecar_consistency.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# needs_llm: these tests DO have skip-guards, but both sit downstream of _get_live_services()
# -> start_runtime(), which runs the runtime preflight and raises when the LLM/reranker endpoints
# are unreachable ("Graphiti extraction connectivity/model check failed"). The guard never gets a
# chance to skip, so with Neo4j but no LLM this fails instead of skipping. Contrast
# test_ingest_live / test_recall_live / test_lifecycle_live, which call their guard BEFORE
# building services and therefore skip cleanly without this marker.
pytestmark = [pytest.mark.online, pytest.mark.needs_llm]


async def _get_live_services():
    """Build live services from the canonical runtime bootstrap path."""
    from menhir.core.runtime import start_runtime

    ctx = await start_runtime()
    return ctx.built


def _get_telemetry_db() -> Path:
    from menhir.mcp.telemetry import default_telemetry_db_path
    return default_telemetry_db_path()


class TestSidecarConsistency:
    """Spot-check graph/SQLite split state for consistency."""

    async def test_ready_episodes_have_lifecycle_events(self):
        """Each READY episode in Neo4j should have a corresponding mcp_event."""
        built = await _get_live_services()
        adapter = built.graph_adapter

        ready_episodes = adapter.list_episode_processing(
            processing_states=["READY"], limit=50
        )
        if not ready_episodes:
            pytest.skip("No READY episodes in graph — nothing to check")

        db_path = _get_telemetry_db()
        if not db_path.exists():
            pytest.skip(f"Telemetry DB not found at {db_path}")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Check for episode_enrichment events in mcp_events
            cursor = conn.execute(
                "SELECT COUNT(*) AS cnt FROM mcp_events WHERE operation = 'episode_enrichment' AND success = 1"
            )
            row = cursor.fetchone()
            event_count = row["cnt"] if row else 0
        finally:
            conn.close()

        # At least some READY episodes should have corresponding events
        assert event_count > 0, (
            f"Found {len(ready_episodes)} READY episodes in Neo4j but "
            f"no successful episode_enrichment events in SQLite"
        )

    async def test_lifecycle_actions_reference_existing_nodes(self):
        """lifecycle_actions rows should reference node UUIDs that exist in the graph."""
        db_path = _get_telemetry_db()
        if not db_path.exists():
            pytest.skip(f"Telemetry DB not found at {db_path}")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT DISTINCT node_uuid FROM lifecycle_actions ORDER BY timestamp DESC LIMIT 20"
            )
            action_uuids = [row["node_uuid"] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            pytest.skip("lifecycle_actions table does not exist yet")
        finally:
            conn.close()

        if not action_uuids:
            pytest.skip("No lifecycle_actions rows — nothing to check")

        built = await _get_live_services()
        adapter = built.graph_adapter

        # Spot-check: at least some referenced nodes should still exist
        # (GONE nodes may have been deleted, so we allow partial matches)
        found = 0
        for uuid in action_uuids[:10]:
            metadata = adapter.fetch_candidate_metadata([uuid])
            if metadata:
                found += 1

        # At least 1 of the 10 sampled should exist (GONE nodes are the exception)
        assert found > 0 or len(action_uuids) == 0, (
            f"None of {len(action_uuids[:10])} lifecycle_actions node UUIDs "
            "found in graph — possible data integrity issue"
        )
