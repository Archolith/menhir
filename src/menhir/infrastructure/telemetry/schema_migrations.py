"""Additive schema migrations for the SQLite telemetry sidecar.

``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, so a sidecar
created before a column was introduced keeps its old column set forever. Columns added
after a table shipped therefore need an explicit ``PRAGMA table_info`` / ``ALTER TABLE``
pass, following the idiom in ``infrastructure/graph_operations.py``.

These live outside ``store.py`` because that module is budgeted as a thin owner of
connection + schema only (enforced by ``tests/test_large_module_boundaries.py``), and
migrations accumulate over time.

Every function here must be idempotent: ``_ensure_ready()`` runs on first use in every
process, so a migration re-runs constantly and must be a no-op once applied.
"""

from __future__ import annotations

import sqlite3


_NON_EPISODE_SUBJECT_KEY = "__non_episode__"
_EXTRACTION_LAB_UNSCOPED_KEY = "__extraction_lab_unscoped__"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_forward_lineage_guards(conn: sqlite3.Connection) -> None:
    """Make the sidecar itself reject/minimize new content that lacks an erasure key.

    CF-165 originally fixed the normal wrapper writers. That is not a sufficient closure
    invariant: ``McpTelemetryStore`` also exposes lower-level insert methods, and a future caller
    can bypass a wrapper without realizing it is also bypassing its lineage/minimization logic.
    These triggers put the invariant at the persistence boundary, in the same transaction as the
    write. Historical rows are deliberately untouched here (apart from the separately documented
    recall-feedback scrub); the guards apply to NEW forward traffic.

    Rules:
    * feedback prose has no sound tenant owner, so it is never persisted;
    * genuinely non-episode diagnostics are retained only after their free-text payload is
      minimized and they receive an explicit operational sentinel rather than a NULL key;
    * a raw MCP event with no namespace is minimized before commit and assigned the reserved
      default scope;
    * Extraction Lab direct inserts with no owner are minimized and placed in the explicit
      synthetic-lab scope;
    * merge snapshots and node-specific lifecycle/revision content fail closed when their
      required ownership key cannot be derived. Silently assigning those records a fake owner
      would make later namespace erasure report success while leaving private recovery content.
    """

    if "reason" in _table_columns(conn, "recall_receipts"):
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_recall_reason_insert
            AFTER INSERT ON recall_receipts
            WHEN NEW.reason IS NOT NULL
            BEGIN
                UPDATE recall_receipts SET reason = NULL WHERE id = NEW.id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_recall_reason_update
            AFTER UPDATE OF reason ON recall_receipts
            WHEN NEW.reason IS NOT NULL
            BEGIN
                UPDATE recall_receipts SET reason = NULL WHERE id = NEW.id;
            END
            """
        )

    mcp_columns = _table_columns(conn, "mcp_events")
    if {"namespace", "payload_preview", "error"}.issubset(mcp_columns):
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_mcp_missing_lineage
            AFTER INSERT ON mcp_events
            WHEN NEW.namespace IS NULL OR trim(NEW.namespace) = ''
            BEGIN
                UPDATE mcp_events
                SET namespace = 'default',
                    payload_preview = CASE
                        WHEN NEW.payload_preview IS NULL OR NEW.payload_preview = ''
                            THEN NEW.payload_preview
                        ELSE '[redacted]'
                    END,
                    error = CASE
                        WHEN NEW.error IS NULL OR NEW.error = '' THEN NEW.error
                        ELSE '[redacted]'
                    END
                WHERE id = NEW.id;
            END
            """
        )

    failure_columns = _table_columns(conn, "failure_events")
    if {"episode_uuid", "error", "details_json"}.issubset(failure_columns):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_cf165_failure_missing_lineage
            AFTER INSERT ON failure_events
            WHEN NEW.episode_uuid IS NULL OR trim(NEW.episode_uuid) = ''
            BEGIN
                UPDATE failure_events
                SET episode_uuid = '{_NON_EPISODE_SUBJECT_KEY}',
                    error = '[redacted]',
                    details_json = CASE
                        WHEN NEW.details_json IS NULL OR NEW.details_json = '' THEN NEW.details_json
                        ELSE '{{}}'
                    END
                WHERE id = NEW.id;
            END
            """
        )

    lifecycle_columns = _table_columns(conn, "lifecycle_events")
    if {"episode_uuid", "details_json"}.issubset(lifecycle_columns):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_cf165_lifecycle_missing_lineage
            AFTER INSERT ON lifecycle_events
            WHEN NEW.episode_uuid IS NULL OR trim(NEW.episode_uuid) = ''
            BEGIN
                UPDATE lifecycle_events
                SET episode_uuid = '{_NON_EPISODE_SUBJECT_KEY}',
                    details_json = CASE
                        WHEN NEW.details_json IS NULL OR NEW.details_json = '' THEN NEW.details_json
                        WHEN json_valid(NEW.details_json)
                         AND NOT EXISTS (
                                SELECT 1
                                FROM json_tree(NEW.details_json)
                                WHERE type = 'text' AND atom IS NOT NULL AND atom != ''
                            )
                            THEN NEW.details_json
                        ELSE '{{}}'
                    END
                WHERE id = NEW.id;
            END
            """
        )

    llm_columns = _table_columns(conn, "llm_usage_events")
    if {"episode_uuid", "provider_usage_json", "error"}.issubset(llm_columns):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_cf165_llm_missing_lineage
            AFTER INSERT ON llm_usage_events
            WHEN NEW.episode_uuid IS NULL OR trim(NEW.episode_uuid) = ''
            BEGIN
                UPDATE llm_usage_events
                SET episode_uuid = '{_NON_EPISODE_SUBJECT_KEY}',
                    provider_usage_json = CASE
                        WHEN NEW.provider_usage_json IS NULL OR NEW.provider_usage_json = ''
                            THEN NEW.provider_usage_json
                        ELSE '{{}}'
                    END,
                    error = CASE
                        WHEN NEW.error IS NULL OR NEW.error = '' THEN NEW.error
                        ELSE '[redacted]'
                    END
                WHERE call_id = NEW.call_id;
            END
            """
        )

    task_columns = _table_columns(conn, "episode_task_events")
    if {"episode_uuid", "details_json"}.issubset(task_columns):
        # Episode-task telemetry is semantically episode-specific. A blank UUID is therefore a
        # malformed writer, not a legitimate global event. Keep the operational row but strip the
        # only content-bearing field before assigning the non-episode sentinel.
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_cf165_episode_task_blank_lineage
            AFTER INSERT ON episode_task_events
            WHEN trim(NEW.episode_uuid) = ''
            BEGIN
                UPDATE episode_task_events
                SET episode_uuid = '{_NON_EPISODE_SUBJECT_KEY}',
                    details_json = CASE
                        WHEN NEW.details_json IS NULL OR NEW.details_json = '' THEN NEW.details_json
                        ELSE '{{}}'
                    END
                WHERE id = NEW.id;
            END
            """
        )

    extraction_columns = _table_columns(conn, "extraction_lab_runs")
    extraction_payload = {"namespace", "current_message", "arms_json", "request_json", "result_json"}
    if extraction_payload.issubset(extraction_columns):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_cf165_extraction_missing_lineage
            AFTER INSERT ON extraction_lab_runs
            WHEN NEW.namespace IS NULL OR trim(NEW.namespace) = ''
            BEGIN
                UPDATE extraction_lab_runs
                SET namespace = '{_EXTRACTION_LAB_UNSCOPED_KEY}',
                    current_message = CASE
                        WHEN NEW.current_message IS NULL OR NEW.current_message = ''
                            THEN NEW.current_message
                        ELSE '[redacted]'
                    END,
                    arms_json = CASE
                        WHEN NEW.arms_json IS NULL OR NEW.arms_json = '' THEN NEW.arms_json
                        ELSE '[]'
                    END,
                    request_json = CASE
                        WHEN NEW.request_json IS NULL OR NEW.request_json = '' THEN NEW.request_json
                        ELSE '{{}}'
                    END,
                    result_json = CASE
                        WHEN NEW.result_json IS NULL OR NEW.result_json = '' THEN NEW.result_json
                        ELSE '{{}}'
                    END
                WHERE id = NEW.id;
            END
            """
        )

    merge_columns = _table_columns(conn, "merge_audit")
    merge_needed = {
        "snapshot_json",
        "survivor_namespace",
        "absorbed_namespace",
    }
    if merge_needed.issubset(merge_columns):
        # Current merge snapshots carry absorbed properties, including namespace. Derive missing
        # lineage at the DB boundary so a direct store call cannot bypass the wrapper's inference.
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_merge_infer_lineage
            AFTER INSERT ON merge_audit
            WHEN (NEW.survivor_namespace IS NULL OR trim(NEW.survivor_namespace) = '')
              OR (NEW.absorbed_namespace IS NULL OR trim(NEW.absorbed_namespace) = '')
            BEGIN
                UPDATE merge_audit
                SET survivor_namespace = coalesce(
                        nullif(trim(NEW.survivor_namespace), ''),
                        nullif(trim(NEW.absorbed_namespace), ''),
                        CASE WHEN json_valid(NEW.snapshot_json)
                             THEN nullif(trim(json_extract(NEW.snapshot_json, '$.properties.namespace')), '')
                        END
                    ),
                    absorbed_namespace = coalesce(
                        nullif(trim(NEW.absorbed_namespace), ''),
                        nullif(trim(NEW.survivor_namespace), ''),
                        CASE WHEN json_valid(NEW.snapshot_json)
                             THEN nullif(trim(json_extract(NEW.snapshot_json, '$.properties.namespace')), '')
                        END
                    )
                WHERE id = NEW.id;
            END
            """
        )
        # AFTER inference, a genuinely namespace-less snapshot is not safe to keep: its UUID keys
        # may disappear from the graph before a later namespace erasure, making the recovery text
        # invisible to that purge. Remove the inserted row rather than retain unowned content.
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_merge_drop_unowned
            AFTER INSERT ON merge_audit
            WHEN (NEW.survivor_namespace IS NULL OR trim(NEW.survivor_namespace) = '')
             AND (NEW.absorbed_namespace IS NULL OR trim(NEW.absorbed_namespace) = '')
             AND (
                    json_valid(NEW.snapshot_json) = 0
                 OR json_extract(NEW.snapshot_json, '$.properties.namespace') IS NULL
                 OR trim(json_extract(NEW.snapshot_json, '$.properties.namespace')) = ''
                 )
            BEGIN
                DELETE FROM merge_audit WHERE id = NEW.id;
            END
            """
        )

    action_columns = _table_columns(conn, "lifecycle_actions")
    if {"node_uuid", "notes"}.issubset(action_columns):
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_lifecycle_action_blank_key
            AFTER INSERT ON lifecycle_actions
            WHEN trim(NEW.node_uuid) = '' AND NEW.notes IS NOT NULL AND NEW.notes != ''
            BEGIN
                UPDATE lifecycle_actions SET notes = NULL WHERE id = NEW.id;
            END
            """
        )

    revision_columns = _table_columns(conn, "memory_revisions")
    if {"node_uuid", "old_value", "new_value"}.issubset(revision_columns):
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_cf165_revision_blank_key
            AFTER INSERT ON memory_revisions
            WHEN trim(NEW.node_uuid) = ''
            BEGIN
                UPDATE memory_revisions
                SET old_value = NULL, new_value = NULL
                WHERE id = NEW.id;
            END
            """
        )


def ensure_lineage_columns(conn: sqlite3.Connection) -> None:
    """Ensure current sidecar content has a sound erasure key where one is derivable (CF-165).

    ``recall_receipts.reason`` is scrubbed instead of assigned fake ownership: a usefulness
    receipt can cover a global/workspace read, so there is no sound session->namespace mapping.

    Recall Lab is different. Its historical ``namespace=NULL`` means exactly "unscoped/default
    recall"; that maps to Menhir's reserved ``default`` namespace. Backfill is therefore provable,
    and a trigger keeps future direct-store callers from recreating NULL-keyed raw query/results.
    """
    additions: dict[str, tuple[str, ...]] = {
        "merge_audit": ("survivor_namespace", "absorbed_namespace"),
        "mcp_events": ("namespace", "node_uuid"),
        "extraction_lab_runs": ("namespace",),
    }
    for table, columns in additions.items():
        existing = _table_columns(conn, table)
        if not existing:
            continue
        for column in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    recall_columns = _table_columns(conn, "recall_receipts")
    if "reason" in recall_columns:
        conn.execute("UPDATE recall_receipts SET reason = NULL WHERE reason IS NOT NULL")

    recall_lab_columns = _table_columns(conn, "recall_lab_runs")
    if "namespace" in recall_lab_columns:
        # In RecallLabRequest, None is the normal unscoped/default graph read -- unlike
        # Extraction Lab's synthetic fixtures, this ownership is deterministic.
        conn.execute(
            "UPDATE recall_lab_runs SET namespace = 'default' "
            "WHERE namespace IS NULL OR namespace = ''"
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_recall_lab_namespace_lineage
            AFTER INSERT ON recall_lab_runs
            WHEN NEW.namespace IS NULL OR NEW.namespace = ''
            BEGIN
                UPDATE recall_lab_runs SET namespace = 'default' WHERE id = NEW.id;
            END
            """
        )

    _ensure_forward_lineage_guards(conn)


__all__ = ["ensure_lineage_columns"]
