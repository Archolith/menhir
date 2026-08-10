"""MCP tool: list_clients — list registered (non-revoked) per-client tokens (operator only)."""

from __future__ import annotations

import json

from menhir.mcp.tools.base import BaseTextTool


async def list_clients() -> str:
    """List registered (non-revoked) per-client tokens (no token material)."""

    return await ListClientsTool().execute()


class ListClientsTool(BaseTextTool):
    name = "list_clients"
    required_tier = "operator"
    description = "list registered (non-revoked) per-client tokens (no token material)."

    async def endpoint(self) -> str:
        from menhir.api.client_token_store import get_client_token_store

        store = get_client_token_store()
        if store is None:
            return json.dumps({"error": "per-client token tier is not enabled (set MENHIR_CLIENT_TOKENS_ENABLED=1)"})

        return json.dumps({
            "clients": [
                {
                    "client_id": r.client_id,
                    "client_name": r.client_name,
                    "tier": r.tier,
                    "created_at": r.created_at,
                }
                for r in store.all()
            ]
        })
