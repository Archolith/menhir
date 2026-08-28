from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any, Awaitable, Callable, TypeVar, cast

from menhir.infrastructure.telemetry.helpers import _safe_preview_of, _size_of, _utc_now_iso
from menhir.infrastructure.telemetry.store import McpTelemetryStore, telemetry_store
from menhir.core.request_context import get_request_session, get_request_tier
from menhir.mcp.service_access import McpOAuthInvocationDenied

#: Execution stage, so a failed row says WHERE it failed (CF-29).
#:
#: HONEST LIMIT, and the reason there is no "committed"/"rolled back" pair here: this wrapper sees
#: only the boundary of `runner()`. It cannot know whether a mutation inside the runner reached the
#: graph. `TIMEOUT` is exactly the case where that is unknown -- the runner was cancelled mid-flight
#: -- which narrows CF-28 to one stage rather than resolving it. Claiming more would be a stage
#: label that lies.
STAGE_DENIED = "denied"       # gates refused before the runner published its arguments
STAGE_FAILED = "failed"       # runner raised; the call did not return
STAGE_TIMEOUT = "timeout"     # runner cancelled at the deadline; commit state UNKNOWN
STAGE_COMPLETED = "completed"  # runner returned


def _caller_identity() -> tuple[str | None, str | None, str | None, str | None]:
    """(client_name, client_id, session_id, tier) for the current request, or Nones.

    Read from the request context, which is the same source the namespace pin keys on -- so the
    recorded client is the SERVER-resolved identity, never a value the caller supplied in its
    arguments. A background or internal call has no session and yields Nones, which is the truthful
    answer for work no client invoked.
    """
    session = get_request_session()
    tier = get_request_tier() or None
    if session is None:
        return None, None, None, tier
    return (
        (getattr(session, "client_name", "") or "").strip() or None,
        (getattr(session, "client_id", "") or "").strip() or None,
        (getattr(session, "session_id", "") or "").strip() or None,
        tier,
    )

T = TypeVar("T")
logger = logging.getLogger(__name__)

DEFAULT_MCP_TIMEOUT = int(os.getenv("MENHIR_MCP_TIMEOUT", "120"))

#: Stand-in preview for a call refused before the authorization gates published its arguments
#: (CF-118). A marker rather than NULL: NULL would be indistinguishable from a caller that sent
#: no arguments at all, and the whole point is that this row records an ATTEMPT, not an action.
PREVIEW_UNAUTHORIZED = '{"unauthorized": true}'

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
    timeout: int = DEFAULT_MCP_TIMEOUT,  # noqa: ASYNC109 -- public tool contract
    error_mapper: Callable[[str], T] | None = None,
    effective_payload: Callable[[], Any | None] | None = None,
) -> T:
    """Measure and persist one MCP call around an async runner.

    Returns an error string instead of raising so Claude always gets a response.
    Applies an async timeout (default 120s) to prevent indefinite hangs.

    ``payload`` is used only for sizing and a privacy-minimized preview. Tool callers may provide
    ``effective_payload`` to publish the structural arguments only after authorization, allowlist
    checks, and namespace pinning have completed. If a protected runner is denied before that
    point, telemetry falls back to the server-side pinned/default namespace and does not treat raw
    caller UUID/namespace arguments as ownership.

    THE PREVIEW OBEYS THE SAME RULE AS THE LINEAGE (CF-118). It used to be rendered from the RAW
    caller kwargs, eagerly, before the runner ran -- so a call refused at the tier or allowlist gate
    still persisted the identifiers that caller chose. The values are redacted and caller-known, so
    this is not a disclosure; what it is, is attacker-controlled content in the operator's durable
    audit trail, one row per attempt, indistinguishable from a namespace the server actually acted
    on. A refused call now persists ``PREVIEW_UNAUTHORIZED`` instead.

    An authorized call previews the EFFECTIVE payload, which is also strictly more accurate: the
    raw preview showed a pinned client's *requested* namespace rather than the one the server
    enforced.
    """

    started_at = _utc_now_iso()
    started = time.perf_counter()
    # Sizing stays on the raw payload: it measures what the caller actually sent, discloses
    # nothing, and is the only signal that survives a refusal.
    input_size = _size_of(payload) if payload is not None else None

    def _published_payload() -> tuple[Any, bool]:
        """(payload to describe, whether the gates published it).

        ``effective_payload is None`` means a caller with no gates to pass -- background/internal
        work -- so its raw payload IS its effective one.
        """
        if effective_payload is None:
            return payload, True
        try:
            published = effective_payload()
        except Exception:  # pragma: no cover - telemetry lineage is best-effort
            logger.warning("Failed to resolve effective MCP telemetry lineage", exc_info=True)
            return None, False
        return (published, True) if published is not None else (None, False)

    def _resolved_lineage() -> tuple[str, str | None]:
        lineage_payload, _authorized = _published_payload()
        return _lineage_from_payload(lineage_payload)

    def _resolved_preview() -> str | None:
        described, authorized = _published_payload()
        if not authorized:
            return PREVIEW_UNAUTHORIZED
        return _safe_preview_of(described) if described is not None else None

    def _resolved_stage(outcome: str) -> str:
        """DENIED wins over the outcome. A call refused at the tier/allowlist gate raises inside
        the runner like any other failure, so the exception path alone cannot tell "your request
        was rejected" from "the work broke" -- which is the distinction an operator triaging a
        failure needs first. Non-published arguments is the same signal `PREVIEW_UNAUTHORIZED`
        already keys on (CF-118), reused rather than re-derived."""
        _described, authorized = _published_payload()
        return outcome if authorized else STAGE_DENIED

    # Resolved BEFORE the runner: the request context belongs to the caller at this point, and a
    # runner that swaps or clears it must not change who the row says invoked the call.
    client_name, client_id, session_id, tier = _caller_identity()

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
        namespace, node_uuid = _resolved_lineage()
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
                payload_preview=_resolved_preview(),
                namespace=namespace,
                node_uuid=node_uuid,
                client_name=client_name,
                client_id=client_id,
                session_id=session_id,
                tier=tier,
                stage=_resolved_stage(STAGE_TIMEOUT),
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
        namespace, node_uuid = _resolved_lineage()
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
                payload_preview=_resolved_preview(),
                namespace=namespace,
                node_uuid=node_uuid,
                client_name=client_name,
                client_id=client_id,
                session_id=session_id,
                tier=tier,
                stage=_resolved_stage(STAGE_FAILED),
            )
        except sqlite3.Error:
            logger.exception("Failed to record error telemetry")
        if isinstance(exc, McpOAuthInvocationDenied):
            from mcp.types import CallToolResult, TextContent

            return cast(
                T,
                CallToolResult(
                    content=[TextContent(type="text", text=exc.description)],
                    isError=True,
                    _meta={"mcp/www_authenticate": exc.challenge},
                ),
            )
        if error_mapper is not None:
            return error_mapper(error_msg)
        return cast(T, f"Error: {error_msg}")

    completed_at = _utc_now_iso()
    duration_ms = int((time.perf_counter() - started) * 1000)
    result_size = _size_of(result) if result is not None else None
    logger.info("MCP %s/%s completed in %dms", kind, operation, duration_ms)
    namespace, node_uuid = _resolved_lineage()
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
            payload_preview=_resolved_preview(),
            namespace=namespace,
            node_uuid=node_uuid,
            client_name=client_name,
            client_id=client_id,
            session_id=session_id,
            tier=tier,
            stage=STAGE_COMPLETED,
        )
    except sqlite3.Error:
        logger.exception("Failed to record success telemetry")
    return result
