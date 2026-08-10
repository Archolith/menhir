from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.providers import (
    GeminiChatBackend,
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
    UnimplementedProviderChatBackend,
    build_chat_backend,
    parse_provider_kind,
)


@pytest.mark.unit
def test_parse_provider_kind_normalizes_values() -> None:
    assert parse_provider_kind("openai-compat") is ProviderKind.LOCAL
    assert parse_provider_kind("gemini") is ProviderKind.GEMINI


@pytest.mark.unit
def test_build_chat_backend_returns_openai_style_backend() -> None:
    settings = MemorySettings(chat_provider="local")

    backend = build_chat_backend(settings)

    assert isinstance(backend, OpenAIStyleChatBackend)


@pytest.mark.unit
def test_build_chat_backend_returns_placeholder_for_gemini() -> None:
    settings = MemorySettings(chat_provider="gemini")

    backend = build_chat_backend(settings)

    assert isinstance(backend, GeminiChatBackend)


@pytest.mark.unit
def test_provider_config_for_chat_uses_gemini_settings() -> None:
    settings = MemorySettings(
        chat_provider="gemini",
        gemini_base_url="https://generativelanguage.googleapis.com/v1beta",
        gemini_api_key="gem-key",
        gemini_chat_model="gemini-2.5-flash",
    )

    config = ProviderConfig.for_chat(settings)

    assert config.kind is ProviderKind.GEMINI
    assert config.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert config.api_key == "gem-key"
    assert config.chat_model == "gemini-2.5-flash"


@pytest.mark.unit
def test_provider_config_for_graphiti_llm_uses_openai_settings() -> None:
    settings = MemorySettings(
        chat_provider="local",
        graphiti_provider="openai",
        openai_api_key="openai-key",
        openai_chat_model="gpt-5-mini",
    )

    config = ProviderConfig.for_graphiti_llm(settings)

    assert config.kind is ProviderKind.OPENAI
    assert config.base_url == "https://api.openai.com/v1"
    assert config.api_key == "openai-key"
    assert config.chat_model == "gpt-5-mini"


@pytest.mark.unit
def test_provider_config_for_graphiti_embedder_allows_local_override() -> None:
    settings = MemorySettings(
        graphiti_provider="openai",
        graphiti_embed_provider="local",
        local_llm_base_url="http://localhost:1234/v1",
        local_llm_api_key="local-key",
        local_llm_embed_model="bge-base",
        local_llm_embed_base_url="http://localhost:1235/v1",
    )

    config = ProviderConfig.for_graphiti_embedder(settings)

    assert config.kind is ProviderKind.LOCAL
    assert config.base_url == "http://localhost:1235/v1"
    assert config.api_key == "local-key"
    assert config.embed_model == "bge-base"


@pytest.mark.unit
def test_provider_config_for_graphiti_reranker_allows_independent_override() -> None:
    settings = MemorySettings(
        graphiti_provider="openai",
        graphiti_reranker_provider="local",
        local_llm_base_url="http://localhost:1234/v1",
        local_llm_api_key="local-key",
        local_llm_chat_model="qwen3.5-35b-a3b",
    )

    config = ProviderConfig.for_graphiti_reranker(settings)

    assert config.kind is ProviderKind.LOCAL
    assert config.base_url == "http://localhost:1234/v1"
    assert config.api_key == "local-key"
    assert config.chat_model == "qwen3.5-35b-a3b"


@pytest.mark.unit
def test_provider_config_for_graphiti_reranker_inherits_llm_by_default() -> None:
    settings = MemorySettings(
        graphiti_provider="openai",
        openai_api_key="openai-key",
        openai_chat_model="gpt-5-mini",
    )

    config = ProviderConfig.for_graphiti_reranker(settings)

    assert config.kind is ProviderKind.OPENAI
    assert config.base_url == "https://api.openai.com/v1"
    assert config.api_key == "openai-key"
    assert config.chat_model == "gpt-5-mini"


@pytest.mark.unit
def test_provider_config_for_graphiti_reranker_explicit_model_override() -> None:
    settings = MemorySettings(
        graphiti_provider="openai",
        openai_api_key="openai-key",
        openai_chat_model="gpt-5-mini",
    )

    config = ProviderConfig.for_graphiti_reranker(settings)

    assert config.kind is ProviderKind.OPENAI
    assert config.chat_model == "gpt-5-mini"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_style_backend_uses_openai_client() -> None:
    settings = MemorySettings()
    provider = ProviderConfig(
        kind=ProviderKind.LOCAL,
        base_url="http://localhost:1234/v1",
        api_key="test-key",
        chat_model="test-model",
    )
    mock_create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create)))
    client_calls: list[dict[str, object]] = []

    def build_client(
        *,
        base_url: str,
        api_key: str,
        settings: MemorySettings,
        request_timeout_s: float | None = None,
    ):
        client_calls.append({"base_url": base_url, "api_key": api_key, "timeout_s": request_timeout_s})
        return client

    backend = OpenAIStyleChatBackend(
        provider=provider,
        settings=settings,
        dependencies=ProviderRuntimeDependencies(openai_client_factory=build_client, request_timeout_s=0.5),
    )

    result = await backend.create_chat_completion(
        system_prompt="sys",
        user_prompt="user",
        operation="compression",
        max_tokens=64,
        temperature=0.3,
    )

    assert result == "ok"
    assert client_calls == [
        {"base_url": "http://localhost:1234/v1", "api_key": "test-key", "timeout_s": 0.5}
    ]
    assert mock_create.call_args.kwargs["model"] == "test-model"
    assert mock_create.call_args.kwargs["max_tokens"] == 64


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_style_backend_uses_structured_scheduler_task_label() -> None:
    settings = MemorySettings()
    provider = ProviderConfig(
        kind=ProviderKind.LOCAL,
        base_url="http://localhost:1234/v1",
        api_key="test-key",
        chat_model="test-model",
    )
    acquire_mock = AsyncMock(return_value="http://localhost:8082/v1/t/memory--llm-compression")
    backend = OpenAIStyleChatBackend(
        provider=provider,
        settings=settings,
        dependencies=ProviderRuntimeDependencies(scheduler_url_acquire=acquire_mock, request_timeout_s=0.5),
    )

    with patch("menhir.infrastructure.providers.should_use_scheduler", return_value=True):
        resolved = await backend._resolve_base_url("compression", "user prompt")

    assert resolved == "http://localhost:8082/v1/t/memory--llm-compression"
    assert acquire_mock.await_args.kwargs["task"] == "memory: llm compression"
    assert acquire_mock.await_args.kwargs["timeout_s"] == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_backend_uses_generate_content_contract() -> None:
    provider = ProviderConfig(
        kind=ProviderKind.GEMINI,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gem-key",
        chat_model="gemini-2.5-flash",
    )
    backend = GeminiChatBackend(provider=provider)

    with patch(
        "menhir.infrastructure.providers._gemini_generate_content",
        new=AsyncMock(return_value={"candidates": [{"content": {"parts": [{"text": "gemini ok"}]}}]}),
    ) as generate_mock:
        result = await backend.create_chat_completion(
            system_prompt="sys",
            user_prompt="user",
            operation="compression",
            max_tokens=64,
            temperature=0.3,
        )

    assert result == "gemini ok"
    call_kwargs = generate_mock.await_args.kwargs
    assert call_kwargs["model"] == "gemini-2.5-flash"
    assert call_kwargs["payload"]["system_instruction"]["parts"][0]["text"] == "sys"
    assert call_kwargs["payload"]["contents"][0]["parts"][0]["text"] == "user"
