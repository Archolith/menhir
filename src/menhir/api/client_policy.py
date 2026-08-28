"""Immutable OAuth client policy keyed by verified ``client_id``."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TIER_RANK = {"readonly": 0, "agent": 1, "operator": 2}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in production client policy: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class ClientPolicy:
    client_id: str
    label: str
    scopes: frozenset[str]
    maximum_tier: str
    namespace: str
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    consent_group: str = ""


@dataclass(frozen=True)
class ClientPolicyAuthority:
    version: int
    digest: str
    clients: dict[str, ClientPolicy]

    def consent_group_clients(self, client_id: str) -> tuple[str, ...]:
        """Return the exact policy-bound suite approved with *client_id*.

        Empty groups remain client-scoped. Group membership is only sourced
        from this digest-bound authority, never from dynamic registrations.
        """

        policy = self.policy_for_client_id(client_id)
        if not policy.consent_group:
            return (client_id,)
        return tuple(
            sorted(
                candidate.client_id
                for candidate in self.clients.values()
                if candidate.consent_group == policy.consent_group
            )
        )

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
        if scopes != policy.scopes:
            raise PermissionError(
                "OAuth authorization scopes do not match production client policy"
            )
        if any(scope.endswith(":admin") for scope in scopes):
            raise PermissionError("OAuth admin scope is forbidden by production policy")
        return policy

    def require_client(
        self,
        *,
        client_id: str,
        scopes: frozenset[str],
        tier: str,
    ) -> ClientPolicy:
        policy = self.policy_for_client_id(client_id)
        if scopes != policy.scopes:
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


def load_client_policy(
    path: str,
    expected_digest: str,
    *,
    tool_catalog: frozenset[str] | None = None,
) -> ClientPolicyAuthority:
    """Load and verify one versioned client-policy artifact."""

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
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("production client policy must use supported version 1")
    digest = hashlib.sha256(_canonical_payload(payload)).hexdigest()
    declared_digest = str(payload.get("canonical_digest", ""))
    if not expected_digest or digest != expected_digest or digest != declared_digest:
        raise ValueError(
            "production client policy digest does not match its configured authority"
        )

    raw_clients = payload.get("clients")
    if not isinstance(raw_clients, dict) or not raw_clients:
        raise ValueError("production client policy must contain at least one client")

    clients: dict[str, ClientPolicy] = {}
    labels: set[str] = set()
    for client_id, raw in raw_clients.items():
        if not isinstance(client_id, str) or not client_id or not isinstance(raw, dict):
            raise ValueError("production client policy has an invalid client entry")
        label = str(raw.get("label", "")).strip().lower()
        scopes = frozenset(str(value) for value in raw.get("scopes", ()))
        maximum_tier = str(raw.get("maximum_tier", ""))
        namespace = str(raw.get("namespace", ""))
        allowed_tools = frozenset(str(value) for value in raw.get("allowed_tools", ()))
        denied_tools = frozenset(str(value) for value in raw.get("denied_tools", ()))
        consent_group_raw = raw.get("consent_group", "")
        consent_group = (
            consent_group_raw.strip().lower()
            if isinstance(consent_group_raw, str)
            else ""
        )
        if (
            not label
            or label in labels
            or not scopes
            or maximum_tier not in _TIER_RANK
            or not allowed_tools
            or not denied_tools
            or bool(allowed_tools & denied_tools)
            or any(not value for value in scopes | allowed_tools | denied_tools)
            or (
                "consent_group" in raw
                and (
                    not consent_group
                    or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", consent_group)
                    is None
                )
            )
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
            consent_group=consent_group,
        )

    consent_group_authority: dict[str, tuple[object, ...]] = {}
    for policy in clients.values():
        if not policy.consent_group:
            continue
        signature = (
            policy.scopes,
            policy.maximum_tier,
            policy.namespace,
            policy.allowed_tools,
            policy.denied_tools,
        )
        existing = consent_group_authority.setdefault(policy.consent_group, signature)
        if existing != signature:
            raise ValueError(
                "production consent-group clients must have identical authority"
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

    return ClientPolicyAuthority(version=1, digest=digest, clients=clients)


__all__ = ["ClientPolicy", "ClientPolicyAuthority", "load_client_policy"]
