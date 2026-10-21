"""Unit tests for the Graphiti client wrapper."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import sys
from types import ModuleType

import pytest
from pydantic import BaseModel, Field
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance.node_operations import _extract_entity_attributes

from menhir.config import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_helpers import (
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
)
from menhir.infrastructure.graphiti_llm_adapter import (
    MenhirOpenAIGenericClient,
    _openai_strict_json_schema,
)
import menhir.infrastructure.graphiti_client as graphiti_client_module


class _DummyTemporalValue:
    def iso_format(self) -> str:
        return "2026-03-06T12:00:00+00:00"


class _DummyOpenAIGenericClient:
    def __init__(self, *, config: object, client: object | None = None, max_tokens: int | None = None, **kwargs: object) -> None:
        self.config = config
        self.client = client
        self.max_tokens = max_tokens
        self.kwargs = kwargs


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
        **hook_kwargs: object,
    ) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.graph_driver = graph_driver
        self.llm_client = llm_client
        self.embedder = embedder
        self.cross_encoder = cross_encoder
        self.hook_kwargs = hook_kwargs
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
        extra_body: object | None = None,
    ) -> _DummyChatResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "extra_body": extra_body,
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
    monkeypatch.setattr(graphiti_client_module, "MenhirOpenAIGenericClient", _DummyOpenAIGenericClient)
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
    assert wrapper.client.llm_client.client._inner is observed_client
    assert wrapper.client.embedder.config.base_url == "http://local-llm:1234/v1"
    assert wrapper.client.embedder.config.api_key == "local-key"
    assert wrapper.client.embedder.config.embedding_model == "embed-model"
    assert wrapper.client.embedder.client is observed_client
    assert isinstance(wrapper.client.cross_encoder, _DummyOpenAIRerankerClient)
    assert wrapper.client.cross_encoder.config.base_url == "http://local-llm:1234/v1"
    assert wrapper.client.cross_encoder.config.api_key == "local-key"
    assert wrapper.client.cross_encoder.config.model == "chat-model"
    assert wrapper.client.cross_encoder.client._inner is observed_client
    assert wrapper.reranker_provider_kind == "local"


@pytest.mark.unit
def test_graphiti_client_pins_llm_temperature_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Graphiti DEFAULT_TEMPERATURE=1 caused ~3% stochastic entity conflation.

    The production GraphitiClient must construct LLMConfig with temperature=0
    to substantially reduce sampling variance in extraction and dedup calls.
    """
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "MenhirOpenAIGenericClient", _DummyOpenAIGenericClient)
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
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "model", "expected_mode"),
    [
        ("https://api.deepseek.com/v1", "chat-model", "json_object"),
        ("https://llm.example/v1", "deepseek-v4-flash", "json_object"),
        ("https://llm.example/v1", "chat-model", "json_schema"),
    ],
)
async def test_graphiti_client_uses_compatible_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    model: str,
    expected_mode: str,
) -> None:
    """The configured endpoint/model controls the actual fork request format."""
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedderConfig", _DummyOpenAIEmbedderConfig)
    monkeypatch.setattr(graphiti_client_module, "OpenAIEmbedder", _DummyOpenAIEmbedder)
    monkeypatch.setattr(graphiti_client_module, "OpenAIRerankerClient", _DummyOpenAIRerankerClient)
    monkeypatch.setattr(graphiti_client_module, "Neo4jDriver", _DummyNeo4jDriver)
    monkeypatch.setattr(graphiti_client_module, "Graphiti", _DummyGraphiti)
    chat_client = _DummyOpenAIChat(response=_DummyChatResponse(content='{"value": "ok"}'))
    monkeypatch.setattr(
        graphiti_client_module,
        "build_async_openai_client",
        lambda **_kwargs: type("Client", (), {"chat": chat_client})(),
    )
    settings = MemorySettings(
        graphiti_provider="local",
        neo4j_uri="bolt://db:7687",
        neo4j_database="test",
        neo4j_user="neo",
        neo4j_password="secret",
        local_llm_base_url=base_url,
        local_llm_api_key="key",
        local_llm_chat_model=model,
        local_llm_embed_model="embed-model",
    )

    wrapper = GraphitiClient.from_settings(settings)

    class ResponsePayload(BaseModel):
        value: str

    response = await wrapper.client.llm_client.generate_response(
        [
            _DummyOpenAIMessage(role="system", content="Extract a value."),
            _DummyOpenAIMessage(role="user", content="hello"),
        ],
        response_model=ResponsePayload,
    )

    assert response == {"value": "ok"}
    request = chat_client.completions.calls[0]
    assert request["response_format"]["type"] == expected_mode
    if expected_mode == "json_object":
        assert "Respond with a JSON object in the following format" in request["messages"][-1]["content"]
        assert '"value"' in request["messages"][-1]["content"]
        assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    else:
        assert "Respond with a JSON object in the following format" not in request["messages"][-1]["content"]
        assert request["extra_body"] is None


