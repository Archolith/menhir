"""Shared support functions for the combined FastAPI server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from menhir.api import oauth_as_register, oauth_authorize
from menhir.api.auth import BearerAuthMiddleware
from menhir.api.auth_code_store import configure_auth_code_store
from menhir.api.client_token_store import configure_client_token_store
from menhir.api.errors import error_response, request_id_for_request
from menhir.api.oauth_as_metadata import router as oauth_as_metadata_router
from menhir.api.oauth_as_register import router as oauth_as_register_router
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_client_store import configure_client_store
from menhir.api.oauth_keys import configure_signing_key
from menhir.api.oauth_metadata import router as oauth_metadata_router
from menhir.api.oauth_token import router as oauth_token_router
from menhir.api.request_context import RequestContextMiddleware
from menhir.api.routes import router as api_router
from menhir.config import MemorySettings, build_oauth_config, resolve_auth_mode
from menhir.config.settings import is_loopback_host
from menhir.core.runtime import RuntimeContext, start_runtime, stop_runtime
from menhir.explorer.integration import mount_explorer
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.candidate_service import CandidateService
from menhir.services.lifecycle_service import LifecycleService

logger = logging.getLogger(__name__)

BACKENDLESS_SCOPES = frozenset({"auth-only", "http-only", "no-backend"})


def build_server_prereqs(settings: MemorySettings) -> dict[str, Any]:
    """Construct auth/OAuth/client-token collaborators from the settings snapshot."""
    oauth_config = build_oauth_config(settings)
    client_token_store = configure_client_token_store(settings)
    oauth_as_register._register_limiter = oauth_as_register.build_register_limiter(settings)
    oauth_authorize._approve_limiter = oauth_authorize.build_approve_limiter(settings)
    oauth_client_store = None
    auth_code_store = None
    signing_key = None
    if settings.oauth_as_enabled:
        oauth_client_store = configure_client_store(settings)
        auth_code_store = configure_auth_code_store(settings)
        signing_key = configure_signing_key(settings)
    return {
        "oauth_config": oauth_config,
        "client_token_store": client_token_store,
        "oauth_client_store": oauth_client_store,
        "auth_code_store": auth_code_store,
        "signing_key": signing_key,
    }


def build_runtime_lifespan(
    *,
    settings: MemorySettings,
    startup_scope: str,
    mcp_http_instance: Any,
):
    """Build the FastAPI lifespan closure for runtime + MCP HTTP session management."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start shared runtime + streamable HTTP session manager."""
        if startup_scope in BACKENDLESS_SCOPES:
            logger.warning(
                "Starting menhir in reduced scope %r — memory backend NOT "
                "started; backend routes will return 503. Auth/OAuth surface is "
                "fully active. Do not use this scope in production.",
                startup_scope,
            )
            async with mcp_http_instance.session_manager.run():
                yield
            return
        logger.info("Starting menhir remote API server...")
        ctx: RuntimeContext = await start_runtime(settings)
        app.state.runtime_ctx = ctx
        app.state.repo = ctx.built.neo4j
        graph_adapter = MemoryGraphAdapter(neo4j=app.state.repo)
        lifecycle_service = LifecycleService(
            graph_adapter=graph_adapter,
            graphiti_client=ctx.built.graphiti_client,
            llm=ctx.built.llm,
            settings=settings,
        )
        app.state.candidate_service = CandidateService(
            graph_adapter=graph_adapter,
            lifecycle_service=lifecycle_service,
        )
        logger.info("Runtime ready — session=%s", ctx.session.session_id)
        async with mcp_http_instance.session_manager.run():
            try:
                yield
            finally:
                logger.info("Shutting down menhir remote API server...")
                await stop_runtime()

    return _lifespan


