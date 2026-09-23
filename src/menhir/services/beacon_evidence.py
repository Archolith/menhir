"""Menhir evidence dump for Beacon's build pipeline (replaces the #120 mapping).

Beacon owns Beacon generation: its ``MenhirSourceAdapter`` consumes a
**versioned evidence document** (``beacon-menhir-evidence`` v1.0) describing
what Menhir indexed about one project. Menhir's only job here is to dump that
document from its structure read surface -- deterministically, fail-closed,
and without mapping any Beacon manifest fields. The bespoke raw-manifest
construction this module supersedes lived in ``beacon_generation`` until the
ownership switch: Menhir supplies indexed facts; Beacon decides what a Beacon
is.

Determinism: for a frozen graph state the dump is byte-identical (stable
sort orders, capped sections, no timestamps). The scan fingerprint doubles
as the citation value Beacon records. Evidence is explicitly point-in-time:
Menhir rejects observed filesystem/graph drift but cannot lock arbitrary
repository editors through the later publication side effect.

Git state is part of that point in time. Menhir passes ``--repo`` so Beacon
can cross-check the indexed root, and that also runs Beacon's git tier: the
manifest cites ``git HEAD <sha>`` and takes ``project.repository`` from the
``origin`` remote. The scan fingerprint excludes ``.git``, so an empty commit
changes the manifest without changing the fingerprint (PR #125 F2). The
guard therefore also captures whether the root is a git repository, its HEAD,
and a digest of its origin URL, and publication is refused if any of them
moved since capture, exactly like a scan change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from menhir.infrastructure.project_scanner import ProjectScanner
from menhir.services.beacon_compat import child_environment

__all__ = [
    "PROVIDER_EVIDENCE_VERSION",
    "build_provider_evidence",
    "BeaconEvidenceError",
    "BeaconEvidenceGuard",
    "BeaconEvidenceProjectReader",
    "RepositoryGitState",
    "capture_evidence",
    "dump_evidence",
    "read_git_state",
    "require_evidence_guard",
    "write_evidence_document",
]

EVIDENCE_VERSION = "1.0"

#: Stable section caps (mirror the adapter's bounds with room to spare; the
#: adapter refuses anything over its own caps, so the dump stays inside them).
_MAX_DOCUMENTS = 32
_MAX_FILES = 256

#: Priority documents always surface first when capping.
_PRIORITY_DOCS = ("README.md", ".agent/README.md")

#: The documented default ``document_type`` (``ingest_document``'s default), used for any
#: indexed document whose type was never recorded.
_DEFAULT_DOCUMENT_TYPE = "generic"


_GIT_TIMEOUT_SECONDS = 30


class BeaconEvidenceError(ValueError):
    """Raised when required source evidence for the evidence dump is unavailable."""


@dataclass(frozen=True)
class RepositoryGitState:
    """The git facts Beacon's git tier projects into the manifest.

    Mirrors Beacon's own availability rule (a ``.git`` directory or file at the root).
    ``head`` is the HEAD commit (``""`` when not a repository, or while HEAD is unborn), and
    ``origin_digest`` is a SHA-256 of the raw ``origin`` URL, so a credential-bearing URL is
    compared without being held.
    """

    is_repository: bool
    head: str = ""
    origin_digest: str = ""


@dataclass(frozen=True)
class BeaconEvidenceGuard:
    """Graph/filesystem/git version proving one point-in-time evidence capture."""

    root_path: str
    scan_fingerprint: str
    files_discovered: int
    files_eligible: int
    files_indexed: int
    partial_index: bool
    project_id: str
    writer_revision: str
    git: RepositoryGitState


class BeaconEvidenceProjectReader(Protocol):
    """The subset of StructureQueries the evidence dump reads; injectable for tests."""

    def get_beacon_evidence_guard(self, project_name: str) -> dict[str, Any]: ...

    def query_overview(self, project: str) -> dict[str, Any]: ...

    def query_files(
        self, project: str, path_filter: str = ""
    ) -> list[dict[str, str]]: ...

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]: ...


def _git_output(repo_root: Path, *args: str) -> str | None:
    """Run one fixed, read-only git query at the root; ``None`` when it fails."""
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "--no-optional-locks", *args],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**child_environment(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip()


def read_git_state(repo_root: Path) -> RepositoryGitState:
    """Capture the repository git state Beacon's build will read at *repo_root*."""
    dot_git = repo_root / ".git"
    if not (dot_git.is_dir() or dot_git.is_file()):
        return RepositoryGitState(is_repository=False)
    head = _git_output(repo_root, "rev-parse", "--verify", "HEAD") or ""
    origin = _git_output(repo_root, "remote", "get-url", "origin") or ""
    return RepositoryGitState(
        is_repository=True,
        head=head,
        origin_digest=hashlib.sha256(origin.encode("utf-8")).hexdigest() if origin else "",
    )


