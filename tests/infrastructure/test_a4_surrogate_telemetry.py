"""Ledger A4 - is the surrogate-encoding crash reachable, and what does it actually cost?

A lone surrogate is not exotic input. `json.loads('"\\ud800"')` yields one, so any MCP client or
HTTP body can produce a Python string SQLite refuses to store: sqlite3 raises
`UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud800' ... surrogates not allowed`.

Measured answer to A4:

* **Reachable: yes.** `memory_revisions.new_value` carries memory text, and consolidation writes it
  whenever it rewrites a memory. A memory containing a lone surrogate reaches that column.
* **Crashes the caller: no.** Every production call goes through the module-level recorder, which
  wraps the store in `except Exception`. `enrichment_steps` and `lifecycle_consolidation` both use
  the wrapper, never the store method directly.
* **What it costs: one silently lost audit row** -- and specifically the revision history of the one
  memory whose text is unusual.

These tests pin the guarantee (the caller is never broken) and document the cost (the row is lost),
so a refactor that removes the containment is caught and a future fix that preserves the row has to
change this file deliberately.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure.telemetry import recorders
from menhir.infrastructure.telemetry.store import McpTelemetryStore

LONE_SURROGATE = "memory text with \ud800 inside"


@pytest.fixture
def store(tmp_path) -> McpTelemetryStore:
    return McpTelemetryStore(db_path=tmp_path / "telemetry.db")


def _rows(store, table: str) -> int:
    try:
        with sqlite3.connect(store.db_path) as conn:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0


@pytest.mark.unit
def test_a_lone_surrogate_is_ordinary_json_input():
    """The premise. If this were exotic, A4 would be theoretical rather than reachable."""
    decoded = json.loads('{"text": "\\ud800"}')["text"]
    assert len(decoded) == 1
    assert 0xD800 <= ord(decoded) <= 0xDFFF


@pytest.mark.unit
def test_sqlite_refuses_a_lone_surrogate(tmp_path):
    """The mechanism, stated at the layer that actually raises."""
    path = tmp_path / "probe.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x TEXT)")
        with pytest.raises(UnicodeEncodeError):
            conn.execute("INSERT INTO t VALUES (?)", (LONE_SURROGATE,))


@pytest.mark.unit
def test_the_recorder_contains_it_and_the_caller_is_unaffected(store):
    """THE GUARANTEE. Telemetry must never break the operation it is observing.

    This is the property worth pinning: if someone removes the `except Exception` in
    `record_memory_revision`, a memory containing a lone surrogate starts crashing consolidation.
    """
    recorders.record_memory_revision(
        node_uuid="node-1",
        field="content",
        old_value="plain",
        new_value=LONE_SURROGATE,
        changed_by="ingest",
        store=store,
    )  # must not raise


@pytest.mark.unit
def test_the_cost_is_a_silently_lost_audit_row(store):
    """THE COST, documented rather than implied.

    The revision row is dropped. `memory_revisions` is the per-memory audit trail, so the memory
    whose text is unusual is exactly the one whose history has a hole. If this is ever fixed by
    sanitising the value at the write boundary, this test must be changed deliberately.
    """
    recorders.record_memory_revision(
        node_uuid="node-1", field="content", old_value="plain",
        new_value=LONE_SURROGATE, changed_by="ingest", store=store,
    )
    assert _rows(store, "memory_revisions") == 0

    # Positive control: the identical call with ordinary text DOES persist, so the assertion above
    # is about the surrogate and not about a store that never writes anything.
    recorders.record_memory_revision(
        node_uuid="node-2", field="content", old_value="plain",
        new_value="ordinary text", changed_by="ingest", store=store,
    )
    assert _rows(store, "memory_revisions") == 1


@pytest.mark.unit
def test_the_store_method_itself_still_raises(store):
    """Marks where the containment lives. The boundary is the recorder, not the store.

    A future caller reaching for the store method directly gets the exception, which is why every
    production call site goes through the module-level recorder.
    """
    with pytest.raises(UnicodeEncodeError):
        store.record_memory_revision(
            node_uuid="node-1", field="content", old_value="plain",
            new_value=LONE_SURROGATE, changed_by="ingest",
        )


@pytest.mark.unit
def test_mcp_event_payloads_are_neutralised_before_they_reach_sqlite(store):
    """The highest-volume caller-controlled path is safe for a different reason, worth pinning.

    `_preview_of` renders through `json.dumps` with `ensure_ascii=True`, so a lone surrogate comes
    back out as an ASCII escape and never reaches SQLite raw. That is why an `add_memory` carrying
    one does not lose its telemetry row the way a revision does.
    """
    recorders.record_mcp_event(
        kind="tool", operation="add_memory", duration_ms=1, success=True,
        payload={"text": LONE_SURROGATE}, store=store,
    )
    assert _rows(store, "mcp_events") == 1
