"""Neo4j text inventory: which (label, property) pairs hold memory text (erasure rebuild R1.1).

The graph counterpart of ``telemetry/erasure_inventory.py`` ``CONTENT_COLUMNS``. An erase must find
every holder of an erased memory's words; the T14 lint
(``tests/test_erasure_text_writer_lint.py``) fails a writer of an inventoried pair that does not
go through a fence. Plan: ``.agent/plans/menhir-erasure-audit-remediation-plan-2026-11-11.md`` R1.1
and plan v2 section 3.4.

``Entity`` text is open-ended (Graphiti custom attributes, View fields), so it is inverted: every
Entity property NOT in :data:`ENTITY_STRUCTURAL_PROPERTIES` counts as text. A new property is text
until someone argues otherwise in that allowlist.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class HolderSelector(str, Enum):
    """How an erase finds the holders of one memory's text."""

    UUID = "uuid"  # the node is in the erase closure by its own uuid / turn_id
    CLOSURE = "closure"  # reached through the seal closure (queue node, twins, projections)
    PROVENANCE_EDGE = "provenance_edge"  # MENTIONS / RETENTION_SOURCE from an erased episode
    EPISODES_LIST = "episodes_list"  # the item's own ``episodes`` provenance list
    EDGE_UUID_REF = "edge_uuid_ref"  # references a deleted or flagged edge by uuid
    PROVENANCE_LIST = "provenance_list"  # a uuid list property such as ``absorbed_from``
    SOURCE_UUID = "source_uuid"  # a scalar back-reference such as ``raw_capture_for``
    NEEDLE = "needle"  # no structural link; found by an exact uuid needle in serialized text
    UNCLASSIFIED = "unclassified"  # holder selection not designed yet (see ``finding``)


class EraseAction(str, Enum):
    DELETE = "delete"
    DELETE_OR_TOMBSTONE = "delete_or_tombstone"
    SOLE_SOURCE_DELETE_ELSE_SCRUB = "sole_source_delete_else_scrub_and_flag"
    DEAD_DELETE_LIVE_SCRUB = "dead_delete_live_scrub_and_flag"
    DELETE_WITH_OP = "delete_with_op"
    SCRUB = "scrub"


@dataclass(frozen=True)
class TextHolder:
    """One label or relationship type whose properties carry memory text."""

    name: str  # node label or relationship type
    kind: str  # "node" | "relationship"
    text_properties: tuple[str, ...]
    selectors: tuple[HolderSelector, ...]
    action: EraseAction
    #: Remediation finding that still has to close a gap for this holder (R2 / P5), or "".
    finding: str = ""
    #: True when ``text_properties`` is not exhaustive: every non-structural property is text.
    open_ended: bool = False
    #: Restrict the entry to nodes carrying this marker property (e.g. raw captures).
    discriminator: str = ""


#: Entity properties that never hold memory text. Everything else on an Entity is text.
#: Each group says why it is structural; a property belongs here only with that argument.
ENTITY_STRUCTURAL_PROPERTIES: frozenset[str] = frozenset(
    {
        # Identity and tenancy. ``name`` is the shared identity key that dedupe matches on;
        # a sole-source entity is deleted with its memory, a shared one keeps its name.
        "uuid", "name", "name_embedding", "group_id", "namespace", "labels",
        # Timestamps and lifecycle bookkeeping.
        "created_at", "updated_at", "valid_at", "expired_at", "invalid_at", "last_accessed",
        "last_seen_at", "first_seen_at", "promoted_at", "closed_at", "recorded_at",
        "decay_last_selected_at", "freshness", "sharpness", "hot_count", "rehydration_count",
        "seen_count", "rank", "priority", "enrichment_priority",
        # Classification flags and enums.
        "scope", "source", "source_confidence", "source_kind", "source_client", "user_flagged",
        "bootstrap_scope", "entity_role", "is_self", "is_structural", "identity_source",
        "category", "type", "status", "medium", "role", "declarant", "directness", "retired",
        "retired_reason", "superseded", "pending_review", "enabled",
        # Erasure and rederive state (uuids, epochs, flags).
        "rederive_pending", "rederive_reason", "rederive_flagged_at", "created_epoch",
        "erasure_scrubbed_epoch", "raw_capture", "raw_capture_for", "absorbed_from",
        "merged_from", "merge_irreversible",
        # Conflict and supersession links (uuids and enums).
        "conflict_group_id", "conflict_status", "conflict_created_at", "supersedes",
        "superseded_by", "resolution_status", "resolution_reason",
        # Code-structure graph (paths, symbols, commits: repository metadata, not memory).
        "structure_project_id", "structure_project", "structure_role", "structure_path",
        "symbol_signature", "symbol_parent", "symbol_line", "symbol_kind", "symbol_decorator",
        "file_mtime", "repository", "indexed_repository", "indexed_commit", "indexed_dirty",
        "locator_path", "locator_repository", "current_locator_key", "project_id", "code_ref",
        "document_type", "digest",
        # Views: keys, versions, fences. view_name / view_summary are text and are NOT here.
        "view_kind", "view_key", "view_class", "view_current", "qs_current", "qs_key",
        "ss_view_key_current", "view_fence_generation", "evidence_generation", "version",
        "version_kind", "schema_version", "scalar_basis", "supporting_event_count",
        "episode_uuids", "subject_uuid", "subject_uuids",
        # Counting-lane decisions (ids and enums).
        "count_status", "count_decided_at", "count_vote", "count_target_id", "count_run_id",
        "count_reason", "count_attempts",
        # Embedding bookkeeping (the vector of a text field is scrubbed with that field).
        "content_embedding_model", "content_embedding_at",
        # Node kinds and tiers that recall and the cascade filter on (R2: the CF-330 scrub
        # removes every non-structural key, so a missing flag here would be deleted).
        "is_view", "is_quantstate", "is_artifact", "is_verifier", "is_evidence_projection",
        "self_tier", "effective_tier", "importance", "needs_review", "review_flagged_at",
        "ttl_expires", "target_date", "session_id", "user_id", "_menhir_schema_v",
        "belief_branch", "belief_commit", "edge_count", "corroboration", "sources",
        "last_merge_op_id", "erased_at", "erasure_compensation_pending", "dirty_at",
        "structure_dirty", "last_event_after_hash", "last_event_mtime", "last_event_op",
        "last_event_source_client", "smoke_project",
        # Evidence projections, scalar views and quantstate bookkeeping (ids, ranks, enums).
        "evidence_finalized", "evidence_finalized_at", "evidence_quarantined",
        "evidence_quarantine_reason", "evidence_tier", "evidence_rank", "evidence_type",
        "evidence_projection_of", "view_subtype", "view_uuid", "view_subject_uuid", "ss_kind",
        "ss_attribute", "ss_scope", "ss_unit", "qs_counter",
        # Artifacts and candidates: ids, paths, enums and counters (labels and notes are text).
        "artifact_id", "artifact_type", "artifact_status", "artifact_source", "artifact_anchors",
        "candidate_kind", "candidate_type", "candidate_first_seen", "candidate_last_seen",
        "candidate_distinct_sessions", "candidate_evidence_strength",
        # Verifiers: probed env/settings values, never memory text (registry EXEMPT reason).
        "verifier_key", "verifier_kind", "verifier_params", "register_subject",
        "register_counter", "last_probe_at", "last_probe_status", "last_verified_at",
        "last_value", "last_display",
        # Structure-graph project bookkeeping.
        "files_discovered", "files_eligible", "files_indexed", "partial_index",
        "scan_fingerprint", "symbols_truncated",
        # Episode processing fields the cascade tombstone writes.
        "processing_state", "processing_stage", "processing_substage",
        "processing_substage_started_at", "processing_progress", "processing_error",
        "processing_heartbeat_at", "processing_completed_at",
    }
)

