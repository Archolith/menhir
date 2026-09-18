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
as the citation value Beacon records, so every claim in the generated
artifact traces back to the exact indexed state it came from.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "BeaconEvidenceError",
    "BeaconEvidenceProjectReader",
    "dump_evidence",
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


class BeaconEvidenceProjectReader(Protocol):
    """The subset of StructureQueries the evidence dump reads; injectable for tests."""

    def get_project_root_path(self, project_name: str) -> str | None: ...

    def get_project_coverage(self, project_name: str) -> dict[str, Any]: ...

    def get_scan_fingerprint(self, project_name: str) -> str | None: ...

    def query_overview(self, project: str) -> dict[str, Any]: ...

    def query_files(
        self, project: str, path_filter: str = ""
    ) -> list[dict[str, str]]: ...

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]: ...


def _require_intact_index(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> str:
    """Fail closed unless the graph holds a complete, current index of *repo_root*."""
    root = reader.get_project_root_path(project)
    if not root:
        raise BeaconEvidenceError(f"project is not indexed (no root path): {project}")
    if Path(root).resolve() != repo_root.resolve():
        raise BeaconEvidenceError(
            f"indexed root {root} does not match requested repository {repo_root}"
        )
    coverage = reader.get_project_coverage(project)
    if not coverage.get("known"):
        raise BeaconEvidenceError(
            f"project coverage is unknown; re-ingest first: {project}"
        )
    if coverage.get("partial_index"):
        raise BeaconEvidenceError(
            f"project index is partial; complete an ingest first: {project}"
        )
    fingerprint = reader.get_scan_fingerprint(project)
    if not fingerprint:
        raise BeaconEvidenceError(
            f"project has no scan fingerprint; re-ingest first: {project}"
        )
    return str(fingerprint)


def _document_rank(path: str) -> tuple[int, str]:
    return (0 if path in _PRIORITY_DOCS else 1, path)


def dump_evidence(
    reader: BeaconEvidenceProjectReader, project: str, repo_root: Path
) -> dict[str, Any]:
    """Build the evidence document strictly from indexed facts; fail closed."""
    fingerprint = _require_intact_index(reader, project, repo_root)
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
                "document_type": str(row.get("document_type") or "generic"),
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

    # The index is verified current above, so Menhir can honestly state the
    # indexed knowledge is current; no other status is asserted.
    return {
        "evidence_version": EVIDENCE_VERSION,
        "project": {
            "name": project,
            "description": description,
            "primary_language": str(overview.get("stack") or ""),
            "root": str(repo_root),
            "status": "current",
            "scan_fingerprint": fingerprint,
        },
        "documents": documents,
        "files": files,
        "structure": {"entities": entities, "edges": edges},
    }


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
