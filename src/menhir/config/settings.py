"""Compatibility shim for environment-backed settings.

Historically this module contained both helper functions and the full
MemorySettings dataclass implementation. It now re-exports the split helper
and model modules so existing imports stay stable.
"""

from __future__ import annotations

from .settings_helpers import (
    assert_bind_safe,
    is_loopback_host,
    parse_bool_env,
    parse_client_namespaces,
    parse_client_tools,
    parse_csv_env,
    validate_no_auth_bind_safety,
)
from .settings_model import MemorySettings

__all__ = [
    "MemorySettings",
    "assert_bind_safe",
    "is_loopback_host",
    "parse_bool_env",
    "parse_client_namespaces",
    "parse_client_tools",
    "parse_csv_env",
    "validate_no_auth_bind_safety",
]
