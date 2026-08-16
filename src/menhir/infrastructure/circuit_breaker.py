"""Circuit breaker for external service calls.

M6 Phase 1 — CLOSED/OPEN/HALF_OPEN state machine with failure classification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Any, Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, message: str, *, snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.snapshot = snapshot


def should_trip_circuit(exc: Exception) -> bool:
    """Return True when the exception indicates a service-health failure.

    Trip-worthy: connect/read timeouts, connection errors, upstream 429s/5xxs,
    scheduler-unavailable, and equivalent transport-level failures.

    Non-trip: Graphiti parse errors, validation/schema errors, malformed content,
    prompt-shape bugs, and other deterministic library exceptions.
    """

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
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def state_snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of breaker state."""

        remaining = 0.0
        if self._opened_at is not None and self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            remaining = max(0.0, self.cooldown_seconds - (monotonic() - self._opened_at))
        return {
            "name": self.name,
            "state": self._state.value,
            "failures": self._failures,
            "cooldown_remaining_s": remaining,
        }

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute fn with circuit breaker state transitions.

        All state transitions happen inside this method under _lock.
        """

        from menhir.infrastructure.telemetry import record_lifecycle_event

        async with self._lock:
            if self._state == CircuitState.OPEN:
                now = monotonic()
                elapsed = now - (self._opened_at or now)
                if elapsed >= self.cooldown_seconds:
                    # Transition to HALF_OPEN — this caller is the probe
                    previous = self._state
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
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
                        f"cooldown_remaining_s={self.cooldown_seconds - elapsed:.0f}",
                        snapshot=self.state_snapshot(),
                    )
            elif self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(
                        f"Circuit breaker open for {self.name}; "
                        f"state={self._state.value} probe in flight",
                        snapshot=self.state_snapshot(),
                    )
                # Should not reach here — but treat as probe
                self._probe_in_flight = True

            is_probe = self._state == CircuitState.HALF_OPEN

        # Execute outside the lock
        try:
            result = await fn()
        except Exception as exc:
            async with self._lock:
                if should_trip_circuit(exc):
                    self._failures += 1
                    if is_probe:
                        # HALF_OPEN -> OPEN on failed probe
                        previous = self._state
                        self._state = CircuitState.OPEN
                        self._opened_at = monotonic()
                        self._probe_in_flight = False
                        record_lifecycle_event(
                            component="circuit_breaker",
                            event=f"{previous.value}->{self._state.value}",
                            state="transition",
                            details={"breaker": self.name, "failures": self._failures},
                        )
                    elif self._state == CircuitState.CLOSED and self._failures >= self.failure_threshold:
                        # CLOSED -> OPEN
                        previous = self._state
                        self._state = CircuitState.OPEN
                        self._opened_at = monotonic()
                        record_lifecycle_event(
                            component="circuit_breaker",
                            event=f"{previous.value}->{self._state.value}",
                            state="transition",
                            details={"breaker": self.name, "failures": self._failures},
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
                        self._probe_in_flight = False
                        record_lifecycle_event(
                            component="circuit_breaker",
                            event=f"{previous.value}->{self._state.value}",
                            state="transition",
                            details={"breaker": self.name, "reason": "non_trip_error_probe_closed"},
                        )
                    logger.info(
                        "Circuit breaker %s: non-trip failure %s (state unchanged)",
                        self.name, type(exc).__name__,
                    )
            raise
        except BaseException:
            # CancelledError / KeyboardInterrupt / SystemExit: the probe never completed,
            # so we learned nothing about backend health. Return to OPEN with a fresh
            # cooldown timer so the next caller may probe again, and clear the in-flight
            # flag that would otherwise wedge the breaker permanently.
            #
            # Deliberately does NOT acquire self._lock: this runs while the task is being
            # cancelled, and awaiting the lock here can re-raise CancelledError before the
            # cleanup executes, reintroducing the bug. These are plain attribute writes and
            # only one probe can be in flight by construction, so nothing races them.
            if is_probe:
                self._state = CircuitState.OPEN
                self._opened_at = monotonic()
                self._probe_in_flight = False
            raise

        # Success
        async with self._lock:
            if is_probe:
                # HALF_OPEN -> CLOSED on successful probe
                previous = self._state
                self._state = CircuitState.CLOSED
                self._failures = 0
                self._opened_at = None
                self._probe_in_flight = False
                record_lifecycle_event(
                    component="circuit_breaker",
                    event=f"{previous.value}->{self._state.value}",
                    state="transition",
                    details={"breaker": self.name},
                )
            elif self._state == CircuitState.CLOSED:
                # Reset consecutive failure counter on success
                self._failures = 0

        return result
