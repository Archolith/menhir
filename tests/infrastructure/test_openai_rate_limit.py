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


def test_chat_backend_leaves_429_to_its_caller(monkeypatch) -> None:
    """LLMAdapter._chat_text owns retry for this backend (compress/merge retry; judges pass
    max_retries=0 to fail fast). A backoff here would multiply or override that budget."""
    from menhir.config import MemorySettings
    from menhir.infrastructure.providers import (
        OpenAIStyleChatBackend,
        ProviderConfig,
        ProviderRuntimeDependencies,
        reset_client_cache,
    )

    script, sleeps = _Script(_rate_limit()), []
    message = SimpleNamespace(content="answer")

    async def create(**kwargs: Any) -> Any:
        script(**kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def sleep(s: float) -> None:
        sleeps.append(s)

    _local_chat_env(monkeypatch)
    settings = MemorySettings.from_env()
    backend = OpenAIStyleChatBackend(
        provider=ProviderConfig.for_chat(settings),
        settings=settings,
        dependencies=ProviderRuntimeDependencies(
            openai_client_factory=lambda **_: client, retry_sleep=sleep
        ),
    )
    reset_client_cache()
    try:
        with pytest.raises(openai.RateLimitError):
            asyncio.run(
                backend.create_chat_completion(
                    system_prompt="s", user_prompt="u", operation="test", max_tokens=8,
                    temperature=0.0,
                )
            )
    finally:
        reset_client_cache()
    assert script.calls == 1 and sleeps == []


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
