"""429 backoff: throttling 429s retry with backoff; everything else stays fail-fast."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from menhir.infrastructure.graphiti_llm_adapter import _ProviderExtrasAsyncClient
from menhir.infrastructure.openai_rate_limit import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS_ENV,
    MAX_WAIT_ENV,
    acall_with_rate_limit_backoff,
    call_with_rate_limit_backoff,
    retry_after_seconds,
)

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(code: str | None = "rate_limit_exceeded", headers: dict[str, str] | None = None):
    response = httpx.Response(429, headers=headers or {}, request=_REQ)
    return openai.RateLimitError("429", response=response, body={"code": code, "type": code})


def _timeout() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_REQ)


class _Script:
    """Raises the scripted errors in order, then returns ``ok``."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self, **_: Any) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def test_sync_retries_429_then_succeeds() -> None:
    script, sleeps = _Script(_rate_limit(), _rate_limit()), []
    assert call_with_rate_limit_backoff(script, sleep=sleeps.append) == "ok"
    assert script.calls == 3 and len(sleeps) == 2


def test_async_retries_429_then_succeeds() -> None:
    script, sleeps = _Script(_rate_limit()), []

    async def fn() -> str:
        return script()

    async def sleep(s: float) -> None:
        sleeps.append(s)

    assert asyncio.run(acall_with_rate_limit_backoff(fn, sleep=sleep)) == "ok"
    assert script.calls == 2 and len(sleeps) == 1


@pytest.mark.parametrize(
    "error",
    [
        _rate_limit(code="insufficient_quota"),
        _timeout(),
        openai.APIConnectionError(request=_REQ),
        openai.InternalServerError("500", response=httpx.Response(500, request=_REQ), body=None),
        ValueError("not an API error"),
    ],
)
def test_non_throttling_errors_are_not_retried(error: BaseException) -> None:
    script, sleeps = _Script(error), []
    with pytest.raises(type(error)):
        call_with_rate_limit_backoff(script, sleep=sleeps.append)
    assert script.calls == 1 and sleeps == []


def test_gives_up_after_max_attempts_and_reraises_the_429(monkeypatch) -> None:
    monkeypatch.delenv(MAX_ATTEMPTS_ENV, raising=False)
    script, sleeps = _Script(*[_rate_limit() for _ in range(DEFAULT_MAX_ATTEMPTS + 2)]), []
    with pytest.raises(openai.RateLimitError):
        call_with_rate_limit_backoff(script, sleep=sleeps.append)
    assert script.calls == DEFAULT_MAX_ATTEMPTS and len(sleeps) == DEFAULT_MAX_ATTEMPTS - 1


def test_one_attempt_disables_retry(monkeypatch) -> None:
    monkeypatch.setenv(MAX_ATTEMPTS_ENV, "1")
    script = _Script(_rate_limit())
    with pytest.raises(openai.RateLimitError):
        call_with_rate_limit_backoff(script, sleep=lambda _s: None)
    assert script.calls == 1


def test_retry_after_hint_is_honored_and_capped(monkeypatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, "30")
    sleeps: list[float] = []
    script = _Script(
        _rate_limit(headers={"retry-after-ms": "12000"}),
        _rate_limit(headers={"retry-after": "500"}),
    )
    call_with_rate_limit_backoff(script, sleep=sleeps.append)
    assert sleeps[0] >= 12.0
    assert sleeps[1] == 30.0
    assert all(s <= 30.0 for s in sleeps)


