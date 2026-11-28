"""#961: touch_session tolerates None ids and never blanks a stored identity."""

from __future__ import annotations

import sqlite3

from menhir.infrastructure.telemetry.store import McpTelemetryStore


def _ids(store: McpTelemetryStore, session_id: str) -> tuple[str, str] | None:
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT client_id, client_name FROM session_registry WHERE session_id = ?", (session_id,)
        ).fetchone()
    return tuple(row) if row else None


def test_none_ids_still_record_the_session(tmp_path) -> None:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    store.touch_session("s1", None, None)
    assert _ids(store, "s1") == ("", "")


def test_blank_ids_do_not_overwrite_a_known_identity(tmp_path) -> None:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    store.touch_session("s1", "c1", "Claude")
    store.touch_session("s1", None, None)
    store.touch_session("s1", "", "")
    assert _ids(store, "s1") == ("c1", "Claude")
    store.touch_session("s1", "c2", "Other")
    assert _ids(store, "s1") == ("c2", "Other")
