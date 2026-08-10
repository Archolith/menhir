"""Unit tests for the Graphiti client wrapper."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import sys
from types import ModuleType

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_helpers import (
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
)
from menhir.infrastructure.scheduler_trace import build_episode_scheduler_task
import menhir.infrastructure.graphiti_client as graphiti_client_module


class _DummyTemporalValue:
    def iso_format(self) -> str:
        return "2026-03-06T12:00:00+00:00"


class _DummyOpenAIGenericClient:
    def __init__(self, *, config: object, client: object | None = None, max_tokens: int | None = None) -> None:
        self.config = config
        self.client = client
        self.max_tokens = max_tokens


class _DummyLLMConfig:
    def __init__(self, *, api_key: str, base_url: str, model: str, temperature: float = 1.0) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature


class _DummyOpenAIEmbedder:
    def __init__(self, *, config: object, client: object | None = None) -> None:
        self.config = config
        self.client = client


class _DummyOpenAIEmbedderConfig:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        embedding_model: str,
        embedding_dim: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim


class _DummyNeo4jDriver:
    def __init__(self, *, uri: str, user: str, password: str, database: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database


class _DummyOpenAIRerankerClient:
    def __init__(self, *, config: object, client: object | None = None) -> None:
        self.config = config
        self.client = client


class _DummyGraphiti:
    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        graph_driver: object,
        llm_client: object,
        embedder: object,
        cross_encoder: object | None = None,
    ) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.graph_driver = graph_driver
        self.llm_client = llm_client
        self.embedder = embedder
        self.cross_encoder = cross_encoder
        self.indices_calls = 0
        self.add_episode_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def build_indices_and_constraints(self) -> None:
        self.indices_calls += 1

    async def add_episode(self, **kwargs: object) -> dict[str, object]:
        self.add_episode_calls.append(kwargs)
        return {"ok": True, "kind": "episode"}

    async def search(self, query: str, **kwargs: object) -> dict[str, object]:
        self.search_calls.append({"query": query, "kwargs": kwargs})
        return {"ok": True, "kind": "search"}

    async def close(self) -> None:
        self.close_calls += 1


class _DummyNode:
    def __init__(self, *, uuid: str, name: str, labels: list[str] | None = None) -> None:
        self.uuid = uuid
        self.name = name
        self.labels = labels or ["Entity"]


class _DummySearchResults:
    def __init__(self, nodes: list[object], scores: list[float]) -> None:
        self.nodes = nodes
        self.node_reranker_scores = scores


class _DummyOpenAIMessage:
    def __init__(self, *, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _DummyChatResponseMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _DummyChatResponseChoice:
    def __init__(self, *, content: str) -> None:
        self.message = _DummyChatResponseMessage(content=content)


class _DummyChatResponse:
    def __init__(self, *, content: str) -> None:
        self.id = "resp-1"
        self.status_code = 200
        self.choices = [_DummyChatResponseChoice(content=content)]


class _DummyChatCompletions:
    def __init__(self, *, response: _DummyChatResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: object,
    ) -> _DummyChatResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        return self.response


class _DummyOpenAIChat:
    def __init__(self, *, response: _DummyChatResponse) -> None:
        self.completions = _DummyChatCompletions(response=response)


def _stub_search_config(monkeypatch: pytest.MonkeyPatch) -> None:
    search_config_module = ModuleType("graphiti_core.search.search_config")

    class _NodeSearchMethod:
        bm25 = "bm25"
        cosine_similarity = "cosine_similarity"

    class _NodeReranker:
        rrf = "rrf"

    class _NodeSearchConfig:
        def __init__(self, *, search_methods: list[object], reranker: object) -> None:
            self.search_methods = search_methods
            self.reranker = reranker

    class _SearchConfig:
        def __init__(self, *, node_config: object, limit: int) -> None:
            self.node_config = node_config
            self.limit = limit

    search_config_module.NodeSearchMethod = _NodeSearchMethod
    search_config_module.NodeReranker = _NodeReranker
    search_config_module.NodeSearchConfig = _NodeSearchConfig
    search_config_module.SearchConfig = _SearchConfig

    graphiti_core_module = ModuleType("graphiti_core")
    search_package = ModuleType("graphiti_core.search")
    search_package.search_config = search_config_module
    graphiti_core_module.search = search_package

    monkeypatch.setitem(sys.modules, "graphiti_core", graphiti_core_module)
    monkeypatch.setitem(sys.modules, "graphiti_core.search", search_package)
    monkeypatch.setitem(sys.modules, "graphiti_core.search.search_config", search_config_module)


@pytest.mark.unit
def test_graphiti_client_from_settings_builds_expected_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _DummyOpenAIGenericClient)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    observed_client = object()
    monkeypatch.setattr(
        graphiti_client_module,
        "build_async_openai_client",
        lambda *, base_url, api_key, settings, embedding_cache=None: observed_client,
    )

    settings = MemorySettings(
        neo4j_uri="bolt://db:7687",
        neo4j_database="gemini-test",
        neo4j_user="neo-user",
        neo4j_password="secret",
        local_llm_base_url="http://local-llm:1234/v1",
        local_llm_api_key="local-key",
        local_llm_chat_model="chat-model",
        local_llm_embed_model="embed-model",
    )

    wrapper = GraphitiClient.from_settings(settings)

    assert isinstance(wrapper.client, _DummyGraphiti)
    assert wrapper.client.uri == "bolt://db:7687"
    assert isinstance(wrapper.client.graph_driver, _DummyNeo4jDriver)
    assert wrapper.client.graph_driver.database == "gemini-test"
    assert wrapper.client.user == "neo-user"
    assert wrapper.client.password == "secret"
    assert wrapper.client.llm_client.config.base_url == "http://local-llm:1234/v1"
    assert wrapper.client.llm_client.config.api_key == "local-key"
    assert wrapper.client.llm_client.config.model == "chat-model"
    assert wrapper.client.llm_client.client is observed_client
    assert wrapper.client.embedder.config.base_url == "http://local-llm:1234/v1"
    assert wrapper.client.embedder.config.api_key == "local-key"
    assert wrapper.client.embedder.config.embedding_model == "embed-model"
    assert wrapper.client.embedder.client is observed_client
    assert isinstance(wrapper.client.cross_encoder, _DummyOpenAIRerankerClient)
    assert wrapper.client.cross_encoder.config.base_url == "http://local-llm:1234/v1"
    assert wrapper.client.cross_encoder.config.api_key == "local-key"
    assert wrapper.client.cross_encoder.config.model == "chat-model"
    assert wrapper.client.cross_encoder.client is observed_client
    assert wrapper.reranker_provider_kind == "local"


@pytest.mark.unit
def test_graphiti_client_pins_llm_temperature_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Graphiti DEFAULT_TEMPERATURE=1 caused ~3% stochastic entity conflation.

    The production GraphitiClient must construct LLMConfig with temperature=0
    to substantially reduce sampling variance in extraction and dedup calls.
    """
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _DummyOpenAIGenericClient)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    monkeypatch.setattr(
        graphiti_client_module,
        "build_async_openai_client",
        lambda *, base_url, api_key, settings, embedding_cache=None: object(),
    )

    settings = MemorySettings(
        neo4j_uri="bolt://db:7687",
        neo4j_database="test",
        neo4j_user="neo",
        neo4j_password="secret",
        local_llm_base_url="http://llm:1234/v1",
        local_llm_api_key="key",
        local_llm_chat_model="model",
        local_llm_embed_model="embed",
    )

    wrapper = GraphitiClient.from_settings(settings)

    llm_config = wrapper.client.llm_client.config
    assert llm_config.temperature == 0, (
        f"LLMConfig temperature must be pinned to 0 to prevent stochastic entity "
        f"conflation (Graphiti default is 1). Got {llm_config.temperature}"
    )


