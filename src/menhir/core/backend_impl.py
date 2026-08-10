"""Compatibility shim for backend adapters.

Historically this module held the full RuntimeProvider and BackendClient
implementations. It now re-exports the split modules so existing imports stay
stable while the oversized implementation is decomposed.
"""

from __future__ import annotations

from .backend_client import BackendClient
from .backend_runtime import RuntimeProvider
from .backend_shared import (
    _drain_background_errors,
    _project_scan_from_dict,
    _push_background_error,
    _push_client_warning,
    _to_jsonable,
    drain_client_warnings,
)

__all__ = [
    'BackendClient',
    'RuntimeProvider',
    '_drain_background_errors',
    '_project_scan_from_dict',
    '_push_background_error',
    '_push_client_warning',
    '_to_jsonable',
    'drain_client_warnings',
]
