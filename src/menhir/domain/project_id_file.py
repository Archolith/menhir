"""Reading the legacy per-project identity file: ``.agent/project-id``. Never written.

CF-257 phase 1 minted a random id into each checkout. Identity now lives only in the graph
binding (:mod:`menhir.infrastructure.project_identity_binding`); Menhir writes nothing into a
checkout. This module survives only to READ the files earlier versions left behind:

- a legacy binding that recorded no repository is verified once by the file naming its id, and
  then records the repository so the file is never consulted again;
- a moved checkout's file offers its id as a candidate for an operator's adopt decision.

The file is never created, changed or deleted here. Users may delete legacy files at any time;
they were always git-ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProjectIdFile",
    "ProjectIdFileError",
    "MalformedIdentityFile",
    "IDENTITY_DIR",
    "IDENTITY_FILENAME",
    "identity_path",
    "read_identity",
]

IDENTITY_DIR = ".agent"
IDENTITY_FILENAME = "project-id"


class ProjectIdFileError(RuntimeError):
    """Base for identity-file problems."""


class MalformedIdentityFile(ProjectIdFileError):
    """The file exists but cannot be read as an identity."""


@dataclass(frozen=True)
class ProjectIdFile:
    project_id: str
    display_name: str
    namespace: str | None
    path: Path


def identity_path(root: str | Path) -> Path:
    return Path(root) / IDENTITY_DIR / IDENTITY_FILENAME


def read_identity(root: str | Path) -> ProjectIdFile | None:
    """Return the identity a legacy file records for *root*, or None if there is no file.

    Raises :class:`MalformedIdentityFile` when a file exists but is unusable.
    """
    path = identity_path(root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MalformedIdentityFile(f"{path} exists but could not be read as JSON ({exc}).") from exc
    if not isinstance(raw, dict) or not str(raw.get("project_id") or "").strip():
        raise MalformedIdentityFile(f"{path} carries no usable project_id.")
    return ProjectIdFile(
        project_id=str(raw["project_id"]).strip(),
        display_name=str(raw.get("display_name") or Path(root).name),
        namespace=(str(raw["namespace"]).strip() or None) if raw.get("namespace") else None,
        path=path,
    )
