"""Safe filesystem and JSON helpers for the bench-run explorer.

Extracted from ``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

def _safe_int(value: object, default: int = 0) -> int:
    """Coerce *value* to int, returning *default* for None, empty, or malformed."""
    import math
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if not math.isfinite(value):
                return default
            return int(value)
        except (OverflowError, ValueError, TypeError):
            return default
    try:
        return int(str(value))
    except (ValueError, TypeError, OverflowError):
        return default

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_component(component: str) -> bool:
    return bool(_SAFE_COMPONENT.match(component))


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if path.is_symlink():
        logger.warning("Rejecting symlinked artifact: %s", path)
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None


def _is_bare_real_directory(path: Path) -> bool:
    """Return True if path is a real directory (not a symlink) and exists."""
    return path.exists() and path.is_dir() and not path.is_symlink() and not path.resolve().is_symlink()


def _safe_resolve(root: Path, *parts: str) -> Path | None:
    """Resolve *parts* under *root*; return the resolved path.

    Rejects if any intermediate component is a symlink, if the result
    escapes *root*, or if the result does not exist.
    Returns None on rejection.
    """
    root_resolved = root.resolve()
    # Build the path one component at a time, rejecting symlinks at every step.
    current = root_resolved
    for part in parts:
        candidate = current / part
        if candidate.is_symlink():
            logger.warning("Symlink rejected at %s/%s", current, part)
            return None
        current = candidate
    final = current.resolve()
    if final.is_symlink():
        logger.warning("Final resolved path is a symlink: %s", final)
        return None
    try:
        final.relative_to(root_resolved)
    except ValueError:
        logger.warning("Path escapes root: %s", final)
        return None
    if not final.exists():
        return None
    return final


def _safe_resolve_file(root: Path, *parts: str) -> Path | None:
    """Like _safe_resolve but for files (does not require is_dir)."""
    root_resolved = root.resolve()
    current = root_resolved
    for part in parts:
        candidate = current / part
        if candidate.is_symlink():
            return None
        current = candidate
    final = current.resolve()
    if final.is_symlink():
        return None
    try:
        final.relative_to(root_resolved)
    except ValueError:
        return None
    if not final.exists():
        return None
    return final
