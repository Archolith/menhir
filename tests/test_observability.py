"""Unit tests for optional Langfuse/OpenAI client wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import httpx

import menhir.infrastructure.observability as observability
from menhir.config import MemorySettings


@pytest.mark.unit
def test_langfuse_enabled_requires_all_credentials() -> None:
    assert observability.langfuse_enabled(MemorySettings()) is False
    assert observability.langfuse_enabled(
        MemorySettings(
            langfuse_host="http://localhost:3001",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
    ) is True


@pytest.mark.unit
def test_memory_settings_from_env_accepts_langfuse_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3001")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    settings = MemorySettings.from_env()

    assert settings.langfuse_host == "http://localhost:3001"
    assert settings.langfuse_public_key == "pk-test"
    assert settings.langfuse_secret_key == "sk-test"


@pytest.mark.unit
def test_build_async_openai_client_uses_plain_openai_without_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, str]] = []

    class _FakeAsyncOpenAI:
        def __init__(self, *, base_url: str, api_key: str, http_client=None) -> None:
            created.append({"base_url": base_url, "api_key": api_key, "has_http_client": http_client is not None})

        @property
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer test"}

    monkeypatch.setattr(observability, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(observability, "_LocalAsyncOpenAI", _FakeAsyncOpenAI)

    client = observability.build_async_openai_client(
        base_url="https://api.openai.com/v1",
        api_key="remote-key",
        settings=MemorySettings(),
    )

    assert client.auth_headers == {"Authorization": "Bearer test"}
    assert created == [{"base_url": "https://api.openai.com/v1", "api_key": "remote-key", "has_http_client": False}]


@pytest.mark.unit
def test_build_async_openai_client_bypasses_auth_for_local_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, str]] = []

    class _FakeAsyncOpenAI:
        def __init__(self, *, base_url: str, api_key: str, http_client=None) -> None:
            created.append({"base_url": base_url, "api_key": api_key, "has_http_client": http_client is not None})

        @property
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer should-not-be-used"}

    class _FakeLocalAsyncOpenAI(_FakeAsyncOpenAI):
        @property
        def auth_headers(self) -> dict[str, str]:
            return {}

    monkeypatch.setattr(observability, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(observability, "_LocalAsyncOpenAI", _FakeLocalAsyncOpenAI)

    client = observability.build_async_openai_client(
        base_url="http://localhost:1234/v1",
        api_key="any-key",
        settings=MemorySettings(),
    )

    assert client.auth_headers == {}
    assert created == [{"base_url": "http://localhost:1234/v1", "api_key": "any-key", "has_http_client": True}]


@pytest.mark.unit
def test_build_async_openai_client_skips_langfuse_for_local_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class _FakeLocalAsyncOpenAI:
        def __init__(self, *, base_url: str, api_key: str, http_client=None) -> None:
            created.append("local")

        @property
        def auth_headers(self) -> dict[str, str]:
            return {}

    def _fake_langfuse_async_openai(*, base_url: str, api_key: str, http_client=None) -> object:
        created.append("langfuse")
        return SimpleNamespace(kind="plain")

    monkeypatch.setattr(observability, "_LocalAsyncOpenAI", _FakeLocalAsyncOpenAI)
    monkeypatch.setattr(observability, "LangfuseAsyncOpenAI", _fake_langfuse_async_openai)

    client = observability.build_async_openai_client(
        base_url="http://localhost:1234/v1",
        api_key="local-key",
        settings=MemorySettings(
            langfuse_host="http://localhost:3001",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )

    assert client.auth_headers == {}
    assert created == ["local"]


@pytest.mark.unit
def test_build_async_openai_client_uses_langfuse_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, str]] = []

    def fake_langfuse_async_openai(
        *,
        base_url: str,
        api_key: str,
        http_client=None,
    ) -> object:
        created.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "has_http_client": http_client is not None,
            }
        )
        return SimpleNamespace(kind="langfuse")

    monkeypatch.setattr(observability, "LangfuseAsyncOpenAI", fake_langfuse_async_openai)

    client = observability.build_async_openai_client(
        base_url="https://api.openai.com/v1",
        api_key="openai-key",
        settings=MemorySettings(
            langfuse_host="http://localhost:3001",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )

    assert client.kind == "langfuse"
    assert created == [
        {
            "base_url": "https://api.openai.com/v1",
            "api_key": "openai-key",
            "has_http_client": False,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_async_openai_client_emits_llm_usage_events(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeCompletions:
        async def create(self, *args: object, **kwargs: object) -> object:
            return {"ok": True, "kind": "chat", "kwargs": kwargs}

    class _FakeEmbeddings:
        async def create(self, *args: object, **kwargs: object) -> object:
            return {"ok": True, "kind": "embedding", "kwargs": kwargs}

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()
            self.embeddings = _FakeEmbeddings()

    monkeypatch.setattr(
        observability,
        "AsyncOpenAI",
        lambda *, base_url, api_key, http_client=None: _FakeClient(),
    )
    monkeypatch.setattr(
        observability,
        "_LocalAsyncOpenAI",
        lambda *, base_url, api_key, http_client=None: _FakeClient(),
    )

    seen: list[observability.LLMUsageEvent] = []
    token = observability.set_llm_usage_callback(lambda event: seen.append(event))
    try:
        client = observability.build_async_openai_client(
            base_url="http://localhost:1234/v1",
            api_key="local",
            settings=MemorySettings(),
        )
        await client.chat.completions.create(model="chat-model", messages=[{"role": "user", "content": "hi"}])
        await client.embeddings.create(model="embed-model", input="hello")
    finally:
        observability.reset_llm_usage_callback(token)

    assert [event.kind for event in seen] == ["chat", "chat", "embedding", "embedding"]
    assert [event.phase for event in seen] == ["started", "completed", "started", "completed"]
    assert [event.model for event in seen] == ["chat-model", "chat-model", "embed-model", "embed-model"]
    assert seen[0].call_id == seen[1].call_id
    assert seen[2].call_id == seen[3].call_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_instrumented_endpoint_captures_provider_reported_token_details() -> None:
    class _Completions:
        async def create(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=30,
                    total_tokens=150,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
                )
            )

    seen: list[observability.LLMUsageEvent] = []
    token = observability.set_llm_usage_callback(seen.append)
    try:
        endpoint = observability._InstrumentedEndpoint(
            _Completions(),
            event_type="chat",
            endpoint="chat.completions.create",
        )
        await endpoint.create(model="gpt-test")
    finally:
        observability.reset_llm_usage_callback(token)

    assert len(seen) == 2
    started, completed = seen
    assert started.call_id == completed.call_id
    assert completed.input_tokens == 120
    assert completed.output_tokens == 30
    assert completed.total_tokens == 150
    assert completed.cached_input_tokens == 80
    assert completed.reasoning_output_tokens == 12
    assert completed.duration_ms is not None
    assert completed.provider_usage is not None


@pytest.mark.unit
def test_normalized_usage_accepts_gemini_usage_metadata() -> None:
    usage = observability._normalized_usage(
        {
            "promptTokenCount": 41,
            "candidatesTokenCount": 9,
            "totalTokenCount": 50,
            "cachedContentTokenCount": 7,
            "thoughtsTokenCount": 3,
        }
    )

    assert usage["input_tokens"] == 41
    assert usage["output_tokens"] == 9
    assert usage["total_tokens"] == 50
    assert usage["cached_input_tokens"] == 7
    assert usage["reasoning_output_tokens"] == 3


@pytest.mark.unit
def test_request_usage_callback_overrides_process_fallback() -> None:
    fallback: list[observability.LLMUsageEvent] = []
    scoped: list[observability.LLMUsageEvent] = []
    observability.set_default_llm_usage_callback(fallback.append)
    token = observability.set_llm_usage_callback(scoped.append)
    try:
        handle = observability.start_llm_usage_call(
            kind="chat",
            model="test",
            endpoint="chat.completions.create",
        )
        observability.complete_llm_usage_call(
            handle,
            usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        )
    finally:
        observability.reset_llm_usage_callback(token)
        observability.set_default_llm_usage_callback(None)

    assert [event.phase for event in scoped] == ["started", "completed"]
    assert fallback == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_async_openai_client_emits_failed_llm_usage_events(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingCompletions:
        async def create(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FailingCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    monkeypatch.setattr(
        observability,
        "AsyncOpenAI",
        lambda *, base_url, api_key, http_client=None: _FakeClient(),
    )
    monkeypatch.setattr(
        observability,
        "_LocalAsyncOpenAI",
        lambda *, base_url, api_key, http_client=None: _FakeClient(),
    )

    seen: list[observability.LLMUsageEvent] = []
    token = observability.set_llm_usage_callback(seen.append)
    try:
        client = observability.build_async_openai_client(
            base_url="http://localhost:1234/v1",
            api_key="local",
            settings=MemorySettings(),
        )
        with pytest.raises(RuntimeError, match="boom"):
            await client.chat.completions.create(model="chat-model", messages=[])
    finally:
        observability.reset_llm_usage_callback(token)

    assert [event.phase for event in seen] == ["started", "failed"]
    assert seen[0].call_id == seen[1].call_id
    assert seen[1].error == "boom"
    assert seen[1].duration_ms is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_async_openai_client_strips_auth_for_local_llama() -> None:
    http_client = observability._build_http_client("http://127.0.0.1:8081/v1")

    assert http_client is not None

    request = httpx.Request("GET", "http://127.0.0.1:8081/v1/models", headers={"Authorization": "Bearer nope"})
    hooks = http_client.event_hooks.get("request", [])
    assert hooks
    await hooks[0](request)

    assert "Authorization" not in request.headers
