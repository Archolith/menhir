"""Live integration tests for M2.5 session consolidation.

Marked ``online`` â€” skipped unless Neo4j, llama.cpp, and Graphiti are available.
Run with: pytest -m online tests/test_lifecycle_live.py -q -rs
"""

from __future__ import annotations

import importlib.util
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from dotenv import load_dotenv

from menhir.config import MemorySettings
from menhir.core import build_memory_services, prepare_memory_runtime
from menhir.domain import IngestStatus, new_session
from menhir.infrastructure import Neo4jRepository
from menhir.services.lifecycle_service import (
    ConsolidationResult,
    SHARPNESS_PROMOTE_THRESHOLD,
)

load_dotenv()


def _check_llama_connectivity(
    *,
    base_url: str,
    api_key: str,
    chat_model: str,
    embed_model: str,
) -> bool:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    models_url = base_url.rstrip("/") + "/models"
    try:
        request = Request(models_url, headers=headers, method="GET")
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        available = {
            str(item.get("id"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
        return all(required in available for required in (chat_model, embed_model))
    except (HTTPError, URLError, ValueError, json.JSONDecodeError):
        return False


async def _skip_unless_online_ready(settings: MemorySettings) -> None:
    if importlib.util.find_spec("graphiti_core") is None:
        pytest.skip("graphiti_core is not installed")

    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        if not repo.ping():
            pytest.skip("Neo4j is not reachable")
        lm_ok = _check_llama_connectivity(
            base_url=settings.local_llm_base_url,
            api_key=settings.local_llm_api_key,
            chat_model=settings.local_llm_chat_model,
            embed_model=settings.local_llm_embed_model,
        )
        if not lm_ok:
            pytest.skip("llama.cpp is not reachable or required models are missing")
    finally:
        repo.close()


def _cleanup_session_data(built, session_id: str) -> None:
    built.neo4j.execute(
        """
        MATCH (n)
        WHERE n.session_id = $session_id
        DETACH DELETE n
        """,
        params={"session_id": session_id},
    )


@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidate_promotes_ingested_session_node() -> None:
    """Ingest an episode, then consolidate â€” the unique entity should be promoted."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-promote-{uuid4()}")
    unique_name = f"M25UniqueEntity-{uuid4()}"
    episode = f"{unique_name} is a one-of-a-kind project for testing consolidation."

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "integration-test")
        assert ingest_result.status is IngestStatus.INGESTED

        # Verify entity starts as SESSION
        entity_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid, n.scope AS scope
            LIMIT 1
            """,
            params={"episode_id": ingest_result.episode_id},
        )
        assert len(entity_rows) >= 1
        entity_uuid = entity_rows[0]["uuid"]
        assert entity_rows[0]["scope"] == "SESSION"

        # Run consolidation
        result = await built.lifecycle_service.consolidate_session(session.session_id)

        assert isinstance(result, ConsolidationResult)
        assert result.promoted >= 1

        # Verify entity is now PERSISTENT
        promoted_rows = built.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            RETURN n.scope AS scope
            """,
            params={"uuid": entity_uuid},
        )
        assert len(promoted_rows) == 1
        assert promoted_rows[0]["scope"] == "PERSISTENT"
    finally:
        _cleanup_session_data(built, session.session_id)
        # Also clean up any promoted nodes (no longer have session_id)
        built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.name CONTAINS $unique_name
            DETACH DELETE n
            """,
            params={"unique_name": unique_name},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidate_flagged_node_always_promoted() -> None:
    """A flagged SESSION node should always be promoted regardless of sharpness."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-flagged-{uuid4()}")
    unique_name = f"M25FlaggedEntity-{uuid4()}"
    episode = f"{unique_name} stores important project decisions."

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "integration-test")
        assert ingest_result.status is IngestStatus.INGESTED

        # Find and flag the entity
        entity_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid
            LIMIT 1
            """,
            params={"episode_id": ingest_result.episode_id},
        )
        assert len(entity_rows) >= 1
        entity_uuid = entity_rows[0]["uuid"]
        assert built.ingest_service.flag_memory(entity_uuid) is True

        # Consolidate
        result = await built.lifecycle_service.consolidate_session(session.session_id)
        assert result.promoted >= 1

        # Verify promoted
        promoted_rows = built.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            RETURN n.scope AS scope, n.user_flagged AS flagged
            """,
            params={"uuid": entity_uuid},
        )
        assert len(promoted_rows) == 1
        assert promoted_rows[0]["scope"] == "PERSISTENT"
        assert promoted_rows[0]["flagged"] is True
    finally:
        _cleanup_session_data(built, session.session_id)
        built.neo4j.execute(
            "MATCH (n) WHERE n.name CONTAINS $name DETACH DELETE n",
            params={"name": unique_name},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidate_updates_sharpness_on_entity() -> None:
    """Consolidation should compute and store sharpness on session entities."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-sharpness-{uuid4()}")
    unique_name = f"M25SharpnessEntity-{uuid4()}"
    episode = f"{unique_name} is an extremely unique concept for sharpness testing."

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "integration-test")
        assert ingest_result.status is IngestStatus.INGESTED

        entity_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid, n.sharpness AS sharpness
            LIMIT 1
            """,
            params={"episode_id": ingest_result.episode_id},
        )
        assert len(entity_rows) >= 1
        entity_uuid = entity_rows[0]["uuid"]
        initial_sharpness = entity_rows[0].get("sharpness")

        # Consolidate
        await built.lifecycle_service.consolidate_session(session.session_id)

        # Check sharpness was updated
        updated_rows = built.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            RETURN n.sharpness AS sharpness
            """,
            params={"uuid": entity_uuid},
        )
        assert len(updated_rows) == 1
        new_sharpness = updated_rows[0]["sharpness"]
        # Sharpness should have been computed (not None/0 default)
        assert new_sharpness is not None
        assert new_sharpness > 0
    finally:
        _cleanup_session_data(built, session.session_id)
        built.neo4j.execute(
            "MATCH (n) WHERE n.name CONTAINS $name DETACH DELETE n",
            params={"name": unique_name},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidation_is_idempotent_live() -> None:
    """Running consolidation twice should not double-promote or error."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-idempotent-{uuid4()}")
    unique_name = f"M25IdempotentEntity-{uuid4()}"
    episode = f"{unique_name} tests that consolidation is safe to run repeatedly."

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "integration-test")
        assert ingest_result.status is IngestStatus.INGESTED

        # Use session_id=None to consolidate all SESSION nodes â€” Graphiti
        # doesn't always propagate our session_id to extracted entities.
        r1 = await built.lifecycle_service.consolidate_session()

        # Second run â€” previously SESSION nodes are now PERSISTENT, so
        # this should be a no-op (or at most process other stale nodes).
        r2 = await built.lifecycle_service.consolidate_session(session.session_id)
        # The key invariant: running twice doesn't error and the second
        # run processes fewer or equal nodes.
        assert r2.promoted <= r1.promoted
    finally:
        _cleanup_session_data(built, session.session_id)
        built.neo4j.execute(
            "MATCH (n) WHERE n.name CONTAINS $name DETACH DELETE n",
            params={"name": unique_name},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_orphan_recovery_promotes_old_session_nodes() -> None:
    """recover_orphans() should promote SESSION nodes older than the threshold."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-orphan-{uuid4()}")
    unique_name = f"M25OrphanEntity-{uuid4()}"
    episode = f"{unique_name} simulates a stale session node for orphan recovery."

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "integration-test")
        assert ingest_result.status is IngestStatus.INGESTED

        # Backdate entity created_at to simulate an old orphan
        entity_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid
            LIMIT 1
            """,
            params={"episode_id": ingest_result.episode_id},
        )
        assert len(entity_rows) >= 1
        entity_uuid = entity_rows[0]["uuid"]

        built.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            SET n.created_at = datetime() - duration({hours: 10})
            """,
            params={"uuid": entity_uuid},
        )

        # Orphan recovery with a short age threshold
        result = await built.lifecycle_service.recover_orphans(max_age_hours=1.0)

        assert isinstance(result, ConsolidationResult)
        # The old node should have been evaluated (promoted or deleted)
        assert result.promoted + result.deleted >= 1
    finally:
        _cleanup_session_data(built, session.session_id)
        built.neo4j.execute(
            "MATCH (n) WHERE n.name CONTAINS $name DETACH DELETE n",
            params={"name": unique_name},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_dense_episode_enrichment_extracts_entities() -> None:
    """A long, dense bullet-point status update must extract at least one entity.

    Regression test for a real failure where Graphiti returned zero nodes/edges
    from a 1800-char M4 progress update, and the pipeline silently marked it READY.
    With the zero-extraction failsafe, this should now be caught and retried.
    """
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-dense-enrichment-{uuid4()}")

    # This is the actual text that caused zero extraction in production.
    dense_episode = (
        "M4 Lifecycle & Decay progress update (2026-03-07):\n\n"
        "COMPLETED:\n"
        "- M2.5 session consolidation fully done (all 8 criteria met, including skipped_pending)\n"
        "- M4 Phase 1: edge count sync + gamma activation (sync_edge_counts in "
        "prepare_memory_runtime, PRESET_WEIGHTS gamma restored to 0.1)\n"
        "- M4 Phase 2: core decay pipeline (_run_decay, should_compress, should_delete, "
        "DecayResult, fetch_decay_candidates, compress_node, bridge_and_delete, "
        "cleanup_orphan_subgraphs)\n"
        "- LLM compression: LLMAdapter.compress_content() with retry logic (3 attempts, "
        "3s delay), thinking tag stripping for Qwen, wired into decay pipeline\n"
        "- Removed truncation fallback: if LLM fails or is unavailable, node is queued "
        "in pending_actions SQLite table for retry next cycle, content is never destroyed\n"
        "- PendingActionStore: SQLite-backed work queue (pending_actions table) for "
        "deferred LLM operations, replaces needs_compression graph property\n\n"
        "REMAINING M4 WORK:\n"
        "- Rehydration core (lifecycle_service.rehydrate_node)\n"
        "- Retrieval-side rehydration\n"
        "- Ingestion-side rehydration\n"
        "- Wire lifecycle_service into RecallService and IngestService via bootstrap\n"
        "- Pipeline runner async fix\n"
        "- Telemetry for decay/rehydration events\n"
        "- Live integration tests\n"
        "- Update roadmap + changelog"
    )

    try:
        await prepare_memory_runtime(built)
        result = await built.ingest_service.queue_episode_for_enrichment(
            dense_episode, session, "integration-test",
        )
        assert result.status is IngestStatus.QUEUED
        episode_uuid = result.episode_id

        # Wait for background enrichment â€” 180s to allow llama.cpp model reloads
        processing = await built.ingest_service.wait_for_episode_processing(
            episode_uuid, timeout_s=180.0, poll_interval_s=2.0,
        )
        assert processing is not None, "Episode processing timed out (returned None)"

        state = str(processing.get("processing_state") or "")
        error = str(processing.get("processing_error") or "")
        attempts = int(processing.get("processing_attempts") or 0)

        if state == "ENRICHING":
            pytest.fail(
                f"Episode still ENRICHING after 180s timeout (attempts={attempts}). "
                f"llama.cpp likely crashed or reloaded mid-extraction."
            )

        if state == "FAILED" and error == "zero_extraction":
            pytest.fail(
                f"Zero-extraction detected: Graphiti returned no entities from "
                f"{len(dense_episode)}-char episode (attempts={attempts}). "
                f"The failsafe caught it, but the LLM extraction needs investigation."
            )

        assert state == "READY", f"Expected READY, got {state} (error={error}, attempts={attempts})"

        if attempts > 1:
            import warnings
            warnings.warn(
                f"Episode required {attempts} enrichment attempts â€” "
                f"first attempt(s) likely failed due to LLM instability",
                stacklevel=1,
            )

        # Verify at least one entity was linked
        resolved_uuid = str(processing.get("resolved_episode_uuid") or episode_uuid)
        entity_uuids = built.graph_adapter.fetch_linked_entity_uuids_for_episode(resolved_uuid)
        assert len(entity_uuids) >= 1, (
            f"Expected at least 1 linked entity, got {len(entity_uuids)}. "
            f"resolved_episode_uuid={resolved_uuid}, attempts={attempts}. "
            f"Graphiti may have failed to extract from dense bullet-point text."
        )
    finally:
        _cleanup_session_data(built, session.session_id)
        # Clean up any entities created with M4-related names
        built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.session_id = $session_id
               OR (n.name IS NOT NULL AND n.name CONTAINS 'M4')
            WITH n WHERE n.session_id = $session_id
            DETACH DELETE n
            """,
            params={"session_id": session.session_id},
        )
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_consolidation_result_has_all_fields() -> None:
    """ConsolidationResult should carry all expected counters."""
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m25-fields-{uuid4()}")

    try:
        await prepare_memory_runtime(built)
        result = await built.lifecycle_service.consolidate_session(session.session_id)

        assert isinstance(result, ConsolidationResult)
        assert hasattr(result, "promoted")
        assert hasattr(result, "deleted")
        assert hasattr(result, "conflicts_detected")
        assert hasattr(result, "skipped_pending")
        assert hasattr(result, "orphan_episodes_cleaned")
        assert result.promoted >= 0
        assert result.deleted >= 0
        assert result.conflicts_detected >= 0
    finally:
        await built.graphiti_client.close()
        built.neo4j.close()

