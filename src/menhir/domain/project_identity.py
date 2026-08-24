"""Whether a scan root is entitled to write under a project's identity.

Phase 0 of the project-identity plan (CF-257). Project identity is a directory basename
(``project_name = name or root.name``), so two directories sharing a basename are one project as
far as the graph is concerned. The writers are ``MERGE ... SET`` and the fingerprint is looked up
by the same name, so a scan from the wrong copy overwrites the canonical rows *and* runs the
per-project stale prune, deleting the files that copy does not have.

Measured on the workspace: 167 git repos, 7 basenames claimed by more than one of them, three of
those collisions being worktrees that keep the repo's own basename as their inner directory, and
one a fork (``projects/forked/yawn.frontend`` against ``projects/yawn/yawn.frontend``).

**Two refusals, because one does not cover the other.**

* A *secondary checkout* (worktree, submodule) is refused on filesystem shape alone -- see
  :mod:`menhir.infrastructure.repo_topology`. This needs no graph state and catches a first-ever
  scan.
* A *fork* is an independent clone, so it passes every git check. It is caught only by noticing
  that the project already records a different ``root_path``. This needs graph state and cannot
  catch a first-ever scan of a name nothing has claimed -- which is correct: the first claimant
  is not the problem.

The pair is deliberate. Dropping either one leaves half the measured population unguarded.

This module is pure: it decides from values and raises. Reading the stored root path and
classifying the directory happen at the call site, so both are trivially fake-able in tests.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath

from menhir.infrastructure.repo_topology import RootKind, RootTopology

__all__ = [
    "ProjectIdentityRefused",
    "OPERATOR_TIER",
    "ensure_scan_root_owns_identity",
]

#: The tier permitted to override. Overriding deliberately writes across an identity boundary,
#: which is not an agent-tier decision -- it is the same reasoning that puts the ingest-path
#: allowlist bypass at operator tier in `core/ingest_guard.py`.
OPERATOR_TIER = "operator"


class ProjectIdentityRefused(ValueError):
    """Raised when a scan root may not write under the project identity it is claiming."""


def _looks_like_windows_path(value: str) -> bool:
    """True for a drive-letter or UNC path, which are case-insensitive in practice."""
    stripped = value.strip()
    if stripped.startswith("\\\\") or stripped.startswith("//"):
        return True
    return len(stripped) >= 2 and stripped[1] == ":" and stripped[0].isalpha()


def _normalized(value: str) -> str:
    """Canonical spelling for comparison, respecting the path flavor's case rules.

    Separator- and trailing-slash-insensitive for both flavors. **Case is folded only for Windows
    paths.** Folding it for POSIX was a defect: ``/srv/Foo`` and ``/srv/foo`` are different
    directories on Linux, and treating them as equal makes the guard ADMIT the second under the
    first's identity -- a false allow, which is the direction that loses data. A false refusal
    merely annoys someone; a false allow prunes a project's files.
    """
    raw = value.strip().rstrip("/\\")
    if _looks_like_windows_path(raw):
        return PureWindowsPath(raw.replace("/", "\\")).as_posix().casefold()
    return PurePosixPath(raw.replace("\\", "/")).as_posix()


def _same_path(left: str | None, right: str | None) -> bool:
    """Compare two recorded paths for practical equality.

    ``os.path.samefile`` first when both exist locally: it resolves symlinks, junctions and
    Windows 8.3 short names, so it answers "the same directory" rather than "the same spelling",
    and it consults the real filesystem's own case rules instead of inferring them. It needs both
    paths to exist, which a recorded root_path from another host will not, so string comparison
    remains the fallback rather than the primary.
    """
    if not left or not right:
        return False
    try:
        if os.path.exists(left) and os.path.exists(right):
            return os.path.samefile(left, right)
    except OSError:
        pass  # unreadable or cross-device; fall through to the textual comparison
    return _normalized(left) == _normalized(right)


def ensure_scan_root_owns_identity(
    *,
    topology: RootTopology,
    project_name: str,
    recorded_root_path: str | None,
    tier: str | None,
    force: bool = False,
    allow_unobservable_root: bool = False,
) -> None:
    """Raise :class:`ProjectIdentityRefused` unless this root may write as *project_name*.

    Args:
        topology: Filesystem classification of the scan root.
        project_name: The identity the scan intends to write under.
        recorded_root_path: ``root_path`` already stored for that project, or None if the project
            is unknown to the graph.
        tier: The caller's request tier. ``None`` means no auth is configured (local dev), which
            the rest of the auth model treats as unrestricted.
        force: The caller passed the explicit override.
        allow_unobservable_root: Accept a root that is not present on this host. Set ONLY by the
            compatibility write path, where a remote client scanned on its own machine and
            shipped the result. The shape refusal is unavailable there by construction; the
            recorded-root refusal below still applies and is what catches a fork.

    The override is checked FIRST for the two refusals but never suppresses
    :attr:`RootKind.UNRESOLVABLE`. Forcing means "I know this is a second checkout and I want it
    anyway", which is a coherent intention; there is no coherent version of "I know this pointer
    is broken and I want it anyway", because nothing downstream knows what identity was meant.
    """
    if topology.kind is RootKind.MISSING and not allow_unobservable_root:
        raise ProjectIdentityRefused(
            f"Cannot identify the scan root {topology.root}: {topology.detail}. "
            "A root this process cannot see cannot be checked for being a worktree or a "
            "submodule, so admitting it would make the shape refusal optional."
        )

    if topology.kind is RootKind.UNRESOLVABLE:
        raise ProjectIdentityRefused(
            f"Cannot identify the scan root {topology.root}: {topology.detail}. "
            "Refusing rather than assuming it is an independent clone -- an unrecognised checkout "
            "may write into another project's structure. This is not overridable; repair the "
            "checkout or scan the repository it points at."
        )

    may_force = bool(force) and (not tier or tier == OPERATOR_TIER)

    # Checked BEFORE the refusals it would have suppressed. Ordering it after them meant an
    # agent-tier caller who passed the override got the generic refusal, whose remedy is "pass the
    # operator-tier override" -- advice they had just followed and been denied for. The refusal was
    # right and the diagnostic sent them in a circle.
    if force and not may_force:
        raise ProjectIdentityRefused(
            f"The identity override requires {OPERATOR_TIER} tier; this request is {tier!r}."
        )

    if topology.is_secondary_checkout and not may_force:
        if topology.kind is RootKind.WORKTREE:
            primary = topology.primary_worktree or "<unresolved>"
            detail = (
                f"It is a worktree; the primary checkout at {primary} holds this repository's "
                "identity. Scanning here would overwrite that project's structure and prune the "
                "files this branch does not have."
            )
        else:
            detail = (
                "It is a submodule of a superproject, so it has no alternate primary checkout. "
                "Ingest it directly as its own project instead of through the parent."
            )
        raise ProjectIdentityRefused(
            f"Refusing to scan {topology.root} as project {project_name!r}. {detail} "
            "Pass the operator-tier override to scan it anyway."
        )

    # Fork case: an independent clone whose basename collides with a project already recorded
    # elsewhere. Only reachable once the graph has claimed the name.
    if recorded_root_path and not _same_path(recorded_root_path, str(topology.root)):
        if may_force:
            return
        raise ProjectIdentityRefused(
            f"Refusing to scan {topology.root} as project {project_name!r}: that project is "
            f"already recorded at {recorded_root_path}. Two directories sharing a basename write "
            "into one silo, and the second scan prunes the first's files. If the project moved, "
            "re-scan with the operator-tier override; if these are different projects, give this "
            "one an explicit name."
        )
