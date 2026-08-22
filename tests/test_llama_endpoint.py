"""Unit tests for scheduler-backed llama endpoint helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

import menhir.infrastructure.llama_endpoint as llama_endpoint


def _make_local_test_dir(name: str) -> Path:
    root = Path(__file__).resolve().parent / ".tmp_llama_endpoint"
    target = root / name
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.mark.unit
def test_should_use_scheduler_for_local_default() -> None:
    assert llama_endpoint.should_use_scheduler("http://127.0.0.1:8081/v1") is True
    assert llama_endpoint.should_use_scheduler("http://localhost:8081/v1") is True
    assert llama_endpoint.should_use_scheduler("") is True


@pytest.mark.unit
def test_should_not_use_scheduler_for_remote_base() -> None:
    assert llama_endpoint.should_use_scheduler("https://api.example.com/v1") is False


@pytest.mark.unit
def test_scheduler_autostart_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHEDULER_AUTO_START", raising=False)

    assert llama_endpoint._scheduler_autostart_enabled() is False


@pytest.mark.unit
def test_scheduler_open_dashboard_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHEDULER_OPEN_DASHBOARD", raising=False)

    assert llama_endpoint._scheduler_open_dashboard_enabled() is False


@pytest.mark.unit
def test_scheduler_url_from_env_normalizes_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULER_URL", "http://localhost:8082")

    assert llama_endpoint.scheduler_url_from_env() == "http://127.0.0.1:8082"


@pytest.mark.unit
def test_acquire_llama_url_sync_ensures_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"ensure": 0}

    def _fake_ensure() -> None:
        called["ensure"] += 1

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"url":"http://127.0.0.1:8081/v1"}'

    monkeypatch.setattr(llama_endpoint, "ensure_scheduler_running", _fake_ensure)
    monkeypatch.setattr(llama_endpoint, "urlopen", lambda req, timeout=0: _Resp())

    url = llama_endpoint.acquire_llama_url_sync(
        fallback="http://127.0.0.1:8081/v1",
        timeout_s=1.0,
    )

    assert called["ensure"] == 1
    assert url == "http://127.0.0.1:8081/v1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_acquire_llama_url_async_ensures_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"ensure": 0}
    lifecycle: list[tuple[str, str, str, dict[str, object] | None]] = []

    def _fake_ensure() -> None:
        called["ensure"] += 1

    async def _fake_to_thread(fn):
        fn()

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"url": "http://127.0.0.1:8081/v1"}

    # CF-174: the client is now shared per event loop instead of built and closed per acquire,
    # and the caller's budget is passed per request rather than baked into the constructor. The
    # assertions below are unchanged; the stub tracks the two contract changes.
    class _Client:
        is_closed = False

        async def post(self, _url: str, json: dict[str, str], timeout: float | None = None):
            assert json == {"task": "memory: graphiti add_episode"}
            assert timeout == 1.0, "the caller's timeout must reach the request"
            return _Response()

    monkeypatch.setattr(llama_endpoint, "ensure_scheduler_running", _fake_ensure)
    monkeypatch.setattr(llama_endpoint.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(llama_endpoint.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(
        llama_endpoint,
        "record_lifecycle_event",
        lambda *, component, event, state, details=None, episode_uuid=None: lifecycle.append(
            (component, event, state, details)
        ),
    )

    url = await llama_endpoint.acquire_llama_url_async(
        fallback="http://127.0.0.1:8081/v1",
        task="memory: graphiti add_episode",
        timeout_s=1.0,
    )

    assert called["ensure"] == 1
    assert url == "http://127.0.0.1:8081/v1"
    assert lifecycle == [
        (
            "llama_endpoint",
            "acquire_async",
            "started",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
            },
        ),
        (
            "llama_endpoint",
            "ensure_scheduler_running",
            "started",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
            },
        ),
        (
            "llama_endpoint",
            "ensure_scheduler_running",
            "completed",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
            },
        ),
        (
            "llama_endpoint",
            "scheduler_acquire_request",
            "started",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
            },
        ),
        (
            "llama_endpoint",
            "scheduler_acquire_request",
            "completed",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
                "acquired_url": "http://127.0.0.1:8081/v1",
            },
        ),
        (
            "llama_endpoint",
            "acquire_async",
            "completed",
            {
                "task": "memory: graphiti add_episode",
                "fallback": "http://127.0.0.1:8081/v1",
                "scheduler_url": "http://127.0.0.1:8082",
                "timeout_s": 1.0,
                "acquired_url": "http://127.0.0.1:8081/v1",
            },
        ),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_acquire_llama_url_async_records_failure_and_returns_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[tuple[str, str, str, dict[str, object] | None]] = []

    def _fake_ensure() -> None:
        return None

    async def _fake_to_thread(fn):
        fn()

    # CF-174: the client is now shared per event loop instead of built and closed per acquire,
    # and the caller's budget is passed per request rather than baked into the constructor. The
    # assertions below are unchanged; the stub tracks the two contract changes.
    class _Client:
        is_closed = False

        async def post(self, _url: str, json: dict[str, str], timeout: float | None = None):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(llama_endpoint, "ensure_scheduler_running", _fake_ensure)
    monkeypatch.setattr(llama_endpoint.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(llama_endpoint.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(
        llama_endpoint,
        "record_lifecycle_event",
        lambda *, component, event, state, details=None, episode_uuid=None: lifecycle.append(
            (component, event, state, details)
        ),
    )

    fallback = "http://127.0.0.1:8081/v1"
    url = await llama_endpoint.acquire_llama_url_async(
        fallback=fallback,
        task="memory: graphiti add_episode",
        timeout_s=1.0,
    )

    assert url == fallback
    assert lifecycle[-1][0:3] == ("llama_endpoint", "acquire_async", "failed")
    assert lifecycle[-1][3]["error"] == "ReadTimeout: timed out"


@pytest.mark.unit
def test_public_ensure_scheduler_running_calls_internal_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"ensure": 0}

    def _fake_ensure() -> None:
        called["ensure"] += 1

    monkeypatch.setattr(llama_endpoint, "_ensure_scheduler_running", _fake_ensure)

    llama_endpoint.ensure_scheduler_running()

    assert called["ensure"] == 1


@pytest.mark.unit
def test_scheduler_status_ok_uses_watchdog_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"state":"running"}'

    def _fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(llama_endpoint, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SCHEDULER_URL", "http://localhost:8082")

    assert llama_endpoint._scheduler_status_ok(timeout_s=1.5) is True
    assert seen == {"url": "http://127.0.0.1:8082/watchdog-status", "timeout": 1.5}


@pytest.mark.unit
def test_resolve_model_path_from_env_prefers_explicit_path() -> None:
    tmp_path = _make_local_test_dir("explicit-model")
    try:
        model = tmp_path / "model.gguf"
        model.write_text("x", encoding="utf-8")
        env = {"LLAMA_MODEL_PATH": str(model)}
        assert llama_endpoint._resolve_model_path_from_env(env) == str(model)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.unit
def test_resolve_model_path_from_env_translates_windows_path_under_wsl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = _make_local_test_dir("explicit-model-wsl")
    try:
        model = tmp_path / "model.gguf"
        model.write_text("x", encoding="utf-8")
        monkeypatch.setattr(llama_endpoint, "_running_under_wsl", lambda: True)
        monkeypatch.setattr(
            llama_endpoint,
            "_translate_windows_path_for_wsl",
            lambda _value: str(model),
        )
        env = {"LLAMA_MODEL_PATH": r"C:\Users\dev\models\model.gguf"}

        assert llama_endpoint._resolve_model_path_from_env(env) == str(model)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)




@pytest.mark.unit
def test_start_scheduler_process_windows_uses_hidden_direct_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = _make_local_test_dir("windows-hidden-popen")
    try:
        project_dir = tmp_path / "yawn.scheduler"
        project_dir.mkdir(parents=True, exist_ok=True)
        manager_path = project_dir / "manager.py"
        manager_path.write_text("print('scheduler')", encoding="utf-8")

        calls: dict[str, object] = {}

        monkeypatch.setattr(llama_endpoint, "_scheduler_autostart_enabled", lambda: True)
        monkeypatch.setattr(llama_endpoint, "_resolve_scheduler_project_dir", lambda: project_dir)
        monkeypatch.setattr(llama_endpoint, "_resolve_scheduler_manager_path", lambda: manager_path)
        monkeypatch.setattr(llama_endpoint, "_resolve_scheduler_python", lambda: r"C:\scheduler\python.exe")
        monkeypatch.setattr(
            llama_endpoint,
            "_build_scheduler_child_env",
            lambda _project_dir: {"LLAMA_SERVER_BIN": "bin", "LLAMA_MODEL_PATH": "model"},
        )
        monkeypatch.setattr(llama_endpoint, "_scheduler_status_ok", lambda timeout_s=0: True)
        monkeypatch.setattr(llama_endpoint, "_claim_scheduler_startup_marker", lambda: True)
        monkeypatch.setattr(llama_endpoint, "_release_scheduler_startup_marker", lambda: None)
        monkeypatch.setattr(llama_endpoint.os, "name", "nt")
        monkeypatch.setattr(llama_endpoint.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr(llama_endpoint, "_scheduler_open_dashboard_enabled", lambda: False)

        def _fake_popen(args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(llama_endpoint.subprocess, "Popen", _fake_popen)

        started = llama_endpoint._start_scheduler_process(startup_wait_s=0.5)

        assert started is True
        assert calls["args"] == [r"C:\scheduler\python.exe", str(manager_path)]
        assert calls["kwargs"]["cwd"] == str(project_dir)
        assert calls["kwargs"]["env"] == {"LLAMA_SERVER_BIN": "bin", "LLAMA_MODEL_PATH": "model"}
        assert calls["kwargs"]["stdout"] == llama_endpoint.subprocess.DEVNULL
        assert calls["kwargs"]["stderr"] == llama_endpoint.subprocess.DEVNULL
        assert calls["kwargs"]["stdin"] == llama_endpoint.subprocess.DEVNULL
        assert calls["kwargs"]["close_fds"] is True
        assert calls["kwargs"]["creationflags"] == 0x08000000
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
