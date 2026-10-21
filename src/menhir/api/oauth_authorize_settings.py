"""Settings/policy helpers for the embedded OAuth AS authorization endpoint.

Extracted verbatim from :mod:`menhir.api.oauth_authorize` (behavior-preserving
split); the facade re-exports these names so existing imports keep working.
"""

from __future__ import annotations

from fastapi import Request

from menhir.api.client_policy import ClientPolicy, ClientPolicyAuthority
from menhir.config import MemorySettings
from menhir.config.oauth import _get_setting


def _settings_for(request: Request) -> object:
    return getattr(request.app.state, "settings", None) or MemorySettings.from_env()


def _production_client_policy(
    request: Request,
    *,
    client_id: str,
    scopes: frozenset[str] | None = None,
) -> ClientPolicy | None:
    """Resolve the production policy before an OAuth authority mutation.

    Non-production/dev applications do not install a policy authority and retain
    their existing OAuth behavior. A production application always installs one.
    """

    authority = getattr(request.app.state, "client_policy", None)
    if authority is None:
        return None
    if not isinstance(authority, ClientPolicyAuthority):
        raise PermissionError("Production client policy authority is invalid")
    if scopes is None:
        return authority.policy_for_client_id(client_id)
    return authority.require_authorization(client_id=client_id, scopes=scopes)


def _operator_key(settings: object) -> str:
    return str(_get_setting(settings, "operator_key", "MENHIR_OPERATOR_KEY", "")).strip()
