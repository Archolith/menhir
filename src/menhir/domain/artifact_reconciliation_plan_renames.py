"""The Git-rename match pass.

Recorded history tying one exact path to another; byte equality is
deliberately not required.
"""

from typing import Sequence

from menhir.domain.artifact_reconciliation_model import (
    ActionKind,
    ConflictKind,
    GitRename,
    MatchBasis,
    ReconciliationAction,
)
from menhir.domain.artifact_reconciliation_plan_state import (
    _PlanState,
    _relocate_or_refresh,
)


def _plan_git_renames(
    state: _PlanState, renames: Sequence[GitRename], repository: str
) -> None:
    """Recorded history: this exact path became that exact path.

    Byte equality is deliberately not required. A commit can rename and edit in
    one step, and demanding an unchanged hash would turn the most reliable
    evidence available into the one that fires least often.
    """
    scoped = [r for r in renames if r.repository in (None, "", repository)]
    by_new_path: dict[str, list[GitRename]] = {}
    by_old_path: dict[str, list[GitRename]] = {}
    for rename in scoped:
        by_new_path.setdefault(rename.new_path, []).append(rename)
        by_old_path.setdefault(rename.old_path, []).append(rename)

    for entry in state.unclaimed_entries():
        group = by_new_path.get(entry.path) or []
        if not group:
            continue
        implicated = [
            snapshot
            for rename in group
            for snapshot in state.snapshots_by_key.get(
                (entry.repository, entry.medium, rename.old_path), []
            )
        ]
        if len(group) != 1:
            state.reserve_conflict(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.AMBIGUOUS_GIT_RENAME,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="multiple_git_renames_claim_destination",
                    detail=tuple(sorted(rename.old_path for rename in group)),
                ),
                entry=entry,
                snapshots=implicated,
            )
            continue

        rename = group[0]
        old_path = rename.old_path
        outgoing = by_old_path.get(old_path) or []
        raw_candidates = state.snapshots_by_key.get(
            (entry.repository, entry.medium, old_path), []
        )
        if len(outgoing) != 1:
            state.reserve_conflict(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.AMBIGUOUS_GIT_RENAME,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    old_path=old_path,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="git_rename_source_has_multiple_destinations",
                    detail=tuple(sorted(item.new_path for item in outgoing)),
                ),
                entry=entry,
                snapshots=raw_candidates,
            )
            continue

        candidates = [
            s for s in raw_candidates if s.identity not in state.claimed_sources
        ]
        if len(candidates) != 1:
            if raw_candidates:
                state.reserve_conflict(
                    ReconciliationAction(
                        kind=ActionKind.CONFLICT,
                        conflict_kind=ConflictKind.AMBIGUOUS_GIT_RENAME,
                        repository=entry.repository,
                        medium=entry.medium,
                        path=entry.path,
                        old_path=old_path,
                        lane=entry.lane,
                        integrity=entry.integrity,
                        title=entry.title,
                        reason="git_rename_source_is_not_uniquely_available",
                        detail=tuple(sorted(s.identity for s in raw_candidates)),
                    ),
                    entry=entry,
                    snapshots=raw_candidates,
                )
            continue
        if entry.key in state.claimed_destinations:
            continue
        snapshot = candidates[0]

        occupants = [
            occupant
            for occupant in state.snapshots_by_key.get(entry.key, [])
            if occupant.identity != snapshot.identity
            and occupant.identity not in state.claimed_sources
        ]
        blocked_occupants = []
        for occupant in occupants:
            occupant_outgoing = by_old_path.get(occupant.path or "") or []
            can_vacate = (
                len(occupant_outgoing) == 1
                and len(by_new_path.get(occupant_outgoing[0].new_path) or []) == 1
                and (
                    entry.repository,
                    entry.medium,
                    occupant_outgoing[0].new_path,
                )
                in state.entries_by_key
            )
            if not can_vacate:
                blocked_occupants.append(occupant)
        if blocked_occupants:
            state.reserve_conflict(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.DESTINATION_ALREADY_CLAIMED,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    old_path=old_path,
                    source_uuid=snapshot.source_uuid,
                    source_identity=snapshot.identity,
                    artifact_uuid=snapshot.artifact_uuid,
                    artifact_type=snapshot.artifact_type,
                    lane=entry.lane,
                    integrity=entry.integrity,
                    title=entry.title,
                    reason="git_rename_destination_has_nonmoving_source",
                    detail=tuple(sorted(o.artifact_uuid for o in blocked_occupants)),
                ),
                entry=entry,
                snapshots=[snapshot, *blocked_occupants],
            )
            continue
        action = _relocate_or_refresh(entry, snapshot, MatchBasis.GIT_RENAME)
        state.claim(action, entry=entry, snapshot=snapshot)
