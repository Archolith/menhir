"""A backoff-aware deadline nested inside another still pauses the outer one during 429 sleeps.

Counterexample: add_episode (outer, backoff-aware) runs the anchored-time resolver (inner,
backoff-aware). If the inner clock hid its sleeps, the outer deadline would expire mid-backoff.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver
from menhir.infrastructure.openai_rate_limit import (
    MAX_WAIT_ENV,
    RateLimitBackoffClock,
    acall_with_rate_limit_backoff,
    await_with_backoff_aware_timeout,
)

pytestmark = pytest.mark.unit

TIMEOUT_S = 0.4
BACKOFF_S = 0.6  # longer than either timeout on its own
_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
_CONTENT = '{"facts": [], "missing_events": []}'


@pytest.fixture(autouse=True)
def _fixed_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, str(BACKOFF_S))
    monkeypatch.delenv("MENHIR_OPENAI_SERVICE_TIER", raising=False)


def _rate_limit() -> openai.RateLimitError:
    response = httpx.Response(
        429, request=_REQ, headers={"retry-after-ms": str(int(BACKOFF_S * 1000))}
    )
    return openai.RateLimitError("429", response=response, body={"code": "rate_limit_exceeded"})


async def _throttled_once_call() -> str:
    pending = [_rate_limit()]

    async def attempt() -> str:
        if pending:
            raise pending.pop(0)
        return "ok"

    return await acall_with_rate_limit_backoff(attempt, label="test")


@pytest.mark.asyncio
async def test_nested_backoff_pauses_the_enclosing_deadline() -> None:
    inner = await_with_backoff_aware_timeout(_throttled_once_call(), timeout_s=TIMEOUT_S)
    assert await await_with_backoff_aware_timeout(inner, timeout_s=TIMEOUT_S) == "ok"


@pytest.mark.asyncio
async def test_nested_deadline_still_bounds_real_work() -> None:
    inner = await_with_backoff_aware_timeout(asyncio.sleep(BACKOFF_S), timeout_s=10.0)
    with pytest.raises(TimeoutError):
        await await_with_backoff_aware_timeout(inner, timeout_s=TIMEOUT_S)


def test_child_clock_forwards_sleeps_to_parent_once() -> None:
    now = [0.0]
    parent = RateLimitBackoffClock(now=lambda: now[0])
    a = RateLimitBackoffClock(now=lambda: now[0], parent=parent)
    b = RateLimitBackoffClock(now=lambda: now[0], parent=parent)
    a.enter()
    now[0] = 1.0
    b.enter()  # overlaps a: the parent counts the shared second once
    now[0] = 2.0
    a.exit()
    assert parent.snapshot() == (True, 2.0)
    now[0] = 3.0
    b.exit()
    assert parent.snapshot() == (False, 3.0)
    assert a.snapshot() == (False, 2.0) and b.snapshot() == (False, 2.0)


class _ThrottledOnceCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise _rate_limit()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_CONTENT))])


@pytest.mark.asyncio
async def test_resolver_backoff_inside_add_episode_deadline_does_not_expire_it() -> None:
    completions = _ThrottledOnceCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    resolver = AnchoredTimeResolver(
        client, base_url="https://api.openai.com/v1", model="gpt-6-luna", timeout_s=TIMEOUT_S
    )
    outcome = await await_with_backoff_aware_timeout(
        resolver.resolve("I left yesterday.", date(2024, 2, 14), ["The user left."]),
        timeout_s=TIMEOUT_S,
    )
    assert outcome.status == "ok"
    assert len(completions.calls) == 2  # one retry layer, not nested