def register_exception_handlers(app: FastAPI) -> None:
    """Install consistent JSON exception handlers on the FastAPI app."""

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = request_id_for_request(request)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        code = f"http_{exc.status_code}"
        extra = {"headers": exc.headers} if exc.headers else None
        logger.warning(
            "HTTP exception: %s %s request_id=%s status=%s detail=%s",
            request.method,
            request.url.path,
            request_id,
            exc.status_code,
            detail,
        )
        return error_response(
            status_code=exc.status_code,
            error="HTTPException",
            detail=detail,
            code=code,
            request_id=request_id,
            extra=extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = request_id_for_request(request)
        logger.warning(
            "Validation error: %s %s request_id=%s errors=%s",
            request.method,
            request.url.path,
            request_id,
            len(exc.errors()),
        )
        return error_response(
            status_code=422,
            error="ValidationError",
            detail="Request validation failed",
            code="request_validation_error",
            request_id=request_id,
            extra={"errors": exc.errors()},
        )

    @app.exception_handler(PermissionError)
    async def _permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
        """Map a tenancy or tier refusal to 403 instead of letting it read as a crash.

        The backend boundary raises `PermissionError` for an ownership violation (see
        `core.tenancy`). Without this handler the generic `Exception` handler below catches it
        and returns 500 "An unexpected server error occurred" -- which is wrong twice: the
        caller cannot tell a deliberate refusal from a server fault, and an operator watching
        error rates sees a spike of fake internal errors when tenancy is working correctly.

        The refusal message is returned as the detail on purpose. It names the namespace the
        object actually belongs to only in the sense of "not yours" -- it never discloses the
        owning namespace -- and it tells a legitimate caller that got the wrong uuid what
        happened.
        """
        request_id = request_id_for_request(request)
        logger.warning(
            "Tenancy refusal: %s %s request_id=%s -- %s",
            request.method,
            request.url.path,
            request_id,
            exc,
        )
        return error_response(
            status_code=403,
            error="Forbidden",
            detail=str(exc) or "Forbidden",
            code="forbidden",
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_for_request(request)
        logger.exception(
            "Unhandled exception: %s %s request_id=%s — %s: %s",
            request.method,
            request.url.path,
            request_id,
            type(exc).__name__,
            exc,
        )
        return error_response(
            status_code=500,
            error="InternalServerError",
            detail="An unexpected server error occurred",
            code="internal_server_error",
            request_id=request_id,
        )



def configure_cors(app: FastAPI, settings: MemorySettings) -> None:
    """Attach CORS middleware when origins are configured."""
    cors_origins = list(settings.cors_origins)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )



def mount_server_routes(
    app: FastAPI,
    *,
    settings: MemorySettings,
    startup_scope: str,
    mcp_http_app: Any,
) -> None:
    """Mount REST/OAuth/explorer/MCP routes onto the FastAPI app."""
    app.include_router(oauth_metadata_router)
    app.include_router(oauth_as_metadata_router)
    app.include_router(oauth_as_register_router)
    app.include_router(oauth_authorize_router)
    app.include_router(oauth_token_router)
    app.include_router(api_router)

    if settings.explorer_enabled and startup_scope not in BACKENDLESS_SCOPES:
        mount_explorer(app)

    from menhir.api.mcp_remote import create_mcp_sse_app
    from starlette.routing import Route as StarletteRoute

    mcp_sse = create_mcp_sse_app()
    app.mount("/mcp", mcp_sse)
    streamable_handler = mcp_http_app.routes[0].app
    app.routes.insert(0, StarletteRoute("/mcp-http", endpoint=streamable_handler))



def wrap_server_middlewares(
    app: FastAPI,
    *,
    settings: MemorySettings,
    oauth_config: Any,
    client_token_store: Any,
) -> Any:
    """Wrap the FastAPI app with auth + request-context middleware."""
    app = BearerAuthMiddleware(  # type: ignore[assignment]
        app,
        api_key=settings.api_key,
        operator_key=settings.operator_key,
        agent_key=settings.agent_key,
        readonly_key=settings.readonly_key,
        oauth_config=oauth_config,
        client_token_store=client_token_store,
        loopback_bound=is_loopback_host(settings.api_host),
        auth_mode=resolve_auth_mode(settings),
    )
    app = RequestContextMiddleware(app)  # type: ignore[assignment]
    return app
