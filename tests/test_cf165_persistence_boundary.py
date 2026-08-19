"""Persistence-boundary regressions for CF-165 forward erasure completeness.

These deliberately call the lower-level telemetry store methods rather than the safer wrappers.
The sidecar must remain safe even when a future caller bypasses wrapper-level minimization.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure.telemetry.erasure_purge import count_unaddressable_content
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = [pytest.mark.unit]


def _store(tmp_path) -> tuple[McpTelemetryStore, object]:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store._ensure_ready()
    return store, db


def test_direct_recall_feedback_cannot_recreate_free_text_reason(tmp_path) -> None:
    store, db = _store(tmp_path)
    store.record_recall_receipt(
        token="r-1",
        operation="recall_memories",
        client_id="c",
        session_id="s",
    )
    rated = store.record_recall_feedback(
        token="r-1",
        score_label="useful",
        score_value=1.0,
        reason="private feedback that must not survive",
    )
    assert rated is not None

    with sqlite3.connect(db) as conn:
        reason = conn.execute(
            "SELECT reason FROM recall_receipts WHERE token='r-1'"
        ).fetchone()[0]
        stranded = count_unaddressable_content(conn)
    assert reason is None
    assert "recall_receipts.reason" not in stranded


def test_direct_mcp_store_without_lineage_is_minimized_before_commit(tmp_path) -> None:
    store, db = _store(tmp_path)
    store.record(
        kind="tool",
        operation="future_direct_caller",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=1,
        success=False,
        error="private exception prose",
        input_size=1,
        result_size=0,
        payload_preview='{"text":"private memory"}',
        namespace=None,
        node_uuid=None,
    )

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT namespace, error, payload_preview FROM mcp_events"
        ).fetchone()
        stranded = count_unaddressable_content(conn)
    assert row == ("default", "[redacted]", "[redacted]")
    assert not any(key.startswith("mcp_events.") for key in stranded)


def test_direct_diagnostic_stores_minimize_missing_episode_lineage(tmp_path) -> None:
    store, db = _store(tmp_path)
    assert store.record_failure(
        recorded_at="2026-01-01T00:00:00Z",
        operation="future_direct_failure",
        episode_uuid=None,
        failure_stage="x",
        classification="x",
        retryable=False,
        processing_attempt=None,
        queue_depth=None,
        worker_id=None,
        error_type="RuntimeError",
        error="private failure prose",
        details_json=json.dumps({"prompt": "private prompt"}),
    ) is True
    store.record_lifecycle_event(
        recorded_at="2026-01-01T00:00:00Z",
        component="future",
        event="event",
        state="failed",
        episode_uuid=None,
        details_json=json.dumps({"memory": "private memory"}),
    )
    assert store.record_llm_usage_event(
        call_id="call-direct",
        recorded_at="2026-01-01T00:00:00Z",
        run_id=None,
        episode_uuid=None,
        operation="future",
        kind="chat",
        model="m",
        endpoint="e",
        status="failed",
        duration_ms=1,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cached_input_tokens=None,
        reasoning_output_tokens=None,
        provider_usage_json=json.dumps({"prompt": "private prompt"}),
        error="private provider error",
    ) is True

    with sqlite3.connect(db) as conn:
        failure = conn.execute(
            "SELECT episode_uuid, error, details_json FROM failure_events"
        ).fetchone()
        lifecycle = conn.execute(
            "SELECT episode_uuid, details_json FROM lifecycle_events"
        ).fetchone()
        usage = conn.execute(
            "SELECT episode_uuid, provider_usage_json, error FROM llm_usage_events"
        ).fetchone()
        stranded = count_unaddressable_content(conn)

    assert failure == ("__non_episode__", "[redacted]", "{}")
    assert lifecycle == ("__non_episode__", "{}")
    assert usage == ("__non_episode__", "{}", "[redacted]")
    assert not any(
        key.startswith(("failure_events.", "lifecycle_events.", "llm_usage_events."))
        for key in stranded
    )


def test_blank_episode_task_key_cannot_retain_details(tmp_path) -> None:
    store, db = _store(tmp_path)
    store.record_episode_task_event(
        recorded_at="2026-01-01T00:00:00Z",
        episode_uuid="",
        parent_task=None,
        child_task=None,
        phase="failed",
        kind="chat",
        model="m",
        endpoint="e",
        scheduler_task=None,
        details_json=json.dumps({"prompt": "private prompt"}),
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT episode_uuid, details_json FROM episode_task_events"
        ).fetchone()
        stranded = count_unaddressable_content(conn)
    assert row == ("__non_episode__", "{}")
    assert "episode_task_events.details_json" not in stranded


def test_direct_merge_store_derives_namespace_or_drops_unowned_snapshot(tmp_path) -> None:
    store, db = _store(tmp_path)
    store.record_merge(
        survivor_uuid="s-1",
        absorbed_uuid="a-1",
        similarity=0.9,
        snapshot_json=json.dumps({"properties": {"namespace": "tenant-a", "content": "private"}}),
    )
    store.record_merge(
        survivor_uuid="s-2",
        absorbed_uuid="a-2",
        similarity=0.8,
        snapshot_json=json.dumps({"properties": {"content": "unowned private"}}),
    )

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT survivor_uuid, survivor_namespace, absorbed_namespace "
            "FROM merge_audit ORDER BY survivor_uuid"
        ).fetchall()
    assert rows == [("s-1", "tenant-a", "tenant-a")]


def test_raw_extraction_insert_without_namespace_is_minimized(tmp_path) -> None:
    _, db = _store(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO extraction_lab_runs
                (recorded_at, current_message, arms_json, request_json, result_json, namespace)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                "2026-01-01T00:00:00Z",
                "private current message",
                json.dumps([{"error": "private"}]),
                json.dumps({"current_message": "private"}),
                json.dumps({"current_message": "private"}),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT namespace, current_message, arms_json, request_json, result_json "
            "FROM extraction_lab_runs"
        ).fetchone()
        stranded = count_unaddressable_content(conn)

    assert row == ("__extraction_lab_unscoped__", "[redacted]", "[]", "{}", "{}")
    assert not any(key.startswith("extraction_lab_runs.") for key in stranded)
