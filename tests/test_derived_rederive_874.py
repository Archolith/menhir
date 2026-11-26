"""#874: provider outages must not spend repair attempts, exhaustion must be visible, and one
item's graph error must not end the batch."""

from __future__ import annotations

import asyncio

import httpx
import openai

from menhir.infrastructure.llm import LLMAdapter
from menhir.services import derived_rederive as dr
from menhir.services.derived_rederive import MAX_ATTEMPTS, run_derived_rederive
from tests.test_derived_rederive_unit import FakeAdapter, FakeLLM, _item


def _repairs(n):
    return [
        {"repair_key": f"k{i}", "item_kind": "fact", "item_uuid": f"u{i}", "namespace": "default"}
        for i in range(n)
    ]


def _items(n):
    return {("fact", f"u{i}"): _item() for i in range(n)}


def _rate_limit_error() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"code": "rate_limit_exceeded"}})
    return openai.RateLimitError("slow down", response=response, body={"code": "rate_limit_exceeded"})


class _Backend:
    def __init__(self, exc=None, answer=""):
        self.exc = exc
        self.answer = answer
        self.calls = 0

    async def create_chat_completion(self, **_kw):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.answer


def _adapter(backend) -> LLMAdapter:
    return LLMAdapter(
        base_url="http://example.invalid", api_key="k", chat_model="m", embed_model="e",
        backend=backend,
    )


def _assert_stopped_untouched(adapter, result, n):
    assert result["stopped"] == "provider_unavailable"
    assert result["errors"] == 0 and result["failed"] == 0
    for i in range(n):
        repair = adapter.repairs[f"k{i}"]
        assert repair["status"] == "pending" and repair["attempts"] == 0
    assert not adapter.applied


def test_timeout_through_real_adapter_leaves_pending_and_stops_batch():
    adapter = FakeAdapter(_items(3), _repairs(3))
    backend = _Backend(exc=TimeoutError("read timed out"))
    result = asyncio.run(run_derived_rederive(adapter, _adapter(backend)))
    _assert_stopped_untouched(adapter, result, 3)
    assert backend.calls == 1  # stopped after the first item


def test_provider_429_through_real_adapter_leaves_pending_and_stops_batch():
    adapter = FakeAdapter(_items(3), _repairs(3))
    backend = _Backend(exc=_rate_limit_error())
    result = asyncio.run(run_derived_rederive(adapter, _adapter(backend)))
    _assert_stopped_untouched(adapter, result, 3)
    assert backend.calls == 1


def test_connection_error_leaves_pending_and_stops_batch():
    adapter = FakeAdapter(_items(2), _repairs(2))
    llm = FakeLLM(exc=ConnectionError("refused"))
    result = asyncio.run(run_derived_rederive(adapter, llm))
    _assert_stopped_untouched(adapter, result, 2)
    assert llm.calls == 1


def test_repeated_outages_never_fail_a_repair():
    adapter = FakeAdapter(_items(1), _repairs(1))
    llm = _adapter(_Backend(exc=TimeoutError()))
    for _ in range(MAX_ATTEMPTS + 2):
        asyncio.run(run_derived_rederive(adapter, llm))
    assert adapter.repairs["k0"]["status"] == "pending" and adapter.repairs["k0"]["attempts"] == 0


def test_unparseable_answer_still_spends_an_attempt():
    adapter = FakeAdapter(_items(2), _repairs(2))
    result = asyncio.run(run_derived_rederive(adapter, _adapter(_Backend(answer="I am not sure"))))
    assert "stopped" not in result
    assert result["errors"] == 2
    assert all(adapter.repairs[f"k{i}"]["attempts"] == 1 for i in range(2))
    assert adapter.repairs["k0"]["last_error"] == "no_verdict"


