"""Scheduler-backed llama.cpp endpoint resolution helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import httpx

from menhir.infrastructure.paths import scheduler_project_dir
from menhir.infrastructure.telemetry import record_lifecycle_event

logger = logging.getLogger(__name__)
SCHEDULER_URL_DEFAULT = "http://localhost:8082"
_DEFAULT_LOCAL_BASES = {
    "http://127.0.0.1:8081/v1",
    "http://localhost:8081/v1",
}
_SCHEDULER_STARTUP_MARKER_TTL_S = 20.0
_SCHEDULER_AUTOSTART_RETRY_COOLDOWN_S = 30.0
_scheduler_autostart_state_lock = Lock()
_last_scheduler_autostart_attempt_monotonic: float | None = None
_last_scheduler_autostart_success: bool | None = None


def _normalize_scheduler_url(url: str) -> str:
    normalized = (url or "").strip() or SCHEDULER_URL_DEFAULT
    parts = urlsplit(normalized)
    hostname = parts.hostname or ""
    if hostname.lower() != "localhost":
        return normalized.rstrip("/")

    netloc = "127.0.0.1"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit(
        (
            parts.scheme or "http",
            netloc,
            parts.path,
            parts.query,
            parts.fragment,
        )
    ).rstrip("/")


def scheduler_url_from_env() -> str:
    """Return configured scheduler URL with sane default."""
    return _normalize_scheduler_url(os.getenv("SCHEDULER_URL", SCHEDULER_URL_DEFAULT))


def should_use_scheduler(base_url: str) -> bool:
    """Return whether scheduler should manage endpoint resolution for this base URL."""
    normalized = (base_url or "").strip()
    return not normalized or normalized in _DEFAULT_LOCAL_BASES


def _scheduler_autostart_enabled() -> bool:
    raw = os.getenv("SCHEDULER_AUTO_START", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _scheduler_open_dashboard_enabled() -> bool:
    raw = os.getenv("SCHEDULER_OPEN_DASHBOARD", "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _default_scheduler_project_dir() -> Path:
    """Return default scheduler project directory using paths helper."""
    return scheduler_project_dir()


def _resolve_scheduler_project_dir() -> Path:
    configured = os.getenv("SCHEDULER_PROJECT_DIR", "").strip()
    if configured:
        return Path(configured)
    return _default_scheduler_project_dir()


def _resolve_scheduler_manager_path() -> Path:
    configured = os.getenv("SCHEDULER_MANAGER_PATH", "").strip()
    if configured:
        return Path(configured)
    return _resolve_scheduler_project_dir() / "manager.py"


def _resolve_scheduler_python() -> str:
    configured = os.getenv("SCHEDULER_PYTHON", "").strip()
    if configured:
        return configured
    project_dir = _resolve_scheduler_project_dir()
    if os.name == "nt":
        candidate = project_dir / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = project_dir / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable or "python"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _running_under_wsl() -> bool:
    if os.name != "posix":
        return False
    if os.getenv("WSL_INTEROP") or os.getenv("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in platform.release().lower()
    except OSError:
        return False


def _translate_windows_path_for_wsl(value: str) -> str:
    normalized = (value or "").strip()
    drive = normalized[:1]
    if (
        len(normalized) < 3
        or not drive.isalpha()
        or normalized[1] != ":"
        or normalized[2] not in {"\\", "/"}
    ):
        return normalized
    remainder = normalized[3:].replace("\\", "/")
    return f"/mnt/{drive.lower()}/{remainder}"


def _normalize_runtime_path(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if _running_under_wsl():
        return _translate_windows_path_for_wsl(normalized)
    return normalized


def _resolve_model_path_from_env(env: dict[str, str]) -> str:
    explicit = _normalize_runtime_path(
        (env.get("LLAMA_MODEL_PATH") or env.get("SCHEDULER_LLAMA_MODEL_PATH") or "")
    )
    if explicit and Path(explicit).exists():
        return explicit

    chat_model = _normalize_runtime_path(env.get("LLAMA_CHAT_MODEL") or "")
    if not chat_model:
        return ""
    model_candidate = Path(chat_model)
    return str(model_candidate) if model_candidate.is_file() else ""


def _build_scheduler_child_env(project_dir: Path) -> dict[str, str]:
    child_env = dict(os.environ)
    dotenv_path = project_dir / ".env"
    file_values = _parse_env_file(dotenv_path) if dotenv_path.exists() else {}
    child_env.update(file_values)

    server_bin = (
        child_env.get("LLAMA_SERVER_BIN")
        or child_env.get("SCHEDULER_LLAMA_SERVER_BIN")
        or ""
    ).strip()
    server_bin = _normalize_runtime_path(server_bin)
    if not server_bin:
        default_bin = Path.home() / "llama-cpp-cuda" / "llama-server.exe"
        if default_bin.exists():
            server_bin = str(default_bin)
    if server_bin:
        child_env["LLAMA_SERVER_BIN"] = server_bin

    model_path = _resolve_model_path_from_env(child_env)
    if model_path:
        child_env["LLAMA_MODEL_PATH"] = model_path

    return child_env


def _scheduler_status_ok(*, timeout_s: float = 1.0) -> bool:
    scheduler_url = scheduler_url_from_env()
    try:
        req = Request(f"{scheduler_url}/watchdog-status", method="GET")
        with urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return isinstance(payload, dict) and bool(payload.get("state"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def _scheduler_startup_marker_path() -> Path:
    return Path(tempfile.gettempdir()) / "menhir-scheduler-startup.lock"


def _claim_scheduler_startup_marker() -> bool:
    marker = _scheduler_startup_marker_path()
    while True:
        now = time.time()
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age_s = now - marker.stat().st_mtime
            except OSError:
                return False
            if age_s <= _SCHEDULER_STARTUP_MARKER_TTL_S:
                return False
            try:
                marker.unlink()
            except OSError:
                return False
            continue
        try:
            payload = json.dumps({"pid": os.getpid(), "started_at": now}).encode(
                "utf-8"
            )
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True


def _release_scheduler_startup_marker() -> None:
    marker = _scheduler_startup_marker_path()
    try:
        marker.unlink()
    except OSError:
        pass


def _wait_for_scheduler_ready_status(*, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.5, timeout_s)
    while time.monotonic() < deadline:
        if _scheduler_status_ok(timeout_s=1.0):
            return True
        time.sleep(0.3)
    return _scheduler_status_ok(timeout_s=1.0)


def _should_skip_autostart_retry() -> bool:
    with _scheduler_autostart_state_lock:
        if _last_scheduler_autostart_attempt_monotonic is None:
            return False
        if _last_scheduler_autostart_success is not False:
            return False
        elapsed = time.monotonic() - _last_scheduler_autostart_attempt_monotonic
        return elapsed < _SCHEDULER_AUTOSTART_RETRY_COOLDOWN_S


def _record_autostart_attempt(success: bool) -> None:
    global \
        _last_scheduler_autostart_attempt_monotonic, \
        _last_scheduler_autostart_success
    with _scheduler_autostart_state_lock:
        _last_scheduler_autostart_attempt_monotonic = time.monotonic()
        _last_scheduler_autostart_success = success


def _start_scheduler_process(*, startup_wait_s: float = 8.0) -> bool:
    if not _scheduler_autostart_enabled():
        logger.info(
            "Scheduler autostart disabled; set SCHEDULER_AUTO_START=1 to enable"
        )
        return False
    if _should_skip_autostart_retry():
        logger.warning(
            "Skipping scheduler autostart retry because a recent launch attempt already failed; cooldown_s=%.1f",
            _SCHEDULER_AUTOSTART_RETRY_COOLDOWN_S,
        )
        return False
    manager_path = _resolve_scheduler_manager_path()
    if not manager_path.exists():
        logger.warning("Scheduler manager path does not exist: %s", manager_path)
        _record_autostart_attempt(False)
        return False
    project_dir = _resolve_scheduler_project_dir()
    child_env = _build_scheduler_child_env(project_dir)
    if not child_env.get("LLAMA_SERVER_BIN") or not child_env.get("LLAMA_MODEL_PATH"):
        logger.warning(
            "Scheduler autostart skipped; missing LLAMA_SERVER_BIN or LLAMA_MODEL_PATH project_dir=%s",
            project_dir,
        )
        _record_autostart_attempt(False)
        return False
    python_bin = _resolve_scheduler_python()
    marker_claimed = _claim_scheduler_startup_marker()
    if not marker_claimed:
        logger.info("Scheduler startup already in progress; waiting for readiness")
        started = _wait_for_scheduler_ready_status(timeout_s=startup_wait_s)
        _record_autostart_attempt(started)
        return started
    try:
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [python_bin, str(manager_path)],
                cwd=str(project_dir),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
        else:
            subprocess.Popen(
                [python_bin, str(manager_path)],
                cwd=str(project_dir),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError:
        logger.exception(
            "Scheduler autostart launch failed project_dir=%s manager_path=%s python=%s",
            project_dir,
            manager_path,
            python_bin,
        )
        _release_scheduler_startup_marker()
        _record_autostart_attempt(False)
        return False
    started = _wait_for_scheduler_ready_status(timeout_s=startup_wait_s)
    _release_scheduler_startup_marker()
    if not started:
        logger.error(
            "Scheduler autostart launched but did not become ready within %.1fs project_dir=%s",
            startup_wait_s,
            project_dir,
        )
        _record_autostart_attempt(False)
        return False
    _record_autostart_attempt(True)
    if _scheduler_open_dashboard_enabled():
        scheduler_url = scheduler_url_from_env()
        try:
            import webbrowser

            webbrowser.open(f"{scheduler_url}/")
        except OSError:
            logger.exception("Failed to open scheduler dashboard browser")
    return True


def _ensure_scheduler_running() -> None:
    if _scheduler_status_ok(timeout_s=1.0):
        return
    _start_scheduler_process()


def ensure_scheduler_running() -> None:
    """Public helper for explicitly starting the scheduler process when needed."""

    _ensure_scheduler_running()


async def acquire_llama_url_async(
    *, fallback: str, task: str | None = None, timeout_s: float = 30.0
) -> str:
    """Acquire llama base URL from scheduler asynchronously."""
    scheduler_url = scheduler_url_from_env()
    details = {
        "task": task,
        "fallback": fallback,
        "scheduler_url": scheduler_url,
        "timeout_s": timeout_s,
    }
    record_lifecycle_event(
        component="llama_endpoint",
        event="acquire_async",
        state="started",
        details=details,
    )
    try:
        record_lifecycle_event(
            component="llama_endpoint",
            event="ensure_scheduler_running",
            state="started",
            details=details,
        )
        await asyncio.to_thread(ensure_scheduler_running)
        record_lifecycle_event(
            component="llama_endpoint",
            event="ensure_scheduler_running",
            state="completed",
            details=details,
        )
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            record_lifecycle_event(
                component="llama_endpoint",
                event="scheduler_acquire_request",
                state="started",
                details=details,
            )
            response = await client.post(
                f"{scheduler_url}/acquire",
                json={"task": task} if task else {},
            )
            response.raise_for_status()
            acquired_url = str(response.json()["url"])
            record_lifecycle_event(
                component="llama_endpoint",
                event="scheduler_acquire_request",
                state="completed",
                details={**details, "acquired_url": acquired_url},
            )
            record_lifecycle_event(
                component="llama_endpoint",
                event="acquire_async",
                state="completed",
                details={**details, "acquired_url": acquired_url},
            )
            return acquired_url
    except (httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError) as exc:
        record_lifecycle_event(
            component="llama_endpoint",
            event="acquire_async",
            state="failed",
            details={**details, "error": f"{type(exc).__name__}: {exc}"},
        )
        return fallback


def acquire_llama_url_sync(
    *, fallback: str, task: str | None = None, timeout_s: float = 2.0
) -> str:
    """Acquire llama base URL from scheduler synchronously."""
    scheduler_url = scheduler_url_from_env()
    ensure_scheduler_running()
    payload_bytes = json.dumps({"task": task} if task else {}).encode("utf-8")
    request = Request(f"{scheduler_url}/acquire", data=payload_bytes, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
            url = response_payload.get("url")
            if isinstance(url, str) and url:
                return url
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return fallback
    return fallback
