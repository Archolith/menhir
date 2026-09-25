"""Shared low-level pieces for the throwaway test-server launcher.

Constants, port/origin validation, health polling, and process termination —
the base layer every other ``test_server_*`` helper module builds on. These
live here so helper modules never need to import the launcher facade itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PORTS = {8090}  # the real server — never bind or probe it here


def free_port() -> int:
    """Return an OS-assigned free localhost TCP port.

    Throwaway servers must never bind a fixed port: a fixed port can collide with
    an already-running instance (a dev/container server on 8099, say), and the
    health-wait would then talk to *that* process. An ephemeral port + the
    instance-id handshake together guarantee isolation.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])

SHAPES = ("no-auth", "static", "client-token", "oauth", "oauth-as")

# Fixed, non-secret test credentials. These are deliberately well-known — the
# whole point is a throwaway server with no real data behind it.
TEST_KEYS = {
    "operator": "test-operator-key",
    "agent": "test-agent-key",
    "readonly": "test-readonly-key",
}

# A JWKS URI that resolves to nothing, so the OAuth path exercises the
# IdP-outage branch (server_error -> 503) deterministically.
DEAD_JWKS_URI = "http://127.0.0.1:9/.well-known/jwks.json"


def _remove_workdir(path: Path) -> None:
    """Remove sensitive throwaway state and surface any cleanup failure."""
    shutil.rmtree(path)


def _validated_public_origin(value: str) -> str:
    """Return a canonical HTTPS origin suitable for a public OAuth test."""
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("--public-base-url must be a valid HTTPS origin") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "--public-base-url must be an HTTPS origin without credentials, path, query, or fragment"
        )
    return f"https://{parts.netloc}"


def _wait_for_health(base_url: str, timeout_s: float, proc: subprocess.Popen,
                     *, expect_instance_id: str) -> dict:
    """Poll /api/health until *our* server answers or *timeout_s* elapses.

    Verifies the health response carries our ``instance_id``. If a *different*
    process holds this port (e.g. a dev server or a stale instance), its health
    response won't match and we fail loudly instead of silently running against
    the wrong server.
    """
    deadline = time.monotonic() + timeout_s
    url = f"{base_url}/api/health"
    last_err = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (code {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 (loopback)
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = str(exc)
            time.sleep(0.4)
            continue
        got = payload.get("instance_id")
        if got == expect_instance_id:
            return payload
        # A server answered but it is not ours — a foreign process on this port.
        raise RuntimeError(
            f"port {base_url} is held by a different server "
            f"(instance_id={got!r}, expected {expect_instance_id!r}); refusing to "
            f"run against it"
        )
    raise TimeoutError(f"health check timed out after {timeout_s}s: {last_err}")


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"test server process {proc.pid} did not exit after termination and kill"
            ) from exc
    if proc.poll() is None:
        raise RuntimeError(f"test server process {proc.pid} is still running after termination")
