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

from menhir.services.beacon_evidence import (
    BeaconEvidenceError,
    dump_evidence,
    write_evidence_document,
)


def _reader(
    *,
    root: str | None,
    coverage: dict | None = None,
    fingerprint: str | None = "fp-1",
    overview: dict | None = None,
    documents: list[dict] | None = None,
    files: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        get_project_root_path=lambda p: root,
        get_project_coverage=lambda p: (
            coverage or {"known": True, "partial_index": False}
        ),
        get_scan_fingerprint=lambda p: fingerprint,
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
                    "structure_path": "README.md",
                    "title": "Read Me",
                    "document_type": "generic",
                },
                {
                    "structure_path": "docs/architecture.md",
                    "title": "Architecture",
                    "document_type": "generic",
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
        "status": "current",
        "scan_fingerprint": "fp-1",
    }
    assert first["documents"][0]["path"] == "README.md"  # priority ordering
    assert [f["path"] for f in first["files"]] == [
        "src/__main__.py",
        "src/core.py",
    ]  # entrypoints first
    assert first["structure"] == {"entities": {"file": 3}, "edges": {"IMPORTS": 2}}


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


def test_missing_description_fails_closed(tmp_path: Path) -> None:
    empty = _reader(root=str(tmp_path), overview={"description": "", "stack": ""})
    with pytest.raises(BeaconEvidenceError, match="description"):
        dump_evidence(empty, "fixture", tmp_path)
