"""CF-108: `PendingActionStore` schema init must run once per db_path, not per instance.

`explorer/app.py` builds a fresh ``PendingActionStore`` on each request. Before this change the
per-instance ``_initialized`` flag was ``False`` every time, so every request re-ran a ``mkdir``,
a connect, and a ``CREATE TABLE IF NOT EXISTS``; the per-instance ``threading.Lock`` serialised
nothing across requests. The fix memoizes initialization in class-level state keyed on the
resolved ``db_path`` so the DDL runs once per file while per-request construction of the cheap
dataclass stays cheap. The memo must be keyed (not a module-level singleton) so stores built
against the per-test redirected ``MENHIR_MCP_TELEMETRY_DB`` still target the test's isolated DB.
"""

from __future__ import annotations

import threading

import pytest

import menhir.infrastructure.pending_actions as pending_actions
from menhir.infrastructure.pending_actions import PendingActionStore


@pytest.fixture(autouse=True)
def _isolate_memo():
    """Snapshot and clear the class-level memo so tests start from a clean slate.

    The memo is process-global; tests use unique ``tmp_path`` DBs so there is no cross-test
    collision, but clearing it makes each test deterministic and independent of run order.
    """
    saved = set(PendingActionStore._initialized_paths)
    PendingActionStore._initialized_paths.clear()
    try:
        yield
    finally:
        PendingActionStore._initialized_paths.clear()
        PendingActionStore._initialized_paths.update(saved)


def _install_connect_counter(monkeypatch) -> dict:
    """Count `connect_telemetry_db` calls as seen from the pending_actions module.

    The store imports the helper directly, so patching the module-level name is the seam that
    observes exactly what the store calls. `_ensure_ready` calls it once for the DDL and zero
    times when the path is already memoized.
    """
    counter = {"n": 0}
    original = pending_actions.connect_telemetry_db

    def counting(db_path, *args, **kwargs):
        counter["n"] += 1
        return original(db_path, *args, **kwargs)

    monkeypatch.setattr(pending_actions, "connect_telemetry_db", counting)
    return counter


def test_schema_init_runs_once_for_same_path(tmp_path, monkeypatch) -> None:
    counter = _install_connect_counter(monkeypatch)
    db = tmp_path / "pending.db"
    for _ in range(5):
        PendingActionStore(db_path=db)._ensure_ready()
    # One DDL connect for the whole run; the other four constructions hit the memo.
    assert counter["n"] == 1


def test_different_paths_each_initialize(tmp_path, monkeypatch) -> None:
    counter = _install_connect_counter(monkeypatch)
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    PendingActionStore(db_path=db_a)._ensure_ready()
    PendingActionStore(db_path=db_b)._ensure_ready()
    PendingActionStore(db_path=db_a)._ensure_ready()  # memoized; no third connect
    assert counter["n"] == 2
    # Both paths remain independently usable.
    PendingActionStore(db_path=db_a).upsert("cf108-a", "compress")
    PendingActionStore(db_path=db_b).upsert("cf108-b", "compress")
    assert {r["node_uuid"] for r in PendingActionStore(db_path=db_a).fetch_pending()} == {
        "cf108-a"
    }


def test_positive_control_round_trip_after_memo(tmp_path) -> None:
    db = tmp_path / "roundtrip.db"
    store = PendingActionStore(db_path=db)
    store.upsert(node_uuid="cf108-x", action="compress", context="ctx")
    rows = store.fetch_pending(action="compress")
    assert len(rows) == 1
    assert rows[0]["node_uuid"] == "cf108-x"
    assert rows[0]["action"] == "compress"
    assert rows[0]["context"] == "ctx"
    # A second store against the same DB (already memoized) still reads correctly.
    rows = PendingActionStore(db_path=db).fetch_pending(action="compress")
    assert len(rows) == 1


def test_positive_control_explicit_path_targets_that_path(tmp_path) -> None:
    # `isolated_telemetry_db` (autouse) redirects MENHIR_MCP_TELEMETRY_DB to a temp DB, so the
    # default resolves to a different path than the explicit one we write to below.
    explicit = tmp_path / "explicit.db"
    PendingActionStore(db_path=explicit).upsert("only-explicit", "compress")
    rows = PendingActionStore(db_path=explicit).fetch_pending()
    assert rows and rows[0]["node_uuid"] == "only-explicit"
    # The default path must NOT contain the row written to the explicit path.
    default_rows = PendingActionStore().fetch_pending()
    assert all(r["node_uuid"] != "only-explicit" for r in default_rows)


def test_thread_safety_initializes_once(tmp_path, monkeypatch) -> None:
    counter = _install_connect_counter(monkeypatch)
    db = tmp_path / "threaded.db"

    def init() -> None:
        PendingActionStore(db_path=db)._ensure_ready()

    threads = [threading.Thread(target=init) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The class-level lock serialises the first run so the DDL connect happens exactly once.
    assert counter["n"] == 1
    # Still usable after concurrent initialization.
    PendingActionStore(db_path=db).upsert("threaded", "compress")
    rows = PendingActionStore(db_path=db).fetch_pending()
    assert rows and rows[0]["node_uuid"] == "threaded"
