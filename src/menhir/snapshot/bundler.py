"""Client-side snapshot bundler: enumerate tracked files, plan a bundle, write a deterministic ZIP.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P1 -- local bundler, inspect-only).

**Bytes come from the working tree; the file list comes from the index.** ``git ls-files`` says
which paths are tracked and what mode each carries, then every byte is read from disk as it is
right now. That is what makes dirty tracked work visible without ``git stash``, ``git add`` or any
other index mutation -- the P1 gate requires ``git status`` to be byte-identical before and after
a sync, so nothing here may write to the repository.

**This module shells out to git on purpose, which is the opposite of
:mod:`menhir.infrastructure.repo_topology`.** That module refuses to invoke git because it runs
server-side, where ``dubious ownership`` and missing worktree gitdirs made the subprocess fail on
directories it still had to classify. Here we are on the user's own machine, asking for tracked
paths and file modes -- facts that have no filesystem-only equivalent. The failure modes it warned
about are real, so every git failure surfaces as an actionable :class:`BundlerError` rather than a
traceback.

**Executable bits come from git, not from the filesystem.** ``os.access(X_OK)`` is meaningless on
Windows, so a bundle built there would flip every mode and change ``tree_digest`` for the same
content. Git's 100755/100644 is the same on every host.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from menhir.snapshot.policy import Decision, SelectionPolicy
from menhir.snapshot.protocol import (
    CONTENT_PREFIX,
    MANIFEST_NAME,
    PROVENANCE_SELF_REPORTED,
    PROVISIONAL_LIMITS,
    FileRecord,
    GitProvenance,
    Omission,
    OmissionReason,
    SnapshotLimits,
    SnapshotManifest,
    normalize_bundle_path,
    sha256_hex,
)

__all__ = [
    "BundlePlan",
    "BundlerError",
    "Refusal",
    "build_plan",
    "estimate_archive_upper_bound",
    "resolve_repo_root",
    "write_bundle",
]

GitRunner = Callable[[Sequence[str]], bytes]

ERR_NOT_A_REPO = "snapshot.source.not_a_git_repository"
ERR_GIT_UNAVAILABLE = "snapshot.source.git_unavailable"
ERR_GIT_FAILED = "snapshot.source.git_failed"
ERR_UNMERGED = "snapshot.source.unmerged_paths"
ERR_CONTENT_CHANGED = "snapshot.bundle.content_changed_during_write"
ERR_LIMIT_FILE_COUNT = "snapshot.limit.file_count"
ERR_LIMIT_TOTAL_BYTES = "snapshot.limit.total_bytes"

#: Fixed ZIP timestamp. The plan states that ZIP timestamps do not decide change detection, so
#: they are pinned: a bundle of identical content must be identical bytes, whenever it was built.
#: 1980-01-01 is the earliest the format can represent.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Pinned so the archive is reproducible across zlib versions and Python releases.
_DEFLATE_LEVEL = 6

_GIT_TIMEOUT_S = 30.0


class BundlerError(RuntimeError):
    """A condition that stops a sync, with a stable code and an actionable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Refusal:
    """A path whose upload is itself the harm. Blocks the sync until overridden."""

    path: str
    label: str


@dataclass(frozen=True)
class BundlePlan:
    """Everything decided before a byte is written."""

    root: Path
    manifest: SnapshotManifest
    refusals: tuple[Refusal, ...] = ()
    #: Tracked paths that no longer exist on disk. Not omissions: their absence IS the state the
    #: server should record, which is how a deletion reaches the graph at all.
    deleted: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.refusals)