def _read_guard(
    reader: BeaconEvidenceProjectReader,
    project: str,
    repo_root: Path,
    git: RepositoryGitState,
) -> BeaconEvidenceGuard:
    """Read and validate the authoritative graph-side evidence fence.

    *git* is the repository state captured alongside it. Graph reads never change it, so two
    guards compare equal only when both the graph fence and the captured git state match.
    """
    raw = reader.get_beacon_evidence_guard(project)
    if not raw.get("project_known"):
        raise BeaconEvidenceError(f"project is not indexed (no root path): {project}")
    root = str(raw.get("root_path") or "")
    if not root or Path(root).resolve() != repo_root.resolve():
        raise BeaconEvidenceError(
            f"indexed root {root} does not match requested repository {repo_root}"
        )
    if raw.get("files_indexed") is None:
        raise BeaconEvidenceError(
            f"project coverage is unknown; re-ingest first: {project}"
        )
    if raw.get("partial_index"):
        raise BeaconEvidenceError(
            f"project index is partial; complete an ingest first: {project}"
        )
    fingerprint = str(raw.get("scan_fingerprint") or "")
    if not fingerprint:
        raise BeaconEvidenceError(
            f"project has no scan fingerprint; re-ingest first: {project}"
        )
    if not raw.get("project_id") or not raw.get("identity_known"):
        raise BeaconEvidenceError(
            f"project has no fenced structure identity; re-ingest first: {project}"
        )
    if raw.get("active_writers"):
        raise BeaconEvidenceError(
            f"project structure is being updated; retry generation: {project}"
        )
    return BeaconEvidenceGuard(
        root_path=root,
        scan_fingerprint=fingerprint,
        files_discovered=int(raw.get("files_discovered") or 0),
        files_eligible=int(raw.get("files_eligible") or 0),
        files_indexed=int(raw.get("files_indexed") or 0),
        partial_index=bool(raw.get("partial_index")),
        project_id=str(raw.get("project_id") or ""),
        writer_revision=str(raw.get("writer_revision") or ""),
        git=git,
    )


def _require_filesystem_match(guard: BeaconEvidenceGuard, project: str, repo_root: Path) -> None:
    """Refuse unless the checkout still matches the guard: scan fingerprint AND git state.

    This is the "is the output still current" comparison. The fingerprint excludes ``.git``,
    so a HEAD move (for example an empty commit) or an origin change is checked separately:
    both change the manifest Beacon builds.
    """
    current_fingerprint = ProjectScanner().scan(repo_root).scan_fingerprint
    if guard.scan_fingerprint != current_fingerprint:
        raise BeaconEvidenceError(
            f"project index is stale for the current checkout; re-ingest first: {project}"
        )
    if read_git_state(repo_root) != guard.git:
        raise BeaconEvidenceError(
            "repository git state (HEAD or origin) changed since evidence capture; "
            f"retry generation: {project}"
        )


