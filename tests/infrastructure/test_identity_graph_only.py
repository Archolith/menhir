"""Project identity lives only in the graph: Menhir writes nothing into a checkout.

The property: a scan writes under identity I for directory D on host H only if an operator chose
I for D in this call, or H's active binding for D is I AND D is the checkout that binding
recorded -- same ``origin`` (``""`` for none), or, for a legacy binding that recorded none, a
legacy ``.agent/project-id`` naming I. Everything else is a decision. The legacy file is only
ever read.

Each test is a counterexample to one way of breaking that: a first ingest dirtying the checkout,
a re-clone of another repository absorbed into the old silo, a legacy binding trusted without
proof, a legacy file modified or treated as fatal, a moved checkout silently re-minted, the
scanner indexing Menhir's own state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.infrastructure.project_identity_binding import root_key_for

SHOP = "https://git.example.com/org/shop.git"


@pytest.fixture(autouse=True)
def _host_h1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("menhir.infrastructure.project_identity_binding._host", lambda: "h1")


def _settle(root: Path, graph, **kw):
    from menhir.services.project_identity_service import settle_project_identity

    return settle_project_identity(
        SimpleNamespace(neo4j=graph), root_path=str(root), display_name="shop", **kw
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _checkout(root: Path, origin: str | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _untouched(root: Path) -> bool:
    status = _git(root, "status", "--porcelain", "--ignored", "--untracked-files=all")
    return status == "" and not (root / ".agent").exists()


def _legacy_file(root: Path, project_id: str) -> Path:
    path = root / ".agent" / "project-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "project_id": project_id}), encoding="utf-8")
    return path


def _legacy_binding(graph, project_id: str, root: Path | str, **extra) -> None:
    graph.nodes[project_id] = {
        "canonical_root_path": str(root),
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for(str(root)),
        **extra,
    }


def _remove_tree(path: Path) -> None:
    def retry_writable(func, target, _exc):
        Path(target).chmod(0o700)
        func(target)

    shutil.rmtree(path, onerror=retry_writable)


@pytest.mark.unit
def test_first_ingest_leaves_the_checkout_untouched(fake_identity_graph, tmp_path):
    root = _checkout(tmp_path / "shop", SHOP)

    claim, _ = _settle(root, fake_identity_graph, identity_action="new")

    assert claim is not None
    assert _untouched(root)
    assert fake_identity_graph.nodes[claim.project_id]["bound_repository"] == SHOP


@pytest.mark.unit
def test_a_bound_checkout_resolves_with_no_file_and_no_decision(fake_identity_graph, tmp_path):
    root = _checkout(tmp_path / "shop", SHOP)
    first, _ = _settle(root, fake_identity_graph, identity_action="new")

    again, resolution = _settle(root, fake_identity_graph)

    assert again is not None and resolution.resolved
    assert (again.project_id, again.generation) == (first.project_id, first.generation)
    assert _untouched(root)


@pytest.mark.unit
def test_a_non_git_directory_is_bound_by_its_empty_origin(fake_identity_graph, tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    first, _ = _settle(root, fake_identity_graph, identity_action="new")

    assert fake_identity_graph.nodes[first.project_id]["bound_repository"] == ""
    assert _settle(root, fake_identity_graph)[0].project_id == first.project_id
    assert list(root.iterdir()) == []


@pytest.mark.unit
def test_another_repository_cloned_into_a_bound_directory_needs_a_decision(
    fake_identity_graph, tmp_path
):
    """Deleted and re-cloned with another repository: the old silo must not absorb it."""
    root = tmp_path / "shop"
    _checkout(root, SHOP)
    first, _ = _settle(root, fake_identity_graph, identity_action="new")
    _remove_tree(root)
    _checkout(root, "https://git.example.com/org/other.git")

    claim, resolution = _settle(root, fake_identity_graph)

    assert claim is None and resolution.reason == "repository_changed"
    assert [c.project_id for c in resolution.candidates] == [first.project_id]
    assert fake_identity_graph.nodes[first.project_id]["bound_repository"] == SHOP


@pytest.mark.unit
def test_operator_adopt_records_the_current_repository(fake_identity_graph, tmp_path):
    root = _checkout(tmp_path / "shop", SHOP)
    first, _ = _settle(root, fake_identity_graph, identity_action="new")
    renamed = "https://git.example.com/org/renamed.git"
    _git(root, "remote", "set-url", "origin", renamed)
    assert _settle(root, fake_identity_graph)[0] is None

    adopted, _ = _settle(
        root, fake_identity_graph, identity_action="adopt", adopt_project_id=first.project_id
    )

    assert adopted is not None and adopted.project_id == first.project_id
    assert fake_identity_graph.nodes[first.project_id]["bound_repository"] == renamed
    assert _settle(root, fake_identity_graph)[0] is not None
    assert _untouched(root)


@pytest.mark.unit
def test_a_legacy_binding_is_verified_once_by_its_file_which_is_never_touched(
    fake_identity_graph, tmp_path
):
    root = _checkout(tmp_path / "shop", SHOP)
    _legacy_binding(fake_identity_graph, "legacy-id", root, claim_generation=3)
    legacy = _legacy_file(root, "legacy-id")
    before = (legacy.read_bytes(), legacy.stat().st_mtime_ns)

    claim, _ = _settle(root, fake_identity_graph)

    assert claim is not None and (claim.project_id, claim.generation) == ("legacy-id", 3)
    assert (legacy.read_bytes(), legacy.stat().st_mtime_ns) == before
    assert fake_identity_graph.nodes["legacy-id"]["bound_repository"] == SHOP
    legacy.unlink()  # users may delete it: the recorded repository is the proof from now on
    assert _settle(root, fake_identity_graph)[0] is not None


@pytest.mark.unit
def test_a_legacy_binding_without_a_matching_file_needs_a_decision(fake_identity_graph, tmp_path):
    root = _checkout(tmp_path / "shop", SHOP)
    _legacy_binding(fake_identity_graph, "legacy-id", root)
    _legacy_file(root, "someone-else")

    claim, resolution = _settle(root, fake_identity_graph)

    assert claim is None and resolution.reason == "legacy_binding_unverified"
    assert "bound_repository" not in fake_identity_graph.nodes["legacy-id"]


@pytest.mark.unit
def test_a_malformed_legacy_file_is_ignored_not_fatal(fake_identity_graph, tmp_path):
    root = _checkout(tmp_path / "shop", None)
    _legacy_binding(fake_identity_graph, "legacy-id", root)
    (root / ".agent").mkdir()
    (root / ".agent" / "project-id").write_text("{not json", encoding="utf-8")

    claim, resolution = _settle(root, fake_identity_graph)

    assert claim is None and resolution.reason == "legacy_binding_unverified"
    assert (root / ".agent" / "project-id").read_text(encoding="utf-8") == "{not json"


@pytest.mark.unit
def test_a_moved_checkout_offers_its_legacy_id_for_adopt(fake_identity_graph, tmp_path):
    _legacy_binding(fake_identity_graph, "moved-id", tmp_path / "old-place")
    root = _checkout(tmp_path / "new-place", SHOP)
    _legacy_file(root, "moved-id")

    claim, resolution = _settle(root, fake_identity_graph)

    assert claim is None and resolution.reason == "directory_not_bound"
    assert [c.project_id for c in resolution.candidates] == ["moved-id"]


@pytest.mark.unit
def test_the_scanner_never_lists_a_legacy_identity_file(tmp_path):
    from menhir.infrastructure.project_scanner import ProjectScanner

    root = _checkout(tmp_path / "shop", None)
    _legacy_file(root, "legacy-id")

    scan = ProjectScanner().scan(root, "shop")

    assert ".agent/project-id" not in {f.rel_path for f in scan.files}
