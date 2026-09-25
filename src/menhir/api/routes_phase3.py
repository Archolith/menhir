"""Phase 3 View-consolidation endpoints (thin wrappers over ``routes_handlers``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query, Request

from .routes_handlers import phase3_run_impl, phase3_status_impl, phase3_views_impl
from .routes_router import router
from .routes_support import (
    Phase3RunRequest,
    Phase3RunResponse,
    Phase3StatusResponse,
    Phase3ViewsResponse,
    _require_phase3_adapter,
    _require_tier,
)


@router.post("/phase3/run", response_model=Phase3RunResponse)
async def phase3_run(request: Request, body: Phase3RunRequest) -> Phase3RunResponse:
    """Run one personal-memory View consolidation pass over a single namespace (black-box eval
    surface for archolith-bench). Mirrors the scheduler job: real LLM, all bias guards pinned on,
    batch re-fold, isolated to the explicit namespace so it never touches other silos."""
    return await phase3_run_impl(
        request,
        body,
        require_tier=_require_tier,
        require_phase3_adapter=_require_phase3_adapter,
    )


@router.get("/phase3/status", response_model=Phase3StatusResponse)
async def phase3_status(
    request: Request,
    namespace: Annotated[str, Query(min_length=1)],
) -> Phase3StatusResponse:
    """Report whether a namespace is dirty (Phase 3 would consolidate it) and its evidence count."""
    return await phase3_status_impl(
        request,
        namespace,
        require_phase3_adapter=_require_phase3_adapter,
    )


@router.get("/views", response_model=Phase3ViewsResponse)
async def phase3_views(
    request: Request,
    namespace: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Phase3ViewsResponse:
    """List current counter Views for a namespace, each with full value history (current +
    superseded, so currentness/supersession is inspectable). USER Views and `subject='perception'`
    abstention receipts (F1 observability, out of recall) are returned separately."""
    return await phase3_views_impl(
        request,
        namespace,
        limit,
        require_phase3_adapter=_require_phase3_adapter,
    )
