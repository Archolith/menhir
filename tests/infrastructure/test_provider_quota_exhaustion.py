"""#241: an insufficient_quota 429 is a billing state, kept through the real Graphiti wrapper.

The quota error goes through the real ``MenhirOpenAIGenericClient`` (which Graphiti rewraps as its
own RateLimitError), the real ``GraphitiClient.add_episode`` and the real ``CircuitBreaker``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.errors import RateLimitError as GraphitiRateLimitError
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
)
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.enrichment_steps import add_episode_with_timeout

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
_WHEN = datetime(2024, 2, 14, tzinfo=timezone.utc)


def _openai_429(code: str) -> openai.RateLimitError:
    return openai.RateLimitError(
        f"Error code: 429 - {{'error': {{'code': '{code}'}}}}",
        response=httpx.Response(429, request=_REQ),
        body={"code": code, "type": code, "message": "x"},
    )


def _graphiti_llm(code: str) -> MenhirOpenAIGenericClient:
    async def create(**kwargs):
        raise _openai_429(code)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return MenhirOpenAIGenericClient(config=LLMConfig(api_key="k", model="m"), client=fake)


def _client(code: str) -> tuple[GraphitiClient, dict]:
    """A real GraphitiClient whose native add_episode makes one real-wrapper LLM call."""
    llm = _graphiti_llm(code)
    state = {"calls": 0}

    async def native_add_episode(**kwargs):
        state["calls"] += 1
        await llm.generate_response([Message(role="system", content="s"), Message(role="user", content="u")])

    native = SimpleNamespace(add_episode=native_add_episode)
    return GraphitiClient(client=native), state


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


async def test_quota_429_through_the_graphiti_wrapper_yields_the_quota_class_and_message() -> None:
    client, _ = _client("insufficient_quota")

    with pytest.raises(ProviderQuotaExhaustedError) as caught:
        await _add(client)

    exc = caught.value
    assert QUOTA_REASON_CODE in str(exc) and "Rate limit exceeded" not in str(exc)
    # The real Graphiti wrapper replaced the openai error; the quota error chains through it.
    assert isinstance(exc.__cause__, GraphitiRateLimitError)
    assert classify_enrichment_failure(exc, error_type=type(exc).__name__) == "manual_review"
    # The persisted-text form (retry polling reads the stored message) classifies the same way.
    assert classify_enrichment_failure(str(exc)) == "manual_review"


async def test_the_wrapped_error_alone_is_still_recognised_through_its_cause_chain() -> None:
    llm = _graphiti_llm("insufficient_quota")
    with pytest.raises(GraphitiRateLimitError) as caught:
        await llm.generate_response([Message(role="user", content="u")])

    assert "Rate limit exceeded" in str(caught.value)  # the quota code is gone from the text
    assert classify_enrichment_failure(caught.value) == "manual_review"


async def test_quota_hard_opens_the_breaker_and_never_probes_on_the_30s_cadence(clock) -> None:
    client, state = _client("insufficient_quota")
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)
    assert state["calls"] == 1
    snap = client.circuit_breaker_snapshots()["llm"]
    assert snap["state"] == "open" and snap["reason"] == QUOTA_REASON_CODE

    for _ in range(5):  # well past the 30 s cooldown, repeatedly
        clock[0] += 31.0
        with pytest.raises(CircuitOpenError) as opened:
            await _add(client)
        assert opened.value.snapshot["reason"] == QUOTA_REASON_CODE
    assert state["calls"] == 1, "a half-open probe reached the provider inside the hard-open window"

    clock[0] += cb.QUOTA_COOLDOWN_SECONDS
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)  # the single probe after the long cooldown
    assert state["calls"] == 2
    assert client.circuit_breaker_snapshots()["llm"]["state"] == "open"


async def test_operator_reset_closes_a_hard_open_breaker(clock) -> None:
    client, state = _client("insufficient_quota")
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)

    client._llm_breaker.reset()

    snap = client.circuit_breaker_snapshots()["llm"]
    assert snap["state"] == "closed" and "reason" not in snap
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)
    assert state["calls"] == 2


async def test_normal_rate_limit_429_keeps_todays_behaviour(clock) -> None:
    client, state = _client("rate_limit_exceeded")

    # Not a quota error: the wrapped Graphiti error surfaces unchanged and does not trip at once.
    for _ in range(2):
        with pytest.raises(GraphitiRateLimitError) as caught:
            await _add(client)
        assert not isinstance(caught.value, ProviderQuotaExhaustedError)
    assert classify_enrichment_failure(caught.value) == "retryable"  # CF-307: was manual_review
    assert client.circuit_breaker_snapshots()["llm"]["state"] == "closed"

    with pytest.raises(GraphitiRateLimitError):
        await _add(client)  # third failure reaches the threshold
    snap = client.circuit_breaker_snapshots()["llm"]
    assert snap["state"] == "open" and "reason" not in snap

    clock[0] += 31.0  # the ordinary 30 s cooldown still allows a half-open probe
    with pytest.raises(GraphitiRateLimitError):
        await _add(client)
    assert state["calls"] == 4
    # A raw openai 429 message is still retryable, as before.
    assert classify_enrichment_failure(_openai_429("rate_limit_exceeded")) == "retryable"


def test_quota_condition_is_surfaced_and_cleared_by_a_later_success() -> None:
    clear_provider_quota_exhaustion()
    handle = LLMCallHandle(
        call_id="c", kind="chat", model="m", endpoint="chat.completions.create",
        operation=None, started_at=0.0, report_only=False,
    )

    fail_llm_usage_call(handle, _openai_429("rate_limit_exceeded"))
    assert last_provider_quota_exhaustion() is None

    fail_llm_usage_call(handle, _openai_429("insufficient_quota"))
    assert QUOTA_REASON_CODE in last_provider_quota_exhaustion().summary()

    clear_provider_quota_exhaustion("embedding")  # a different kind succeeding does not clear it
    assert last_provider_quota_exhaustion() is not None
    clear_provider_quota_exhaustion("chat")
    assert last_provider_quota_exhaustion() is None
