import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

import menhir.infrastructure.telemetry.recorders as recorders
from tests._temp_cleanup import remove_test_dir
from menhir.infrastructure.observability import LLMUsageEvent
from menhir.mcp.telemetry import (
    McpTelemetryStore,
    record_failure_event,
    record_lifecycle_event,
    record_llm_usage_event,
    record_mcp_event,
    track_mcp_call,
)

_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"


def _make_db_path() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = _TEST_TMP_ROOT / f"mcp-telemetry-{uuid4()}"
    run_dir.mkdir()
    return run_dir / "mcp_telemetry.db"


def _cleanup_db_path(db_path: Path) -> None:
    remove_test_dir(db_path.parent)


def test_safe_structural_uri_redacts_userinfo_but_preserves_normal_identifiers():
    from menhir.infrastructure.telemetry.helpers import _preview_of

    preview = _preview_of(
        {
            "source": "https://dummy-user:dummy-secret@repo.example/org/repo",
            "repository": "https://repo.example/org/repo",
            "project": "C:/work/repo",
        }
    )

    assert "dummy-user" not in preview
    assert "dummy-secret" not in preview
    assert '"source": "https://repo.example/org/repo"' in preview
    assert '"repository": "https://repo.example/org/repo"' in preview
    assert '"project": "C:/work/repo"' in preview


def _fetch_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT operation, kind, success, error, duration_ms, input_size, result_size, payload_preview
            FROM mcp_events
            ORDER BY id ASC
            """
        ).fetchall()


def _fetch_failure_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT operation, episode_uuid, failure_stage, classification, retryable,
                   processing_attempt, queue_depth, worker_id, error_type, error, details_json
            FROM failure_events
            ORDER BY id ASC
            """
        ).fetchall()


def _fetch_lifecycle_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT phase AS component, event, status AS state, episode_uuid, details_json
            FROM lifecycle_events
            ORDER BY id ASC
            """
        ).fetchall()


def _synthetic_timestamp(index: int) -> str:
    return f"2026-11-25T00:{index // 60:02d}:{index % 60:02d}"


def _seed_mcp_events(db_path: Path, count: int) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO mcp_events (
                started_at, completed_at, duration_ms, operation, kind, success
            ) VALUES (?, ?, 0, ?, 'tool', 1)
            """,
            (
                (_synthetic_timestamp(index), _synthetic_timestamp(index), f"operation-{index}")
                for index in range(count)
            ),
        )


def _recent_query_plan(db_path: Path) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [
            row[3]
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT id, started_at, completed_at, duration_ms, operation, kind,
                       success, error, input_size, result_size, payload_preview
                FROM mcp_events
                ORDER BY started_at DESC
                LIMIT 50
                """
            ).fetchall()
        ]


def _seed_failure_events(db_path: Path, count: int) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO failure_events (recorded_at, operation, episode_uuid, error)
            VALUES (?, 'episode_enrichment', ?, ?)
            """,
            (
                (
                    _synthetic_timestamp(index),
                    "episode-even" if index % 2 == 0 else "episode-odd",
                    f"failure-{index}",
                )
                for index in range(count)
            ),
        )


def test_bump_failure_count_is_atomic_under_concurrency(monkeypatch):
    class YieldingCounts(dict[str, int]):
        def get(self, key: str, default: int = 0) -> int:
            count = super().get(key, default)
            time.sleep(0)  # Give another thread a chance to read the same count.
            return count

    counts = YieldingCounts()
    monkeypatch.setattr(recorders, "_TELEMETRY_FAILURE_COUNTS", counts)
    workers = 8
    increments_per_worker = 32
    start = threading.Barrier(workers, timeout=5)

    def bump() -> list[int]:
        start.wait()
        return [recorders._bump_failure_count("op") for _ in range(increments_per_worker)]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        jobs = [executor.submit(bump) for _ in range(workers)]
        observed = [count for job in jobs for count in job.result(timeout=10)]

    assert sorted(observed) == list(range(1, workers * increments_per_worker + 1))


