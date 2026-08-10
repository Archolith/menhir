"""Shared API error envelope helpers."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from menhir.api.request_context import get_request_id, get_request_id_from_scope


def error_payload(
    *,
    error: str,
    detail: str,
    code: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": error,
        "detail": detail,
        "code": code,
        "request_id": request_id,
    }
    if extra:
        payload.update(extra)
    return payload


def request_id_for_request(request: Request) -> str | None:
    state_id = getattr(request.state, "request_id", None)
    if isinstance(state_id, str) and state_id:
        return state_id
    return get_request_id()


def request_id_for_scope(scope: dict[str, Any]) -> str | None:
    return get_request_id_from_scope(scope) or get_request_id()


def error_response(
    *,
    status_code: int,
    error: str,
    detail: str,
    code: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(
            error=error,
            detail=detail,
            code=code,
            request_id=request_id,
            extra=extra,
        ),
    )
