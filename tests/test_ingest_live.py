"""Live integration tests for M2 ingestion.

Marked ``online`` â€” skipped unless Neo4j, llama.cpp, and Graphiti are available.
Run with: pytest -m online tests/test_ingest_live.py
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
        pytest.skip("graphiti_core is not installed in the active test environment")

    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        if not repo.ping():
            pytest.skip("Neo4j is not reachable â€” skipping online tests")

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


def _cleanup_session_data(built: object, session_id: str) -> None:
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
async def test_prepare_memory_runtime_and_ingest_episode_live() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m2-live-{uuid4()}")
    episode = (
        f"The M2LiveProject-{session.session_id} service is a Spring Boot API for Pokemon card pricing."
    )

    try:
        prepared = await prepare_memory_runtime(built)
        result = await built.ingest_service.ingest_episode(episode, session, "integration-test")

        assert prepared["graphiti"] == "ok"
        assert prepared["schema"]["status"] == "ok"
        assert result.status is IngestStatus.INGESTED
        assert result.episode_id
        assert result.nodes_touched >= 1
        assert result.edges_touched >= 0

        episode_rows = built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.uuid = $episode_id
            RETURN
                labels(n) AS labels,
                n.scope AS scope,
                n.session_id AS session_id,
                n.user_id AS user_id,
                n.source AS source,
                n.user_flagged AS user_flagged
            """,
            params={"episode_id": result.episode_id},
        )

        assert len(episode_rows) == 1
        assert "Episodic" in episode_rows[0]["labels"]
        assert episode_rows[0]["scope"] == "SESSION"
        assert episode_rows[0]["session_id"] == session.session_id
        assert episode_rows[0]["user_id"] == session.user_id
        assert episode_rows[0]["source"] == "integration-test"
        assert episode_rows[0]["user_flagged"] is False

        session_rows = built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.session_id = $session_id
            RETURN count(DISTINCT n) AS nodes_in_session
            """,
            params={"session_id": session.session_id},
        )

        assert int(session_rows[0]["nodes_in_session"]) >= 1
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_flag_memory_marks_ingested_node_live() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m2-flag-{uuid4()}")
    episode = f"The project named M2FlagEntity-{session.session_id} tracks memory experiments."

    try:
        await prepare_memory_runtime(built)
        result = await built.ingest_service.ingest_episode(episode, session, "integration-test")

        candidate_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid
            LIMIT 1
            """,
            params={"episode_id": result.episode_id},
        )
        assert len(candidate_rows) == 1

        node_id = candidate_rows[0]["uuid"]
        assert built.ingest_service.flag_memory(node_id) is True

        flagged_rows = built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.uuid = $node_id
            RETURN n.user_flagged AS user_flagged
            """,
            params={"node_id": node_id},
        )

        assert len(flagged_rows) == 1
        assert flagged_rows[0]["user_flagged"] is True
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_unflag_memory_removes_retention_flag_live() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("live-user", session_id=f"test-m2-unflag-{uuid4()}")
    episode = f"The project named M2UnflagEntity-{session.session_id} tracks memory experiments."

    try:
        await prepare_memory_runtime(built)
        result = await built.ingest_service.ingest_episode(episode, session, "integration-test")

        candidate_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid
            LIMIT 1
            """,
            params={"episode_id": result.episode_id},
        )
        assert len(candidate_rows) == 1

        node_id = candidate_rows[0]["uuid"]

        # Flag it first, then verify
        assert built.ingest_service.flag_memory(node_id) is True
        flagged_rows = built.neo4j.execute(
            """
            MATCH (n) WHERE n.uuid = $node_id
            RETURN n.user_flagged AS user_flagged
            """,
            params={"node_id": node_id},
        )
        assert flagged_rows[0]["user_flagged"] is True

        # Now unflag and verify
        assert built.ingest_service.unflag_memory(node_id) is True
        unflagged_rows = built.neo4j.execute(
            """
            MATCH (n) WHERE n.uuid = $node_id
            RETURN n.user_flagged AS user_flagged
            """,
            params={"node_id": node_id},
        )
        assert unflagged_rows[0]["user_flagged"] is False
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_existing_persistent_entity_is_not_downgraded_live() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    first_session = new_session("live-user", session_id=f"test-m2-persist-a-{uuid4()}")
    second_session = new_session("live-user", session_id=f"test-m2-persist-b-{uuid4()}")
    entity_name = f"M2PersistentEntity-{uuid4()}"
    entity_uuid: str | None = None

    try:
        await prepare_memory_runtime(built)
        first_result = await built.ingest_service.ingest_episode(
            f"{entity_name} is a project for memory graph experiments.",
            first_session,
            "integration-test",
        )
        assert first_result.status is IngestStatus.INGESTED

        first_entity_rows = built.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_id})-[:MENTIONS]-(n:Entity)
            RETURN n.uuid AS uuid
            LIMIT 1
            """,
            params={"episode_id": first_result.episode_id},
        )
        assert len(first_entity_rows) == 1
        entity_uuid = first_entity_rows[0]["uuid"]

        built.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid = $entity_uuid
            SET n.scope = 'PERSISTENT',
                n.freshness = 'ACTIVE',
                n.session_id = null
            """,
            params={"entity_uuid": entity_uuid},
        )

        second_result = await built.ingest_service.ingest_episode(
            f"I updated {entity_name} yesterday with a new retrieval workflow.",
            second_session,
            "integration-test",
        )
        assert second_result.status is IngestStatus.INGESTED

        persisted_rows = built.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid = $entity_uuid
            RETURN n.scope AS scope, n.freshness AS freshness, n.session_id AS session_id
            """,
            params={"entity_uuid": entity_uuid},
        )
        assert len(persisted_rows) == 1
        assert persisted_rows[0]["scope"] == "PERSISTENT"
        assert persisted_rows[0]["freshness"] == "ACTIVE"
        assert persisted_rows[0]["session_id"] is None

        second_session_rows = built.neo4j.execute(
            """
            MATCH (n)
            WHERE n.session_id = $session_id
            RETURN count(DISTINCT n) AS nodes_in_session
            """,
            params={"session_id": second_session.session_id},
        )
        assert int(second_session_rows[0]["nodes_in_session"]) >= 1
    finally:
        _cleanup_session_data(built, first_session.session_id)
        _cleanup_session_data(built, second_session.session_id)
        if entity_uuid is not None:
            built.neo4j.execute(
                """
                MATCH (n)
                WHERE n.uuid = $entity_uuid
                DETACH DELETE n
                """,
                params={"entity_uuid": entity_uuid},
            )
        await built.graphiti_client.close()
        built.neo4j.close()

