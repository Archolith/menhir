"""Circuit breaker for external service calls.

M6 Phase 1 — CLOSED/OPEN/HALF_OPEN state machine with failure classification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial
from enum import StrEnum
from time import monotonic
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from menhir.infrastructure.openai_rate_limit import (
    QUOTA_REASON_CODE,
    ProviderQuotaExhaustedError,
    find_transient_payment_refusal,
    quota_exhausted_error,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Hard-open cooldown after a billing refusal (402, or insufficient_quota 429): one half-open probe
#: per 5 minutes, not per 30 s. A refused probe costs nothing, and this bounds how long the breaker
#: stays open after credit is added (#492); there is no operator reset.
QUOTA_COOLDOWN_SECONDS = 300.0

#: Attribute set on an exception raised through a breaker, naming that breaker. An outer breaker
#: wrapping a call that contains an inner one (add_episode runs the embedder) must not take the
#: inner provider's failure as its own (#493).
BREAKER_ORIGIN_ATTR = "_menhir_breaker"


def breaker_origin(exc: BaseException) -> str | None:
    """The breaker that raised ``exc`` or an exception in its explicit ``__cause__`` chain.

    Implicit ``__context__`` is not followed: an error raised while another breaker's error was
    being handled is still this call's own failure, and counting it as foreign would keep the
    outer breaker from opening.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        try:
            origin = getattr(current, BREAKER_ORIGIN_ATTR, None)
        except Exception:
            return None
        if origin:
            return str(origin)
        current = current.__cause__
    return None


def tag_breaker_origin(exc: BaseException, name: str) -> None:
    """Record that ``exc`` came out of breaker ``name``, unless an inner breaker already did."""
    if breaker_origin(exc) is None:
        try:
            setattr(exc, BREAKER_ORIGIN_ATTR, name)
        except (AttributeError, TypeError):  # pragma: no cover - exceptions with __slots__
            pass


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, message: str, *, snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.snapshot = snapshot


