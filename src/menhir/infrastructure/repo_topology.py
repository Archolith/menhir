"""What KIND of checkout a directory is -- resolved from files, never from ``git``.

Phase 0 of the project-identity plan (CF-257). Project identity is currently a directory
basename, so scanning a worktree or a fork writes into the canonical project's silo and the
per-project stale prune then deletes the rows that copy does not have. This module answers the
one question that guard needs: *is this scan root a checkout of a repository the graph already
holds under another path?*

**Why this does not shell out to git.** ``git rev-parse --git-common-dir`` is the obvious
implementation and it fails on this workspace in two different ways:

    live worktree  -> fatal: detected dubious ownership in repository at '...'
    stale worktree -> fatal: not a git repository: '.../worktrees/cth.harness-mcp+agent_plan'

The first is ownership policy (the service user is not the directory's owner); the second affects
the 7 of 81 worktrees whose gitdir no longer exists, mostly backup copies. Both are recoverable by
reading the same files git would read, because ``.git`` and ``commondir`` are plain text and need
no ownership check. A subprocess would also make this untestable without a real git installation.

**Why not ``_is_independent_clone``.** That predicate is ``os.path.isdir(".git")``, which is false
for a worktree, false for a submodule, **and false for a directory that is not a repository at
all** -- so using it on a scan root would refuse ordinary non-git project directories. It answers
"is this its own clone", which is the right question while walking children and the wrong question
for the root.

Every unresolvable case is :attr:`RootKind.UNRESOLVABLE`, never a fallback to "probably fine". A
scan root we cannot identify is precisely the one that must not proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = ["RootKind", "RootTopology", "classify_root"]

#: The marker git writes at the top of a worktree or submodule checkout.
_GITDIR_PREFIX = "gitdir:"

#: A worktree's private gitdir lives at ``<common>/worktrees/<name>``; a submodule's at
#: ``<superproject>/.git/modules/<name>``. The parent directory name is the discriminator, and it
#: is a git layout guarantee rather than a heuristic on the checkout's own name.
_WORKTREE_PARENT = "worktrees"
_SUBMODULE_PARENT = "modules"


class RootKind(Enum):
    """What a scan root is, structurally."""

    CLONE = "clone"
    """``.git`` is a directory: an independent clone. Safe to scan under its own identity."""

    WORKTREE = "worktree"
    """A second checkout of a repository that has a primary worktree elsewhere."""

    SUBMODULE = "submodule"
    """A nested repository owned by a superproject. Has no alternate primary checkout."""

    PLAIN = "plain"
    """Not a git repository. A legitimate scan target -- many projects are not repos."""

    UNRESOLVABLE = "unresolvable"
    """``.git`` is a file whose pointer cannot be followed. Fail closed."""

    MISSING = "missing"
    """The directory is not present on this host, so its shape cannot be observed at all.

    Distinct from :attr:`PLAIN` on purpose. A caller-supplied scan payload can name a root that
    only exists on the sender's machine, and reporting that as "not a git repository" would be a
    claim this process is in no position to make -- it would let a remotely-scanned worktree read
    as an ordinary directory. The caller decides what an unobservable root may do; the classifier
    only refuses to guess.
    """


@dataclass(frozen=True)
class RootTopology:
    """The classification, plus whatever the resolution could name.

    ``primary_worktree`` is populated only for :attr:`RootKind.WORKTREE`. A submodule genuinely
    has none -- "the primary" is undefined for it -- so reporting one would be a fabrication, and
    the refusal message must say *submodule* rather than naming a sibling that does not exist.
    """

    kind: RootKind
    root: Path
    gitdir: Path | None = None
    primary_worktree: Path | None = None
    detail: str = ""

    @property
    def is_secondary_checkout(self) -> bool:
        """True when scanning this root would write under another checkout's identity."""
        return self.kind in (RootKind.WORKTREE, RootKind.SUBMODULE)

    @property
    def may_scan(self) -> bool:
        """True only for shapes OBSERVED to own their identity outright.

        :attr:`RootKind.MISSING` is excluded deliberately: nothing was observed, so this cannot
        be a positive answer. Callers that legitimately accept an unobservable root -- the
        compatibility write path, where a remote client scanned on its own machine -- must handle
        that kind explicitly rather than reading silence as approval.
        """
        return self.kind in (RootKind.CLONE, RootKind.PLAIN)


