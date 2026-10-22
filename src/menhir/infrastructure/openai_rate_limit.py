"""Automatic backoff for OpenAI-compatible 429 rate-limit responses (tenacity).

Only throttling 429s are retried. Timeouts, connection errors and 5xx stay fail-fast (CF-190),
and ``insufficient_quota`` 429s are a billing state that waiting cannot fix. The wait is the
larger of the server's ``retry-after-ms``/``retry-after`` hint and a full-jitter exponential,
capped per attempt. Retrying a chat completion has no side effects beyond token cost.

``MENHIR_OPENAI_RATE_LIMIT_MAX_ATTEMPTS`` (default 6, ``1`` disables) and
``MENHIR_OPENAI_RATE_LIMIT_MAX_WAIT_S`` (default 60) bound the total: at most about five minutes
of sleep per call, under the 900 s enrichment lease, which the heartbeat renews up to a
10800 s hard ceiling per claim (not per episode).

Applied at each OpenAI call seam: the Graphiti proxy (``generate_response`` re-raises
RateLimitError), the sync chat seam, ``providers.OpenAIStyleChatBackend`` (the judges call it
with ``max_retries=0``, and a throttle there used to return a None verdict), the instrumented
async embeddings endpoint (Graphiti's embedder), the Graphiti reranker
(``RateLimitedChatClient``), and the sync view embedder. Chat on the
instrumented client is not wrapped, so the proxy's retry is never nested inside another.
``LLMAdapter._chat_text`` retries remain for non-429 faults and wrap the inner 429 retry.

Backoff sleeps are reported to the ``RateLimitBackoffClock`` bound in the current context, if
any. ``await_with_backoff_aware_timeout`` binds one, so an enclosing deadline (today
``add_episode_with_timeout``) excludes them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

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
#: How far backoff may push a backoff-aware deadline, as a multiple of its timeout. Bounds an
#: operation that stays throttled indefinitely.
DEFAULT_BACKOFF_MAX_EXTENSION = 3.0
_NOT_RETRYABLE_CODES = frozenset({"insufficient_quota"})


class RateLimitBackoffClock:
    """Wall time during which at least one rate-limited call in this context is sleeping.

    Overlapping sleeps (parallel calls backing off together) count once. Locked because the
    sync seams sleep on worker threads. ``on_change`` runs on the sleeping thread after every
    enter and exit. A ``parent`` (the enclosing deadline's clock) sees every enter and exit too,
    so a nested deadline never hides its sleeps from the outer one.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
        parent: RateLimitBackoffClock | None = None,
    ) -> None:
        self._now = now
        self.on_change = on_change
        self._parent = parent
        self._lock = threading.Lock()
        self._sleepers = 0
        self._since = 0.0
        self._total = 0.0

    def snapshot(self) -> tuple[bool, float]:
        """(any call sleeping now, total paused seconds so far)."""
        with self._lock:
            open_s = self._now() - self._since if self._sleepers else 0.0
            return self._sleepers > 0, self._total + open_s

    def enter(self) -> None:
        with self._lock:
            if self._sleepers == 0:
                self._since = self._now()
            self._sleepers += 1
        if self._parent is not None:
            self._parent.enter()
        self._notify()

    def exit(self) -> None:
        with self._lock:
            self._sleepers -= 1
            if self._sleepers == 0:
                self._total += self._now() - self._since
        if self._parent is not None:
            self._parent.exit()
        self._notify()

    def _notify(self) -> None:
        callback = self.on_change
        if callback is not None:
            callback()


_BACKOFF_CLOCK: ContextVar[RateLimitBackoffClock | None] = ContextVar(
    "menhir_rate_limit_backoff_clock", default=None
)


@contextmanager
def bind_backoff_clock(clock: RateLimitBackoffClock) -> Iterator[RateLimitBackoffClock]:
    """Report backoff sleeps in this context (and tasks/threads it spawns) to ``clock``."""
    token = _BACKOFF_CLOCK.set(clock)
    try:
        yield clock
    finally:
        _BACKOFF_CLOCK.reset(token)


def is_retryable_rate_limit(exc: BaseException) -> bool:
    if not isinstance(exc, openai.RateLimitError):
        return False
    return exc.code not in _NOT_RETRYABLE_CODES and exc.type not in _NOT_RETRYABLE_CODES


QUOTA_REASON_CODE = "provider_quota_exhausted"


class ProviderQuotaExhaustedError(Exception):
    """The provider refused with 429 insufficient_quota: a billing state, not throttling."""

    reason_code = QUOTA_REASON_CODE


