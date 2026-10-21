"""The Graphiti reranker retries throttling 429s per passage and passes requests through unchanged."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from menhir.infrastructure.graphiti_llm_adapter import RateLimitedChatClient
from menhir.infrastructure.openai_rate_limit import MAX_WAIT_ENV

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(code: str = "rate_limit_exceeded") -> openai.RateLimitError:
    response = httpx.Response(429, request=_REQ)
    return openai.RateLimitError("429", response=response, body={"code": code, "type": code})


def _client(*errors: BaseException) -> tuple[RateLimitedChatClient, list[dict[str, Any]]]:
    pending = list(errors)
    seen: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> str:
        seen.append(kwargs)
        if pending:
            raise pending.pop(0)
        return "ok"

    inner = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), base_url="u"
    )
    return RateLimitedChatClient(inner, label="reranker"), seen


def test_reranker_client_retries_429_with_the_same_request(monkeypatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    client, seen = _client(_rate_limit())
    request = {"model": "gpt-4.1-nano", "messages": [], "temperature": 0, "max_tokens": 1}

    assert asyncio.run(client.chat.completions.create(**request)) == "ok"
    assert seen == [request, request]
    assert client.base_url == "u"


def test_reranker_client_does_not_retry_quota_or_timeouts(monkeypatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    for error in (_rate_limit("insufficient_quota"), openai.APITimeoutError(request=_REQ)):
        client, seen = _client(error)
        with pytest.raises(type(error)):
            asyncio.run(client.chat.completions.create(model="m", messages=[]))
        assert len(seen) == 1


def test_graphiti_reranker_rank_survives_a_429(monkeypatch) -> None:
    pytest.importorskip("graphiti_core")
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.llm_client import LLMConfig

    monkeypatch.setenv(MAX_WAIT_ENV, "0")
    pending = [_rate_limit()]

    async def create(**kwargs: Any) -> Any:
        if pending:
            raise pending.pop(0)
        top = [SimpleNamespace(token="True", logprob=0.0)]
        content = [SimpleNamespace(top_logprobs=top)]
        return SimpleNamespace(choices=[SimpleNamespace(logprobs=SimpleNamespace(content=content))])

    inner = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reranker = OpenAIRerankerClient(
        config=LLMConfig(api_key="k", model="m"),
        client=RateLimitedChatClient(inner, label="reranker"),
    )

    ranked = asyncio.run(reranker.rank("q", ["p1", "p2"]))

    assert pending == []
    assert sorted(ranked) == [("p1", 1.0), ("p2", 1.0)]
