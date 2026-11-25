"""#743: a billing refusal or an open breaker is reported as unavailable, not as a server fault.

/api/phase3/run answers 503 with the reason; the scheduler logs a warning without a traceback and
records the failure as retryable. Any other error keeps its old handling.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi import HTTPException

from menhir.infrastructure.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    provider_unavailable_reason,
)

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")


def _quota_429() -> openai.RateLimitError:
    return openai.RateLimitError(
        "Error code: 429 - insufficient_quota",
        response=httpx.Response(429, request=_REQ),
        body={"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"},
    )


def _open_refusal(reason: str | None = None) -> CircuitOpenError:
    snap: dict[str, Any] = {"name": "llm:http://x/v1", "state": "open"}
    if reason:
        snap["reason"] = reason
    return CircuitOpenError("Circuit breaker open for llm:http://x/v1", snapshot=snap)


# --- classifier --------------------------------------------------------------------------------


def test_billing_refusal_is_unavailable() -> None:
    assert provider_unavailable_reason(_quota_429()) == "provider refused for billing"


def test_open_breaker_is_unavailable_and_names_the_breaker() -> None:
    assert provider_unavailable_reason(_open_refusal()) == "circuit breaker llm:http://x/v1 is open"
    assert provider_unavailable_reason(_open_refusal("provider_quota_exhausted")).endswith(
        "is open (billing)")


def test_wrapped_refusal_is_found_through_the_cause_chain() -> None:
    try:
        try:
            raise _open_refusal()
        except CircuitOpenError as inner:
            raise RuntimeError("consolidation pass failed") from inner
    except RuntimeError as outer:
        assert provider_unavailable_reason(outer) is not None


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError("slow"),
    openai.APIStatusError("503", response=httpx.Response(503, request=_REQ), body=None),
    ValueError("bad payload"),
])
def test_other_errors_are_not_unavailable(exc: Exception) -> None:
    assert provider_unavailable_reason(exc) is None


async def test_a_real_breaker_refusal_is_unavailable() -> None:
    breaker = CircuitBreaker(name="llm:x")

    async def refuse():
        raise _quota_429()

    with pytest.raises(Exception):
        await breaker.call(refuse)
    with pytest.raises(CircuitOpenError) as refused:
        await breaker.call(lambda: asyncio.sleep(0))
    assert provider_unavailable_reason(refused.value) == "circuit breaker llm:x is open (billing)"


# --- /api/phase3/run ---------------------------------------------------------------------------


async def _run_route(monkeypatch: pytest.MonkeyPatch, error: Exception):
    from menhir.api import routes_handlers
    from menhir.infrastructure import sync_llm, view_embedder
    from menhir.services import scheduler_tasks

    monkeypatch.setattr(sync_llm, "make_sync_chat", lambda *a, **k: (lambda s, u: "[]"))
    monkeypatch.setattr(view_embedder, "make_view_embedder", lambda *a, **k: None)
    monkeypatch.setattr(view_embedder, "view_embedder_version", lambda *a, **k: None)
    monkeypatch.setattr(routes_handlers, "require_namespace_target", lambda ns, **k: ns)

    async def dirty(*_a, **_k):
        return True

    monkeypatch.setattr(routes_handlers, "_scalar_dirty", dirty)

    async def consolidate(*_a, **_k):
        raise error

    monkeypatch.setattr(scheduler_tasks, "consolidate_personal_memory", consolidate)
    runtime_ctx = SimpleNamespace(built=SimpleNamespace(settings=SimpleNamespace(),
                                                        graphiti_client=None))
    return await routes_handlers.phase3_run_impl(
        SimpleNamespace(),
        SimpleNamespace(namespace="ns", k=1, source="perception", call_budget=None),
        require_tier=lambda tier: None,
        require_phase3_adapter=lambda request: (runtime_ctx, object()),
    )


@pytest.mark.parametrize("error, detail", [
    (_quota_429(), "provider refused for billing"),
    (_open_refusal("provider_quota_exhausted"), "circuit breaker llm:http://x/v1 is open (billing)"),
])
async def test_phase3_run_answers_503_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, caplog, error: Exception, detail: str
) -> None:
    caplog.set_level(logging.WARNING, logger="menhir.api.routes_handlers")
    with pytest.raises(HTTPException) as raised:
        await _run_route(monkeypatch, error)
    assert raised.value.status_code == 503
    assert raised.value.detail == f"phase 3 consolidation unavailable: {detail}"
    logged = [r for r in caplog.records if r.name == "menhir.api.routes_handlers"]
    assert logged and all(r.levelno == logging.WARNING and r.exc_info is None for r in logged)


async def test_phase3_run_other_errors_still_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="bad payload"):
        await _run_route(monkeypatch, ValueError("bad payload"))


# --- scheduler ---------------------------------------------------------------------------------


async def _run_scheduler_job(monkeypatch: pytest.MonkeyPatch, error: Exception):
    from menhir.services import maintenance_scheduler as ms

    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "record_failure_event", lambda **kw: failures.append(kw))
    monkeypatch.setattr(ms, "record_mcp_event", lambda **kw: None)

    class _Ingest:
        def get_queue_depth(self):
            return 0

    sched = ms.MaintenanceScheduler(ingest_service=_Ingest(), graph_adapter=object())

    async def passthrough(coro, operation):
        return await coro

    monkeypatch.setattr(sched, "_await_job_under_lease", passthrough)

    async def job():
        raise error

    state = ms._JobState(interval_s=60.0)
    await sched._run_job(state, "scheduler_consolidate_personal_memory", job())
    return state, failures


async def test_scheduler_reports_a_billing_refusal_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger="menhir.services.maintenance_scheduler")
    state, failures = await _run_scheduler_job(monkeypatch, _open_refusal("provider_quota_exhausted"))
    assert state.last_success is False
    assert state.last_result["provider_unavailable"].endswith("(billing)")
    [failure] = failures
    assert failure["classification"] == "provider_unavailable"
    assert failure["retryable"] is True and failure["traceback_text"] == ""
    logged = [r for r in caplog.records if r.name == "menhir.services.maintenance_scheduler"]
    assert logged and all(r.levelno == logging.WARNING and r.exc_info is None for r in logged)


async def test_scheduler_keeps_the_traceback_for_other_errors(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger="menhir.services.maintenance_scheduler")
    state, failures = await _run_scheduler_job(monkeypatch, ValueError("bad payload"))
    [failure] = failures
    assert failure["classification"] == "scheduler_error" and failure["retryable"] is False
    assert "ValueError" in failure["traceback_text"]
    assert "provider_unavailable" not in state.last_result
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)
