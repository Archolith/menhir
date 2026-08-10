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
