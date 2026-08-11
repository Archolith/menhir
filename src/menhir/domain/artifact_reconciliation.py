"""Pure reconciliation of a document corpus against recorded artifact sources.

File state and semantic state have different authorities. The filesystem and Git
decide that a document exists, changed bytes, moved, or can no longer be found;
menhir owns identity, lifecycle, declared relationships and provenance. This
module is the deterministic half of the boundary: given what the disk says and
what the graph says, it decides what the *source records* should look like, and
refuses to decide anything semantic.

Nothing here touches Neo4j, the filesystem, or Git. It takes already-collected
values and returns actions. That is what makes the whole match matrix testable
offline and what makes ``audit`` provably read-only.

Two rules run through every branch:

* **A hash is evidence, not identity.** Templates and copies are byte-identical;
  equal bytes never prove equal identity on their own.
* **Ambiguity fails closed.** A detector may say CONFLICT or UNRESOLVED. It may
  not pick the nearest-looking file, and no title, prose similarity or model
  judgement participates in a match.

See .agent/plans/menhir-work-artifact-reconciliation-2026-08-11.md for the
design and .agent/workflows/artifact_authoring.md for the authoring contract
whose metadata block this module reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from menhir.domain.work_artifact import (
    ARTIFACT_TYPES,
    ArtifactMedium,
    ArtifactType,
    INITIAL_STATUS,
    status_from_header,
    valid_statuses,
)

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


CORPUS_LANES: frozenset[str] = frozenset({
    CorpusLane.ACTIVE,
    CorpusLane.BACKLOG,
    CorpusLane.REFERENCE,
    CorpusLane.ARCHIVE,
})

#: Lanes an artifact can be worked from. Used only to report contradictions.
EXECUTABLE_LANES: frozenset[str] = frozenset({CorpusLane.ACTIVE, CorpusLane.BACKLOG})


class ActionKind:
    NOOP = "NOOP"
    REFRESH_SOURCE = "REFRESH_SOURCE"
    RELOCATE_SOURCE = "RELOCATE_SOURCE"
    REGISTER_ARTIFACT = "REGISTER_ARTIFACT"
    MARK_SOURCE_UNRESOLVED = "MARK_SOURCE_UNRESOLVED"
    CONFLICT = "CONFLICT"


#: Actions apply mode is allowed to perform. CONFLICT is deliberately absent:
#: a conflict is a report, never a mutation.
SAFE_ACTION_KINDS: frozenset[str] = frozenset({
    ActionKind.REFRESH_SOURCE,
    ActionKind.RELOCATE_SOURCE,
    ActionKind.REGISTER_ARTIFACT,
    ActionKind.MARK_SOURCE_UNRESOLVED,
})


class MatchBasis:
    """Why an entry was tied to an existing source. An enum, not a score.

    Ordered by strength of evidence: a declared UUID is an author's statement,
    an exact locator is a fact, a Git rename is recorded history, and a unique
    content hash is the weakest -- admissible only under the strict conditions
    in ``_plan_hash_matches``.
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
    UNCLASSIFIED_NEW_SOURCE = "UNCLASSIFIED_NEW_SOURCE"
    INVALID_DECLARED_METADATA = "INVALID_DECLARED_METADATA"


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
# Registration routes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusRoute:
    """One directory's routing rule.

    ``directory`` matches files sitting *directly* in it, not recursively --
    recursion happens by declaring the child directory as its own route. That is
    the difference from the old one-level ``DIR_TYPES`` scan: subdirectories are
    covered because they are named, not because a glob swallowed them, so a new
    unnamed subdirectory is reported rather than silently typed by its parent.
    """

    directory: str
    artifact_type: str | None
    lane: str
    #: Archive/reference routes never retype an artifact that already exists.
    preserve_existing_type: bool = False
    #: Reference has no single type; a new record there must declare one.
    requires_declared_type: bool = False


#: Ordered longest-directory-first so ``plans/backlog`` is tested before ``plans``.
CORPUS_ROUTES: tuple[CorpusRoute, ...] = (
    CorpusRoute(".agent/plans/backlog", ArtifactType.PLAN, CorpusLane.BACKLOG),
    CorpusRoute(".agent/plans", ArtifactType.PLAN, CorpusLane.ACTIVE),
    CorpusRoute(".agent/reviews", ArtifactType.REVIEW, CorpusLane.ACTIVE),
    CorpusRoute(".agent/handoffs", ArtifactType.HANDOFF, CorpusLane.ACTIVE),
    CorpusRoute(
        ".agent/for-review", ArtifactType.IMPLEMENTATION_REPORT, CorpusLane.ACTIVE
    ),
    CorpusRoute(
        ".agent/archive/plans", ArtifactType.PLAN, CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/archive/reviews", ArtifactType.REVIEW, CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/archive/handoffs", ArtifactType.HANDOFF, CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/reference", None, CorpusLane.REFERENCE,
        preserve_existing_type=True, requires_declared_type=True,
    ),
)

