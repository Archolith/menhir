"""The reconciliation planner.

Runs the passes strongest-evidence-first, reports lane contradictions,
summarizes counts, and derives the plan digest. Pure; no I/O.
"""

import hashlib
import json
from typing import Any, Iterable, Sequence

from menhir.domain.artifact_reconciliation_model import (
    ARTIFACT_SOURCE_SCHEMA_VERSION,
    ActionKind,
    ArtifactSourceSnapshot,
    CorpusEntry,
    CorpusLane,
    EXECUTABLE_LANES,
    GitRename,
    LaneContradiction,
    MatchBasis,
    ReconciliationAction,
    ReconciliationReport,
    WorkArtifactIdentitySnapshot,
)
from menhir.domain.artifact_reconciliation_plan_passes import (
    _plan_duplicate_locators,
    _plan_exact_locators,
    _plan_hash_matches,
    _plan_registrations,
    _plan_remaining_unscoped_sources,
    _plan_unresolved_sources,
    _plan_unscoped_path_conflicts,
)
from menhir.domain.artifact_reconciliation_plan_renames import _plan_git_renames
from menhir.domain.artifact_reconciliation_plan_state import _PlanState
from menhir.domain.artifact_reconciliation_plan_uuid import _plan_declared_uuids


def plan_reconciliation(
    *,
    repository: str,
    entries: Sequence[CorpusEntry],
    snapshots: Sequence[ArtifactSourceSnapshot],
    unscoped_snapshots: Sequence[ArtifactSourceSnapshot] = (),
    identities: Sequence[WorkArtifactIdentitySnapshot] = (),
    renames: Sequence[GitRename] = (),
    observed_commit: str | None = None,
    cursor_commit: str | None = None,
    evidence_from_commit: str | None = None,
    evidence_base_valid: bool = True,
) -> ReconciliationReport:
    """Decide what each source record should become. Pure; no I/O.

    Passes run strongest-evidence-first and each one only sees what the earlier
    passes left alone. The order is the whole safety argument: a declared UUID
    beats Git history, Git history beats a stale path, a path beats a matching
    hash, and a matching hash is admissible only when nothing else could explain
    it.
    """
    scoped_entries = sorted(
        (e for e in entries if e.repository == repository), key=lambda e: e.path
    )
    scoped_snapshots = sorted(
        (s for s in snapshots if (s.repository or "") == repository),
        key=lambda s: (s.path or "", s.artifact_uuid),
    )
    state = _PlanState(
        scoped_entries,
        scoped_snapshots,
        unscoped_snapshots,
        identities,
        repository,
    )

    _plan_duplicate_locators(state)
    _plan_declared_uuids(state)
    _plan_unscoped_path_conflicts(state)
    _plan_git_renames(state, renames, repository)
    _plan_exact_locators(state)
    _plan_hash_matches(state)
    _plan_registrations(state)
    _plan_unresolved_sources(state)
    _plan_remaining_unscoped_sources(state)

    actions = tuple(sorted(state.actions, key=lambda a: a.sort_key))
    contradictions = tuple(_lane_contradictions(state))
    counts = _summarize(state, actions, contradictions)
    digest = compute_plan_digest(
        repository=repository,
        observed_commit=observed_commit,
        cursor_commit=cursor_commit,
        evidence_from_commit=evidence_from_commit,
        evidence_base_valid=evidence_base_valid,
        entries=scoped_entries,
        snapshots=scoped_snapshots,
        unscoped_snapshots=unscoped_snapshots,
        identities=identities,
        actions=actions,
    )
    return ReconciliationReport(
        repository=repository,
        observed_commit=observed_commit,
        cursor_commit=cursor_commit,
        evidence_from_commit=evidence_from_commit,
        evidence_base_valid=evidence_base_valid,
        actions=actions,
        contradictions=contradictions,
        plan_digest=digest,
        counts=counts,
    )


def _lane_contradictions(state: _PlanState) -> list[LaneContradiction]:
    """Where routing and lifecycle disagree. A report line, not a transition."""
    found: list[LaneContradiction] = []
    matched_by_key = {
        (a.repository, a.medium, a.path): a
        for a in state.actions
        if a.kind
        in (ActionKind.NOOP, ActionKind.REFRESH_SOURCE, ActionKind.RELOCATE_SOURCE)
    }
    by_artifact = {s.artifact_uuid: s for s in state.snapshots if s.artifact_uuid}
    for entry in state.entries:
        action = matched_by_key.get((entry.repository, entry.medium, entry.path))
        if action is None or not action.artifact_uuid:
            continue
        snapshot = by_artifact.get(action.artifact_uuid)
        status = snapshot.status if snapshot else None
        if status is None:
            continue
        if entry.lane == CorpusLane.ARCHIVE and status not in _TERMINALISH:
            found.append(
                LaneContradiction(
                    repository=entry.repository,
                    path=entry.path,
                    lane=entry.lane,
                    artifact_uuid=action.artifact_uuid,
                    artifact_type=action.artifact_type,
                    status=status,
                    reason="archived_source_without_terminal_lifecycle",
                )
            )
        elif entry.lane in EXECUTABLE_LANES and status in _TERMINALISH:
            found.append(
                LaneContradiction(
                    repository=entry.repository,
                    path=entry.path,
                    lane=entry.lane,
                    artifact_uuid=action.artifact_uuid,
                    artifact_type=action.artifact_type,
                    status=status,
                    reason="terminal_lifecycle_in_executable_lane",
                )
            )
    return sorted(found, key=lambda c: (c.repository, c.path))


