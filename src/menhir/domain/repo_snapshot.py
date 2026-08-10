"""Repo session-snapshot anchors + durable file identity (Chronostratum Rung 4.5 / Rev 5).

At session boundaries a coding agent captures repo state (HEAD, branch, working-tree
diff); menhir anchors memories to that snapshot so "what commit was active during this
failed session?" / "what changed after X?" become answerable. The graph-write side
(:GitCommit / :WorkingTreeSnapshot / :GitChange joined to existing :File nodes) is gated;
this module is the deterministic domain backbone it needs.

The one HARD requirement (Rev 5): **identity != location**. A file's path is a mutable
attribute; the node needs a durable ``file_id`` so git + belief + structure history follows
code across moves/renames (exactly when "what moved and what broke" matters). Keying nodes
on path orphans history on every refactor. ``FileIdentityResolver`` assigns a stable id on
first sight and re-points the path on a rename, keeping the id (and therefore every edge).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from menhir.domain.git_staleness import GitChange


@dataclass
class FileIdentityResolver:
    """Stable path -> file_id map that survives renames (durable identity, Rev 5).

    ``resolve`` assigns an id on first sight; ``apply_rename`` moves the id from the old
    path to the new one (path is an attribute, id is the node). Namespacing keeps the same
    repo-relative path in different projects distinct."""

    namespace: str = "default"
    _by_path: dict[str, str] = field(default_factory=dict)
    _counter: int = 0

    def resolve(self, path: str) -> str:
        """Get-or-assign the durable file_id for a repo-relative path."""
        existing = self._by_path.get(path)
        if existing is not None:
            return existing
        self._counter += 1
        fid = f"{self.namespace}:file:{self._counter}"
        self._by_path[path] = fid
        return fid

    def id_for(self, path: str) -> str | None:
        return self._by_path.get(path)

    def apply_rename(self, old_path: str, new_path: str) -> str:
        """Re-point a rename: keep the file_id, move it from old_path to new_path.

        If the old path was never seen, this is effectively a first-sight of new_path."""
        fid = self._by_path.pop(old_path, None)
        if fid is None:
            return self.resolve(new_path)
        self._by_path[new_path] = fid
        return fid


@dataclass(frozen=True)
class RepoSnapshot:
    """Repo state captured at a session boundary (Rung 4.5 MVP).

    ``dirty`` flags uncommitted work; ``changes`` are the diff entries (reuse GitChange,
    which already carries renamed_from / worktree_hash for stash-correctness)."""

    head: str                  # git rev-parse HEAD
    branch: str                # git branch --show-current
    captured_at: str           # ISO ingested_at — survives a vanished SHA (plan caveat)
    dirty: bool = False
    changes: tuple[GitChange, ...] = ()


@dataclass(frozen=True)
class ReconciledChange:
    """A snapshot change joined to the durable file identity it touches."""

    file_id: str
    path: str
    change: GitChange


def reconcile_snapshot(snapshot: RepoSnapshot, resolver: FileIdentityResolver) -> list[ReconciledChange]:
    """Join a snapshot's changes to durable file_ids, applying renames first.

    A rename (change.renamed_from set) re-points the id so the moved file's history stays
    attached; other changes resolve (get-or-assign) their path's id. This is the
    file-identity reconciliation the plan names as the one hard MVP requirement."""
    out: list[ReconciledChange] = []
    for change in snapshot.changes:
        if change.renamed_from is not None:
            fid = resolver.apply_rename(change.renamed_from, change.target)
        else:
            fid = resolver.resolve(change.target)
        out.append(ReconciledChange(file_id=fid, path=change.target, change=change))
    return out
