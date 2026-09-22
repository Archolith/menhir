"""Menhir as a Beacon memory provider (plan Phase 3A).

The safety property: evidence is served only when its binding truthfully describes the indexed
content -- the commit and the fingerprint come from the same scan of a clean checkout. These
tests pin each link: the scan reads the binding, the upload carries it, a skipped re-scan keeps
it current, the builder refuses anything unbound, dirty, partial or changing, and the tool is a
read-only readonly-tier surface.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from menhir.core.backend_shared import _project_scan_from_dict
from menhir.infrastructure.git_binding import read_git_binding
from menhir.infrastructure.project_scanner import ProjectScanner
from menhir.services.beacon_evidence import (
    PROVIDER_EVIDENCE_VERSION,
    BeaconEvidenceError,
    build_provider_evidence,
)

COMMIT = "a" * 40


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------------------
# The scan records the binding
# ---------------------------------------------------------------------------


def test_clean_checkout_binding(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    binding = read_git_binding(root)
    assert binding == (_git(root, "rev-parse", "HEAD"), "", False)


def test_uncommitted_change_marks_the_binding_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "README.md").write_text("# Shop\nedited\n", encoding="utf-8")
    binding = read_git_binding(root)
    assert binding is not None and binding[2] is True


def test_untracked_file_marks_the_binding_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "NEW.md").write_text("new\n", encoding="utf-8")
    binding = read_git_binding(root)
    assert binding is not None and binding[2] is True


def test_non_git_directory_has_no_binding(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert read_git_binding(plain) is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:secret@git.example.com/org/shop.git",
        "https://git.example.com/org/shop.git?access_token=secret",
        "https://git.example.com/org/shop.git#secret",
    ],
)
def test_origin_credentials_are_never_recorded(tmp_path: Path, origin: str) -> None:
    root = _repo(tmp_path)
    _git(root, "remote", "add", "origin", origin)
    binding = read_git_binding(root)
    assert binding is not None
    assert binding[1] == "https://git.example.com/org/shop.git"


def test_scan_carries_the_binding_across_the_upload_boundary(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    scan = ProjectScanner().scan(root, "shop")
    assert scan.indexed_commit == _git(root, "rev-parse", "HEAD")
    assert scan.indexed_dirty is False

    restored = _project_scan_from_dict(json.loads(json.dumps(asdict(scan))))
    assert (restored.indexed_commit, restored.indexed_repository, restored.indexed_dirty) == (
        scan.indexed_commit,
        scan.indexed_repository,
        scan.indexed_dirty,
    )


def test_an_edit_after_the_walk_is_still_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing binding read comes after the scan's LAST file read (imports, symbols)."""
    import menhir.infrastructure.project_scanner as scanner_module

    root = _repo(tmp_path)
    original = scanner_module._detect_cross_project_refs

    def edit_then_detect(*args: Any, **kwargs: Any) -> Any:
        (root / "src" / "app.py").write_text("print('late edit')\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(scanner_module, "_detect_cross_project_refs", edit_then_detect)
    assert ProjectScanner().scan(root, "shop").indexed_dirty is True


def test_scan_of_a_dirty_checkout_is_marked_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert ProjectScanner().scan(root, "shop").indexed_dirty is True


# ---------------------------------------------------------------------------
# A skipped re-scan (same files, new commit) keeps the binding current
# ---------------------------------------------------------------------------


def test_watcher_refreshes_the_binding_when_files_are_unchanged(tmp_path: Path) -> None:
    from menhir.services.scheduler_tasks import refresh_structure_graphs

    root = _repo(tmp_path)
    fingerprint = ProjectScanner().scan(root, "shop").scan_fingerprint
    _git(root, "commit", "-q", "--allow-empty", "-m", "same files, new commit")
    new_head = _git(root, "rev-parse", "HEAD")

    class Adapter:
        def __init__(self) -> None:
            self.refreshed: list[tuple[str, str, str, bool]] = []
            self.wrote = False

        def list_structure_projects(self) -> list[dict[str, str]]:
            return [{"name": "shop", "root_path": str(root)}]

        def get_scan_fingerprint(self, name: str) -> str:
            return fingerprint

        def refresh_indexed_binding(
            self, name: str, fp: str, commit: str, repository: str, dirty: bool
        ) -> bool:
            assert fp == fingerprint  # the refresh is conditional on the fingerprint it read
            self.refreshed.append((name, commit, repository, dirty))
            return True

        def write_project_structure(self, *args: Any, **kwargs: Any) -> None:
            self.wrote = True

    adapter = Adapter()
    result = asyncio.run(refresh_structure_graphs(adapter))  # type: ignore[arg-type]

    assert result["skipped"] == 1
    assert adapter.refreshed == [("shop", new_head, "", False)]
    assert adapter.wrote is False


class _ProjectNode:
    """A stateful stand-in for the one project node, applying the refresh's WHERE/SET."""

    def __init__(self, **props: Any) -> None:
        self.props = props

    def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, int]]:
        p = self.props
        differs = (
            p.get("indexed_commit", "") != params["commit"]
            or p.get("indexed_repository", "") != params["repository"]
            or p.get("indexed_dirty") is None
            or p.get("indexed_dirty") != params["dirty"]
        )
        assert "n.scan_fingerprint = $fingerprint" in query
        if p.get("scan_fingerprint") == params["fingerprint"] and differs:
            p.update(
                indexed_commit=params["commit"],
                indexed_repository=params["repository"],
                indexed_dirty=params["dirty"],
            )
            return [{"updated": 1}]
        return [{"updated": 0}]