def test_retry_after_parsing() -> None:
    assert retry_after_seconds(_rate_limit(headers={"retry-after-ms": "250"})) == 0.25
    assert retry_after_seconds(_rate_limit(headers={"retry-after": "3"})) == 3.0
    assert retry_after_seconds(_rate_limit(headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    assert retry_after_seconds(_rate_limit()) is None
    assert retry_after_seconds(None) is None


def test_graphiti_proxy_retries_429(monkeypatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    script = _Script(_rate_limit())

    async def create(**kwargs: Any) -> str:
        return script(**kwargs)

    inner = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    proxy = _ProviderExtrasAsyncClient(inner, "https://api.openai.com/v1")
    result = asyncio.run(proxy.chat.completions.create(model="gpt-4o-mini", messages=[]))
    assert result == "ok" and script.calls == 2


def _local_chat_env(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_CHAT_PROVIDER", "local")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_CHAT_MODEL", "test-model")


def _chat_backend(monkeypatch, script: _Script, sleeps: list[float]) -> Any:
    from menhir.config import MemorySettings
    from menhir.infrastructure.providers import (
        OpenAIStyleChatBackend,
        ProviderConfig,
        ProviderRuntimeDependencies,
    )

    message = SimpleNamespace(content="answer")

    async def create(**kwargs: Any) -> Any:
        script(**kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def sleep(s: float) -> None:
        sleeps.append(s)

    _local_chat_env(monkeypatch)
    settings = MemorySettings.from_env()
    return OpenAIStyleChatBackend(
        provider=ProviderConfig.for_chat(settings),
        settings=settings,
        dependencies=ProviderRuntimeDependencies(
            openai_client_factory=lambda **_: client, retry_sleep=sleep
        ),
    )


def _run_isolated(coro_factory: Any) -> Any:
    from menhir.infrastructure.providers import reset_client_cache

    reset_client_cache()
    try:
        return asyncio.run(coro_factory())
    finally:
        reset_client_cache()


def _run_chat_backend(monkeypatch, script: _Script, sleeps: list[float]) -> str:
    backend = _chat_backend(monkeypatch, script, sleeps)
    return _run_isolated(
        lambda: backend.create_chat_completion(
            system_prompt="s", user_prompt="u", operation="identity_judgment", max_tokens=8,
            temperature=0.0,
        )
    )


def test_chat_backend_retries_429(monkeypatch) -> None:
    """Judges call this backend with max_retries=0; a throttle must wait, not become a None verdict."""
    script, sleeps = _Script(_rate_limit(), _rate_limit()), []
    assert _run_chat_backend(monkeypatch, script, sleeps) == "answer"
    assert script.calls == 3 and len(sleeps) == 2


def test_chat_backend_does_not_retry_quota_429(monkeypatch) -> None:
    script, sleeps = _Script(_rate_limit(code="insufficient_quota")), []
    with pytest.raises(openai.RateLimitError):
        _run_chat_backend(monkeypatch, script, sleeps)
    assert script.calls == 1 and sleeps == []


def test_judge_survives_429_through_llm_adapter(monkeypatch) -> None:
    """End to end: identity_judgment (max_retries=0) gets a verdict after a throttle."""
    from menhir.infrastructure.llm import LLMAdapter

    script, sleeps = _Script(_rate_limit()), []
    backend = _chat_backend(monkeypatch, script, sleeps)
    adapter = LLMAdapter(
        base_url="http://localhost:1234/v1", api_key="k", chat_model="test-model", embed_model="",
        backend=backend, dependencies=backend.dependencies,
    )
    text = _run_isolated(
        lambda: adapter._chat_text(
            system_prompt="s", user_prompt="u", operation="identity_judgment", max_retries=0,
        )
    )
    assert text == "answer"
    assert script.calls == 2 and len(sleeps) == 1


def test_instrumented_embeddings_retry_429_but_chat_does_not(monkeypatch) -> None:
    """Graphiti's embedder calls the instrumented client directly; chat retries live in the proxy."""
    from menhir.infrastructure.observability import _InstrumentedAsyncOpenAI

    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    embed_script, chat_script = _Script(_rate_limit()), _Script(_rate_limit())

    async def embed_create(**kwargs: Any) -> str:
        return embed_script(**kwargs)

    async def chat_create(**kwargs: Any) -> str:
        return chat_script(**kwargs)

    inner = SimpleNamespace(
        embeddings=SimpleNamespace(create=embed_create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
    )
    client = _InstrumentedAsyncOpenAI(inner)
    assert asyncio.run(client.embeddings.create(model="m", input=["x"])) == "ok"
    assert embed_script.calls == 2
    with pytest.raises(openai.RateLimitError):
        asyncio.run(client.chat.completions.create(model="m", messages=[]))
    assert chat_script.calls == 1


def test_view_embedder_retries_429(monkeypatch) -> None:
    from menhir.config import MemorySettings
    from menhir.infrastructure import view_embedder

    script = _Script(_rate_limit())

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.embeddings = SimpleNamespace(create=self._create)

        def _create(self, **kwargs: Any) -> Any:
            script(**kwargs)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])], usage=None)

    _local_chat_env(monkeypatch)
    monkeypatch.setenv("GRAPHITI_EMBED_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_LLM_EMBED_MODEL", "test-embed")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    embed = view_embedder.make_view_embedder(MemorySettings.from_env())
    assert embed is not None
    assert embed("hello") == [0.1, 0.2]
    assert script.calls == 2


def test_sync_chat_seam_retries_429(monkeypatch) -> None:
    from menhir.config import MemorySettings
    from menhir.infrastructure import sync_llm

    script = _Script(_rate_limit())
    message = SimpleNamespace(content="answer")

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            script(**kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    _local_chat_env(monkeypatch)
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    complete = sync_llm.make_sync_chat(MemorySettings.from_env())
    assert complete is not None
    assert complete("s", "u") == "answer"
    assert script.calls == 2
