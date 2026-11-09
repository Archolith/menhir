"""Opt-in recall query log: query text + ordered result ids linked to the recall receipt token."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.infrastructure.telemetry.erasure_purge import ErasureSubjects, purge_content
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.mcp import feedback

pytestmark = pytest.mark.unit

QUERY = "what colour is the sky"


@pytest.fixture
def store(tmp_path) -> McpTelemetryStore:
    s = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    s._ensure_ready()
    return s


@pytest.fixture
def wired(store, monkeypatch):
    session = SimpleNamespace(session_id="sess-1", client_id="client-1")
    with (
        patch("menhir.mcp.telemetry.telemetry_store", store),
        patch("menhir.mcp.service_access.get_request_session", return_value=session),
    ):
        yield store


def _payload() -> dict:
    return {
        "query": QUERY,
        "preset": "knowledge",
        "items": [
            {"uuid": "u-2", "score": 0.9},
            {"uuid": "u-1", "score": 0.4},
            {"uuid": "u-3"},
        ],
        "source_memories": [{"uuid": "s-1"}],
    }


def _rows(store: McpTelemetryStore) -> list[sqlite3.Row]:
    # Writes made on an event loop are queued (#527); a raw read must drain like the store does.
    from menhir.infrastructure.telemetry.async_writer import TELEMETRY_WRITER

    assert TELEMETRY_WRITER.drain(5)
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM recall_query_log ORDER BY id").fetchall()


def _receipts(store: McpTelemetryStore) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM recall_receipts").fetchone()[0]


CTX = {"namespace": "ns-a", "limit": 7}


def test_flag_off_writes_nothing(wired, monkeypatch):
    monkeypatch.delenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", raising=False)
    payload = _payload()
    feedback.attach_recall_receipt("recall_memories", payload, log_context=CTX)
    assert payload["recall_id"]
    assert _rows(wired) == []
    assert _receipts(wired) == 1


def test_flag_on_stores_query_and_ordered_ids(wired, monkeypatch):
    monkeypatch.setenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", "true")
    monkeypatch.delenv("MENHIR_PRIVACY_REDACT", raising=False)
    payload = _payload()
    feedback.attach_recall_receipt("recall_memories", payload, log_context=CTX)
    (row,) = _rows(wired)
    assert row["token"] == payload["recall_id"]
    assert row["operation"] == "recall_memories"
    assert row["query"] == QUERY
    assert row["query_sha256"] == hashlib.sha256(QUERY.encode()).hexdigest()
    assert json.loads(row["result_ids_json"]) == ["u-2", "u-1", "u-3"]
    assert json.loads(row["result_scores_json"]) == [0.9, 0.4, None]
    assert json.loads(row["source_memory_ids_json"]) == ["s-1"]
    assert (row["namespace"], row["result_limit"], row["preset"]) == (
        "ns-a",
        7,
        "knowledge",
    )
    assert (row["client_id"], row["session_id"]) == ("client-1", "sess-1")


def test_flag_on_with_redact_omits_query_text(wired, monkeypatch):
    monkeypatch.setenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", "true")
    monkeypatch.setenv("MENHIR_PRIVACY_REDACT", "true")
    feedback.attach_recall_receipt("recall_memories", _payload(), log_context=CTX)
    (row,) = _rows(wired)
    assert row["query"] is None
    assert row["query_sha256"] == hashlib.sha256(QUERY.encode()).hexdigest()
    assert json.loads(row["result_ids_json"]) == ["u-2", "u-1", "u-3"]


def test_payload_without_query_is_not_logged(wired, monkeypatch):
    monkeypatch.setenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", "true")
    payload = {"items": [{"uuid": "x"}]}
    feedback.attach_recall_receipt("read_flagged_memories", payload)
    assert payload["recall_id"]
    assert _rows(wired) == []


def test_logging_failure_never_breaks_the_response(wired, monkeypatch):
    monkeypatch.setenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", "true")
    monkeypatch.setattr(
        wired, "record_recall_query", MagicMock(side_effect=RuntimeError("boom"))
    )
    payload = _payload()
    feedback.attach_recall_receipt("recall_memories", payload, log_context=CTX)
    assert payload["recall_id"]


def _post_recall(store) -> None:
    backend = SimpleNamespace(
        recall=AsyncMock(
            return_value={
                "query": QUERY,
                "preset": "knowledge",
                "results": [
                    {"uuid": "h-1", "final_score": 0.8},
                    {"uuid": "h-2", "final_score": 0.5},
                ],
                "candidates_evaluated": 2,
            }
        )
    )
    app = FastAPI()
    app.include_router(router)
    with (
        patch.object(api_routes, "_get_backend", return_value=backend),
        patch("menhir.mcp.telemetry.telemetry_store", store),
    ):
        resp = TestClient(app).post(
            "/api/recall", json={"query": QUERY, "namespace": "ns-h", "limit": 4}
        )
    assert resp.status_code == 200


def test_http_recall_logs_with_null_token(store, monkeypatch):
    monkeypatch.setenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", "true")
    monkeypatch.delenv("MENHIR_PRIVACY_REDACT", raising=False)
    _post_recall(store)
    (row,) = _rows(store)
    assert row["token"] is None
    assert row["operation"] == "api_recall"
    assert row["query"] == QUERY
    assert json.loads(row["result_ids_json"]) == ["h-1", "h-2"]
    assert json.loads(row["result_scores_json"]) == [0.8, 0.5]
    assert row["result_limit"] == 4


def test_http_recall_flag_off_writes_nothing(store, monkeypatch):
    monkeypatch.delenv("MENHIR_TELEMETRY_RECORD_RECALL_QUERIES", raising=False)
    _post_recall(store)
    assert _rows(store) == []


def _insert(store, created_at: str, namespace: str = "ns-a") -> None:
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO recall_query_log (operation, created_at, namespace, query, query_sha256) "
            "VALUES ('recall_memories', ?, ?, 'q', 'h')",
            (created_at, namespace),
        )
        conn.commit()


def test_prune_deletes_only_rows_past_diagnostic_retention(store):
    _insert(store, "2020-01-01T00:00:00")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO recall_query_log (operation, created_at) "
            "VALUES ('recall_memories', datetime('now'))"
        )
        conn.commit()
    deleted = store.prune_telemetry_tables(observability_days=30, diagnostic_days=90)
    assert deleted["recall_query_log"] == 1
    assert len(_rows(store)) == 1


def test_namespace_erasure_scrubs_only_that_namespace(store):
    _insert(store, "2026-01-01T00:00:00", "tenant-a")
    _insert(store, "2026-01-01T00:00:00", "tenant-b")
    with sqlite3.connect(store.db_path) as conn:
        purge_content(
            conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=False
        )
        conn.commit()
    assert {r["namespace"]: r["query"] for r in _rows(store)}.get("tenant-b") == "q"
    assert not {r["query"] for r in _rows(store) if r["namespace"] == "tenant-a"} & {
        "q"
    }
