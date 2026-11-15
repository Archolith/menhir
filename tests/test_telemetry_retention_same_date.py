"""Issue #157: same-date boundary behaviour of the telemetry retention pruner.

The existing CF-171 'T'-separator test seeds 2020 and 2099, so it passes even with the
`substr(replace(col, 'T', ' '), 1, 19)` normalization removed. The original bug: raw text
comparison of `2026-08-19T22:00:00` against cutoff `2026-08-19 23:00:00` keeps the expired row,
because 'T' (0x54) sorts above ' ' (0x20).

The pruner takes its cutoff from SQLite's own clock (`datetime('now', ...)`), not Python's, so the
clock is pinned by wrapping the connection the REAL `prune_telemetry_tables` opens and substituting
the `'now'` modifier with a fixed instant. No production code is edited.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit

# Fixed instant, 23:00 -- seconds from midnight. With a 1-day window the cutoff is
# 2026-08-19 23:00:00, so every row below shares the cutoff's calendar date.
PINNED_NOW = "2026-08-20 23:00:00"
CUTOFF = "2026-08-19 23:00:00"

_NORMALIZED = re.compile(r"substr\(replace\((\w+), 'T', ' '\), 1, 19\)")


class _PinnedConn:
    """Delegating connection: pins `datetime('now'` and optionally swaps in raw comparison."""

    def __init__(self, conn: sqlite3.Connection, raw_comparison: bool) -> None:
        self._conn = conn
        self._raw = raw_comparison
        self.rewrites = 0

    def execute(self, sql: str, *args):
        if "datetime('now'" in sql:
            sql = sql.replace("datetime('now'", f"datetime('{PINNED_NOW}'")
            self.rewrites += 1
        if self._raw:
            sql = _NORMALIZED.sub(r"\1", sql)
        return self._conn.execute(sql, *args)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def store(tmp_path: Path) -> McpTelemetryStore:
    st = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    st._ensure_ready()
    return st


def _pin(store: McpTelemetryStore, monkeypatch: pytest.MonkeyPatch, raw: bool = False) -> list:
    wrappers: list[_PinnedConn] = []
    real_connect = store._connect

    def connect():
        w = _PinnedConn(real_connect(), raw)
        wrappers.append(w)
        return w

    monkeypatch.setattr(store, "_connect", connect)
    return wrappers


# (timestamp, expected_to_survive)
CASES = [
    ("2026-08-19T22:00:00", False),  # the issue's example: T-form, 1h before cutoff
    ("2026-08-19 22:00:00", False),  # space-form, 1h before cutoff
    ("2026-08-19T22:59:59", False),  # 1s before cutoff, T-form
    ("2026-08-19 22:59:59", False),  # 1s before cutoff, space-form
    ("2026-08-19T22:59:59.999+00:00", False),  # fraction + offset are trimmed to 19 chars
    ("2026-08-19T23:00:00", True),  # exactly the cutoff, T-form
    ("2026-08-19 23:00:00", True),  # exactly the cutoff, space-form
    ("2026-08-19T23:00:00.500Z", True),  # equals cutoff once trimmed to 19 chars
    ("2026-08-19T23:00:01", True),  # 1s after cutoff, T-form
    ("2026-08-19 23:00:01", True),  # 1s after cutoff, space-form
    ("2026-08-19T23:59:59", True),  # same date, end of day
]


def _seed_all(store: McpTelemetryStore) -> None:
    with sqlite3.connect(store.db_path) as conn:
        for ts, _ in CASES:
            conn.execute(
                "INSERT INTO lifecycle_events(recorded_at,phase,event,status) VALUES(?,?,?,?)",
                (ts, "p", "e", "s"),
            )
        conn.commit()


def _remaining(store: McpTelemetryStore) -> set[str]:
    with sqlite3.connect(store.db_path) as conn:
        return {r[0] for r in conn.execute("SELECT recorded_at FROM lifecycle_events")}


def test_same_date_rows_around_the_cutoff_are_pruned_by_instant_not_text(
    store: McpTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows strictly before the pinned cutoff go; rows at or after it stay, whatever the
    separator. Equality: the current code uses a strict `<`, so a row exactly AT the cutoff is
    KEPT (in both separator forms). Fails if the normalization is replaced by raw `{column}`:
    the T-form expired rows then survive."""
    wrappers = _pin(store, monkeypatch)
    _seed_all(store)

    deleted = store.prune_telemetry_tables(observability_days=1, diagnostic_days=90)

    assert any(w.rewrites for w in wrappers), "clock pin did not take effect"
    expected_survivors = {ts for ts, keep in CASES if keep}
    expected_deleted = len(CASES) - len(expected_survivors)
    assert _remaining(store) == expected_survivors
    assert deleted["lifecycle_events"] == expected_deleted


def test_pruner_comparison_sanity_check_detects_raw_comparison(
    store: McpTelemetryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation guard: with normalization stripped (raw column compare) the same seed MUST
    diverge from the specified outcome. Proves the test above can fail, in-process, without
    editing production code."""
    _pin(store, monkeypatch, raw=True)
    _seed_all(store)

    store.prune_telemetry_tables(observability_days=1, diagnostic_days=90)

    expected_survivors = {ts for ts, keep in CASES if keep}
    assert _remaining(store) != expected_survivors, (
        "raw comparison produced the correct result; the same-date test cannot detect it"
    )
