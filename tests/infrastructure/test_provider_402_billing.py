"""#482: a provider HTTP 402 Payment Required is a billing state, like a 429 insufficient_quota."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.prompts.models import Message

from menhir.infrastructure import circuit_breaker as cb
from menhir.infrastructure.circuit_breaker import CircuitOpenError
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_llm_adapter import MenhirOpenAIGenericClient
from menhir.infrastructure.observability import (
    LLMCallHandle,
    clear_provider_quota_exhaustion,
    fail_llm_usage_call,
    last_provider_quota_exhaustion,
)
from menhir.infrastructure.openai_rate_limit import (
    QUOTA_REASON_CODE,
    ProviderQuotaExhaustedError,
    find_quota_exhaustion,
    quota_exhausted_error,
)
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.enrichment_steps import add_episode_with_timeout

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
_WHEN = datetime(2024, 2, 14, tzinfo=timezone.utc)


def _status(code: int, body: dict | None = None) -> openai.APIStatusError:
    return openai.APIStatusError(f"{code}", response=httpx.Response(code, request=_REQ), body=body)


def _rate_limit_429() -> openai.RateLimitError:
    return openai.RateLimitError(
        "429",
        response=httpx.Response(429, request=_REQ),
        body={"code": "rate_limit_exceeded", "type": "rate_limit_exceeded", "message": "x"},
    )


def _client_402() -> tuple[GraphitiClient, dict]:
    async def create(**kwargs):
        raise _status(402)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm = MenhirOpenAIGenericClient(config=LLMConfig(api_key="k", model="m"), client=fake)
    state = {"calls": 0}

    async def native_add_episode(**kwargs):
        state["calls"] += 1
        await llm.generate_response([Message(role="system", content="s"), Message(role="user", content="u")])

    return GraphitiClient(client=SimpleNamespace(add_episode=native_add_episode)), state


async def _add(client: GraphitiClient) -> None:
    await add_episode_with_timeout(
        client,
        name="n",
        episode_body="b",
        source_description="s",
        reference_time=_WHEN,
        episode_uuid="ep-1",
        timeout_s=5.0,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


def test_find_quota_exhaustion_returns_a_402_directly_and_through_a_wrapper() -> None:
    err = _status(402)
    assert find_quota_exhaustion(err) is err

    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = err
    assert find_quota_exhaustion(wrapper) is err

    quota = quota_exhausted_error(wrapper)
    assert isinstance(quota, ProviderQuotaExhaustedError)
    assert quota.__cause__ is wrapper
    assert QUOTA_REASON_CODE in str(quota)


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_other_status_errors_are_not_billing(code: int) -> None:
    assert find_quota_exhaustion(_status(code)) is None


def test_plain_429_is_not_billing() -> None:
    assert find_quota_exhaustion(_rate_limit_429()) is None


async def test_402_hard_opens_the_breaker_and_never_probes_on_the_30s_cadence(clock) -> None:
    client, state = _client_402()
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)
    assert state["calls"] == 1
    snap = client.circuit_breaker_snapshots()["llm"]
    assert snap["state"] == "open" and snap["reason"] == QUOTA_REASON_CODE

    for _ in range(5):
        clock[0] += 31.0
        with pytest.raises(CircuitOpenError) as opened:
            await _add(client)
        assert opened.value.snapshot["reason"] == QUOTA_REASON_CODE
    assert state["calls"] == 1, "a half-open probe reached the provider inside the hard-open window"


def test_classify_grades_a_402_as_manual_review() -> None:
    err = _status(402)
    assert classify_enrichment_failure(err, error_type=type(err).__name__) == "manual_review"
    wrapped = quota_exhausted_error(err)
    assert wrapped is not None
    assert classify_enrichment_failure(wrapped, error_type=type(wrapped).__name__) == "manual_review"


def test_fail_llm_usage_call_records_a_402_quota_condition() -> None:
    clear_provider_quota_exhaustion()
    handle = LLMCallHandle(
        call_id="c", kind="chat", model="m", endpoint="chat.completions.create",
        operation=None, started_at=0.0, report_only=False,
    )
    try:
        fail_llm_usage_call(handle, _status(402))
        assert QUOTA_REASON_CODE in last_provider_quota_exhaustion().summary()
    finally:
        clear_provider_quota_exhaustion()
    assert last_provider_quota_exhaustion() is None