#: Statuses that mean the artifact is finished with, for contradiction reporting
#: only. Nothing here transitions anything.
_TERMINALISH: frozenset[str] = frozenset(
    {
        "IMPLEMENTED",
        "COMPLETE",
        "SUPERSEDED",
        "DEFERRED",
    }
)


def _summarize(
    state: _PlanState,
    actions: Sequence[ReconciliationAction],
    contradictions: Sequence[LaneContradiction],
) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_basis: dict[str, int] = {}
    by_conflict: dict[str, int] = {}
    for action in actions:
        by_kind[action.kind] = by_kind.get(action.kind, 0) + 1
        if action.basis != MatchBasis.NONE:
            by_basis[action.basis] = by_basis.get(action.basis, 0) + 1
        if action.conflict_kind:
            by_conflict[action.conflict_kind] = (
                by_conflict.get(action.conflict_kind, 0) + 1
            )

    by_lane: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for entry in state.entries:
        by_lane[entry.lane] = by_lane.get(entry.lane, 0) + 1
        key = entry.effective_type or "undeclared"
        by_type[key] = by_type.get(key, 0) + 1

    return {
        "entries": len(state.entries),
        "sources": len(state.snapshots),
        "unscoped_sources": len(state.unscoped_snapshots),
        "artifact_identities": len(state.identities),
        "actions": len(actions),
        "by_kind": dict(sorted(by_kind.items())),
        "by_basis": dict(sorted(by_basis.items())),
        "by_conflict": dict(sorted(by_conflict.items())),
        "entries_by_lane": dict(sorted(by_lane.items())),
        "entries_by_type": dict(sorted(by_type.items())),
        "contradictions": len(contradictions),
    }


def compute_plan_digest(
    *,
    repository: str,
    observed_commit: str | None,
    cursor_commit: str | None = None,
    evidence_from_commit: str | None = None,
    evidence_base_valid: bool = True,
    entries: Iterable[CorpusEntry],
    snapshots: Iterable[ArtifactSourceSnapshot],
    unscoped_snapshots: Iterable[ArtifactSourceSnapshot] = (),
    identities: Iterable[WorkArtifactIdentitySnapshot] = (),
    actions: Iterable[ReconciliationAction],
) -> str:
    """A digest over the premises *and* the conclusions.

    Apply refuses when this changes, so it has to cover everything the plan was
    derived from -- not just the action list. A digest over conclusions alone
    would happily approve an apply whose inputs had moved underneath it in a way
    that happened to produce the same actions.
    """
    payload = {
        "repository": repository,
        "observed_commit": observed_commit,
        "cursor_commit": cursor_commit,
        "evidence_from_commit": evidence_from_commit,
        "evidence_base_valid": evidence_base_valid,
        "schema": ARTIFACT_SOURCE_SCHEMA_VERSION,
        "sources": sorted(
            [
                s.source_uuid or "",
                s.artifact_uuid,
                s.medium,
                s.repository or "",
                s.path or "",
                s.integrity or "",
                s.resolution_status,
            ]
            for s in snapshots
        ),
        "unscoped_sources": sorted(
            [
                s.source_uuid or "",
                s.artifact_uuid,
                s.medium,
                s.path or "",
                s.integrity or "",
                s.resolution_status,
            ]
            for s in unscoped_snapshots
        ),
        "artifact_identities": sorted(
            [
                identity.artifact_uuid,
                identity.artifact_type or "",
                identity.title or "",
                identity.status or "",
                str(identity.source_count),
            ]
            for identity in identities
        ),
        "entries": sorted(
            [
                e.repository,
                e.medium,
                e.path,
                e.integrity,
                str(e.size_bytes),
                e.declared_uuid or "",
                e.declared_type or "",
                e.declared_status or "",
                e.lane,
            ]
            for e in entries
        ),
        "actions": [a.as_dict() for a in sorted(actions, key=lambda a: a.sort_key)],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
