"""A telemetry read after a timed-out writer drain must say it may be stale, not read silently."""

from __future__ import annotations

import logging

import pytest

from menhir.infrastructure.telemetry import store as store_mod
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit


def _ready_store(tmp_path) -> McpTelemetryStore:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    store.fetch_recent_lifecycle_events(limit=1)  # initialize the schema
    return store


def test_a_timed_out_drain_warns_and_the_read_still_returns(tmp_path, monkeypatch, caplog) -> None:
    store = _ready_store(tmp_path)
    monkeypatch.setattr(store_mod.TELEMETRY_WRITER, "drain", lambda timeout_s: False)

    with caplog.at_level(logging.WARNING, logger=store_mod.__name__):
        rows = store.fetch_recent_lifecycle_events(limit=10)

    assert rows == []
    assert any("still queued" in r.getMessage() for r in caplog.records), caplog.text


def test_a_completed_drain_does_not_warn(tmp_path, monkeypatch, caplog) -> None:
    store = _ready_store(tmp_path)
    monkeypatch.setattr(store_mod.TELEMETRY_WRITER, "drain", lambda timeout_s: True)

    with caplog.at_level(logging.WARNING, logger=store_mod.__name__):
        store.fetch_recent_lifecycle_events(limit=10)

    assert not any("still queued" in r.getMessage() for r in caplog.records), caplog.text
