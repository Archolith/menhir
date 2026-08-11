"""Repository, scanner, and service behaviour for source reconciliation.

The planner tests prove what should happen. These prove the writes are
conditional, the scanner reads what is actually on disk, and apply refuses a
stale ledger before it touches anything.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    ArtifactSourceSnapshot,
    ConflictKind,
    CorpusLane,
    MatchBasis,
    ReconciliationAction,
    ResolutionStatus,
    SourceObservation,
    WorkArtifactIdentitySnapshot,
    sha256_bytes,
)
from menhir.domain.work_artifact import ArtifactType
from menhir.infrastructure.artifact_corpus_scanner import (
    GitEvidence,
    collect_git_evidence,
    parse_rename_status,
    read_index_links,
    scan_corpus,
)
from menhir.infrastructure.schema import ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
from menhir.services.artifact_reconciliation_service import (
    ArtifactReconciliationService,
)


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []

    @property
    def writes(self) -> list[dict]:
        return [
            c for c in self.calls if " SET " in c["query"] or "CREATE " in c["query"]
        ]


OBSERVATION = SourceObservation(
    integrity="abc123",
    size_bytes=42,
    lane=CorpusLane.ARCHIVE,
    observed_at="2026-08-11T00:00:00+00:00",
    basis=MatchBasis.GIT_RENAME,
)


# ---------------------------------------------------------------------------
# Conditional writes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_relocation_updates_one_source_and_no_relationship_endpoints() -> None:
    neo = _StubNeo4j(
        responses=[
            [{"blockers": 0, "at_old": True, "fresh": True, "artifact_uuid": "a-1"}]
        ]
    )
    repo = WorkArtifactRepository(neo)
    result = repo.relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "new.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result["applied"] is True
    query = neo.calls[0]["query"]
    assert "SET s += $props" in query
    assert "DELETE" not in query and "MERGE" not in query
    assert "CASE WHEN other IS NOT NULL" in query
    props = neo.calls[0]["params"]["props"]
    assert props["locator_path"] == "new.md"
    assert props["current_locator_key"] == "menhir|markdown|new.md"
    assert props["schema_version"] == 2


@pytest.mark.unit
def test_a_claimed_destination_is_refused() -> None:
    neo = _StubNeo4j(
        responses=[
            [{"blockers": 1, "at_old": True, "fresh": True, "artifact_uuid": "a-1"}]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "taken.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "destination_already_claimed"}


@pytest.mark.unit
def test_a_stale_expected_integrity_is_refused() -> None:
    neo = _StubNeo4j(
        responses=[
            [{"blockers": 0, "at_old": True, "fresh": False, "artifact_uuid": "a-1"}]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "new.md", "medium": "markdown"},
        observation=OBSERVATION,
        expected_integrity="stale",
    )
    assert result["reason"] == "stale_expected_integrity"


@pytest.mark.unit
def test_a_source_that_moved_since_the_audit_is_refused() -> None:
    neo = _StubNeo4j(
        responses=[
            [{"blockers": 0, "at_old": False, "fresh": True, "artifact_uuid": "a-1"}]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "new.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result["reason"] == "stale_old_locator"


@pytest.mark.unit
def test_an_ambiguous_old_locator_refuses_rather_than_picking_one() -> None:
    neo = _StubNeo4j(responses=[[{"source_uuid": "s-1"}, {"source_uuid": "s-2"}]])
    result = WorkArtifactRepository(neo).relocate_artifact_source_by_locator(
        repository="menhir",
        medium="markdown",
        old_path="old.md",
        new_path="new.md",
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "locator_is_ambiguous"}
    assert not neo.writes


@pytest.mark.unit
def test_a_source_without_a_backfilled_uuid_refuses_rather_than_guessing() -> None:
    neo = _StubNeo4j(responses=[[{"source_uuid": None}]])
    result = WorkArtifactRepository(neo).refresh_artifact_source_by_locator(
        repository="menhir",
        medium="markdown",
        path="a.md",
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "source_uuid_not_backfilled"}


@pytest.mark.unit
def test_marking_unresolved_never_deletes_and_keeps_the_locator() -> None:
    neo = _StubNeo4j(responses=[[{"artifact_uuid": "a-1"}]])
    result = WorkArtifactRepository(neo).mark_artifact_source_unresolved(
        source_uuid="s-1",
        reason="source_not_observed",
        observed_commit="c0ffee",
    )
    assert result["applied"] is True
    query = neo.calls[0]["query"]
    assert "DELETE" not in query
    assert "locator_path" not in query
    assert neo.calls[0]["params"]["unresolved"] == ResolutionStatus.UNRESOLVED


@pytest.mark.unit
def test_registration_is_idempotent_by_declared_uuid() -> None:
    neo = _StubNeo4j(responses=[[{"uuid": "a-1"}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN,
        title="T",
        repository="menhir",
        path="a.md",
        medium="markdown",
        observation=OBSERVATION,
        artifact_uuid="a-1",
    )
    assert result == {
        "applied": False,
        "reason": "declared_uuid_already_registered",
        "artifact_uuid": "a-1",
    }
    assert not neo.writes


@pytest.mark.unit
def test_registration_is_idempotent_by_current_locator() -> None:
    neo = _StubNeo4j(responses=[[{"n": 1}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN,
        title="T",
        repository="menhir",
        path="a.md",
        medium="markdown",
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "destination_already_claimed"}
    assert not neo.writes


@pytest.mark.unit
def test_registration_reuses_a_declared_uuid_rather_than_minting_one() -> None:
    declared = "33333333-3333-4333-8333-333333333333"
    neo = _StubNeo4j(responses=[[], [{"n": 0}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN,
        title="T",
        repository="menhir",
        path="a.md",
        medium="markdown",
        observation=OBSERVATION,
        artifact_uuid=declared,
    )
    assert result["artifact_uuid"] == declared


@pytest.mark.unit
def test_an_unobserved_leg_is_left_alone_rather_than_nulled() -> None:
    """No blob OID this scan must not erase the one already recorded."""
    repo = WorkArtifactRepository(_StubNeo4j())
    props = repo._source_write_props(
        SourceObservation(integrity="abc", observed_at="2026-08-11T00:00:00+00:00")
    )
    assert "version" not in props
    assert props["integrity"] == "abc"


@pytest.mark.unit
def test_legacy_versions_are_relabelled_never_reinterpreted() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "eid": "e1",
                    "repository": "menhir",
                    "medium": "markdown",
                    "path": "a.md",
                    "version": "f441a237" * 5,
                    "version_kind": None,
                },
            ]
        ]
    )
    assert WorkArtifactRepository(neo).backfill_current_locator_keys() == 1
    params = neo.calls[1]["params"]
    assert params["version_kind"] == "legacy_commit_sha"
    assert params["key"] == "menhir|markdown|a.md"


@pytest.mark.unit
def test_preparation_does_not_resurrect_an_unresolved_v2_locator() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "eid": "e1",
                    "repository": "menhir",
                    "medium": "markdown",
                    "path": "old.md",
                    "version": "f441a237" * 5,
                    "version_kind": None,
                    "resolution_status": ResolutionStatus.UNRESOLVED,
                }
            ]
        ]
    )

    assert WorkArtifactRepository(neo).backfill_current_locator_keys() == 1
    params = neo.calls[1]["params"]
    assert params["key"] is None
    assert params["version_kind"] == "legacy_commit_sha"
    assert params["schema_version"] == 2
    assert (
        "s.resolution_status = coalesce(s.resolution_status, $resolved)"
        in neo.calls[1]["query"]
    )


@pytest.mark.unit
def test_artifact_reconciliation_preflight_reports_global_blockers() -> None:
    expected = {
        "sources": 112,
        "missing_source_uuids": 112,
        "missing_locator_keys": 112,
        "duplicate_artifact_uuids": 0,
        "duplicate_source_uuids": 0,
        "duplicate_raw_locators": 0,
        "duplicate_locator_keys": 0,
        "duplicate_cursor_repositories": 0,
    }
    neo = _StubNeo4j(responses=[[expected]])

    assert WorkArtifactRepository(neo).artifact_reconciliation_preflight() == expected
    assert "MATCH (s:ArtifactSource)" in neo.calls[0]["query"]
    assert "duplicate_raw_locators" in neo.calls[0]["query"]


@pytest.mark.unit
def test_artifact_reconciliation_schema_activation_verifies_online_indexes() -> None:
    names = list(ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS)
    neo = _StubNeo4j(responses=[[], [], [], [], [], [{"names": names}]])

    result = WorkArtifactRepository(neo).activate_artifact_reconciliation_schema()

    assert result["ready"] is True
    assert result["constraints_missing"] == []
    assert result["constraints_online"] == sorted(names)
    assert neo.calls[0]["query"] == "DROP INDEX work_artifact_uuid_idx IF EXISTS"
    assert "SHOW INDEXES" in neo.calls[-1]["query"]


@pytest.mark.unit
def test_cursor_read_is_repository_scoped() -> None:
    neo = _StubNeo4j(responses=[[{"commit": "abc123"}]])
    cursor = WorkArtifactRepository(neo).get_artifact_reconciliation_cursor(
        repository="menhir"
    )
    assert cursor == "abc123"
    assert neo.calls[0]["params"] == {"repository": "menhir"}


@pytest.mark.unit
def test_first_cursor_advance_uses_atomic_merge() -> None:
    neo = _StubNeo4j(responses=[[{"advanced": True, "current_commit": "head"}]])
    result = WorkArtifactRepository(neo).advance_artifact_reconciliation_cursor(
        repository="menhir",
        expected_commit=None,
        observed_commit="head",
        observed_at="2026-08-11T00:00:00+00:00",
    )
    assert result == {"advanced": True, "current_commit": "head"}
    assert "MERGE (c:ArtifactReconciliationCursor" in neo.calls[0]["query"]
    assert neo.calls[0]["params"]["cas_token"]


@pytest.mark.unit
def test_existing_cursor_advance_is_compare_and_set() -> None:
    neo = _StubNeo4j(responses=[])
    result = WorkArtifactRepository(neo).advance_artifact_reconciliation_cursor(
        repository="menhir",
        expected_commit="stale",
        observed_commit="head",
        observed_at="2026-08-11T00:00:00+00:00",
    )
    assert result == {"advanced": False, "current_commit": None}
    query = neo.calls[0]["query"]
    assert "commit: $expected_commit" in query
    assert "MERGE" not in query


@pytest.mark.unit
def test_declared_artifact_identity_read_includes_source_less_artifacts() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "artifact_uuid": "a-1",
                    "artifact_type": ArtifactType.PLAN,
                    "title": "T",
                    "status": "APPROVED",
                    "source_count": 0,
                }
            ]
        ]
    )
    identities = WorkArtifactRepository(neo).list_work_artifact_identities(
        artifact_uuids=["a-1"]
    )
    assert identities == [
        WorkArtifactIdentitySnapshot(
            artifact_uuid="a-1",
            artifact_type=ArtifactType.PLAN,
            title="T",
            status="APPROVED",
            source_count=0,
        )
    ]
    assert "OPTIONAL MATCH" in neo.calls[0]["query"]


@pytest.mark.unit
def test_unscoped_source_read_is_bounded_to_paths_and_declared_uuids() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "artifact_uuid": "a-1",
                    "artifact_type": ArtifactType.PLAN,
                    "source_uuid": "s-1",
                    "medium": "markdown",
                    "repository": None,
                    "path": ".agent/plans/a.md",
                }
            ]
        ]
    )
    rows = WorkArtifactRepository(neo).list_unscoped_artifact_source_snapshots(
        paths=[".agent/plans/a.md"], artifact_uuids=["a-1"]
    )
    assert len(rows) == 1 and rows[0].repository is None
    assert neo.calls[0]["params"] == {
        "paths": [".agent/plans/a.md"],
        "artifact_uuids": ["a-1"],
    }
    assert "coalesce(trim(s.locator_repository), '') = ''" in neo.calls[0]["query"]


@pytest.mark.unit
def test_attach_source_preserves_artifact_and_creates_only_the_embodiment() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "artifact_type": ArtifactType.PLAN,
                    "source_count": 0,
                    "blockers": 0,
                    "type_matches": True,
                    "attached": True,
                }
            ]
        ]
    )
    result = WorkArtifactRepository(neo).attach_artifact_source(
        artifact_uuid="a-1",
        expected_artifact_type=ArtifactType.PLAN,
        repository="menhir",
        path=".agent/plans/a.md",
        medium="markdown",
        observation=OBSERVATION,
    )
    assert result["applied"] is True
    query = neo.calls[0]["query"]
    assert "CREATE (a)-[:EMBODIED_IN]->(s:ArtifactSource)" in query
    assert "CASE WHEN occupied IS NOT NULL" in query
    assert "SET a.title" not in query and "SET a.status" not in query
    props = neo.calls[0]["params"]["props"]
    assert props["current_locator_key"] == "menhir|markdown|.agent/plans/a.md"
    assert props["corpus_lane"] == CorpusLane.ARCHIVE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            {
                "artifact_type": ArtifactType.REVIEW,
                "source_count": 0,
                "blockers": 0,
                "type_matches": False,
                "attached": False,
            },
            "artifact_type_changed",
        ),
        (
            {
                "artifact_type": ArtifactType.PLAN,
                "source_count": 1,
                "blockers": 0,
                "type_matches": True,
                "attached": False,
            },
            "artifact_already_has_source",
        ),
        (
            {
                "artifact_type": ArtifactType.PLAN,
                "source_count": 0,
                "blockers": 1,
                "type_matches": True,
                "attached": False,
            },
            "destination_already_claimed",
        ),
        (
            {
                "artifact_type": ArtifactType.PLAN,
                "source_count": 0,
                "blockers": 1,
                "unscoped_blockers": 1,
                "type_matches": True,
                "attached": False,
            },
            "unscoped_source_claims_destination",
        ),
    ],
)
def test_attach_source_refuses_changed_premises(row: dict, reason: str) -> None:
    neo = _StubNeo4j(responses=[[row]])
    result = WorkArtifactRepository(neo).attach_artifact_source(
        artifact_uuid="a-1",
        expected_artifact_type=ArtifactType.PLAN,
        repository="menhir",
        path=".agent/plans/a.md",
        medium="markdown",
        observation=OBSERVATION,
    )
    assert result["applied"] is False
    assert result["reason"] == reason


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


@pytest.mark.unit
def test_the_scan_reaches_backlog_records_the_old_one_level_scan_missed(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".agent/plans/top.md", "# Top\n")
    _write(tmp_path, ".agent/plans/backlog/deep.md", "# Deep\n")
    _write(tmp_path, ".agent/plans/README.md", "# Index\n")
    _write(tmp_path, ".agent/plans/backlog/proto.py", "print(1)\n")

    entries = scan_corpus(tmp_path, repository="t")
    paths = {e.path for e in entries}
    assert paths == {".agent/plans/top.md", ".agent/plans/backlog/deep.md"}
    lanes = {e.path: e.lane for e in entries}
    assert lanes[".agent/plans/backlog/deep.md"] == CorpusLane.BACKLOG


@pytest.mark.unit
def test_a_dirty_working_tree_file_records_integrity_with_no_blob(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    entry = scan_corpus(tmp_path, repository="t")[0]
    assert entry.integrity == sha256_bytes(b"# A\n")
    assert entry.version is None
    assert entry.version_kind is None


@pytest.mark.unit
def test_declared_metadata_reaches_the_entry(tmp_path: Path) -> None:
    uuid = "44444444-4444-4444-8444-444444444444"
    _write(
        tmp_path,
        ".agent/plans/a.md",
        f"---\nartifact_uuid: {uuid}\nartifact_type: plan\n---\n# A Plan\n",
    )
    entry = scan_corpus(tmp_path, repository="t")[0]
    assert entry.declared_uuid == uuid
    assert entry.declared_type == ArtifactType.PLAN
    assert entry.title == "A Plan"
    assert entry.title_from_h1


@pytest.mark.unit
def test_only_git_recognized_renames_are_parsed() -> None:
    diff = (
        "R100\t.agent/plans/a.md\t.agent/archive/plans/a.md\n"
        "D\t.agent/plans/b.md\n"
        "A\t.agent/archive/plans/b.md\n"
        "M\t.agent/plans/c.md\n"
    )
    renames = parse_rename_status(diff)
    assert len(renames) == 1
    assert renames[0].old_path == ".agent/plans/a.md"
    assert renames[0].new_path == ".agent/archive/plans/a.md"


@pytest.mark.unit
def test_a_directory_that_is_not_a_repository_still_scans(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    evidence = collect_git_evidence(tmp_path)
    assert evidence.available is False
    assert scan_corpus(tmp_path, repository="t", git=evidence)


@pytest.mark.unit
def test_index_links_are_read_from_the_directory_readme(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/README.md", "- [A](a.md)\n- b.md\n")
    links = read_index_links(tmp_path, ".agent/plans")
    assert links == frozenset({"a.md", "b.md"})


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class _RecordingRepo:
    """Records every call so a test can assert what the service did and did not do."""

    def __init__(
        self, snapshots=(), *, unscoped=(), identities=(), cursor: str | None = None
    ) -> None:
        self.snapshots = list(snapshots)
        self.unscoped = list(unscoped)
        self.identities = list(identities)
        self.cursor = cursor
        self.calls: list[tuple[str, dict]] = []

    def get_artifact_reconciliation_cursor(self, *, repository: str):
        self.calls.append(("cursor", {"repository": repository}))
        return self.cursor

    def advance_artifact_reconciliation_cursor(self, **kwargs):
        self.calls.append(("advance_cursor", kwargs))
        if self.cursor != kwargs["expected_commit"]:
            return {"advanced": False, "current_commit": self.cursor}
        self.cursor = kwargs["observed_commit"]
        return {"advanced": True, "current_commit": self.cursor}

    def list_artifact_source_snapshots(self, *, repository: str | None = None):
        self.calls.append(("list", {"repository": repository}))
        return list(self.snapshots)

    def list_unscoped_artifact_source_snapshots(self, *, paths, artifact_uuids):
        self.calls.append(
            (
                "unscoped",
                {
                    "paths": paths,
                    "artifact_uuids": artifact_uuids,
                },
            )
        )
        return list(self.unscoped)

    def list_work_artifact_identities(self, *, artifact_uuids):
        self.calls.append(("identities", {"artifact_uuids": artifact_uuids}))
        return list(self.identities)

    def register_work_artifact(self, **kwargs):
        self.calls.append(("register", kwargs))
        return {"applied": True, "artifact_uuid": "new"}

    def attach_artifact_source(self, **kwargs):
        self.calls.append(("attach", kwargs))
        return {"applied": True, "artifact_uuid": kwargs["artifact_uuid"]}

    def refresh_artifact_source(self, **kwargs):
        self.calls.append(("refresh", kwargs))
        return {"applied": True}

    def relocate_artifact_source(self, **kwargs):
        self.calls.append(("relocate", kwargs))
        return {"applied": True}

    def mark_artifact_source_unresolved(self, **kwargs):
        self.calls.append(("unresolved", kwargs))
        return {"applied": True}


class _PreparationRepo:
    def __init__(self, before: dict, after: dict | None = None) -> None:
        self.before = before
        self.after = after or before
        self.preflight_reads = 0
        self.calls: list[str] = []

    def artifact_reconciliation_preflight(self):
        self.calls.append("preflight")
        self.preflight_reads += 1
        return self.before if self.preflight_reads == 1 else self.after

    def backfill_source_uuids(self):
        self.calls.append("source_uuids")
        return self.before["missing_source_uuids"]

    def backfill_current_locator_keys(self):
        self.calls.append("locator_keys")
        return self.before["missing_locator_keys"]

    def activate_artifact_reconciliation_schema(self):
        self.calls.append("schema")
        return {
            "queries_executed": 5,
            "constraints_online": list(ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS),
            "constraints_missing": [],
            "ready": True,
        }


def _preflight(**overrides: int) -> dict[str, int]:
    result = {
        "sources": 112,
        "missing_source_uuids": 112,
        "missing_locator_keys": 112,
        "duplicate_artifact_uuids": 0,
        "duplicate_source_uuids": 0,
        "duplicate_raw_locators": 0,
        "duplicate_locator_keys": 0,
        "duplicate_cursor_repositories": 0,
    }
    result.update(overrides)
    return result


@pytest.mark.unit
def test_prepare_refuses_changed_global_source_count_before_writes() -> None:
    repo = _PreparationRepo(_preflight())

    with pytest.raises(ValueError, match="expected 111, observed 112"):
        ArtifactReconciliationService(repo).prepare_sources(expected_source_count=111)

    assert repo.calls == ["preflight"]


@pytest.mark.unit
def test_prepare_refuses_duplicate_locator_before_writes() -> None:
    repo = _PreparationRepo(_preflight(duplicate_raw_locators=1))

    with pytest.raises(ValueError, match="duplicate_raw_locators"):
        ArtifactReconciliationService(repo).prepare_sources(expected_source_count=112)

    assert repo.calls == ["preflight"]


@pytest.mark.unit
def test_prepare_backfills_then_activates_and_verifies_constraints() -> None:
    repo = _PreparationRepo(
        _preflight(),
        _preflight(missing_source_uuids=0, missing_locator_keys=0),
    )

    result = ArtifactReconciliationService(repo).prepare_sources(
        expected_source_count=112
    )

    assert repo.calls == [
        "preflight",
        "source_uuids",
        "locator_keys",
        "schema",
        "preflight",
    ]
    assert result["stamped"] == {"source_uuids": 112, "locator_keys": 112}
    assert result["schema"]["ready"] is True


@pytest.mark.unit
def test_audit_performs_no_write_calls(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    ArtifactReconciliationService(repo).audit(tmp_path, repository="t")
    assert [name for name, _ in repo.calls] == ["cursor", "list", "unscoped"]


@pytest.mark.unit
def test_graph_backed_audit_requires_explicit_repository_identity(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    with pytest.raises(ValueError, match="repository is required"):
        ArtifactReconciliationService(repo).audit(tmp_path)
    assert repo.calls == []


@pytest.mark.unit
def test_apply_with_the_wrong_digest_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path, expected_digest="not-the-digest", repository="t"
    )
    assert result.ok is False
    assert result.refused_reason == "plan_digest_mismatch"
    assert [name for name, _ in repo.calls] == ["cursor", "list", "unscoped"]


@pytest.mark.unit
def test_apply_refuses_implicit_first_repository_registration(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    service = ArtifactReconciliationService(_RecordingRepo())
    digest = service.audit(tmp_path, repository="t").plan_digest

    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path, expected_digest=digest, repository="t"
    )
    assert result.ok is False
    assert result.refused_reason == "new_repository_requires_explicit_allow"
    assert [name for name, _ in repo.calls] == ["cursor", "list", "unscoped", "cursor"]


@pytest.mark.unit
def test_explicit_first_repository_registration_can_apply(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    service = ArtifactReconciliationService(_RecordingRepo())
    digest = service.audit(tmp_path, repository="t").plan_digest

    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path,
        expected_digest=digest,
        repository="t",
        allow_new_repository=True,
    )
    assert result.ok
    assert len(result.applied) == 1
    assert [name for name, _ in repo.calls] == [
        "cursor",
        "list",
        "unscoped",
        "cursor",
        "register",
    ]


@pytest.mark.unit
def test_existing_source_less_declared_uuid_dispatches_attach_not_register(
    tmp_path: Path,
) -> None:
    declared = "33333333-3333-4333-8333-333333333333"
    _write(
        tmp_path,
        ".agent/plans/a.md",
        f"---\nartifact_uuid: {declared}\nartifact_type: plan\n---\n# A\n",
    )
    identity = WorkArtifactIdentitySnapshot(
        artifact_uuid=declared,
        artifact_type=ArtifactType.PLAN,
        source_count=0,
    )
    repo = _RecordingRepo(identities=[identity])
    service = ArtifactReconciliationService(repo)
    report = service.audit(tmp_path, repository="t")
    assert [action.kind for action in report.safe_actions] == [ActionKind.ATTACH_SOURCE]

    repo.calls.clear()
    result = service.apply(
        tmp_path,
        expected_digest=report.plan_digest,
        repository="t",
    )
    assert result.ok
    assert len(result.applied) == 1
    names = [name for name, _ in repo.calls]
    assert "attach" in names
    assert "register" not in names


@pytest.mark.unit
def test_declared_uuid_dispatches_unscoped_repository_adoption(tmp_path: Path) -> None:
    declared = "44444444-4444-4444-8444-444444444444"
    _write(
        tmp_path,
        ".agent/plans/a.md",
        f"---\nartifact_uuid: {declared}\nartifact_type: plan\n---\n# A\n",
    )
    unscoped = ArtifactSourceSnapshot(
        artifact_uuid=declared,
        artifact_type=ArtifactType.PLAN,
        source_uuid="legacy-source",
        medium="markdown",
        repository=None,
        path=".agent/plans/a.md",
        integrity=sha256_bytes(
            f"---\nartifact_uuid: {declared}\nartifact_type: plan\n---\n# A\n".encode()
        ),
    )
    repo = _RecordingRepo(unscoped=[unscoped])
    service = ArtifactReconciliationService(repo)
    report = service.audit(tmp_path, repository="t")
    assert [a.kind for a in report.safe_actions] == [ActionKind.ADOPT_SOURCE_REPOSITORY]

    repo.calls.clear()
    result = service.apply(
        tmp_path,
        expected_digest=report.plan_digest,
        repository="t",
    )
    assert result.ok
    relocation = next(kwargs for name, kwargs in repo.calls if name == "relocate")
    assert relocation["old_locator"]["repository"] == ""
    assert relocation["new_locator"]["repository"] == "t"
    assert not any(name == "register" for name, _ in repo.calls)


@pytest.mark.unit
def test_manual_unscoped_adoption_uses_source_uuid_and_null_old_repository(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).adopt_source_repository_manually(
        source_uuid="legacy-source",
        repository="t",
        medium="markdown",
        old_path=".agent/plans/legacy.md",
        new_path=".agent/plans/a.md",
        repo_root=tmp_path,
    )
    assert result["applied"] is True
    kwargs = next(kwargs for name, kwargs in repo.calls if name == "relocate")
    assert kwargs["source_uuid"] == "legacy-source"
    assert kwargs["old_locator"]["repository"] == ""
    assert kwargs["new_locator"]["repository"] == "t"
    assert kwargs["observation"].lane == CorpusLane.ACTIVE


@pytest.mark.unit
def test_audit_uses_the_persisted_cursor_as_git_evidence_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    seen: list[str | None] = []

    def fake_git(root: Path, *, from_commit: str | None = None) -> GitEvidence:
        seen.append(from_commit)
        return GitEvidence(
            observed_commit="head", available=True, rename_evidence_available=True
        )

    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        fake_git,
    )
    report = ArtifactReconciliationService(_RecordingRepo(cursor="stored")).audit(
        tmp_path, repository="t"
    )
    assert seen == ["stored"]
    assert report.cursor_commit == "stored"
    assert report.evidence_from_commit == "stored"


@pytest.mark.unit
def test_explicit_from_commit_overrides_only_the_git_evidence_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    seen: list[str | None] = []

    def fake_git(root: Path, *, from_commit: str | None = None) -> GitEvidence:
        seen.append(from_commit)
        return GitEvidence(
            observed_commit="head", available=True, rename_evidence_available=True
        )

    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        fake_git,
    )
    report = ArtifactReconciliationService(_RecordingRepo(cursor="stored")).audit(
        tmp_path, repository="t", from_commit="override"
    )
    assert seen == ["override"]
    assert report.cursor_commit == "stored"
    assert report.evidence_from_commit == "override"


@pytest.mark.unit
def test_clean_apply_advances_cursor_to_the_observed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        lambda *_args, **_kwargs: GitEvidence(observed_commit="head", available=True),
    )
    repo = _RecordingRepo()
    service = ArtifactReconciliationService(repo)
    digest = service.audit(tmp_path, repository="t").plan_digest
    repo.calls.clear()

    result = service.apply(
        tmp_path,
        expected_digest=digest,
        repository="t",
        allow_new_repository=True,
    )
    assert result.ok
    assert result.cursor_advanced is True
    assert repo.cursor == "head"
    assert [name for name, _ in repo.calls] == [
        "cursor",
        "list",
        "unscoped",
        "cursor",
        "register",
        "advance_cursor",
    ]


@pytest.mark.unit
def test_apply_reports_a_cursor_cas_race_after_completed_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        lambda *_args, **_kwargs: GitEvidence(observed_commit="head", available=True),
    )

    class _FailedAdvanceRepo(_RecordingRepo):
        def advance_artifact_reconciliation_cursor(self, **kwargs):
            self.calls.append(("advance_cursor", kwargs))
            return {"advanced": False, "current_commit": "other"}

    repo = _FailedAdvanceRepo()
    service = ArtifactReconciliationService(repo)
    digest = service.audit(tmp_path, repository="t").plan_digest
    result = service.apply(
        tmp_path,
        expected_digest=digest,
        repository="t",
        allow_new_repository=True,
    )
    assert len(result.applied) == 1
    assert result.ok is False
    assert result.refused_reason == "reconciliation_cursor_update_failed"
    assert result.cursor_reason == "compare_and_set_failed"


@pytest.mark.unit
def test_apply_refuses_when_cursor_changes_after_reaudit(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    approved = (
        ArtifactReconciliationService(_RecordingRepo(cursor="c1"))
        .audit(tmp_path, repository="t")
        .plan_digest
    )

    class _MovingCursorRepo(_RecordingRepo):
        def __init__(self) -> None:
            super().__init__(cursor="c1")
            self.reads = 0

        def get_artifact_reconciliation_cursor(self, *, repository: str):
            self.reads += 1
            self.calls.append(("cursor", {"repository": repository}))
            return "c1" if self.reads == 1 else "c2"

    repo = _MovingCursorRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path,
        expected_digest=approved,
        repository="t",
        allow_new_repository=True,
    )
    assert result.refused_reason == "reconciliation_cursor_changed"
    assert not any(name == "register" for name, _ in repo.calls)


@pytest.mark.unit
def test_apply_refuses_an_unavailable_git_evidence_base_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        lambda *_args, **_kwargs: GitEvidence(
            observed_commit="head",
            available=True,
            rename_evidence_available=False,
        ),
    )
    repo = _RecordingRepo(cursor="missing-or-diverged")
    service = ArtifactReconciliationService(repo)
    report = service.audit(tmp_path, repository="t")
    assert report.evidence_base_valid is False

    repo.calls.clear()
    result = service.apply(
        tmp_path,
        expected_digest=report.plan_digest,
        repository="t",
        allow_new_repository=True,
    )
    assert result.refused_reason == "git_evidence_base_unavailable"
    assert result.cursor_reason == "invalid_evidence_base"
    assert not any(name == "register" for name, _ in repo.calls)


@pytest.mark.unit
def test_a_conflict_does_not_block_unrelated_safe_actions(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/good.md", "# Good\n")
    _write(tmp_path, ".agent/plans/bad.md", "---\nartifact_uuid: nope\n---\n# Bad\n")
    service = ArtifactReconciliationService(_RecordingRepo())
    digest = service.audit(tmp_path, repository="t").plan_digest

    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path,
        expected_digest=digest,
        repository="t",
        allow_new_repository=True,
    )
    assert len(result.conflicted) == 1
    assert len(result.applied) == 1
    assert result.applied[0]["path"] == ".agent/plans/good.md"
    assert result.cursor_advanced is False
    assert result.cursor_reason == "conflicts_present"


@pytest.mark.unit
def test_source_less_unclassified_conflict_allows_cursor_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".agent/reference/context.md", "# Context\n")
    evidence = GitEvidence(
        observed_commit="head",
        available=True,
        rename_evidence_available=True,
    )
    monkeypatch.setattr(
        "menhir.services.artifact_reconciliation_service.collect_git_evidence",
        lambda *_args, **_kwargs: evidence,
    )
    repo = _RecordingRepo()
    service = ArtifactReconciliationService(repo)
    report = service.audit(tmp_path, repository="t")
    assert [action.conflict_kind for action in report.conflicts] == [
        ConflictKind.UNCLASSIFIED_NEW_SOURCE
    ]

    result = service.apply(
        tmp_path,
        expected_digest=report.plan_digest,
        repository="t",
    )

    assert len(result.conflicted) == 1
    assert result.cursor_advanced is True
    assert result.cursor_reason == "advanced_with_acknowledged_conflicts"
    assert repo.cursor == "head"


@pytest.mark.unit
def test_identity_bearing_unclassified_conflict_still_blocks_cursor() -> None:
    action = ReconciliationAction(
        kind=ActionKind.CONFLICT,
        conflict_kind=ConflictKind.UNCLASSIFIED_NEW_SOURCE,
        repository="t",
        path=".agent/reference/context.md",
        source_uuid="source-1",
    )

    assert ArtifactReconciliationService._conflict_allows_cursor_advance(action) is False


@pytest.mark.unit
def test_validation_reports_a_duplicate_uuid_across_two_documents(
    tmp_path: Path,
) -> None:
    uuid = "55555555-5555-4555-8555-555555555555"
    body = f"---\nartifact_uuid: {uuid}\nartifact_type: plan\n---\n# T\n"
    _write(tmp_path, ".agent/plans/a.md", body)
    _write(tmp_path, ".agent/plans/b.md", body)
    report = ArtifactReconciliationService(_RecordingRepo()).validate(
        tmp_path, repository="t"
    )
    codes = {f.code for f in report.findings}
    assert "duplicate_artifact_uuid" in codes
    assert report.ok is False


@pytest.mark.unit
def test_validation_needs_no_graph_connection(tmp_path: Path) -> None:
    class _Exploding:
        def list_artifact_source_snapshots(self, *, repository=None):
            raise AssertionError("validation must not read the graph")

    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    report = ArtifactReconciliationService(_Exploding()).validate(
        tmp_path, repository="t"
    )
    assert report.checked == 1


@pytest.mark.unit
def test_a_document_with_no_h1_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "no heading here\n")
    report = ArtifactReconciliationService(_RecordingRepo()).validate(
        tmp_path, repository="t"
    )
    assert "missing_h1_title" in {f.code for f in report.findings}


# ---------------------------------------------------------------------------
# Live-ish: a real temporary Git repository
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.mark.unit
def test_a_rename_plus_edit_in_one_commit_is_still_recognized(tmp_path: Path) -> None:
    """Git's own rename detection is the evidence; byte equality is not required."""
    try:
        _git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")

    original = "# A Plan\n\n" + ("body line\n" * 40)
    _write(tmp_path, ".agent/plans/a.md", original)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    (tmp_path / ".agent/plans/a.md").unlink()
    _write(tmp_path, ".agent/archive/plans/a.md", original + "one more line\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "archive")

    evidence = collect_git_evidence(tmp_path, from_commit=base)
    assert evidence.available
    assert evidence.rename_evidence_available
    assert any(r.new_path == ".agent/archive/plans/a.md" for r in evidence.renames)

    from menhir.domain.artifact_reconciliation import (
        ArtifactSourceSnapshot,
        plan_reconciliation,
    )

    entries = scan_corpus(tmp_path, repository="t", git=evidence)
    snapshot = ArtifactSourceSnapshot(
        artifact_uuid="a-1",
        medium="markdown",
        source_uuid="s-1",
        artifact_type=ArtifactType.PLAN,
        repository="t",
        path=".agent/plans/a.md",
        integrity=sha256_bytes(original.encode()),
        lane=CorpusLane.ACTIVE,
    )
    report = plan_reconciliation(
        repository="t",
        entries=entries,
        snapshots=[snapshot],
        renames=evidence.renames,
    )
    relocations = [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    assert len(relocations) == 1
    assert relocations[0].basis == MatchBasis.GIT_RENAME
    assert relocations[0].artifact_uuid == "a-1"
    assert relocations[0].lane == CorpusLane.ARCHIVE
    # A committed file has a blob OID; the raw-byte hash is recorded separately.
    entry = entries[0]
    assert entry.version and entry.version_kind == "git_blob_oid"
    assert entry.integrity != entry.version


@pytest.mark.unit
def test_an_unknown_git_evidence_base_is_reported_not_treated_as_no_renames(
    tmp_path: Path,
) -> None:
    try:
        _git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add")

    evidence = collect_git_evidence(tmp_path, from_commit="not-a-real-commit")
    assert evidence.available is True
    assert evidence.rename_evidence_available is False
    assert evidence.renames == ()


@pytest.mark.unit
def test_collision_checks_see_sources_that_predate_the_locator_key() -> None:
    """A v1 source has a null `current_locator_key`.

    Matching the destination on that property alone would not see it, and the
    relocation would land on top of the very source the check exists to protect
    -- on exactly the unprepared graph the one-time repair runs against.
    """
    neo = _StubNeo4j(
        responses=[
            [{"blockers": 1, "at_old": True, "fresh": True, "artifact_uuid": "a-1"}]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "taken.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result["reason"] == "destination_already_claimed"
    query = neo.calls[0]["query"]
    assert "coalesce(other.current_locator_key" in query, (
        "the destination check must fall back to the raw locator legs"
    )


@pytest.mark.unit
def test_registration_collision_check_also_falls_back_to_raw_locator_legs() -> None:
    neo = _StubNeo4j(responses=[[{"n": 1}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN,
        title="T",
        repository="menhir",
        path="a.md",
        medium="markdown",
        observation=OBSERVATION,
    )
    assert result["reason"] == "destination_already_claimed"
    assert "coalesce(s.current_locator_key" in neo.calls[0]["query"]


@pytest.mark.unit
def test_registration_refuses_an_unscoped_same_path_source() -> None:
    neo = _StubNeo4j(responses=[[{"n": 1, "unscoped": 1}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN,
        title="T",
        repository="menhir",
        path=".agent/plans/a.md",
        medium="markdown",
        observation=OBSERVATION,
    )
    assert result == {
        "applied": False,
        "reason": "unscoped_source_claims_destination",
    }
    params = neo.calls[0]["params"]
    assert params["medium"] == "markdown"
    assert params["path"] == ".agent/plans/a.md"


@pytest.mark.unit
def test_relocation_refuses_an_unscoped_same_path_destination() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "blockers": 1,
                    "unscoped_blockers": 1,
                    "at_old": True,
                    "fresh": True,
                    "artifact_uuid": "a-1",
                }
            ]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "new.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result["reason"] == "unscoped_source_claims_destination"


@pytest.mark.unit
def test_unscoped_adoption_accepts_whitespace_legacy_repository_values() -> None:
    neo = _StubNeo4j(
        responses=[
            [
                {
                    "blockers": 0,
                    "unscoped_blockers": 0,
                    "at_old": True,
                    "fresh": True,
                    "artifact_uuid": "a-1",
                }
            ]
        ]
    )
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "new.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result["applied"] is True
    assert neo.calls[0]["params"]["old_repository_unscoped"] is True
    assert "coalesce(trim(s.locator_repository), '') = ''" in neo.calls[0]["query"]


@pytest.mark.unit
def test_observing_a_source_clears_the_reason_it_went_missing() -> None:
    """A resolved source carrying "source_not_observed" contradicts itself.

    Every other absent leg is left alone so a partial observation cannot erase a
    known value; this one is written back as null on purpose.
    """
    props = WorkArtifactRepository(_StubNeo4j())._source_write_props(
        SourceObservation(integrity="abc", observed_at="2026-08-11T00:00:00+00:00")
    )
    assert props["resolution_status"] == ResolutionStatus.RESOLVED
    assert "resolution_reason" in props and props["resolution_reason"] is None
    assert "version" not in props, "an unobserved leg is still left alone"