def _default_runner(repo_path: str) -> GitRunner:
    def run(args: Sequence[str]) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", repo_path, *args],
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
                check=True,
            )
        except FileNotFoundError as exc:
            raise BundlerError(
                ERR_GIT_UNAVAILABLE,
                "git was not found on PATH. `menhir sync` needs git to list tracked files.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BundlerError(
                ERR_GIT_FAILED, f"git {args[0]} timed out after {_GIT_TIMEOUT_S:.0f}s."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            if "not a git repository" in detail.lower():
                raise BundlerError(
                    ERR_NOT_A_REPO,
                    "not a git repository. Snapshot sync covers git repositories in v1; "
                    "plain-directory support is a later phase.",
                ) from exc
            raise BundlerError(ERR_GIT_FAILED, f"git {args[0]} failed: {detail}") from exc
        return completed.stdout

    return run


def resolve_repo_root(path: str | os.PathLike[str], *, runner: GitRunner | None = None) -> Path:
    """Return the repository root containing *path*."""
    run = runner or _default_runner(str(path))
    out = run(["rev-parse", "--show-toplevel"]).decode("utf-8", "strict").strip()
    if not out:
        raise BundlerError(ERR_NOT_A_REPO, "git did not report a repository root.")
    return Path(out)


def _git_text(run: GitRunner, args: list[str]) -> str | None:
    """Run a git command for a provenance label. A missing answer is absent, never invented."""
    try:
        out = run(args).decode("utf-8", "replace").strip()
    except BundlerError:
        return None
    return out or None


def _collect_provenance(run: GitRunner) -> GitProvenance:
    """Describe the source checkout: base commit, its tree, branch label, and clean/dirty.

    Every field is a CLAIM about this machine -- see :class:`GitProvenance`. Each git call is
    individually optional, because a repository with no commits has no HEAD and a detached one has
    no branch, and neither is a reason to fail a sync.

    ``--untracked-files=no`` is the load-bearing flag in the dirty check: untracked files never
    enter the bundle, so they cannot make the bundled bytes differ from the base commit, and
    counting them would report almost every working repository as dirty for content it did not
    send. Staged-but-uncommitted changes DO count -- the bundler reads working-tree bytes, so they
    are in the snapshot.
    """
    base_commit = _git_text(run, ["rev-parse", "HEAD"])
    commit_tree = _git_text(run, ["rev-parse", "HEAD^{tree}"]) if base_commit else None
    branch = _git_text(run, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        # Detached: `--abbrev-ref` echoes the literal, which is not a branch label.
        branch = None
    status = _git_text(run, ["status", "--porcelain", "--untracked-files=no"])
    return GitProvenance(
        base_commit=base_commit,
        commit_tree=commit_tree,
        branch=branch,
        dirty=bool(status),
        quality=PROVENANCE_SELF_REPORTED,
    )


@dataclass(frozen=True)
class _IndexEntry:
    mode: str
    path: str


def _displayable(path: str) -> str:
    """Make a path safe to put in the manifest even if it is not valid UTF-8.

    `git ls-files` output is decoded with ``surrogateescape``, so a filename in some other
    encoding survives as lone surrogates. Those cannot be re-encoded, and the manifest is
    serialized as UTF-8 -- so recording such a path verbatim turned one oddly-named file into a
    crash for the whole repository. Only omissions can reach this: a path that does not normalize
    is never bundled.
    """
    return path.encode("utf-8", "replace").decode("utf-8")


def _list_index(run: GitRunner) -> list[_IndexEntry]:
    """Parse ``git ls-files -s -z --full-name``: ``<mode> <object> <stage>\\t<path>\\0``.

    ``--full-name`` makes every path relative to the repository root rather than to whatever
    directory git was invoked from. The caller binds the runner to the repository root anyway, so
    this is belt and braces -- but the failure it prevents is silent and expensive: paths relative
    to a subdirectory would be joined onto the root, every file would miss, and the snapshot would
    arrive looking like a repository whose entire contents had just been deleted.

    A non-zero stage means an unmerged path. Bundling one would upload a conflicted half-state and
    make the graph describe a tree that never existed, so it stops the sync.
    """
    raw = run(["ls-files", "-s", "-z", "--full-name"])
    entries: list[_IndexEntry] = []
    unmerged: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            meta, path_bytes = record.split(b"\t", 1)
            mode, _object_id, stage = meta.split(b" ")
        except ValueError as exc:
            raise BundlerError(
                ERR_GIT_FAILED, "could not parse `git ls-files -s -z` output."
            ) from exc
        path = path_bytes.decode("utf-8", "surrogateescape")
        if stage != b"0":
            unmerged.append(path)
            continue
        entries.append(_IndexEntry(mode=mode.decode("ascii"), path=path))
    if unmerged:
        raise BundlerError(
            ERR_UNMERGED,
            f"{len(unmerged)} path(s) have unresolved merge conflicts. "
            "Resolve them before syncing a snapshot.",
        )
    return entries


def build_plan(
    root: str | os.PathLike[str],
    *,
    display_name: str | None = None,
    policy: SelectionPolicy | None = None,
    limits: SnapshotLimits = PROVISIONAL_LIMITS,
    project_id: str | None = None,
    source_id: str | None = None,
    runner: GitRunner | None = None,
) -> BundlePlan:
    """Enumerate, classify, hash, and produce the manifest. Reads the repository; never writes it.

    *root* may be any directory inside the repository; the snapshot always covers the whole
    repository. Enumerating only the subtree the command was run from would produce a bundle that
    looks like a complete snapshot of the project and is missing most of it -- and the server
    prunes what a complete snapshot does not contain.
    """
    run = runner or _default_runner(str(root))
    repo_root = resolve_repo_root(root, runner=run)
    if runner is None:
        # Re-bind to the repository root so `menhir sync src/` still enumerates the whole project.
        run = _default_runner(str(repo_root))
    active_policy = policy or SelectionPolicy(max_file_bytes=limits.max_file_bytes)

    records: list[FileRecord] = []
    omissions: list[Omission] = []
    refusals: list[Refusal] = []
    deleted: list[str] = []
    total_bytes = 0

    for entry in _list_index(run):
        # Normalize first: a path git reports that this contract cannot represent is an omission,
        # not a crash, so one pathological filename cannot make a repository unsyncable.
        try:
            path = normalize_bundle_path(entry.path, limits)
        except ValueError:
            omissions.append(
                Omission(path=_displayable(entry.path), reason=OmissionReason.UNREADABLE)
            )
            continue

        if entry.mode == "160000":
            omissions.append(Omission(path=path, reason=OmissionReason.SUBMODULE))
            continue
        if entry.mode == "120000":
            omissions.append(Omission(path=path, reason=OmissionReason.SYMLINK))
            continue

        absolute = repo_root / path
        try:
            stat_result = absolute.stat()
        except OSError:
            # Tracked but absent: an upstream deletion, which the snapshot must express.
            deleted.append(path)
            continue
        if not absolute.is_file():
            omissions.append(Omission(path=path, reason=OmissionReason.UNREADABLE))
            continue

        if len(records) >= limits.max_file_count:
            raise BundlerError(
                ERR_LIMIT_FILE_COUNT,
                f"snapshot exceeds the {limits.max_file_count}-file limit.",
            )

        # Classify on the stat size, BEFORE reading: invariant 6 wants the limit enforced ahead of
        # allocation, not after a 900 MB read has already happened.
        verdict = active_policy.classify(path, stat_result.st_size)
        if verdict.decision is Decision.REFUSE:
            refusals.append(Refusal(path=path, label=verdict.explanation))
            continue
        if verdict.decision is Decision.OMIT:
            omissions.append(Omission(path=path, reason=verdict.reason))
            continue

        try:
            data = absolute.read_bytes()
        except OSError:
            omissions.append(Omission(path=path, reason=OmissionReason.UNREADABLE))
            continue
        if len(data) > limits.max_file_bytes:
            # The file grew between stat and read. Re-checking after decode is the same rule
            # invariant 6 applies server-side, for the same reason.
            omissions.append(Omission(path=path, reason=OmissionReason.OVERSIZE))
            continue

        total_bytes += len(data)
        if total_bytes > limits.max_total_bytes:
            raise BundlerError(
                ERR_LIMIT_TOTAL_BYTES,
                f"snapshot exceeds the {limits.max_total_bytes}-byte total limit.",
            )
        records.append(
            FileRecord(
                path=path,
                size=len(data),
                sha256=sha256_hex(data),
                executable=entry.mode == "100755",
            )
        )

    manifest = SnapshotManifest.build(
        display_name=display_name or repo_root.name,
        files=records,
        omissions=omissions,
        project_id=project_id,
        source_id=source_id,
        provenance=_collect_provenance(run),
        deleted_count=len(deleted),
    )
    return BundlePlan(
        root=repo_root,
        manifest=manifest,
        refusals=tuple(refusals),
        deleted=tuple(deleted),
    )


#: ZIP per-entry fixed cost: 30-byte local header + 46-byte central directory record, plus the
#: filename stored twice. The end-of-central-directory record adds 22 bytes once.
_ZIP_ENTRY_FIXED = 76
_ZIP_EOCD = 22


def _deflate_worst_case(size: int) -> int:
    """Deflate's guaranteed ceiling: incompressible input grows by ~5 bytes per 16 KiB block."""
    return size + (size // 1000) + 16


def estimate_archive_upper_bound(manifest: SnapshotManifest) -> int:
    """Upper bound on the archive size, assuming every byte is incompressible.

    Deliberately a bound rather than a prediction. A real figure means compressing the whole
    repository, which is the work ``--check`` exists to avoid, and a guessed compression ratio
    would be a number the user could not act on. Real archives come in well under this.
    """
    total = _ZIP_EOCD
    total += _ZIP_ENTRY_FIXED + 2 * len(MANIFEST_NAME) + _deflate_worst_case(
        len(manifest.to_canonical_bytes())
    )
    for record in manifest.files:
        name_len = len((CONTENT_PREFIX + record.path).encode("utf-8"))
        total += _ZIP_ENTRY_FIXED + 2 * name_len + _deflate_worst_case(record.size)
    return total


def write_bundle(plan: BundlePlan, target: BinaryIO) -> None:
    """Write the deterministic ZIP for *plan* into *target*.

    Determinism is not incidental: the server verifies a digest over these bytes, and a resumed
    upload re-sends chunks of an archive the client must be able to rebuild identically. Fixed
    entry order, a pinned timestamp, a fixed compression level, and modes taken from the manifest
    are what buy that.

    Each file is re-hashed as it is written. A mismatch means the working tree changed between
    planning and writing -- the snapshot would then disagree with its own manifest, so it stops
    rather than shipping a bundle whose digest the server will reject after the whole upload.
    """
    if plan.blocked:
        raise BundlerError(
            "snapshot.bundle.refused",
            f"{len(plan.refusals)} path(s) are refused by the secret-risk policy.",
        )
    manifest_bytes = plan.manifest.to_canonical_bytes()
    with zipfile.ZipFile(target, "w") as archive:
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=_ZIP_EPOCH)
        info.external_attr = 0o644 << 16
        info.create_system = 3
        _write_entry(archive, info, manifest_bytes)

        for record in plan.manifest.files:
            data = (plan.root / record.path).read_bytes()
            if sha256_hex(data) != record.sha256:
                raise BundlerError(
                    ERR_CONTENT_CHANGED,
                    "a file changed while the bundle was being written; re-run the sync.",
                )
            entry = zipfile.ZipInfo(CONTENT_PREFIX + record.path, date_time=_ZIP_EPOCH)
            entry.external_attr = (0o755 if record.executable else 0o644) << 16
            entry.create_system = 3
            _write_entry(archive, entry, data)


def _write_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, data: bytes) -> None:
    """Write one entry with compression and level stated per entry.

    Both arguments are passed here rather than to the ``ZipFile`` constructor because a manually
    built ``ZipInfo`` carries its own ``compress_type``, which defaults to ``ZIP_STORED`` and wins
    over the archive-level setting. Menhir's own tree went out 4x larger than it needed to be
    before this was measured, and nothing failed: stored entries are perfectly deterministic, so
    the determinism tests stayed green while every upload carried uncompressed bytes. Pinning the
    level explicitly also keeps the archive reproducible if zlib's default ever changes.
    """
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=_DEFLATE_LEVEL)
