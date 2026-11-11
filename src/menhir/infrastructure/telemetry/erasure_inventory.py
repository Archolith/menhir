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
    - TWO_PARTY_UUID: content keyed by multiple party identifiers; any matching key erases it.
    - ANY_SUBJECT: content may be keyed by different subject dimensions (for example either
      node_uuid or namespace); any resolved matching key erases it.
    - NAMESPACE_KEYED: content keyed by a namespace rather than a single UUID.
    - DERIVABLE_SUBJECT: subject can be derived from surrounding row data.
    - UNADDRESSABLE: carries content but has no subject key, so a UUID-keyed purge
      cannot reach it.
    """

    DIRECT_SUBJECT_UUID = "DIRECT_SUBJECT_UUID"
    TWO_PARTY_UUID = "TWO_PARTY_UUID"
    ANY_SUBJECT = "ANY_SUBJECT"
    NAMESPACE_KEYED = "NAMESPACE_KEYED"
    DERIVABLE_SUBJECT = "DERIVABLE_SUBJECT"
    UNADDRESSABLE = "UNADDRESSABLE"
    #: Content addressed through a side table mapping row_key -> subject uuid.
    INDEXED_SUBJECT = "INDEXED_SUBJECT"


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
    # INDEXED_SUBJECT only: rows of ``table`` whose ``row_key`` appears in ``via_table``
    # (``via_key`` = row_key value) with ``via_subject`` in the erased node uuids.
    via_table: str | None = None
    via_key: str | None = None
    via_subject: str | None = None
    row_key: str | None = None
    # Extra SQL predicate the purge (not the residual count) applies, so rows it must not
    # touch stay visible as residue and the erasure quarantines instead of claiming success.
    purge_filter: str | None = None


# Unresolved saga rows still need their snapshot to replay; never purge under them.
_GRAPH_OPS_TERMINAL = "state IN ('COMMITTED', 'FAILED', 'REVERSED')"

_GRAPH_OPS_INDEX_NOTE = (
    "Journal snapshot/error text holds node properties and relationships (memory text). "
    "Addressed through graph_operation_subjects, which indexes every uuid the row mentions."
)

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
            "Absorbed-node snapshot used for unmerge; addressed by both participant UUIDs "
            "and durable namespace lineage."
        ),
    ),
    ContentColumn(
        table="mcp_events",
        column="payload_preview",
        shape=ErasureShape.ANY_SUBJECT,
        key_columns=("node_uuid", "namespace", "session_id"),
        note=(
            "Legacy rows can contain memory text; current writers privacy-minimize the preview. "
            "Rows are addressable by a specific node UUID when one exists or by the effective "
            "namespace for namespace erasure. Pre-lineage NULL/NULL rows remain explicit residue. "
            "CF-29 added session_id as a third key. Not for rows lacking a namespace -- the CF-165 "
            "insert trigger stamps 'default' on any row without one -- but so a single "
            "session's calls can be erased WITHOUT erasing the whole silo they landed in. "
            "Most traffic is namespace='default', where a namespace-keyed purge is far too "
            "wide to be the only option. ANY_SUBJECT ORs the keys, so this widens reach and "
            "never narrows it."
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
        table="recall_query_log",
        column="query",
        shape=ErasureShape.NAMESPACE_KEYED,
        key_columns=("namespace",),
        note=(
            "Opt-in recall query log (MENHIR_TELEMETRY_RECORD_RECALL_QUERIES); raw user query "
            "text, NULL when privacy_redact is on. Has a namespace column, so it is purgeable "
            "by namespace."
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
        shape=ErasureShape.ANY_SUBJECT,
        key_columns=("node_uuid", "namespace", "session_id"),
        note=(
            "Legacy tool errors can embed request payload content; current writers persist only a "
            "redacted/classification label. Rows are addressable by node UUID or effective "
            "namespace, and since CF-29 also by the session that made the call."
        ),
    ),
    ContentColumn(
        table="recall_receipts",
        column="reason",
        shape=ErasureShape.DERIVABLE_SUBJECT,
        key_columns=("session_id",),
        note=(
            "Legacy/operator-authored rating text. Current writers no longer persist this field "
            "and schema migration scrubs existing values, but any populated value is still "
            "content and must be reported when an erasure cannot derive its session ownership."
        ),
    ),
    ContentColumn(
        table="graph_operations",
        column="before_snapshot_json",
        shape=ErasureShape.INDEXED_SUBJECT,
        key_columns=("op_id",),
        note=_GRAPH_OPS_INDEX_NOTE,
        via_table="graph_operation_subjects",
        via_key="op_id",
        via_subject="subject_uuid",
        row_key="op_id",
        purge_filter=_GRAPH_OPS_TERMINAL,
    ),
    ContentColumn(
        table="graph_operations",
        column="last_error",
        shape=ErasureShape.INDEXED_SUBJECT,
        key_columns=("op_id",),
        note=_GRAPH_OPS_INDEX_NOTE + " Exception text can embed content.",
        via_table="graph_operation_subjects",
        via_key="op_id",
        via_subject="subject_uuid",
        row_key="op_id",
        purge_filter=_GRAPH_OPS_TERMINAL,
    ),
)


NON_CONTENT_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        # R1.2 (erasure audit remediation): stores whose tables were created outside this
        # inventory. Classified from their writers; each group says why it holds no memory text.
        # pending_actions: ids, the action enum and timestamps. Its `context` and
        # `failure_reason` columns are NOT here: they can hold memory text (CF-329, R2).
        ("pending_actions", "node_uuid"),
        ("pending_actions", "source_uuid"),
        ("pending_actions", "action"),
        ("pending_actions", "created_at"),
        ("pending_actions", "failed_at"),
        # migration_batches: the one-time Metric migration's manifest is a uuid list of
        # instrumentation nodes plus its SHA and state (migration_batches.py module docstring).
        ("migration_batches", "batch_id"),
        ("migration_batches", "classifier_version"),
        ("migration_batches", "manifest_sha256"),
        ("migration_batches", "manifest_json"),
        ("migration_batches", "state"),
        ("migration_batches", "created_at"),
        ("migration_batches", "approved_at"),
        ("migration_batches", "applied_at"),
        ("migration_batches", "menhir_commit"),
        # metric_receipts: fold receipts. grouping_json holds {operation, error_type} or
        # {node_uuid, field} (failure_counter_bridge.py, instability_counter_bridge.py);
        # source_row_ids_json holds integer row ids.
        ("metric_receipts", "op_id"),
        ("metric_receipts", "metric_uuid"),
        ("metric_receipts", "view_key"),
        ("metric_receipts", "receipt_kind"),
        ("metric_receipts", "source_table"),
        ("metric_receipts", "grouping_json"),
        ("metric_receipts", "query_version"),
        ("metric_receipts", "previous_receipt_op_id"),
        ("metric_receipts", "source_row_ids_json"),
        ("metric_receipts", "input_digest"),
        ("metric_receipts", "run_id"),
        ("metric_receipts", "created_at"),
        # erasure_residue_waiver: which column an operator accepted residue in, and their note
        # (operator text about the waiver, never memory content).
        ("erasure_residue_waiver", "table_name"),
        ("erasure_residue_waiver", "column_name"),
        ("erasure_residue_waiver", "accepted_at"),
        ("erasure_residue_waiver", "note"),
        # scheduler_leases: lease identity and heartbeats.
        ("scheduler_leases", "lease_name"),
        ("scheduler_leases", "owner_id"),
        ("scheduler_leases", "hostname"),
        ("scheduler_leases", "started_at"),
        ("scheduler_leases", "heartbeat_at"),
        # mcp_events
        ("mcp_events", "started_at"),
        ("mcp_events", "completed_at"),
        ("mcp_events", "operation"),
        ("mcp_events", "kind"),
        ("mcp_events", "namespace"),
        # CF-29 caller identity and execution stage. Operational metadata, not content: a client
        # name and tier come from server-side config, `stage` is one of four fixed labels, and the
        # ids are identifiers rather than text anyone wrote.
        #
        # `session_id` is listed here as NON-CONTENT while ALSO being a key_column on this table's
        # two content columns. Those are different questions and both answers are right: the column
        # holds nothing to erase, and it is a subject you can erase BY.
        ("mcp_events", "client_name"),
        ("mcp_events", "client_id"),
        ("mcp_events", "session_id"),
        ("mcp_events", "tier"),
        ("mcp_events", "stage"),
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
        # Exception class, HTTP status and provider code only, each held to an identifier shape
        # by openai_rate_limit.provider_error_code; message text never reaches this column.
        ("llm_usage_events", "error_code"),
        # Token counter and provider tier identifier (shape-checked); no content.
        ("llm_usage_events", "cache_write_input_tokens"),
        ("llm_usage_events", "service_tier"),
        # operator_warnings -- warning kind, LLM call kind, state, timestamps and the acknowledging
        # client name (server-side identity); no user or memory content.
        ("operator_warnings", "kind"),
        ("operator_warnings", "subject"),
        ("operator_warnings", "state"),
        ("operator_warnings", "first_seen_at"),
        ("operator_warnings", "last_seen_at"),
        ("operator_warnings", "acked_at"),
        ("operator_warnings", "acked_by"),
        ("operator_warnings", "ack_watermark"),
        ("operator_warnings", "resolved_at"),
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
        # recall_receipts -- free-text reasons are scrubbed by the schema migration and no
        # longer persisted by rate_recall; remaining fields are operational metrics only.
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
        # recall_query_log -- ids, scores and a query hash only; the query text is classified above.
        ("recall_query_log", "token"),
        ("recall_query_log", "operation"),
        ("recall_query_log", "created_at"),
        ("recall_query_log", "client_id"),
        ("recall_query_log", "session_id"),
        ("recall_query_log", "namespace"),
        ("recall_query_log", "preset"),
        ("recall_query_log", "query_sha256"),
        ("recall_query_log", "result_ids_json"),
        ("recall_query_log", "result_scores_json"),
        ("recall_query_log", "source_memory_ids_json"),
        # extraction_lab_runs
        ("extraction_lab_runs", "recorded_at"),
        ("extraction_lab_runs", "namespace"),
        # graph_operations -- request_json holds uuids and request metadata only; the snapshot
        # and error text are classified above.
        ("graph_operations", "op_id"),
        ("graph_operations", "batch_id"),
        ("graph_operations", "operation_kind"),
        ("graph_operations", "target_uuid"),
        ("graph_operations", "target_key"),
        ("graph_operations", "request_json"),
        ("graph_operations", "expected_after_sha256"),
        ("graph_operations", "state"),
        ("graph_operations", "created_at"),
        ("graph_operations", "updated_at"),
        ("graph_operations", "committed_at"),
        ("graph_operations", "reverses_op_id"),
        ("graph_operations", "owner_token"),
        ("graph_operations", "owner_heartbeat_at"),
        ("graph_operations", "owner_lease_expires_at"),
        ("graph_operations", "owner_death_attested_by"),
        ("graph_operations", "owner_death_attested_at"),
        ("graph_operations", "owner_death_attested_for_token"),
        ("graph_operations", "resolved_by_token"),
        # graph_operation_locks
        ("graph_operation_locks", "entity_uuid"),
        ("graph_operation_locks", "op_id"),
        ("graph_operation_locks", "operation_kind"),
        ("graph_operation_locks", "created_at"),
        # graph_operation_subjects / backfill marker
        ("graph_operation_subjects", "op_id"),
        ("graph_operation_subjects", "subject_uuid"),
        ("graph_operation_subjects_backfill", "done_at"),
        # erasure_subjects -- identifiers (uuids, namespace keys) and timestamps only
        ("erasure_subjects", "op_id"),
        ("erasure_subjects", "subject_type"),
        ("erasure_subjects", "subject_value"),
        ("erasure_subjects", "recorded_at"),
        ("erasure_subjects", "purged_at"),
        # erased_subject_denylist -- subject keys only, never content (P3 denylist)
        ("erased_subject_denylist", "subject_value"),
        ("erased_subject_denylist", "op_id"),
        # graph_operations.sidecar_compacted_at -- timestamp stamped by the compaction job
        ("graph_operations", "sidecar_compacted_at"),
    }
)

#: Subject types the denylist (and the closure row-delete) treats as erased closure items.
#: FLAGGED_UUID and CONTEXT_HOLDER_UUID are live shared items: they are column-purged but never
#: denylisted and never row-deleted.
CLOSURE_SUBJECT_TYPES: tuple[str, ...] = ("NODE_UUID", "EPISODE_UUID", "DERIVED_UUID")

#: Columns a CONTEXT_HOLDER_UUID purge may blank (plan 3.5): prompt/provider text that can embed
#: closure text. Keyed by the holder's episode_uuid; no other column of that row is touched.
CONTEXT_HOLDER_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("failure_events", "error"),
        ("llm_usage_events", "provider_usage_json"),
        ("llm_usage_events", "error"),
    }
)

_NS_REASON = (
    "keyed by namespace, not a per-row subject uuid: purged by the namespace-erase path. A "
    "namespace denylist would black-hole a namespace that is legitimately re-created."
)
_SESSION_REASON = (
    "keyed by session_id only: a session is not an erased subject, so no row-level denylist "
    "applies; the purge and the unaddressable census cover it."
)
_JOURNAL_REASON = (
    "journal table: the saga's own row indexes its target as a subject, so a trigger would drop "
    "the erase's own updates. Purged explicitly after COMMITTED by the INDEXED_SUBJECT purge."
)

#: Content columns deliberately WITHOUT a denylist trigger, each with its reason. The denylist
#: coverage test requires every CONTENT_COLUMNS entry to be either triggered or listed here.
DENYLIST_EXCLUDED: dict[tuple[str, str], str] = {
    ("recall_lab_runs", "query"): _NS_REASON,
    ("recall_lab_runs", "arms_json"): _NS_REASON,
    ("recall_lab_runs", "request_json"): _NS_REASON,
    ("recall_lab_runs", "result_json"): _NS_REASON,
    ("recall_query_log", "query"): _NS_REASON,
    ("extraction_lab_runs", "current_message"): _NS_REASON,
    ("extraction_lab_runs", "arms_json"): _NS_REASON,
    ("extraction_lab_runs", "request_json"): _NS_REASON,
    ("extraction_lab_runs", "result_json"): _NS_REASON,
    ("recall_receipts", "reason"): _SESSION_REASON,
    ("graph_operations", "before_snapshot_json"): _JOURNAL_REASON,
    ("graph_operations", "last_error"): _JOURNAL_REASON,
}


def classified_columns() -> frozenset[tuple[str, str]]:
    """Return the (table, column) pairs covered by ``CONTENT_COLUMNS``."""
    return frozenset((c.table, c.column) for c in CONTENT_COLUMNS)