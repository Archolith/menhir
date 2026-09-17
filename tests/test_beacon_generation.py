"""Generation-path tests for #120: evidence gating, compat boundary, determinism.

The graph side is a fake in-memory reader; the Beacon side runs the real,
isolated Beacon package in a subprocess (its own parser/validator) rather than a
stub. These tests require MENHIR_TEST_BEACON_PYTHON to point at an absolute path
to a Python interpreter with the Beacon package installed; there is no stub or
skip fallback.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.services.beacon_compat import BeaconCompatError, beacon_python_is_usable
from menhir.services.beacon_generation import (
    BeaconGenerationError,
    build_raw_manifest,
    generate_beacon,
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
        get_project_coverage=lambda p: coverage or {"known": True, "partial_index": False},
        get_scan_fingerprint=lambda p: fingerprint,
        query_overview=lambda p: overview or {
            "description": "Fixture project for Beacon generation.",
            "stack": "python",
            "entities": {"file": 3},
            "edges": {"IMPORTS": 2},
            "coverage": {"known": True},
            "contains_repos": [],
        },
        query_documents=lambda p, path_filter="", document_type=None: documents
        or [
            {"structure_path": "README.md", "title": "Read Me", "document_type": "generic"},
            {"structure_path": "docs/architecture.md", "title": "Architecture", "document_type": "generic"},
        ],
        query_files=lambda p, path_filter="": files
        or [
            {"path": "src/__main__.py", "role": "entrypoint", "description": ""},
            {"path": "src/core.py", "role": "file", "description": "core"},
        ],
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """A fixture repository whose cited canonical docs exist on disk."""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("# Fixture project for Beacon generation.\n", encoding="utf-8")
    (tmp_path / "docs" / "architecture.md").write_text("# Architecture\nLayered design.\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def beacon_python() -> str:
    """Absolute path to a real Python interpreter with the Beacon package installed.

    Required via MENHIR_TEST_BEACON_PYTHON; these tests run the real Beacon package
    in an isolated subprocess, so there is no stub or skip fallback.
    """
    raw = os.environ.get("MENHIR_TEST_BEACON_PYTHON", "").strip()
    if not raw:
        pytest.fail(
            "MENHIR_TEST_BEACON_PYTHON is not set: these tests run the real Beacon "
            "package in a subprocess. Set it to the absolute path of a Python "
            "interpreter with Beacon installed, e.g. "
            "MENHIR_TEST_BEACON_PYTHON=/opt/beacon-venv/bin/python"
        )
    interpreter = Path(raw)
    if not interpreter.is_absolute():
        pytest.fail(
            f"MENHIR_TEST_BEACON_PYTHON must be an absolute path, got {raw!r}"
        )
    if not interpreter.is_file():
        pytest.fail(
            "MENHIR_TEST_BEACON_PYTHON does not point at an existing interpreter "
            f"file: {raw!r}"
        )
    return str(interpreter)


def test_unindexed_project_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconGenerationError, match="not indexed"):
        build_raw_manifest(_reader(root=None), "fixture", tmp_path)


def test_root_mismatch_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BeaconGenerationError, match="does not match"):
        build_raw_manifest(_reader(root="C:/somewhere/else"), "fixture", tmp_path)


def test_partial_index_and_missing_description_fail_closed(tmp_path: Path) -> None:
    partial = _reader(root=str(tmp_path), coverage={"known": True, "partial_index": True})
    with pytest.raises(BeaconGenerationError, match="partial"):
        build_raw_manifest(partial, "fixture", tmp_path)
    empty = _reader(root=str(tmp_path), overview={"description": "", "stack": ""})
    with pytest.raises(BeaconGenerationError, match="description"):
        build_raw_manifest(empty, "fixture", tmp_path)


def test_manifest_grounding_and_determinism(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path))
    first = build_raw_manifest(reader, "fixture", tmp_path)
    second = build_raw_manifest(_reader(root=str(tmp_path)), "fixture", tmp_path)
    assert first == second  # same graph state -> identical mapping
    assert first["project"]["name"] == "fixture"
    assert first["canonical_docs"][0]["path"] == "README.md"
    concept = first["core_concepts"][0]
    assert concept["id"] == "project-structure"
    assert concept["sources"] and concept["sources"][0]["type"] == "manifest"


def test_usable_beacon_python_accepts_real_package(beacon_python: str) -> None:
    beacon_python_is_usable(beacon_python)  # real interpreter; fails closed otherwise


def test_unusable_interpreter_fails(tmp_path: Path) -> None:
    with pytest.raises(BeaconCompatError):
        beacon_python_is_usable(str(tmp_path / "no-such-python.exe"))


def test_real_beacon_round_trip(tmp_path: Path, beacon_python: str) -> None:
    """Build through the real Beacon package, then validate the on-disk manifest."""
    repo = _fixture_repo(tmp_path)
    reader = _reader(root=str(repo))
    outcome = generate_beacon(
        reader, "fixture", repo, beacon_python=beacon_python, refresh=False
    )
    target = repo / "beacon.generated.yaml"
    assert outcome.created and target.is_file()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Generated by Menhir beacon v1\n")
    assert "project:" in text and "canonical_docs:" in text
    # Refresh with wrong digest must not clobber.
    with pytest.raises(BeaconGenerationError):
        generate_beacon(reader, "fixture", repo, beacon_python=beacon_python,
                        refresh=True, expected_sha256="0" * 64)
    assert target.read_text(encoding="utf-8") == text


def test_beacon_validate_cli_accepts_generated_output(tmp_path: Path, beacon_python: str) -> None:
    repo = _fixture_repo(tmp_path)
    reader = _reader(root=str(repo))
    generate_beacon(reader, "fixture", repo, beacon_python=beacon_python)
    completed = subprocess.run(
        [beacon_python, "-m", "beacon", "validate", str(repo / "beacon.generated.yaml")],
        capture_output=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_staleness_guard(tmp_path: Path) -> None:
    reader = _reader(root=str(tmp_path), fingerprint="fp-1")
    build_raw_manifest(reader, "fixture", tmp_path)
    reader.get_scan_fingerprint = lambda p: None  # lost fingerprint after graph change
    with pytest.raises(BeaconGenerationError, match="fingerprint"):
        build_raw_manifest(reader, "fixture", tmp_path)


def test_refresh_deterministic_round_trip(tmp_path: Path, beacon_python: str) -> None:
    repo = _fixture_repo(tmp_path)
    reader = _reader(root=str(repo))
    first = generate_beacon(reader, "fixture", repo, beacon_python=beacon_python)
    refreshed = generate_beacon(reader, "fixture", repo, beacon_python=beacon_python,
                                refresh=True, expected_sha256=first.sha256)
    assert refreshed.sha256 == first.sha256  # zero semantic diff on unchanged inputs
