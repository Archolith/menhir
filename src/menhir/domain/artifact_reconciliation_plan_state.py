"""Planner bookkeeping.

The claim-tracking state shared by the match passes, and the constructor
that turns one matched entry/source pair into a NOOP, REFRESH, or
RELOCATE action.
"""

from typing import Sequence

from menhir.domain.artifact_reconciliation_model import (
    ARTIFACT_SOURCE_SCHEMA_VERSION,
    ActionKind,
    ArtifactSourceSnapshot,
    CorpusEntry,
    ReconciliationAction,
    ResolutionStatus,
    WorkArtifactIdentitySnapshot,
)


class _PlanState:
    """Bookkeeping shared by the match passes.

    Claims are tracked in both directions -- an entry is claimed once some pass
    has decided what happens to it, a source once some pass has tied it to an
    entry -- because every later pass is only allowed to consider what is still
    unclaimed. That is what stops two passes from relocating the same source to
    two different paths.
    """

    def __init__(
        self,
        entries: Sequence[CorpusEntry],
        snapshots: Sequence[ArtifactSourceSnapshot],
        unscoped_snapshots: Sequence[ArtifactSourceSnapshot],
        identities: Sequence[WorkArtifactIdentitySnapshot],
        repository: str,
    ) -> None:
        self.repository = repository
        self.entries = list(entries)
        self.snapshots = list(snapshots)
        self.unscoped_snapshots = list(unscoped_snapshots)
        self.identities = list(identities)
        self.actions: list[ReconciliationAction] = []
        self.claimed_entries: set[tuple[str, str, str]] = set()
        self.claimed_sources: set[str] = set()
        #: Destination keys already assigned by an action in this run.
        self.claimed_destinations: set[tuple[str, str, str]] = set()

        self.entries_by_key: dict[tuple[str, str, str], CorpusEntry] = {}
        for entry in self.entries:
            self.entries_by_key.setdefault(entry.key, entry)
        self.paths_on_disk: set[tuple[str, str]] = {
            (entry.repository, entry.path) for entry in self.entries
        }

        self.snapshots_by_key: dict[
            tuple[str, str, str], list[ArtifactSourceSnapshot]
        ] = {}
        self.snapshots_by_artifact: dict[str, list[ArtifactSourceSnapshot]] = {}
        for snapshot in self.snapshots:
            self.snapshots_by_key.setdefault(snapshot.key, []).append(snapshot)
            self.snapshots_by_artifact.setdefault(snapshot.artifact_uuid, []).append(
                snapshot
            )
        self.unscoped_by_artifact: dict[str, list[ArtifactSourceSnapshot]] = {}
        self.unscoped_by_key: dict[tuple[str, str], list[ArtifactSourceSnapshot]] = {}
        for snapshot in self.unscoped_snapshots:
            self.unscoped_by_artifact.setdefault(snapshot.artifact_uuid, []).append(
                snapshot
            )
            self.unscoped_by_key.setdefault(
                (snapshot.medium, snapshot.path or ""), []
            ).append(snapshot)
        self.identities_by_artifact = {
            identity.artifact_uuid: identity for identity in self.identities
        }

    def unclaimed_entries(self) -> list[CorpusEntry]:
        return [e for e in self.entries if e.key not in self.claimed_entries]

    def unclaimed_snapshots(self) -> list[ArtifactSourceSnapshot]:
        return [s for s in self.snapshots if s.identity not in self.claimed_sources]

    def unclaimed_unscoped_snapshots(self) -> list[ArtifactSourceSnapshot]:
        return [
            s for s in self.unscoped_snapshots if s.identity not in self.claimed_sources
        ]

    def claim(
        self,
        action: ReconciliationAction,
        *,
        entry: CorpusEntry | None = None,
        snapshot: ArtifactSourceSnapshot | None = None,
    ) -> None:
        self.actions.append(action)
        if entry is not None:
            self.claimed_entries.add(entry.key)
            if action.kind != ActionKind.CONFLICT:
                self.claimed_destinations.add(entry.key)
        if snapshot is not None and action.kind != ActionKind.CONFLICT:
            self.claimed_sources.add(snapshot.identity)

    def reserve_conflict(
        self,
        action: ReconciliationAction,
        *,
        entry: CorpusEntry,
        snapshots: Sequence[ArtifactSourceSnapshot] = (),
    ) -> None:
        """Record a conflict and keep every implicated record out of later passes.

        A conflict is a terminal planner decision for the records it names. If
        those records remained unclaimed, exact-locator or unresolved-source
        passes could still emit a mutation for the same ambiguity.
        """
        if action.kind != ActionKind.CONFLICT:
            raise ValueError("reserve_conflict requires a CONFLICT action")
        self.claim(action, entry=entry)
        self.claimed_sources.update(snapshot.identity for snapshot in snapshots)


def _relocate_or_refresh(
    entry: CorpusEntry, snapshot: ArtifactSourceSnapshot, basis: str
) -> ReconciliationAction:
    """One matched pair becomes NOOP, REFRESH, or RELOCATE.

    Location and content are independent: a file can move without changing, or
    change without moving, or both at once. Relocation therefore also carries
    the fresh integrity rather than assuming the bytes survived the move.
    """
    moved = (snapshot.path or "") != entry.path
    content_changed = snapshot.integrity != entry.integrity
    lane_changed = (snapshot.lane or None) != entry.lane
    was_unresolved = snapshot.resolution_status == ResolutionStatus.UNRESOLVED
    stale_schema = (snapshot.schema_version or 1) < ARTIFACT_SOURCE_SCHEMA_VERSION

    common = {
        "basis": basis,
        "repository": entry.repository,
        "medium": entry.medium,
        "path": entry.path,
        "source_uuid": snapshot.source_uuid,
        "source_identity": snapshot.identity,
        "artifact_uuid": snapshot.artifact_uuid,
        "artifact_type": snapshot.artifact_type,
        "lane": entry.lane,
        "integrity": entry.integrity,
        "expected_integrity": snapshot.integrity,
        "version": entry.version,
        "version_kind": entry.version_kind,
        "size_bytes": entry.size_bytes,
        "title": entry.title,
    }
    if moved:
        return ReconciliationAction(
            kind=ActionKind.RELOCATE_SOURCE,
            old_path=snapshot.path,
            reason="source_moved"
            if not content_changed
            else "source_moved_and_changed",
            **common,
        )
    if content_changed or lane_changed or was_unresolved or stale_schema:
        reasons = []
        if content_changed:
            reasons.append("content_changed")
        if lane_changed:
            reasons.append("lane_changed")
        if was_unresolved:
            reasons.append("source_reappeared")
        if stale_schema:
            reasons.append("source_schema_upgrade")
        return ReconciliationAction(
            kind=ActionKind.REFRESH_SOURCE, reason="+".join(reasons), **common
        )
    return ReconciliationAction(kind=ActionKind.NOOP, reason="unchanged", **common)
