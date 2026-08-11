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
    CorpusLane,
    MatchBasis,
    ResolutionStatus,
    SourceObservation,
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
        return [c for c in self.calls if " SET " in c["query"] or "CREATE " in c["query"]]


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
    neo = _StubNeo4j(responses=[[{"blockers": 0, "at_old": True, "fresh": True,
                                  "artifact_uuid": "a-1"}]])
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
    props = neo.calls[0]["params"]["props"]
    assert props["locator_path"] == "new.md"
    assert props["current_locator_key"] == "menhir|markdown|new.md"
    assert props["schema_version"] == 2


@pytest.mark.unit
def test_a_claimed_destination_is_refused() -> None:
    neo = _StubNeo4j(responses=[[{"blockers": 1, "at_old": True, "fresh": True,
                                  "artifact_uuid": "a-1"}]])
    result = WorkArtifactRepository(neo).relocate_artifact_source(
        source_uuid="s-1",
        old_locator={"repository": "menhir", "path": "old.md", "medium": "markdown"},
        new_locator={"repository": "menhir", "path": "taken.md", "medium": "markdown"},
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "destination_already_claimed"}


@pytest.mark.unit
def test_a_stale_expected_integrity_is_refused() -> None:
    neo = _StubNeo4j(responses=[[{"blockers": 0, "at_old": True, "fresh": False,
                                  "artifact_uuid": "a-1"}]])
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
    neo = _StubNeo4j(responses=[[{"blockers": 0, "at_old": False, "fresh": True,
                                  "artifact_uuid": "a-1"}]])
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
        repository="menhir", medium="markdown", old_path="old.md", new_path="new.md",
        observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "locator_is_ambiguous"}
    assert not neo.writes


@pytest.mark.unit
def test_a_source_without_a_backfilled_uuid_refuses_rather_than_guessing() -> None:
    neo = _StubNeo4j(responses=[[{"source_uuid": None}]])
    result = WorkArtifactRepository(neo).refresh_artifact_source_by_locator(
        repository="menhir", medium="markdown", path="a.md", observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "source_uuid_not_backfilled"}


@pytest.mark.unit
def test_marking_unresolved_never_deletes_and_keeps_the_locator() -> None:
    neo = _StubNeo4j(responses=[[{"artifact_uuid": "a-1"}]])
    result = WorkArtifactRepository(neo).mark_artifact_source_unresolved(
        source_uuid="s-1", reason="source_not_observed", observed_commit="c0ffee",
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
        artifact_type=ArtifactType.PLAN, title="T", repository="menhir",
        path="a.md", medium="markdown", observation=OBSERVATION, artifact_uuid="a-1",
    )
    assert result == {"applied": False, "reason": "declared_uuid_already_registered",
                      "artifact_uuid": "a-1"}
    assert not neo.writes


