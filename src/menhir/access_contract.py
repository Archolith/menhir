"""Production access invariant shared by runtime and release authoring.

The digest-bound client policy carries the concrete client identities.  This
module states the part that must not drift between those entries: one public
MCP data-plane endpoint, one cryptographic authorization protocol, and a
product-to-role mapping. Product names and roles belong to the deployment's
policy; this module only enforces that each product's clients match its role.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: The one public MCP data-plane path. The origin is deployment configuration
#: (``MENHIR_PUBLIC_BASE_URL``); the policy's ``primary_endpoint`` must be that origin
#: plus this path, and production startup binds it to ``MENHIR_OAUTH_RESOURCE``.
PRIMARY_ENDPOINT_PATH = "/mcp-http"
EXPECTED_AUTHENTICATION = {
    "protocol": "oauth-2.1",
    "grant_type": "authorization_code",
    "pkce_method": "S256",
    "access_token": "signed_jwt",
    "client_identity": "policy_bound_client_id",
}
READONLY_SCOPES = frozenset({"menhir:read"})
OPERATOR_SCOPES = frozenset({"menhir:read", "menhir:write", "menhir:admin"})
AGENT_SCOPES = frozenset({"menhir:read", "menhir:write"})
#: Exact OAuth permission scopes for each tier.
TIER_SCOPES: dict[str, frozenset[str]] = {
    "readonly": READONLY_SCOPES,
    "agent": AGENT_SCOPES,
    "operator": OPERATOR_SCOPES,
}
#: Roles a version 2 contract may name; version 2 fixes their tool sets below.
V2_PRODUCT_ROLES = frozenset({"operator", "agent"})

# The tool sets below bind version 2 policies only; version 3 uses tool profiles.
OPERATOR_DENIED_TOOLS = frozenset(
    {"delete_namespace", "mint_client", "revoke_client"}
)
AGENT_ALLOWED_TOOLS = frozenset(
    {
        "add_memory",
        "add_todo",
        "build_context",
        "close_stale_todos",
        "close_todo",
        "get_todo",
        "list_todos",
        # Read-only Beacon memory evidence; the same structure facts query_structure exposes.
        "get_beacon_evidence",
        "get_scalar_view_items",
        "query_structure",
        "read_flagged_memories",
        "recall_context_memories",
        "recall_history",
        "recall_memories",
        "recall_timeline",
    }
)
OPERATOR_REQUIRED_TOOLS = frozenset(
    {
        "acknowledge_warning",
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

    def require_oauth_scope_mapping(
        self,
        *,
        scopes_supported: tuple[str, ...],
        read_scopes: tuple[str, ...],
        write_scopes: tuple[str, ...],
        admin_scopes: tuple[str, ...],
    ) -> None:
        """Refuse a runtime that cannot issue every production access role."""

        expected = {
            "supported": OPERATOR_SCOPES,
            "read": frozenset({"menhir:read"}),
            "write": frozenset({"menhir:write"}),
            "admin": frozenset({"menhir:admin"}),
        }
        actual = {
            "supported": frozenset(scopes_supported),
            "read": frozenset(read_scopes),
            "write": frozenset(write_scopes),
            "admin": frozenset(admin_scopes),
        }
        if actual != expected:
            raise ValueError(
                "production OAuth scope mapping must expose read/write/admin "
                "and map menhir:admin to the operator tier"
            )


def is_canonical_primary_endpoint(value: object) -> bool:
    """True when ``value`` is ``https://<host>/mcp-http`` with no query or fragment."""

    if not isinstance(value, str) or not value:
        return False
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and bool(parts.netloc)
        and parts.path == PRIMARY_ENDPOINT_PATH
        and not parts.query
        and not parts.fragment
    )


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly {sorted(expected)}")
    return value


@dataclass(frozen=True)
class ResolvedClientAccess:
    """What a version 3 contract checks about one client after profile resolution."""

    maximum_tier: str
    scopes: frozenset[str]
    allowed_tools: frozenset[str]


