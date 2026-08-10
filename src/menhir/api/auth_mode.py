"""Compatibility exports for config-owned authentication-mode resolution."""

from menhir.config.auth_mode import (
    AuthMode,
    auth_mode_from,
    resolve_auth_mode,
    static_keys_present,
)

__all__ = [
    "AuthMode",
    "auth_mode_from",
    "resolve_auth_mode",
    "static_keys_present",
]
