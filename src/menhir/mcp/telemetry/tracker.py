from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any, Awaitable, Callable, TypeVar, cast

from menhir.infrastructure.telemetry.store import McpTelemetryStore, _preview_of, _size_of, _utc_now_iso, telemetry_store

T = TypeVar("T")
logger = logging.getLogger(__name__)

DEFAULT_MCP_TIMEOUT = int(os.getenv("MENHIR_MCP_TIMEOUT", "120"))

#: exception type names that mean the graph store (Neo4j) is unreachable/degraded.
_GRAPH_UNREACHABLE_ERRORS = frozenset({"ServiceUnavailable", "SessionExpired", "SessionError"})


def _diagnose_failure(operation: str, exc: Exception) -> str:
    """Turn an INFRASTRUCTURE exception into an actionable, caller-facing message, so a degraded
    server explains WHAT is wrong instead of surfacing a bare exception. Non-infra errors keep the
    plain ``Type: message`` form (callers and existing tests rely on it), so this only enriches the
    two failure modes a caller cannot otherwise diagnose from the client side."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in low or "busy" in low):
        return (
            f"{operation}: memory telemetry store is busy/locked (SQLite). The server is DEGRADED "
            "-- a duplicate 'menhir serve' process or an un-checkpointed database is likely "
            "contending for .agent/mcp_telemetry.db. The graph store itself may still be reachable; "
            f"retry shortly or check server health. (original: {type(exc).__name__}: {text})"
        )
    if type(exc).__name__ in _GRAPH_UNREACHABLE_ERRORS or "neo4j" in low:
        return (
            f"{operation}: graph store (Neo4j) is unreachable or timed out. The server is DEGRADED; "
            f"check the remote graph endpoint. (original: {type(exc).__name__}: {text})"
        )
    return f"{type(exc).__name__}: {text}"


async def track_mcp_call(
    *,
    kind: str,
    operation: str,
    payload: Any,
    runner: Callable[[], Awaitable[T]],
    store: McpTelemetryStore = telemetry_store,
    timeout: int = DEFAULT_MCP_TIMEOUT,
    error_mapper: Callable[[str], T] | None = None,
) -> T:
    """Measure and persist one MCP call around an async runner.

    Returns an error string instead of raising so Claude always gets a response.
    Applies an async timeout (default 120s) to prevent indefinite hangs.
    """

    started_at = _utc_now_iso()
    started = time.perf_counter()
    input_size = _size_of(payload) if payload is not None else None
    payload_preview = _preview_of(payload) if payload is not None else None

    try:
        result = await asyncio.wait_for(runner(), timeout=timeout)
    except asyncio.TimeoutError:
        completed_at = _utc_now_iso()
        duration_ms = int((time.perf_counter() - started) * 1000)
        error_msg = (
            f"TIMEOUT: {operation} exceeded {timeout}s limit. The server may be DEGRADED -- a "
            "synchronous store call (a locked telemetry DB or an unreachable graph) can block the "
            "event loop so the call cannot return sooner. Check server health."
        )
        logger.error("%s (duration=%dms)", error_msg, duration_ms)
        try:
            store.record(
                kind=kind,
                operation=operation,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
                input_size=input_size,
                result_size=None,
                payload_preview=payload_preview,
            )
        except sqlite3.Error:
            logger.exception("Failed to record timeout telemetry")
        if error_mapper is not None:
            return error_mapper(error_msg)
        return cast(T, f"Error: {error_msg}")
    except Exception as exc:
        completed_at = _utc_now_iso()
        duration_ms = int((time.perf_counter() - started) * 1000)
        error_msg = _diagnose_failure(operation, exc)
        logger.error("MCP %s/%s failed after %dms: %s", kind, operation, duration_ms, error_msg)
        try:
            store.record(
                kind=kind,
                operation=operation,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
                input_size=input_size,
                result_size=None,
                payload_preview=payload_preview,
            )
        except sqlite3.Error:
            logger.exception("Failed to record error telemetry")
        if error_mapper is not None:
            return error_mapper(error_msg)
        return cast(T, f"Error: {error_msg}")

    completed_at = _utc_now_iso()
    duration_ms = int((time.perf_counter() - started) * 1000)
    result_size = _size_of(result) if result is not None else None
    logger.info("MCP %s/%s completed in %dms", kind, operation, duration_ms)
    try:
        store.record(
            kind=kind,
            operation=operation,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            success=True,
            error=None,
            input_size=input_size,
            result_size=result_size,
            payload_preview=payload_preview,
        )
    except sqlite3.Error:
        logger.exception("Failed to record success telemetry")
    return result