def test_bump_failure_count_evicts_oldest_at_capacity(monkeypatch):
    counts = {"oldest": 1, "newest": 1}
    monkeypatch.setattr(recorders, "_TELEMETRY_FAILURE_COUNTS", counts)
    monkeypatch.setattr(recorders, "_TELEMETRY_FAILURE_COUNTS_MAX", 2)

    assert recorders._bump_failure_count("oldest") == 2
    assert recorders._bump_failure_count("added") == 1
    assert counts == {"newest": 1, "added": 1}


def test_track_mcp_call_records_success():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        return {"ok": True, "items": [1, 2, 3]}

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="tool",
                operation="add_memory",
                payload={"text": "hello"},
                runner=_runner,
                store=store,
            )
        )
        assert result == {"ok": True, "items": [1, 2, 3]}
        rows = _fetch_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["operation"] == "add_memory"
        assert row["kind"] == "tool"
        assert row["success"] == 1
        assert row["error"] is None
        assert row["duration_ms"] >= 0
        assert row["input_size"] is not None
        assert row["result_size"] is not None
        # CF-167: the KEY is structural and survives; the VALUE is user memory text and
        # must not reach the plaintext sidecar. This assertion previously required the
        # opposite, pinning the leak as correct behaviour.
        assert '"text": "[redacted]"' in row["payload_preview"]
        assert "hello" not in row["payload_preview"]
    finally:
        _cleanup_db_path(db_path)


def test_track_mcp_call_records_failure_and_returns_error_string():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        raise RuntimeError("boom")

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="resource_template",
                operation="memory://search/{term}",
                payload={"term": "miso"},
                runner=_runner,
                store=store,
            )
        )
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "boom" in result
        rows = _fetch_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["operation"] == "memory://search/{term}"
        assert row["kind"] == "resource_template"
        assert row["success"] == 0
        assert row["error"] == "RuntimeError"
        assert "boom" not in row["error"]
        assert row["duration_ms"] >= 0
    finally:
        _cleanup_db_path(db_path)


def test_connect_uses_bounded_busy_timeout():
    # a bounded busy_timeout is what lets a locked DB RAISE fast instead of freezing the caller
    # (and, on the async MCP path, the event loop) — the precondition for returning a diagnostic.
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store._ensure_ready()
        # sqlite3.Connection.__exit__ commits or rolls back; it does not close.
        with closing(store._connect()) as conn:
            (busy_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert busy_ms == 5000        # bounded, not 0 (the indefinite/no-wait default)
    finally:
        _cleanup_db_path(db_path)


def test_track_mcp_call_reports_locked_store_with_diagnostic():
    # a busted store (locked SQLite) must come back as a MESSAGE explaining what is wrong, not an
    # opaque failure — while still preserving the original error text for debugging.
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        raise sqlite3.OperationalError("database is locked")

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="tool",
                operation="recall_memories",
                payload={"query": "x"},
                runner=_runner,
                store=store,
            )
        )
        assert isinstance(result, str) and result.startswith("Error:")
        low = result.lower()
        assert ("busy" in low or "locked" in low) and "degraded" in low   # says what's wrong
        assert "database is locked" in result                             # original preserved
    finally:
        _cleanup_db_path(db_path)


def test_track_mcp_call_timeout_returns_degraded_message():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        await asyncio.sleep(0.3)
        return "late"

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="tool",
                operation="get_memory_stats",
                payload={},
                runner=_runner,
                store=store,
                timeout=0,
            )
        )
        assert isinstance(result, str) and result.startswith("Error: TIMEOUT")
        assert "degraded" in result.lower()
    finally:
        _cleanup_db_path(db_path)


def test_track_mcp_call_generic_error_message_is_unchanged():
    # non-infra errors must keep the plain "Type: message" form that callers/mappers depend on.
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        raise RuntimeError("boom")

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="tool", operation="add_memory", payload={}, runner=_runner, store=store,
            )
        )
        assert result == "Error: RuntimeError: boom"     # exact plain form preserved
    finally:
        _cleanup_db_path(db_path)


def test_record_mcp_event_records_background_metrics():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={"episode_uuid": "pending-1", "queue_depth": 2},
            result={"resolved_episode_uuid": "episode-1"},
            duration_ms=321,
            success=True,
            store=store,
        )

        rows = _fetch_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["operation"] == "episode_enrichment"
        assert row["kind"] == "background"
        assert row["success"] == 1
        assert row["duration_ms"] == 321
        assert '"queue_depth": 2' in row["payload_preview"]
    finally:
        _cleanup_db_path(db_path)