@pytest.mark.unit
def test_graphiti_client_rejects_non_openai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)

    settings = MemorySettings(graphiti_provider="gemini")

    with pytest.raises(NotImplementedError):
        GraphitiClient.from_settings(settings)


@pytest.mark.unit
def test_graphiti_client_supports_openai_llm_with_local_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _DummyOpenAIGenericClient)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    observed_clients: list[tuple[str, str, object]] = []

    def _fake_build_async_openai_client(*, base_url: str, api_key: str, settings: object, embedding_cache=None) -> object:
        client = object()
        observed_clients.append((base_url, api_key, client))
        return client

    monkeypatch.setattr(graphiti_client_module, "build_async_openai_client", _fake_build_async_openai_client)

    settings = MemorySettings(
        graphiti_provider="openai",
        graphiti_embed_provider="local",
        openai_api_key="openai-key",
        openai_chat_model="gpt-5-mini",
        local_llm_base_url="http://localhost:1234/v1",
        local_llm_api_key="local-key",
        local_llm_embed_model="bge-base",
        local_llm_embed_base_url="http://localhost:1235/v1",
    )

    wrapper = GraphitiClient.from_settings(settings)

    assert wrapper.client.llm_client.config.base_url == "https://api.openai.com/v1"
    assert wrapper.client.llm_client.config.api_key == "openai-key"
    assert wrapper.client.llm_client.config.model == "gpt-5-mini"
    assert wrapper.client.embedder.config.base_url == "http://localhost:1235/v1"
    assert wrapper.client.embedder.config.api_key == "local-key"
    assert wrapper.client.embedder.config.embedding_model == "bge-base"
    assert observed_clients[0][0] == "https://api.openai.com/v1"
    assert observed_clients[1][0] == "http://localhost:1235/v1"
    # reranker inherits from the graphiti LLM provider (openai) by default
    assert wrapper.client.cross_encoder.config.base_url == "https://api.openai.com/v1"
    assert wrapper.reranker_provider_kind == "openai"


