import asyncio
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from menhir.mcp.telemetry import (
    McpTelemetryStore,
    record_failure_event,
    record_lifecycle_event,
    record_llm_usage_event,
    record_mcp_event,
    track_mcp_call,
)
from menhir.infrastructure.observability import LLMUsageEvent


_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"


def _make_db_path() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = _TEST_TMP_ROOT / f"mcp-telemetry-{uuid4()}"
    run_dir.mkdir()
    return run_dir / "mcp_telemetry.db"


def _cleanup_db_path(db_path: Path) -> None:
    shutil.rmtree(db_path.parent, ignore_errors=True)


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
        with store._connect() as conn:
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