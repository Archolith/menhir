"""MCP tool: revoke_client — revoke a client token by client_id (operator only)."""

from __future__ import annotations

import json

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def revoke_client(client_id: str) -> str:
    """Revoke a client token by client_id."""

    return await RevokeClientTool().execute(client_id=client_id)


class RevokeClientTool(BaseTextTool):
    name = "revoke_client"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "revoke a client token by client_id."
    title = "Revoke Client Token"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False

    async def endpoint(self, client_id: str) -> str:
        from menhir.api.client_token_store import get_client_token_store

        store = get_client_token_store()
        if store is None:
            return json.dumps({"error": "per-client token tier is not enabled (set MENHIR_CLIENT_TOKENS_ENABLED=1)"})

        revoked = store.revoke(client_id)
        return json.dumps({"client_id": client_id, "revoked": revoked})
