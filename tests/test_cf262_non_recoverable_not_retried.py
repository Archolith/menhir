"""CF-262 F2/F5/F7: errors that cannot succeed on retry are attempted once; transient ones still retry."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.llm import LLMAdapter
from menhir.infrastructure.observability import ProviderQuotaExhaustion
from menhir.infrastructure.openai_rate_limit import (
    find_quota_exhaustion,
    is_retryable_rate_limit,
    rate_limit_category,
)

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status(code: int, body: dict | None = None) -> openai.APIStatusError:
    return openai.APIStatusError(f"{code}", response=httpx.Response(code, request=_REQ), body=body)


def _rate_limit(body: dict, message: str = "429") -> openai.RateLimitError:
    return openai.RateLimitError(message, response=httpx.Response(429, request=_REQ), body=body)


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


async def _chat(backend: _Backend) -> str | None:
    adapter = LLMAdapter(base_url="http://x", api_key="k", chat_model="m", embed_model="e", backend=backend)
    return await adapter._chat_text(system_prompt="s", user_prompt="u", operation="op", retry_sleep=_no_sleep)


# --- F2: deterministic 4xx and unimplemented backends -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 422])
async def test_deterministic_4xx_is_one_attempt(code: int) -> None:
    backend = _Backend(*[_status(code) for _ in range(5)])
    assert await _chat(backend) is None
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_wrapped_4xx_is_one_attempt() -> None:
    # Graphiti-style rewrap: the status error is only on the cause chain.
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = _status(400)
    backend = _Backend(wrapped, wrapped)
    assert await _chat(backend) is None
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_not_implemented_is_one_attempt() -> None:
    backend = _Backend(NotImplementedError("no chat"), NotImplementedError("no chat"))
    assert await _chat(backend) is None
    assert backend.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [408, 409, 500, 502, 503])
async def test_transient_status_still_retries(code: int) -> None:
    backend = _Backend(_status(code), _status(code))
    assert await _chat(backend) == "summary"
    assert backend.calls == 3


@pytest.mark.asyncio
async def test_connection_error_still_retries() -> None:
    backend = _Backend(openai.APIConnectionError(request=_REQ), openai.APITimeoutError(request=_REQ))
    assert await _chat(backend) == "summary"
    assert backend.calls == 3


# --- F7: billing-coded 429s other than insufficient_quota ---------------------------------------


_BILLING = [
    _rate_limit({"code": "billing_hard_limit_reached"}),
    _rate_limit({"code": "credit_balance_too_low"}),
    _rate_limit({"type": "credit_balance_exhausted"}),
    _rate_limit({}, message="You exceeded your current quota, please check your plan"),
    _rate_limit({"code": "insufficient_quota"}),
]


@pytest.mark.parametrize("exc", _BILLING)
def test_billing_429_is_quota_and_not_retried(exc: openai.RateLimitError) -> None:
    assert is_retryable_rate_limit(exc) is False
    assert rate_limit_category(exc) == "quota"
    assert find_quota_exhaustion(exc) is exc


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "resource_unavailable", None])
def test_throttle_429_still_retried(code: str | None) -> None:
    exc = _rate_limit({"code": code} if code else {})
    assert is_retryable_rate_limit(exc) is True
    assert rate_limit_category(exc) != "quota"
    assert find_quota_exhaustion(exc) is None


# --- F5: conflict review under a standing chat quota --------------------------------------------


def _groups(n: int) -> list[dict]:
    return [
        {
            "group_id": f"grp-{i}",
            "created_at": datetime.now(timezone.utc),
            "members": [
                {"uuid": f"a{i}", "name": f"A{i}", "content": "a", "status": "pending_llm_review"},
                {"uuid": f"b{i}", "name": f"B{i}", "content": "b", "status": "pending_llm_review"},
            ],
        }
        for i in range(n)
    ]


def _service(llm: MagicMock):
    from menhir.services.lifecycle_service import LifecycleService

    adapter = MagicMock()
    adapter.list_conflict_groups.return_value = _groups(3)
    return LifecycleService(graph_adapter=adapter, graphiti_client=MagicMock(), llm=llm, settings=MemorySettings())


_QUOTA = ProviderQuotaExhaustion(kind="chat", endpoint="chat", model="m", at="2026-10-30T00:00:00+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("standing", "expected_calls"),
    [(_QUOTA, 1), (None, 3), (ProviderQuotaExhaustion(kind="embed", endpoint=None, model=None, at="x"), 3)],
)
async def test_conflict_review_stops_only_on_chat_quota(standing, expected_calls: int) -> None:
    llm = MagicMock()
    llm.confirm_contradiction = AsyncMock(return_value=None)
    svc = _service(llm)
    with patch("menhir.services.lifecycle_conflicts.last_provider_quota_exhaustion", return_value=standing), \
            patch("menhir.services.lifecycle_conflicts.telemetry_store", MagicMock()):
        result = await svc.confirm_pending_conflicts(limit=10)
    assert llm.confirm_contradiction.await_count == expected_calls
    assert result["errors"] == expected_calls
    svc.graph_adapter.set_conflict_group_status.assert_not_called()
    svc.graph_adapter.resolve_conflict_group.assert_not_called()
