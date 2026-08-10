"""Unit tests for the circuit breaker (M6 Phase 1)."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from menhir.infrastructure.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    should_trip_circuit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    request = httpx.Request("GET", "http://test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} error", request=request, response=response,
    )


async def _succeed() -> str:
    return "ok"


async def _fail_timeout() -> str:
    raise asyncio.TimeoutError("upstream timed out")


async def _fail_value() -> str:
    raise ValueError("bad parse")


# ---------------------------------------------------------------------------
# 1. CircuitBreaker opens after failure_threshold (3) consecutive failures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opens_after_failure_threshold():
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=30.0)

    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(_fail_timeout)

    assert breaker._state == CircuitState.OPEN
    assert breaker._failures == 3


# ---------------------------------------------------------------------------
# 2. Cooldown expiry transitions next caller to HALF_OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cooldown_expiry_transitions_to_half_open():
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=0.0)

    # Trip the breaker
    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(_fail_timeout)

    assert breaker._state == CircuitState.OPEN

    # Cooldown is 0 seconds so the next call should transition to HALF_OPEN
    # and succeed, resetting to CLOSED.  We verify HALF_OPEN was reached
    # by checking the lifecycle event mock.
    with patch("menhir.infrastructure.telemetry.record_lifecycle_event") as mock_event:
        result = await breaker.call(_succeed)

    assert result == "ok"
    # The open->half_open transition should have been emitted
    transition_events = [
        c for c in mock_event.call_args_list
        if c.kwargs.get("event") == "open->half_open"
    ]
    assert len(transition_events) == 1


# ---------------------------------------------------------------------------
# 3. Circuit rejects calls before cooldown expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_calls_before_cooldown_expiry():
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=9999.0)

    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(_fail_timeout)

    assert breaker._state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError) as exc_info:
        await breaker.call(_succeed)

    assert "Circuit breaker open" in str(exc_info.value)
    assert "snapshot" in dir(exc_info.value)
    assert exc_info.value.snapshot["state"] == "open"


# ---------------------------------------------------------------------------
# 4. Successful call after cooldown resets failure count (half-open probe)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_probe_resets_failure_count():
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=0.0)

    # Trip the breaker
    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(_fail_timeout)

    assert breaker._failures == 3
    assert breaker._state == CircuitState.OPEN

    # Cooldown=0 so probe is allowed immediately
    result = await breaker.call(_succeed)

    assert result == "ok"
    assert breaker._state == CircuitState.CLOSED
    assert breaker._failures == 0
    assert breaker._opened_at is None


# ---------------------------------------------------------------------------
# 5. Concurrent callers after cooldown allow exactly one probe and reject
#    followers (single-probe safety)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_callers_single_probe():
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=0.0)

    # Trip the breaker
    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(_fail_timeout)

    assert breaker._state == CircuitState.OPEN

    # The first concurrent caller will become the probe (HALF_OPEN) and hold the
    # slot.  The second concurrent caller should be rejected.
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    async def slow_probe() -> str:
        probe_entered.set()
        await probe_release.wait()
        return "probe_ok"

    results: list[str | CircuitOpenError] = []

    async def caller_probe():
        result = await breaker.call(slow_probe)
        results.append(result)

    async def caller_follower():
        await probe_entered.wait()
        # Give the probe a chance to be fully registered
        await asyncio.sleep(0)
        try:
            await breaker.call(_succeed)
            results.append("follower_ok")
        except CircuitOpenError as exc:
            results.append(exc)

    task_probe = asyncio.create_task(caller_probe())
    task_follower = asyncio.create_task(caller_follower())

    # Let the follower attempt while probe is in flight
    await asyncio.sleep(0.05)
    probe_release.set()

    await asyncio.gather(task_probe, task_follower)

    # Probe should have succeeded
    assert "probe_ok" in results
    # Follower should have been rejected with CircuitOpenError
    rejected = [r for r in results if isinstance(r, CircuitOpenError)]
    assert len(rejected) == 1
    assert "probe in flight" in str(rejected[0])


# ---------------------------------------------------------------------------
# 6. IngestService requeues episode as PENDING when circuit is open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_service_requeues_on_circuit_open():
    """When _run_graphiti_extraction raises CircuitOpenError, IngestService
    should call graph_adapter.mark_episode_pending with retry_after_s=30.0."""

    from menhir.services.ingest_service import IngestService

    mock_graph_adapter = MagicMock()
    mock_graph_adapter.mark_episode_pending = MagicMock(return_value=True)
    mock_graph_adapter.claim_pending_episode = MagicMock(return_value={
        "uuid": "ep-123",
        "name": "test",
        "content": "test content",
        "session_id": "sess",
        "user_id": "user",
        "source": "mcp",
        "source_confidence": 0.9,
        "processing_state": "ENRICHING",
        "processing_owner": "worker-1",
        "processing_attempts": 1,
        "processing_stage": "claiming",
        "processing_substage": "lease_acquired",
        "processing_steps_total": 5,
        "processing_steps_completed": 0,
        "processing_progress": 5.0,
    })
    mock_graph_adapter.update_episode_processing = MagicMock(return_value=True)
    mock_graph_adapter.increment_episode_llm_usage = MagicMock(return_value=True)
    mock_graph_adapter.fetch_relevant_pending_episodes = MagicMock(return_value=[])
    mock_graph_adapter.find_completed_episode_artifact = MagicMock(return_value=None)
    mock_graph_adapter.touch_episode_processing_heartbeat = MagicMock(return_value=True)

    svc = IngestService(
        graphiti_client=MagicMock(),
        graph_adapter=mock_graph_adapter,
        llm=MagicMock(),
    )

    circuit_error = CircuitOpenError(
        "Circuit breaker open for graphiti",
        snapshot={"name": "graphiti", "state": "open", "failures": 3, "cooldown_remaining_s": 25.0},
    )

    with patch("menhir.services.ingest_worker.run_graphiti_extraction", new_callable=AsyncMock, side_effect=circuit_error), \
         patch("menhir.services.ingest_worker.try_reconcile_existing", new_callable=AsyncMock, return_value=False), \
         patch("menhir.services.ingest_worker.run_preflight_rejection", new_callable=AsyncMock, return_value=False), \
         patch("menhir.services.ingest_worker.record_lifecycle_event"), \
         patch("menhir.services.ingest_worker.set_llm_usage_callback", return_value="token"), \
         patch("menhir.services.ingest_worker.reset_llm_usage_callback"), \
         patch("menhir.services.ingest_worker.record_episode_task_event"):
        await svc._process_pending_episode("ep-123")

    mock_graph_adapter.mark_episode_pending.assert_called_once_with(
        "ep-123", retry_after_s=30.0, worker_id=svc._worker_id,
    )


# ---------------------------------------------------------------------------
# 7. Circuit state transition emits record_lifecycle_event (observability)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_transition_emits_lifecycle_event():
    breaker = CircuitBreaker(name="graphiti", failure_threshold=3, cooldown_seconds=0.0)

    with patch("menhir.infrastructure.telemetry.record_lifecycle_event") as mock_event:
        # Trip the breaker: 3 failures -> CLOSED->OPEN
        for _ in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await breaker.call(_fail_timeout)

        # Verify CLOSED->OPEN transition event
        open_events = [
            c for c in mock_event.call_args_list
            if c.kwargs.get("event") == "closed->open"
        ]
        assert len(open_events) == 1
        assert open_events[0].kwargs["component"] == "circuit_breaker"
        assert open_events[0].kwargs["state"] == "transition"
        assert open_events[0].kwargs["details"]["breaker"] == "graphiti"

        # Now allow probe (cooldown=0) -> OPEN->HALF_OPEN then HALF_OPEN->CLOSED
        mock_event.reset_mock()
        result = await breaker.call(_succeed)
        assert result == "ok"

        half_open_events = [
            c for c in mock_event.call_args_list
            if c.kwargs.get("event") == "open->half_open"
        ]
        assert len(half_open_events) == 1

        closed_events = [
            c for c in mock_event.call_args_list
            if c.kwargs.get("event") == "half_open->closed"
        ]
        assert len(closed_events) == 1


# ---------------------------------------------------------------------------
# 8. Graphiti parse/validation failures do NOT increment breaker state
# ---------------------------------------------------------------------------

class TestShouldNotTripCircuit:
    """Non-trip classification: parse/validation errors should not trip."""

    def test_value_error_does_not_trip(self):
        assert should_trip_circuit(ValueError("bad parse")) is False

    def test_key_error_does_not_trip(self):
        assert should_trip_circuit(KeyError("missing field")) is False

    def test_json_decode_error_does_not_trip(self):
        exc = json.JSONDecodeError("bad json", "", 0)
        assert should_trip_circuit(exc) is False

    def test_generic_exception_does_not_trip(self):
        assert should_trip_circuit(Exception("something went wrong")) is False

    def test_http_400_does_not_trip(self):
        exc = _make_http_status_error(400)
        assert should_trip_circuit(exc) is False

    def test_http_404_does_not_trip(self):
        exc = _make_http_status_error(404)
        assert should_trip_circuit(exc) is False


@pytest.mark.asyncio
async def test_non_trip_failure_does_not_increment_breaker():
    """A ValueError inside the breaker must not increment failure count."""
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=30.0)

    with pytest.raises(ValueError):
        await breaker.call(_fail_value)

    assert breaker._failures == 0
    assert breaker._state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 9. Transport timeout / upstream 5xx increments breaker state
# ---------------------------------------------------------------------------

class TestShouldTripCircuit:
    """Trip classification: transport/upstream failures should trip."""

    def test_asyncio_timeout_error_trips(self):
        assert should_trip_circuit(asyncio.TimeoutError()) is True

    def test_timeout_error_trips(self):
        assert should_trip_circuit(TimeoutError()) is True

    def test_httpx_connect_error_trips(self):
        request = httpx.Request("GET", "http://test")
        exc = httpx.ConnectError("connect failed", request=request)
        assert should_trip_circuit(exc) is True

    def test_httpx_read_timeout_trips(self):
        request = httpx.Request("GET", "http://test")
        exc = httpx.ReadTimeout("read timeout", request=request)
        assert should_trip_circuit(exc) is True

    def test_httpx_connect_timeout_trips(self):
        request = httpx.Request("GET", "http://test")
        exc = httpx.ConnectTimeout("connect timeout", request=request)
        assert should_trip_circuit(exc) is True

    def test_http_429_trips(self):
        exc = _make_http_status_error(429)
        assert should_trip_circuit(exc) is True

    def test_http_500_trips(self):
        exc = _make_http_status_error(500)
        assert should_trip_circuit(exc) is True

    def test_http_502_trips(self):
        exc = _make_http_status_error(502)
        assert should_trip_circuit(exc) is True

    def test_http_503_trips(self):
        exc = _make_http_status_error(503)
        assert should_trip_circuit(exc) is True

    def test_connection_error_trips(self):
        assert should_trip_circuit(ConnectionError("refused")) is True

    def test_os_error_trips(self):
        assert should_trip_circuit(OSError("network unreachable")) is True


@pytest.mark.asyncio
async def test_trip_failure_increments_breaker():
    """A TimeoutError inside the breaker must increment consecutive failures."""
    breaker = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=30.0)

    with pytest.raises(asyncio.TimeoutError):
        await breaker.call(_fail_timeout)

    assert breaker._failures == 1
    assert breaker._state == CircuitState.CLOSED

    with pytest.raises(asyncio.TimeoutError):
        await breaker.call(_fail_timeout)

    assert breaker._failures == 2
    assert breaker._state == CircuitState.CLOSED
