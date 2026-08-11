"""Live acceptance: a throwaway Neo4j and a temporary Git repository.

The planner tests prove the decisions and the stub tests prove the queries are
shaped right. These prove the whole loop actually holds against a real database:
identity survives a move, a second apply does nothing, and an interrupted run can
be resumed without minting a duplicate.

Requires the test instance on :7688 (`docker compose -f docker-compose.test.yml
up -d`) and `--run-online`. It never touches the production graph -- conftest
forces every URI onto the test instance before collection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    MatchBasis,
    ResolutionStatus,
)
from menhir.domain.work_artifact import (
    ArtifactSourceSpec,
    ArtifactStatus,
    ArtifactType,
)
from menhir.infrastructure.schema import get_phase1_bootstrap_queries
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
from menhir.services.artifact_reconciliation_service import (
    ArtifactReconciliationService,
)

pytestmark = [pytest.mark.online, pytest.mark.timeout(120)]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


@pytest.fixture
def repo_tree(tmp_path: Path) -> Path:
    try:
        _git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


@pytest.fixture
def service(test_neo4j_repo) -> ArtifactReconciliationService:
    for query in get_phase1_bootstrap_queries():
        try:
            test_neo4j_repo.execute(query, {})
        except Exception:  # noqa: BLE001 - unrelated index failures must not skip the suite
            pass
    return ArtifactReconciliationService(WorkArtifactRepository(test_neo4j_repo))


def _apply(service: ArtifactReconciliationService, root: Path):
    report = service.audit(root, repository="t")
    return service.apply(
        root,
        expected_digest=report.plan_digest,
        repository="t",
        allow_new_repository=True,
    )


def _sources(neo4j) -> list[dict]:
    return neo4j.execute(
        "MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource) "
        "RETURN a.artifact_uuid AS artifact_uuid, a.status AS status, "
        "       s.source_uuid AS source_uuid, s.locator_path AS path, "
        "       s.corpus_lane AS lane, s.integrity AS integrity, "
        "       s.resolution_status AS resolution_status, "
        "       s.current_locator_key AS key ORDER BY s.locator_path",
        {},
    )


@pytest.mark.online
def test_create_edit_rename_archive_preserves_identity(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """The full lifecycle of a document's location, with one identity throughout."""
    body = "# A Plan\n\n" + ("body line\n" * 40)
    _write(repo_tree, ".agent/plans/a.md", body)
    _git(repo_tree, "add", "-A")
    _git(repo_tree, "commit", "-q", "-m", "add")

    # create
    result = _apply(service, repo_tree)
    assert len(result.applied) == 1
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1
    original_uuid = rows[0]["artifact_uuid"]
    original_source = rows[0]["source_uuid"]
    assert rows[0]["lane"] == "active"
    assert rows[0]["key"] == "t|markdown|.agent/plans/a.md"

    # edit in place
    _write(repo_tree, ".agent/plans/a.md", body + "an edit\n")
    result = _apply(service, repo_tree)
    assert [r["outcome"]["applied"] for r in result.applied] == [True]
    rows = _sources(test_neo4j_repo)
    assert rows[0]["artifact_uuid"] == original_uuid
    assert rows[0]["source_uuid"] == original_source
    edited_integrity = rows[0]["integrity"]

    # rename into the archive, with a further edit in the same commit
    base = subprocess.run(
        ["git", "-C", str(repo_tree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo_tree / ".agent/plans/a.md").unlink()
    _write(repo_tree, ".agent/archive/plans/a.md", body + "an edit\nand another\n")
    _git(repo_tree, "add", "-A")
    _git(repo_tree, "commit", "-q", "-m", "archive")

    report = service.audit(repo_tree, repository="t", from_commit=base)
    relocations = [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    assert len(relocations) == 1 and relocations[0].basis == MatchBasis.GIT_RENAME
    result = service.apply(
        repo_tree, expected_digest=report.plan_digest, repository="t", from_commit=base
    )
    assert len(result.applied) == 1

    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1, "a move must never create a second source"
    assert rows[0]["artifact_uuid"] == original_uuid
    assert rows[0]["source_uuid"] == original_source
    assert rows[0]["path"] == ".agent/archive/plans/a.md"
    assert rows[0]["lane"] == "archive"
    assert rows[0]["integrity"] != edited_integrity
    # The lane moved; the lifecycle did not.
    assert rows[0]["status"] == ArtifactStatus.PROPOSED


@pytest.mark.online
def test_a_second_apply_performs_zero_mutations(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _write(repo_tree, ".agent/plans/backlog/b.md", "# B\n")
    _apply(service, repo_tree)
    before = _sources(test_neo4j_repo)

    report = service.audit(repo_tree, repository="t")
    assert not report.safe_actions, "settled state must propose nothing"
    second = service.apply(
        repo_tree, expected_digest=report.plan_digest, repository="t"
    )
    assert second.applied == [] and second.skipped == []
    assert _sources(test_neo4j_repo) == before


@pytest.mark.online
def test_a_copy_gets_its_own_identity_and_leaves_the_original_alone(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/original.md", "# Original\n")
    _apply(service, repo_tree)
    original = _sources(test_neo4j_repo)[0]

    _write(repo_tree, ".agent/plans/copy.md", "# Original\n")  # byte-identical
    _apply(service, repo_tree)

    rows = _sources(test_neo4j_repo)
    assert len(rows) == 2
    by_path = {r["path"]: r for r in rows}
    assert (
        by_path[".agent/plans/original.md"]["artifact_uuid"]
        == original["artifact_uuid"]
    )
    assert (
        by_path[".agent/plans/copy.md"]["artifact_uuid"] != original["artifact_uuid"]
    ), "an equal-hash copy must not inherit the original's identity"


@pytest.mark.online
def test_a_deleted_source_is_unresolved_and_recovers_when_restored(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _apply(service, repo_tree)
    original = _sources(test_neo4j_repo)[0]

    (repo_tree / ".agent/plans/a.md").unlink()
    _apply(service, repo_tree)
    row = _sources(test_neo4j_repo)[0]
    assert row["resolution_status"] == ResolutionStatus.UNRESOLVED
    assert row["path"] == ".agent/plans/a.md", "the locator is retained, not cleared"
    assert row["artifact_uuid"] == original["artifact_uuid"]

    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _apply(service, repo_tree)
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1, "a restored file must not mint a second identity"
    assert rows[0]["resolution_status"] == ResolutionStatus.RESOLVED
    assert rows[0]["artifact_uuid"] == original["artifact_uuid"]


@pytest.mark.online
def test_an_interrupted_apply_reruns_idempotently(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """Half the corpus registered, then the run dies. The rerun completes it."""
    for name in ("a", "b", "c", "d"):
        _write(repo_tree, f".agent/plans/{name}.md", f"# {name}\n")

    # Simulate the interruption: register two records directly, as a partial run would.
    repo = WorkArtifactRepository(test_neo4j_repo)
    partial = service.audit(repo_tree, repository="t")
    from datetime import datetime, timezone

    from menhir.domain.artifact_reconciliation import observation_from_action

    now = datetime.now(timezone.utc).isoformat()
    for action in [
        a for a in partial.actions if a.kind == ActionKind.REGISTER_ARTIFACT
    ][:2]:
        repo.register_work_artifact(
            artifact_type=action.artifact_type,
            title=action.title,
            repository=action.repository,
            path=action.path,
            medium=action.medium,
            observation=observation_from_action(
                action, observed_commit=None, observed_at=now, run_id="interrupted"
            ),
            status=action.status,
        )
    assert len(_sources(test_neo4j_repo)) == 2

    result = _apply(service, repo_tree)
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 4, "the rerun must complete without duplicating the first two"
    assert len({r["path"] for r in rows}) == 4
    assert len(result.applied) == 2


@pytest.mark.online
def test_a_claimed_destination_is_refused_at_the_database(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """The collision check is enforced by the write, not only by the planner."""
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _write(repo_tree, ".agent/plans/b.md", "# B\n")
    _apply(service, repo_tree)
    rows = {r["path"]: r for r in _sources(test_neo4j_repo)}

    from menhir.domain.artifact_reconciliation import SourceObservation

    repo = WorkArtifactRepository(test_neo4j_repo)
    outcome = repo.relocate_artifact_source(
        source_uuid=rows[".agent/plans/a.md"]["source_uuid"],
        old_locator={
            "repository": "t",
            "path": ".agent/plans/a.md",
            "medium": "markdown",
        },
        new_locator={
            "repository": "t",
            "path": ".agent/plans/b.md",
            "medium": "markdown",
        },
        observation=SourceObservation(observed_at="2026-08-11T00:00:00+00:00"),
    )
    assert outcome == {"applied": False, "reason": "destination_already_claimed"}
    assert {r["path"] for r in _sources(test_neo4j_repo)} == {
        ".agent/plans/a.md",
        ".agent/plans/b.md",
    }


@pytest.mark.online
def test_relationships_survive_a_relocation(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """The whole reason identity is not the path."""
    _write(repo_tree, ".agent/plans/a.md", "# A Plan\n")
    _write(repo_tree, ".agent/reviews/r.md", "# A Review\n")
    _apply(service, repo_tree)

    repo = WorkArtifactRepository(test_neo4j_repo)
    rows = {r["path"]: r for r in _sources(test_neo4j_repo)}
    plan_uuid = rows[".agent/plans/a.md"]["artifact_uuid"]
    review_uuid = rows[".agent/reviews/r.md"]["artifact_uuid"]
    linked = repo.link_artifacts(review_uuid, plan_uuid, "reviews")
    assert linked.get("linked"), linked

    (repo_tree / ".agent/archive/plans").mkdir(parents=True, exist_ok=True)
    (repo_tree / ".agent/plans/a.md").rename(repo_tree / ".agent/archive/plans/a.md")
    _apply(service, repo_tree)

    edges = test_neo4j_repo.execute(
        "MATCH (r:WorkArtifact {artifact_uuid: $r})-[:REVIEWS]->(p:WorkArtifact) "
        "RETURN p.artifact_uuid AS uuid",
        {"r": review_uuid},
    )
    assert [e["uuid"] for e in edges] == [plan_uuid]


@pytest.mark.online
def test_the_uniqueness_constraints_reject_a_duplicate_locator(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _apply(service, repo_tree)
    key = _sources(test_neo4j_repo)[0]["key"]

    with pytest.raises(Exception):
        test_neo4j_repo.execute(
            "CREATE (s:ArtifactSource {source_uuid: $u, current_locator_key: $k})",
            {"u": "duplicate-probe", "k": key},
        )


@pytest.mark.online
def test_a_declared_uuid_survives_a_clean_clone(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """Registration reuses the author's UUID rather than minting a rival one."""
    declared = "7a1c9f30-2b48-4e15-9c76-0d3f8a21b654"
    _write(
        repo_tree,
        ".agent/plans/a.md",
        f"---\nartifact_schema: 1\nartifact_uuid: {declared}\n"
        f"artifact_type: plan\nartifact_status: APPROVED\n---\n\n# A Plan\n",
    )
    _apply(service, repo_tree)
    row = _sources(test_neo4j_repo)[0]
    assert row["artifact_uuid"] == declared
    assert row["status"] == ArtifactStatus.APPROVED

    # A second run over the same declaration is a NOOP, not a second artifact.
    report = service.audit(repo_tree, repository="t")
    assert not report.safe_actions
    assert len(_sources(test_neo4j_repo)) == 1


@pytest.mark.online
def test_a_declared_uuid_attaches_the_first_source_without_mutating_semantics(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    declared = "8c3dd5f8-3786-47de-a212-e918580ff492"
    WorkArtifactRepository(test_neo4j_repo).create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="Graph-owned title",
        status=ArtifactStatus.APPROVED,
        artifact_uuid=declared,
    )
    _write(
        repo_tree,
        ".agent/plans/a.md",
        f"---\nartifact_uuid: {declared}\nartifact_type: plan\n"
        "artifact_status: PROPOSED\n---\n# File title\n",
    )

    report = service.audit(repo_tree, repository="t")
    assert [a.kind for a in report.safe_actions] == [ActionKind.ATTACH_SOURCE]
    result = service.apply(
        repo_tree,
        expected_digest=report.plan_digest,
        repository="t",
    )
    assert len(result.applied) == 1
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1 and rows[0]["artifact_uuid"] == declared

    artifact = test_neo4j_repo.execute(
        "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) "
        "RETURN a.title AS title, a.status AS status",
        {"uuid": declared},
    )[0]
    assert artifact == {"title": "Graph-owned title", "status": ArtifactStatus.APPROVED}

    second = service.audit(repo_tree, repository="t")
    assert not second.safe_actions


@pytest.mark.online
def test_declared_uuid_adopts_a_legacy_unscoped_source_without_duplication(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    declared = "fd24368c-7500-4dde-97fc-8616df9a7ef4"
    repository = WorkArtifactRepository(test_neo4j_repo)
    repository.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="Legacy",
        artifact_uuid=declared,
        source=ArtifactSourceSpec(
            medium="markdown",
            locator={"path": ".agent/plans/a.md"},
        ),
    )
    repository.backfill_source_uuids()
    repository.backfill_current_locator_keys()
    _write(
        repo_tree,
        ".agent/plans/a.md",
        f"---\nartifact_uuid: {declared}\nartifact_type: plan\n---\n# Legacy\n",
    )

    report = service.audit(repo_tree, repository="t")
    assert [a.kind for a in report.safe_actions] == [ActionKind.ADOPT_SOURCE_REPOSITORY]
    result = service.apply(
        repo_tree,
        expected_digest=report.plan_digest,
        repository="t",
    )
    assert len(result.applied) == 1
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1 and rows[0]["artifact_uuid"] == declared
    scoped = test_neo4j_repo.execute(
        "MATCH (:WorkArtifact {artifact_uuid: $uuid})-[:EMBODIED_IN]->(s) "
        "RETURN s.locator_repository AS repository",
        {"uuid": declared},
    )
    assert scoped == [{"repository": "t"}]
    assert not service.audit(repo_tree, repository="t").safe_actions


@pytest.mark.online
def test_audit_leaves_the_graph_byte_identical(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _apply(service, repo_tree)
    before = _sources(test_neo4j_repo)
    cursor_before = WorkArtifactRepository(
        test_neo4j_repo
    ).get_artifact_reconciliation_cursor(repository="t")

    _write(repo_tree, ".agent/plans/a.md", "# A changed\n")
    _write(repo_tree, ".agent/plans/new.md", "# New\n")
    first = service.audit(repo_tree, repository="t")
    second = service.audit(repo_tree, repository="t")

    assert first.plan_digest == second.plan_digest
    assert [a.as_dict() for a in first.actions] == [a.as_dict() for a in second.actions]
    assert _sources(test_neo4j_repo) == before, "audit must not write"
    assert (
        WorkArtifactRepository(test_neo4j_repo).get_artifact_reconciliation_cursor(
            repository="t"
        )
        == cursor_before
    ), "audit must not advance the cursor"


@pytest.mark.online
def test_apply_with_a_stale_digest_writes_nothing(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    approved = service.audit(repo_tree, repository="t").plan_digest

    # The premises move after approval.
    _write(repo_tree, ".agent/plans/b.md", "# B\n")
    result = service.apply(repo_tree, expected_digest=approved, repository="t")
    assert result.refused_reason == "plan_digest_mismatch"
    assert _sources(test_neo4j_repo) == []


@pytest.mark.online
def test_prepare_backfills_sources_created_before_v2(
    service: ArtifactReconciliationService, test_neo4j_repo
) -> None:
    """A v1 source gets a UUID, a locator key, and its version leg relabelled."""
    test_neo4j_repo.execute(
        """
        CREATE (a:WorkArtifact {artifact_uuid: 'legacy-1', artifact_type: 'plan',
                                title: 'Legacy', status: 'PROPOSED', namespace: 'default'})
        CREATE (a)-[:EMBODIED_IN]->(:ArtifactSource {
            medium: 'markdown', locator_repository: 't', locator_path: '.agent/plans/x.md',
            version: 'f441a237f441a237f441a237f441a237f441a237', schema_version: 1})
        """,
        {},
    )
    stamped = service.prepare_sources()
    assert stamped == {"source_uuids": 1, "locator_keys": 1}

    row = _sources(test_neo4j_repo)[0]
    assert row["source_uuid"]
    assert row["key"] == "t|markdown|.agent/plans/x.md"
    kind = test_neo4j_repo.execute(
        "MATCH (s:ArtifactSource) RETURN s.version_kind AS k", {}
    )[0]["k"]
    assert kind == "legacy_commit_sha", (
        "a commit SHA is labelled, never reread as a blob OID"
    )


@pytest.mark.online
def test_a_branch_switch_uses_the_persisted_cursor_when_it_is_comparable(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    """The prior reconciled commit is an ancestor, so Git carries the move."""
    _write(repo_tree, ".agent/plans/a.md", "# A\n")
    _git(repo_tree, "add", "-A")
    _git(repo_tree, "commit", "-q", "-m", "add")
    _apply(service, repo_tree)
    original = _sources(test_neo4j_repo)[0]

    _git(repo_tree, "checkout", "-q", "-b", "other")
    (repo_tree / ".agent/plans/a.md").unlink()
    _write(repo_tree, ".agent/plans/backlog/a.md", "# A\n")
    _git(repo_tree, "add", "-A")
    _git(repo_tree, "commit", "-q", "-m", "move on branch")

    report = service.audit(repo_tree, repository="t")  # persisted cursor is automatic
    relocations = [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    assert len(relocations) == 1
    assert relocations[0].basis == MatchBasis.GIT_RENAME
    assert report.cursor_commit
    assert report.evidence_from_commit == report.cursor_commit

    service.apply(repo_tree, expected_digest=report.plan_digest, repository="t")
    rows = _sources(test_neo4j_repo)
    assert len(rows) == 1
    assert rows[0]["artifact_uuid"] == original["artifact_uuid"]
    assert rows[0]["lane"] == "backlog"
    assert rows[0]["artifact_uuid"] and rows[0]["path"] == ".agent/plans/backlog/a.md"


@pytest.mark.online
def test_registration_records_the_type_its_route_asserts(
    service: ArtifactReconciliationService, repo_tree: Path, test_neo4j_repo
) -> None:
    _write(repo_tree, ".agent/plans/p.md", "# P\n")
    _write(repo_tree, ".agent/reviews/r.md", "# R\n")
    _write(repo_tree, ".agent/handoffs/h.md", "# H\n")
    _write(repo_tree, ".agent/for-review/WRAPUP-x.md", "# W\n")
    _apply(service, repo_tree)

    rows = test_neo4j_repo.execute(
        "MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource) "
        "RETURN s.locator_path AS path, a.artifact_type AS t ORDER BY s.locator_path",
        {},
    )
    assert {r["path"]: r["t"] for r in rows} == {
        ".agent/for-review/WRAPUP-x.md": ArtifactType.IMPLEMENTATION_REPORT,
        ".agent/handoffs/h.md": ArtifactType.HANDOFF,
        ".agent/plans/p.md": ArtifactType.PLAN,
        ".agent/reviews/r.md": ArtifactType.REVIEW,
    }
