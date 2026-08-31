"""Production access invariant shared by runtime and release authoring.

The digest-bound client policy carries the concrete client identities.  This
module states the part that must not drift between those entries: one public
MCP data-plane endpoint, one cryptographic authorization protocol, and the
product-to-role mapping approved for this deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CANONICAL_PRIMARY_ENDPOINT = "https://memory.ctharvey.me/mcp-http"
EXPECTED_AUTHENTICATION = {
    "protocol": "oauth-2.1",
    "grant_type": "authorization_code",
    "pkce_method": "S256",
    "access_token": "signed_jwt",
    "client_identity": "policy_bound_client_id",
}
EXPECTED_PRODUCT_ROLES = {
    "chatgpt": "operator",
    "codex": "operator",
    "claude": "operator",
    "opencode": "agent",
}

OPERATOR_SCOPES = frozenset({"menhir:read", "menhir:write", "menhir:admin"})
AGENT_SCOPES = frozenset({"menhir:read", "menhir:write"})
OPERATOR_DENIED_TOOLS = frozenset(
    {"delete_namespace", "mint_client", "revoke_client"}
)
AGENT_ALLOWED_TOOLS = frozenset(
    {
        "add_memory",
        "build_context",
        "list_todos",
        "query_structure",
        "read_flagged_memories",
        "recall_context_memories",
        "recall_memories",
    }
)
OPERATOR_REQUIRED_TOOLS = frozenset(
    {
        "get_provenance",
        "get_episode_trace",
        "ingest_document",
        "ingest_project",
        "list_clients",
        "pause_scheduler",
        "resolve_conflict",
    }
)


@dataclass(frozen=True)
class ProductAccess:
    role: str
    client_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProductionAccessContract:
    primary_endpoint: str
    authentication: dict[str, str]
    products: dict[str, ProductAccess]

    def require_primary_endpoint(self, endpoint: str) -> None:
        if endpoint != self.primary_endpoint:
            raise ValueError(
                "production OAuth resource must equal the policy access contract "
                f"primary endpoint {self.primary_endpoint!r}"
            )


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly {sorted(expected)}")
    return value


def validate_access_contract(
    value: object,
    clients: object,
    *,
    tool_catalog: frozenset[str] | None = None,
) -> ProductionAccessContract:
    """Validate the production product/identity/role invariant or fail closed."""

    contract = _exact_keys(
        value,
        {"primary_endpoint", "authentication", "products"},
        "production access contract",
    )
    if contract["primary_endpoint"] != CANONICAL_PRIMARY_ENDPOINT:
        raise ValueError(
            "production access contract must use the canonical /mcp-http endpoint"
        )
    authentication = _exact_keys(
        contract["authentication"],
        set(EXPECTED_AUTHENTICATION),
        "production access contract authentication",
    )
    if authentication != EXPECTED_AUTHENTICATION:
        raise ValueError(
            "production access contract authentication must be OAuth 2.1 "
            "authorization code + PKCE S256 with signed JWT access tokens"
        )
    products = _exact_keys(
        contract["products"],
        set(EXPECTED_PRODUCT_ROLES),
        "production access contract products",
    )
    if not isinstance(clients, dict):
        raise ValueError("production client policy clients must be an object")

    parsed: dict[str, ProductAccess] = {}
    seen_client_ids: set[str] = set()
    for product, expected_role in EXPECTED_PRODUCT_ROLES.items():
        entry = _exact_keys(
            products[product],
            {"role", "client_ids"},
            f"production access contract product {product}",
        )
        role = entry["role"]
        client_ids = entry["client_ids"]
        if role != expected_role:
            raise ValueError(
                f"production access contract requires {product} role {expected_role}"
            )
        if (
            not isinstance(client_ids, list)
            or not client_ids
            or any(not isinstance(client_id, str) or not client_id for client_id in client_ids)
            or len(client_ids) != len(set(client_ids))
        ):
            raise ValueError(
                f"production access contract {product} client_ids must be unique strings"
            )
        overlap = seen_client_ids & set(client_ids)
        if overlap:
            raise ValueError(
                "production access contract client identity belongs to multiple products"
            )
        seen_client_ids.update(client_ids)

        for client_id in client_ids:
            raw = clients.get(client_id)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"production access contract references unknown {product} client_id"
                )
            scopes = frozenset(str(scope) for scope in raw.get("scopes", ()))
            allowed = frozenset(str(tool) for tool in raw.get("allowed_tools", ()))
            denied = frozenset(str(tool) for tool in raw.get("denied_tools", ()))
            if raw.get("maximum_tier") != role:
                raise ValueError(
                    f"production access contract {product} client tier does not match {role}"
                )
            if role == "operator":
                if scopes != OPERATOR_SCOPES:
                    raise ValueError(
                        f"production access contract {product} operator scopes drifted"
                    )
                if denied != OPERATOR_DENIED_TOOLS:
                    raise ValueError(
                        f"production access contract {product} operator deny boundary drifted"
                    )
                if not OPERATOR_REQUIRED_TOOLS <= allowed:
                    raise ValueError(
                        f"production access contract {product} operator tool surface is incomplete"
                    )
                if tool_catalog is not None and allowed != tool_catalog - denied:
                    raise ValueError(
                        f"production access contract {product} operator tool census drifted"
                    )
            else:
                if scopes != AGENT_SCOPES:
                    raise ValueError(
                        f"production access contract {product} agent scopes drifted"
                    )
                if allowed != AGENT_ALLOWED_TOOLS:
                    raise ValueError(
                        f"production access contract {product} agent tool surface drifted"
                    )
                if tool_catalog is not None and denied != tool_catalog - allowed:
                    raise ValueError(
                        f"production access contract {product} agent tool census drifted"
                    )

        parsed[product] = ProductAccess(role=role, client_ids=tuple(client_ids))

    return ProductionAccessContract(
        primary_endpoint=contract["primary_endpoint"],
        authentication=dict(authentication),
        products=parsed,
    )


__all__ = [
    "AGENT_ALLOWED_TOOLS",
    "AGENT_SCOPES",
    "CANONICAL_PRIMARY_ENDPOINT",
    "EXPECTED_AUTHENTICATION",
    "EXPECTED_PRODUCT_ROLES",
    "OPERATOR_DENIED_TOOLS",
    "OPERATOR_REQUIRED_TOOLS",
    "OPERATOR_SCOPES",
    "ProductAccess",
    "ProductionAccessContract",
    "validate_access_contract",
]
