"""The reconciliation match matrix, offline.

Every case here is a decision the planner makes without touching a database, a
filesystem, or Git, which is the point: the rules that decide whether a document
keeps its identity have to be checkable without standing anything up.
"""

from __future__ import annotations

import pytest

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    ArtifactSourceSnapshot,
    ConflictKind,
    CorpusEntry,
    CorpusLane,
    GitRename,
    MatchBasis,
    ResolutionStatus,
    WorkArtifactIdentitySnapshot,
    compute_plan_digest,
    is_index_document,
    locator_key,
    medium_for_path,
    parse_frontmatter,
    plan_reconciliation,
    read_document_metadata,
    route_for_path,
    sha256_bytes,
)
from menhir.domain.work_artifact import ArtifactStatus, ArtifactType

REPO = "menhir"
HASH_A = sha256_bytes(b"alpha")
HASH_B = sha256_bytes(b"beta")
UUID_1 = "11111111-1111-4111-8111-111111111111"
UUID_2 = "22222222-2222-4222-8222-222222222222"


def entry(
    path: str,
    *,
    integrity: str = HASH_A,
    lane: str = CorpusLane.ACTIVE,
    route_type: str | None = ArtifactType.PLAN,
    declared_uuid: str | None = None,
    declared_type: str | None = None,
    declared_status: str | None = None,
    metadata_errors: tuple[str, ...] = (),
    requires_declared_type: bool = False,
) -> CorpusEntry:
    return CorpusEntry(
        repository=REPO,
        path=path,
        medium="markdown",
        lane=lane,
        integrity=integrity,
        size_bytes=len(integrity),
        route_type=route_type,
        requires_declared_type=requires_declared_type,
        declared_uuid=declared_uuid,
        declared_type=declared_type,
        declared_status=declared_status,
        title=path.rsplit("/", 1)[-1],
        metadata_errors=metadata_errors,
    )


def source(
    path: str,
    *,
    artifact_uuid: str = UUID_1,
    integrity: str | None = HASH_A,
    source_uuid: str = "s-1",
    lane: str | None = CorpusLane.ACTIVE,
    resolution_status: str = ResolutionStatus.RESOLVED,
    status: str | None = None,
    schema_version: int | None = 2,
    repository: str | None = REPO,
) -> ArtifactSourceSnapshot:
    return ArtifactSourceSnapshot(
        artifact_uuid=artifact_uuid,
        medium="markdown",
        source_uuid=source_uuid,
        artifact_type=ArtifactType.PLAN,
        repository=repository,
        path=path,
        integrity=integrity,
        lane=lane,
        resolution_status=resolution_status,
        status=status,
        schema_version=schema_version,
    )


def plan(entries, snapshots, renames=(), identities=(), unscoped=()):
    return plan_reconciliation(
        repository=REPO,
        entries=entries,
        snapshots=snapshots,
        identities=identities,
        unscoped_snapshots=unscoped,
        renames=renames,
        observed_commit="c0ffee",
    )


def only(report, kind: str):
    matches = [a for a in report.actions if a.kind == kind]
    assert matches, f"no {kind} in {[a.kind for a in report.actions]}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1-2. Exact locator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_path_same_hash_is_a_noop() -> None:
    report = plan([entry(".agent/plans/a.md")], [source(".agent/plans/a.md")])
    action = only(report, ActionKind.NOOP)
    assert action.basis == MatchBasis.EXACT_LOCATOR
    assert not report.safe_actions


@pytest.mark.unit
def test_exact_path_changed_hash_refreshes_without_changing_identity() -> None:
    report = plan(
        [entry(".agent/plans/a.md", integrity=HASH_B)], [source(".agent/plans/a.md")]
    )
    action = only(report, ActionKind.REFRESH_SOURCE)
    assert action.artifact_uuid == UUID_1
    assert action.integrity == HASH_B
    assert action.expected_integrity == HASH_A


