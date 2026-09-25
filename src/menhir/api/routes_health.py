"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import Request

from .routes_router import router
from .routes_support import (
    HealthResponse,
    ReadyResponse,
    _capability_payload,
    _get_runtime_context,
    _provider_auth_failure_text,
    _service_payload,
)


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    runtime_ctx = _get_runtime_context(request)
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    settings = getattr(request.app.state, "settings", None)
    status = "ok" if runtime_ctx is not None else "starting"
    return HealthResponse(
        status=status,
        services=_service_payload(runtime_ctx),
        startup_mode=capabilities.startup_mode if capabilities is not None else None,
        instance_id=str(settings.instance_id) or None if settings is not None else None,
        provider_auth_failure=_provider_auth_failure_text(),
    )


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse:
    runtime_ctx = _get_runtime_context(request)
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    if capabilities is None:
        return ReadyResponse(
            status="starting",
            startup_mode="unavailable",
            capabilities=_capability_payload(None),
            failures=["runtime not initialized"],
        )
    auth_failure = _provider_auth_failure_text()
    status = "ready" if capabilities.enrichment_ready and auth_failure is None else "degraded"
    return ReadyResponse(
        status=status,
        startup_mode=capabilities.startup_mode,
        capabilities=_capability_payload(runtime_ctx),
        failures=list(capabilities.failures),
        provider_auth_failure=auth_failure,
    )
