"""The declared-UUID match pass.

An author's stated identity is the strongest evidence available; this
pass also refuses every ambiguity a declared UUID can fall into.
"""

from menhir.domain.artifact_reconciliation_model import (
    ActionKind,
    ConflictKind,
    CorpusEntry,
    MatchBasis,
    ReconciliationAction,
)
from menhir.domain.artifact_reconciliation_plan_state import (
    _PlanState,
    _relocate_or_refresh,
)


def _plan_declared_uuids(state: _PlanState) -> None:
    """An author's declared UUID is the strongest evidence available."""
    by_uuid: dict[str, list[CorpusEntry]] = {}
    for entry in state.unclaimed_entries():
        if entry.declared_uuid:
            by_uuid.setdefault(entry.declared_uuid, []).append(entry)

    for declared_uuid, group in sorted(by_uuid.items()):
        if len(group) > 1:
            # A copy that kept its parent's UUID. Both records now claim one
            # identity, and nothing on disk says which was the original.
            for entry in group:
                state.claim(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.DUPLICATE_DECLARED_UUID,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        artifact_uuid=declared_uuid,
                        lane=entry.lane,
                        integrity=entry.integrity,
                        title=entry.title,
                        reason="declared_uuid_claimed_by_multiple_documents",
                        detail=tuple(sorted(e.path for e in group)),
                    ),
                    entry=entry,
                )
            continue

        entry = group[0]
        if entry.metadata_errors:
            continue  # handled by registration/validation, not identity matching

        candidates = [
            s
            for s in state.snapshots_by_artifact.get(declared_uuid, [])
            if s.medium == entry.medium and s.identity not in state.claimed_sources
        ]
        unscoped_candidates = [
            s
            for s in state.unscoped_by_artifact.get(declared_uuid, [])
            if s.medium == entry.medium and s.identity not in state.claimed_sources
        ]
        occupant = [
            s
            for s in state.snapshots_by_key.get(entry.key, [])
            if s.artifact_uuid != declared_uuid
        ]
        if occupant:
            state.claim(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.UUID_LOCATOR_DISAGREEMENT,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    artifact_uuid=declared_uuid,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="declared_uuid_and_current_locator_name_different_artifacts",
                    detail=tuple(sorted(s.artifact_uuid for s in occupant)),
                ),
                entry=entry,
            )
            continue

        if len(candidates) + len(unscoped_candidates) > 1:
            implicated = candidates + unscoped_candidates
            state.reserve_conflict(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.UUID_LOCATOR_DISAGREEMENT,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    artifact_uuid=declared_uuid,
                    lane=entry.lane,
                    reason="declared_uuid_has_multiple_sources_of_this_medium",
                    detail=tuple(sorted(s.path or "" for s in implicated)),
                ),
                entry=entry,
                snapshots=implicated,
            )
            continue

        unscoped_occupants = [
            s
            for s in state.unscoped_by_key.get((entry.medium, entry.path), [])
            if s.artifact_uuid != declared_uuid
            and s.identity not in state.claimed_sources
        ]
        if unscoped_occupants:
            state.reserve_conflict(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.UNSCOPED_SOURCE_REPOSITORY,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    artifact_uuid=declared_uuid,
                    lane=entry.lane,
                    reason="declared_uuid_destination_claimed_by_unscoped_source",
                    detail=tuple(sorted(s.artifact_uuid for s in unscoped_occupants)),
                ),
                entry=entry,
                snapshots=[*candidates, *unscoped_occupants],
            )
            continue

        if unscoped_candidates:
            snapshot = unscoped_candidates[0]
            destination_occupants = [
                s
                for s in state.unscoped_by_key.get((entry.medium, entry.path), [])
                if s.identity != snapshot.identity
            ]
            if destination_occupants:
                state.reserve_conflict(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.UNSCOPED_SOURCE_REPOSITORY,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        old_path=snapshot.path,
                        artifact_uuid=declared_uuid,
                        lane=entry.lane,
                        reason="unscoped_destination_claimed_by_different_artifact",
                        detail=tuple(
                            sorted(s.artifact_uuid for s in destination_occupants)
                        ),
                    ),
                    entry=entry,
                    snapshots=[snapshot, *destination_occupants],
                )
                continue
            if entry.effective_type != snapshot.artifact_type:
                state.reserve_conflict(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.DECLARED_UUID_TYPE_DISAGREEMENT,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        old_path=snapshot.path,
                        artifact_uuid=declared_uuid,
                        artifact_type=entry.effective_type,
                        lane=entry.lane,
                        reason="declared_uuid_type_disagrees_with_unscoped_source_owner",
                        detail=(
                            snapshot.artifact_type or "graph_type_missing",
                            entry.effective_type or "document_type_missing",
                        ),
                    ),
                    entry=entry,
                    snapshots=[snapshot],
                )
                continue
            if not snapshot.source_uuid:
                state.reserve_conflict(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.UNSCOPED_SOURCE_REPOSITORY,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        old_path=snapshot.path,
                        artifact_uuid=declared_uuid,
                        lane=entry.lane,
                        reason="unscoped_source_uuid_not_backfilled",
                    ),
                    entry=entry,
                    snapshots=[snapshot],
                )
                continue
            state.claim(
                ReconciliationAction(
                    kind=ActionKind.ADOPT_SOURCE_REPOSITORY,
                    basis=MatchBasis.DECLARED_UUID,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    old_path=snapshot.path,
                    source_uuid=snapshot.source_uuid,
                    source_identity=snapshot.identity,
                    artifact_uuid=declared_uuid,
                    artifact_type=snapshot.artifact_type,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    expected_integrity=snapshot.integrity,
                    version=entry.version,
                    version_kind=entry.version_kind,
                    size_bytes=entry.size_bytes,
                    title=snapshot.title or entry.title,
                    status=snapshot.status,
                    reason="declared_uuid_adopts_unscoped_source_repository",
                ),
                entry=entry,
                snapshot=snapshot,
            )
            continue

        if not candidates:
            identity = state.identities_by_artifact.get(declared_uuid)
            if identity is None:
                continue  # a pre-minted UUID on a new document; registration handles it

            artifact_type = entry.effective_type
            if artifact_type != identity.artifact_type:
                state.claim(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.DECLARED_UUID_TYPE_DISAGREEMENT,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        artifact_uuid=declared_uuid,
                        artifact_type=artifact_type,
                        lane=entry.lane,
                        integrity=entry.integrity,
                        title=entry.title,
                        reason="declared_uuid_type_disagrees_with_graph_artifact",
                        detail=(
                            identity.artifact_type or "graph_type_missing",
                            artifact_type or "document_type_missing",
                        ),
                    ),
                    entry=entry,
                )
                continue

            if identity.source_count:
                state.claim(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.DECLARED_UUID_ALREADY_EMBODIED,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        artifact_uuid=declared_uuid,
                        artifact_type=identity.artifact_type,
                        lane=entry.lane,
                        integrity=entry.integrity,
                        title=entry.title,
                        reason="declared_uuid_has_sources_outside_reconciliation_scope",
                        detail=(f"source_count:{identity.source_count}",),
                    ),
                    entry=entry,
                )
                continue

            state.claim(
                ReconciliationAction(
                    kind=ActionKind.ATTACH_SOURCE,
                    basis=MatchBasis.DECLARED_UUID,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    artifact_uuid=declared_uuid,
                    artifact_type=identity.artifact_type or artifact_type,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    version=entry.version,
                    version_kind=entry.version_kind,
                    size_bytes=entry.size_bytes,
                    title=entry.title,
                    status=identity.status,
                    reason="declared_uuid_identifies_source_less_artifact",
                ),
                entry=entry,
            )
            continue

        snapshot = candidates[0]
        state.claim(
            _relocate_or_refresh(entry, snapshot, MatchBasis.DECLARED_UUID),
            entry=entry,
            snapshot=snapshot,
        )
