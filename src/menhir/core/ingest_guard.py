"""Path containment for filesystem-reading ingest operations.

Bug SEC-02 (Menhir Frontier Phase 11 audit): agent-tier ``ingest_document`` and
``scan_and_write_project`` accepted any caller-supplied path and read the file/scanned the
directory verbatim, so a scoped agent credential could read any host-readable file. This module
resolves the path (following symlinks) and requires it to sit under an allowlisted root unless the
caller is operator tier.

Policy (secure-by-default, operator bypass):
- ``operator`` tier: unrestricted root-wise, but the denied-path patterns below still apply —
  an operator credential is trusted with reach, not with secrets-ingest.
- Empty/``None`` tier: unrestricted — mirrors the rest of the auth model, where an empty tier
  means no API keys are configured (local dev) and enforcement is off.
- any other tier (``agent``/``readonly``): the resolved path must equal or sit under one of the
  roots configured via ``MENHIR_INGEST_ALLOWED_ROOTS`` (an ``os.pathsep``-delimited list).
  There is NO default root (#83): falling back to the service working directory silently
  exposed the server's own tree (config, logs, ``.env``) to agent credentials. When the
  variable is unset, every non-operator ingest is refused with the setup message.

Denied by name (#83, second layer): dotfiles and dot-directories (covers ``.env*`` and
``.git``) and anything under a ``logs/`` or ``backups/`` directory. For confined tiers the
check runs on the components below the matched root; for the operator/empty tier, which has
no configured anchor, it runs on the ingested artifact itself — its filename and immediate
parent — so a checkout that merely lives under a dot-directory stays ingestable.
"""

from __future__ import annotations

import os
from pathlib import Path

INGEST_ALLOWED_ROOTS_ENV = "MENHIR_INGEST_ALLOWED_ROOTS"

_DENIED_DIR_NAMES = frozenset({"logs", "backups"})

_DENIED_MESSAGE = (
    "ingest path {resolved} matches a denied path pattern "
    "(dotfiles/dot-directories, logs/, backups/); these are refused for every tier"
)


class IngestPathNotAllowedError(ValueError):
    """Raised when a non-operator caller supplies an ingest path outside the allowed roots."""


def allowed_ingest_roots() -> list[Path]:
    """Return the configured roots under which non-operator ingest is permitted.

    ``MENHIR_INGEST_ALLOWED_ROOTS`` (os.pathsep-delimited) is the only source — no default
    root (#83). An empty list means every non-operator ingest is refused.
    """
    configured = os.getenv(INGEST_ALLOWED_ROOTS_ENV, "").strip()
    roots: list[Path] = []
    for part in configured.split(os.pathsep):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).resolve())
        except OSError:
            continue
    return roots


def _is_within(resolved: Path, root: Path) -> bool:
    return resolved == root or root in resolved.parents


def _has_denied_component(parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part.lower() in _DENIED_DIR_NAMES for part in parts)


def ensure_ingest_path_allowed(path: str, *, tier: str | None) -> Path:
    """Resolve *path* (following symlinks) and enforce the containment policy.

    Returns the resolved absolute ``Path`` when permitted; raises
    ``IngestPathNotAllowedError`` when the path is denied by name or, for a non-operator
    caller, sits outside the configured allowed roots.
    """
    resolved = Path(path).resolve()
    if not tier or tier == "operator":
        if _has_denied_component(resolved.parts[-2:]):
            raise IngestPathNotAllowedError(_DENIED_MESSAGE.format(resolved=resolved))
        return resolved
    roots = allowed_ingest_roots()
    for root in roots:
        if not _is_within(resolved, root):
            continue
        if resolved != root and _has_denied_component(resolved.relative_to(root).parts):
            raise IngestPathNotAllowedError(_DENIED_MESSAGE.format(resolved=resolved))
        return resolved
    raise IngestPathNotAllowedError(
        f"ingest path {resolved} is outside the allowed roots for tier '{tier}'; "
        f"set {INGEST_ALLOWED_ROOTS_ENV} or use an operator credential"
    )