def find_quota_exhaustion(exc: BaseException | None) -> openai.RateLimitError | None:
    """The insufficient_quota openai error in ``exc``'s __cause__/__context__ chain, if any.

    Graphiti rewraps the openai error as its own RateLimitError (``raise ... from e``), which
    loses the quota code, so the chain is walked rather than only ``exc`` itself.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, openai.RateLimitError) and (
            exc.code in _NOT_RETRYABLE_CODES or exc.type in _NOT_RETRYABLE_CODES
        ):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def quota_exhausted_error(exc: BaseException | None) -> ProviderQuotaExhaustedError | None:
    """``exc`` itself if already a quota error, else a new one chained to it, else None."""
    if isinstance(exc, ProviderQuotaExhaustedError):
        return exc
    if find_quota_exhaustion(exc) is None:
        return None
    error = ProviderQuotaExhaustedError(
        f"{QUOTA_REASON_CODE}: the provider returned 429 insufficient_quota (credits "
        "exhausted); waiting will not help - add credits, then re-enrich"
    )
    error.__cause__ = exc
    return error


#: Flex returns 429 ``resource_unavailable`` when it has no capacity: a wait, not throttling.
_FLEX_CAPACITY_CODES = frozenset({"resource_unavailable"})
#: Provider codes/types and request ids are kept only in this shape. A compatible provider can put
#: anything in those fields, and they reach telemetry rows that are classified as non-content.
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _safe_token(value: object) -> str | None:
    text = str(value) if value is not None else ""
    return text if _SAFE_TOKEN.fullmatch(text) else None


def find_provider_status_error(exc: BaseException | None) -> openai.APIStatusError | None:
    """The first openai HTTP-status error in ``exc``'s __cause__/__context__ chain, if any.

    Graphiti rewraps provider errors (see ``find_quota_exhaustion``), so the chain is walked.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, openai.APIStatusError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def rate_limit_category(exc: BaseException | None) -> str | None:
    """``quota``, ``flex_capacity`` or ``rate_limit`` for a provider 429; None for anything else."""
    status_error = find_provider_status_error(exc)
    if status_error is None or getattr(status_error, "status_code", None) != 429:
        return None
    codes = {
        value for value in (getattr(status_error, "code", None), getattr(status_error, "type", None))
        if isinstance(value, str)
    }
    if codes & _NOT_RETRYABLE_CODES:
        return "quota"
    if codes & _FLEX_CAPACITY_CODES:
        return "flex_capacity"
    return "rate_limit"


def provider_error_code(exc: BaseException | None) -> str | None:
    """A content-free label for a failed provider call, safe to persist; None when ``exc`` is None.

    ``<ExceptionClass>`` for a non-HTTP failure (timeout, connection), else
    ``<ExceptionClass>:<status>:<provider code or type>``. The provider's message text is never
    included: it can echo prompt content.
    """
    if exc is None:
        return None
    try:
        status_error = find_provider_status_error(exc)
        if status_error is None:
            return _safe_token(type(exc).__name__) or "Exception"
        status = getattr(status_error, "status_code", None)
        parts = [_safe_token(type(status_error).__name__) or "APIStatusError",
                 str(status) if isinstance(status, int) and 0 <= status <= 999 else "0"]
        code = _safe_token(getattr(status_error, "code", None)) or _safe_token(
            getattr(status_error, "type", None)
        )
        if code:
            parts.append(code)
        return ":".join(parts)
    except Exception:  # noqa: BLE001 - a label must never fail the failure path it describes
        return "Exception"


def describe_provider_error(exc: BaseException | None) -> str:
    """One log-line fragment naming what kind of provider failure ``exc`` is. Never raises."""
    try:
        return _describe_provider_error(exc)
    except Exception:  # noqa: BLE001 - logging must not break the retry it reports on
        return "unclassified"


def _describe_provider_error(exc: BaseException | None) -> str:
    status_error = find_provider_status_error(exc)
    if status_error is None:
        return type(exc).__name__ if exc is not None else "unknown"
    status = _safe_token(getattr(status_error, "status_code", None)) or "?"
    fields = [rate_limit_category(status_error) or f"http {status}"]
    code = _safe_token(getattr(status_error, "code", None))
    kind = _safe_token(getattr(status_error, "type", None))
    if code:
        fields.append(f"code={code}")
    if kind and kind != code:
        fields.append(f"type={kind}")
    hint = retry_after_seconds(status_error)
    fields.append(f"retry-after={hint:.1f}s" if hint is not None else "retry-after=none")
    request_id = _safe_token(getattr(status_error, "request_id", None))
    if request_id:
        fields.append(f"request_id={request_id}")
    return ", ".join(fields)


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


