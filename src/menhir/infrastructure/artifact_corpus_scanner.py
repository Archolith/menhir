"""Collect corpus entries from a working tree, with Git evidence.

The impure half of reconciliation: reads files, hashes raw bytes, and asks Git
what it knows. It answers "what is on disk right now" and nothing else -- no
graph access, no writes anywhere, no interpretation beyond the route table.

Git and the filesystem are kept apart on purpose. A dirty working-tree file has
a real raw-byte hash and no committed blob, which is a valid observation rather
than an error; recording the commit SHA in the same field as a content hash is
the v1 mistake this module exists to stop repeating.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from menhir.domain.artifact_reconciliation import (
    CorpusEntry,
    GitRename,
    VersionKind,
    is_index_document,
    medium_for_path,
    read_document_metadata,
    route_for_path,
    sha256_bytes,
)
from menhir.domain.work_artifact import ArtifactMedium

#: Anything a scan must never treat as a document.
_SKIP_PREFIXES = (".", "~", "#")
_SKIP_SUFFIXES = (".tmp", ".swp", ".bak", ".orig", ".rej")

_GIT_TIMEOUT_S = 30


@dataclass(frozen=True)
class GitEvidence:
    """What Git could say about a repository at scan time.

    Every field is optional. A plain directory that is not a repository, or a
    clone with no commits, still yields a usable corpus scan -- Git strengthens
    reconciliation but is never a precondition for it.
    """

    observed_commit: str | None = None
    blob_oids: dict[str, str] = field(default_factory=dict)
    renames: tuple[GitRename, ...] = ()
    available: bool = False
    rename_evidence_available: bool = False


def _run_git(repo: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def collect_git_evidence(repo: Path, *, from_commit: str | None = None) -> GitEvidence:
    """Observed commit, tracked blob OIDs, and renames since ``from_commit``.

    ``from_commit`` is the selected evidence base (normally the persisted
    cursor). Without one there is no rename window, and the caller falls back to
    the full corpus audit rather than assuming every delete/create pair in the
    interval was a move. A failed diff is represented separately from a valid
    diff containing zero renames.
    """
    head = _run_git(repo, ["rev-parse", "HEAD"])
    if head is None:
        return GitEvidence(available=False)
    observed_commit = head.strip() or None

    blob_oids: dict[str, str] = {}
    listing = _run_git(repo, ["ls-files", "-s", "--", "."])
    for line in (listing or "").splitlines():
        # `<mode> <oid> <stage>\t<path>`
        meta, tab, path = line.partition("\t")
        if not tab:
            continue
        parts = meta.split()
        if len(parts) >= 2:
            blob_oids[path.strip()] = parts[1]

    renames: list[GitRename] = []
    rename_evidence_available = from_commit is None
    if from_commit:
        if _is_valid_commit_cursor(from_commit):
            diff = _run_git(
                repo,
                ["diff", "--name-status", "-M", "--end-of-options", f"{from_commit}..HEAD"],
            )
            if diff is not None:
                rename_evidence_available = True
                renames.extend(parse_rename_status(diff))

    return GitEvidence(
        observed_commit=observed_commit,
        blob_oids=blob_oids,
        renames=tuple(renames),
        available=True,
        rename_evidence_available=rename_evidence_available,
    )


def renames_in_commit(repo: Path, commit: str) -> tuple[GitRename, ...]:
    """Renames Git recognizes inside one commit. Used by the one-time repair."""
    if not _is_valid_commit_cursor(commit):
        return ()
    diff = _run_git(
        repo, ["show", "--name-status", "-M", "--format=", "--end-of-options", commit]
    )
    return tuple(parse_rename_status(diff or ""))


_COMMIT_CURSOR_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_valid_commit_cursor(value: str) -> bool:
    return bool(_COMMIT_CURSOR_RE.match(value))


def parse_rename_status(text: str) -> list[GitRename]:
    """Parse ``R<score>\\told\\tnew`` lines out of a name-status diff.

    Only ``R`` records are read. A delete plus a create is not a rename unless
    Git itself says so; inferring one from a coincidence of timing is exactly the
    guess this design refuses.
    """
    renames: list[GitRename] = []
    for line in text.splitlines():
        if not line or not line[0] == "R":
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        renames.append(
            GitRename(old_path=parts[1].strip(), new_path=parts[2].strip())
        )
    return renames


def _is_scannable(path: Path) -> bool:
    name = path.name
    if name.startswith(_SKIP_PREFIXES):
        return False
    if name.endswith(_SKIP_SUFFIXES):
        return False
    return True


def scan_corpus(
    repo_root: Path,
    *,
    repository: str | None = None,
    git: GitEvidence | None = None,
) -> list[CorpusEntry]:
    """Every routed document in the tree, hashed and classified.

    Walks the declared routes rather than the whole tree: a route table that has
    to be consulted for every file in the repository would spend its time
    rejecting source code. Files in a routed directory whose extension is not a
    supported medium are skipped silently -- a ``.py`` prototype filed beside a
    plan is not a work artifact and was never claimed to be.
    """
    root = Path(repo_root).resolve()
    name = repository or root.name
    evidence = git if git is not None else GitEvidence(available=False)

    entries: list[CorpusEntry] = []
    seen: set[str] = set()
    for route in _route_directories():
        directory = root / route
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or not _is_scannable(path):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in seen:
                continue
            entry = build_entry(
                root, rel, repository=name, git=evidence
            )
            if entry is not None:
                seen.add(rel)
                entries.append(entry)
    return sorted(entries, key=lambda e: e.path)


def _route_directories() -> tuple[str, ...]:
    from menhir.domain.artifact_reconciliation import CORPUS_ROUTES

    return tuple(route.directory for route in CORPUS_ROUTES)


def build_entry(
    repo_root: Path,
    rel_path: str,
    *,
    repository: str,
    git: GitEvidence | None = None,
) -> CorpusEntry | None:
    """One entry for one repo-relative path, or None if it is not corpus material."""
    route = route_for_path(rel_path)
    if route is None or is_index_document(rel_path):
        return None
    medium = medium_for_path(rel_path)
    if medium is None:
        return None

    path = Path(repo_root) / rel_path
    try:
        payload = path.read_bytes()
    except OSError:
        return None

    integrity = sha256_bytes(payload)
    evidence = git or GitEvidence(available=False)
    blob_oid = (evidence.blob_oids or {}).get(rel_path)

    metadata = None
    if medium != ArtifactMedium.PDF:
        try:
            text = payload.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            text = ""
        metadata = read_document_metadata(text, route_type=route.artifact_type)

    return CorpusEntry(
        repository=repository,
        path=rel_path,
        medium=medium,
        lane=route.lane,
        integrity=integrity,
        size_bytes=len(payload),
        route_type=route.artifact_type,
        preserve_existing_type=route.preserve_existing_type,
        requires_declared_type=route.requires_declared_type,
        declared_uuid=metadata.artifact_uuid if metadata else None,
        declared_type=metadata.artifact_type if metadata else None,
        declared_status=metadata.artifact_status if metadata else None,
        raw_status_header=metadata.raw_status_header if metadata else None,
        title=(metadata.title if metadata else None) or Path(rel_path).stem,
        title_from_h1=bool(metadata and metadata.title),
        version=blob_oid,
        version_kind=VersionKind.GIT_BLOB_OID if blob_oid else None,
        metadata_errors=metadata.errors if metadata else (),
    )


def read_index_links(repo_root: Path, directory: str) -> frozenset[str]:
    """Filenames the index README in ``directory`` links to.

    Membership is checked by link text rather than by parsing Markdown properly:
    the question is whether a reader arriving at the index can reach the
    document, and a bare filename mention answers that as well as a link does.
    """
    for candidate in ("README.md", "index.md"):
        index = Path(repo_root) / directory / candidate
        if index.is_file():
            try:
                text = index.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return frozenset()
            names = {
                token.strip("()[]<>`,\"' ")
                for token in text.replace("(", " ").replace(")", " ").split()
            }
            return frozenset(
                Path(n).name for n in names if n.endswith((".md", ".pdf", ".html"))
            )
    return frozenset()
