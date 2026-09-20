"""Graph-side tests for the evidence dump: determinism, fail-closed gates.

These run without a Beacon interpreter: the evidence document is Menhir's
own assertion about its index, and the fail-closed rules (intact index,
matching root, complete coverage, non-empty description) are enforced here
before any Beacon contact.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.infrastructure.project_scanner import ProjectScanner
from menhir.services.beacon_evidence import (
    BeaconEvidenceError,
    dump_evidence,
    write_evidence_document,
)


_CURRENT_FINGERPRINT = object()


def _reader(
    *,
    root: str | None,
    coverage: dict | None = None,
    fingerprint: str | None | object = _CURRENT_FINGERPRINT,
    overview: dict | None = None,
    documents: list[dict] | None = None,
    files: list[dict] | None = None,
) -> SimpleNamespace:
    if fingerprint is _CURRENT_FINGERPRINT:
        fingerprint = (
            ProjectScanner().scan(root).scan_fingerprint
            if root is not None and Path(root).is_dir()
            else "unavailable-current-fingerprint"
        )
    resolved_coverage = coverage or {"known": True, "partial_index": False}
    guard_state = {
        "project_known": root is not None,
        "root_path": root or "",
        "scan_fingerprint": fingerprint,
        "files_discovered": 3,
        "files_eligible": 3,
        "files_indexed": 3 if resolved_coverage.get("known") else None,
        "partial_index": bool(resolved_coverage.get("partial_index")),
        "project_id": "project-id-1",
        "identity_known": True,
        "active_writers": (),
        "writer_revision": "writer-1",
    }
    return SimpleNamespace(
        _guard_state=guard_state,
        get_beacon_evidence_guard=lambda p: dict(guard_state),
        query_overview=lambda p: (
            overview
            or {
                "description": "Fixture project for the evidence dump.",
                "stack": "python",
                "entities": {"file": 3},
                "edges": {"IMPORTS": 2},
                "coverage": {"known": True},
                "contains_repos": [],
            }
        ),
        query_documents=lambda p, path_filter="", document_type=None: (
            documents
            or [
                {
                    "path": "README.md",
                    "name": "Read Me",
                    "doc_type": "generic",
                },
                {
                    "path": "docs/architecture.md",
                    "name": "Architecture",
                    "doc_type": "generic",
                },
            ]
        ),
        query_files=lambda p, path_filter="": (
            files
            or [
                {"path": "src/core.py", "role": "file", "description": "core"},
                {"path": "src/__main__.py", "role": "entrypoint", "description": ""},
            ]
        ),
    )


def test_dump_shape_and_determinism(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    first = dump_evidence(reader, "fixture", tmp_path)
    second = dump_evidence(_reader(root=str(tmp_path)), "fixture", tmp_path)
    assert first == second
    assert first["evidence_version"] == "1.0"
    assert first["project"] == {
        "name": "fixture",
        "description": "Fixture project for the evidence dump.",
        "primary_language": "python",
        "root": str(tmp_path),
        "status": "experimental",
        "scan_fingerprint": ProjectScanner().scan(tmp_path).scan_fingerprint,
    }
    assert first["documents"][0]["path"] == "README.md"  # priority ordering
    assert [f["path"] for f in first["files"]] == [
        "src/__main__.py",
        "src/core.py",
    ]  # entrypoints first
    assert first["structure"] == {"entities": {"file": 3}, "edges": {"IMPORTS": 2}}


def test_document_type_uses_structure_reader_shape(tmp_path: Path) -> None:
    evidence = dump_evidence(
        _reader(
            root=str(tmp_path),
            documents=[
                {
                    "path": "docs/reference.md",
                    "name": "Reference",
                    "doc_type": "reference_article",
                }
            ],
        ),
        "fixture",
        tmp_path,
    )
    assert evidence["documents"] == [
        {
            "path": "docs/reference.md",
            "title": "Reference",
            "document_type": "reference_article",
        }
    ]


def test_written_document_is_deterministic_json(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    document = dump_evidence(reader, "fixture", tmp_path)
    path_a = write_evidence_document(document, tmp_path)
    path_b = write_evidence_document(document, tmp_path)
    try:
        assert path_a.read_bytes() == path_b.read_bytes()
        assert json.loads(path_a.read_text(encoding="utf-8")) == document
    finally:
        path_a.unlink(missing_ok=True)
        path_b.unlink(missing_ok=True)


def test_unindexed_project_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconEvidenceError, match="not indexed"):
        dump_evidence(_reader(root=None), "fixture", tmp_path)


def test_root_mismatch_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconEvidenceError, match="does not match"):
        dump_evidence(_reader(root="C:/somewhere/else"), "fixture", tmp_path)


def test_partial_index_fails_closed(tmp_path: Path) -> None:
    partial = _reader(
        root=str(tmp_path), coverage={"known": True, "partial_index": True}
    )
    with pytest.raises(BeaconEvidenceError, match="partial"):
        dump_evidence(partial, "fixture", tmp_path)


def test_missing_fingerprint_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconEvidenceError, match="fingerprint"):
        dump_evidence(
            _reader(root=str(tmp_path), fingerprint=None), "fixture", tmp_path
        )


def test_stale_fingerprint_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconEvidenceError, match="stale"):
        dump_evidence(
            _reader(root=str(tmp_path), fingerprint="stale-fingerprint"),
            "fixture",
            tmp_path,
        )


def test_active_structure_writer_fails_closed(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    reader._guard_state["active_writers"] = ("writer-2",)
    with pytest.raises(BeaconEvidenceError, match="being updated"):
        dump_evidence(reader, "fixture", tmp_path)


def test_missing_structure_identity_fails_closed(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    reader._guard_state["identity_known"] = False
    with pytest.raises(BeaconEvidenceError, match="no fenced structure identity"):
        dump_evidence(reader, "fixture", tmp_path)


def test_writer_revision_change_during_graph_reads_fails_closed(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    query_documents = reader.query_documents

    def interleaved_documents(*args, **kwargs):
        reader._guard_state["writer_revision"] = "writer-2"
        return query_documents(*args, **kwargs)

    reader.query_documents = interleaved_documents
    with pytest.raises(BeaconEvidenceError, match="changed while evidence was read"):
        dump_evidence(reader, "fixture", tmp_path)


def test_missing_description_fails_closed(tmp_path: Path) -> None:
    empty = _reader(root=str(tmp_path), overview={"description": "", "stack": ""})
    with pytest.raises(BeaconEvidenceError, match="description"):
        dump_evidence(empty, "fixture", tmp_path)
