"""#92: a tracked-write receipt must be traceable to the episode that carries its MENTIONS.

Every write leaves two ``:Episodic`` nodes. Menhir's receipt (the ``episode_id`` handed to the
caller) records the Graphiti-minted twin as ``resolved_episode_uuid`` when enrichment completes;
the twin is what MENTIONS the extracted entities. Before this change ``get_provenance`` named
only the twin, so a caller holding a receipt had nothing to match. Now provenance carries the
receipt as ``episode_id`` and ``get_enrichment_status`` carries the twin as
``enriched_episode_uuid`` -- the pair is reachable from either side.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from unittest.mock import AsyncMock

import pytest

from menhir.mcp.formatters import _format_episode_status
from menhir.mcp.tools.ops.get_provenance import GetProvenanceTool


# ---------------------------------------------------------------------------
# Tool surfaces (offline)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_enrichment_status_names_the_enriched_twin():
    row = {"processing_state": "READY", "resolved_episode_uuid": "graphiti-twin-1"}
    formatted = _format_episode_status(episode_uuid="receipt-1", row=row, history=[], timed_out=False)
    assert "episode_id: receipt-1" in formatted
    assert "enriched_episode_uuid: graphiti-twin-1" in formatted


@pytest.mark.unit
def test_enrichment_status_says_pending_until_the_twin_exists():
    row = {"processing_state": "ENRICHING"}
    formatted = _format_episode_status(episode_uuid="receipt-1", row=row, history=[], timed_out=False)
    assert "enriched_episode_uuid: (pending)" in formatted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_provenance_carries_the_receipt_per_episode():
    backend = AsyncMock()
    backend.fetch_memory_by_uuid = AsyncMock(return_value={"uuid": "entity-1"})
    backend.fetch_node_receipts = AsyncMock(
        return_value={
            "uuid": "entity-1",
            "name": "Atlas Lantern",
            "view_kind": None,
            "episodes": [
                {
                    "uuid": "graphiti-twin-1",
                    "episode_id": "receipt-1",
                    "source": "claude-code",
                    "content": "The Atlas Lantern service uses Stripe.",
                    "created_at": "2026-09-21T00:00:00Z",
                },
                # Enriched before the anchor recorded the link: still listed, honestly unresolved.
                {
                    "uuid": "graphiti-twin-legacy",
                    "episode_id": None,
                    "source": "claude-code",
                    "content": "older",
                    "created_at": "2026-07-01T00:00:00Z",
                },
            ],
            "evidence": [],
            "anchor_paths": [],
        }
    )
    tool = GetProvenanceTool()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]

    payload = json.loads(await tool.endpoint("entity-1"))

    assert payload["ok"] is True
    by_uuid = {ep["uuid"]: ep for ep in payload["episodes"]}
    assert by_uuid["graphiti-twin-1"]["episode_id"] == "receipt-1"
    assert by_uuid["graphiti-twin-legacy"]["episode_id"] is None


# ---------------------------------------------------------------------------
# The Cypher, against a real graph
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_fetch_node_receipts_resolves_the_receipt_from_the_mentioning_twin(test_neo4j_repo):
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    ns = f"issue92-{uuidlib.uuid4().hex[:8]}"
    entity = f"ent-{uuidlib.uuid4().hex}"
    twin = f"twin-{uuidlib.uuid4().hex}"
    receipt = f"receipt-{uuidlib.uuid4().hex}"
    orphan_twin = f"orphan-{uuidlib.uuid4().hex}"
    lonely = f"lonely-{uuidlib.uuid4().hex}"

    test_neo4j_repo.execute(
        """
        CREATE (n:Entity {uuid: $entity, name: 'Atlas Lantern', namespace: $ns})
        CREATE (t:Episodic {uuid: $twin, group_id: $ns, content: 'c', source: 's',
                            created_at: '2026-09-21T00:00:00Z'})
        CREATE (r:Episodic {uuid: $receipt, content: 'c', processing_state: 'READY',
                            resolved_episode_uuid: $twin})
        CREATE (o:Episodic {uuid: $orphan, group_id: $ns, content: 'o', source: 's',
                            created_at: '2026-07-01T00:00:00Z'})
        CREATE (t)-[:MENTIONS]->(n)
        CREATE (o)-[:MENTIONS]->(n)
        """,
        {"entity": entity, "twin": twin, "receipt": receipt, "orphan": orphan_twin, "ns": ns},
    )
    try:
        row = MemoryQueryRepository(test_neo4j_repo).fetch_node_receipts(entity)
        assert row is not None
        episodes = {ep["uuid"]: ep for ep in row["episodes"]}
        # The receipt itself does not MENTION anything and must not appear as an episode.
        assert set(episodes) == {twin, orphan_twin}
        assert episodes[twin]["episode_id"] == receipt
        assert episodes[orphan_twin]["episode_id"] is None
        # The other two signals are untouched by the restructure.
        assert row["evidence"] == [] and row["anchor_paths"] == []

        # A node with no mentioning episodes yields an empty list, not [null].
        test_neo4j_repo.execute("CREATE (:Entity {uuid: $u, name: 'x'})", {"u": lonely})
        lonely_row = MemoryQueryRepository(test_neo4j_repo).fetch_node_receipts(lonely)
        assert lonely_row is not None and lonely_row["episodes"] == []
    finally:
        test_neo4j_repo.execute(
            "MATCH (x) WHERE x.uuid IN $ids DETACH DELETE x",
            {"ids": [entity, twin, receipt, orphan_twin, lonely]},
        )