@pytest.mark.parametrize("field", ["payload", "result"])
def test_record_mcp_event_drops_unserializable_data(field, caplog):
    store = Mock(spec=McpTelemetryStore)
    value = {"nested": []}
    value["nested"].append(value)

    with caplog.at_level(logging.WARNING, logger="menhir.infrastructure.telemetry.recorders"):
        record_mcp_event(
            kind="background", operation="synthetic", store=store, **{field: value},
        )

    store.record.assert_not_called()
    assert any("preparation failed (ValueError)" in message for message in caplog.messages)
    assert "Circular reference detected" not in caplog.text


@pytest.mark.parametrize(
    ("recorder", "fields", "expected"),
    [
        (record_failure_event, {"operation": "test", "error": "test"}, False),
        (record_lifecycle_event, {"component": "test", "event": "test", "state": "test"}, None),
        (recorders.record_episode_task_event, {"parent_task": None, "child_task": None, "phase": "test"}, None),
    ],
)
def test_episode_recorders_drop_unserializable_details(recorder, fields, expected, caplog):
    store = Mock()
    details = {"hidden argument": 1, 2: 3}

    assert recorder(episode_uuid="test", details=details, store=store, **fields) is expected

    assert not store.mock_calls
    assert "preparation failed (TypeError)" in caplog.text
    assert "hidden argument" not in caplog.text


def test_record_failure_event_persists_structured_failure_details():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        record_failure_event(
            operation="episode_enrichment",
            episode_uuid="pending-1",
            failure_stage="graphiti_exception",
            classification="retryable",
            retryable=True,
            processing_attempt=2,
            queue_depth=3,
            worker_id="worker-1",
            error_type="RuntimeError",
            error="graphiti unavailable",
            details={"session_id": "session-1", "duration_ms": 250},
            store=store,
        )

        rows = _fetch_failure_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["operation"] == "episode_enrichment"
        assert row["episode_uuid"] == "pending-1"
        assert row["failure_stage"] == "graphiti_exception"
        assert row["classification"] == "retryable"
        assert row["retryable"] == 1
        assert row["processing_attempt"] == 2
        assert row["queue_depth"] == 3
        assert row["worker_id"] == "worker-1"
        assert row["error_type"] == "RuntimeError"
        assert row["error"] == "graphiti unavailable"
        assert '"duration_ms": 250' in row["details_json"]
    finally:
        _cleanup_db_path(db_path)


def test_track_mcp_call_uses_custom_error_mapper():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    async def _runner():
        raise RuntimeError("boom")

    try:
        result = asyncio.run(
            track_mcp_call(
                kind="resource",
                operation="memory://recent",
                payload={},
                runner=_runner,
                store=store,
                error_mapper=lambda error: {"ok": False, "error": error},
            )
        )
        assert result == {"ok": False, "error": "RuntimeError: boom"}
        rows = _fetch_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["success"] == 0
    finally:
        _cleanup_db_path(db_path)


def test_record_failure_event_persists_traceback_text():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        record_failure_event(
            operation="episode_enrichment",
            episode_uuid="pending-1",
            failure_stage="graphiti_exception",
            classification="retryable",
            retryable=True,
            processing_attempt=1,
            error_type="ValueError",
            error="boom",
            traceback_text="Traceback line 1\nTraceback line 2",
            details={"session_id": "session-1"},
            store=store,
        )

        rows = _fetch_failure_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert '"traceback": "Traceback line 1\\nTraceback line 2"' in row["details_json"]
        assert '"traceback_preview": "Traceback line 1\\nTraceback line 2"' in row["details_json"]
    finally:
        _cleanup_db_path(db_path)


def test_record_failure_event_deduplicates_same_episode_attempt_and_error():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        first = record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid="episode-1",
            failure_stage="retry_classification",
            classification="terminal",
            retryable=False,
            processing_attempt=3,
            error_type="terminal_failure",
            error="zero_extraction",
            details={"decision": "not_requeued"},
            store=store,
        )
        second = record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid="episode-1",
            failure_stage="retry_classification",
            classification="terminal",
            retryable=False,
            processing_attempt=3,
            error_type="terminal_failure",
            error="zero_extraction",
            details={"decision": "not_requeued"},
            store=store,
        )

        rows = _fetch_failure_rows(db_path)
        assert first is True
        assert second is False
        assert len(rows) == 1
    finally:
        _cleanup_db_path(db_path)


