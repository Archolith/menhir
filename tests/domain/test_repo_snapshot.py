"""Tests for repo snapshot anchors + durable file identity (Chronostratum Rung 4.5 / Rev 5)."""

from __future__ import annotations

from menhir.domain.git_staleness import GitChange
from menhir.domain.repo_snapshot import (
    FileIdentityResolver,
    RepoSnapshot,
    reconcile_snapshot,
)


def test_resolve_is_stable_for_same_path() -> None:
    r = FileIdentityResolver()
    a = r.resolve("src/Service.java")
    b = r.resolve("src/Service.java")
    assert a == b
    assert r.id_for("src/Service.java") == a


def test_distinct_paths_get_distinct_ids() -> None:
    r = FileIdentityResolver()
    assert r.resolve("a.py") != r.resolve("b.py")


def test_rename_keeps_file_id_and_repoints_path() -> None:
    """Identity != location: a rename must keep the durable id so history stays attached."""
    r = FileIdentityResolver()
    fid = r.resolve("old/Service.java")
    moved = r.apply_rename("old/Service.java", "new/Service.java")
    assert moved == fid                               # same node
    assert r.id_for("new/Service.java") == fid        # path re-pointed
    assert r.id_for("old/Service.java") is None        # old path gone


def test_rename_of_unseen_path_is_first_sight() -> None:
    r = FileIdentityResolver()
    fid = r.apply_rename("never/seen.py", "now/here.py")
    assert r.id_for("now/here.py") == fid


def test_namespacing_keeps_ids_project_scoped() -> None:
    a = FileIdentityResolver(namespace="yawn.seed").resolve("Service.java")
    b = FileIdentityResolver(namespace="yawn.market").resolve("Service.java")
    assert a != b


def test_reconcile_snapshot_joins_changes_to_durable_ids() -> None:
    r = FileIdentityResolver(namespace="yawn.seed")
    first = r.resolve("LocalDataSourceService.java")  # pre-existing identity
    snap = RepoSnapshot(
        head="abc123", branch="main", captured_at="2026-06-28",
        changes=(
            GitChange("LocalDataSourceService.java", "2026-06-28", commit="c9940ae"),
            GitChange("CardIngestMapper.java", "2026-06-28", commit="c9940ae"),
        ),
    )
    reconciled = reconcile_snapshot(snap, r)
    by_path = {rc.path: rc.file_id for rc in reconciled}
    assert by_path["LocalDataSourceService.java"] == first   # reused, not re-minted
    assert by_path["CardIngestMapper.java"].startswith("yawn.seed:file:")


def test_reconcile_snapshot_follows_rename_keeping_id() -> None:
    r = FileIdentityResolver()
    fid = r.resolve("old/path/Service.java")
    snap = RepoSnapshot(
        head="h", branch="main", captured_at="2026-06-28",
        changes=(GitChange("new/path/Service.java", "2026-06-28", renamed_from="old/path/Service.java"),),
    )
    reconciled = reconcile_snapshot(snap, r)
    assert reconciled[0].file_id == fid               # history follows the move
    assert r.id_for("new/path/Service.java") == fid
