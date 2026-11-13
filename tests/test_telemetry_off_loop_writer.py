"""#527: telemetry writes made on the event loop must not block it, lose order, or outlive erasure."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry import async_writer, helpers
from menhir.infrastructure.telemetry.async_writer import (
    TELEMETRY_WRITER,
    TelemetryDrainTimeout,
    TelemetryWriter,
)
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import REPLAYED, ErasureCoordinator


def _mcp_row(namespace: str) -> dict:
    return dict(
        kind="tool",
        operation="recall_memories",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=1,
        success=True,
        error=None,
        input_size=1,
        result_size=1,
        payload_preview='{"query":"private text"}',
        namespace=namespace,
        node_uuid=None,
    )


def _hold_writer() -> threading.Event:
    """Park the shared writer on a blocking item until the returned event is set."""
    release = threading.Event()
    started = threading.Event()

    def _block() -> None:
        started.set()
        release.wait(30)

    assert TELEMETRY_WRITER.submit("test-blocker", _block)
    assert started.wait(5)
    return release


def test_locked_telemetry_db_does_not_stall_other_coroutines(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store._ensure_ready()
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")

    async def _scenario() -> float:
        worst_gap = 0.0
        stop = asyncio.Event()

        async def _ticker() -> None:
            nonlocal worst_gap
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                worst_gap = max(worst_gap, now - last)
                last = now

        ticker = asyncio.create_task(_ticker())
        await asyncio.sleep(0.05)
        for i in range(3):
            store.record_lifecycle_event(
                recorded_at=f"2026-01-01T00:00:0{i}Z",
                component="ingest_worker",
                event="heartbeat_touch",
                state="completed",
                episode_uuid="ep-1",
                details_json=None,
            )
            await asyncio.sleep(0.05)
        stop.set()
        await ticker
        return worst_gap

    try:
        worst_gap = asyncio.run(_scenario())
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert TELEMETRY_WRITER.drain(30)
    # Before #527 each write stalled the loop for the busy timeout (~5 s).
    assert worst_gap < 1.0
    rows = store.fetch_recent_lifecycle_events(limit=10, component="ingest_worker")
    assert len(rows) <= 3  # writes that hit the lock fail non-fatally; none block the loop


def test_on_loop_writes_are_ordered_and_visible_to_the_next_read(tmp_path) -> None:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")

    async def _scenario() -> list[dict]:
        for i in range(25):
            store.record_lifecycle_event(
                recorded_at=f"2026-01-01T00:00:{i:02d}Z",
                component="order_test",
                event=f"e{i:02d}",
                state="completed",
                episode_uuid="ep-order",
                details_json=None,
            )
        # A read on the loop must see every write submitted before it.
        return store.fetch_recent_lifecycle_events(limit=100, component="order_test")

    rows = asyncio.run(_scenario())
    assert [r["event"] for r in reversed(rows)] == [f"e{i:02d}" for i in range(25)]


def test_off_loop_write_is_synchronous(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store.record(**_mcp_row("tenant-sync"))
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM mcp_events").fetchone()[0]
    assert count == 1


def test_full_queue_falls_back_to_caller_write() -> None:
    writer = TelemetryWriter(maxsize=1)
    release = threading.Event()
    started = threading.Event()
    assert writer.submit("blocker", lambda: (started.set(), release.wait(10)))
    assert started.wait(5)
    assert writer.submit("fills-queue", lambda: None)
    assert writer.submit("overflow", lambda: None) is False
    release.set()
    assert writer.drain(10)


def test_strict_drain_raises_while_writes_are_stuck() -> None:
    writer = TelemetryWriter()
    release = threading.Event()
    assert writer.submit("stuck", lambda: release.wait(10))
    with pytest.raises(TelemetryDrainTimeout):
        writer.drain_strict(0.05)
    release.set()
    writer.drain_strict(10)


def test_drain_from_writer_thread_does_not_deadlock() -> None:
    writer = TelemetryWriter()
    seen: list[bool] = []
    assert writer.submit("reentrant", lambda: seen.append(writer.drain(5)))
    assert writer.drain(10)
    assert seen == [True]


class _GraphAdapter:
    def capture_namespace_uuids(self, group_id: str, *, namespace: str | None = None):
        return []

    def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        return 1

    def purge_turn_evidence(self, namespace: str) -> int:
        return 0

    def delete_memory(self, node_uuid: str) -> bool:
        return False


def test_erasure_purges_a_row_queued_before_it_and_fails_closed_until_drained(
    tmp_path, monkeypatch
) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=_GraphAdapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    monkeypatch.setattr(helpers, "_SQLITE_BUSY_TIMEOUT_S", 0.05)

    release = _hold_writer()
    try:

        async def _queue_row() -> None:
            store.record(**_mcp_row("tenant-a"))

        asyncio.run(_queue_row())
        assert TELEMETRY_WRITER.pending() == 2  # blocker + the tenant-a row

        # The tenant-a row has not landed, so erasing now could leave it behind: refuse.
        with pytest.raises(TelemetryDrainTimeout):
            coord.erase_namespace("tenant-a", namespace="tenant-a")
    finally:
        release.set()

    # The refused attempt stays PREPARED and fences the target; recovery replays it.
    with sqlite3.connect(db) as conn:
        op_id, state = conn.execute(
            "SELECT op_id, state FROM graph_operations WHERE operation_kind = 'EXPLICIT_ERASURE'"
        ).fetchone()
    assert state == "PREPARED"
    outcome, _ = coord.replay_prepared_row(
        {"op_id": op_id}, owner_token=coord.journal.get(op_id)["owner_token"]
    )
    assert outcome == REPLAYED
    with sqlite3.connect(db) as conn:
        previews = conn.execute(
            "SELECT payload_preview FROM mcp_events WHERE namespace = 'tenant-a'"
        ).fetchall()
    assert previews == [(None,)]


def test_durable_records_are_not_offloaded() -> None:
    for name in ("record_memory_revision", "record_merge", "record_destructive_op"):
        assert name not in vars(async_writer.OffLoopObservabilityWritesMixin)


def test_recall_query_log_is_offloaded_and_purged_by_erasure(tmp_path, monkeypatch) -> None:
    db = tmp_path / "telemetry.db"
    store = McpTelemetryStore(db_path=db)
    store._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=_GraphAdapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    monkeypatch.setattr(helpers, "_SQLITE_BUSY_TIMEOUT_S", 0.05)

    release = _hold_writer()
    try:

        async def _queue_query() -> None:
            store.record_recall_query(query="private text", namespace="tenant-q", result_ids=["a"])

        asyncio.run(_queue_query())
        assert TELEMETRY_WRITER.pending() == 2  # blocker + the query row: it did not run on the loop
        with pytest.raises(TelemetryDrainTimeout):
            coord.erase_namespace("tenant-q", namespace="tenant-q")
    finally:
        release.set()

    with sqlite3.connect(db) as conn:
        op_id = conn.execute(
            "SELECT op_id FROM graph_operations WHERE operation_kind = 'EXPLICIT_ERASURE'"
        ).fetchone()[0]
    outcome, _ = coord.replay_prepared_row(
        {"op_id": op_id}, owner_token=coord.journal.get(op_id)["owner_token"]
    )
    assert outcome == REPLAYED
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT query FROM recall_query_log WHERE namespace = 'tenant-q'"
        ).fetchall()
    assert rows and all(r[0] is None for r in rows)
