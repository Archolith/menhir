"""Internal backend passthrough endpoint (``/internal/backend/{operation}``)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from menhir.core.backend_impl import _drain_background_errors

from .routes_handlers import backend_invoke_impl
from .routes_router import router
from .routes_support import (
    _BACKEND_METHODS,
    _get_backend,
    _require_tier,
    _required_tier_for_operation,
    _resolve_caller_session,
    _try_record_destructive_op_rest,
)

logger = logging.getLogger(__name__)

_INTERNAL_BACKEND_PREFIX = "/internal/backend"


@router.post(f"{_INTERNAL_BACKEND_PREFIX}/{{operation}}", include_in_schema=False)
async def backend_invoke(request: Request, operation: str, body: dict[str, Any] | None = None) -> Any:
    return await backend_invoke_impl(
        request,
        operation,
        body,
        backend_methods=_BACKEND_METHODS,
        required_tier_for_operation=_required_tier_for_operation,
        require_tier=_require_tier,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
        resolve_caller_session=_resolve_caller_session,
        get_backend=_get_backend,
        drain_background_errors=_drain_background_errors,
        logger=logger,
    )