def _writer(node: _ProjectNode) -> Any:
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    return StructureGraphWriter(node)  # type: ignore[arg-type]


def test_refresh_moves_the_binding_to_the_new_commit() -> None:
    node = _ProjectNode(scan_fingerprint="fp", indexed_commit=COMMIT, indexed_dirty=False)
    assert _writer(node).refresh_indexed_binding("shop", "fp", "b" * 40, "", False) is True
    assert node.props["indexed_commit"] == "b" * 40


def test_refresh_writes_nothing_when_the_binding_is_unchanged() -> None:
    node = _ProjectNode(
        scan_fingerprint="fp", indexed_commit=COMMIT, indexed_repository="", indexed_dirty=False
    )
    assert _writer(node).refresh_indexed_binding("shop", "fp", COMMIT, "", False) is False


def test_a_stale_skip_cannot_rebind_another_scans_content() -> None:
    """A full re-scan replaced the fingerprint after the skip read it: no write."""
    node = _ProjectNode(scan_fingerprint="fp-new", indexed_commit="c" * 40, indexed_dirty=False)
    assert _writer(node).refresh_indexed_binding("shop", "fp-old", "b" * 40, "", False) is False
    assert node.props["indexed_commit"] == "c" * 40


# ---------------------------------------------------------------------------
# The builder: graph only, refuses anything it cannot vouch for
# ---------------------------------------------------------------------------


def _guard(**overrides: Any) -> dict[str, Any]:
    guard: dict[str, Any] = {
        "project_known": True,
        "ambiguous": False,
        "name": "shop",
        "root_path": "/srv/checkouts/shop",
        "scan_fingerprint": "fp-1",
        "files_discovered": 2,
        "files_eligible": 2,
        "files_indexed": 2,
        "partial_index": False,
        "indexed_commit": COMMIT,
        "indexed_repository": "https://git.example.com/org/shop.git",
        "indexed_dirty": False,
        "identity_known": True,
        "active_writers": (),
        "writer_revision": "w-1",
    }
    guard.update(overrides)
    return guard


class Reader:
    def __init__(self, *guards: dict[str, Any]) -> None:
        self.guards = list(guards)

    def get_beacon_evidence_guard_by_id(self, project_id: str) -> dict[str, Any]:
        return self.guards.pop(0) if len(self.guards) > 1 else self.guards[0]

    def query_structure(self, project: str, query_type: str, **_kwargs: Any) -> Any:
        if query_type == "overview":
            return {
                "indexed_description": "A small shop.",
                "stack": "python",
                "entities": {"file": 2},
                "edges": {"IMPORTS": 1},
            }
        if query_type == "files":
            return [{"path": "src/app.py", "role": "entrypoint", "description": ""}]
        raise AssertionError(query_type)

    def query_documents(self, project: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"structure_path": "README.md", "title": "Shop", "doc_type": None},
            # Written by ingest_document, outside any scan: never served under the binding.
            {"path": "doc:notes", "title": "Notes", "source": "document-ingest"},
        ]


