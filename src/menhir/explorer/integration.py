"""Integration point for mounting explorer routes into the main FastAPI app."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from menhir.explorer.app import create_explorer_router

STATIC_DIR = Path(__file__).with_name("static")


def mount_explorer(app: FastAPI) -> None:
    """Mount explorer routes and static files onto the FastAPI app.

    The explorer must be mounted BEFORE the BearerAuthMiddleware so routes
    exist when auth middleware checks them. Assumes app.state.repo and
    app.state.candidate_service are already set by the lifespan.
    """
    router = create_explorer_router()
    app.include_router(router)
    app.mount("/explorer/static", StaticFiles(directory=str(STATIC_DIR)), name="explorer-static")