def _parse_contract_header(
    value: object,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    contract = _exact_keys(
        value,
        {"primary_endpoint", "authentication", "products"},
        "production access contract",
    )
    if not is_canonical_primary_endpoint(contract["primary_endpoint"]):
        raise ValueError(
            "production access contract must use the canonical /mcp-http endpoint "
            "on an HTTPS origin"
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
    products = contract["products"]
    if (
        not isinstance(products, dict)
        or not products
        or any(not isinstance(name, str) or not name.strip() for name in products)
    ):
        raise ValueError(
            "production access contract products must be a non-empty object "
            "keyed by product name"
        )
    return contract, authentication, products


def _parse_products(
    products: dict[str, Any],
    *,
    allowed_roles: frozenset[str],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    parsed: dict[str, tuple[str, tuple[str, ...]]] = {}
    seen_client_ids: set[str] = set()
    for product, raw_entry in products.items():
        entry = _exact_keys(
            raw_entry,
            {"role", "client_ids"},
            f"production access contract product {product}",
        )
        role = entry["role"]
        client_ids = entry["client_ids"]
        if role not in allowed_roles:
            raise ValueError(
                f"production access contract {product} role must be one of "
                f"{sorted(allowed_roles)}"
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
        if seen_client_ids & set(client_ids):
            raise ValueError(
                "production access contract client identity belongs to multiple products"
            )
        seen_client_ids.update(client_ids)
        parsed[product] = (role, tuple(client_ids))
    return parsed


def validate_access_contract(
    value: object,
    clients: object,
    *,
    tool_catalog: frozenset[str] | None = None,
) -> ProductionAccessContract:
    """Validate a version 2 product/identity/role invariant or fail closed."""

    contract, authentication, products = _parse_contract_header(value)
    if not isinstance(clients, dict):
        raise ValueError("production client policy clients must be an object")

    parsed: dict[str, ProductAccess] = {}
    for product, (role, client_ids) in _parse_products(
        products, allowed_roles=V2_PRODUCT_ROLES
    ).items():
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

        parsed[product] = ProductAccess(role=role, client_ids=client_ids)

    return ProductionAccessContract(
        primary_endpoint=contract["primary_endpoint"],
        authentication=dict(authentication),
        products=parsed,
    )


def validate_access_contract_v3(
    value: object,
    clients: Mapping[str, ResolvedClientAccess],
    *,
    forbidden_product_tools: frozenset[str],
) -> ProductionAccessContract:
    """Validate a version 3 contract against clients whose profiles are resolved.

    Each product's clients must sit exactly at the product's tier, hold exactly
    that tier's scopes, and resolve none of ``forbidden_product_tools``.
    """

    contract, authentication, products = _parse_contract_header(value)
    parsed: dict[str, ProductAccess] = {}
    for product, (role, client_ids) in _parse_products(
        products, allowed_roles=frozenset(TIER_SCOPES)
    ).items():
        for client_id in client_ids:
            client = clients.get(client_id)
            if client is None:
                raise ValueError(
                    f"production access contract references unknown {product} client_id"
                )
            if client.maximum_tier != role:
                raise ValueError(
                    f"production access contract {product} client tier does not match {role}"
                )
            if client.scopes != TIER_SCOPES[role]:
                raise ValueError(
                    f"production access contract {product} {role} scopes drifted"
                )
            leaked = sorted(client.allowed_tools & forbidden_product_tools)
            if leaked:
                raise ValueError(
                    f"production access contract {product} client resolves admin tools {leaked}"
                )
        parsed[product] = ProductAccess(role=role, client_ids=client_ids)

    return ProductionAccessContract(
        primary_endpoint=contract["primary_endpoint"],
        authentication=dict(authentication),
        products=parsed,
    )


__all__ = [
    "AGENT_ALLOWED_TOOLS",
    "AGENT_SCOPES",
    "EXPECTED_AUTHENTICATION",
    "OPERATOR_DENIED_TOOLS",
    "OPERATOR_REQUIRED_TOOLS",
    "OPERATOR_SCOPES",
    "PRIMARY_ENDPOINT_PATH",
    "READONLY_SCOPES",
    "TIER_SCOPES",
    "V2_PRODUCT_ROLES",
    "ProductAccess",
    "ProductionAccessContract",
    "ResolvedClientAccess",
    "is_canonical_primary_endpoint",
    "validate_access_contract",
    "validate_access_contract_v3",
]
