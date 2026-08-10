"""Shared helpers for backend runtime/client adapters."""

from __future__ import annotations

import threading
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from menhir.infrastructure.project_scanner import (
    CallEdge,
    CrossProjectRef,
    DirEntry,
    EndpointEntry,
    FileEntry,
    ImportEdge,
    NestedRepo,
    ProjectScanResult,
    SymbolEntry,
    TestEdge,
)

# ---------------------------------------------------------------------------
# Background-error surface — server side
# RuntimeProvider background tasks push errors here; routes.backend_invoke
# drains and attaches them to the next outbound HTTP response header.
# ---------------------------------------------------------------------------
_background_errors: dict[str, deque[str]] = {}
_background_errors_lock = threading.Lock()


def _push_background_error(scope_key: str, msg: str) -> None:
    with _background_errors_lock:
        bucket = _background_errors.get(scope_key)
        if bucket is None:
            bucket = deque(maxlen=10)
            _background_errors[scope_key] = bucket
        bucket.append(msg[:300])


def _drain_background_errors(scope_key: str) -> list[str]:
    with _background_errors_lock:
        bucket = _background_errors.pop(scope_key, None)
        if bucket is None:
            return []
        msgs = list(bucket)
        bucket.clear()
        return msgs


# ---------------------------------------------------------------------------
# Background-error surface — client side
# BackendClient._request receives errors via x-menhir-bg-warnings response
# header and pushes them here; BaseTool.execute drains and appends to output.
# ---------------------------------------------------------------------------
_client_warnings: ContextVar[deque[str] | None] = ContextVar(
    "menhir_client_warnings", default=None
)


def _push_client_warning(msg: str) -> None:
    warnings = _client_warnings.get()
    if warnings is None:
        warnings = deque(maxlen=10)
        _client_warnings.set(warnings)
    warnings.append(msg)


def drain_client_warnings() -> list[str]:
    warnings = _client_warnings.get()
    if warnings is None:
        return []
    msgs = list(warnings)
    warnings.clear()
    return msgs


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value).__module__.startswith("neo4j"):
        if hasattr(value, "iso_format"):
            return value.iso_format()
        return str(value)
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    return value


def _project_scan_from_dict(payload: dict[str, Any]) -> ProjectScanResult:
    return ProjectScanResult(
        name=str(payload.get("name") or ""),
        root_path=str(payload.get("root_path") or ""),
        stack=str(payload.get("stack") or ""),
        description=str(payload.get("description") or ""),
        directories=[DirEntry(**item) for item in payload.get("directories", []) or []],
        files=[FileEntry(**item) for item in payload.get("files", []) or []],
        dependencies=[str(item) for item in payload.get("dependencies", []) or []],
        imports=[ImportEdge(**item) for item in payload.get("imports", []) or []],
        test_edges=[TestEdge(**item) for item in payload.get("test_edges", []) or []],
        endpoints=[
            EndpointEntry(**item) for item in payload.get("endpoints", []) or []
        ],
        cross_project_refs=[
            CrossProjectRef(**item)
            for item in payload.get("cross_project_refs", []) or []
        ],
        scan_fingerprint=str(payload.get("scan_fingerprint") or ""),
        # Coverage counts must survive this boundary or `partial_index` is silently lost on
        # the remote path and consumers fall back to reporting absence as fact.
        files_discovered=int(payload.get("files_discovered") or 0),
        files_eligible=int(payload.get("files_eligible") or 0),
        files_indexed=int(payload.get("files_indexed") or 0),
        nested_repos=[NestedRepo(**item) for item in payload.get("nested_repos", []) or []],
        symbols=[SymbolEntry(**item) for item in payload.get("symbols", []) or []],
        truncated_symbol_files=[
            str(item) for item in payload.get("truncated_symbol_files", []) or []
        ],
        call_edges=[CallEdge(**item) for item in payload.get("call_edges", []) or []],
    )