# ---------------------------------------------------------------------------
# 3-4. Declared UUID
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_declared_uuid_at_a_new_path_relocates() -> None:
    report = plan(
        [
            entry(
                ".agent/archive/plans/a.md",
                lane=CorpusLane.ARCHIVE,
                declared_uuid=UUID_1,
            )
        ],
        [source(".agent/plans/a.md")],
    )
    action = only(report, ActionKind.RELOCATE_SOURCE)
    assert action.basis == MatchBasis.DECLARED_UUID
    assert action.old_path == ".agent/plans/a.md"
    assert action.path == ".agent/archive/plans/a.md"
    assert action.artifact_uuid == UUID_1


@pytest.mark.unit
def test_uuid_and_locator_naming_different_artifacts_is_a_conflict() -> None:
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [
            source(".agent/plans/a.md", artifact_uuid=UUID_2, source_uuid="s-2"),
            source(".agent/plans/b.md", artifact_uuid=UUID_1, source_uuid="s-1"),
        ],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.UUID_LOCATOR_DISAGREEMENT
    assert not report.safe_actions or all(
        a.kind == ActionKind.MARK_SOURCE_UNRESOLVED for a in report.safe_actions
    )


@pytest.mark.unit
def test_a_copy_that_kept_its_parents_uuid_is_a_conflict_for_both() -> None:
    report = plan(
        [
            entry(".agent/plans/a.md", declared_uuid=UUID_1),
            entry(".agent/plans/a-copy.md", declared_uuid=UUID_1),
        ],
        [source(".agent/plans/a.md")],
    )
    conflicts = [a for a in report.actions if a.kind == ActionKind.CONFLICT]
    assert len(conflicts) == 2
    assert {c.conflict_kind for c in conflicts} == {
        ConflictKind.DUPLICATE_DECLARED_UUID
    }


@pytest.mark.unit
def test_declared_uuid_attaches_a_source_to_a_source_less_artifact() -> None:
    identity = WorkArtifactIdentitySnapshot(
        artifact_uuid=UUID_1,
        artifact_type=ArtifactType.PLAN,
        title="Existing semantic artifact",
        status=ArtifactStatus.APPROVED,
        source_count=0,
    )
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [],
        identities=[identity],
    )
    action = only(report, ActionKind.ATTACH_SOURCE)
    assert action.basis == MatchBasis.DECLARED_UUID
    assert action.artifact_uuid == UUID_1
    assert action.artifact_type == ArtifactType.PLAN
    assert not [a for a in report.actions if a.kind == ActionKind.REGISTER_ARTIFACT]


@pytest.mark.unit
def test_source_less_declared_uuid_with_a_type_disagreement_is_a_conflict() -> None:
    identity = WorkArtifactIdentitySnapshot(
        artifact_uuid=UUID_1,
        artifact_type=ArtifactType.REVIEW,
        source_count=0,
    )
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [],
        identities=[identity],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.DECLARED_UUID_TYPE_DISAGREEMENT
    assert not report.safe_actions


@pytest.mark.unit
def test_declared_uuid_with_only_out_of_scope_sources_is_a_conflict() -> None:
    identity = WorkArtifactIdentitySnapshot(
        artifact_uuid=UUID_1,
        artifact_type=ArtifactType.PLAN,
        source_count=1,
    )
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [],
        identities=[identity],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.DECLARED_UUID_ALREADY_EMBODIED
    assert not report.safe_actions


@pytest.mark.unit
def test_declared_uuid_adopts_an_unscoped_source_repository() -> None:
    unscoped = source(
        ".agent/plans/old.md",
        artifact_uuid=UUID_1,
        source_uuid="legacy-source",
        repository=None,
    )
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [],
        unscoped=[unscoped],
    )
    action = only(report, ActionKind.ADOPT_SOURCE_REPOSITORY)
    assert action.basis == MatchBasis.DECLARED_UUID
    assert action.source_uuid == "legacy-source"
    assert action.old_path == ".agent/plans/old.md"
    assert action.path == ".agent/plans/a.md"
    assert not report.conflicts


@pytest.mark.unit
def test_unscoped_same_path_without_declared_uuid_blocks_registration() -> None:
    report = plan(
        [entry(".agent/plans/a.md")],
        [],
        unscoped=[source(".agent/plans/a.md", repository=None)],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.UNSCOPED_SOURCE_REPOSITORY
    assert not [a for a in report.actions if a.kind == ActionKind.REGISTER_ARTIFACT]


@pytest.mark.unit
def test_unscoped_same_path_with_a_different_declared_uuid_is_a_conflict() -> None:
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_2)],
        [],
        unscoped=[source(".agent/plans/a.md", artifact_uuid=UUID_1, repository=None)],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.UNSCOPED_SOURCE_REPOSITORY
    assert not report.safe_actions