#: Filenames that route documents rather than being work. Excluded from the
#: corpus, not reported as unclassified: an index is not a missing artifact.
INDEX_FILENAMES: frozenset[str] = frozenset({"README.md", "index.md"})

#: Extensions the scanner will consider, mapped to their medium.
MEDIA_BY_SUFFIX: dict[str, str] = {
    ".md": ArtifactMedium.MARKDOWN,
    ".pdf": ArtifactMedium.PDF,
    ".html": ArtifactMedium.HTML,
}


def route_for_path(rel_path: str) -> CorpusRoute | None:
    """The route owning this repo-relative path, or None if it is outside the corpus."""
    normalized = (rel_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    parent, _, filename = normalized.rpartition("/")
    if not filename:
        return None
    for route in CORPUS_ROUTES:
        if parent == route.directory:
            return route
    return None


def is_index_document(rel_path: str) -> bool:
    normalized = (rel_path or "").replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] in INDEX_FILENAMES


def medium_for_path(rel_path: str) -> str | None:
    normalized = (rel_path or "").replace("\\", "/").lower()
    _, dot, suffix = normalized.rpartition(".")
    if not dot:
        return None
    return MEDIA_BY_SUFFIX.get(f".{suffix}")


# ---------------------------------------------------------------------------
# Authored metadata
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_STATUS_RE = re.compile(r"^\s*[-*]?\s*\*{0,2}status\*{0,2}\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: Keys the frontmatter block may carry that this module interprets. Relationship
#: keys are validated elsewhere (``normalize_declarations``) and pass through
#: untouched -- reconciliation never resolves or removes a relationship.
_METADATA_KEYS: frozenset[str] = frozenset({
    "artifact_schema", "artifact_uuid", "artifact_type", "artifact_status",
})

#: Keys an author must never write: menhir derives them from the source itself.
#: Present in a document, they are a stale copy of a derived fact, so they are
#: rejected rather than read.
DERIVED_KEYS: frozenset[str] = frozenset({
    "corpus_lane", "integrity", "integrity_algorithm", "version", "version_kind",
    "observed_commit", "size_bytes", "source_uuid", "resolution_status",
    "resolution_reason", "last_seen_at", "last_reconciled_at",
    "last_reconcile_basis", "schema_version",
})


@dataclass(frozen=True)
class DocumentMetadata:
    """What a document declares about itself, plus why any of it was refused.

    ``errors`` is populated instead of raising: one malformed record must not
    abort a corpus scan, and an author needs the exact reason rather than a
    stack trace.
    """

    artifact_uuid: str | None = None
    artifact_type: str | None = None
    artifact_status: str | None = None
    schema: int | None = None
    title: str | None = None
    raw_status_header: str | None = None
    has_frontmatter: bool = False
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """Split a leading ``---`` block into a flat mapping, body, and errors.

    A deliberately small parser rather than a YAML dependency. The authoring
    contract allows scalars and simple inline/dash lists; anything richer is
    reported as an error instead of being guessed at, which keeps "menhir read
    my metadata differently than I wrote it" off the table.
    """
    errors: list[str] = []
    if not text.startswith("---"):
        return {}, text, ()

    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text, ()

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text, ("frontmatter_not_terminated",)

    mapping: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in lines[1:end]:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            if current_key is None:
                errors.append("frontmatter_list_item_without_key")
                continue
            item = line.lstrip()[2:].strip().strip("'\"")
            existing = mapping.get(current_key)
            if isinstance(existing, list):
                existing.append(item)
            elif existing in (None, ""):
                mapping[current_key] = [item]
            else:
                mapping[current_key] = [existing, item]
            continue
        if ":" not in line:
            errors.append("frontmatter_line_without_key")
            continue
        if line[0] in " \t":
            errors.append("frontmatter_nested_mapping_unsupported")
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [
                part.strip().strip("'\"")
                for part in value[1:-1].split(",")
                if part.strip()
            ]
            mapping[key] = items
        elif value:
            mapping[key] = value.strip("'\"")
        else:
            mapping[key] = ""
        current_key = key

    body = "\n".join(lines[end + 1:])
    return mapping, body, tuple(errors)


def read_document_metadata(text: str, *, route_type: str | None = None) -> DocumentMetadata:
    """Read the authoring block and H1 title out of a document's text.

    ``route_type`` is only used to check a declared status against the type the
    directory asserts. A disagreement is an error, never a silent preference for
    one side: the route and the declaration are both authored, and picking a
    winner would hide the mistake.
    """
    mapping, body, errors_tuple = parse_frontmatter(text)
    errors: list[str] = list(errors_tuple)

    title: str | None = None
    raw_status: str | None = None
    for line in body.splitlines()[:40]:
        if title is None:
            match = _H1_RE.match(line)
            if match:
                title = match.group(1).strip()
                continue
        if raw_status is None:
            match = _STATUS_RE.match(line)
            if match:
                raw_status = match.group(1).strip()

    for key in mapping:
        if key in DERIVED_KEYS:
            errors.append(f"derived_key_declared:{key}")

    declared_uuid = _single_value(mapping.get("artifact_uuid"))
    if declared_uuid is not None:
        if not _UUID_RE.match(declared_uuid):
            errors.append("invalid_artifact_uuid")
            declared_uuid = None
        else:
            declared_uuid = declared_uuid.lower()

    declared_type = _single_value(mapping.get("artifact_type"))
    if declared_type is not None:
        declared_type = declared_type.strip().lower()
        if declared_type not in ARTIFACT_TYPES:
            errors.append("unknown_artifact_type")
            declared_type = None
        elif route_type is not None and declared_type != route_type:
            errors.append("route_type_disagreement")

    effective_type = declared_type or route_type
    declared_status = _single_value(mapping.get("artifact_status"))
    if declared_status is not None:
        declared_status = declared_status.strip().upper()
        if effective_type is None:
            errors.append("status_without_type")
            declared_status = None
        elif declared_status not in valid_statuses(effective_type):
            errors.append("status_invalid_for_type")
            declared_status = None

    schema_raw = _single_value(mapping.get("artifact_schema"))
    schema: int | None = None
    if schema_raw is not None:
        try:
            schema = int(schema_raw)
        except ValueError:
            errors.append("invalid_artifact_schema")

    return DocumentMetadata(
        artifact_uuid=declared_uuid,
        artifact_type=declared_type,
        artifact_status=declared_status,
        schema=schema,
        title=title,
        raw_status_header=raw_status,
        has_frontmatter=bool(mapping),
        errors=tuple(errors),
    )


def _single_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return None if not value else str(value[0])
    text = str(value).strip()
    return text or None


def sha256_bytes(payload: bytes) -> str:
    """Raw-byte digest. No normalization: a CRLF change *is* a source change.

    Normalizing line endings or Markdown before hashing would make the hash
    agree with how a document renders rather than with what the file contains,
    and integrity evidence has to answer the second question.
    """
    return hashlib.sha256(payload).hexdigest()


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
        return self.source_uuid or f"{self.artifact_uuid}:{self.medium}:{self.path or ''}"


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
            "plan_digest": self.plan_digest,
            "counts": dict(self.counts),
            "actions": [a.as_dict() for a in self.actions],
            "contradictions": [c.as_dict() for c in self.contradictions],
        }


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


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
    ) -> None:
        self.entries = list(entries)
        self.snapshots = list(snapshots)
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

        self.snapshots_by_key: dict[tuple[str, str, str], list[ArtifactSourceSnapshot]] = {}
        self.snapshots_by_artifact: dict[str, list[ArtifactSourceSnapshot]] = {}
        for snapshot in self.snapshots:
            self.snapshots_by_key.setdefault(snapshot.key, []).append(snapshot)
            self.snapshots_by_artifact.setdefault(snapshot.artifact_uuid, []).append(snapshot)

    def unclaimed_entries(self) -> list[CorpusEntry]:
        return [e for e in self.entries if e.key not in self.claimed_entries]

    def unclaimed_snapshots(self) -> list[ArtifactSourceSnapshot]:
        return [s for s in self.snapshots if s.identity not in self.claimed_sources]

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