@pytest.mark.unit
def test_graphiti_client_sets_openai_embedding_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _DummyOpenAIGenericClient)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    monkeypatch.setattr(
        graphiti_client_module,
        "build_async_openai_client",
        lambda *, base_url, api_key, settings, embedding_cache=None: object(),
    )

    wrapper = GraphitiClient.from_settings(
        MemorySettings(
            graphiti_provider="openai",
            openai_api_key="openai-key",
            openai_embed_model="text-embedding-3-small",
        )
    )

    assert wrapper.client.embedder.config.embedding_dim == 1536


@pytest.mark.unit
def test_safe_to_prompt_json_serializes_temporal_values() -> None:
    payload = {"created_at": _DummyTemporalValue()}

    result = graphiti_client_module._safe_to_prompt_json(payload)

    assert result == '{"created_at": "2026-03-06T12:00:00+00:00"}'


@pytest.mark.unit
def test_extract_first_json_payload_handles_fenced_json() -> None:
    raw = "```json\n{\"ok\": true}\n```"
    assert _extract_first_json_payload(raw) == "{\"ok\": true}"


@pytest.mark.unit
def test_extract_first_json_payload_rejects_empty_response() -> None:
    with pytest.raises(ValueError, match="empty response"):
        _extract_first_json_payload("   ")


