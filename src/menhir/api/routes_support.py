"""Shared helpers, policies, and models for REST API routes."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from menhir.core.backend_impl import RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.runtime import RuntimeContext
from menhir.domain.session import MemorySession, new_session
from menhir.infrastructure.telemetry import record_destructive_op
from menhir.mcp.service_access import (
    get_pinned_namespace,
    get_request_session,
    get_request_tier,
)

# Request/response models, the backend dispatch policy, and the per-client token tier models
# live in sibling modules; every name is re-exported here so existing import sites
# (`from .routes_support import X`) keep working unchanged.
from .routes_support_clients import (
    ClientSummary,
    ListClientsResponse,
    MintClientRequest,
    MintClientResponse,
    RevokeClientResponse,
    _VALID_CLIENT_TIERS,
    _get_client_token_store,
)
from .routes_support_dispatch import (
    DEPRECATED_OPERATIONS,
    _BACKEND_METHODS,
    _OP_TIER_AGENT,
    _OP_TIER_OPERATOR,
    _required_tier_for_operation,
    deprecated_operation_notice,
)
from .routes_support_models import (
    BootstrapContextRequest,
    ContextRequest,
    ContextResponse,
    EpisodeAdmissionRequest,
    EpisodeAdmissionResponse,
    EventAuthorityVerdictResponse,
    FlagResponse,
    HealthResponse,
    MemoryRequest,
    MemoryResponse,
    Phase3ResetResponse,
    Phase3RunRequest,
    Phase3RunResponse,
    Phase3StatusResponse,
    Phase3ViewsResponse,
    RecallMemory,
    RecallRequest,
    RecallResponse,
    RecallTemporalFact,
    ReadyResponse,
    ScalarAuthorityContributorResponse,
    ScalarAuthorityVerdictResponse,
    StaleAnchorVerificationRequest,
    StaleAnchorVerificationResponse,
    StatsResponse,
    ToolEventRequest,
    ToolEventResponse,
    TurnEvidenceRequest,
    TurnEvidenceResponse,
    UnflagResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# REST tier enforcement (auth Phase 0 / tracker Q4) — mirror of the MCP tool path
# (mcp/contracts.py). The middleware resolves the bearer to a tier; routes enforce a
# minimum. An empty tier means auth is disabled (no keys configured) — enforcement is
# skipped, matching the MCP semantics exactly.
# ---------------------------------------------------------------------------

_TIER_RANK = {"readonly": 0, "agent": 1, "operator": 2}


def _require_tier(required: str) -> None:
    tier = get_request_tier()
    if tier and _TIER_RANK.get(tier, -1) < _TIER_RANK.get(required, 0):
        raise HTTPException(
            status_code=403,
            detail=f"Token tier '{tier}' cannot invoke this endpoint (requires '{required}')",
        )


def _try_record_destructive_op_rest(name: str) -> None:
    """Record operator-tier REST endpoint invocation for audit log (best-effort, non-blocking)."""
    try:
        tier = get_request_tier()
        session = get_request_session()
        user_id = session.user_id if session else ""
        session_id = session.session_id if session else ""
        record_destructive_op(
            surface="rest",
            name=name,
            tier=tier,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception:
        pass  # Telemetry failures must never disrupt the caller


def _get_runtime_context(request: Request) -> RuntimeContext | None:
    return getattr(request.app.state, "runtime_ctx", None)


def _require_runtime_context(request: Request) -> RuntimeContext:
    runtime_ctx = _get_runtime_context(request)
    if runtime_ctx is None:
        raise HTTPException(status_code=503, detail="menhir runtime is not ready")
    return runtime_ctx


def _capability_payload(runtime_ctx: RuntimeContext | None) -> dict[str, bool]:
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    if capabilities is None:
        return {
            "neo4j_ready": False,
            "embedder_ready": False,
            "llm_ready": False,
            "reads_ready": False,
            "queue_writes_ready": False,
            "enrichment_ready": False,
        }
    return {
        "neo4j_ready": capabilities.neo4j_ready,
        "embedder_ready": capabilities.embedder_ready,
        "llm_ready": capabilities.llm_ready,
        "reads_ready": capabilities.reads_ready,
        "queue_writes_ready": capabilities.queue_writes_ready,
        "enrichment_ready": capabilities.enrichment_ready,
    }


def _provider_auth_failure_text() -> str | None:
    from menhir.infrastructure.observability import last_provider_auth_failure

    failure = last_provider_auth_failure()
    return failure.summary() if failure is not None else None


def _service_payload(runtime_ctx: RuntimeContext | None) -> dict[str, str]:
    if runtime_ctx is None:
        return {"runtime": "starting"}
    built = runtime_ctx.built
    capabilities = runtime_ctx.capabilities
    return {
        "neo4j": "ok" if capabilities is None or capabilities.neo4j_ready else "unavailable",
        "graphiti": "ok" if capabilities is None or capabilities.graphiti_ready else "degraded",
        "ingest": "ok" if built.ingest_service and built.ingest_service.enrichment_enabled() else "queue_only",
        "recall": "ok" if capabilities is None or capabilities.reads_ready else "degraded",
        "llm_auth": "rejected" if _provider_auth_failure_text() else "ok",
    }


def _get_backend(request: Request) -> MemoryBackend:
    runtime_ctx = _require_runtime_context(request)
    return RuntimeProvider(
        runtime_ctx.built,
        process_session=runtime_ctx.session,
        caller_session=_resolve_caller_session(request),
    )


def _resolve_caller_session(request: Request, *, default_user_id: str = "remote-api") -> MemorySession:
    bound_session = get_request_session()
    if bound_session is not None:
        return bound_session

    user_id = _caller_header(request, "user-id") or default_user_id
    session_id = _caller_header(request, "session-id") or None
    return new_session(user_id, session_id=session_id)


def _caller_header(request: Request, suffix: str) -> str:
    """Read ``x-menhir-<suffix>``.

    The deprecated ``x-yawn-<suffix>`` alias is no longer accepted: two spellings for the
    same identity assertion is one more than a caller needs, and no client sends the old one.
    """
    return (request.headers.get(f"x-menhir-{suffix}") or "").strip()


def _resolve_namespace(request: Request, body_namespace: str | None) -> str | None:
    """Namespace precedence: server-side pin, then request body, then x-menhir-namespace header.

    None preserves the legacy global behavior (no isolation); an explicit value scopes
    the operation to that silo.

    THE PIN WINS, and it is checked first. `MENHIR_CLIENT_NAMESPACES` binds a client to a
    namespace server-side, and `BaseTool._apply_pinned_namespace` documents the guarantee as
    absolute: a pinned client "cannot escape it, whether by passing another namespace or by
    omitting the argument entirely". That was true only of MCP tools. REST never consulted the
    pin at all, so a credential restricted to one namespace through MCP reached every namespace
    through HTTP by putting one in the request body -- the same client, the same server-side
    policy, one transport enforcing it.

    Nothing new is needed to fix that: the auth middleware already binds the request session
    (carrying `client_name`) on this path, so `get_pinned_namespace()` resolves here exactly as
    it does under MCP. It was simply never called.

    A mismatch is logged rather than rejected, matching the tool path's behaviour precisely --
    the two surfaces must not disagree about what a pinned client's request means.
    """
    pinned = get_pinned_namespace()
    if pinned:
        requested = (body_namespace or _caller_header(request, "namespace") or "").strip()
        if requested and requested != pinned:
            logger.warning(
                "namespace pin: REST client requested namespace=%r; forcing %r",
                requested,
                pinned,
            )
        return pinned
    if body_namespace:
        return body_namespace
    return _caller_header(request, "namespace") or None


def _require_phase3_adapter(request: Request):
    """Return the graph adapter for Phase 3 endpoints, or 503 if the runtime is not ready."""
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="phase 3 consolidation unavailable")
    return runtime_ctx, adapter


def record_deprecated_operation_call(operation: str, *, admitted: bool) -> None:
    """Count a call to a deprecated operation, so removal rests on evidence.

    BOTH outcomes are recorded, not only refusals. A window with zero refusals proves only that
    nobody under-privileged tried; showing there is no LEGITIMATE use needs the admitted count to
    be zero as well. Removing on refusals alone would delete an endpoint an operator still runs.

    Best-effort by construction: telemetry must never break the operation it observes, and this
    sits on the request path of an already-failing call.
    """
    try:
        from menhir.infrastructure.telemetry import recorders

        recorders.record_mcp_event(
            kind="deprecated_operation",
            operation=operation,
            duration_ms=0,
            success=admitted,
            payload={"admitted": admitted, "tier": get_request_tier() or ""},
        )
    except Exception:  # pragma: no cover - never let measurement break the caller
        logger.debug("deprecated-op telemetry failed for %s", operation, exc_info=True)