@pytest.mark.unit
def test_graphiti_client_rejects_non_openai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)

    settings = MemorySettings(graphiti_provider="anthropic")

    with pytest.raises(NotImplementedError):
        GraphitiClient.from_settings(settings)


@pytest.mark.unit
def test_graphiti_client_supports_openai_llm_with_local_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphiti_client_module, "_GRAPHITI_IMPORT_ERROR", None)
    monkeypatch.setattr(graphiti_client_module, "LLMConfig", _DummyLLMConfig)
    monkeypatch.setattr(graphiti_client_module, "MenhirOpenAIGenericClient", _DummyOpenAIGenericClient)
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
    monkeypatch.setattr(graphiti_client_module, "MenhirOpenAIGenericClient", _DummyOpenAIGenericClient)
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
def test_openai_structured_output_schema_is_strict_at_every_object() -> None:
    class NestedPayload(BaseModel):
        name: str
        tags: list[str] = Field(default_factory=list)

    class ResponsePayload(BaseModel):
        nested: NestedPayload

    schema = _openai_strict_json_schema(ResponsePayload)
    nested = schema["$defs"]["NestedPayload"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["nested"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["name", "tags"]


def _make_adapter_client(content: str) -> tuple[MenhirOpenAIGenericClient, _DummyChatCompletions]:
    """Build a real MenhirOpenAIGenericClient over a dummy chat transport."""
    chat_client = _DummyOpenAIChat(response=_DummyChatResponse(content=content))
    client = type("Client", (), {"chat": chat_client})()
    llm_client = MenhirOpenAIGenericClient(
        config=None,
        client=client,
        max_tokens=128,
    )
    llm_client.temperature = 0.0
    llm_client.model = "chat-model"
    return llm_client, chat_client.completions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_menhir_client_preserves_attribute_extraction_preamble_on_retry() -> None:
    """Typed node/edge attribute calls use the fork's keyword-only contract."""
    class ResponsePayload(BaseModel):
        value: str

    llm_client, completions = _make_adapter_client('{"value": "ok"}')
    requests: list[dict[str, object]] = []

    async def flaky_create(**kwargs: object) -> _DummyChatResponse:
        requests.append(kwargs)
        if len(requests) == 1:
            return _DummyChatResponse(content="{broken")
        return _DummyChatResponse(content='{"value": "ok"}')

    completions.create = flaky_create  # type: ignore[method-assign]
    response = await llm_client.generate_response(
        [
            _DummyOpenAIMessage(role="system", content="Extract attributes."),
            _DummyOpenAIMessage(role="user", content="person: Ada"),
        ],
        response_model=ResponsePayload,
        attribute_extraction=True,
    )

    assert response == {"value": "ok"}
    assert len(requests) == 2
    for request in requests:
        assert "<<graphiti.attr_extraction.preamble.v1>>" in request["messages"][0]["content"]
    assert "Retry 1/" in requests[1]["messages"][-1]["content"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_typed_node_attribute_extraction_uses_menhir_adapter() -> None:
    class PersonAttributes(BaseModel):
        favorite_food: str

    llm_client, completions = _make_adapter_client('{"favorite_food": "sushi"}')
    node = EntityNode(name="Ada", group_id="group", labels=["Entity", "Person"])

    attributes = await _extract_entity_attributes(llm_client, node, None, None, PersonAttributes)

    assert attributes == {"favorite_food": "sushi"}
    assert "<<graphiti.attr_extraction.preamble.v1>>" in completions.calls[0]["messages"][0]["content"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_menhir_client_sends_strict_response_format() -> None:
    class ResponsePayload(BaseModel):
        value: str = "default"

    llm_client, completions = _make_adapter_client('{"value": "ok"}')

    response = await llm_client._generate_response(
        messages=[_DummyOpenAIMessage(role="user", content="hello")],
        response_model=ResponsePayload,
        max_tokens=64,
    )

    assert response == {"value": "ok"}
    response_format = completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert response_format["json_schema"]["schema"]["required"] == ["value"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_menhir_request_guard_logs_request_and_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from menhir.infrastructure.graphiti_llm_adapter import build_menhir_request_guard

    llm_client, _completions = _make_adapter_client('{"ok": true}')
    llm_client.request_guard = build_menhir_request_guard(100000)

    with caplog.at_level(logging.INFO):
        response = await llm_client._generate_response(
            messages=[_DummyOpenAIMessage(role="user", content="hello")],
            max_tokens=64,
        )

    assert response == {"ok": True}
    assert "Graphiti OpenAI-compatible request begin" in caplog.text
    assert "Graphiti OpenAI-compatible response received" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_menhir_request_guard_attaches_parse_failure_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from menhir.infrastructure.graphiti_llm_adapter import build_menhir_request_guard

    llm_client, _completions = _make_adapter_client("bad-json")
    llm_client.request_guard = build_menhir_request_guard(100000)

    with caplog.at_level(logging.INFO), pytest.raises(Exception, match="Expecting value") as exc_info:
        await llm_client._generate_response(
            messages=[_DummyOpenAIMessage(role="user", content="hello")],
            max_tokens=64,
        )

    assert "Graphiti OpenAI-compatible request failed" in caplog.text
    details = getattr(exc_info.value, "menhir_failure_details", None)
    assert details is not None
    assert details["graphiti_failure_phase"] == "provider_error"
    assert details["graphiti_message_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_menhir_request_guard_attaches_empty_response_diagnostics() -> None:
    from menhir.infrastructure.graphiti_llm_adapter import build_menhir_request_guard

    llm_client, _completions = _make_adapter_client("")
    llm_client.request_guard = build_menhir_request_guard(100000)

    with pytest.raises(Exception, match="empty response") as exc_info:
        await llm_client._generate_response(
            messages=[
                _DummyOpenAIMessage(role="system", content="sys"),
                _DummyOpenAIMessage(role="user", content="hello"),
            ],
            max_tokens=64,
        )

    details = getattr(exc_info.value, "menhir_failure_details", None)
    assert details is not None
    assert details["graphiti_failure_phase"] == "provider_error"
    assert details["graphiti_message_count"] == 2


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
async def test_await_add_episode_request_cancels_inner_task_on_cancellation() -> None:
    """When asyncio.wait_for times out, the inner
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

    wrapper = GraphitiClient(client=object())  # type: ignore[arg-type]

    with pytest.raises((asyncio.CancelledError, TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(
            wrapper._await_add_episode_request(awaitable=_slow_openai_call()),
            timeout=0.1,
        )

    # Give the event loop a moment to process the cancellation
    await asyncio.sleep(0)

    assert inner_cancelled, "Inner OpenAI task must be cancelled when outer wait_for times out"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_await_add_episode_request_propagates_task_exception_non_watchdog() -> None:
    """Normal exceptions from the OpenAI call propagate unchanged."""

    async def _failing_call() -> None:
        raise ValueError("openai error")

    wrapper = GraphitiClient(client=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="openai error"):
        await wrapper._await_add_episode_request(awaitable=_failing_call())
