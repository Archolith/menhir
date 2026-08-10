"""Combined FastAPI server — REST + MCP SSE, single process."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI

import menhir
from menhir.api.mcp_remote import create_mcp_streamable_http_app
from menhir.config import MemorySettings
from menhir.infrastructure.logging_config import build_logging_config, configure_logging

from .server_support import (
    build_runtime_lifespan,
    build_server_prereqs,
    configure_cors,
    mount_server_routes,
    register_exception_handlers,
    wrap_server_middlewares,
)

logger = logging.getLogger(__name__)


def create_app(*, settings: MemorySettings | None = None) -> FastAPI:
    """Build the combined FastAPI application."""
    settings = settings or MemorySettings.from_env()
    startup_scope = settings.startup_scope
    prereqs = build_server_prereqs(settings)
    mcp_http_app, mcp_http_instance = create_mcp_streamable_http_app()

    app = FastAPI(
        title="menhir",
        description="Long-term graph memory for AI agents — REST + MCP remote.",
        version=menhir.__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=build_runtime_lifespan(
            settings=settings,
            startup_scope=startup_scope,
            mcp_http_instance=mcp_http_instance,
        ),
    )
    app.state.settings = settings
    app.state.client_token_store = prereqs["client_token_store"]
    app.state.oauth_client_store = prereqs["oauth_client_store"]
    app.state.auth_code_store = prereqs["auth_code_store"]
    app.state.oauth_signing_key = prereqs["signing_key"]

    register_exception_handlers(app)
    configure_cors(app, settings)
    mount_server_routes(
        app,
        settings=settings,
        startup_scope=startup_scope,
        mcp_http_app=mcp_http_app,
    )

    wrapped = wrap_server_middlewares(
        app,
        settings=settings,
        oauth_config=prereqs["oauth_config"],
        client_token_store=prereqs["client_token_store"],
    )
    return wrapped  # type: ignore[return-value]



def main() -> None:
    """CLI entry point for ``menhir serve``."""
    import uvicorn

    load_dotenv(os.getenv("ENV_FILE") or None)
    settings = MemorySettings.from_env()
    configure_logging()
    logger.info("Launching uvicorn on %s:%d", settings.api_host, settings.api_port)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        log_level="info",
        log_config=build_logging_config(),
    )
