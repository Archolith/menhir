"""Shared vocabulary for artifact reconciliation.

Schema constants, corpus lanes, action/match kinds, and the frozen value
types the planner and the write side exchange. Pure data; no matching
logic lives here.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping

from menhir.domain.work_artifact import ArtifactMedium


#: Bumped when the ArtifactSource property contract changes. v1 sources stored a
#: commit SHA in ``version``; v2 separates raw-byte integrity, blob identity and
#: the observed commit, so the two are never reinterpreted as each other.
ARTIFACT_SOURCE_SCHEMA_VERSION = 2

#: Metadata block version authors declare. Independent of the source schema:
#: one is what a human writes, the other is what menhir derives.
ARTIFACT_METADATA_SCHEMA = 1

INTEGRITY_ALGORITHM = "sha256"


class CorpusLane:
    """Routing lane, derived from where a source currently lives.

    Not a type and not a lifecycle. A plan moved to reference is still
    historically a plan; a plan moved to archive has not thereby been
    implemented, superseded, or deferred. The lane answers "is this in an
    executable position right now?", which is a different question from
    "what is this?" and "how did it end?".
    """

    ACTIVE = "active"
    BACKLOG = "backlog"
    REFERENCE = "reference"
    ARCHIVE = "archive"


CORPUS_LANES: frozenset[str] = frozenset(
    {
        CorpusLane.ACTIVE,
        CorpusLane.BACKLOG,
        CorpusLane.REFERENCE,
        CorpusLane.ARCHIVE,
    }
)

#: Lanes an artifact can be worked from. Used only to report contradictions.
EXECUTABLE_LANES: frozenset[str] = frozenset({CorpusLane.ACTIVE, CorpusLane.BACKLOG})


class ActionKind:
    NOOP = "NOOP"
    REFRESH_SOURCE = "REFRESH_SOURCE"
    RELOCATE_SOURCE = "RELOCATE_SOURCE"
    ADOPT_SOURCE_REPOSITORY = "ADOPT_SOURCE_REPOSITORY"
    ATTACH_SOURCE = "ATTACH_SOURCE"
    REGISTER_ARTIFACT = "REGISTER_ARTIFACT"
    MARK_SOURCE_UNRESOLVED = "MARK_SOURCE_UNRESOLVED"
    CONFLICT = "CONFLICT"


#: Actions apply mode is allowed to perform. CONFLICT is deliberately absent:
#: a conflict is a report, never a mutation.
SAFE_ACTION_KINDS: frozenset[str] = frozenset(
    {
        ActionKind.REFRESH_SOURCE,
        ActionKind.RELOCATE_SOURCE,
        ActionKind.ADOPT_SOURCE_REPOSITORY,
        ActionKind.ATTACH_SOURCE,
        ActionKind.REGISTER_ARTIFACT,
        ActionKind.MARK_SOURCE_UNRESOLVED,
    }
)


class MatchBasis:
    """Why an entry was tied to an existing source. An enum, not a score.

    Ordered by strength of evidence: a declared UUID is an author's statement,
    a Git rename is recorded history, an exact locator is admissible only when
    history does not say the path moved, and a unique content hash is the
    weakest -- admissible only under the strict conditions in
    ``_plan_hash_matches``.
    """

    DECLARED_UUID = "DECLARED_UUID"
    EXACT_LOCATOR = "EXACT_LOCATOR"
    GIT_RENAME = "GIT_RENAME"
    UNIQUE_CONTENT_SHA256 = "UNIQUE_CONTENT_SHA256"
    NONE = "NONE"


class ConflictKind:
    UUID_LOCATOR_DISAGREEMENT = "UUID_LOCATOR_DISAGREEMENT"
    DESTINATION_ALREADY_CLAIMED = "DESTINATION_ALREADY_CLAIMED"
    DUPLICATE_DECLARED_UUID = "DUPLICATE_DECLARED_UUID"
    DUPLICATE_CURRENT_LOCATOR = "DUPLICATE_CURRENT_LOCATOR"
    AMBIGUOUS_CONTENT_MATCH = "AMBIGUOUS_CONTENT_MATCH"
    AMBIGUOUS_GIT_RENAME = "AMBIGUOUS_GIT_RENAME"
    UNCLASSIFIED_NEW_SOURCE = "UNCLASSIFIED_NEW_SOURCE"
    INVALID_DECLARED_METADATA = "INVALID_DECLARED_METADATA"
    DECLARED_UUID_TYPE_DISAGREEMENT = "DECLARED_UUID_TYPE_DISAGREEMENT"
    DECLARED_UUID_ALREADY_EMBODIED = "DECLARED_UUID_ALREADY_EMBODIED"
    UNSCOPED_SOURCE_REPOSITORY = "UNSCOPED_SOURCE_REPOSITORY"


class ResolutionStatus:
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class VersionKind:
    """What the ``version`` leg of a source actually holds.

    v1 migration wrote the last commit touching the path here. A commit SHA is
    provenance for a repository state, not the file's content -- two different
    forty-character hex strings that mean different things must not share a
    field with no discriminator.
    """

    GIT_BLOB_OID = "git_blob_oid"
    LEGACY_COMMIT_SHA = "legacy_commit_sha"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusEntry:
    """One document found on disk, already hashed and classified by route."""

    repository: str
    path: str
    medium: str
    lane: str
    integrity: str
    size_bytes: int
    route_type: str | None = None
    preserve_existing_type: bool = False
    requires_declared_type: bool = False
    declared_uuid: str | None = None
    declared_type: str | None = None
    declared_status: str | None = None
    raw_status_header: str | None = None
    title: str | None = None
    #: Whether ``title`` came from an H1 or fell back to the filename. The
    #: validator needs the difference; matching never does.
    title_from_h1: bool = False
    version: str | None = None
    version_kind: str | None = None
    metadata_errors: tuple[str, ...] = ()

    @property
    def effective_type(self) -> str | None:
        return self.declared_type or self.route_type

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.repository, self.medium, self.path)


@dataclass(frozen=True)
class ArtifactSourceSnapshot:
    """One ``ArtifactSource`` as the graph currently holds it."""

    artifact_uuid: str
    medium: str
    source_uuid: str | None = None
    artifact_type: str | None = None
    repository: str | None = None
    path: str | None = None
    integrity: str | None = None
    version: str | None = None
    version_kind: str | None = None
    lane: str | None = None
    resolution_status: str = ResolutionStatus.RESOLVED
    title: str | None = None
    status: str | None = None
    schema_version: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.repository or "", self.medium, self.path or "")

    @property
    def identity(self) -> str:
        """Stable handle for planning. Source UUID once backfilled; locator before."""
        return (
            self.source_uuid or f"{self.artifact_uuid}:{self.medium}:{self.path or ''}"
        )


@dataclass(frozen=True)
class WorkArtifactIdentitySnapshot:
    """Semantic identity state for a UUID declared by a corpus document.

    Kept separate from ``ArtifactSourceSnapshot`` so a source-less artifact is
    represented honestly rather than as a fabricated source with an empty
    locator.
    """

    artifact_uuid: str
    artifact_type: str | None
    title: str | None = None
    status: str | None = None
    source_count: int = 0


@dataclass(frozen=True)
class GitRename:
    old_path: str
    new_path: str
    repository: str | None = None


@dataclass(frozen=True)
class ReconciliationAction:
    """One proposed change to one source record, or one refusal to change it."""

    kind: str
    basis: str = MatchBasis.NONE
    repository: str = ""
    medium: str = ArtifactMedium.MARKDOWN
    path: str | None = None
    old_path: str | None = None
    source_uuid: str | None = None
    source_identity: str | None = None
    artifact_uuid: str | None = None
    artifact_type: str | None = None
    lane: str | None = None
    integrity: str | None = None
    expected_integrity: str | None = None
    version: str | None = None
    version_kind: str | None = None
    size_bytes: int | None = None
    title: str | None = None
    status: str | None = None
    raw_status_header: str | None = None
    status_unresolved_reason: str | None = None
    conflict_kind: str | None = None
    reason: str | None = None
    detail: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.kind,
            self.repository,
            self.path or self.old_path or "",
            self.source_identity or self.artifact_uuid or "",
        )

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "basis": self.basis,
            "repository": self.repository,
            "medium": self.medium,
            "path": self.path,
            "old_path": self.old_path,
            "source_uuid": self.source_uuid,
            "source_identity": self.source_identity,
            "artifact_uuid": self.artifact_uuid,
            "artifact_type": self.artifact_type,
            "lane": self.lane,
            "integrity": self.integrity,
            "expected_integrity": self.expected_integrity,
            "version": self.version,
            "version_kind": self.version_kind,
            "size_bytes": self.size_bytes,
            "title": self.title,
            "status": self.status,
            "raw_status_header": self.raw_status_header,
            "status_unresolved_reason": self.status_unresolved_reason,
            "conflict_kind": self.conflict_kind,
            "reason": self.reason,
            "detail": list(self.detail),
        }
        return {k: v for k, v in payload.items() if v not in (None, [], ())}


@dataclass(frozen=True)
class LaneContradiction:
    """A lane and a lifecycle state that disagree. Reported, never resolved.

    An archived plan still marked APPROVED might have been implemented,
    superseded, or deferred, and the directory name says which of those happened
    exactly as well as a coin does.
    """

    repository: str
    path: str
    lane: str
    artifact_uuid: str | None
    artifact_type: str | None
    status: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "path": self.path,
            "lane": self.lane,
            "artifact_uuid": self.artifact_uuid,
            "artifact_type": self.artifact_type,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    repository: str
    observed_commit: str | None
    cursor_commit: str | None
    evidence_from_commit: str | None
    evidence_base_valid: bool
    actions: tuple[ReconciliationAction, ...]
    contradictions: tuple[LaneContradiction, ...]
    plan_digest: str
    counts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def conflicts(self) -> tuple[ReconciliationAction, ...]:
        return tuple(a for a in self.actions if a.kind == ActionKind.CONFLICT)

    @property
    def safe_actions(self) -> tuple[ReconciliationAction, ...]:
        return tuple(a for a in self.actions if a.kind in SAFE_ACTION_KINDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "observed_commit": self.observed_commit,
            "cursor_commit": self.cursor_commit,
            "evidence_from_commit": self.evidence_from_commit,
            "evidence_base_valid": self.evidence_base_valid,
            "plan_digest": self.plan_digest,
            "counts": dict(self.counts),
            "actions": [a.as_dict() for a in self.actions],
            "contradictions": [c.as_dict() for c in self.contradictions],
        }


# ---------------------------------------------------------------------------
# Write-side values
# ---------------------------------------------------------------------------


def locator_key(repository: str | None, medium: str, path: str | None) -> str:
    """The normalized key a current locator is unique on.

    One string rather than a composite constraint because Neo4j node-key
    constraints over nullable legs are awkward, and because the uniqueness
    question is exactly "is any other source sitting at this exact place right
    now" -- a single value that can be compared, indexed, and reported.
    """
    return f"{(repository or '').strip()}|{medium}|{(path or '').strip()}"


@dataclass(frozen=True)
class SourceObservation:
    """Everything a reconciliation write records about one observation.

    Grouped into one value so a repository method cannot be called with half the
    evidence: integrity without the commit it was observed at, or a lane without
    the run that derived it, are the states that made v1 sources unreadable.
    """

    integrity: str | None = None
    size_bytes: int | None = None
    lane: str | None = None
    version: str | None = None
    version_kind: str | None = None
    observed_commit: str | None = None
    observed_at: str | None = None
    basis: str = MatchBasis.NONE
    run_id: str | None = None

    def as_properties(self) -> dict[str, Any]:
        return {
            "integrity_algorithm": INTEGRITY_ALGORITHM if self.integrity else None,
            "integrity": self.integrity,
            "size_bytes": self.size_bytes,
            "corpus_lane": self.lane,
            "version": self.version,
            "version_kind": self.version_kind,
            "observed_commit": self.observed_commit,
            "last_seen_at": self.observed_at,
            "last_reconciled_at": self.observed_at,
            "last_reconcile_basis": self.basis,
            "last_reconcile_run_id": self.run_id,
            "resolution_status": ResolutionStatus.RESOLVED,
            "resolution_reason": None,
            "schema_version": ARTIFACT_SOURCE_SCHEMA_VERSION,
        }


def observation_from_action(
    action: ReconciliationAction,
    *,
    observed_commit: str | None,
    observed_at: str,
    run_id: str | None = None,
) -> SourceObservation:
    """The observation a planned action carries, ready for a repository write."""
    return SourceObservation(
        integrity=action.integrity,
        size_bytes=action.size_bytes,
        lane=action.lane,
        version=action.version,
        version_kind=action.version_kind,
        observed_commit=observed_commit,
        observed_at=observed_at,
        basis=action.basis,
        run_id=run_id,
    )
