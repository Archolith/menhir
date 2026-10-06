"""gpt-6 / reasoning-model request policy and the opt-in OpenAI service tier."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.graphiti_llm_adapter import _ProviderExtrasAsyncClient
from menhir.infrastructure.openai_request_policy import (
    DEFAULT_FLEX_TIMEOUT_S,
    FLEX_TIMEOUT_ENV,
    SERVICE_TIER_ENV,
    apply_openai_request_policy,
)

OPENAI = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def _no_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SERVICE_TIER_ENV, raising=False)
    monkeypatch.delenv(FLEX_TIMEOUT_ENV, raising=False)


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna", "o3-mini", "GPT-6-Luna"])
def test_reasoning_models_get_completion_tokens_and_no_temperature(model: str) -> None:
    out = apply_openai_request_policy(
        {"model": model, "max_tokens": 64, "temperature": 0, "messages": []}, base_url=OPENAI
    )
    assert out == {"model": model, "max_completion_tokens": 64, "messages": []}


@pytest.mark.parametrize("model", ["gpt-4o-mini", "openai/gpt-6-luna", "deepseek-v4-flash", None])
def test_other_models_and_openrouter_slugs_are_unchanged(model: str | None, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    kwargs = {"model": model, "max_tokens": 64, "temperature": 0}
    assert apply_openai_request_policy(kwargs, base_url=OPENAI) == kwargs


def test_input_is_not_mutated() -> None:
    kwargs = {"model": "gpt-6-luna", "max_tokens": 64, "temperature": 0}
    apply_openai_request_policy(kwargs, base_url=OPENAI)
    assert kwargs == {"model": "gpt-6-luna", "max_tokens": 64, "temperature": 0}


def test_existing_completion_tokens_win_and_none_is_dropped() -> None:
    out = apply_openai_request_policy(
        {"model": "gpt-6-luna", "max_tokens": 64, "max_completion_tokens": 900}, base_url=OPENAI
    )
    assert out == {"model": "gpt-6-luna", "max_completion_tokens": 900}
    out = apply_openai_request_policy({"model": "gpt-6-luna", "max_tokens": None}, base_url=OPENAI)
    assert out == {"model": "gpt-6-luna"}


def test_no_service_tier_unless_opted_in() -> None:
    out = apply_openai_request_policy({"model": "gpt-6-luna"}, base_url=OPENAI)
    assert "service_tier" not in out and "timeout" not in out


@pytest.mark.parametrize("base_url", [OPENAI, "", None])
def test_flex_on_openai_endpoint_adds_tier_and_long_timeout(base_url: Any, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    out = apply_openai_request_policy({"model": "gpt-6-luna"}, base_url=base_url)
    assert out["service_tier"] == "flex"
    assert out["timeout"] == DEFAULT_FLEX_TIMEOUT_S


def test_flex_timeout_override_and_explicit_values_kept(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    monkeypatch.setenv(FLEX_TIMEOUT_ENV, "120")
    assert apply_openai_request_policy({"model": "gpt-6-luna"}, base_url=OPENAI)["timeout"] == 120.0
    out = apply_openai_request_policy(
        {"model": "gpt-6-luna", "service_tier": "default", "timeout": 5}, base_url=OPENAI
    )
    assert out["service_tier"] == "default" and out["timeout"] == 5


@pytest.mark.parametrize(
    "base_url", ["https://openrouter.ai/api/v1", "http://127.0.0.1:8080/v1", "https://api.deepseek.com"]
)
def test_tier_never_sent_to_other_endpoints(base_url: str, monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    out = apply_openai_request_policy({"model": "gpt-6-luna"}, base_url=base_url)
    assert "service_tier" not in out and "timeout" not in out


def test_graphiti_proxy_applies_policy(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TIER_ENV, "flex")
    seen: dict[str, Any] = {}

    class _Completions:
        async def create(self, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "ok"

    inner = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    proxy = _ProviderExtrasAsyncClient(inner, OPENAI)
    result = asyncio.run(
        proxy.chat.completions.create(
            model="gpt-6-luna", messages=[], temperature=0, max_tokens=16384, response_format={}
        )
    )
    assert result == "ok"
    assert seen == {
        "model": "gpt-6-luna",
        "messages": [],
        "max_completion_tokens": 16384,
        "response_format": {},
        "service_tier": "flex",
        "timeout": DEFAULT_FLEX_TIMEOUT_S,
    }
