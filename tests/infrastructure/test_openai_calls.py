"""openai_calls: one entry point applies Flex/reasoning shaping and 429 backoff."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from menhir.infrastructure.model_profiles import FLEX_TIMEOUT_ENV, SERVICE_TIER_ENV
from menhir.infrastructure.openai_calls import (
    ResilientChatClient,
    acreate_chat_completion,
    create_chat_completion,
    create_embedding,
)
from menhir.infrastructure.openai_rate_limit import MAX_WAIT_ENV

pytestmark = pytest.mark.unit

OPENAI = "https://api.openai.com/v1"
_REQ = httpx.Request("POST", OPENAI + "/chat/completions")
_GPT6 = {"model": "gpt-6-luna", "messages": [], "max_tokens": 50, "temperature": 0.2}


@pytest.fixture(autouse=True)
def _flex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    monkeypatch.delenv(FLEX_TIMEOUT_ENV, raising=False)
    monkeypatch.setenv(MAX_WAIT_ENV, "0.01")  # keep backoff sleeps short


def _rate_limit() -> openai.RateLimitError:
    return openai.RateLimitError(
        "429",
        response=httpx.Response(429, request=_REQ, headers={"retry-after-ms": "1"}),
        body={"code": "rate_limit_exceeded"},
    )


class _Endpoint:
    """Records each call's kwargs; raises the scripted errors first."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls: list[dict[str, Any]] = []

    def _record(self, kwargs: dict[str, Any]) -> str:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return "ok"

    def sync(self, **kwargs: Any) -> str:
        return self._record(kwargs)

    async def create(self, **kwargs: Any) -> str:
        return self._record(kwargs)


def test_flex_and_reasoning_shaping_on_openai() -> None:
    endpoint = _Endpoint()

    asyncio.run(acreate_chat_completion(endpoint.create, _GPT6, base_url=OPENAI, label="t"))

    (sent,) = endpoint.calls
    assert sent["service_tier"] == "flex"
    assert sent["timeout"] == 900.0
    assert sent["max_completion_tokens"] == 50
    assert "max_tokens" not in sent and "temperature" not in sent


def test_no_service_tier_off_the_openai_endpoint() -> None:
    endpoint = _Endpoint()

    asyncio.run(
        acreate_chat_completion(
            endpoint.create, _GPT6, base_url="http://localhost:8080/v1", label="t"
        )
    )

    (sent,) = endpoint.calls
    assert "service_tier" not in sent and "timeout" not in sent


def test_429_is_retried_with_the_shaped_request() -> None:
    endpoint = _Endpoint(_rate_limit())

    result = asyncio.run(
        acreate_chat_completion(endpoint.create, _GPT6, base_url=OPENAI, label="t")
    )

    assert result == "ok"
    assert len(endpoint.calls) == 2
    assert all(call["service_tier"] == "flex" for call in endpoint.calls)


def test_request_is_not_mutated() -> None:
    request = dict(_GPT6)

    asyncio.run(acreate_chat_completion(_Endpoint().create, request, base_url=OPENAI, label="t"))

    assert request == _GPT6


def test_shape_false_passes_the_request_through() -> None:
    endpoint = _Endpoint()

    asyncio.run(
        acreate_chat_completion(endpoint.create, _GPT6, base_url=OPENAI, label="t", shape=False)
    )

    assert endpoint.calls == [_GPT6]


def test_sync_chat_shapes_and_retries() -> None:
    endpoint = _Endpoint(_rate_limit())

    assert create_chat_completion(endpoint.sync, _GPT6, base_url=OPENAI, label="t") == "ok"
    assert len(endpoint.calls) == 2
    assert endpoint.calls[-1]["service_tier"] == "flex"


def test_embedding_retries_without_shaping() -> None:
    endpoint = _Endpoint(_rate_limit())
    request = {"model": "text-embedding-3-small", "input": ["x"]}

    assert create_embedding(endpoint.sync, request, label="t") == "ok"
    assert endpoint.calls == [request, request]


def test_resilient_client_retries_and_passes_other_attributes_through() -> None:
    endpoint = _Endpoint(_rate_limit())
    inner = SimpleNamespace(chat=SimpleNamespace(completions=endpoint), base_url=OPENAI)
    client = ResilientChatClient(inner, label="reranker")
    request = {"model": "gpt-4o-mini", "messages": [], "max_tokens": 1, "logprobs": True}

    async def call() -> str:
        return await client.chat.completions.create(**request)

    assert asyncio.run(call()) == "ok"

    assert endpoint.calls == [request, request]  # shape=False: reranker kwargs untouched
    assert client.base_url == OPENAI
    assert client._inner is inner