def test_builder_serves_bound_evidence_without_status() -> None:
    evidence = build_provider_evidence(Reader(_guard()), "pid-1")

    assert evidence["evidence_version"] == PROVIDER_EVIDENCE_VERSION == "1.1"
    assert evidence["binding"] == {
        "provider": "menhir",
        "project_id": "pid-1",
        "repository": "https://git.example.com/org/shop.git",
        "indexed_commit": COMMIT,
    }
    assert "status" not in evidence["project"]
    assert "root" not in evidence["project"]
    assert evidence["project"]["description"] == "A small shop."
    assert [d["path"] for d in evidence["documents"]] == ["README.md"]
    assert evidence["files"][0]["path"] == "src/app.py"


@pytest.mark.parametrize(
    ("guard", "reason"),
    [
        ({"project_known": False}, "no indexed project"),
        ({"project_known": True, "ambiguous": True}, "more than one"),
        (_guard(partial_index=True), "partial"),
        (_guard(active_writers=("w",)), "being updated"),
        (_guard(indexed_commit=""), "not indexed from a git checkout"),
        (_guard(indexed_dirty=True), "uncommitted changes"),
        (_guard(indexed_dirty=None), "uncommitted changes"),
        (_guard(scan_fingerprint=""), "no scan fingerprint"),
    ],
    ids=["unknown", "ambiguous", "partial", "writer", "no-commit", "dirty", "dirty-unknown", "no-fp"],
)
def test_builder_refuses_what_it_cannot_vouch_for(guard: dict[str, Any], reason: str) -> None:
    with pytest.raises(BeaconEvidenceError, match=reason):
        build_provider_evidence(Reader(guard), "pid-1")


@pytest.mark.parametrize("key", ["scan_fingerprint", "writer_revision", "indexed_commit"])
def test_builder_refuses_a_reindex_during_the_read(key: str) -> None:
    changed = _guard(**{key: "changed" if key != "indexed_commit" else "b" * 40})
    with pytest.raises(BeaconEvidenceError, match="re-indexed while evidence was read"):
        build_provider_evidence(Reader(_guard(), changed), "pid-1")


def test_builder_never_touches_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    import menhir.services.beacon_evidence as module

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the provider must not read the checkout or run git")

    monkeypatch.setattr(module, "read_git_state", forbidden)
    monkeypatch.setattr(module, "_git_output", forbidden)
    monkeypatch.setattr(module.ProjectScanner, "scan", forbidden)
    build_provider_evidence(Reader(_guard()), "pid-1")


# ---------------------------------------------------------------------------
# The MCP tool
# ---------------------------------------------------------------------------


def test_tool_is_a_readonly_read_only_surface() -> None:
    from menhir.mcp.tools import ALL_TOOLS
    from menhir.mcp.tools.ops.get_beacon_evidence import GetBeaconEvidenceTool

    assert GetBeaconEvidenceTool in ALL_TOOLS
    assert GetBeaconEvidenceTool.required_tier == "readonly"
    assert GetBeaconEvidenceTool.oauth_scopes == ("menhir:read",)
    assert GetBeaconEvidenceTool.read_only_hint is True
    assert GetBeaconEvidenceTool.destructive_hint is False


def test_tool_reports_a_refusal_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.mcp.tools.ops.get_beacon_evidence import GetBeaconEvidenceTool

    class Backend:
        async def query_structure(self, project: str, query_type: str) -> dict[str, str]:
            assert (project, query_type) == ("pid-1", "beacon_evidence")
            return {"error": "project index is partial; complete an ingest first"}

    tool = GetBeaconEvidenceTool()
    monkeypatch.setattr(tool, "get_backend", lambda: Backend())
    with pytest.raises(ValueError, match="partial"):
        asyncio.run(tool.endpoint(" pid-1 "))