def test_none_verdict_still_spends_an_attempt():
    adapter = FakeAdapter(_items(1), _repairs(1))
    result = asyncio.run(run_derived_rederive(adapter, FakeLLM(verdict=None)))
    assert result["errors"] == 1 and "stopped" not in result
    assert adapter.repairs["k0"]["attempts"] == 1 and adapter.repairs["k0"]["status"] == "pending"


def test_max_attempts_records_a_failure_event(monkeypatch):
    events = []
    monkeypatch.setattr(dr, "record_failure_event", lambda **kw: events.append(kw) or True)
    adapter = FakeAdapter(_items(1), _repairs(1))
    llm = FakeLLM(verdict=None)
    for _ in range(MAX_ATTEMPTS - 1):
        asyncio.run(run_derived_rederive(adapter, llm))
    assert events == []
    result = asyncio.run(run_derived_rederive(adapter, llm))
    assert result["failed"] == 1 and adapter.repairs["k0"]["status"] == "failed"
    assert len(events) == 1
    event = events[0]
    assert event["operation"] == "scheduler_derived_rederive"
    assert event["failure_stage"] == "rederive_attempts_exhausted"
    assert event["retryable"] is False and event["error_type"] == "no_verdict"
    assert "old fact" not in repr(event) and "remaining source" not in repr(event)


def test_max_attempts_failure_event_persists_to_telemetry():
    from menhir.infrastructure.telemetry import telemetry_store

    adapter = FakeAdapter(_items(1), _repairs(1))
    llm = FakeLLM(verdict=None)
    for _ in range(MAX_ATTEMPTS):
        asyncio.run(run_derived_rederive(adapter, llm))
    with telemetry_store._connect() as conn:
        rows = conn.execute(
            "SELECT failure_stage FROM failure_events WHERE operation = 'scheduler_derived_rederive'"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("rederive_attempts_exhausted",)]


class _FlakyAdapter(FakeAdapter):
    def __init__(self, *args, raise_on, method, **kw):
        super().__init__(*args, **kw)
        self.raise_on = raise_on
        self.method = method

    def load_derived_rederive_item(self, kind, uuid):
        if self.method == "load" and uuid == self.raise_on:
            raise RuntimeError("neo4j down for this one")
        return super().load_derived_rederive_item(kind, uuid)

    def apply_derived_rederive(self, **kw):
        if self.method == "apply" and kw["item_uuid"] == self.raise_on:
            raise RuntimeError("neo4j write failed")
        return super().apply_derived_rederive(**kw)


def test_one_item_raising_in_apply_does_not_abort_the_others():
    adapter = _FlakyAdapter(_items(3), _repairs(3), raise_on="u1", method="apply")
    result = asyncio.run(run_derived_rederive(adapter, FakeLLM(("supported", "new fact"))))
    assert result["processed"] == 3 and result["rewritten"] == 2 and result["errors"] == 1
    assert adapter.repairs["k0"]["status"] == "done"
    assert adapter.repairs["k1"]["status"] == "pending" and adapter.repairs["k1"]["attempts"] == 0
    assert adapter.repairs["k2"]["status"] == "done"


def test_one_item_raising_in_load_does_not_abort_the_others():
    adapter = _FlakyAdapter(_items(3), _repairs(3), raise_on="u0", method="load")
    result = asyncio.run(run_derived_rederive(adapter, FakeLLM(("supported", "new fact"))))
    assert result["errors"] == 1 and result["rewritten"] == 2


def test_cancellation_is_not_swallowed():
    class _Cancelling(FakeAdapter):
        def apply_derived_rederive(self, **kw):
            raise asyncio.CancelledError()

    adapter = _Cancelling(_items(2), _repairs(2))
    try:
        asyncio.run(run_derived_rederive(adapter, FakeLLM(("supported", "x"))))
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover
        raise AssertionError("CancelledError was swallowed")


def test_other_chat_text_callers_still_get_none_on_timeout():
    adapter = _adapter(_Backend(exc=TimeoutError()))
    result = asyncio.run(adapter._chat_text(system_prompt="s", user_prompt="u", operation="op"))
    assert result is None