def _grounded_description(overview: dict[str, Any], project: str) -> str:
    """Return the description the scanner actually read, or refuse.

    The overview's ``description`` is display text: when neither ``.agent/README.md`` nor
    ``CLAUDE.md`` exists the writer stores a ``"<stack> project"`` placeholder there, so it can
    never be empty for a scanned project. Beacon's evidence schema requires a non-empty
    ``project.description`` and publishes it as the project's purpose, so an ungrounded value
    cannot be omitted either -- it is refused (PR #125 F4). ``indexed_description`` is the raw
    scanner value; ``None`` means the project node predates the property.
    """
    if "indexed_description" not in overview:
        raise BeaconEvidenceError(
            f"no indexed project description available; refusing to invent one: {project}"
        )
    raw = overview.get("indexed_description")
    if raw is None:
        raise BeaconEvidenceError(
            f"project was indexed before descriptions were recorded; re-ingest first: {project}"
        )
    description = str(raw).strip()
    if not description:
        raise BeaconEvidenceError(
            "no indexed project description (.agent/README.md or CLAUDE.md is missing); "
            f"refusing to invent one: {project}"
        )
    return description


def _document_type(raw: object) -> str:
    """Return the indexed document type, or the documented default when it is unset.

    Scanner-indexed documents carry no ``document_type``. A reader that stringified the unset
    value handed over ``"None"``, which is truthy, so a plain ``or`` fallback let Beacon publish
    ``role: None`` (PR #125 F1). ``None``, blank, and the literal ``"None"`` all mean "unset".
    """
    value = "" if raw is None else str(raw).strip()
    if not value or value == "None":
        return _DEFAULT_DOCUMENT_TYPE
    return value


def _document_rank(path: str) -> tuple[int, str]:
    return (0 if path in _PRIORITY_DOCS else 1, path)


def capture_evidence(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> tuple[dict[str, Any], BeaconEvidenceGuard]:
    """Build evidence and return the graph/filesystem/git version that fenced its reads."""
    guard = _read_guard(reader, project, repo_root, read_git_state(repo_root))
    _require_filesystem_match(guard, project, repo_root)
    overview = reader.query_overview(project)
    description = _grounded_description(overview, project)

    documents: list[dict[str, str]] = []
    for row in reader.query_documents(project):
        path = str(row.get("structure_path") or row.get("path") or "")
        if not path:
            continue
        documents.append(
            {
                "path": path,
                "title": str(row.get("title") or row.get("name") or ""),
                "document_type": _document_type(row.get("doc_type")),
            }
        )
    documents.sort(key=lambda d: _document_rank(d["path"]))
    documents = documents[:_MAX_DOCUMENTS]

    files: list[dict[str, str]] = []
    for row in reader.query_files(project):
        path = str(row.get("path") or "")
        if not path:
            continue
        files.append(
            {
                "path": path,
                "role": str(row.get("role") or ""),
                "description": str(row.get("description") or ""),
            }
        )
    files.sort(key=lambda f: (0 if f["role"] == "entrypoint" else 1, f["path"]))
    files = files[:_MAX_FILES]

    entities = {
        str(k): int(v) for k, v in sorted((overview.get("entities") or {}).items()) if v
    }
    edges = {
        str(k): int(v) for k, v in sorted((overview.get("edges") or {}).items()) if v
    }

    after = _read_guard(reader, project, repo_root, guard.git)
    if after != guard:
        raise BeaconEvidenceError(
            f"project structure changed while evidence was read; retry generation: {project}"
        )

    # This status is deliberately weaker than ``current``. The graph and filesystem matched at
    # capture and are checked again before publication, but arbitrary editors do not participate
    # in Menhir's publication lock and can change the checkout immediately afterwards.
    document = {
        "evidence_version": EVIDENCE_VERSION,
        "project": {
            "name": project,
            "description": description,
            "primary_language": str(overview.get("stack") or ""),
            "root": str(repo_root),
            "status": "experimental",
            "scan_fingerprint": guard.scan_fingerprint,
        },
        "documents": documents,
        "files": files,
        "structure": {"entities": entities, "edges": edges},
    }
    return document, guard


#: The evidence version Menhir serves as a Beacon memory provider (plan Phase 3A).
PROVIDER_EVIDENCE_VERSION = "1.1"
PROVIDER_NAME = "menhir"
#: `source` stamped by StructureGraphWriter.write_document (ingest_document).
_DOCUMENT_INGEST_SOURCE = "document-ingest"


class ProviderEvidenceReader(Protocol):
    """What the provider builder reads; the graph adapter satisfies it."""

    def get_beacon_evidence_guard_by_id(self, project_id: str) -> dict[str, Any]: ...

    def list_indexed_repositories(self) -> list[dict[str, str]]: ...

    def query_structure(self, project: str, query_type: str, **kwargs: Any) -> Any: ...

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]: ...