@pytest.mark.unit
def test_registration_is_idempotent_by_current_locator() -> None:
    neo = _StubNeo4j(responses=[[{"n": 1}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN, title="T", repository="menhir",
        path="a.md", medium="markdown", observation=OBSERVATION,
    )
    assert result == {"applied": False, "reason": "destination_already_claimed"}
    assert not neo.writes


@pytest.mark.unit
def test_registration_reuses_a_declared_uuid_rather_than_minting_one() -> None:
    declared = "33333333-3333-4333-8333-333333333333"
    neo = _StubNeo4j(responses=[[], [{"n": 0}]])
    result = WorkArtifactRepository(neo).register_work_artifact(
        artifact_type=ArtifactType.PLAN, title="T", repository="menhir",
        path="a.md", medium="markdown", observation=OBSERVATION, artifact_uuid=declared,
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
    neo = _StubNeo4j(responses=[[
        {"eid": "e1", "repository": "menhir", "medium": "markdown", "path": "a.md",
         "version": "f441a237" * 5, "version_kind": None},
    ]])
    assert WorkArtifactRepository(neo).backfill_current_locator_keys() == 1
    params = neo.calls[1]["params"]
    assert params["version_kind"] == "legacy_commit_sha"
    assert params["key"] == "menhir|markdown|a.md"


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


@pytest.mark.unit
def test_the_scan_reaches_backlog_records_the_old_one_level_scan_missed(tmp_path: Path) -> None:
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
def test_a_dirty_working_tree_file_records_integrity_with_no_blob(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    entry = scan_corpus(tmp_path, repository="t")[0]
    assert entry.integrity == sha256_bytes(b"# A\n")
    assert entry.version is None
    assert entry.version_kind is None


@pytest.mark.unit
def test_declared_metadata_reaches_the_entry(tmp_path: Path) -> None:
    uuid = "44444444-4444-4444-8444-444444444444"
    _write(
        tmp_path, ".agent/plans/a.md",
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

    def __init__(self, snapshots=()) -> None:
        self.snapshots = list(snapshots)
        self.calls: list[tuple[str, dict]] = []

    def list_artifact_source_snapshots(self, *, repository: str | None = None):
        self.calls.append(("list", {"repository": repository}))
        return list(self.snapshots)

    def register_work_artifact(self, **kwargs):
        self.calls.append(("register", kwargs))
        return {"applied": True, "artifact_uuid": "new"}

    def refresh_artifact_source(self, **kwargs):
        self.calls.append(("refresh", kwargs))
        return {"applied": True}

    def relocate_artifact_source(self, **kwargs):
        self.calls.append(("relocate", kwargs))
        return {"applied": True}

    def mark_artifact_source_unresolved(self, **kwargs):
        self.calls.append(("unresolved", kwargs))
        return {"applied": True}


@pytest.mark.unit
def test_audit_performs_no_write_calls(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    ArtifactReconciliationService(repo).audit(tmp_path, repository="t")
    assert [name for name, _ in repo.calls] == ["list"]


@pytest.mark.unit
def test_apply_with_the_wrong_digest_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path, expected_digest="not-the-digest", repository="t"
    )
    assert result.ok is False
    assert result.refused_reason == "plan_digest_mismatch"
    assert [name for name, _ in repo.calls] == ["list"]


@pytest.mark.unit
def test_apply_with_the_matching_digest_registers_the_new_record(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    service = ArtifactReconciliationService(_RecordingRepo())
    digest = service.audit(tmp_path, repository="t").plan_digest

    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path, expected_digest=digest, repository="t"
    )
    assert result.ok
    assert len(result.applied) == 1
    assert [name for name, _ in repo.calls] == ["list", "register"]


@pytest.mark.unit
def test_a_conflict_does_not_block_unrelated_safe_actions(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/good.md", "# Good\n")
    _write(tmp_path, ".agent/plans/bad.md", "---\nartifact_uuid: nope\n---\n# Bad\n")
    service = ArtifactReconciliationService(_RecordingRepo())
    digest = service.audit(tmp_path, repository="t").plan_digest

    repo = _RecordingRepo()
    result = ArtifactReconciliationService(repo).apply(
        tmp_path, expected_digest=digest, repository="t"
    )
    assert len(result.conflicted) == 1
    assert len(result.applied) == 1
    assert result.applied[0]["path"] == ".agent/plans/good.md"


@pytest.mark.unit
def test_validation_reports_a_duplicate_uuid_across_two_documents(tmp_path: Path) -> None:
    uuid = "55555555-5555-4555-8555-555555555555"
    body = f"---\nartifact_uuid: {uuid}\nartifact_type: plan\n---\n# T\n"
    _write(tmp_path, ".agent/plans/a.md", body)
    _write(tmp_path, ".agent/plans/b.md", body)
    report = ArtifactReconciliationService(_RecordingRepo()).validate(tmp_path, repository="t")
    codes = {f.code for f in report.findings}
    assert "duplicate_artifact_uuid" in codes
    assert report.ok is False


@pytest.mark.unit
def test_validation_needs_no_graph_connection(tmp_path: Path) -> None:
    class _Exploding:
        def list_artifact_source_snapshots(self, *, repository=None):
            raise AssertionError("validation must not read the graph")

    _write(tmp_path, ".agent/plans/a.md", "# A\n")
    report = ArtifactReconciliationService(_Exploding()).validate(tmp_path, repository="t")
    assert report.checked == 1


@pytest.mark.unit
def test_a_document_with_no_h1_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/plans/a.md", "no heading here\n")
    report = ArtifactReconciliationService(_RecordingRepo()).validate(tmp_path, repository="t")
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
        capture_output=True, text=True,
    ).stdout.strip()

    (tmp_path / ".agent/plans/a.md").unlink()
    _write(tmp_path, ".agent/archive/plans/a.md", original + "one more line\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "archive")

    evidence = collect_git_evidence(tmp_path, from_commit=base)
    assert evidence.available
    assert any(r.new_path == ".agent/archive/plans/a.md" for r in evidence.renames)

    from menhir.domain.artifact_reconciliation import (
        ArtifactSourceSnapshot,
        plan_reconciliation,
    )

    entries = scan_corpus(tmp_path, repository="t", git=evidence)
    snapshot = ArtifactSourceSnapshot(
        artifact_uuid="a-1", medium="markdown", source_uuid="s-1",
        artifact_type=ArtifactType.PLAN, repository="t", path=".agent/plans/a.md",
        integrity=sha256_bytes(original.encode()), lane=CorpusLane.ACTIVE,
    )
    report = plan_reconciliation(
        repository="t", entries=entries, snapshots=[snapshot], renames=evidence.renames,
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
def test_collision_checks_see_sources_that_predate_the_locator_key() -> None:
    """A v1 source has a null `current_locator_key`.

    Matching the destination on that property alone would not see it, and the
    relocation would land on top of the very source the check exists to protect
    -- on exactly the unprepared graph the one-time repair runs against.
    """
    neo = _StubNeo4j(responses=[[{"blockers": 1, "at_old": True, "fresh": True,
                                  "artifact_uuid": "a-1"}]])
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
        artifact_type=ArtifactType.PLAN, title="T", repository="menhir",
        path="a.md", medium="markdown", observation=OBSERVATION,
    )
    assert result["reason"] == "destination_already_claimed"
    assert "coalesce(s.current_locator_key" in neo.calls[0]["query"]


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
