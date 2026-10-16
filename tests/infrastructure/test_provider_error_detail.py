"""Provider failures are labelled by kind in the backoff log and in llm_usage_events.error_code."""
from __future__ import annotations

import logging
import sqlite3

import httpx
import openai
import pytest

from menhir.infrastructure import openai_rate_limit as rl
from menhir.infrastructure.observability import (
    LLMCallHandle,
    fail_llm_usage_call,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.infrastructure.telemetry.recorders import record_llm_usage_event
from menhir.infrastructure.telemetry.store import McpTelemetryStore

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(cls, status: int, code: str | None, *, headers=None,
                  message="secret prompt text"):
    response = httpx.Response(status, request=_REQ, headers=headers or {})
    body = {"message": message}
    if code is not None:
        body.update({"code": code, "type": code})
    return cls(message, response=response, body=body)


def _rate_limit(code: str | None, **kw):
    return _status_error(openai.RateLimitError, 429, code, **kw)


class _GraphitiRateLimit(Exception):
    """Stands in for Graphiti's rewrapped RateLimitError (raise ... from e)."""


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("resource_unavailable", "flex_capacity"),
        ("rate_limit_exceeded", "rate_limit"),
        ("insufficient_quota", "quota"),
        (None, "rate_limit"),
    ],
)
def test_rate_limit_category(code, category):
    assert rl.rate_limit_category(_rate_limit(code)) == category


def test_category_is_none_for_non_429():
    assert rl.rate_limit_category(_status_error(openai.InternalServerError, 500, None)) is None
    assert rl.rate_limit_category(openai.APITimeoutError(request=_REQ)) is None
    assert rl.rate_limit_category(None) is None


def test_category_and_code_survive_graphiti_rewrap():
    inner = _rate_limit("resource_unavailable")
    try:
        try:
            raise inner
        except openai.RateLimitError as exc:
            raise _GraphitiRateLimit("rate limited") from exc
    except _GraphitiRateLimit as wrapped:
        assert rl.rate_limit_category(wrapped) == "flex_capacity"
        assert rl.provider_error_code(wrapped) == "RateLimitError:429:resource_unavailable"


def test_provider_error_code_shapes():
    throttled = _rate_limit("rate_limit_exceeded")
    server_error = _status_error(openai.InternalServerError, 500, None)
    assert rl.provider_error_code(throttled) == "RateLimitError:429:rate_limit_exceeded"
    assert rl.provider_error_code(server_error) == "InternalServerError:500"
    assert rl.provider_error_code(openai.APITimeoutError(request=_REQ)) == "APITimeoutError"
    assert rl.provider_error_code(ValueError("prompt text here")) == "ValueError"
    assert rl.provider_error_code(None) is None


def test_provider_error_code_never_carries_free_text():
    hostile = _rate_limit("you said: my password is hunter2")
    code = rl.provider_error_code(hostile)
    assert code == "RateLimitError:429"
    assert "hunter2" not in rl.describe_provider_error(hostile)


def test_malformed_provider_fields_never_raise():
    exc = _rate_limit(None)
    exc.code = {"nested": "object"}
    exc.type = ["list"]
    assert rl.rate_limit_category(exc) == "rate_limit"
    assert rl.provider_error_code(exc) == "RateLimitError:429"
    assert rl.describe_provider_error(exc).startswith("rate_limit")
    exc.status_code = "not-a-number"
    assert rl.provider_error_code(exc) == "RateLimitError:0"
    assert isinstance(rl.describe_provider_error(exc), str)


def test_describe_includes_hint_and_request_id():
    exc = _rate_limit(
        "resource_unavailable", headers={"retry-after-ms": "2500", "x-request-id": "req_abc123"}
    )
    text = rl.describe_provider_error(exc)
    assert text.startswith("flex_capacity")
    assert "code=resource_unavailable" in text
    assert "retry-after=2.5s" in text
    assert "request_id=req_abc123" in text
    assert "secret prompt text" not in text


def test_backoff_log_names_the_kind(caplog):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise _rate_limit("resource_unavailable")
        return "ok"

    with caplog.at_level(logging.WARNING, logger=rl.__name__):
        assert rl.call_with_rate_limit_backoff(fn, label="graphiti", sleep=lambda s: None) == "ok"
    messages = [r.getMessage() for r in caplog.records if r.name == rl.__name__]
    assert len(messages) == 1
    assert "OpenAI 429 on graphiti (flex_capacity, code=resource_unavailable" in messages[0]
    assert "secret prompt text" not in messages[0]


def test_failed_call_persists_error_code_but_not_text(tmp_path):
    store = McpTelemetryStore(tmp_path / "telemetry.db")
    events = []
    token = set_llm_usage_callback(events.append)
    try:
        handle = LLMCallHandle(
            call_id="call-1", kind="chat", model="gpt-6-luna",
            endpoint="chat.completions.create", operation="graphiti", started_at=0.0,
        )
        fail_llm_usage_call(handle, _rate_limit("resource_unavailable"))
    finally:
        reset_llm_usage_callback(token)
    assert len(events) == 1
    assert events[0].error_code == "RateLimitError:429:resource_unavailable"
    assert record_llm_usage_event(event=events[0], episode_uuid="ep-1", store=store)

    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        error, error_code = conn.execute(
            "SELECT error, error_code FROM llm_usage_events WHERE call_id = 'call-1'"
        ).fetchone()
    assert error == "[redacted]"
    assert error_code == "RateLimitError:429:resource_unavailable"
    summary = store.fetch_llm_usage_summary()
    assert summary["failed_by_error_code"] == {"RateLimitError:429:resource_unavailable": 1}


def test_store_drops_malformed_error_code(tmp_path):
    store = McpTelemetryStore(tmp_path / "telemetry.db")
    assert store.record_llm_usage_event(
        call_id="call-2", recorded_at="2026-10-15T00:00:00Z", run_id=None, episode_uuid="ep-2",
        operation=None, kind="chat", model=None, endpoint=None, status="failed", duration_ms=1,
        input_tokens=None, output_tokens=None, total_tokens=None, cached_input_tokens=None,
        reasoning_output_tokens=None, provider_usage_json=None, error="[redacted]",
        error_code="RateLimitError:429:the user wrote something private",
    )
    with sqlite3.connect(tmp_path / "telemetry.db") as conn:
        (error_code,) = conn.execute(
            "SELECT error_code FROM llm_usage_events WHERE call_id = 'call-2'"
        ).fetchone()
    assert error_code is None


def test_pre_existing_sidecar_gains_error_code_column(tmp_path):
    path = tmp_path / "telemetry.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE llm_usage_events (call_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, "
            "run_id TEXT, episode_uuid TEXT, operation TEXT, kind TEXT NOT NULL, model TEXT, "
            "endpoint TEXT, status TEXT NOT NULL, duration_ms INTEGER, input_tokens INTEGER, "
            "output_tokens INTEGER, total_tokens INTEGER, cached_input_tokens INTEGER, "
            "reasoning_output_tokens INTEGER, provider_usage_json TEXT, error TEXT)"
        )
    store = McpTelemetryStore(path)
    assert store.fetch_llm_usage_summary()["failed_by_error_code"] == {}
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage_events)")}
    assert "error_code" in columns
