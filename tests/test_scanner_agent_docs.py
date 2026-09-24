"""A0 (v2 design): the scanner indexes the bounded .agent/ doc set as documents.

Beacon generation wants .agent/README.md at the top of canonical_docs, but the
scanner has historically excluded markdown entirely, so the graph starves the
generator. This pins the bounded scan-time ingest: orientation docs become
document-role entities; process-only docs stay out.
"""

from __future__ import annotations

from pathlib import Path

from menhir.infrastructure.project_scanner import ProjectScanner


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_agent_orientation_docs_indexed_as_documents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _write(tmp_path / ".agent" / "README.md", "# Agent entry\nRouter doc.\n")
    _write(tmp_path / ".agent" / "architecture.md", "# Architecture\nSystem design.\n")
    _write(tmp_path / ".agent" / "maintenance.md", "# Contributor process only.\n")

    scan = ProjectScanner().scan(str(tmp_path), "fixture")

    doc_paths = {f.rel_path for f in scan.files if f.role == "document"}
    assert ".agent/README.md" in doc_paths
    assert ".agent/architecture.md" in doc_paths
    # Process-only docs are not part of the bounded orientation set.
    assert ".agent/maintenance.md" not in doc_paths
    # The bounded docs must survive eligibility (not dropped as .md docs).
    assert all(f in scan.files for f in scan.files if f.role == "document")
