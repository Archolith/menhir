"""MCP tool: mint_client — mint a new per-client token (operator only)."""

from __future__ import annotations

import json

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def mint_client(client_name: str, tier: str = "readonly") -> str:
    """Mint a new per-client token; returns the raw token ONCE."""

    return await MintClientTool().execute(client_name=client_name, tier=tier)


class MintClientTool(BaseTextTool):
    name = "mint_client"
    scope = ToolScope.GLOBAL
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

        # CF-83: a namespace pin is server config keyed on client_name, so minting a name that
        # is not configured produces a credential no pin covers -- and a pinned operator could
        # therefore mint its way out of its own data boundary. The pin is documented as absolute
        # ("cannot escape it"); a boundary you can mint your way out of is not a boundary.
        #
        # This does NOT contradict the decision that a pinned client may invoke a GLOBAL tool.
        # That decision says the pin bounds DATA and tier bounds ACTIONS. Minting is a global
        # action whose EFFECT is a new principal with a different data boundary, which is why
        # allowing global actions does not settle it. The action stays allowed; what it may
        # produce is constrained to identities the server operator already declared.
        #
        # Only when restrictions exist. A deployment that configures no per-client policy has no
        # pin to escape, so minting is unrestricted exactly as before.
        #
        # `mint_bootstrap` is deliberately untouched -- first-token bootstrap runs before any
        # client could be declared, and it has its own single-active-token guard (CT-003).
        from menhir.mcp.service_access import (
            client_restrictions_configured,
            declared_client_names,
        )

        requested = client_name.strip()
        if (
            client_restrictions_configured()
            and requested.lower() not in declared_client_names()
        ):
            return json.dumps({
                "error": (
                    f"refusing to mint undeclared client {requested!r}: this deployment "
                    "configures per-client restrictions, so a minted identity must be declared "
                    "server-side first. Add it to MENHIR_CLIENT_NAMESPACES (pinned), "
                    "MENHIR_CLIENT_TOOLS (tool-restricted), or MENHIR_KNOWN_CLIENTS "
                    "(recognized, unrestricted), then mint."
                )
            })

        raw, record = store.mint(requested, tier)
        return json.dumps({
            "client_id": record.client_id,
            "client_name": record.client_name,
            "tier": record.tier,
            "token": raw,
        })
