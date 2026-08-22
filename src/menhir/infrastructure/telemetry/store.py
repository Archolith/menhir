from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from menhir.infrastructure.paths import telemetry_db_path
from menhir.infrastructure.telemetry.schema_migrations import ensure_lineage_columns

logger = logging.getLogger(__name__)


from menhir.infrastructure.telemetry.event_store import TelemetryEventStoreMixin
from menhir.infrastructure.telemetry.helpers import (
    _json_default,
    _preview_of,
    _size_of,
    _span_days,
    _utc_now_iso,
    connect_telemetry_db,
    default_telemetry_db_path,
)
from menhir.infrastructure.telemetry.lifecycle_store import TelemetryLifecycleStoreMixin
from menhir.infrastructure.telemetry.llm_usage_store import (
    TelemetryLLMUsageStoreMixin,
    initialize_llm_usage_schema,
)
from menhir.infrastructure.telemetry.recall_store import TelemetryRecallStoreMixin


@dataclass
class McpTelemetryStore(
    TelemetryEventStoreMixin,
    TelemetryLifecycleStoreMixin,
    TelemetryRecallStoreMixin,
    TelemetryLLMUsageStoreMixin,
):
    """Append-only SQLite store for MCP latency and outcome tracking."""

    db_path: Path = field(default_factory=default_telemetry_db_path)
    _initialized: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def _connect(self) -> sqlite3.Connection:
        """Open the telemetry DB with a BOUNDED busy-wait so a locked/contended DB fails FAST.

        A contended DB raises ``sqlite3.OperationalError: database is locked`` within the configured
        window instead of stalling the caller — and, because the MCP tool path calls this
        synchronously, the event loop with it. The MCP tracker turns that error into an actionable
        "store busy/locked" message.

        The window is NOT what this seam buys: a bare ``sqlite3.connect`` already blocks about 5 s,
        not indefinitely, because Python installs a busy handler from its own ``timeout=5.0``
        default. What it buys is that ``MENHIR_TELEMETRY_BUSY_TIMEOUT_S`` reaches the connection at
        all. That is why the implementation now lives in
        :func:`~menhir.infrastructure.telemetry.helpers.connect_telemetry_db` and every store
        sharing this database file calls it — an operator raising the timeout previously moved the
        ceiling for this store alone while six siblings kept failing at 5 s against the same
        file."""
        return connect_telemetry_db(self.db_path)

    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                # WAL lets readers proceed while a writer holds the DB, which is the durable
                # fix for "database is locked" contention on mcp_telemetry.db. It is applied
                # on every connect by the shared seam connect_telemetry_db -- the file is
                # created in WAL mode no matter which of the five writers touches it first,
                # so application here is NOT the guarantee of init-time setup it once looked
                # like. This explicit pragma is harmless duplication kept as a belt-and-braces
                # guard against a future store path that bypasses the seam.
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:  # pragma: no cover - never let a pragma break init
                    logger.debug("Could not enable WAL on telemetry DB", exc_info=True)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mcp_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        operation TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        success INTEGER NOT NULL,
                        error TEXT,
                        input_size INTEGER,
                        result_size INTEGER,
                        payload_preview TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mcp_events_operation_started
                    ON mcp_events (operation, started_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS failure_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        episode_uuid TEXT,
                        failure_stage TEXT,
                        classification TEXT,
                        retryable INTEGER,
                        processing_attempt INTEGER,
                        queue_depth INTEGER,
                        worker_id TEXT,
                        error_type TEXT,
                        error TEXT NOT NULL,
                        details_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_failure_events_operation_recorded
                    ON failure_events (operation, recorded_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_failure_events_episode_recorded
                    ON failure_events (episode_uuid, recorded_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episode_task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        episode_uuid TEXT NOT NULL,
                        parent_task TEXT,
                        child_task TEXT,
                        phase TEXT NOT NULL,
                        kind TEXT,
                        model TEXT,
                        endpoint TEXT,
                        scheduler_task TEXT,
                        details_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_episode_task_events_episode_recorded
                    ON episode_task_events (episode_uuid, recorded_at)
                    """
                )
                initialize_llm_usage_schema(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        event TEXT NOT NULL,
                        status TEXT NOT NULL,
                        episode_uuid TEXT,
                        details_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_events_phase_recorded
                    ON lifecycle_events (phase, recorded_at)
                    """
                )
                lifecycle_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(lifecycle_events)"
                    ).fetchall()
                }
                if "episode_uuid" not in lifecycle_columns:
                    conn.execute(
                        "ALTER TABLE lifecycle_events ADD COLUMN episode_uuid TEXT"
                    )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_events_episode_recorded
                    ON lifecycle_events (episode_uuid, recorded_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        node_uuid TEXT NOT NULL,
                        session_id TEXT,
                        trigger TEXT NOT NULL,
                        before_freshness TEXT,
                        after_freshness TEXT,
                        llm_used INTEGER,
                        duration_ms INTEGER,
                        notes TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_actions_node
                    ON lifecycle_actions(node_uuid, recorded_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_actions_action
                    ON lifecycle_actions(action, recorded_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        node_uuid TEXT NOT NULL,
                        field TEXT NOT NULL,
                        old_value TEXT,
                        new_value TEXT,
                        changed_by TEXT NOT NULL,
                        episode_uuid TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_revisions_node
                    ON memory_revisions(node_uuid, recorded_at DESC)
                    """
                )
                # Durable merge audit. The graph also denormalizes this onto
                # survivor.merge_audit, but that property dies with the node: decay
                # (bridge_and_delete), orphan cleanup, and user deletes all DETACH DELETE
                # the survivor, destroying the recovery record for every node it ever
                # absorbed. This sidecar outlives the graph node, so merge history --
                # and the absorbed-node snapshot needed to unmerge -- survives.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS merge_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        survivor_uuid TEXT NOT NULL,
                        absorbed_uuid TEXT NOT NULL,
                        similarity REAL,
                        snapshot_json TEXT NOT NULL,
                        survivor_namespace TEXT,
                        absorbed_namespace TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merge_audit_survivor
                    ON merge_audit(survivor_uuid, recorded_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merge_audit_absorbed
                    ON merge_audit(absorbed_uuid)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conflict_resolutions (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        resolved_at TEXT NOT NULL,
                        uuid_a      TEXT NOT NULL,
                        uuid_b      TEXT NOT NULL,
                        status      TEXT NOT NULL,
                        group_id    TEXT NOT NULL,
                        action      TEXT NOT NULL,
                        reviewed_by TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_resolutions_pair
                    ON conflict_resolutions (uuid_a, uuid_b)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conflict_resolutions_resolved
                    ON conflict_resolutions (resolved_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS client_registry (
                        client_id   TEXT PRIMARY KEY,
                        client_name TEXT NOT NULL,
                        first_accessed TEXT NOT NULL,
                        last_accessed  TEXT NOT NULL
                    )
                    """
                )
                # Per-session (per-window/per-conversation) last_accessed tracking.
                # Each conversation gets its own row so Window A's activity does not
                # bleed into Window B's temporal context.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_registry (
                        session_id     TEXT PRIMARY KEY,
                        client_id      TEXT NOT NULL DEFAULT '',
                        client_name    TEXT NOT NULL DEFAULT '',
                        first_accessed TEXT NOT NULL,
                        last_accessed  TEXT NOT NULL
                    )
                    """
                )
                # Self-reported recall usefulness (R8: agent_inference grade — an
                # operational quality signal only; it never feeds memory heat/promotion).
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_receipts (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        token       TEXT NOT NULL UNIQUE,
                        operation   TEXT NOT NULL,
                        client_id   TEXT NOT NULL DEFAULT '',
                        session_id  TEXT NOT NULL DEFAULT '',
                        created_at  TEXT NOT NULL,
                        score_label TEXT,
                        score_value REAL,
                        reason      TEXT,
                        rated_at    TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recall_receipts_session_created
                    ON recall_receipts (session_id, rated_at, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recall_receipts_operation
                    ON recall_receipts (operation, created_at)
                    """
                )
                # Durable Recall Lab experiments. CF-179: `query`, `request_json` and
                # `result_json` hold RAW user text at rest -- redaction is display-time and
                # `privacy_redact` defaults False. Intended (a redacted lab run records nothing),
                # so erasability is the control: both lab tables are namespace-keyed in
                # `erasure_inventory` and reached by `erasure_purge`. Proven by executing the
                # purge in `tests/test_cf179_lab_table_erasure.py`.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_lab_runs (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at   TEXT NOT NULL,
                        query         TEXT NOT NULL,
                        preset        TEXT NOT NULL,
                        namespace     TEXT,
                        judge_enabled INTEGER NOT NULL,
                        judge_ok      INTEGER,
                        judge_model   TEXT,
                        winner_id     TEXT,
                        tied_ids_json TEXT NOT NULL,
                        arms_json     TEXT NOT NULL,
                        request_json  TEXT NOT NULL,
                        result_json   TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recall_lab_runs_recorded
                    ON recall_lab_runs (recorded_at)
                    """
                )
                # Durable Extraction Lab experiments (Recall Labs Phase 0 extension --
                # see .agent/plans/menhir-belief-supersession-code-mapped-plan.md). Mirrors
                # recall_lab_runs' shape; arms_json carries gold-score summaries instead of
                # retrieval-hit summaries.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS extraction_lab_runs (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at    TEXT NOT NULL,
                        current_message TEXT NOT NULL,
                        arms_json      TEXT NOT NULL,
                        request_json   TEXT NOT NULL,
                        result_json    TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_extraction_lab_runs_recorded
                    ON extraction_lab_runs (recorded_at)
                    """
                )
                # CF-165 durable namespace lineage. Lives in schema_migrations because
                # this module is budgeted as a thin connection+schema owner.
                ensure_lineage_columns(conn)
                conn.commit()
            self._initialized = True



telemetry_store = McpTelemetryStore()
