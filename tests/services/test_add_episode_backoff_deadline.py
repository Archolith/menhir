"""The add_episode deadline pauses while 429 backoff sleeps, but still bounds real work."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from menhir.infrastructure.openai_rate_limit import (
    MAX_WAIT_ENV,
    RateLimitBackoffClock,
    acall_with_rate_limit_backoff,
    await_with_backoff_aware_timeout,
    call_with_rate_limit_backoff,
)
from menhir.services.enrichment_steps import add_episode_with_timeout

pytestmark = pytest.mark.unit

TIMEOUT_S = 0.4
BACKOFF_S = 0.6  # longer than the whole timeout on its own
_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


@pytest.fixture(autouse=True)
def _fixed_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server hint equals the per-attempt cap, so every backoff sleeps exactly BACKOFF_S.
    monkeypatch.setenv(MAX_WAIT_ENV, str(BACKOFF_S))


def _throttled_once() -> Any:
    """An OpenAI call that 429s once, then returns "ok"."""
    pending = [
        openai.RateLimitError(
            "429",
            response=httpx.Response(
                429, request=_REQ, headers={"retry-after-ms": str(int(BACKOFF_S * 1000))}
            ),
            body={"code": "rate_limit_exceeded"},
        )
    ]

    def call() -> str:
        if pending:
            raise pending.pop(0)
        return "ok"

    return call


async def _rate_limited_call() -> str:
    call = _throttled_once()

    async def attempt() -> str:
        return call()

    return await acall_with_rate_limit_backoff(attempt, label="test")


def _run(coro: Any) -> tuple[Any, float]:
    start = time.monotonic()
    result = asyncio.run(coro)
    return result, time.monotonic() - start


def test_backoff_longer_than_the_timeout_does_not_time_out() -> None:
    async def work() -> str:
        await asyncio.sleep(0.1)
        return await _rate_limited_call()

    result, elapsed = _run(await_with_backoff_aware_timeout(work(), timeout_s=TIMEOUT_S))

    assert result == "ok"
    assert elapsed > TIMEOUT_S


def test_non_backoff_work_still_times_out() -> None:
    with pytest.raises(TimeoutError):
        _run(await_with_backoff_aware_timeout(asyncio.sleep(BACKOFF_S), timeout_s=TIMEOUT_S))


def test_backoff_extends_the_deadline_only_by_the_time_slept() -> None:
    async def work() -> None:
        await _rate_limited_call()
        await asyncio.sleep(1.0)  # real work past the remaining budget

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(await_with_backoff_aware_timeout(work(), timeout_s=TIMEOUT_S))
    elapsed = time.monotonic() - start

    assert BACKOFF_S + TIMEOUT_S - 0.05 < elapsed < BACKOFF_S + TIMEOUT_S + 0.3


def test_hard_cap_bounds_an_episode_that_stays_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_WAIT_ENV, "5")

    async def work() -> str:
        call = _throttled_once()

        async def attempt() -> str:
            return call()

        return await acall_with_rate_limit_backoff(
            attempt, label="test", sleep=lambda _s: asyncio.sleep(5)
        )

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(
            await_with_backoff_aware_timeout(work(), timeout_s=TIMEOUT_S, max_extension=0.5)
        )

    assert time.monotonic() - start < TIMEOUT_S * 1.5 + 0.3


def test_parallel_backoffs_pause_the_deadline_once() -> None:
    async def work() -> None:
        await asyncio.gather(_rate_limited_call(), _rate_limited_call())
        await asyncio.sleep(1.0)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(await_with_backoff_aware_timeout(work(), timeout_s=TIMEOUT_S))

    # Two overlapping 0.6 s sleeps pause the clock for 0.6 s, not 1.2 s.
    assert time.monotonic() - start < BACKOFF_S + TIMEOUT_S + 0.3


def test_clock_counts_overlapping_sleeps_once() -> None:
    now = [0.0]
    clock = RateLimitBackoffClock(now=lambda: now[0])

    clock.enter()
    now[0] = 1.0
    clock.enter()
    now[0] = 3.0
    clock.exit()
    assert clock.snapshot() == (True, 3.0)
    now[0] = 4.0
    clock.exit()
    now[0] = 10.0
    clock.enter()
    now[0] = 12.0
    clock.exit()

    assert clock.snapshot() == (False, 6.0)


def test_backoff_on_a_worker_thread_pauses_the_deadline() -> None:
    def sync_call() -> str:
        return call_with_rate_limit_backoff(_throttled_once(), label="test")

    result, elapsed = _run(
        await_with_backoff_aware_timeout(asyncio.to_thread(sync_call), timeout_s=TIMEOUT_S)
    )

    assert result == "ok"
    assert elapsed > TIMEOUT_S


def test_outside_cancellation_is_not_turned_into_a_timeout() -> None:
    async def main() -> None:
        task = asyncio.create_task(
            await_with_backoff_aware_timeout(asyncio.sleep(5), timeout_s=TIMEOUT_S * 10)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())


def test_backoff_outliving_the_await_does_not_touch_the_closed_deadline() -> None:
    async def main() -> str:
        straggler: asyncio.Task[str] | None = None

        async def work() -> str:
            nonlocal straggler
            straggler = asyncio.create_task(_rate_limited_call())
            return "done"

        result = await await_with_backoff_aware_timeout(work(), timeout_s=TIMEOUT_S)
        assert straggler is not None
        assert await straggler == "ok"
        return result

    assert asyncio.run(main()) == "done"


def test_add_episode_with_timeout_survives_a_long_backoff() -> None:
    async def add_episode(**_: Any) -> str:
        return await _rate_limited_call()

    result, _ = _run(
        add_episode_with_timeout(
            SimpleNamespace(add_episode=add_episode),
            name="e",
            episode_body="body",
            source_description="test",
            reference_time=datetime.now(timezone.utc),
            episode_uuid="ep-1",
            timeout_s=TIMEOUT_S,
        )
    )

    assert result == "ok"


def test_add_episode_with_timeout_still_reports_unknown_remote_status() -> None:
    async def add_episode(**_: Any) -> None:
        await asyncio.sleep(BACKOFF_S)

    with pytest.raises(TimeoutError, match="remote completion status unknown"):
        _run(
            add_episode_with_timeout(
                SimpleNamespace(add_episode=add_episode),
                name="e",
                episode_body="body",
                source_description="test",
                reference_time=datetime.now(timezone.utc),
                episode_uuid="ep-1",
                timeout_s=TIMEOUT_S,
            )
        )
