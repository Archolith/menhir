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

# Facade: the reconciliation implementation moved to the sibling modules
# named below. Every name this module originally exposed -- public and
# private -- is re-exported here unchanged, so existing import sites keep
# working.

from menhir.domain.artifact_reconciliation_metadata import (
    DERIVED_KEYS as DERIVED_KEYS,
    DocumentMetadata as DocumentMetadata,
    parse_frontmatter as parse_frontmatter,
    read_document_metadata as read_document_metadata,
    sha256_bytes as sha256_bytes,
    _H1_RE as _H1_RE,
    _METADATA_KEYS as _METADATA_KEYS,
    _STATUS_RE as _STATUS_RE,
    _UUID_RE as _UUID_RE,
    _single_value as _single_value,
)

from menhir.domain.artifact_reconciliation_model import (
    ARTIFACT_METADATA_SCHEMA as ARTIFACT_METADATA_SCHEMA,
    ARTIFACT_SOURCE_SCHEMA_VERSION as ARTIFACT_SOURCE_SCHEMA_VERSION,
    ActionKind as ActionKind,
    ArtifactSourceSnapshot as ArtifactSourceSnapshot,
    ConflictKind as ConflictKind,
    CorpusEntry as CorpusEntry,
    CorpusLane as CorpusLane,
    CORPUS_LANES as CORPUS_LANES,
    EXECUTABLE_LANES as EXECUTABLE_LANES,
    GitRename as GitRename,
    INTEGRITY_ALGORITHM as INTEGRITY_ALGORITHM,
    LaneContradiction as LaneContradiction,
    MatchBasis as MatchBasis,
    ReconciliationAction as ReconciliationAction,
    ReconciliationReport as ReconciliationReport,
    ResolutionStatus as ResolutionStatus,
    SAFE_ACTION_KINDS as SAFE_ACTION_KINDS,
    SourceObservation as SourceObservation,
    VersionKind as VersionKind,
    WorkArtifactIdentitySnapshot as WorkArtifactIdentitySnapshot,
    locator_key as locator_key,
    observation_from_action as observation_from_action,
)

from menhir.domain.artifact_reconciliation_plan_passes import (
    _plan_duplicate_locators as _plan_duplicate_locators,
    _plan_exact_locators as _plan_exact_locators,
    _plan_hash_matches as _plan_hash_matches,
    _plan_registrations as _plan_registrations,
    _plan_remaining_unscoped_sources as _plan_remaining_unscoped_sources,
    _plan_unresolved_sources as _plan_unresolved_sources,
    _plan_unscoped_path_conflicts as _plan_unscoped_path_conflicts,
)

from menhir.domain.artifact_reconciliation_plan_renames import (
    _plan_git_renames as _plan_git_renames,
)

from menhir.domain.artifact_reconciliation_plan_state import (
    _PlanState as _PlanState,
    _relocate_or_refresh as _relocate_or_refresh,
)

from menhir.domain.artifact_reconciliation_plan_uuid import (
    _plan_declared_uuids as _plan_declared_uuids,
)

from menhir.domain.artifact_reconciliation_planner import (
    _TERMINALISH as _TERMINALISH,
    _lane_contradictions as _lane_contradictions,
    _summarize as _summarize,
    compute_plan_digest as compute_plan_digest,
    plan_reconciliation as plan_reconciliation,
)

from menhir.domain.artifact_reconciliation_routing import (
    CORPUS_ROUTES as CORPUS_ROUTES,
    CorpusRoute as CorpusRoute,
    INDEX_FILENAMES as INDEX_FILENAMES,
    MEDIA_BY_SUFFIX as MEDIA_BY_SUFFIX,
    is_index_document as is_index_document,
    medium_for_path as medium_for_path,
    route_for_path as route_for_path,
)
