"""MCP tool: mint_client — mint a new per-client token (operator only)."""

from __future__ import annotations

import json

from menhir.mcp.tools.base import BaseTextTool


async def mint_client(client_name: str, tier: str = "readonly") -> str:
    """Mint a new per-client token; returns the raw token ONCE."""

    return await MintClientTool().execute(client_name=client_name, tier=tier)


class MintClientTool(BaseTextTool):
    name = "mint_client"
    required_tier = "operator"
    description = "mint a new per-client token; returns the raw token ONCE."

    async def endpoint(self, client_name: str, tier: str = "readonly") -> str:
        from menhir.api.client_token_store import get_client_token_store

        store = get_client_token_store()
        if store is None:
            return json.dumps({"error": "per-client token tier is not enabled (set MENHIR_CLIENT_TOKENS_ENABLED=1)"})

        if tier not in {"operator", "agent", "readonly"}:
            return json.dumps({"error": "tier must be operator, agent, or readonly"})

        if not client_name.strip():
            return json.dumps({"error": "client_name is required"})

        raw, record = store.mint(client_name.strip(), tier)
        return json.dumps({
            "client_id": record.client_id,
            "client_name": record.client_name,
            "tier": record.tier,
            "token": raw,
        })
