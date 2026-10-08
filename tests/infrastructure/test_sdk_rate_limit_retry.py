"""Builder clients leave 429s to our logged backoff; the SDK still retries 5xx and transport faults."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from menhir.infrastructure import observability

pytestmark = pytest.mark.unit

_NO_LANGFUSE = SimpleNamespace(langfuse_host=None, langfuse_public_key=None, langfuse_secret_key=None)
_OK = {
    "id": "c",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}
    ],
}


def _sdk_client(*statuses: int) -> tuple[openai.AsyncOpenAI, list[int]]:
    """A real SDK client (SDK default max_retries=2) over a scripted transport."""
    script = list(statuses)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = script.pop(0) if script else 200
        seen.append(status)
        headers = {"retry-after-ms": "1", "x-should-retry": "true"} if status != 200 else {}
        return httpx.Response(status, json=_OK if status == 200 else {"error": {}}, headers=headers)

    cls = observability._without_sdk_429_retry(openai.AsyncOpenAI)
    client = cls(
        api_key="k",
        base_url="https://api.example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client, seen


def _create(client: openai.AsyncOpenAI):
    return asyncio.run(client.chat.completions.create(model="m", messages=[]))


def test_sdk_does_not_retry_429_even_when_the_server_asks() -> None:
    client, seen = _sdk_client(429, 200)

    with pytest.raises(openai.RateLimitError):
        _create(client)
    assert seen == [429]


def test_sdk_still_retries_5xx() -> None:
    client, seen = _sdk_client(500, 502, 200)

    assert _create(client).choices[0].message.content == "hi"
    assert seen == [500, 502, 200]


@pytest.mark.parametrize(
    ("base_url", "base_cls"),
    [
        ("https://api.openai.com/v1", openai.AsyncOpenAI),
        ("http://localhost:8080/v1", observability._LocalAsyncOpenAI),
    ],
)
def test_builder_clients_skip_sdk_429_retry(base_url: str, base_cls: type) -> None:
    client = observability.build_async_openai_client(
        base_url=base_url, api_key="k", settings=_NO_LANGFUSE
    )
    inner = client._inner

    assert isinstance(inner, base_cls)
    assert isinstance(inner, observability._NoSdkRateLimitRetry)
    assert inner.max_retries == 2
    assert inner._should_retry(httpx.Response(429)) is False
    assert inner._should_retry(httpx.Response(503)) is True
    if base_cls is observability._LocalAsyncOpenAI:
        assert inner.auth_headers == {}


def test_wrapping_is_cached_and_ignores_non_sdk_factories() -> None:
    wrap = observability._without_sdk_429_retry

    assert wrap(openai.AsyncOpenAI) is wrap(openai.AsyncOpenAI)
    assert wrap(wrap(openai.AsyncOpenAI)) is wrap(openai.AsyncOpenAI)

    def factory(**_: object) -> object:
        return object()

    assert wrap(factory) is factory
    assert wrap(SimpleNamespace) is SimpleNamespace
