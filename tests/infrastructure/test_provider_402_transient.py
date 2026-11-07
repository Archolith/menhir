"""A 402 with a retry hint (OpenRouter's in-flight budget) is throttling, not balance exhaustion.

#482 made every 402 a billing state, which hard-opens the breaker for an hour. OpenRouter also
sends 402 ``in_flight_budget_exhausted`` with a Retry-After when too many requests are in flight;
that clears in seconds and must back off like a 429. A bare 402 stays billing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.prompts.models import Message

from menhir.infrastructure import circuit_breaker as cb
from menhir.infrastructure import openai_rate_limit as rl
from menhir.infrastructure.circuit_breaker import should_trip_circuit
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_llm_adapter import (
    MenhirOpenAIGenericClient,
    _ProviderExtrasAsyncClient,
)
from menhir.infrastructure.observability import (
    LLMCallHandle,
    clear_provider_quota_exhaustion,
    fail_llm_usage_call,
    last_provider_quota_exhaustion,
)
from menhir.infrastructure.openai_rate_limit import (
    ProviderQuotaExhaustedError,
    find_quota_exhaustion,
    is_retryable_rate_limit,
    is_transient_payment_refusal,
    rate_limit_category,
    retry_after_seconds,
)
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.enrichment_steps import add_episode_with_timeout

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
_WHEN = datetime(2024, 2, 14, tzinfo=timezone.utc)
# The body the memory server actually received (2026-11), as the SDK passes it: the "error" object.
_IN_FLIGHT_BODY = {
    "code": 402,
    "message": "Request rejected: in-flight budget exhausted",
    "metadata": {
        "reason": "in_flight_budget_exhausted",
        "limit_source": "openrouter_in_flight_budget",
        "headers": {"Retry-After": "120"},
    },
}


def _status(body: dict | None = None, headers: dict | None = None) -> openai.APIStatusError:
    response = httpx.Response(402, request=_REQ, headers=headers or {})
    return openai.APIStatusError("Error code: 402", response=response, body=body)


def _in_flight() -> openai.APIStatusError:
    return _status(_IN_FLIGHT_BODY)


def _wrapped(cause: BaseException) -> BaseException:
    try:
        raise RuntimeError("Graphiti call failed") from cause
    except RuntimeError as exc:
        return exc


@pytest.mark.parametrize(
    "make",
    [
        _in_flight,
        lambda: _status({"code": 402, "metadata": {"reason": "in_flight_budget_exhausted"}}),
        lambda: _status({"code": 402, "metadata": {"headers": {"retry-after": "5"}}}),
        lambda: _status(None, headers={"Retry-After": "7"}),
        lambda: _status(None, headers={"retry-after-ms": "1500"}),
    ],
)
def test_402_with_a_retry_hint_is_throttling_not_billing(make) -> None:
    err = make()
    assert is_transient_payment_refusal(err)
    assert find_quota_exhaustion(err) is None
    assert find_quota_exhaustion(_wrapped(err)) is None
    assert is_retryable_rate_limit(err)
    assert rate_limit_category(err) == "rate_limit"
    assert should_trip_circuit(err)


def test_retry_hint_is_read_from_the_body_when_headers_have_none() -> None:
    assert retry_after_seconds(_in_flight()) == 120.0
    assert retry_after_seconds(_status(None, headers={"Retry-After": "7"})) == 7.0
    assert retry_after_seconds(_status({"code": 402, "metadata": {"headers": {"Retry-After": "x"}}})) is None


@pytest.mark.parametrize(
    "make",
    [
        lambda: _status(None),
        lambda: _status({"code": 402, "message": "Insufficient credits"}),
        lambda: _status({"code": 402, "metadata": {"reason": "something_else"}}),
        # A billing code wins over a retry hint.
        lambda: _status({"code": "insufficient_quota", "message": "x"}, headers={"Retry-After": "7"}),
        lambda: _status(
            {"code": "credit_balance_too_low", "metadata": {"reason": "in_flight_budget_exhausted"}}
        ),
    ],
)
def test_402_without_a_retry_hint_or_with_a_billing_code_stays_billing(make) -> None:
    err = make()
    assert not is_transient_payment_refusal(err)
    assert find_quota_exhaustion(err) is err
    assert not is_retryable_rate_limit(err)


def test_classifier_requeues_a_transient_402() -> None:
    err = _in_flight()
    assert classify_enrichment_failure(err, error_type=type(err).__name__) == "retryable"
    wrapped = _wrapped(err)
    assert classify_enrichment_failure(wrapped, error_type=type(wrapped).__name__) == "retryable"
    hinted = _status(None, headers={"Retry-After": "7"})
    assert classify_enrichment_failure(hinted, error_type=type(hinted).__name__) == "retryable"
    persisted = f"Error code: 402 - {_IN_FLIGHT_BODY}"
    assert classify_enrichment_failure(persisted, error_type="APIStatusError") == "retryable"


def test_classifier_still_parks_a_bare_402() -> None:
    err = _status(None)
    assert classify_enrichment_failure(err, error_type=type(err).__name__) == "manual_review"


def test_transient_402_does_not_set_the_quota_signal() -> None:
    clear_provider_quota_exhaustion()
    handle = LLMCallHandle(
        call_id="c", kind="chat", model="m", endpoint="chat.completions.create",
        operation=None, started_at=0.0, report_only=False,
    )
    try:
        fail_llm_usage_call(handle, _in_flight())
        assert last_provider_quota_exhaustion() is None
    finally:
        clear_provider_quota_exhaustion()


def _client(errors: list[BaseException]) -> tuple[GraphitiClient, dict]:
    state = {"creates": 0}

    async def create(**kwargs):
        state["creates"] += 1
        if errors:
            raise errors.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}", refusal=None))],
            usage=None,
        )

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    # The production wrapper: it carries the rate-limit backoff for Graphiti's chat calls.
    wrapped = _ProviderExtrasAsyncClient(fake, "https://openrouter.ai/api/v1")
    llm = MenhirOpenAIGenericClient(config=LLMConfig(api_key="k", model="m"), client=wrapped)

    async def native_add_episode(**kwargs):
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
        timeout_s=30.0,
    )


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(rl.MAX_ATTEMPTS_ENV, "3")
    monkeypatch.setenv(rl.MAX_WAIT_ENV, "0")
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


async def test_transient_402_is_retried_and_never_hard_opens_the_breaker(fast_backoff) -> None:
    client, state = _client([_in_flight() for _ in range(3)])
    with pytest.raises(Exception) as raised:
        await _add(client)
    assert find_quota_exhaustion(raised.value) is None
    assert not isinstance(raised.value, ProviderQuotaExhaustedError)
    assert state["creates"] == 3, "the transient 402 was not retried by the rate-limit backoff"
    snap = client.circuit_breaker_snapshots()["llm"]
    assert "reason" not in snap, "a transient 402 hard-opened the breaker"
    assert snap["state"] == "closed" and snap["failures"] == 1


async def test_bare_402_after_a_transient_one_still_hard_opens(fast_backoff) -> None:
    client, state = _client([_in_flight(), _status(None)])
    with pytest.raises(ProviderQuotaExhaustedError):
        await _add(client)
    assert state["creates"] == 2
    snap = client.circuit_breaker_snapshots()["llm"]
    assert snap["state"] == "open" and snap["reason"] == rl.QUOTA_REASON_CODE