@pytest.mark.unit
def test_raw_preview_compacts_whitespace_and_truncates() -> None:
    raw = "  line1 \n  line2   " + ("x" * 600)

    preview = _raw_preview(raw, limit=40)

    assert preview.startswith("line1 line2 ")
    assert preview.endswith("...")
    assert len(preview) == 40


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_openai_generic_client_logs_request_and_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _PatchedOpenAIGenericClient:
        def __init__(self, *, config: object, client: object | None = None, max_tokens: int | None = None) -> None:
            self.config = config
            self.client = client
            self.max_tokens = max_tokens or 128
            self.model = "chat-model"
            self.temperature = 0.1

        def _clean_input(self, value: str) -> str:
            return value

    class _PatchedChatClient:
        def __init__(self) -> None:
            self.chat = _DummyOpenAIChat(response=_DummyChatResponse(content='{"ok": true}'))

    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _PatchedOpenAIGenericClient)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_generate_response", raising=False)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_yawn_patched", raising=False)
    graphiti_client_module._patch_graphiti_openai_generic_client()

    llm_client = _PatchedOpenAIGenericClient(
        config=object(),
        client=_PatchedChatClient(),
        max_tokens=128,
    )

    with caplog.at_level(logging.DEBUG):
        response = await llm_client._generate_response(
            messages=[_DummyOpenAIMessage(role="user", content="hello")],
            max_tokens=64,
        )

    assert response == {"ok": True}
    assert "Graphiti OpenAI-compatible request begin" in caplog.text
    assert "Graphiti OpenAI-compatible response received" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_openai_generic_client_logs_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _PatchedOpenAIGenericClient:
        def __init__(self, *, config: object, client: object | None = None, max_tokens: int | None = None) -> None:
            self.config = config
            self.client = client
            self.max_tokens = max_tokens or 128
            self.model = "chat-model"
            self.temperature = 0.1

        def _clean_input(self, value: str) -> str:
            return value

    class _PatchedChatClient:
        def __init__(self) -> None:
            self.chat = _DummyOpenAIChat(response=_DummyChatResponse(content="bad-json"))

    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _PatchedOpenAIGenericClient)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_generate_response", raising=False)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_yawn_patched", raising=False)
    graphiti_client_module._patch_graphiti_openai_generic_client()

    llm_client = _PatchedOpenAIGenericClient(
        config=object(),
        client=_PatchedChatClient(),
        max_tokens=128,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="not valid JSON") as exc_info:
        await llm_client._generate_response(
            messages=[_DummyOpenAIMessage(role="user", content="hello")],
            max_tokens=64,
        )

    assert "Graphiti OpenAI-compatible request begin" in caplog.text
    assert "Graphiti OpenAI-compatible response parse failure" in caplog.text
    assert exc_info.value.menhir_failure_details["graphiti_prompt_preview"] == "user: hello"
    assert exc_info.value.menhir_failure_details["graphiti_raw_response_preview"] == "bad-json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_openai_generic_client_attaches_empty_response_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PatchedOpenAIGenericClient:
        def __init__(self, *, config: object, client: object | None = None, max_tokens: int | None = None) -> None:
            self.config = config
            self.client = client
            self.max_tokens = max_tokens or 128
            self.model = "chat-model"
            self.temperature = 0.1

        def _clean_input(self, value: str) -> str:
            return value

    class _PatchedChatClient:
        def __init__(self) -> None:
            self.chat = _DummyOpenAIChat(response=_DummyChatResponse(content="   "))

    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _PatchedOpenAIGenericClient)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_generate_response", raising=False)
    monkeypatch.delattr(_PatchedOpenAIGenericClient, "_yawn_patched", raising=False)
    graphiti_client_module._patch_graphiti_openai_generic_client()

    llm_client = _PatchedOpenAIGenericClient(
        config=object(),
        client=_PatchedChatClient(),
        max_tokens=128,
    )

    with pytest.raises(ValueError, match="empty response") as exc_info:
        await llm_client._generate_response(
            messages=[_DummyOpenAIMessage(role="system", content="sys"), _DummyOpenAIMessage(role="user", content="hello")],
            max_tokens=64,
        )

    assert exc_info.value.menhir_failure_details["graphiti_prompt_preview"] == "system: sys | user: hello"
    assert exc_info.value.menhir_failure_details["graphiti_raw_response_preview"] == ""
    assert exc_info.value.menhir_failure_details["graphiti_raw_response_length"] == 3


@pytest.mark.unit
def test_normalize_graphiti_json_payload_maps_entity_keys_to_name() -> None:
    payload = {
        "extracted_entities": [
            {"entity": "git", "entity_type_id": 1},
            {"entity_name": "clean", "entity_type_id": 1},
        ]
    }

    normalized = _normalize_graphiti_json_payload(payload)

    assert normalized == {
        "extracted_entities": [
            {"name": "git", "entity_type_id": 1},
            {"name": "clean", "entity_type_id": 1},
        ]
    }


@pytest.mark.unit
def test_normalize_graphiti_json_payload_maps_entity_type() -> None:
    payload = {
        "extracted_entities": [
            {"name": "yawn.scheduler", "type": 0},
            {"entity": "agent", "type": 1},
            {"entity_name": "graphiti", "type": 2},
        ]
    }

    normalized = _normalize_graphiti_json_payload(payload)

    assert normalized == {
        "extracted_entities": [
            {"name": "yawn.scheduler", "entity_type_id": 0},
            {"name": "agent", "entity_type_id": 1},
            {"name": "graphiti", "entity_type_id": 2},
        ]
    }


@pytest.mark.unit
def test_normalize_graphiti_json_payload_synthesizes_missing_edge_fact_from_relation() -> None:
    payload = {
        "edges": [
            {
                "source_entity_name": "memory server",
                "target_entity_name": "neo4j",
                "relation_type": "USES_BACKEND",
                "valid_at": "2026-03-11T04:10:46Z",
            }
        ]
    }

    normalized = _normalize_graphiti_json_payload(payload)

    assert normalized == {
        "edges": [
            {
                "source_entity_name": "memory server",
                "target_entity_name": "neo4j",
                "relation_type": "USES_BACKEND",
                "fact": "[synthetic] memory server uses backend neo4j",
                "valid_at": "2026-03-11T04:10:46Z",
            }
        ]
    }


