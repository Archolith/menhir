"""Path containment for filesystem-reading ingest operations.

Bug SEC-02 (Menhir Frontier Phase 11 audit): agent-tier ``ingest_document`` and
``scan_and_write_project`` accepted any caller-supplied path and read the file/scanned the
directory verbatim, so a scoped agent credential could read any host-readable file. This module
resolves the path (following symlinks) and requires it to sit under an allowlisted root unless the
caller is operator tier.

Policy (secure-by-default, operator bypass):
- ``operator`` tier: unrestricted (reserved for the trusted operator).
- Empty/``None`` tier: unrestricted — mirrors the rest of the auth model, where an empty tier
  means no API keys are configured (local dev) and enforcement is off.
- any other tier (``agent``/``readonly``): the resolved path must equal or sit under one of the
  allowed roots, otherwise ``IngestPathNotAllowedError`` is raised and nothing is read.

Allowed roots default to the service working-directory tree and can be extended (or replaced) via
the ``MENHIR_INGEST_ALLOWED_ROOTS`` environment variable (an ``os.pathsep``-delimited list).
"""

from __future__ import annotations

import os
from pathlib import Path

INGEST_ALLOWED_ROOTS_ENV = "MENHIR_INGEST_ALLOWED_ROOTS"


class IngestPathNotAllowedError(ValueError):
    """Raised when a non-operator caller supplies an ingest path outside the allowed roots."""


def allowed_ingest_roots() -> list[Path]:
    """Return the resolved roots under which non-operator ingest is permitted.

    ``MENHIR_INGEST_ALLOWED_ROOTS`` (os.pathsep-delimited) replaces the default when set; otherwise
    the service working-directory tree is the sole allowed root.
    """
    configured = os.getenv(INGEST_ALLOWED_ROOTS_ENV, "").strip()
    roots: list[Path] = []
    if configured:
        for part in configured.split(os.pathsep):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                roots.append(Path(candidate).resolve())
            except OSError:
                continue
    if not roots:
        roots.append(Path.cwd().resolve())
    return roots


def _is_within(resolved: Path, root: Path) -> bool:
    return resolved == root or root in resolved.parents


def ensure_ingest_path_allowed(path: str, *, tier: str | None) -> Path:
    """Resolve *path* (following symlinks) and enforce the containment policy.

    Returns the resolved absolute ``Path`` when permitted; raises
    ``IngestPathNotAllowedError`` when a non-operator caller supplies a path outside the allowed
    roots.
    """
    resolved = Path(path).resolve()
    if not tier or tier == "operator":
        return resolved
    roots = allowed_ingest_roots()
    if any(_is_within(resolved, root) for root in roots):
        return resolved
    raise IngestPathNotAllowedError(
        f"ingest path {resolved} is outside the allowed roots for tier '{tier}'; "
        f"set {INGEST_ALLOWED_ROOTS_ENV} or use an operator credential"
    )
