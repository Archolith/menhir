"""Classification registry for content-bearing columns in the SQLite telemetry sidecar.

Erasure of user and memory content from the sidecar cannot be proven complete unless
every content-bearing column is classified: which columns carry content, and by what
subject key each content row can be addressed for deletion. An unclassified new column
is a silent erasure gap -- content would survive a UUID-keyed purge unnoticed.

This module is the single source of truth for that classification. It enumerates the
columns known to carry user or memory text (``CONTENT_COLUMNS``), their erasure shape,
and the subject keys used to address them. It also keeps an explicit allowlist of TEXT
columns known NOT to carry user or memory content (``NON_CONTENT_COLUMNS``): timestamps,
ids, uuids, states, kinds, reasons, hostnames, tokens, and similar operational metadata.

The guarding test (``tests/test_sidecar_erasure_inventory.py``) walks the real schema and
fails on any TEXT column that is neither classified as content nor allowlisted, so a new
or renamed content-bearing column cannot silently slip through the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErasureShape(str, Enum):
    """How content in a column is addressed for erasure.

    - DIRECT_SUBJECT_UUID: content keyed by a single subject UUID (e.g. a memory node).
    - TWO_PARTY_UUID: content keyed by two UUIDs (e.g. survivor and absorbed node).
    - NAMESPACE_KEYED: content keyed by a namespace rather than a single UUID.
    - DERIVABLE_SUBJECT: subject can be derived from surrounding row data.
    - UNADDRESSABLE: carries content but has no subject key, so a UUID-keyed purge
      cannot reach it.
    """

    DIRECT_SUBJECT_UUID = "DIRECT_SUBJECT_UUID"
    TWO_PARTY_UUID = "TWO_PARTY_UUID"
    NAMESPACE_KEYED = "NAMESPACE_KEYED"
    DERIVABLE_SUBJECT = "DERIVABLE_SUBJECT"
    UNADDRESSABLE = "UNADDRESSABLE"


@dataclass(frozen=True)
class ContentColumn:
    """A single content-bearing column in the sidecar schema.

    Attributes:
        table: The sidecar table name.
        column: The column name within ``table``.
        shape: How the column's content is addressed for erasure.
        key_columns: Subject columns that address content rows for erasure. Must be
            empty for ``UNADDRESSABLE`` shapes and non-empty otherwise.
        note: Human-readable note on why the column is classified this way.
    """

    table: str
    column: str
    shape: ErasureShape
    key_columns: tuple[str, ...]
    note: str = ""


CONTENT_COLUMNS: tuple[ContentColumn, ...] = (
    ContentColumn(
        table="memory_revisions",
        column="old_value",
        shape=ErasureShape.DIRECT_SUBJECT_UUID,
        key_columns=("node_uuid",),
        note="Prior value of a memory field; addressed by the subject memory node_uuid.",
    ),
    ContentColumn(
        table="memory_revisions",
        column="new_value",
        shape=ErasureShape.DIRECT_SUBJECT_UUID,
        key_columns=("node_uuid",),
        note="Updated value of a memory field; addressed by the subject memory node_uuid.",
    ),
    ContentColumn(
        table="lifecycle_actions",
        column="notes",
        shape=ErasureShape.DIRECT_SUBJECT_UUID,
        key_columns=("node_uuid",),
        note="Lifecycle notes tied to a memory node_uuid.",
    ),
    ContentColumn(
        table="merge_audit",
        column="snapshot_json",
        shape=ErasureShape.TWO_PARTY_UUID,
        # Namespace lineage is a key here, not decoration. A namespace erasure reaches this
        # table through captured member uuids -- but a historical merge whose participants are
        # both long gone from the graph has no uuid in that captured set, so the row would
        # survive the erasure of its own namespace. The lineage columns are the durable
        # selector for exactly that case, which is why they were added.
        key_columns=(
            "survivor_uuid",
            "absorbed_uuid",
            "survivor_namespace",
            "absorbed_namespace",
        ),
        note=(
            "Absorbed-node snapshot used for unmerge; addressed by both the survivor "
            "and the absorbed memory UUIDs."
        ),
    ),
    ContentColumn(
        table="mcp_events",
        column="payload_preview",
        shape=ErasureShape.DIRECT_SUBJECT_UUID,
        key_columns=("node_uuid",),
        note=(
            "Carries a preview of memory text. Durable subject lineage (node_uuid) was "
            "added by CF-165 Phase C, so a UUID-keyed purge can now reach it. Rows "
            "written before that migration have NULL lineage and are not addressable."
        ),
    ),
    ContentColumn(
        table="recall_lab_runs",
        column="query",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Recall-lab payload carrying user query and retrieved memory text; this table has a namespace column, so it is purgeable by namespace."
        ),
    ),
    ContentColumn(
        table="recall_lab_runs",
        column="arms_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Recall-lab payload carrying user query and retrieved memory text; this table has a namespace column, so it is purgeable by namespace."
        ),
    ),
    ContentColumn(
        table="recall_lab_runs",
        column="request_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Recall-lab payload carrying user query and retrieved memory text; this table has a namespace column, so it is purgeable by namespace."
        ),
    ),
    ContentColumn(
        table="recall_lab_runs",
        column="result_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Recall-lab payload carrying user query and retrieved memory text; this table has a namespace column, so it is purgeable by namespace."
        ),
    ),
    ContentColumn(
        table="extraction_lab_runs",
        column="current_message",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Extraction-lab payload carrying raw message and extracted memory text. Durable "
            "subject lineage (namespace) was added by CF-165 Phase C, so a namespace-keyed "
            "purge can now reach it. Rows written before that migration have NULL lineage "
            "and are not addressable."
        ),
    ),
    ContentColumn(
        table="extraction_lab_runs",
        column="arms_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Extraction-lab payload carrying raw message and extracted memory text. Durable "
            "subject lineage (namespace) was added by CF-165 Phase C, so a namespace-keyed "
            "purge can now reach it. Rows written before that migration have NULL lineage "
            "and are not addressable."
        ),
    ),
    ContentColumn(
        table="extraction_lab_runs",
        column="request_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Extraction-lab payload carrying raw message and extracted memory text. Durable "
            "subject lineage (namespace) was added by CF-165 Phase C, so a namespace-keyed "
            "purge can now reach it. Rows written before that migration have NULL lineage "
            "and are not addressable."
        ),
    ),
    ContentColumn(
        table="extraction_lab_runs",
        column="result_json",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Extraction-lab payload carrying raw message and extracted memory text. Durable "
            "subject lineage (namespace) was added by CF-165 Phase C, so a namespace-keyed "
            "purge can now reach it. Rows written before that migration have NULL lineage "
            "and are not addressable."
        ),
    ),
    ContentColumn(
        table="failure_events",
        column="details_json",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Diagnostic payload that can embed episode content; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="episode_task_events",
        column="details_json",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Diagnostic payload that can embed episode content; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="lifecycle_events",
        column="details_json",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Diagnostic payload that can embed episode content; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="failure_events",
        column="error",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Error text can embed the episode content that triggered it; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="llm_usage_events",
        column="provider_usage_json",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Provider usage payload can embed prompt/completion text; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="llm_usage_events",
        column="error",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("episode_uuid",),
        note=(
            "Error text can embed prompt content; subject derived from episode_uuid."
        ),
    ),
    ContentColumn(
        table="mcp_events",
        column="error",
        shape=ErasureShape.DIRECT_SUBJECT_UUID,
        key_columns=("node_uuid",),
        note=(
            "Tool error text can embed request payload content. Durable subject lineage "
            "(node_uuid) was added by CF-165 Phase C, so a UUID-keyed purge can now reach "
            "it. Rows written before that migration have NULL lineage and are not "
            "addressable."
        ),
    ),
    ContentColumn(
        table="recall_receipts",
        column="reason",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("session_id",),
        note=(
            "Operator-authored rating text; only session-scoped, with no node key."
        ),
    ),
)


NON_CONTENT_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        # mcp_events
        ("mcp_events", "started_at"),
        ("mcp_events", "completed_at"),
        ("mcp_events", "operation"),
        ("mcp_events", "kind"),
        ("mcp_events", "namespace"),
        ("mcp_events", "node_uuid"),
        # failure_events
        ("failure_events", "recorded_at"),
        ("failure_events", "operation"),
        ("failure_events", "episode_uuid"),
        ("failure_events", "failure_stage"),
        ("failure_events", "classification"),
        ("failure_events", "worker_id"),
        ("failure_events", "error_type"),
        # episode_task_events
        ("episode_task_events", "recorded_at"),
        ("episode_task_events", "episode_uuid"),
        ("episode_task_events", "parent_task"),
        ("episode_task_events", "child_task"),
        ("episode_task_events", "phase"),
        ("episode_task_events", "kind"),
        ("episode_task_events", "model"),
        ("episode_task_events", "endpoint"),
        ("episode_task_events", "scheduler_task"),
        # llm_usage_events
        ("llm_usage_events", "call_id"),
        ("llm_usage_events", "recorded_at"),
        ("llm_usage_events", "run_id"),
        ("llm_usage_events", "episode_uuid"),
        ("llm_usage_events", "operation"),
        ("llm_usage_events", "kind"),
        ("llm_usage_events", "model"),
        ("llm_usage_events", "endpoint"),
        ("llm_usage_events", "status"),
        # lifecycle_events
        ("lifecycle_events", "recorded_at"),
        ("lifecycle_events", "phase"),
        ("lifecycle_events", "event"),
        ("lifecycle_events", "status"),
        ("lifecycle_events", "episode_uuid"),
        # lifecycle_actions
        ("lifecycle_actions", "recorded_at"),
        ("lifecycle_actions", "action"),
        ("lifecycle_actions", "node_uuid"),
        ("lifecycle_actions", "session_id"),
        ("lifecycle_actions", "trigger"),
        ("lifecycle_actions", "before_freshness"),
        ("lifecycle_actions", "after_freshness"),
        # memory_revisions
        ("memory_revisions", "recorded_at"),
        ("memory_revisions", "node_uuid"),
        ("memory_revisions", "field"),
        ("memory_revisions", "changed_by"),
        ("memory_revisions", "episode_uuid"),
        # merge_audit
        ("merge_audit", "recorded_at"),
        ("merge_audit", "survivor_uuid"),
        ("merge_audit", "absorbed_uuid"),
        ("merge_audit", "survivor_namespace"),
        ("merge_audit", "absorbed_namespace"),
        # conflict_resolutions
        ("conflict_resolutions", "resolved_at"),
        ("conflict_resolutions", "uuid_a"),
        ("conflict_resolutions", "uuid_b"),
        ("conflict_resolutions", "status"),
        ("conflict_resolutions", "group_id"),
        ("conflict_resolutions", "action"),
        ("conflict_resolutions", "reviewed_by"),
        # client_registry
        ("client_registry", "client_id"),
        ("client_registry", "client_name"),
        ("client_registry", "first_accessed"),
        ("client_registry", "last_accessed"),
        # session_registry
        ("session_registry", "session_id"),
        ("session_registry", "client_id"),
        ("session_registry", "client_name"),
        ("session_registry", "first_accessed"),
        ("session_registry", "last_accessed"),
        # recall_receipts
        ("recall_receipts", "token"),
        ("recall_receipts", "operation"),
        ("recall_receipts", "client_id"),
        ("recall_receipts", "session_id"),
        ("recall_receipts", "created_at"),
        ("recall_receipts", "score_label"),
        ("recall_receipts", "rated_at"),
        # recall_lab_runs
        ("recall_lab_runs", "recorded_at"),
        ("recall_lab_runs", "preset"),
        ("recall_lab_runs", "namespace"),
        ("recall_lab_runs", "judge_model"),
        ("recall_lab_runs", "winner_id"),
        ("recall_lab_runs", "tied_ids_json"),
        # extraction_lab_runs
        ("extraction_lab_runs", "recorded_at"),
        ("extraction_lab_runs", "namespace"),
    }
)


def classified_columns() -> frozenset[tuple[str, str]]:
    """Return the (table, column) pairs covered by ``CONTENT_COLUMNS``."""
    return frozenset((c.table, c.column) for c in CONTENT_COLUMNS)