@pytest.mark.unit
def test_unscoped_path_collision_blocks_refresh_of_a_scoped_declared_source() -> None:
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1, integrity=HASH_B)],
        [source(".agent/plans/a.md", artifact_uuid=UUID_1, integrity=HASH_A)],
        unscoped=[source(".agent/plans/a.md", artifact_uuid=UUID_2, repository=None)],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.UNSCOPED_SOURCE_REPOSITORY
    assert not report.safe_actions


@pytest.mark.unit
def test_unscoped_declared_uuid_without_source_uuid_requires_backfill() -> None:
    report = plan(
        [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        [],
        unscoped=[
            source(
                ".agent/plans/a.md",
                artifact_uuid=UUID_1,
                source_uuid="",
                repository=None,
            )
        ],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.reason == "unscoped_source_uuid_not_backfilled"


# ---------------------------------------------------------------------------
# 5. Git rename
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_git_rename_relocates_even_when_the_bytes_changed() -> None:
    report = plan(
        [entry(".agent/archive/plans/a.md", lane=CorpusLane.ARCHIVE, integrity=HASH_B)],
        [source(".agent/plans/a.md")],
        renames=[
            GitRename(
                old_path=".agent/plans/a.md", new_path=".agent/archive/plans/a.md"
            )
        ],
    )
    action = only(report, ActionKind.RELOCATE_SOURCE)
    assert action.basis == MatchBasis.GIT_RENAME
    assert action.integrity == HASH_B
    assert action.reason == "source_moved_and_changed"


@pytest.mark.unit
def test_rename_is_refused_when_the_old_path_names_two_sources() -> None:
    report = plan(
        [entry(".agent/archive/plans/a.md", lane=CorpusLane.ARCHIVE)],
        [
            source(".agent/plans/a.md", artifact_uuid=UUID_1, source_uuid="s-1"),
            source(".agent/plans/a.md", artifact_uuid=UUID_2, source_uuid="s-2"),
        ],
        renames=[
            GitRename(
                old_path=".agent/plans/a.md", new_path=".agent/archive/plans/a.md"
            )
        ],
    )
    assert not [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    kinds = {a.conflict_kind for a in report.actions if a.kind == ActionKind.CONFLICT}
    assert ConflictKind.DUPLICATE_CURRENT_LOCATOR in kinds


@pytest.mark.unit
def test_git_rename_precedes_stale_exact_locator_in_a_chain() -> None:
    report = plan(
        [
            entry(".agent/plans/b.md", integrity=HASH_A),
            entry(".agent/plans/c.md", integrity=HASH_B),
        ],
        [
            source(
                ".agent/plans/a.md",
                artifact_uuid=UUID_1,
                source_uuid="s-1",
                integrity=HASH_A,
            ),
            source(
                ".agent/plans/b.md",
                artifact_uuid=UUID_2,
                source_uuid="s-2",
                integrity=HASH_B,
            ),
        ],
        renames=[
            GitRename(old_path=".agent/plans/a.md", new_path=".agent/plans/b.md"),
            GitRename(old_path=".agent/plans/b.md", new_path=".agent/plans/c.md"),
        ],
    )

    relocations = [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    assert {(a.artifact_uuid, a.old_path, a.path, a.basis) for a in relocations} == {
        (UUID_1, ".agent/plans/a.md", ".agent/plans/b.md", MatchBasis.GIT_RENAME),
        (UUID_2, ".agent/plans/b.md", ".agent/plans/c.md", MatchBasis.GIT_RENAME),
    }
    assert not report.conflicts


@pytest.mark.unit
def test_git_rename_precedes_stale_exact_locator_in_a_path_swap() -> None:
    report = plan(
        [
            entry(".agent/plans/a.md", integrity=HASH_B),
            entry(".agent/plans/b.md", integrity=HASH_A),
        ],
        [
            source(
                ".agent/plans/a.md",
                artifact_uuid=UUID_1,
                source_uuid="s-1",
                integrity=HASH_A,
            ),
            source(
                ".agent/plans/b.md",
                artifact_uuid=UUID_2,
                source_uuid="s-2",
                integrity=HASH_B,
            ),
        ],
        renames=[
            GitRename(old_path=".agent/plans/a.md", new_path=".agent/plans/b.md"),
            GitRename(old_path=".agent/plans/b.md", new_path=".agent/plans/a.md"),
        ],
    )

    relocations = [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    assert {(a.artifact_uuid, a.path, a.basis) for a in relocations} == {
        (UUID_1, ".agent/plans/b.md", MatchBasis.GIT_RENAME),
        (UUID_2, ".agent/plans/a.md", MatchBasis.GIT_RENAME),
    }
    assert not report.conflicts


@pytest.mark.unit
def test_multiple_git_renames_to_one_destination_fail_closed() -> None:
    report = plan(
        [entry(".agent/plans/c.md")],
        [
            source(".agent/plans/a.md", artifact_uuid=UUID_1, source_uuid="s-1"),
            source(".agent/plans/b.md", artifact_uuid=UUID_2, source_uuid="s-2"),
        ],
        renames=[
            GitRename(old_path=".agent/plans/a.md", new_path=".agent/plans/c.md"),
            GitRename(old_path=".agent/plans/b.md", new_path=".agent/plans/c.md"),
        ],
    )

    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.AMBIGUOUS_GIT_RENAME
    assert conflict.reason == "multiple_git_renames_claim_destination"
    assert not report.safe_actions


@pytest.mark.unit
def test_one_git_rename_source_to_multiple_destinations_fails_closed() -> None:
    report = plan(
        [entry(".agent/plans/b.md"), entry(".agent/plans/c.md")],
        [source(".agent/plans/a.md")],
        renames=[
            GitRename(old_path=".agent/plans/a.md", new_path=".agent/plans/b.md"),
            GitRename(old_path=".agent/plans/a.md", new_path=".agent/plans/c.md"),
        ],
    )

    conflicts = [a for a in report.actions if a.kind == ActionKind.CONFLICT]
    assert len(conflicts) == 2
    assert {a.conflict_kind for a in conflicts} == {ConflictKind.AMBIGUOUS_GIT_RENAME}
    assert not report.safe_actions


@pytest.mark.unit
def test_git_rename_to_a_nonmoving_occupied_destination_fails_closed() -> None:
    report = plan(
        [entry(".agent/plans/taken.md")],
        [
            source(".agent/plans/gone.md", artifact_uuid=UUID_1, source_uuid="s-1"),
            source(".agent/plans/taken.md", artifact_uuid=UUID_2, source_uuid="s-2"),
        ],
        renames=[
            GitRename(old_path=".agent/plans/gone.md", new_path=".agent/plans/taken.md")
        ],
    )

    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.DESTINATION_ALREADY_CLAIMED
    assert conflict.reason == "git_rename_destination_has_nonmoving_source"
    assert not report.safe_actions


# ---------------------------------------------------------------------------
# 6-9. Content hash
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_absent_old_path_and_one_equal_hash_relocates() -> None:
    report = plan(
        [entry(".agent/archive/plans/moved.md", lane=CorpusLane.ARCHIVE)],
        [source(".agent/plans/gone.md")],
    )
    action = only(report, ActionKind.RELOCATE_SOURCE)
    assert action.basis == MatchBasis.UNIQUE_CONTENT_SHA256
    assert action.old_path == ".agent/plans/gone.md"


@pytest.mark.unit
def test_a_copy_never_steals_the_originals_identity() -> None:
    """The old path still exists, so the equal-hash file is a copy, not a move."""
    report = plan(
        [entry(".agent/plans/original.md"), entry(".agent/plans/copy.md")],
        [source(".agent/plans/original.md")],
    )
    assert not [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    registered = only(report, ActionKind.REGISTER_ARTIFACT)
    assert registered.path == ".agent/plans/copy.md"
    assert only(report, ActionKind.NOOP).path == ".agent/plans/original.md"


@pytest.mark.unit
def test_two_equal_hash_destinations_are_ambiguous() -> None:
    report = plan(
        [entry(".agent/plans/one.md"), entry(".agent/plans/two.md")],
        [source(".agent/plans/gone.md")],
    )
    conflicts = [a for a in report.actions if a.kind == ActionKind.CONFLICT]
    assert len(conflicts) == 2
    assert {c.conflict_kind for c in conflicts} == {
        ConflictKind.AMBIGUOUS_CONTENT_MATCH
    }
    assert not [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]


@pytest.mark.unit
def test_a_destination_already_claimed_is_never_relocated_into() -> None:
    """The entry at the claimed path matches its own source; nothing moves onto it."""
    report = plan(
        [entry(".agent/plans/taken.md")],
        [
            source(".agent/plans/taken.md", artifact_uuid=UUID_2, source_uuid="s-2"),
            source(".agent/plans/gone.md", artifact_uuid=UUID_1, source_uuid="s-1"),
        ],
    )
    assert not [a for a in report.actions if a.kind == ActionKind.RELOCATE_SOURCE]
    unresolved = only(report, ActionKind.MARK_SOURCE_UNRESOLVED)
    assert unresolved.path == ".agent/plans/gone.md"


# ---------------------------------------------------------------------------
# 10. Missing source
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_source_with_no_match_is_unresolved_not_deleted() -> None:
    report = plan([], [source(".agent/plans/gone.md", integrity=None)])
    action = only(report, ActionKind.MARK_SOURCE_UNRESOLVED)
    assert action.artifact_uuid == UUID_1
    assert action.reason == "source_not_observed_in_corpus_scan"


@pytest.mark.unit
def test_an_already_unresolved_source_is_not_re_marked() -> None:
    report = plan(
        [],
        [source(".agent/plans/gone.md", resolution_status=ResolutionStatus.UNRESOLVED)],
    )
    assert not [
        a for a in report.actions if a.kind == ActionKind.MARK_SOURCE_UNRESOLVED
    ]


@pytest.mark.unit
def test_a_reappeared_source_refreshes_back_to_resolved() -> None:
    report = plan(
        [entry(".agent/plans/a.md")],
        [source(".agent/plans/a.md", resolution_status=ResolutionStatus.UNRESOLVED)],
    )
    action = only(report, ActionKind.REFRESH_SOURCE)
    assert "source_reappeared" in (action.reason or "")


# ---------------------------------------------------------------------------
# 11-13. Routes, registration, contradictions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_new_backlog_plan_registers_as_a_plan_in_the_backlog_lane() -> None:
    report = plan([entry(".agent/plans/backlog/new.md", lane=CorpusLane.BACKLOG)], [])
    action = only(report, ActionKind.REGISTER_ARTIFACT)
    assert action.artifact_type == ArtifactType.PLAN
    assert action.lane == CorpusLane.BACKLOG
    assert action.status == ArtifactStatus.PROPOSED


@pytest.mark.unit
def test_a_reference_record_without_a_declared_type_is_unclassified() -> None:
    report = plan(
        [
            entry(
                ".agent/reference/paper.pdf",
                lane=CorpusLane.REFERENCE,
                route_type=None,
                requires_declared_type=True,
            )
        ],
        [],
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.UNCLASSIFIED_NEW_SOURCE


@pytest.mark.unit
def test_malformed_metadata_is_reported_rather_than_registered() -> None:
    report = plan(
        [entry(".agent/plans/bad.md", metadata_errors=("invalid_artifact_uuid",))], []
    )
    conflict = only(report, ActionKind.CONFLICT)
    assert conflict.conflict_kind == ConflictKind.INVALID_DECLARED_METADATA
    assert conflict.detail == ("invalid_artifact_uuid",)


@pytest.mark.unit
def test_archive_path_with_nonterminal_status_is_reported_never_transitioned() -> None:
    report = plan(
        [entry(".agent/archive/plans/a.md", lane=CorpusLane.ARCHIVE)],
        [
            source(
                ".agent/archive/plans/a.md",
                lane=CorpusLane.ARCHIVE,
                status=ArtifactStatus.APPROVED,
            )
        ],
    )
    assert len(report.contradictions) == 1
    assert (
        report.contradictions[0].reason == "archived_source_without_terminal_lifecycle"
    )
    # Reporting a contradiction must not propose a status change of any kind.
    assert all(a.status is None for a in report.actions)


@pytest.mark.unit
def test_a_terminal_plan_sitting_in_an_executable_lane_is_reported() -> None:
    report = plan(
        [entry(".agent/plans/a.md")],
        [source(".agent/plans/a.md", status=ArtifactStatus.IMPLEMENTED)],
    )
    assert report.contradictions[0].reason == "terminal_lifecycle_in_executable_lane"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_identical_plans_produce_identical_ordering_and_digest() -> None:
    entries = [entry(".agent/plans/b.md"), entry(".agent/plans/a.md", integrity=HASH_B)]
    snapshots = [source(".agent/plans/a.md")]
    first = plan(entries, snapshots)
    second = plan(list(reversed(entries)), snapshots)
    assert first.plan_digest == second.plan_digest
    assert [a.as_dict() for a in first.actions] == [a.as_dict() for a in second.actions]


@pytest.mark.unit
def test_the_digest_covers_premises_not_only_conclusions() -> None:
    """A source whose recorded hash changed yields a different digest.

    Both plans propose the same REFRESH; if the digest only covered actions, an
    apply approved against one premise could run against the other.
    """
    entries = [entry(".agent/plans/a.md", integrity=HASH_B)]
    one = compute_plan_digest(
        repository=REPO,
        observed_commit="c1",
        entries=entries,
        snapshots=[source(".agent/plans/a.md", integrity=HASH_A)],
        actions=[],
    )
    two = compute_plan_digest(
        repository=REPO,
        observed_commit="c1",
        entries=entries,
        snapshots=[source(".agent/plans/a.md", integrity="deadbeef")],
        actions=[],
    )
    assert one != two


@pytest.mark.unit
def test_the_digest_covers_cursor_and_selected_evidence_base() -> None:
    entries = [entry(".agent/plans/a.md")]
    common = {
        "repository": REPO,
        "observed_commit": "head",
        "entries": entries,
        "snapshots": [],
        "actions": [],
    }
    stored = compute_plan_digest(
        **common, cursor_commit="stored", evidence_from_commit="stored"
    )
    override = compute_plan_digest(
        **common, cursor_commit="stored", evidence_from_commit="override"
    )
    moved = compute_plan_digest(
        **common, cursor_commit="moved", evidence_from_commit="stored"
    )
    invalid = compute_plan_digest(
        **common,
        cursor_commit="stored",
        evidence_from_commit="stored",
        evidence_base_valid=False,
    )
    assert len({stored, override, moved, invalid}) == 4


@pytest.mark.unit
def test_the_digest_covers_declared_artifact_identity_state() -> None:
    common = {
        "repository": REPO,
        "observed_commit": "head",
        "entries": [entry(".agent/plans/a.md", declared_uuid=UUID_1)],
        "snapshots": [],
        "actions": [],
    }
    source_less = compute_plan_digest(
        **common,
        identities=[
            WorkArtifactIdentitySnapshot(
                artifact_uuid=UUID_1,
                artifact_type=ArtifactType.PLAN,
                source_count=0,
            )
        ],
    )
    now_embodied = compute_plan_digest(
        **common,
        identities=[
            WorkArtifactIdentitySnapshot(
                artifact_uuid=UUID_1,
                artifact_type=ArtifactType.PLAN,
                source_count=1,
            )
        ],
    )
    assert source_less != now_embodied


@pytest.mark.unit
def test_the_digest_covers_unscoped_source_state() -> None:
    common = {
        "repository": REPO,
        "observed_commit": "head",
        "entries": [entry(".agent/plans/a.md")],
        "snapshots": [],
        "actions": [],
    }
    clear = compute_plan_digest(**common)
    blocked = compute_plan_digest(
        **common,
        unscoped_snapshots=[source(".agent/plans/a.md", repository=None)],
    )
    assert clear != blocked


@pytest.mark.unit
def test_an_unchanged_corpus_proposes_no_safe_actions() -> None:
    """Idempotence: the second run over settled state has nothing to do."""
    entries = [
        entry(".agent/plans/a.md"),
        entry(".agent/plans/backlog/b.md", lane=CorpusLane.BACKLOG),
    ]
    snapshots = [
        source(".agent/plans/a.md", source_uuid="s-1"),
        source(
            ".agent/plans/backlog/b.md",
            artifact_uuid=UUID_2,
            source_uuid="s-2",
            lane=CorpusLane.BACKLOG,
        ),
    ]
    report = plan(entries, snapshots)
    assert not report.safe_actions
    assert all(a.kind == ActionKind.NOOP for a in report.actions)


# ---------------------------------------------------------------------------
# Routes and hashing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,expected_type,expected_lane",
    [
        (".agent/plans/x.md", ArtifactType.PLAN, CorpusLane.ACTIVE),
        (".agent/plans/backlog/x.md", ArtifactType.PLAN, CorpusLane.BACKLOG),
        (".agent/reviews/x.md", ArtifactType.REVIEW, CorpusLane.ACTIVE),
        (".agent/handoffs/x.md", ArtifactType.HANDOFF, CorpusLane.ACTIVE),
        (
            ".agent/for-review/x.md",
            ArtifactType.IMPLEMENTATION_REPORT,
            CorpusLane.ACTIVE,
        ),
        (".agent/archive/plans/x.md", ArtifactType.PLAN, CorpusLane.ARCHIVE),
        (".agent/reference/x.md", None, CorpusLane.REFERENCE),
    ],
)
def test_route_table(path: str, expected_type: str | None, expected_lane: str) -> None:
    route = route_for_path(path)
    assert route is not None
    assert route.artifact_type == expected_type
    assert route.lane == expected_lane


@pytest.mark.unit
def test_backlog_is_matched_before_its_parent_directory() -> None:
    assert route_for_path(".agent/plans/backlog/x.md").lane == CorpusLane.BACKLOG
    assert route_for_path(".agent/plans/x.md").lane == CorpusLane.ACTIVE


@pytest.mark.unit
def test_an_undeclared_subdirectory_is_outside_the_corpus() -> None:
    """Not typed by its parent: an unnamed subdirectory routes nowhere."""
    assert route_for_path(".agent/plans/scratch/deep/x.md") is None


@pytest.mark.unit
def test_archived_wrapups_stay_in_the_corpus() -> None:
    """CF-15: the gateway's own archive destination was the one archive dir with no route.

    A wrapup was tracked in `.agent/for-review` and then left the corpus silently the moment it
    was archived -- `route_for_path` returned None, so `build_entry` returned None and the
    document was neither tracked nor reported as unclassified.
    """
    route = route_for_path(".agent/archive/wrapups/WRAPUP-2026-08-20-x.md")
    assert route is not None
    assert route.lane == CorpusLane.ARCHIVE
    # Same type as the active lane it came from: archiving does not change what a document is.
    assert route.artifact_type == route_for_path(".agent/for-review/WRAPUP-x.md").artifact_type

    # CONTROL: the sibling archive routes still resolve, so a change that broke the route
    # table generally would fail here rather than silently passing the assertion above.
    assert route_for_path(".agent/archive/handoffs/x.md").lane == CorpusLane.ARCHIVE
    assert route_for_path(".agent/archive/reviews/x.md").lane == CorpusLane.ARCHIVE


@pytest.mark.unit
def test_index_documents_are_excluded() -> None:
    assert is_index_document(".agent/plans/README.md")
    assert not is_index_document(".agent/plans/readme-driven-plan.md")


@pytest.mark.unit
def test_crlf_and_lf_hash_differently() -> None:
    """A byte change is a real source change even when the prose renders alike."""
    assert sha256_bytes(b"# Plan\r\ntext\r\n") != sha256_bytes(b"# Plan\ntext\n")


@pytest.mark.unit
def test_hashing_is_stable_over_repeated_reads() -> None:
    payload = b"# Plan\n\nStatus: APPROVED\n"
    assert sha256_bytes(payload) == sha256_bytes(payload)


@pytest.mark.unit
def test_medium_is_derived_from_the_suffix() -> None:
    assert medium_for_path("a/b.md") == "markdown"
    assert medium_for_path("a/b.PDF") == "pdf"
    assert medium_for_path("a/b.py") is None


@pytest.mark.unit
def test_locator_key_is_stable_and_scoped() -> None:
    assert locator_key("menhir", "markdown", "a.md") == "menhir|markdown|a.md"
    assert locator_key("menhir", "markdown", "a.md") != locator_key(
        "other", "markdown", "a.md"
    )


# ---------------------------------------------------------------------------
# Authored metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_complete_metadata_block_is_read() -> None:
    text = (
        "---\n"
        "artifact_schema: 1\n"
        f"artifact_uuid: {UUID_1}\n"
        "artifact_type: plan\n"
        "artifact_status: APPROVED\n"
        "---\n"
        "\n# A Plan\n"
    )
    meta = read_document_metadata(text, route_type=ArtifactType.PLAN)
    assert meta.is_valid, meta.errors
    assert meta.artifact_uuid == UUID_1
    assert meta.artifact_type == ArtifactType.PLAN
    assert meta.artifact_status == ArtifactStatus.APPROVED
    assert meta.title == "A Plan"


@pytest.mark.unit
def test_a_malformed_uuid_is_rejected_with_an_exact_reason() -> None:
    meta = read_document_metadata("---\nartifact_uuid: not-a-uuid\n---\n# T\n")
    assert "invalid_artifact_uuid" in meta.errors
    assert meta.artifact_uuid is None


@pytest.mark.unit
def test_a_status_the_type_cannot_hold_is_rejected_not_coerced() -> None:
    meta = read_document_metadata(
        "---\nartifact_type: plan\nartifact_status: DRAFT\n---\n# T\n",
        route_type=ArtifactType.PLAN,
    )
    assert "status_invalid_for_type" in meta.errors
    assert meta.artifact_status is None


@pytest.mark.unit
def test_a_declared_type_disagreeing_with_its_route_is_an_error() -> None:
    meta = read_document_metadata(
        "---\nartifact_type: review\n---\n# T\n", route_type=ArtifactType.PLAN
    )
    assert "route_type_disagreement" in meta.errors


@pytest.mark.unit
def test_a_derived_key_written_by_an_author_is_refused() -> None:
    """Authors never declare what menhir observes; a stale copy is worse than none."""
    meta = read_document_metadata("---\ncorpus_lane: active\n---\n# T\n")
    assert any(e.startswith("derived_key_declared:corpus_lane") for e in meta.errors)


@pytest.mark.unit
def test_an_unterminated_frontmatter_block_is_reported() -> None:
    mapping, _body, errors = parse_frontmatter("---\nartifact_type: plan\n# T\n")
    assert errors == ("frontmatter_not_terminated",)
    assert mapping == {}


@pytest.mark.unit
def test_a_document_without_frontmatter_still_yields_its_h1_and_status() -> None:
    meta = read_document_metadata(
        "# Legacy Plan\n\nStatus: **APPROVED** as the basis\n"
    )
    assert meta.title == "Legacy Plan"
    assert meta.raw_status_header.startswith("**APPROVED**")
    assert not meta.has_frontmatter


@pytest.mark.unit
def test_a_legacy_status_header_is_transcribed_into_the_registration() -> None:
    report = plan(
        [
            CorpusEntry(
                repository=REPO,
                path=".agent/plans/legacy.md",
                medium="markdown",
                lane=CorpusLane.ACTIVE,
                integrity=HASH_A,
                size_bytes=5,
                route_type=ArtifactType.PLAN,
                raw_status_header="IMPLEMENTED",
            )
        ],
        [],
    )
    action = only(report, ActionKind.REGISTER_ARTIFACT)
    assert action.status == ArtifactStatus.IMPLEMENTED
    assert action.raw_status_header == "IMPLEMENTED"


@pytest.mark.unit
def test_an_unmappable_header_lands_in_the_initial_state_and_keeps_the_raw_text() -> (
    None
):
    report = plan(
        [
            CorpusEntry(
                repository=REPO,
                path=".agent/plans/legacy.md",
                medium="markdown",
                lane=CorpusLane.ACTIVE,
                integrity=HASH_A,
                size_bytes=5,
                route_type=ArtifactType.PLAN,
                raw_status_header="mostly done-ish",
            )
        ],
        [],
    )
    action = only(report, ActionKind.REGISTER_ARTIFACT)
    assert action.status == ArtifactStatus.PROPOSED
    assert action.raw_status_header == "mostly done-ish"
