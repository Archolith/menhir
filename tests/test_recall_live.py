"""Live integration tests for M3 recall against real Neo4j + Graphiti.

Marked ``online`` â€” skipped unless Neo4j, llama.cpp, and Graphiti are available.
Run with: pytest -m online tests/test_recall_live.py -v -rs
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
from menhir.domain.recall import QueryPreset, RecallResult, ScoredMemory
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
async def test_recall_returns_scored_results_for_ingested_episode() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("recall-test-user", session_id=f"test-m3-recall-{uuid4()}")
    episode = (
        f"The RecallTestProject-{session.session_id} is an AI assistant "
        "made by Anthropic. It is helpful, harmless, and honest."
    )

    try:
        await prepare_memory_runtime(built)
        ingest_result = await built.ingest_service.ingest_episode(episode, session, "recall-live-test")
        assert ingest_result.status is IngestStatus.INGESTED

        result = await built.recall_service.recall("AI assistant Anthropic", include_session=True)

        assert isinstance(result, RecallResult)
        assert len(result.results) > 0
        assert all(isinstance(r, ScoredMemory) for r in result.results)
        assert all(r.final_score > 0 for r in result.results)
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_recall_explainability_contains_all_components() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("recall-test-user", session_id=f"test-m3-explain-{uuid4()}")
    episode = (
        f"ExplainProject-{session.session_id} uses graph-based memory "
        "for long-term context in AI agents."
    )

    try:
        await prepare_memory_runtime(built)
        await built.ingest_service.ingest_episode(episode, session, "recall-live-test")

        result = await built.recall_service.recall("graph memory AI agents", include_session=True)

        assert len(result.results) > 0
        r = result.results[0]
        b = r.breakdown
        assert b.semantic_similarity >= 0
        assert b.recency_bonus >= 0
        assert b.prominence_bonus >= 0
        assert b.adjacency_bonus >= 0
        assert b.preset == "knowledge"
        assert b.alpha >= 0
        assert b.beta >= 0
        assert b.gamma >= 0
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_recall_updates_last_accessed_on_retrieval() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("recall-test-user", session_id=f"test-m3-touch-{uuid4()}")
    episode = (
        f"TouchProject-{session.session_id} tracks last-accessed timestamps "
        "for memory freshness lifecycle management."
    )

    try:
        await prepare_memory_runtime(built)
        await built.ingest_service.ingest_episode(episode, session, "recall-live-test")

        result = await built.recall_service.recall("memory freshness lifecycle", include_session=True)

        assert result.nodes_touched > 0
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_recall_with_different_presets() -> None:
    settings = MemorySettings.from_env()
    await _skip_unless_online_ready(settings)
    built = build_memory_services(settings)
    session = new_session("recall-test-user", session_id=f"test-m3-presets-{uuid4()}")
    episode = (
        f"PresetProject-{session.session_id} experiments with different "
        "retrieval scoring presets for memory ranking."
    )

    try:
        await prepare_memory_runtime(built)
        await built.ingest_service.ingest_episode(episode, session, "recall-live-test")

        for preset in QueryPreset:
            result = await built.recall_service.recall(
                "retrieval scoring presets", preset=preset, include_session=True
            )
            assert isinstance(result, RecallResult)
            assert result.preset == preset.value
    finally:
        _cleanup_session_data(built, session.session_id)
        await built.graphiti_client.close()
        built.neo4j.close()