def test_record_failure_event_dedupe_runs_in_immediate_transaction(monkeypatch):
    db_path = _make_db_path()
    first_store = McpTelemetryStore(db_path=db_path)
    second_store = McpTelemetryStore(db_path=db_path)
    identity = dict(
        operation="scheduler_retry_failed_enrichments",
        episode_uuid="episode-1",
        failure_stage="retry_classification",
        classification="terminal",
        retryable=False,
        processing_attempt=3,
        error_type="terminal_failure",
        error="zero_extraction",
    )
    try:
        assert record_failure_event(store=first_store, **identity) is True

        second_store._ensure_ready()
        statements: list[str] = []
        select_in_transaction: list[bool] = []
        original_connect = second_store._connect

        def _instrumented_connect():
            conn = original_connect()

            def _trace(statement: str) -> None:
                statements.append(statement)
                if "SELECT 1" in statement and "FROM failure_events" in statement:
                    select_in_transaction.append(conn.in_transaction)

            conn.set_trace_callback(_trace)
            return conn

        monkeypatch.setattr(second_store, "_connect", _instrumented_connect)

        assert record_failure_event(store=second_store, **identity) is False
        assert any(
            statement.strip().upper().startswith("BEGIN IMMEDIATE")
            for statement in statements
        )
        assert select_in_transaction == [True]
        assert len(_fetch_failure_rows(db_path)) == 1
    finally:
        _cleanup_db_path(db_path)


def test_record_failure_event_keeps_distinct_attempts():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid="episode-1",
            failure_stage="retry_attempts_exhausted",
            classification="exhausted",
            retryable=False,
            processing_attempt=2,
            error_type="retry_attempts_exhausted",
            error="model unloaded",
            store=store,
        )
        record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid="episode-1",
            failure_stage="retry_attempts_exhausted",
            classification="exhausted",
            retryable=False,
            processing_attempt=3,
            error_type="retry_attempts_exhausted",
            error="model unloaded",
            store=store,
        )

        rows = _fetch_failure_rows(db_path)
        assert len(rows) == 2
    finally:
        _cleanup_db_path(db_path)


def test_record_lifecycle_event_persists_debug_trace_rows():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        record_lifecycle_event(
            component="ingest_worker",
            event="graphiti_add_episode",
            state="started",
            episode_uuid="episode-1",
            details={"worker_id": "worker-1"},
            store=store,
        )

        rows = _fetch_lifecycle_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["component"] == "ingest_worker"
        assert row["event"] == "graphiti_add_episode"
        assert row["state"] == "started"
        assert row["episode_uuid"] == "episode-1"
        assert '"worker_id": "worker-1"' in row["details_json"]
    finally:
        _cleanup_db_path(db_path)


def test_record_llm_usage_event_persists_and_aggregates_provider_counts(monkeypatch):
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    monkeypatch.setenv("MENHIR_BENCH_ACTIVE_RUN_ID", "canonical-test-run")
    completed = LLMUsageEvent(
        kind="chat",
        phase="completed",
        model="gpt-test",
        endpoint="chat.completions.create",
        operation="graphiti_extract",
        call_id="call-1",
        duration_ms=25,
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        cached_input_tokens=80,
        reasoning_output_tokens=12,
        provider_usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    )
    failed = LLMUsageEvent(
        kind="chat",
        phase="failed",
        model="gpt-test",
        endpoint="chat.completions.create",
        operation="graphiti_extract",
        call_id="call-2",
        duration_ms=10,
        error="provider unavailable",
    )
    try:
        assert record_llm_usage_event(event=completed, episode_uuid="episode-1", store=store) is True
        assert record_llm_usage_event(event=completed, episode_uuid="episode-1", store=store) is False
        assert record_llm_usage_event(event=failed, episode_uuid="episode-1", store=store) is True

        summary = store.fetch_llm_usage_summary(run_id="canonical-test-run")
        assert summary["calls"] == 2
        assert summary["completed_calls"] == 1
        assert summary["failed_calls"] == 1
        assert summary["missing_usage_calls"] == 0
        assert summary["input_tokens"] == 120
        assert summary["output_tokens"] == 30
        assert summary["total_tokens"] == 150
        assert summary["cached_input_tokens"] == 80
        assert summary["reasoning_output_tokens"] == 12
        assert summary["by_model"][0]["model"] == "gpt-test"
    finally:
        _cleanup_db_path(db_path)


