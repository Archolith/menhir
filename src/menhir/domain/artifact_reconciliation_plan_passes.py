"""Match passes over locators, hashes, registrations, and missing sources.

Each pass sees only what the earlier passes left unclaimed; the ordering
is enforced by ``plan_reconciliation``.
"""

from menhir.domain.artifact_reconciliation_model import (
    ActionKind,
    ArtifactSourceSnapshot,
    ConflictKind,
    CorpusEntry,
    MatchBasis,
    ReconciliationAction,
    ResolutionStatus,
)
from menhir.domain.artifact_reconciliation_plan_state import (
    _PlanState,
    _relocate_or_refresh,
)
from menhir.domain.work_artifact import INITIAL_STATUS, status_from_header


def _plan_duplicate_locators(state: _PlanState) -> None:
    """Two sources claiming one current locator is a graph defect, not a move."""
    for key, group in sorted(state.snapshots_by_key.items()):
        if len(group) < 2:
            continue
        repository, medium, path = key
        for snapshot in group:
            state.claimed_sources.add(snapshot.identity)
            state.actions.append(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.DUPLICATE_CURRENT_LOCATOR,
                    repository=repository,
                    medium=medium,
                    path=path,
                    source_uuid=snapshot.source_uuid,
                    source_identity=snapshot.identity,
                    artifact_uuid=snapshot.artifact_uuid,
                    artifact_type=snapshot.artifact_type,
                    reason="multiple_sources_share_current_locator",
                    detail=tuple(sorted(s.artifact_uuid for s in group)),
                )
            )
        # The entry sitting at that path cannot be safely tied to either source.
        entry = state.entries_by_key.get(key)
        if entry is not None:
            state.claimed_entries.add(entry.key)


def _plan_unscoped_path_conflicts(state: _PlanState) -> None:
    """A path-only match cannot assign an unknown repository safely."""
    for entry in state.unclaimed_entries():
        candidates = [
            snapshot
            for snapshot in state.unscoped_by_key.get((entry.medium, entry.path), [])
            if snapshot.identity not in state.claimed_sources
        ]
        if not candidates:
            continue
        state.reserve_conflict(
            ReconciliationAction(
                kind=ActionKind.CONFLICT,
                conflict_kind=ConflictKind.UNSCOPED_SOURCE_REPOSITORY,
                repository=entry.repository,
                medium=entry.medium,
                path=entry.path,
                lane=entry.lane,
                integrity=entry.integrity,
                title=entry.title,
                reason="path_match_cannot_infer_unscoped_source_repository",
                detail=tuple(sorted(snapshot.artifact_uuid for snapshot in candidates)),
            ),
            entry=entry,
            snapshots=candidates,
        )


def _plan_exact_locators(state: _PlanState) -> None:
    """The path still points at the record it always pointed at."""
    for entry in state.unclaimed_entries():
        group = [
            s
            for s in state.snapshots_by_key.get(entry.key, [])
            if s.identity not in state.claimed_sources
        ]
        if len(group) != 1:
            continue
        snapshot = group[0]
        state.claim(
            _relocate_or_refresh(entry, snapshot, MatchBasis.EXACT_LOCATOR),
            entry=entry,
            snapshot=snapshot,
        )


def _plan_hash_matches(state: _PlanState) -> None:
    """The weakest admissible evidence, under the strictest conditions.

    All of these must hold: the source's old path is gone from disk, exactly one
    unclaimed source carries this hash, and exactly one unclaimed entry does. If
    the old path still exists, the new file is a copy -- claiming the original's
    identity for it would leave the original orphaned and the copy wearing its
    history.
    """
    remaining_entries = [e for e in state.unclaimed_entries() if not e.declared_uuid]
    remaining_sources = [
        s
        for s in state.unclaimed_snapshots()
        if s.integrity and (s.repository, s.path or "") not in state.paths_on_disk
    ]

    entries_by_hash: dict[str, list[CorpusEntry]] = {}
    for entry in remaining_entries:
        entries_by_hash.setdefault(entry.integrity, []).append(entry)
    sources_by_hash: dict[str, list[ArtifactSourceSnapshot]] = {}
    for snapshot in remaining_sources:
        sources_by_hash.setdefault(str(snapshot.integrity), []).append(snapshot)

    for digest, candidates in sorted(entries_by_hash.items()):
        sources = sources_by_hash.get(digest) or []
        if not sources:
            continue
        if len(candidates) > 1 or len(sources) > 1:
            for entry in candidates:
                state.claim(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.AMBIGUOUS_CONTENT_MATCH,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        lane=entry.lane,
                        integrity=entry.integrity,
                        title=entry.title,
                        reason="content_hash_matches_more_than_one_candidate",
                        detail=tuple(
                            sorted(
                                [f"entry:{e.path}" for e in candidates]
                                + [f"source:{s.path or ''}" for s in sources]
                            )
                        ),
                    ),
                    entry=entry,
                )
            continue

        entry, snapshot = candidates[0], sources[0]
        if entry.key in state.claimed_destinations:
            continue
        state.claim(
            _relocate_or_refresh(entry, snapshot, MatchBasis.UNIQUE_CONTENT_SHA256),
            entry=entry,
            snapshot=snapshot,
        )


