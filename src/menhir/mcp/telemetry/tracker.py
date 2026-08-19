from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any, Awaitable, Callable, TypeVar, cast

from menhir.infrastructure.telemetry.helpers import _safe_preview_of, _size_of, _utc_now_iso
from menhir.infrastructure.telemetry.store import McpTelemetryStore, telemetry_store

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


def _telemetry_error(exc: Exception) -> str:
    """Return an error label safe to persist without copying arbitrary exception prose.

    Caller-facing diagnostics still use :func:`_diagnose_failure`; the sidecar only needs
    enough information to aggregate failure classes. Arbitrary exception text can contain the
    request body and must not become another durable copy of user content.
    """
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return "SQLiteOperationalError: telemetry store busy/locked"
    if type(exc).__name__ in _GRAPH_UNREACHABLE_ERRORS or "neo4j" in text:
        return f"{type(exc).__name__}: graph store unavailable"
    return type(exc).__name__


def _lineage_from_payload(payload: Any) -> tuple[str, str | None]:
    """Resolve durable telemetry lineage from effective request context and structural args.

    Namespace pinning is server policy, so it wins over a caller-supplied namespace. When no
    tenant namespace exists, stamp the explicit default namespace instead of NULL: a current
    telemetry row must never look like pre-lineage historical residue merely because the call
    was global/default-scoped.
    """
    from menhir.domain.namespace import DEFAULT_NAMESPACE

    requested_namespace = ""
    node_uuid: str | None = None
    if isinstance(payload, dict):
        requested_namespace = str(payload.get("namespace") or "").strip()
        for key in ("node_uuid", "memory_uuid", "uuid"):
            value = str(payload.get(key) or "").strip()
            if value:
                node_uuid = value
                break

    pinned_namespace = ""
    try:
        from menhir.mcp.service_access import get_pinned_namespace

        pinned_namespace = str(get_pinned_namespace() or "").strip()
    except Exception:  # pragma: no cover - telemetry lineage is best-effort
        pinned_namespace = ""

    namespace = pinned_namespace or requested_namespace or DEFAULT_NAMESPACE
    return namespace, node_uuid


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

    The telemetry preview is privacy-minimized before persistence, and every new MCP-event row
    receives non-NULL namespace lineage. This is load-bearing for explicit erasure: historical
    rows written before lineage existed remain distinguishable from current writes.
    """

    started_at = _utc_now_iso()
    started = time.perf_counter()
    input_size = _size_of(payload) if payload is not None else None
    payload_preview = _safe_preview_of(payload) if payload is not None else None
    namespace, node_uuid = _lineage_from_payload(payload)

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
                error=f"TimeoutError: exceeded {timeout}s",
                input_size=input_size,
                result_size=None,
                payload_preview=payload_preview,
                namespace=namespace,
                node_uuid=node_uuid,
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
                error=_telemetry_error(exc),
                input_size=input_size,
                result_size=None,
                payload_preview=payload_preview,
                namespace=namespace,
                node_uuid=node_uuid,
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
            namespace=namespace,
            node_uuid=node_uuid,
        )
    except sqlite3.Error:
        logger.exception("Failed to record success telemetry")
    return result