def test_record_lab_runs_return_none_on_sqlite_operational_error(monkeypatch):
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)

    def _raise_sqlite_error():
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(store, "_ensure_ready", _raise_sqlite_error)

    try:
        assert store.record_recall_lab_run(
            request_payload={"query": "x"},
            result_payload={"query": "x", "arms": []},
        ) is None
        assert store.record_extraction_lab_run(
            request_payload={"current_message": "x"},
            result_payload={"current_message": "x", "arms": []},
            namespace="lab",
        ) is None
    finally:
        _cleanup_db_path(db_path)


def test_fetch_recent_clamps_limit_and_keeps_default_and_order():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store._ensure_ready()
        _seed_mcp_events(db_path, 502)

        assert store.fetch_recent(limit=-1) == []
        assert store.fetch_recent(limit=0) == []
        clamped = store.fetch_recent(limit=10_000)
        assert len(clamped) == 500
        assert [row["operation"] for row in clamped[:3]] == [
            "operation-501", "operation-500", "operation-499",
        ]

        default = store.fetch_recent()
        assert len(default) == 50
        assert default[0]["operation"] == "operation-501"
    finally:
        _cleanup_db_path(db_path)


def test_fetch_recent_query_uses_started_at_index():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store._ensure_ready()
        _seed_mcp_events(db_path, 502)

        plan = _recent_query_plan(db_path)
        assert any(
            "USING INDEX idx_mcp_events_started_at" in detail for detail in plan
        )
        assert not any("TEMP B-TREE" in detail for detail in plan)
        assert [row["operation"] for row in store.fetch_recent(limit=3)] == [
            "operation-501", "operation-500", "operation-499",
        ]
    finally:
        _cleanup_db_path(db_path)


def test_started_at_index_is_recreated_on_reopen_without_losing_events():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store._ensure_ready()
        _seed_mcp_events(db_path, 12)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("DROP INDEX IF EXISTS idx_mcp_events_started_at")
        assert not any(
            "idx_mcp_events_started_at" in detail for detail in _recent_query_plan(db_path)
        )

        reopened = McpTelemetryStore(db_path=db_path)
        reopened._ensure_ready()
        with closing(sqlite3.connect(db_path)) as conn:
            (index_sql,) = conn.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'index' AND name = 'idx_mcp_events_started_at'
                """
            ).fetchone()
            (count,) = conn.execute("SELECT COUNT(*) FROM mcp_events").fetchone()
        assert "started_at DESC" in index_sql
        assert count == 12
        assert any(
            "USING INDEX idx_mcp_events_started_at" in detail
            for detail in _recent_query_plan(db_path)
        )
        assert [row["operation"] for row in reopened.fetch_recent(limit=12)] == [
            f"operation-{index}" for index in range(11, -1, -1)
        ]
    finally:
        _cleanup_db_path(db_path)


def test_fetch_recent_failures_clamps_limit_and_keeps_episode_filter():
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store._ensure_ready()
        _seed_failure_events(db_path, 502)

        assert store.fetch_recent_failures(limit=-5) == []
        assert store.fetch_recent_failures(limit=0) == []
        clamped = store.fetch_recent_failures(limit=10_000)
        assert len(clamped) == 500
        assert [row["error"] for row in clamped[:3]] == [
            "failure-501", "failure-500", "failure-499",
        ]

        scoped = store.fetch_recent_failures(limit=10_000, episode_uuid="episode-even")
        assert len(scoped) == 251
        assert all(row["episode_uuid"] == "episode-even" for row in scoped)
        assert scoped[0]["error"] == "failure-500"

        default = store.fetch_recent_failures()
        assert len(default) == 50
        assert default[0]["error"] == "failure-501"
    finally:
        _cleanup_db_path(db_path)