def circuit_open_cause(exc: BaseException) -> CircuitOpenError | None:
    """The CircuitOpenError behind ``exc``, if a breaker refused the call (#630)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, CircuitOpenError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def provider_unavailable_reason(exc: BaseException) -> str | None:
    """Why a provider call stopped, when the cause is a known provider state, else None (#743).

    Covers a billing refusal and a breaker refusing the call. Both are already reported by the
    breaker, so a caller reports them as unavailable, without a traceback. Any other error,
    timeouts and 5xx included, returns None and stays an error.
    """
    refusal = circuit_open_cause(exc)
    if refusal is not None:
        snap = refusal.snapshot or {}
        why = " (billing)" if snap.get("reason") == QUOTA_REASON_CODE else ""
        return f"circuit breaker {snap.get('name') or 'unknown'} is {snap.get('state') or 'open'}{why}"
    if isinstance(exc, Exception) and quota_exhausted_error(exc) is not None:
        return "provider refused for billing"
    return None


def should_trip_circuit(exc: Exception) -> bool:
    """Return True when the exception indicates a service-health failure.

    Trip-worthy: connect/read timeouts, connection errors, upstream 429s/5xxs,
    scheduler-unavailable, and equivalent transport-level failures.

    Non-trip: Graphiti parse errors, validation/schema errors, malformed content,
    prompt-shape bugs, and other deterministic library exceptions.
    """

    if isinstance(exc, ProviderQuotaExhaustedError):
        return True

    # A 402 with a retry hint is throttling: counts toward the threshold like a 429.
    if find_transient_payment_refusal(exc) is not None:
        return True

    # Timeouts
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True

    # httpx transport errors
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                        httpx.WriteTimeout, httpx.PoolTimeout)):
        return True

    # httpx status errors (429, 5xx)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or code >= 500:
            return True
        return False

    # Generic connection errors
    if isinstance(exc, (ConnectionError, OSError)):
        return True

    # Check for wrapped status codes in exception args/attributes
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True

    # OpenAI API errors with status codes
    error_type = type(exc).__name__
    if error_type in ("APIConnectionError", "APITimeoutError"):
        return True
    if error_type in ("RateLimitError", "InternalServerError"):
        return True

    return False


@dataclass
class CircuitBreaker:
    """Async circuit breaker with CLOSED/OPEN/HALF_OPEN state machine."""

    name: str
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probe_in_flight: bool = field(default=False, init=False)
    _hard_open_reason: str | None = field(default=None, init=False)
    # Bumped on every billing hard-open, so a probe can tell whether one happened while it ran.
    _hard_open_seq: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # Set when the running probe settles, by any path; a caller allowed to wait for it awaits this
    # instead of being refused (#640).
    _probe_settled: asyncio.Event | None = field(default=None, init=False, repr=False)
    # The loop this breaker's calls run on, so a sync caller on a worker thread can report a
    # billing refusal to it (#639). Set by call().
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _report_tasks: set[asyncio.Future[Any]] = field(default_factory=set, init=False, repr=False)

    def _start_probe(self) -> None:
        self._probe_in_flight = True
        self._probe_settled = asyncio.Event()

    def _end_probe(self) -> None:
        """Free the probe slot and wake every caller waiting on the probe. Loop thread only."""
        self._probe_in_flight = False
        settled, self._probe_settled = self._probe_settled, None
        if settled is not None:
            settled.set()

    def _hard_open(self, *, is_probe: bool) -> CircuitState:
        """Billing state: open now, no failure threshold and no 30 s probes. Caller holds _lock."""
        previous = self._state
        self._failures += 1
        self._state = CircuitState.OPEN
        self._opened_at = monotonic()
        if is_probe:
            # A non-probe refusal recorded while a probe runs must not free the probe slot, or
            # a second probe can start beside the first.
            self._end_probe()
        self._hard_open_reason = QUOTA_REASON_CODE
        self._hard_open_seq += 1
        return previous

    async def _record_hard_open(self, previous: CircuitState, quota_error: Exception) -> None:
        from menhir.infrastructure.telemetry import record_lifecycle_event

        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="circuit_breaker",
                event=f"{previous.value}->{self._state.value}",
                state="transition",
                details={"breaker": self.name, "reason": QUOTA_REASON_CODE},
            )
        )
        logger.warning(
            "Circuit breaker %s hard-open: %s (next probe in %.0fs)",
            self.name, quota_error, QUOTA_COOLDOWN_SECONDS,
        )

    async def record_quota_refusal(self, exc: BaseException) -> bool:
        """Hard-open for a billing refusal a caller got outside call() (#639).

        For provider clients that cannot go through call(), such as the sync view embedder and
        sync chat. Anything that is not a billing refusal is ignored; returns whether it opened.
        """
        quota_error = quota_exhausted_error(exc) if isinstance(exc, Exception) else None
        if quota_error is None:
            return False
        async with self._lock:
            previous = self._hard_open(is_probe=False)
            # Inside the lock, as in _call: the hard-open is recorded before another call runs.
            await self._record_hard_open(previous, quota_error)
        return True

    def report_quota_refusal_threadsafe(self, exc: BaseException) -> None:
        """record_quota_refusal from sync code on any thread. Never raises, never blocks.

        State changes run on the breaker's loop, under its lock, so they cannot interleave with
        a call() in progress. Before any call() has run there is no loop, so the hard-open is
        applied directly; a first call() racing that write can at most run once more.
        """
        try:
            if not isinstance(exc, Exception) or quota_exhausted_error(exc) is None:
                return
            loop = self._loop
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if loop is None or loop.is_closed():
                if running is None:
                    self._hard_open(is_probe=False)
                    logger.warning("Circuit breaker %s hard-open: %s", self.name, exc)
                    return
                loop = running
            if running is loop:
                task = loop.create_task(self.record_quota_refusal(exc))
                # Held until done: the loop keeps only a weak reference to a task.
                self._report_tasks.add(task)
                task.add_done_callback(self._report_tasks.discard)
            else:
                asyncio.run_coroutine_threadsafe(self.record_quota_refusal(exc), loop)
        except Exception:
            logger.warning("Circuit breaker %s: could not record a billing refusal", self.name,
                           exc_info=True)

    def _effective_cooldown(self) -> float:
        return QUOTA_COOLDOWN_SECONDS if self._hard_open_reason else self.cooldown_seconds

    def state_snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of breaker state."""

        remaining = 0.0
        if self._opened_at is not None and self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            remaining = max(0.0, self._effective_cooldown() - (monotonic() - self._opened_at))
        snapshot: dict[str, Any] = {
            "name": self.name,
            "state": self._state.value,
            "failures": self._failures,
            "cooldown_remaining_s": remaining,
        }
        if self._hard_open_reason:
            snapshot["reason"] = self._hard_open_reason
        return snapshot

    def refusal_snapshot(self) -> dict[str, Any] | None:
        """The snapshot a call made now would be refused with, or None if it would run.

        Reads state only. Lets a caller skip work that needs this breaker's provider later, before
        spending on another provider first.
        """
        if self._state == CircuitState.OPEN:
            elapsed = monotonic() - (self._opened_at or monotonic())
            if elapsed < self._effective_cooldown() or self._probe_in_flight:
                return self.state_snapshot()
        elif self._state == CircuitState.HALF_OPEN and self._probe_in_flight:
            return self.state_snapshot()
        return None

    def reset(self) -> None:
        """Close the breaker now; a process restart does the same. Nothing calls it today."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._end_probe()
        self._hard_open_reason = None

    async def _acquire_or_abandon(self, is_probe: bool, *, healthy: bool) -> None:
        """Acquire _lock, settling probe state first if cancellation lands on the wait.

        The bare ``async with self._lock`` form leaks the same wedge ``_abandon_probe``
        exists to close: under contention the acquire suspends, and a CancelledError
        delivered there skips the state cleanup that was about to run.
        """
        try:
            await self._lock.acquire()
        except BaseException:
            if is_probe:
                self._abandon_probe(healthy=healthy)
            raise

    def _abandon_probe(self, *, healthy: bool) -> None:
        """Settle probe state without the lock, for use on the cancellation path.

        Deliberately does NOT acquire self._lock: this runs while the task is being
        cancelled, and awaiting the lock here re-raises CancelledError before the
        cleanup executes -- which is the very wedge this guards against. These are
        plain attribute writes and only one probe can be in flight by construction,
        so nothing races them.

        ``healthy=True`` means fn() already returned, so the backend is known good
        and the breaker closes. ``healthy=False`` means we learned nothing (or
        learned it is down): return to OPEN with a fresh cooldown so the next
        caller may probe again.
        """
        self._end_probe()
        if healthy:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._hard_open_reason = None
        else:
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()

    async def call(
        self, fn: Callable[[], Awaitable[T]], *, wait_for_probe_s: float = 0.0
    ) -> T:
        """Execute fn with circuit breaker state transitions.

        Every exception leaving here is tagged with this breaker's name unless an inner breaker
        tagged it first, so an outer breaker can tell its own provider's failures from another's.

        ``wait_for_probe_s`` > 0: a call that arrives while a half-open probe is running waits up
        to that long for the probe's outcome instead of being refused at once (#640). It then
        runs if the probe closed the breaker, and is refused if the probe failed.
        """
        self._loop = asyncio.get_running_loop()
        try:
            return await self._call(fn, wait_for_probe_s=wait_for_probe_s)
        except Exception as exc:
            tag_breaker_origin(exc, self.name)
            raise

    async def _call(self, fn: Callable[[], Awaitable[T]], *, wait_for_probe_s: float = 0.0) -> T:
        """All state transitions happen inside this method under _lock."""

        deadline = monotonic() + max(0.0, wait_for_probe_s)
        while True:
            settled, is_probe, probe_seq = await self._admit(deadline)
            if settled is None:
                break
            # Wait outside the lock, so the probe can take it to settle. Every path that ends a
            # probe sets the event, cancellation included; the deadline bounds a hung probe.
            try:
                await asyncio.wait_for(settled.wait(), timeout=max(0.0, deadline - monotonic()))
            except (asyncio.TimeoutError, TimeoutError):
                raise CircuitOpenError(
                    f"Circuit breaker open for {self.name}; "
                    f"state={self._state.value} probe in flight (waited {wait_for_probe_s:.0f}s)",
                    snapshot=self.state_snapshot(),
                ) from None
        return await self._run(fn, is_probe=is_probe, probe_seq=probe_seq)

    async def _admit(self, deadline: float) -> tuple[asyncio.Event | None, bool, int]:
        """Admit this call, refuse it (raises), or hand back the running probe's event to wait on.

        Returns ``(event, is_probe, probe_seq)``; ``event`` is None when the call is admitted.
        """

        from menhir.infrastructure.telemetry import record_lifecycle_event

        async with self._lock:
            if self._state == CircuitState.OPEN:
                now = monotonic()
                elapsed = now - (self._opened_at or now)
                # A probe still running (a concurrent billing refusal re-opened the breaker under
                # it) keeps the slot: only one probe at a time.
                if elapsed >= self._effective_cooldown() and not self._probe_in_flight:
                    # Transition to HALF_OPEN — this caller is the probe
                    previous = self._state
                    self._state = CircuitState.HALF_OPEN
                    self._start_probe()
                    # DELIBERATELY SYNCHRONOUS, unlike every other telemetry write in this file.
                    # This one sits inside the lock, between setting `_probe_in_flight = True` and
                    # the caller reaching `await fn()` -- the only stretch not covered by the
                    # `except BaseException` probe-abandon handler below. An await here is a
                    # cancellation point that leaves the breaker HALF_OPEN with a probe flagged
                    # in flight that will never run, wedging it permanently: the exact defect
                    # `test_cancelled_probe_does_not_wedge_breaker` exists to prevent. A breaker
                    # state transition is rare, so the loop stall CF-170 targets is not paid here.
                    record_lifecycle_event(
                        component="circuit_breaker",
                        event=f"{previous.value}->{self._state.value}",
                        state="transition",
                        details={"breaker": self.name},
                    )
                else:
                    raise CircuitOpenError(
                        f"Circuit breaker open for {self.name}; "
                        f"state={self._state.value} failures={self._failures} "
                        f"cooldown_remaining_s={max(0.0, self._effective_cooldown() - elapsed):.0f}",
                        snapshot=self.state_snapshot(),
                    )
            elif self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    if self._probe_settled is not None and monotonic() < deadline:
                        return self._probe_settled, False, 0
                    raise CircuitOpenError(
                        f"Circuit breaker open for {self.name}; "
                        f"state={self._state.value} probe in flight",
                        snapshot=self.state_snapshot(),
                    )
                # Should not reach here — but treat as probe
                self._start_probe()

            return None, self._state == CircuitState.HALF_OPEN, self._hard_open_seq

    async def _run(self, fn: Callable[[], Awaitable[T]], *, is_probe: bool, probe_seq: int) -> T:
        """Run an admitted call and record its outcome."""

        from menhir.infrastructure.telemetry import record_lifecycle_event

        # Execute outside the lock
        try:
            result = await fn()
        except Exception as exc:
            # Classified before taking the lock: nothing between the acquire and the try/finally
            # below may raise, or the lock leaks and every later call hangs.
            try:
                origin = breaker_origin(exc)
                quota_error = quota_exhausted_error(exc)
            except Exception:
                origin, quota_error = None, None
            await self._acquire_or_abandon(is_probe, healthy=False)
            if origin is not None and origin != self.name:
                # Another breaker's provider failed inside this call: no failure counted, no
                # hard-open. A probe learned nothing about this provider, so the billing hold
                # (if any) ends and the normal cooldown restarts: the next probe re-hard-opens if
                # this provider still refuses, and back-to-back probes cannot pile up.
                try:
                    if is_probe:
                        self._state = CircuitState.OPEN
                        self._end_probe()
                        self._opened_at = monotonic()
                        if self._hard_open_seq == probe_seq:
                            self._hard_open_reason = None
                    logger.info(
                        "Circuit breaker %s: %s from breaker %s (state unchanged)",
                        self.name, type(exc).__name__, origin,
                    )
                finally:
                    self._lock.release()
                raise
            try:
                if quota_error is not None:
                    previous = self._hard_open(is_probe=is_probe)
                    await self._record_hard_open(previous, quota_error)
                elif should_trip_circuit(exc):
                    self._failures += 1
                    if is_probe:
                        # HALF_OPEN -> OPEN on failed probe. The provider did not refuse for
                        # billing this time, so the billing hold ends: the normal cooldown
                        # applies, not another quota interval (#492).
                        previous = self._state
                        self._state = CircuitState.OPEN
                        self._opened_at = monotonic()
                        self._end_probe()
                        if self._hard_open_seq == probe_seq:
                            # A billing refusal recorded while this probe ran still stands.
                            self._hard_open_reason = None
                        await asyncio.to_thread(
                            partial(
                                record_lifecycle_event,
                                component="circuit_breaker",
                                event=f"{previous.value}->{self._state.value}",
                                state="transition",
                                details={"breaker": self.name, "failures": self._failures},
                            )
                        )
                    elif self._state == CircuitState.CLOSED and self._failures >= self.failure_threshold:
                        # CLOSED -> OPEN
                        previous = self._state
                        self._state = CircuitState.OPEN
                        self._opened_at = monotonic()
                        await asyncio.to_thread(
                            partial(
                                record_lifecycle_event,
                                component="circuit_breaker",
                                event=f"{previous.value}->{self._state.value}",
                                state="transition",
                                details={"breaker": self.name, "failures": self._failures},
                            )
                        )
                else:
                    # Non-trip failure — log but don't change breaker state
                    if is_probe:
                        # Non-trip error during probe (e.g. parse/schema error) means
                        # the backend is reachable, so close the circuit.
                        previous = self._state
                        self._state = CircuitState.CLOSED
                        self._failures = 0
                        self._opened_at = None
                        self._end_probe()
                        self._hard_open_reason = None
                        await asyncio.to_thread(
                            partial(
                                record_lifecycle_event,
                                component="circuit_breaker",
                                event=f"{previous.value}->{self._state.value}",
                                state="transition",
                                details={"breaker": self.name, "reason": "non_trip_error_probe_closed"},
                            )
                        )
                    logger.info(
                        "Circuit breaker %s: non-trip failure %s (state unchanged)",
                        self.name, type(exc).__name__,
                    )
            finally:
                self._lock.release()
            if quota_error is not None and quota_error is not exc:
                raise quota_error
            raise
        except BaseException:
            # CancelledError / KeyboardInterrupt / SystemExit: the probe never completed,
            # so we learned nothing about backend health. Return to OPEN with a fresh
            # cooldown timer so the next caller may probe again, and clear the in-flight
            # flag that would otherwise wedge the breaker permanently.
            if is_probe:
                self._abandon_probe(healthy=False)
            raise

        # Success. fn() already returned, so a cancellation landing on the lock wait
        # settles the probe as healthy rather than discarding what we just learned.
        await self._acquire_or_abandon(is_probe, healthy=True)
        try:
            if is_probe:
                # HALF_OPEN -> CLOSED on successful probe
                previous = self._state
                self._state = CircuitState.CLOSED
                self._failures = 0
                self._opened_at = None
                self._end_probe()
                self._hard_open_reason = None
                await asyncio.to_thread(
                    partial(
                        record_lifecycle_event,
                        component="circuit_breaker",
                        event=f"{previous.value}->{self._state.value}",
                        state="transition",
                        details={"breaker": self.name},
                    )
                )
            elif self._state == CircuitState.CLOSED:
                # Reset consecutive failure counter on success
                self._failures = 0
        finally:
            self._lock.release()

        return result
