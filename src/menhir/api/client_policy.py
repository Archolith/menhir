"""Immutable OAuth client policy keyed by verified ``client_id``."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archolith_oauth import valid_redirect_uri

from menhir.access_contract import (
    TIER_SCOPES,
    ProductionAccessContract,
    ResolvedClientAccess,
    validate_access_contract,
    validate_access_contract_v3,
)
from menhir.tiers import TIER_RANK

logger = logging.getLogger(__name__)

_TIER_RANK = TIER_RANK
_PROTOCOL_SCOPES = frozenset({"offline_access"})
_MAX_REDIRECT_URIS = 5
_MAX_CLIENT_NAME_LEN = 255
_SUPPORTED_VERSIONS = frozenset({1, 2, 3})
_COMMON_CLIENT_KEYS = frozenset(
    {"label", "maximum_tier", "namespace", "registration", "scopes"}
)
_EXPLICIT_TOOL_KEYS = frozenset({"allowed_tools", "denied_tools"})
_PROFILE_TOOL_KEYS = frozenset({"profiles", "allow", "deny"})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in production client policy: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class OAuthClientRegistration:
    """Static public-client metadata owned by the digest-bound policy."""

    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    protocol_scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ClientPolicy:
    client_id: str
    label: str
    scopes: frozenset[str]
    maximum_tier: str
    namespace: str
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    registration: OAuthClientRegistration | None = None

    @property
    def oauth_scopes(self) -> frozenset[str]:
        """Exact OAuth grant: permissions plus reviewed protocol capabilities."""

        protocol_scopes = (
            frozenset()
            if self.registration is None
            else self.registration.protocol_scopes
        )
        return self.scopes | protocol_scopes


@dataclass(frozen=True)
class ClientPolicyAuthority:
    version: int
    digest: str
    clients: dict[str, ClientPolicy]
    access_contract: ProductionAccessContract | None = None

    def policy_for_client_id(self, client_id: str) -> ClientPolicy:
        """Resolve an immutable client identity or fail closed."""

        policy = self.clients.get(client_id)
        if policy is None:
            raise PermissionError("OAuth client_id is not present in production policy")
        return policy

    def require_authorization(
        self,
        *,
        client_id: str,
        scopes: frozenset[str],
    ) -> ClientPolicy:
        """Authorize an OAuth grant before any authority state is mutated."""

        policy = self.policy_for_client_id(client_id)
        if scopes != policy.oauth_scopes:
            raise PermissionError(
                "OAuth authorization scopes do not match production client policy"
            )
        return policy

    def require_client(
        self,
        *,
        client_id: str,
        scopes: frozenset[str],
        tier: str,
    ) -> ClientPolicy:
        policy = self.policy_for_client_id(client_id)
        if scopes != policy.oauth_scopes:
            raise PermissionError(
                "OAuth token scopes do not match production client policy"
            )
        if _TIER_RANK.get(tier, -1) > _TIER_RANK[policy.maximum_tier]:
            raise PermissionError("OAuth token tier exceeds production client policy")
        return policy


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    canonical = dict(payload)
    canonical.pop("canonical_digest", None)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _parse_registration(raw_registration: object) -> OAuthClientRegistration | None:
    if raw_registration is None:
        return None
    if not isinstance(raw_registration, dict):
        raise ValueError("production client policy registration must be an object")
    allowed_registration_keys = {
        "client_name",
        "protocol_scopes",
        "redirect_uris",
        "token_endpoint_auth_method",
    }
    if set(raw_registration) - allowed_registration_keys:
        raise ValueError("production client policy registration has unknown fields")
    client_name = raw_registration.get("client_name")
    redirect_uris_raw = raw_registration.get("redirect_uris")
    protocol_scopes_raw = raw_registration.get("protocol_scopes", [])
    token_endpoint_auth_method = raw_registration.get(
        "token_endpoint_auth_method", "none"
    )
    if (
        not isinstance(client_name, str)
        or not client_name.strip()
        or len(client_name.strip()) > _MAX_CLIENT_NAME_LEN
        or not isinstance(redirect_uris_raw, list)
        or not 1 <= len(redirect_uris_raw) <= _MAX_REDIRECT_URIS
        or any(not isinstance(uri, str) for uri in redirect_uris_raw)
        or len(set(redirect_uris_raw)) != len(redirect_uris_raw)
        or any(not valid_redirect_uri(uri) for uri in redirect_uris_raw)
        or not isinstance(protocol_scopes_raw, list)
        or any(not isinstance(scope, str) for scope in protocol_scopes_raw)
        or len(set(protocol_scopes_raw)) != len(protocol_scopes_raw)
        or not set(protocol_scopes_raw).issubset(_PROTOCOL_SCOPES)
        or token_endpoint_auth_method != "none"
    ):
        raise ValueError(
            "production client policy has an invalid public-client registration"
        )
    return OAuthClientRegistration(
        client_name=client_name.strip(),
        redirect_uris=tuple(redirect_uris_raw),
        token_endpoint_auth_method="none",
        protocol_scopes=frozenset(protocol_scopes_raw),
    )


def _list_field(raw: dict[str, Any], key: str, *, required: bool = False) -> list[Any]:
    if key not in raw:
        if required:
            raise ValueError(f"production client policy client is missing {key}")
        return []
    value = raw[key]
    if not isinstance(value, list):
        raise ValueError(f"production client policy {key} must be a list")
    return value


def _resolve_profile_tools(
    client_id: str,
    raw: dict[str, Any],
    *,
    maximum_tier: str,
    tool_tiers: Mapping[str, str],
    policy_catalog: int,
) -> tuple[frozenset[str], frozenset[str]]:
    from menhir.mcp.tool_profiles import resolve

    try:
        resolved = resolve(
            profiles=_list_field(raw, "profiles", required=True),
            allow=_list_field(raw, "allow"),
            deny=_list_field(raw, "deny"),
            maximum_tier=maximum_tier,
            tool_tiers=tool_tiers,
            policy_catalog=policy_catalog,
        )
    except ValueError as exc:
        raise ValueError(
            f"production client policy tool profiles are invalid for a client: {exc}"
        ) from exc
    label = str(raw.get("label", "")).strip().lower() or client_id
    if resolved.above_tier:
        logger.info(
            "client policy: %s (%s tier) does not receive profile tools above its tier: %s",
            label,
            maximum_tier,
            sorted(resolved.above_tier),
        )
    if resolved.withheld:
        logger.warning(
            "client policy: %s is pinned to profile catalog %d and does not receive "
            "newer profile tools: %s",
            label,
            policy_catalog,
            sorted(resolved.withheld),
        )
    allowed = resolved.allowed
    return allowed, frozenset(tool_tiers) - allowed


def load_client_policy(
    path: str,
    expected_digest: str,
    *,
    tool_catalog: frozenset[str] | None = None,
    tool_tiers: Mapping[str, str] | None = None,
) -> ClientPolicyAuthority:
    """Load and verify one versioned client-policy artifact.

    Version 3 composes each client's allowlist from named tool profiles; the
    result is the same ``allowed_tools``/``denied_tools`` pair that versions 1
    and 2 spell out, so runtime enforcement does not change. ``tool_tiers``
    defaults to the registered production tools.
    """

    policy_path = Path(path)
    if not policy_path.is_absolute() or not policy_path.is_file():
        raise ValueError(
            "production client policy path must be an existing absolute file"
        )
    try:
        payload = json.loads(
            policy_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("production client policy is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or isinstance(payload.get("version"), bool)
        or payload.get("version") not in _SUPPORTED_VERSIONS
    ):
        raise ValueError(
            "production client policy must use supported version 1, 2 or 3"
        )
    version = int(payload["version"])
    expected_top_keys = {"version", "canonical_digest", "clients"}
    if version >= 2:
        expected_top_keys.add("access_contract")
    if version == 3:
        expected_top_keys.add("profile_catalog")
    if set(payload) != expected_top_keys:
        raise ValueError("production client policy has invalid top-level fields")
    digest = hashlib.sha256(_canonical_payload(payload)).hexdigest()
    declared_digest = str(payload.get("canonical_digest", ""))
    if not expected_digest or digest != expected_digest or digest != declared_digest:
        raise ValueError(
            "production client policy digest does not match its configured authority"
        )

    raw_clients = payload.get("clients")
    if not isinstance(raw_clients, dict) or not raw_clients:
        raise ValueError("production client policy must contain at least one client")

    access_contract = None
    if version == 2:
        access_contract = validate_access_contract(
            payload.get("access_contract"),
            raw_clients,
            tool_catalog=tool_catalog,
        )

    resolved_tiers: Mapping[str, str] = {}
    policy_catalog = 0
    if version == 3:
        from menhir.mcp.tool_profiles import production_tool_tiers

        resolved_tiers = tool_tiers if tool_tiers is not None else production_tool_tiers()
        if tool_catalog is not None and frozenset(resolved_tiers) != tool_catalog:
            raise ValueError(
                "production client policy tool tiers disagree with the runtime catalog"
            )
        policy_catalog = payload.get("profile_catalog")
        if isinstance(policy_catalog, bool) or not isinstance(policy_catalog, int):
            raise ValueError("production client policy profile_catalog must be an integer")

    tool_keys = _PROFILE_TOOL_KEYS if version == 3 else _EXPLICIT_TOOL_KEYS
    allowed_client_keys = _COMMON_CLIENT_KEYS | tool_keys
    clients: dict[str, ClientPolicy] = {}
    labels: set[str] = set()
    for client_id, raw in raw_clients.items():
        if not isinstance(client_id, str) or not client_id or not isinstance(raw, dict):
            raise ValueError("production client policy has an invalid client entry")
        if set(raw) - allowed_client_keys:
            raise ValueError("production client policy has unknown client fields")
        label = str(raw.get("label", "")).strip().lower()
        scopes = frozenset(str(value) for value in raw.get("scopes", ()))
        maximum_tier = str(raw.get("maximum_tier", ""))
        namespace = str(raw.get("namespace", ""))
        registration = _parse_registration(raw.get("registration"))
        if version == 3:
            if maximum_tier not in _TIER_RANK:
                raise ValueError(
                    "production client policy has an incomplete or duplicate entry"
                )
            # A v3 client's scopes are exactly its tier's: anything else either
            # mints tokens the tier cap refuses or caps tools the profiles grant.
            if scopes != TIER_SCOPES[maximum_tier]:
                raise ValueError(
                    f"production client policy scopes must equal the {maximum_tier} "
                    f"tier scopes {sorted(TIER_SCOPES[maximum_tier])}"
                )
            allowed_tools, denied_tools = _resolve_profile_tools(
                client_id,
                raw,
                maximum_tier=maximum_tier,
                tool_tiers=resolved_tiers,
                policy_catalog=policy_catalog,
            )
        else:
            allowed_tools = frozenset(str(value) for value in raw.get("allowed_tools", ()))
            denied_tools = frozenset(str(value) for value in raw.get("denied_tools", ()))
        if (
            not label
            or label in labels
            or not scopes
            or bool(scopes & _PROTOCOL_SCOPES)
            or maximum_tier not in _TIER_RANK
            or not allowed_tools
            or (version != 3 and not denied_tools)
            or bool(allowed_tools & denied_tools)
            or any(not value for value in scopes | allowed_tools | denied_tools)
        ):
            raise ValueError(
                "production client policy has an incomplete or duplicate entry"
            )
        labels.add(label)
        clients[client_id] = ClientPolicy(
            client_id=client_id,
            label=label,
            scopes=scopes,
            maximum_tier=maximum_tier,
            namespace=namespace,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            registration=registration,
        )

    if version == 3:
        from menhir.mcp.tool_profiles import ADMIN, PROFILES

        access_contract = validate_access_contract_v3(
            payload.get("access_contract"),
            {
                client_id: ResolvedClientAccess(
                    maximum_tier=policy.maximum_tier,
                    scopes=policy.scopes,
                    allowed_tools=policy.allowed_tools,
                )
                for client_id, policy in clients.items()
            },
            forbidden_product_tools=PROFILES[ADMIN],
        )

    if tool_catalog is not None:
        for policy in clients.values():
            decisions = policy.allowed_tools | policy.denied_tools
            if decisions != tool_catalog:
                missing = sorted(tool_catalog - decisions)
                unknown = sorted(decisions - tool_catalog)
                raise ValueError(
                    "production client policy tool census disagrees with the runtime "
                    f"catalog (missing={missing}, unknown={unknown})"
                )

    return ClientPolicyAuthority(
        version=version,
        digest=digest,
        clients=clients,
        access_contract=access_contract,
    )


__all__ = [
    "ClientPolicy",
    "ClientPolicyAuthority",
    "OAuthClientRegistration",
    "load_client_policy",
]
