"""#960: the erasure lookup must stay under SQLite's bound-variable limit."""

from __future__ import annotations

import sqlite3

from menhir.infrastructure.erasure_subjects import suppressed_node_uuids


def test_candidates_beyond_the_variable_limit_are_looked_up_in_chunks(tmp_path) -> None:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE erasure_subjects (subject_type TEXT, subject_value TEXT, purged_at TEXT)")
    conn.execute("CREATE TABLE erased_subject_denylist (subject_value TEXT)")
    conn.execute("INSERT INTO erasure_subjects VALUES ('NODE_UUID', 'n-39999', NULL)")
    conn.execute("INSERT INTO erased_subject_denylist VALUES ('n-5')")
    conn.commit()
    conn.close()
    candidates = [f"n-{i}" for i in range(40_000)]
    assert suppressed_node_uuids(db, candidates) == frozenset({"n-39999", "n-5"})
