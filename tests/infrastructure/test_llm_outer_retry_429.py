"""CF-262: the outer _chat_text retry loop must not retry provider 429s (the inner layer owns them)."""

from __future__ import annotations

import httpx
import openai
import pytest

from menhir.infrastructure.llm import LLMAdapter

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(code: str) -> openai.RateLimitError:
    response = httpx.Response(429, request=_REQ)
    return openai.RateLimitError("429", response=response, body={"code": code, "type": code})


class _Backend:
    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def create_chat_completion(self, **_: object) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "summary"


async def _no_sleep(_: float) -> None:
    return None


def _adapter(backend: _Backend) -> LLMAdapter:
    return LLMAdapter(
        base_url="http://x", api_key="k", chat_model="m", embed_model="e", backend=backend
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["insufficient_quota", "rate_limit_exceeded", "resource_unavailable"])
async def test_provider_429_is_one_outer_attempt(code: str) -> None:
    backend = _Backend(*[_rate_limit(code) for _ in range(20)])
    result = await _adapter(backend)._chat_text(
        system_prompt="s", user_prompt="u", operation="op", retry_sleep=_no_sleep
    )
    assert result is None
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_generic_transient_error_still_retries() -> None:
    backend = _Backend(RuntimeError("boom"), RuntimeError("boom"))
    result = await _adapter(backend)._chat_text(
        system_prompt="s", user_prompt="u", operation="op", retry_sleep=_no_sleep
    )
    assert result == "summary"
    assert backend.calls == 3