def plan_reconciliation(
    *,
    repository: str,
    entries: Sequence[CorpusEntry],
    snapshots: Sequence[ArtifactSourceSnapshot],
    renames: Sequence[GitRename] = (),
    observed_commit: str | None = None,
) -> ReconciliationReport:
    """Decide what each source record should become. Pure; no I/O.

    Passes run strongest-evidence-first and each one only sees what the earlier
    passes left alone. The order is the whole safety argument: a declared UUID
    beats a path, a path beats Git history, Git history beats a matching hash,
    and a matching hash is admissible only when nothing else could explain it.
    """
    scoped_entries = sorted(
        (e for e in entries if e.repository == repository), key=lambda e: e.path
    )
    scoped_snapshots = sorted(
        (s for s in snapshots if (s.repository or "") == repository),
        key=lambda s: (s.path or "", s.artifact_uuid),
    )
    state = _PlanState(scoped_entries, scoped_snapshots)

    _plan_duplicate_locators(state)
    _plan_declared_uuids(state)
    _plan_exact_locators(state)
    _plan_git_renames(state, renames, repository)
    _plan_hash_matches(state)
    _plan_registrations(state)
    _plan_unresolved_sources(state)

    actions = tuple(sorted(state.actions, key=lambda a: a.sort_key))
    contradictions = tuple(_lane_contradictions(state))
    counts = _summarize(state, actions, contradictions)
    digest = compute_plan_digest(
        repository=repository,
        observed_commit=observed_commit,
        entries=scoped_entries,
        snapshots=scoped_snapshots,
        actions=actions,
    )
    return ReconciliationReport(
        repository=repository,
        observed_commit=observed_commit,
        actions=actions,
        contradictions=contradictions,
        plan_digest=digest,
        counts=counts,
    )


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
        if not candidates:
            continue  # a pre-minted UUID on a new document; registration handles it

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

        if len(candidates) > 1:
            state.claim(
                ReconciliationAction(
                    kind=ActionKind.CONFLICT,
                    conflict_kind=ConflictKind.UUID_LOCATOR_DISAGREEMENT,
                    repository=entry.repository,
                    medium=entry.medium,
                    path=entry.path,
                    artifact_uuid=declared_uuid,
                    lane=entry.lane,
                    reason="declared_uuid_has_multiple_sources_of_this_medium",
                    detail=tuple(sorted(s.path or "" for s in candidates)),
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


def _plan_git_renames(
    state: _PlanState, renames: Sequence[GitRename], repository: str
) -> None:
    """Recorded history: this exact path became that exact path.

    Byte equality is deliberately not required. A commit can rename and edit in
    one step, and demanding an unchanged hash would turn the most reliable
    evidence available into the one that fires least often.
    """
    scoped = [
        r for r in renames if r.repository in (None, "", repository)
    ]
    by_new_path: dict[str, list[GitRename]] = {}
    for rename in scoped:
        by_new_path.setdefault(rename.new_path, []).append(rename)

    for entry in state.unclaimed_entries():
        group = by_new_path.get(entry.path) or []
        if len(group) != 1:
            continue
        old_path = group[0].old_path
        candidates = [
            s
            for s in state.snapshots_by_key.get((entry.repository, entry.medium, old_path), [])
            if s.identity not in state.claimed_sources
        ]
        if len(candidates) != 1:
            continue
        if entry.key in state.claimed_destinations:
            continue
        snapshot = candidates[0]
        action = _relocate_or_refresh(entry, snapshot, MatchBasis.GIT_RENAME)
        state.claim(action, entry=entry, snapshot=snapshot)


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
        if status is None:
            # A prose `Status:` header is authored intent in the legacy corpus,
            # so it is transcribed; anything unmappable lands in the type's
            # initial state and keeps the raw header for a human to read.
            status, _reason = status_from_header(raw_status, artifact_type)
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
            reason="source_moved" if not content_changed else "source_moved_and_changed",
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


def _lane_contradictions(state: _PlanState) -> list[LaneContradiction]:
    """Where routing and lifecycle disagree. A report line, not a transition."""
    found: list[LaneContradiction] = []
    matched_by_key = {
        (a.repository, a.medium, a.path): a
        for a in state.actions
        if a.kind in (ActionKind.NOOP, ActionKind.REFRESH_SOURCE, ActionKind.RELOCATE_SOURCE)
    }
    by_artifact = {
        s.artifact_uuid: s for s in state.snapshots if s.artifact_uuid
    }
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
_TERMINALISH: frozenset[str] = frozenset({
    "IMPLEMENTED", "COMPLETE", "SUPERSEDED", "DEFERRED",
})


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
            by_conflict[action.conflict_kind] = by_conflict.get(action.conflict_kind, 0) + 1

    by_lane: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for entry in state.entries:
        by_lane[entry.lane] = by_lane.get(entry.lane, 0) + 1
        key = entry.effective_type or "undeclared"
        by_type[key] = by_type.get(key, 0) + 1

    return {
        "entries": len(state.entries),
        "sources": len(state.snapshots),
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
    entries: Iterable[CorpusEntry],
    snapshots: Iterable[ArtifactSourceSnapshot],
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
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    action: ReconciliationAction, *, observed_commit: str | None, observed_at: str,
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
