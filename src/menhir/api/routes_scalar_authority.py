"""Scalar-authority provenance expansion endpoint."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import HTTPException, Query, Request

from .routes_router import router
from .routes_support import _require_runtime_context, _require_tier, _resolve_namespace


@router.get("/scalar-authority/{view_uuid}/contributors")
async def scalar_authority_contributors(
    request: Request,
    view_uuid: str,
    namespace: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Expand a structured scalar-authority verdict's bounded provenance window."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "fetch_scalar_authority_contributors"):
        raise HTTPException(status_code=503, detail="scalar authority provenance unavailable")
    resolved = _resolve_namespace(request, namespace)
    from menhir.domain.namespace import stamped_namespace
    payload = await asyncio.to_thread(
        adapter.fetch_scalar_authority_contributors,
        view_uuid=view_uuid, limit=limit, offset=offset,
        namespace=stamped_namespace(resolved),
    )
    return {"view_uuid": view_uuid, **payload}
