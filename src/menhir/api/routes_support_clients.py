"""Per-client API token tier models and the token store accessor (see routes_support)."""

from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel

_VALID_CLIENT_TIERS = frozenset({"operator", "agent", "readonly"})


class MintClientRequest(BaseModel):
    client_name: str
    tier: str = "readonly"


class MintClientResponse(BaseModel):
    client_id: str
    client_name: str
    tier: str
    token: str  # shown once at mint time — never retrievable again


class RevokeClientResponse(BaseModel):
    client_id: str
    revoked: bool


class ClientSummary(BaseModel):
    client_id: str
    client_name: str
    tier: str
    created_at: float


class ListClientsResponse(BaseModel):
    clients: list[ClientSummary]


def _get_client_token_store(request: Request):
    store = getattr(request.app.state, "client_token_store", None)
    if store is None:
        raise HTTPException(status_code=404, detail="Per-client token tier is not enabled")
    return store
