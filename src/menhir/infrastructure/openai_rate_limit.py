"""Automatic backoff for OpenAI-compatible 429 rate-limit responses (tenacity).

Only throttling 429s are retried. Timeouts, connection errors and 5xx stay fail-fast (CF-190),
and ``insufficient_quota`` 429s are a billing state that waiting cannot fix. The wait is the
larger of the server's ``retry-after-ms``/``retry-after`` hint and a full-jitter exponential,
capped per attempt. Retrying a chat completion has no side effects beyond token cost.

``MENHIR_OPENAI_RATE_LIMIT_MAX_ATTEMPTS`` (default 6, ``1`` disables) and
``MENHIR_OPENAI_RATE_LIMIT_MAX_WAIT_S`` (default 60) bound the total: at most about five minutes
of sleep per call, under the 900 s enrichment lease, which a heartbeat renews anyway.

Applied only where no caller retries: the Graphiti proxy (``generate_response`` re-raises
RateLimitError) and the sync chat seam (lane callers fail the page on any exception).
``providers.OpenAIStyleChatBackend`` is deliberately excluded: ``LLMAdapter._chat_text`` owns
its retry budget, including the judges' explicit ``max_retries=0``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import openai
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS_ENV = "MENHIR_OPENAI_RATE_LIMIT_MAX_ATTEMPTS"
MAX_WAIT_ENV = "MENHIR_OPENAI_RATE_LIMIT_MAX_WAIT_S"
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_MAX_WAIT_S = 60.0
_NOT_RETRYABLE_CODES = frozenset({"insufficient_quota"})



def is_retryable_rate_limit(exc: BaseException) -> bool:
    if not isinstance(exc, openai.RateLimitError):
        return False
    return exc.code not in _NOT_RETRYABLE_CODES and exc.type not in _NOT_RETRYABLE_CODES


def retry_after_seconds(exc: BaseException | None) -> float | None:
    """The server's retry hint in seconds, or None when absent or unparseable (HTTP-date form)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw) / scale
        except ValueError:
            continue
        if value >= 0:
            return value
    return None


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name) or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name) or default))
    except ValueError:
        return default


def _retry_kwargs(label: str) -> dict[str, Any]:
    max_wait = _env_float(MAX_WAIT_ENV, DEFAULT_MAX_WAIT_S)
    jitter = wait_random_exponential(multiplier=1, max=max_wait)

    def _wait(state: RetryCallState) -> float:
        hint = retry_after_seconds(state.outcome.exception() if state.outcome else None)
        return min(max_wait, max(jitter(state), hint or 0.0))

    def _log(state: RetryCallState) -> None:
        wait_s = state.next_action.sleep if state.next_action else 0.0
        logger.warning(
            "OpenAI 429 on %s; backing off %.1fs before attempt %d",
            label,
            wait_s,
            state.attempt_number + 1,
        )

    return {
        "retry": retry_if_exception(is_retryable_rate_limit),
        "stop": stop_after_attempt(_env_int(MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS)),
        "wait": _wait,
        "before_sleep": _log,
        "reraise": True,
    }


def call_with_rate_limit_backoff[T](
    fn: Callable[[], T],
    *,
    label: str = "chat.completions",
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    return Retrying(sleep=sleep, **_retry_kwargs(label))(fn)


async def acall_with_rate_limit_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "chat.completions",
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> T:
    kwargs = _retry_kwargs(label)
    if sleep is not None:
        kwargs["sleep"] = sleep

    # tenacity awaits only callables it detects as coroutine functions; a lambda returning a
    # coroutine would come back unawaited. test_graphiti_proxy_retries_429 covers this.
    async def _attempt() -> T:
        return await fn()

    return await AsyncRetrying(**kwargs)(_attempt)
