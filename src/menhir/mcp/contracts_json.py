"""JSON rendering and defensive precomputation helpers for MCP contracts."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _safe_precompute(label: str, name: str, fn: Callable[[], _T], fallback: _T) -> _T:
    """Evaluate a pre-runner computation (``call_payload``/``timeout_for``) defensively.

    Both run OUTSIDE ``track_mcp_call``'s try/except -- they are its own call
    arguments, evaluated before that function is even entered -- so an exception raised
    here used to skip ``_diagnose_failure``, telemetry recording, and the tool/resource's
    own ``error_mapper`` entirely and reach the bare FastMCP/MCP-SDK fallback, which under
    ``mask_error_details`` (or an exception whose ``str()`` is empty) surfaces as an
    undiagnosable generic JSON-RPC internal error. Logging the full traceback here keeps
    the diagnosis in server.log even though the caller falls back to *fallback* and the
    call proceeds through the normal, protected path.
    """
    try:
        return fn()
    except Exception:
        logger.exception("%s raised for `%s`; falling back to %r", label, name, fallback)
        return fallback


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=_json_default)
