"""Per-client token tier — admin endpoints (mint / revoke)."""

from __future__ import annotations

from fastapi import Request

from menhir.mcp.service_access import get_request_session

from .routes_handlers import list_clients_impl, mint_client_impl, revoke_client_impl
from .routes_router import router
from .routes_support import (
    ListClientsResponse,
    MintClientRequest,
    MintClientResponse,
    RevokeClientResponse,
    _VALID_CLIENT_TIERS,
    _get_client_token_store,
    _require_tier,
    _try_record_destructive_op_rest,
)

# ---------------------------------------------------------------------------
# Auth for /api/admin/* is enforced by BearerAuthMiddleware's admin gate
# (operator key or loopback origin), which binds the operator tier before the
# handler runs. _require_tier("operator") below is defense-in-depth.
# ---------------------------------------------------------------------------

@router.post("/admin/clients", response_model=MintClientResponse)
async def mint_client(request: Request, body: MintClientRequest) -> MintClientResponse:
    return await mint_client_impl(
        request,
        body,
        require_tier=_require_tier,
        valid_client_tiers=_VALID_CLIENT_TIERS,
        get_client_token_store=_get_client_token_store,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
        get_request_session_func=get_request_session,
    )


@router.get("/admin/clients", response_model=ListClientsResponse)
async def list_clients(request: Request) -> ListClientsResponse:
    return await list_clients_impl(
        request,
        require_tier=_require_tier,
        get_client_token_store=_get_client_token_store,
    )


@router.post("/admin/clients/{client_id}/revoke", response_model=RevokeClientResponse)
async def revoke_client(request: Request, client_id: str) -> RevokeClientResponse:
    return await revoke_client_impl(
        request,
        client_id,
        require_tier=_require_tier,
        get_client_token_store=_get_client_token_store,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
    )