#: The memory text an Entity can hold today that the structural allowlist does not cover, named
#: for documentation and tests; the authoritative rule is "not in ENTITY_STRUCTURAL_PROPERTIES".
ENTITY_KNOWN_TEXT_PROPERTIES: tuple[str, ...] = (
    "summary", "content", "original_content", "view_name", "view_summary", "attributes",
    "content_embedding", "merge_audit", "candidate_notes",
)

GRAPH_TEXT_INVENTORY: tuple[TextHolder, ...] = (
    TextHolder(
        "Episodic", "node", ("content", "source_description", "name"),
        (HolderSelector.UUID, HolderSelector.CLOSURE), EraseAction.DELETE_OR_TOMBSTONE,
    ),
    TextHolder(
        "Entity", "node", ENTITY_KNOWN_TEXT_PROPERTIES,
        (HolderSelector.PROVENANCE_EDGE, HolderSelector.PROVENANCE_LIST),
        EraseAction.SOLE_SOURCE_DELETE_ELSE_SCRUB,
        finding="CF-332 absorbed_from",
        open_ended=True,
    ),
    TextHolder(
        "Entity", "node", ("content", "name"), (HolderSelector.SOURCE_UUID,),
        EraseAction.DELETE, finding="CF-327 raw capture not in closure or cascade",
        discriminator="raw_capture_for",
    ),
    TextHolder(
        "RELATES_TO", "relationship",
        # Of the anchored-time props only time_expression quotes the memory; the rest are enums,
        # timestamps, an edge uuid (time_anchor_ref) and a schema/model tag (time_contract).
        ("fact", "fact_embedding", "name", "time_expression"),
        (HolderSelector.EPISODES_LIST,), EraseAction.DEAD_DELETE_LIVE_SCRUB,
    ),
    TextHolder(
        "TurnEvidence", "node", ("text",), (HolderSelector.UUID, HolderSelector.CLOSURE),
        EraseAction.DELETE,
    ),
    TextHolder(
        "TypedAssertion", "node", ("stated_span", "value", "value_json", "subject_display"),
        (HolderSelector.SOURCE_UUID,), EraseAction.DELETE,
    ),
    TextHolder(
        "EdgeInvalidationConflict", "node", ("kept_fact", "cause_fact"),
        # cause_episode_uuids (CF-331): the cause edge may never have persisted.
        (HolderSelector.EDGE_UUID_REF, HolderSelector.PROVENANCE_LIST), EraseAction.DELETE_WITH_OP,
    ),
    TextHolder(
        "Entity", "node", ("merge_audit",), (HolderSelector.NEEDLE,), EraseAction.SCRUB,
    ),
)


def inventoried_pairs() -> frozenset[tuple[str, str]]:
    """Every explicitly listed (label or rel type, property) pair."""
    return frozenset(
        (holder.name, prop) for holder in GRAPH_TEXT_INVENTORY for prop in holder.text_properties
    )


def is_text_property(label: str, prop: str) -> bool:
    """True when writing ``prop`` on a node/relationship of ``label`` writes memory text."""
    if label == "Entity":
        return prop not in ENTITY_STRUCTURAL_PROPERTIES
    return (label, prop) in inventoried_pairs()


def entity_text_properties(props: Iterable[str]) -> set[str]:
    """The text-bearing subset of an Entity's property names."""
    return {p for p in props if p not in ENTITY_STRUCTURAL_PROPERTIES}


def inventoried_labels() -> frozenset[str]:
    return frozenset(holder.name for holder in GRAPH_TEXT_INVENTORY)
