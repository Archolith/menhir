"""The per-project identity file: ``.agent/project-id``.

CF-257 phase 1. Project identity is currently a directory basename, so two directories sharing one
are the same project to the graph. A minted random id fixes that without depending on paths, which
are not unique across machines -- two hosts can carry the same folder layout.

**Ignoring is a PRECONDITION, not a side effect.** Measured on this workspace: 0 of 138 repos with
an ``.agent/`` would ignore this file today, and 29 repos have no ``.agent/`` at all. Minting
without fixing that drops an untracked file into 138 repositories; in a workspace whose convention
is explicit-path staging, one eventually gets committed by accident -- which silently converts the
design to the committed variant, where a fork inherits its parent's identity. That is the one case
this whole scheme exists to separate, so the ignore rule is written and verified before any id is.

**The ignore check does not shell out to git.** ``git check-ignore`` fails on this workspace for
the same two reasons ``git rev-parse`` does -- dubious ownership under the service user, and
worktrees whose gitdir is gone. Menhir writes the rule into the repo's own ``.agent/.gitignore``
and verifies that file, which needs no ownership check and cannot be defeated by a broken checkout.

**Publication is ``O_CREAT|O_EXCL`` on the destination, never a rename.** Measured on this
platform: a second ``O_EXCL`` create raises ``FileExistsError`` while ``os.replace`` over an
existing file SUCCEEDS. Write-to-temp-then-rename therefore does not give "create if absent" -- two
scanners racing would each publish, and the last rename would win, leaving two ids for one
directory and no trace that it happened.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProjectIdFile",
    "ProjectIdFileError",
    "IdentityFileNotIgnored",
    "MalformedIdentityFile",
    "IDENTITY_DIR",
    "IDENTITY_FILENAME",
    "SCHEMA_VERSION",
    "identity_path",
    "read_identity",
    "mint_identity",
    "ensure_ignore_rule",
]

IDENTITY_DIR = ".agent"
IDENTITY_FILENAME = "project-id"
SCHEMA_VERSION = 1

#: What is written into the repo's own `.agent/.gitignore`.
_IGNORE_LINE = "project-id"


class ProjectIdFileError(RuntimeError):
    """Base for identity-file problems."""


class IdentityFileNotIgnored(ProjectIdFileError):
    """The ignore rule is not in place, so minting would leave a trackable file."""


class MalformedIdentityFile(ProjectIdFileError):
    """The file exists but cannot be read as an identity.

    Never repaired by overwriting: a corrupt file may still be the only record of an id whose
    project holds thousands of entities, and re-minting would orphan them silently.
    """


@dataclass(frozen=True)
class ProjectIdFile:
    project_id: str
    display_name: str
    namespace: str | None
    path: Path


def identity_path(root: str | Path) -> Path:
    return Path(root) / IDENTITY_DIR / IDENTITY_FILENAME


def _gitignore_path(root: str | Path) -> Path:
    return Path(root) / IDENTITY_DIR / ".gitignore"


def ensure_ignore_rule(root: str | Path) -> bool:
    """Make sure ``.agent/.gitignore`` ignores the identity file. Returns True if it changed.

    Idempotent, and deliberately additive: menhir appends its rule rather than rewriting the file,
    because `.agent/.gitignore` is the repo's, not menhir's -- this one already carries
    ``test_tmp/``, ``mcp_telemetry.db`` and ``*.log`` here.
    """
    gi = _gitignore_path(root)
    gi.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if gi.exists():
        existing = gi.read_text(encoding="utf-8", errors="replace")
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped == _IGNORE_LINE or stripped == f"/{_IGNORE_LINE}":
                return False
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    gi.write_text(
        f"{existing}{prefix}# menhir project identity (CF-257): per-checkout, never committed\n"
        f"{_IGNORE_LINE}\n",
        encoding="utf-8",
    )
    return True


def is_ignore_rule_present(root: str | Path) -> bool:
    gi = _gitignore_path(root)
    if not gi.exists():
        return False
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip() in (_IGNORE_LINE, f"/{_IGNORE_LINE}"):
            return True
    return False


def read_identity(root: str | Path) -> ProjectIdFile | None:
    """Return the identity recorded for *root*, or None if there is no file.

    Raises :class:`MalformedIdentityFile` when a file exists but is unusable -- including a
    partially-written one, which is how a reader that catches a concurrent mint mid-write lands
    here rather than on a wrong id.
    """
    path = identity_path(root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MalformedIdentityFile(
            f"{path} exists but could not be read as JSON ({exc}). Refusing to overwrite it: it "
            "may be the only record of this project's identity. Repair or remove it deliberately."
        ) from exc
    if not isinstance(raw, dict) or not str(raw.get("project_id") or "").strip():
        raise MalformedIdentityFile(
            f"{path} carries no usable project_id. Refusing to overwrite it."
        )
    return ProjectIdFile(
        project_id=str(raw["project_id"]).strip(),
        display_name=str(raw.get("display_name") or Path(root).name),
        namespace=(str(raw["namespace"]).strip() or None) if raw.get("namespace") else None,
        path=path,
    )


def mint_identity(
    root: str | Path,
    *,
    project_id: str | None = None,
    display_name: str | None = None,
    namespace: str | None = None,
) -> ProjectIdFile:
    """Create the identity file for *root*, or raise if one already exists.

    ``project_id`` is supplied when ADOPTING an id the graph already holds; omitted it mints a
    fresh one. Both go through the same exclusive create, so an adopt cannot silently clobber a
    file that appeared in the meantime.
    """
    root_path = Path(root)
    if not is_ignore_rule_present(root_path):
        raise IdentityFileNotIgnored(
            f"{_gitignore_path(root_path)} does not ignore {IDENTITY_FILENAME!r}. Refusing to "
            "mint: an untracked identity file eventually gets committed, and a committed id is "
            "inherited by every clone and fork -- the case this identity scheme exists to "
            "separate. Call ensure_ignore_rule() first."
        )

    path = identity_path(root_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA_VERSION,
        "project_id": project_id or str(uuid.uuid4()),
        "display_name": display_name or root_path.name,
    }
    if namespace:
        payload["namespace"] = namespace

    # O_CREAT|O_EXCL on the DESTINATION. Not a temp file plus rename: rename replaces whatever is
    # at the target, so two concurrent scanners would both "succeed" and the loser's id would
    # vanish without an error. Here the loser gets FileExistsError and re-reads the winner's file.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ProjectIdFileError(
            f"Could not create {path}: {exc}. A read-only checkout cannot mint an identity; scan "
            "it from a writable copy, or adopt an existing id."
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        # A half-written file reads as malformed, which REFUSES rather than re-mints, so removing
        # it here is a courtesy and its failure is not important.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    return ProjectIdFile(
        project_id=payload["project_id"],
        display_name=payload["display_name"],
        namespace=namespace,
        path=path,
    )
