"""MCP lifecycle proxy — backend-first mode (requires menhir serve)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING

from menhir.core import runtime as _runtime
from menhir.mcp.service_access import backend_client_mode_enabled, probe_backend_health

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

RuntimeState = _runtime.RuntimeState
RuntimeContext = _runtime.RuntimeContext

_state = _runtime._state


def _shutdown_runtime_sync() -> None:
    _runtime._shutdown_runtime_sync()


def _remember_flagged_bootstrap_read(
    reader_id: str, flagged_version: str, workspace: str | None = None
) -> None:
    _runtime._remember_flagged_bootstrap_read(
        reader_id, flagged_version, workspace=workspace
    )


def _has_recent_flagged_bootstrap_read(
    reader_id: str, flagged_version: str, workspace: str | None = None
) -> bool:
    return _runtime._has_recent_flagged_bootstrap_read(
        reader_id, flagged_version, workspace=workspace
    )


def _diag(msg: str) -> None:
    """Write a diagnostic line to stderr so silent hangs are visible."""
    import sys
    print(f"[menhir] {msg}", file=sys.stderr, flush=True)


@asynccontextmanager
async def _mcp_lifespan(app: FastMCP[object]) -> AsyncIterator[dict[str, object]]:
    if not backend_client_mode_enabled():
        _diag("FATAL: menhir_BACKEND_URL is not set.")
        _diag("Start `menhir serve` and set menhir_BACKEND_URL in .env")
        raise RuntimeError(
            "MCP stdio now requires menhir_BACKEND_URL. "
            "Start `menhir serve` and point stdio MCP at that backend."
        )

    from menhir.mcp.service_access import _normalized_backend_url
    url = _normalized_backend_url()
    _diag(f"Probing backend at {url} ...")

    try:
        health = await probe_backend_health()
    except Exception as exc:
        _diag(f"FATAL: Backend unreachable at {url}/api/ready — {exc}")
        _diag("Is `menhir serve` running? Is the URL correct in .env?")
        raise RuntimeError(
            f"Backend health probe failed ({url}/api/ready): {exc}"
        ) from exc

    _diag(
        f"Backend OK — status={health.get('status')} "
        f"startup_mode={health.get('startup_mode')}"
    )
    logger.debug(
        "MCP stdio using backend-first mode url=%s status=%s startup_mode=%s",
        health.get("url"),
        health.get("status"),
        health.get("startup_mode"),
    )

    yield {"mode": "backend_client", "backend": health}