def normalize_repository(url: str) -> str:
    """Repository identity as Beacon compares it: host (+ non-default port) + path, no scheme,
    no ``.git``, lowercase. Mirrors ``beacon.main._normalize_repository`` so a lookup agrees
    with Beacon's own freshness check."""
    text = url.strip()
    if not text:
        return ""
    scp = re.match(r"^[\w.-]+@([^:/]+):(.+)$", text)
    if scp:
        text = f"ssh://{scp.group(1)}/{scp.group(2)}"
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    try:
        port_number = parts.port
    except ValueError:
        return ""
    port = f":{port_number}" if port_number and port_number not in (22, 80, 443) else ""
    return f"{(parts.hostname or '').lower()}{port}{path.lower()}"


def find_projects_by_repository(
    reader: ProviderEvidenceReader, repository: str
) -> list[dict[str, str]]:
    """Indexed projects whose last scan recorded *repository* as origin (Beacon's lookup).

    Several checkouts of one repository are several projects; the caller must choose, so all are
    returned rather than one guessed.
    """
    wanted = normalize_repository(repository)
    if not wanted:
        raise BeaconEvidenceError("a repository origin is required")
    return sorted(
        (
            {k: row[k] for k in ("project_id", "name", "root_path")}
            for row in reader.list_indexed_repositories()
            if row.get("project_id") and normalize_repository(row.get("repository", "")) == wanted
        ),
        key=lambda row: (row["name"], row["project_id"]),
    )


def _provider_guard(reader: ProviderEvidenceReader, project_id: str) -> dict[str, Any]:
    raw = reader.get_beacon_evidence_guard_by_id(project_id)
    if not raw.get("project_known"):
        raise BeaconEvidenceError(f"no indexed project has id {project_id}")
    if raw.get("ambiguous"):
        raise BeaconEvidenceError(f"more than one indexed project has id {project_id}")
    if raw.get("files_indexed") is None:
        raise BeaconEvidenceError("project coverage is unknown; re-ingest first")
    if raw.get("partial_index"):
        raise BeaconEvidenceError("project index is partial; complete an ingest first")
    if not raw.get("scan_fingerprint"):
        raise BeaconEvidenceError("project has no scan fingerprint; re-ingest first")
    if not raw.get("identity_known"):
        raise BeaconEvidenceError("project has no fenced structure identity; re-ingest first")
    if raw.get("active_writers"):
        raise BeaconEvidenceError("project structure is being updated; retry")
    if not raw.get("indexed_commit"):
        raise BeaconEvidenceError(
            "project was not indexed from a git checkout, or before commits were recorded; "
            "re-ingest from the repository"
        )
    if raw.get("indexed_dirty") is not False:
        raise BeaconEvidenceError(
            "project was indexed from a checkout with uncommitted changes; "
            "commit and re-ingest so the evidence describes a commit"
        )
    return raw


