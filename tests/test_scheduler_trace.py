from __future__ import annotations

import httpx
import pytest

import menhir.infrastructure.scheduler_trace as scheduler_trace


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://scheduler.test"),
                response=httpx.Response(self.status_code),
            )


class _AsyncClient:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict[str, object]) -> _Response:
        return self._response


@pytest.mark.asyncio
async def test_register_scheduler_task_source_does_not_mark_registered_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_trace, "_registered", False)
    monkeypatch.setattr(scheduler_trace, "scheduler_url_from_env", lambda: "http://scheduler.test")
    monkeypatch.setattr(scheduler_trace.httpx, "AsyncClient", lambda timeout=2.0: _AsyncClient(_Response(500)))

    await scheduler_trace.register_scheduler_task_source()

    assert scheduler_trace._registered is False


@pytest.mark.asyncio
async def test_emit_scheduler_task_event_swallows_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_trace, "scheduler_url_from_env", lambda: "http://scheduler.test")
    monkeypatch.setattr(scheduler_trace.httpx, "AsyncClient", lambda timeout=2.0: _AsyncClient(_Response(500)))

    await scheduler_trace.emit_scheduler_task_event(
        parent_job_id="episode-1",
        parent_label="Episode 1",
        parent_state="graphiti_extracting",
    )


class _ExplodingClient:
    """Any use of this client is a failure: the disabled path must not reach HTTP."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("scheduler tracing is disabled; no HTTP client may be built")


def _forbid_network_and_url(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Make every side effect of the enabled path detectable."""
    touched = {"url": False, "lifecycle": False}

    def _url() -> str:
        touched["url"] = True
        return "http://scheduler.test"

    def _record(*args, **kwargs) -> None:
        touched["lifecycle"] = True

    monkeypatch.setattr(scheduler_trace, "scheduler_url_from_env", _url)
    monkeypatch.setattr(scheduler_trace, "record_lifecycle_event", _record)
    monkeypatch.setattr(scheduler_trace.httpx, "AsyncClient", _ExplodingClient)
    return touched


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " on "])
def test_recognized_disabled_values_turn_tracing_off(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SCHEDULER_TRACE_DISABLED", raw)
    assert scheduler_trace.scheduler_trace_enabled() is False


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_other_values_leave_tracing_enabled(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SCHEDULER_TRACE_DISABLED", raw)
    assert scheduler_trace.scheduler_trace_enabled() is True


def test_tracing_is_enabled_when_the_variable_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCHEDULER_TRACE_DISABLED", raising=False)
    assert scheduler_trace.scheduler_trace_enabled() is True


@pytest.mark.asyncio
async def test_disabled_emit_skips_url_lifecycle_and_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEDULER_TRACE_DISABLED", "1")
    touched = _forbid_network_and_url(monkeypatch)

    await scheduler_trace.emit_scheduler_task_event(
        parent_job_id="episode-1",
        parent_label="Episode 1",
        parent_state="graphiti_extracting",
    )

    assert touched == {"url": False, "lifecycle": False}


@pytest.mark.asyncio
async def test_disabled_register_skips_url_lifecycle_http_and_stays_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEDULER_TRACE_DISABLED", "1")
    monkeypatch.setattr(scheduler_trace, "_registered", False)
    touched = _forbid_network_and_url(monkeypatch)

    await scheduler_trace.register_scheduler_task_source()

    assert touched == {"url": False, "lifecycle": False}
    # Disabling must not fake a successful registration: if tracing is turned back
    # on in the same process, registration still has to happen.
    assert scheduler_trace._registered is False


@pytest.mark.asyncio
async def test_enabled_emit_still_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disabled guard must not short-circuit the default path."""
    monkeypatch.delenv("SCHEDULER_TRACE_DISABLED", raising=False)
    posted: list[str] = []

    class _RecordingClient(_AsyncClient):
        async def post(self, url: str, json: dict[str, object]) -> _Response:
            posted.append(url)
            return await super().post(url, json)

    monkeypatch.setattr(scheduler_trace, "scheduler_url_from_env", lambda: "http://scheduler.test")
    monkeypatch.setattr(
        scheduler_trace.httpx, "AsyncClient", lambda timeout=2.0: _RecordingClient(_Response(200))
    )

    await scheduler_trace.emit_scheduler_task_event(
        parent_job_id="episode-1",
        parent_label="Episode 1",
        parent_state="graphiti_extracting",
    )

    assert posted == ["http://scheduler.test/task-events"]