def _read_gitdir_pointer(git_file: Path) -> Path | None:
    """Return the absolute gitdir a ``.git`` FILE points at, or None if it is not readable.

    The pointer may be relative to the checkout, which is how git writes it for a worktree created
    with a relative path -- resolving against the parent rather than the cwd is what makes this
    work from any working directory.
    """
    try:
        raw = git_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw.lower().startswith(_GITDIR_PREFIX):
        return None
    target = raw[len(_GITDIR_PREFIX):].strip()
    if not target:
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = (git_file.parent / candidate)
    try:
        return candidate.resolve()
    except OSError:
        return None


def _resolve_common_dir(gitdir: Path) -> Path | None:
    """Resolve ``<gitdir>/commondir`` to the shared ``.git`` directory, or None.

    ``commondir`` holds a path relative to the gitdir (``../..`` on this workspace's worktrees).
    Submodules do not write one at all, which is part of how the two are told apart.
    """
    commondir_file = gitdir / "commondir"
    try:
        raw = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = gitdir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def classify_root(root: str | Path) -> RootTopology:
    """Classify *root* as a scan target. Pure filesystem reads; never invokes git."""
    root_path = Path(root)
    try:
        root_path = root_path.resolve()
    except OSError:
        return RootTopology(
            kind=RootKind.UNRESOLVABLE, root=Path(root), detail="root path could not be resolved"
        )

    if not root_path.is_dir():
        return RootTopology(
            kind=RootKind.MISSING,
            root=root_path,
            detail="directory is not present on this host; shape cannot be observed",
        )

    git_marker = root_path / ".git"

    if git_marker.is_dir():
        return RootTopology(kind=RootKind.CLONE, root=root_path, detail="independent clone")

    if not git_marker.exists():
        return RootTopology(kind=RootKind.PLAIN, root=root_path, detail="not a git repository")

    # `.git` exists and is not a directory: a worktree or submodule pointer.
    gitdir = _read_gitdir_pointer(git_marker)
    if gitdir is None:
        return RootTopology(
            kind=RootKind.UNRESOLVABLE,
            root=root_path,
            detail=".git is a file but holds no readable 'gitdir:' pointer",
        )
    if not gitdir.is_dir():
        # The 7-of-81 case: the checkout survived but the repository it pointed into did not.
        return RootTopology(
            kind=RootKind.UNRESOLVABLE,
            root=root_path,
            gitdir=gitdir,
            detail=f"gitdir does not exist: {gitdir}",
        )

    parent_name = gitdir.parent.name

    if parent_name == _SUBMODULE_PARENT or _SUBMODULE_PARENT in gitdir.parts:
        return RootTopology(
            kind=RootKind.SUBMODULE,
            root=root_path,
            gitdir=gitdir,
            detail="submodule of a superproject; it has no alternate primary checkout",
        )

    if parent_name == _WORKTREE_PARENT:
        common = _resolve_common_dir(gitdir)
        if common is None:
            return RootTopology(
                kind=RootKind.UNRESOLVABLE,
                root=root_path,
                gitdir=gitdir,
                detail="worktree gitdir carries no resolvable 'commondir'",
            )
        return RootTopology(
            kind=RootKind.WORKTREE,
            root=root_path,
            gitdir=gitdir,
            primary_worktree=common.parent,
            detail="worktree; the primary checkout holds this repository's identity",
        )

    # A `.git` file in a layout this does not recognise. Refuse rather than guess -- an
    # unrecognised shape is exactly the case where assuming "clone" would be destructive.
    return RootTopology(
        kind=RootKind.UNRESOLVABLE,
        root=root_path,
        gitdir=gitdir,
        detail=f"unrecognised gitdir layout (parent directory {parent_name!r})",
    )
