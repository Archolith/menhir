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
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from menhir.infrastructure.project_scanner import ProjectScanner

__all__ = [
    "BeaconEvidenceError",
    "BeaconEvidenceGuard",
    "BeaconEvidenceProjectReader",
    "capture_evidence",
    "dump_evidence",
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


class BeaconEvidenceError(ValueError):
    """Raised when required source evidence for the evidence dump is unavailable."""


@dataclass(frozen=True)
class BeaconEvidenceGuard:
    """Graph/filesystem version proving one point-in-time evidence capture."""

    root_path: str
    scan_fingerprint: str
    files_discovered: int
    files_eligible: int
    files_indexed: int
    partial_index: bool
    project_id: str
    writer_revision: str


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


def _read_guard(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> BeaconEvidenceGuard:
    """Read and validate the authoritative graph-side evidence fence."""
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
    )


def _require_filesystem_match(guard: BeaconEvidenceGuard, project: str, repo_root: Path) -> None:
    current_fingerprint = ProjectScanner().scan(repo_root).scan_fingerprint
    if guard.scan_fingerprint != current_fingerprint:
        raise BeaconEvidenceError(
            f"project index is stale for the current checkout; re-ingest first: {project}"
        )


def _document_rank(path: str) -> tuple[int, str]:
    return (0 if path in _PRIORITY_DOCS else 1, path)


def capture_evidence(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> tuple[dict[str, Any], BeaconEvidenceGuard]:
    """Build evidence and return the graph/filesystem version that fenced its reads."""
    guard = _read_guard(reader, project, repo_root)
    _require_filesystem_match(guard, project, repo_root)
    overview = reader.query_overview(project)
    description = str(overview.get("description") or "").strip()
    if not description:
        raise BeaconEvidenceError(
            f"no indexed project description available; refusing to invent one: {project}"
        )

    documents: list[dict[str, str]] = []
    for row in reader.query_documents(project):
        path = str(row.get("structure_path") or row.get("path") or "")
        if not path:
            continue
        documents.append(
            {
                "path": path,
                "title": str(row.get("title") or row.get("name") or ""),
                "document_type": str(row.get("doc_type") or "generic"),
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

    after = _read_guard(reader, project, repo_root)
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
    """Refuse unless graph and filesystem still match a captured evidence version."""
    before = _read_guard(reader, project, repo_root)
    if before != expected:
        raise BeaconEvidenceError(
            f"project structure changed after evidence capture; retry generation: {project}"
        )
    _require_filesystem_match(expected, project, repo_root)
    after = _read_guard(reader, project, repo_root)
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