# Callers pass os.getenv(<named constant>) so the feature-flag inventory can see each read.
def _int_or(raw: str | None, default: int) -> int:
    try:
        return max(1, int(raw or default))
    except ValueError:
        return default


def _float_or(raw: str | None, default: float) -> float:
    try:
        return max(0.0, float(raw or default))
    except ValueError:
        return default


def _retry_kwargs(label: str) -> dict[str, Any]:
    max_wait = _float_or(os.getenv(MAX_WAIT_ENV), DEFAULT_MAX_WAIT_S)
    jitter = wait_random_exponential(multiplier=1, max=max_wait)

    def _wait(state: RetryCallState) -> float:
        hint = retry_after_seconds(state.outcome.exception() if state.outcome else None)
        return min(max_wait, max(jitter(state), hint or 0.0))

    def _log(state: RetryCallState) -> None:
        wait_s = state.next_action.sleep if state.next_action else 0.0
        # Name the kind of 429: a Flex capacity wait and real throttling call for different
        # operator responses, and the bare status hid which one a run was hitting.
        logger.warning(
            "OpenAI 429 on %s (%s); backing off %.1fs before attempt %d",
            label,
            describe_provider_error(state.outcome.exception() if state.outcome else None),
            wait_s,
            state.attempt_number + 1,
        )

    return {
        "retry": retry_if_exception(is_retryable_rate_limit),
        "stop": stop_after_attempt(_int_or(os.getenv(MAX_ATTEMPTS_ENV), DEFAULT_MAX_ATTEMPTS)),
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
    clock = _BACKOFF_CLOCK.get()
    if clock is None:
        return Retrying(sleep=sleep, **_retry_kwargs(label))(fn)

    def _tracked_sleep(seconds: float) -> Any:
        clock.enter()
        try:
            return sleep(seconds)
        finally:
            clock.exit()

    return Retrying(sleep=_tracked_sleep, **_retry_kwargs(label))(fn)


async def acall_with_rate_limit_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "chat.completions",
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> T:
    kwargs = _retry_kwargs(label)
    clock = _BACKOFF_CLOCK.get()
    if clock is not None:
        inner_sleep = sleep or asyncio.sleep

        async def _tracked_sleep(seconds: float) -> None:
            clock.enter()
            try:
                await inner_sleep(seconds)
            finally:
                clock.exit()

        kwargs["sleep"] = _tracked_sleep
    elif sleep is not None:
        kwargs["sleep"] = sleep

    # tenacity awaits only callables it detects as coroutine functions; a lambda returning a
    # coroutine would come back unawaited. test_graphiti_proxy_retries_429 covers this.
    async def _attempt() -> T:
        return await fn()

    return await AsyncRetrying(**kwargs)(_attempt)


_R = TypeVar("_R")


async def await_with_backoff_aware_timeout(
    awaitable: Awaitable[_R],
    *,
    timeout_s: float,
    max_extension: float = DEFAULT_BACKOFF_MAX_EXTENSION,
) -> _R:
    """Await under a ``timeout_s`` deadline that pauses while any 429 backoff inside it sleeps.

    Use instead of ``asyncio.wait_for`` around work that makes OpenAI calls: a 429 chain can
    sleep about five minutes per call, which alone used up the 300 s add_episode timeout. Wall
    time is still capped at ``timeout_s * (1 + max_extension)``. Expiry raises TimeoutError and
    outside cancellation propagates, as with ``asyncio.wait_for``. The awaitable runs in the
    current task; tasks and ``asyncio.to_thread`` workers it starts inherit the clock. Nested
    inside another such deadline, its sleeps pause the outer deadline as well.
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    start = loop.time()
    hard_deadline = start + timeout_s * (1.0 + max_extension)
    clock = RateLimitBackoffClock(now=loop.time, parent=_BACKOFF_CLOCK.get())

    async with asyncio.timeout_at(start + timeout_s) as scope:

        def _reschedule() -> None:
            # A queued call can land after the scope fired or the await returned.
            if clock.on_change is None or scope.expired():
                return
            sleeping, paused_s = clock.snapshot()
            when = hard_deadline if sleeping else start + timeout_s + paused_s
            scope.reschedule(min(when, hard_deadline))

        def _on_change() -> None:
            # Timeout.reschedule must run on the loop thread; sync seams sleep on workers.
            if threading.get_ident() == loop_thread:
                _reschedule()
            else:
                loop.call_soon_threadsafe(_reschedule)

        clock.on_change = _on_change
        try:
            with bind_backoff_clock(clock):
                return await awaitable
        finally:
            clock.on_change = None