def build_provider_evidence(reader: ProviderEvidenceReader, project_id: str) -> dict[str, Any]:
    """Return ``beacon-memory-evidence-1.1`` for one indexed project, read from the graph only.

    Menhir as a Beacon memory provider: no filesystem path is read, no git command runs, and
    nothing is written. The binding (repository, indexed commit) was recorded by the scan that
    produced the fingerprint, and only a clean checkout's scan is served. The fence is read
    before and after the content queries; any writer or binding change in between refuses.
    """
    project_id = project_id.strip()
    if not project_id:
        raise BeaconEvidenceError("project_id is required")
    before = _provider_guard(reader, project_id)
    name = before["name"]
    overview = reader.query_structure(name, "overview")
    description = _grounded_description(overview, name)

    documents: list[dict[str, str]] = []
    for row in reader.query_documents(name):
        # Only documents the bound scan produced: an ingest_document node is written outside
        # any scan, so the indexed commit says nothing about it.
        if row.get("source") == _DOCUMENT_INGEST_SOURCE:
            continue
        path = str(row.get("structure_path") or row.get("path") or "")
        if not path:
            continue
        documents.append(
            {
                "path": path,
                "title": str(row.get("title") or row.get("name") or ""),
                "document_type": _document_type(row.get("doc_type")),
            }
        )
    documents.sort(key=lambda d: _document_rank(d["path"]))

    files: list[dict[str, str]] = []
    for row in reader.query_structure(name, "files"):
        path = str(row.get("path") or "")
        if path:
            files.append(
                {
                    "path": path,
                    "role": str(row.get("role") or ""),
                    "description": str(row.get("description") or ""),
                }
            )
    files.sort(key=lambda f: (0 if f["role"] == "entrypoint" else 1, f["path"]))

    after = _provider_guard(reader, project_id)
    fence_keys = ("scan_fingerprint", "writer_revision", "indexed_commit", "indexed_repository")
    if any(before[key] != after[key] for key in fence_keys):
        raise BeaconEvidenceError("project was re-indexed while evidence was read; retry")

    entities = {str(k): int(v) for k, v in sorted((overview.get("entities") or {}).items()) if v}
    edges = {str(k): int(v) for k, v in sorted((overview.get("edges") or {}).items()) if v}
    project: dict[str, Any] = {
        "name": name,
        "description": description,
        "scan_fingerprint": before["scan_fingerprint"],
    }
    stack = str(overview.get("stack") or "")
    if stack:
        project["primary_language"] = stack
    # No project.status: Menhir indexes code, it does not judge maturity (plan A1).
    return {
        "evidence_version": PROVIDER_EVIDENCE_VERSION,
        "binding": {
            "provider": PROVIDER_NAME,
            "project_id": project_id,
            "repository": before["indexed_repository"],
            "indexed_commit": before["indexed_commit"],
        },
        "project": project,
        "documents": documents[:_MAX_DOCUMENTS],
        "files": files[:_MAX_FILES],
        "structure": {"entities": entities, "edges": edges},
    }


def dump_evidence(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> dict[str, Any]:
    """Build the point-in-time evidence document strictly from indexed facts."""
    return capture_evidence(reader, project, repo_root)[0]


def require_evidence_guard(
    reader: BeaconEvidenceProjectReader,
    project: str,
    repo_root: Path,
    expected: BeaconEvidenceGuard,
) -> None:
    """Refuse unless graph, filesystem, and git state still match a captured evidence version."""
    before = _read_guard(reader, project, repo_root, expected.git)
    if before != expected:
        raise BeaconEvidenceError(
            f"project structure changed after evidence capture; retry generation: {project}"
        )
    _require_filesystem_match(expected, project, repo_root)
    after = _read_guard(reader, project, repo_root, expected.git)
    if after != expected:
        raise BeaconEvidenceError(
            f"project structure changed during final freshness check; retry generation: {project}"
        )


def write_evidence_document(document: dict[str, Any], directory: Path) -> Path:
    """Write the evidence document as deterministic JSON; return the path.

    The file lands in *directory* under a unique temporary name and is not
    fsynced for crash-safety: it is scratch input for a local subprocess, and
    the caller unlinks it when done.
    """
    payload = json.dumps(document, sort_keys=True, ensure_ascii=True, indent=1)
    fd, raw_path = tempfile.mkstemp(
        dir=directory, prefix=".beacon-evidence-", suffix=".json"
    )
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    return Path(raw_path)
