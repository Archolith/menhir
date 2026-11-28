"""#957: the destructive-op audit and query-auth log must persist which operation ran."""

from __future__ import annotations

import json
import sqlite3

from menhir.infrastructure.telemetry import recorders
from menhir.infrastructure.telemetry.helpers import _SAFE_TELEMETRY_STRING_KEYS
from menhir.infrastructure.telemetry.recorders import record_mcp_event
from menhir.infrastructure.telemetry.store import McpTelemetryStore


def _persisted(store: McpTelemetryStore, operation: str) -> dict:
    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute(
            "SELECT payload_preview FROM mcp_events WHERE operation = ?", (operation,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def test_destructive_op_row_keeps_operation_name(tmp_path, monkeypatch) -> None:
    store = McpTelemetryStore(tmp_path / "t.db")
    real = record_mcp_event
    monkeypatch.setattr(
        recorders, "record_mcp_event", lambda **kw: real(store=store, **kw)
    )
    recorders.record_destructive_op(
        surface="rest", name="delete_namespace", tier="operator", session_id="s1", user_id="u1"
    )
    payload = _persisted(store, "destructive_op")
    assert payload["operation"] == "delete_namespace"


def test_query_auth_row_keeps_tool_name(tmp_path, monkeypatch) -> None:
    from menhir.mcp import contracts

    store = McpTelemetryStore(tmp_path / "t.db")
    real = record_mcp_event
    monkeypatch.setattr(contracts, "record_mcp_event", lambda **kw: real(store=store, **kw))
    contracts._try_log_query_auth_usage("recall_memories")
    assert _persisted(store, "query_auth_usage")["operation"] == "recall_memories"


def test_free_text_name_keys_stay_unallowlisted() -> None:
    # `name` / `tool` can carry entity or memory names elsewhere; they must stay redacted.
    assert "name" not in _SAFE_TELEMETRY_STRING_KEYS
    assert "tool" not in _SAFE_TELEMETRY_STRING_KEYS