def _plan_registrations(state: _PlanState) -> None:
    """Everything still unmatched is either a new record or unclassifiable."""
    for entry in state.unclaimed_entries():
        if entry.metadata_errors:
            state.claim(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.INVALID_DECLARED_METADATA,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="declared_metadata_rejected",
                    detail=entry.metadata_errors,
                ),
                entry=entry,
            )
            continue

        artifact_type = entry.effective_type
        if artifact_type is None:
            state.claim(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.UNCLASSIFIED_NEW_SOURCE,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="route_requires_a_declared_artifact_type",
                ),
                entry=entry,
            )
            continue

        status = entry.declared_status
        raw_status = entry.raw_status_header
        status_unresolved_reason = None
        if status is None:
            # A prose `Status:` header is authored intent in the legacy corpus,
            # so it is transcribed; anything unmappable lands in the type's
            # initial state and keeps the raw header for a human to read.
            status, status_unresolved_reason = status_from_header(
                raw_status, artifact_type
            )
        state.claim(
            ReconciliationAction(
                kind=ActionKind.REGISTER_ARTIFACT,
                basis=MatchBasis.NONE,
                repository=entry.repository,
                medium=entry.medium,
                path=entry.path,
                artifact_uuid=entry.declared_uuid,
                artifact_type=artifact_type,
                lane=entry.lane,
                integrity=entry.integrity,
                version=entry.version,
                version_kind=entry.version_kind,
                size_bytes=entry.size_bytes,
                title=entry.title,
                status=status or INITIAL_STATUS[artifact_type],
                raw_status_header=raw_status,
                status_unresolved_reason=status_unresolved_reason,
                reason="new_source_in_typed_route",
            ),
            entry=entry,
        )


def _plan_unresolved_sources(state: _PlanState) -> None:
    """A source nobody could find. Retained with a reason, never deleted."""
    for snapshot in state.unclaimed_snapshots():
        if snapshot.resolution_status == ResolutionStatus.UNRESOLVED:
            continue  # already recorded as missing; re-marking is not a change
        state.actions.append(
            ReconciliationAction(
                kind=ActionKind.MARK_SOURCE_UNRESOLVED,
                basis=MatchBasis.NONE,
                repository=snapshot.repository or "",
                medium=snapshot.medium,
                path=snapshot.path,
                source_uuid=snapshot.source_uuid,
                source_identity=snapshot.identity,
                artifact_uuid=snapshot.artifact_uuid,
                artifact_type=snapshot.artifact_type,
                reason="source_not_observed_in_corpus_scan",
            )
        )


def _plan_remaining_unscoped_sources(state: _PlanState) -> None:
    """Surface relevant unscoped sources even when their old path is absent."""
    for snapshot in state.unclaimed_unscoped_snapshots():
        state.actions.append(
            ReconciliationAction(
                kind=ActionKind.CONFLICT,
                conflict_kind=ConflictKind.UNSCOPED_SOURCE_REPOSITORY,
                repository=state.repository,
                medium=snapshot.medium,
                old_path=snapshot.path,
                source_uuid=snapshot.source_uuid,
                source_identity=snapshot.identity,
                artifact_uuid=snapshot.artifact_uuid,
                artifact_type=snapshot.artifact_type,
                reason="unscoped_source_requires_repository_disposition",
            )
        )
        state.claimed_sources.add(snapshot.identity)