@pytest.mark.unit
def test_normalize_graphiti_json_payload_prefers_alternate_edge_text_before_synthesizing() -> None:
    payload = {
        "edges": [
            {
                "source_entity_name": "memory server",
                "target_entity_name": "neo4j",
                "relation_type": "USES_BACKEND",
                "relationship": "memory server uses Neo4j as its backend",
            }
        ]
    }

    normalized = _normalize_graphiti_json_payload(payload)

    assert normalized == {
        "edges": [
            {
                "source_entity_name": "memory server",
                "target_entity_name": "neo4j",
                "relation_type": "USES_BACKEND",
                "relationship": "memory server uses Neo4j as its backend",
                "fact": "memory server uses Neo4j as its backend",
            }
        ]
    }


@pytest.mark.unit
def test_build_episode_scheduler_task_uses_full_episode_uuid() -> None:
    task = build_episode_scheduler_task(
        episode_uuid="123e4567-e89b-12d3-a456-426614174000",
        provider="graphiti",
        action="add-episode",
    )

    assert task == "memory-123e4567e89b12d3a456426614174000--graphiti-add-episode"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_indices_and_constraints_runs_once_unless_forced() -> None:
    client = _DummyGraphiti(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(client=client)

    await wrapper.build_indices_and_constraints()
    await wrapper.build_indices_and_constraints()
    await wrapper.build_indices_and_constraints(force=True)

    assert client.indices_calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_episode_uses_episode_uuid_for_telemetry_only_not_graphiti_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DummyGraphiti(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(client=client)
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))
    monkeypatch.setattr(wrapper, "_await_add_episode_request", lambda **kwargs: kwargs["awaitable"])

    await wrapper.add_episode(
        name="episode-session-1-pending-1",
        episode_body="first event",
        source_description="unit-test",
        reference_time=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
        episode_uuid="pending-1",
        attempt=2,
    )

    assert client.add_episode_calls == [
        {
            "name": "episode-session-1-pending-1",
            "episode_body": "first event",
            "source_description": "unit-test",
            "reference_time": datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
            "group_id": "",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_client_delegates_episode_search_and_close() -> None:
    client = _DummyGraphiti(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(client=client)
    reference_time = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)

    episode_result = await wrapper.add_episode(
        name="episode-1",
        episode_body="first event",
        source_description="unit-test",
        reference_time=reference_time,
    )
    search_result = await wrapper.search("first event", limit=5)
    await wrapper.close()

    assert episode_result == {"ok": True, "kind": "episode"}
    assert search_result == {"ok": True, "kind": "search"}
    assert client.add_episode_calls == [
        {
            "name": "episode-1",
            "episode_body": "first event",
            "source_description": "unit-test",
            "reference_time": reference_time,
            "group_id": "",
        }
    ]
    assert client.search_calls == [{"query": "first event", "kwargs": {"limit": 5}}]
    assert client.close_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_episode_watchdog_fails_when_scheduler_goes_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HungClient(_DummyGraphiti):
        async def add_episode(self, **kwargs: object) -> dict[str, object]:
            await asyncio.sleep(60)
            return {"ok": True, "kind": "episode"}

    client = _HungClient(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(
        client=client,
        scheduler_fallback_base_url="http://127.0.0.1:8081/v1",
        scheduler_request_stall_timeout_s=0.1,
    )
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))

    async def _idle_status() -> dict[str, object]:
        return {
            "busy": False,
            "slot_active": False,
            "active_proxy_connections": 0,
            "current_task": None,
        }

    monkeypatch.setattr(wrapper, "_fetch_scheduler_status", _idle_status)

    with pytest.raises(TimeoutError, match="stalled after scheduler request went idle"):
        await wrapper.add_episode(
            name="episode-1",
            episode_body="first event",
            source_description="unit-test",
            reference_time=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
            episode_uuid="episode-uuid",
            attempt=1,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_episode_watchdog_fails_when_scheduler_status_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HungClient(_DummyGraphiti):
        async def add_episode(self, **kwargs: object) -> dict[str, object]:
            await asyncio.sleep(60)
            return {"ok": True, "kind": "episode"}

    client = _HungClient(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(
        client=client,
        scheduler_fallback_base_url="http://127.0.0.1:8081/v1",
        scheduler_request_stall_timeout_s=0.1,
    )
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))
    monkeypatch.setattr(wrapper, "_fetch_scheduler_status", lambda: asyncio.sleep(0, result=None))

    with pytest.raises(TimeoutError, match="scheduler status was unavailable"):
        await wrapper.add_episode(
            name="episode-1",
            episode_body="first event",
            source_description="unit-test",
            reference_time=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
            episode_uuid="episode-uuid",
            attempt=1,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_scheduler_status_uses_watchdog_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"state": "running", "current_task": "memory-task"}

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str):
            seen["url"] = url
            return _Response()

    wrapper = GraphitiClient(client=object())
    monkeypatch.setattr(graphiti_client_module.httpx, "AsyncClient", lambda timeout=0: _Client(timeout=timeout))
    monkeypatch.setattr(graphiti_client_module, "scheduler_url_from_env", lambda: "http://127.0.0.1:8082")

    payload = await wrapper._fetch_scheduler_status()

    assert payload == {"state": "running", "current_task": "memory-task"}
    assert seen == {"timeout": 3.0, "url": "http://127.0.0.1:8082/watchdog-status"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_episode_bypasses_scheduler_watchdog_for_non_scheduler_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DummyGraphiti(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(
        client=client,
        scheduler_fallback_base_url="https://api.openai.com/v1",
    )
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))

    async def _unexpected_status() -> dict[str, object] | None:
        raise AssertionError("scheduler status should not be polled for non-scheduler endpoints")

    monkeypatch.setattr(wrapper, "_fetch_scheduler_status", _unexpected_status)

    result = await wrapper.add_episode(
        name="episode-1",
        episode_body="first event",
        source_description="unit-test",
        reference_time=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
        episode_uuid="episode-uuid",
        attempt=1,
    )

    assert result == {"ok": True, "kind": "episode"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_episode_skips_scheduler_trace_for_non_scheduler_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DummyGraphiti(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
        graph_driver=_DummyNeo4jDriver(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            database="neo4j",
        ),
        llm_client=object(),
        embedder=object(),
    )
    wrapper = GraphitiClient(
        client=client,
        scheduler_fallback_base_url="https://api.openai.com/v1",
    )
    emitted_events: list[dict[str, object]] = []

    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))

    async def _fake_emit(**kwargs: object) -> None:
        emitted_events.append(kwargs)

    monkeypatch.setattr(graphiti_client_module, "emit_scheduler_task_event", _fake_emit)

    result = await wrapper.add_episode(
        name="episode-1",
        episode_body="first event",
        source_description="unit-test",
        reference_time=datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
        episode_uuid="episode-uuid",
        attempt=1,
    )

    assert result == {"ok": True, "kind": "episode"}
    assert emitted_events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_client_refreshes_llm_base_url_from_scheduler_acquire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "should_use_scheduler", lambda _base_url: True)

    acquired_urls = iter(
        [
            "http://127.0.0.1:8082/v1/t/memory--graphiti-bootstrap",
            "http://127.0.0.1:8082/v1/t/memory--graphiti-embed-bootstrap",
            "http://127.0.0.1:8082/v1/t/memory--graphiti-reranker-bootstrap",
            "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode",
            "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-embed",
            "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-reranker",
        ]
    )

    def _fake_acquire_sync(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        return next(acquired_urls)

    async def _fake_acquire_async(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        return next(acquired_urls)

    observed_clients: list[tuple[str, object]] = []

    def _fake_build_async_openai_client(*, base_url: str, api_key: str, settings: object, embedding_cache=None) -> object:
        client = object()
        observed_clients.append((base_url, client))
        return client

    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIGenericClient", _DummyOpenAIGenericClient)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_sync", _fake_acquire_sync)
    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _fake_acquire_async)
    monkeypatch.setattr(graphiti_client_module, "build_async_openai_client", _fake_build_async_openai_client)

    settings = MemorySettings(
        neo4j_uri="bolt://db:7687",
        neo4j_database="gemini-test",
        neo4j_user="neo-user",
        neo4j_password="secret",
        local_llm_base_url="http://127.0.0.1:8081/v1",
        local_llm_api_key="local-key",
        local_llm_chat_model="chat-model",
        local_llm_embed_model="embed-model",
        local_llm_embed_base_url="http://127.0.0.1:8081-embed/v1",
    )

    wrapper = GraphitiClient.from_settings(settings)
    reference_time = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)

    await wrapper.add_episode(
        name="episode-1",
        episode_body="first event",
        source_description="unit-test",
        reference_time=reference_time,
    )

    assert observed_clients[0][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-bootstrap"
    assert observed_clients[1][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-embed-bootstrap"
    assert observed_clients[2][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-reranker-bootstrap"
    assert observed_clients[3][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode"
    assert observed_clients[4][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-embed"
    assert observed_clients[5][0] == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-reranker"
    assert wrapper.llm_client_ref.config.base_url == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode"
    assert wrapper.llm_client_ref.client is observed_clients[3][1]
    assert wrapper.embedder_ref.config.base_url == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-embed"
    assert wrapper.embedder_ref.client is observed_clients[4][1]
    assert wrapper.reranker_ref.config.base_url == "http://127.0.0.1:8082/v1/t/memory--graphiti-add-episode-reranker"
    assert wrapper.reranker_ref.client is observed_clients[5][1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_scored_wakes_scheduler_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SearchClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object]] = []

        async def search_(self, query: str, config: object, *, group_ids: list[str] | None = None) -> _DummySearchResults:
            self.calls.append((query, config, group_ids))
            return _DummySearchResults(
                nodes=[_DummyNode(uuid="n1", name="node-1")],
                scores=[0.42],
            )

    observed_calls: list[tuple[str, str | None]] = []

    async def _fake_acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        observed_calls.append((fallback, task))
        return "http://127.0.0.1:8081/v1"

    _stub_search_config(monkeypatch)
    monkeypatch.setattr(graphiti_client_module, "should_use_scheduler", lambda _base_url: True)
    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _fake_acquire)

    wrapper = GraphitiClient(
        client=_SearchClient(),
        scheduler_fallback_base_url="http://127.0.0.1:8081/v1",
    )
    scored = await wrapper.search_scored("hello", num_results=5)

    assert observed_calls == [("http://127.0.0.1:8081/v1", "memory: graphiti search_scored")]
    assert scored == [("n1", "node-1", 0.42)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_scored_scheduler_failure_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SearchClient:
        async def search_(self, query: str, config: object, *, group_ids: list[str] | None = None) -> _DummySearchResults:
            return _DummySearchResults(
                nodes=[_DummyNode(uuid="n2", name="node-2")],
                scores=[0.99],
            )

    async def _failing_acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        raise RuntimeError("scheduler down")

    _stub_search_config(monkeypatch)
    monkeypatch.setattr(graphiti_client_module, "should_use_scheduler", lambda _base_url: True)
    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _failing_acquire)

    wrapper = GraphitiClient(
        client=_SearchClient(),
        scheduler_fallback_base_url="http://127.0.0.1:8081/v1",
    )

    # Acquire failure should not prevent search execution.
    scored = await wrapper.search_scored("hello", num_results=5)
    assert scored == [("n2", "node-2", 0.99)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_scored_retries_with_bm25_when_vector_dimensions_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SearchClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object]] = []

        async def search_(self, query: str, config: object, *, group_ids: list[str] | None = None) -> _DummySearchResults:
            self.calls.append((query, config, group_ids))
            methods = list(config.node_config.search_methods)
            if len(methods) == 2:
                raise RuntimeError(
                    "Invalid input for 'vector.similarity.cosine()': The supplied vectors do not have the same number of dimensions."
                )
            return _DummySearchResults(
                nodes=[_DummyNode(uuid="n3", name="node-3")],
                scores=[0.77],
            )

    _stub_search_config(monkeypatch)
    wrapper = GraphitiClient(client=_SearchClient())

    scored = await wrapper.search_scored("hello", num_results=5)

    assert scored == [("n3", "node-3", 0.77)]
    assert wrapper.client.calls[0][1].node_config.search_methods == ["bm25", "cosine_similarity"]
    assert wrapper.client.calls[1][1].node_config.search_methods == ["bm25"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_scored_recovers_from_fused_failure_with_isolated_lane(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _SearchClient:
        async def search_(
            self, query: str, config: object, *, group_ids: list[str] | None = None
        ) -> _DummySearchResults:
            raise ValueError("one fused record was malformed")

    async def _isolated_search(*args: object, **kwargs: object):
        return {
            "bm25": [("bm25-1", "surviving lexical result")],
            "cosine_similarity": [],
        }

    _stub_search_config(monkeypatch)
    wrapper = GraphitiClient(client=_SearchClient())
    monkeypatch.setattr(wrapper, "search_ranked_by_method", _isolated_search)

    with caplog.at_level(logging.ERROR):
        scored = await wrapper.search_scored("hello", num_results=5)

    assert scored == [("bm25-1", "surviving lexical result", 1.0)]
    assert "retrying isolated BM25/cosine lanes" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_scored_skips_one_malformed_returned_node(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _SearchClient:
        async def search_(
            self, query: str, config: object, *, group_ids: list[str] | None = None
        ) -> _DummySearchResults:
            return _DummySearchResults(
                nodes=[
                    _DummyNode(uuid="", name="malformed"),
                    _DummyNode(uuid="valid-1", name="valid result"),
                ],
                scores=[0.9, 0.8],
            )

    _stub_search_config(monkeypatch)
    wrapper = GraphitiClient(client=_SearchClient())

    with caplog.at_level(logging.ERROR):
        scored = await wrapper.search_scored("hello", num_results=5)

    assert scored == [("valid-1", "valid result", 0.8)]
    assert "skipped malformed result" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_ranked_by_method_keeps_healthy_lane_when_other_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import graphiti_core.search.search_utils as search_utils

    async def _bm25_failure(*args: object, **kwargs: object):
        raise ValueError("bad fulltext record")

    async def _cosine_success(*args: object, **kwargs: object):
        return [_DummyNode(uuid="cosine-1", name="surviving semantic result")]

    async def _embedding(_query: str) -> list[float]:
        return [0.1, 0.2]

    monkeypatch.setattr(search_utils, "node_fulltext_search", _bm25_failure)
    monkeypatch.setattr(search_utils, "node_similarity_search", _cosine_success)
    wrapper = GraphitiClient(client=type("Client", (), {"driver": object()})())
    monkeypatch.setattr(wrapper, "embed_query", _embedding)

    with caplog.at_level(logging.ERROR):
        ranked = await wrapper.search_ranked_by_method(
            "hello", methods=["bm25", "cosine_similarity"], num_results=5
        )

    assert ranked == {
        "bm25": [],
        "cosine_similarity": [("cosine-1", "surviving semantic result")],
    }
    assert "bm25 lane failed" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_wakes_scheduler_with_task_label(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SearchClient:
        async def search(self, query: str, **kwargs: object) -> dict[str, object]:
            return {"query": query, "kwargs": kwargs}

    observed_calls: list[tuple[str, str | None]] = []

    async def _fake_acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        observed_calls.append((fallback, task))
        return "http://127.0.0.1:8081/v1"

    monkeypatch.setattr(graphiti_client_module, "should_use_scheduler", lambda _base_url: True)
    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _fake_acquire)

    wrapper = GraphitiClient(
        client=_SearchClient(),
        scheduler_fallback_base_url="http://127.0.0.1:8081/v1",
    )
    result = await wrapper.search("hello", limit=3)

    assert observed_calls == [("http://127.0.0.1:8081/v1", "memory: graphiti search")]
    assert result == {"query": "hello", "kwargs": {"limit": 3}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_await_add_episode_request_cancels_inner_task_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When asyncio.wait_for times out (non-watchdog/OpenAI path), the inner
    create_task() must be cancelled — not left as an orphan running in the
    background holding connections and emitting unhandled-exception warnings."""
    inner_cancelled = False
    inner_started = asyncio.Event()

    async def _slow_openai_call() -> dict[str, object]:
        nonlocal inner_cancelled
        inner_started.set()
        try:
            await asyncio.sleep(60)  # simulate a hung OpenAI request
        except asyncio.CancelledError:
            inner_cancelled = True
            raise
        return {"ok": True}

    wrapper = GraphitiClient(
        client=object(),  # type: ignore[arg-type]
        scheduler_fallback_base_url="https://api.openai.com/v1",  # non-scheduler
    )
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))

    with pytest.raises((asyncio.CancelledError, TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(
            wrapper._await_add_episode_request(
                awaitable=_slow_openai_call(),
                task="test-task",
                episode_uuid="test-uuid",
                child_task_id="test-child",
            ),
            timeout=0.1,
        )

    # Give the event loop a moment to process the cancellation
    await asyncio.sleep(0)

    assert inner_cancelled, "Inner OpenAI task must be cancelled when outer wait_for times out"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_await_add_episode_request_propagates_task_exception_non_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal exceptions from the OpenAI call propagate unchanged on the non-watchdog path."""

    async def _failing_call() -> None:
        raise ValueError("openai error")

    wrapper = GraphitiClient(
        client=object(),  # type: ignore[arg-type]
        scheduler_fallback_base_url="https://api.openai.com/v1",
    )
    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", lambda task=None: asyncio.sleep(0))

    with pytest.raises(ValueError, match="openai error"):
        await wrapper._await_add_episode_request(
            awaitable=_failing_call(),
            task="test-task",
            episode_uuid="test-uuid",
            child_task_id="test-child",
        )
