"""Redacting the telemetry sidecar's content-bearing rows (owner ruling 2026-08-21).

*"There is very little value in preserving historical raw prompt/memory snippets inside an
observability sidecar... this is probably the one decision where doing nothing continues to carry
privacy/erasure debt."*

THE SELECTOR IS THE DECISION. `redact_unaddressable_legacy` already existed and selects on NULL
LINEAGE -- measured on the live sidecar that is **214,969 rows spanning 78 operations**, which is
the opposite of what was ruled: it mutates ~209k scheduler/enrichment rows whose previews are
operation shape (`{"max_age_hours": 4.0}`) and carry nothing to erase, while leaving a genuine
content row untouched if it happens to have lineage.

`redact_content_operations` selects on the OPERATION instead -- 6,131 rows on the same sidecar, of
which 6,071 still held content. Content-bearing and unaddressable are different properties, so they
are different functions rather than one function with a mode.

Every test here runs against a throwaway SQLite file. The real sidecar is 1.85 GB of the operator's
data and redaction is irreversible.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.services.erasure_backfill import (
    CONTENT_BEARING_OPERATIONS,
    redact_content_operations,
    redact_unaddressable_legacy,
)

pytestmark = pytest.mark.unit


def _db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE mcp_events (id INTEGER PRIMARY KEY, operation TEXT, "
        "payload_preview TEXT, error TEXT, node_uuid TEXT, namespace TEXT)"
    )
    rows = [
        # content-bearing, no lineage -- the common historical shape
        ("add_memory", '{"text": "my private memory"}', None, None, None),
        ("recall_memories", '{"query": "workspace rename"}', None, None, None),
        ("ingest_document", '{"path": "/x/y.md"}', None, None, None),
        # content-bearing WITH lineage: still content, still redacted
        ("add_memory", '{"text": "also private"}', None, "uuid-1", "archolith"),
        # content-bearing with an error string
        ("build_context", '{"query": "q"}', "PermissionError: nope", None, None),
        # operational, no lineage -- must NOT be touched
        ("scheduler_queue_health", '{"max_age_hours": 4.0}', None, None, None),
        ("episode_enrichment", '{"episode": 3}', None, None, None),
        ("identity_decision", '{"a": 1}', None, None, None),
    ]
    conn.executemany("INSERT INTO mcp_events (operation, payload_preview, error, node_uuid, namespace) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _previews(conn) -> dict[str, list]:
    out: dict[str, list] = {}
    for op, prev in conn.execute("SELECT operation, payload_preview FROM mcp_events"):
        out.setdefault(op, []).append(prev)
    return out


# ---------------------------------------------------------------------------
# the ruling
# ---------------------------------------------------------------------------


def test_content_operations_are_redacted(tmp_path) -> None:
    conn = _db(tmp_path)
    redact_content_operations(conn, dry_run=False)

    previews = _previews(conn)
    for op in CONTENT_BEARING_OPERATIONS:
        for value in previews.get(op, []):
            assert value is None, f"{op} preview survived: {value!r}"


def test_operational_rows_are_left_alone(tmp_path) -> None:
    """THE HALF THAT IS EASY TO GET WRONG. The ruling is explicit: do not mutate the ~209k
    scheduler/operational rows. They carry no user content and redacting them destroys telemetry
    for no privacy gain."""
    conn = _db(tmp_path)
    redact_content_operations(conn, dry_run=False)

    previews = _previews(conn)
    assert previews["scheduler_queue_health"] == ['{"max_age_hours": 4.0}']
    assert previews["episode_enrichment"] == ['{"episode": 3}']
    assert previews["identity_decision"] == ['{"a": 1}']


def test_a_content_row_with_lineage_is_still_redacted(tmp_path) -> None:
    """Lineage is not part of the predicate. A memory's text sitting in an observability sidecar is
    the thing being protected; that a subject purge COULD reach it is not a reason to keep it."""
    conn = _db(tmp_path)
    redact_content_operations(conn, dry_run=False)

    row = conn.execute(
        "SELECT payload_preview FROM mcp_events WHERE node_uuid = 'uuid-1'"
    ).fetchone()
    assert row[0] is None


def test_the_error_column_is_redacted_too(tmp_path) -> None:
    """Exception text on a content operation can contain the request body."""
    conn = _db(tmp_path)
    redact_content_operations(conn, dry_run=False)

    row = conn.execute("SELECT error FROM mcp_events WHERE operation = 'build_context'").fetchone()
    assert row[0] is None


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


def test_dry_run_counts_without_writing(tmp_path) -> None:
    """Irreversible operations get a preview. A dry run that silently mutated would be the worst
    possible defect in this function."""
    conn = _db(tmp_path)
    before = _previews(conn)

    report = redact_content_operations(conn, dry_run=True)

    assert report["mcp_events.payload_preview"] == 5
    assert report["mcp_events.error"] == 1
    assert _previews(conn) == before


def test_it_is_idempotent(tmp_path) -> None:
    """Re-running must be a no-op, not an error: an operator who is unsure whether it ran should be
    able to run it again."""
    conn = _db(tmp_path)
    first = redact_content_operations(conn, dry_run=False)
    second = redact_content_operations(conn, dry_run=False)

    assert first["mcp_events.payload_preview"] == 5
    assert second == {}


def test_an_empty_operation_list_does_nothing(tmp_path) -> None:
    """POSITIVE CONTROL against a fencepost that would redact everything."""
    conn = _db(tmp_path)
    before = _previews(conn)

    assert redact_content_operations(conn, dry_run=False, operations=()) == {}
    assert _previews(conn) == before


# ---------------------------------------------------------------------------
# the two selectors are different, and must stay different
# ---------------------------------------------------------------------------


def test_the_unaddressable_selector_would_hit_operational_rows(tmp_path) -> None:
    """THE REASON THIS IS A SEPARATE FUNCTION, pinned so the two are never merged.

    `redact_unaddressable_legacy` selects NULL lineage, so on this fixture it takes the scheduler
    and enrichment rows as well -- exactly what the ruling excludes. On the live sidecar that is
    214,969 rows against this function's 6,131."""
    conn = _db(tmp_path)

    unaddressable = redact_unaddressable_legacy(conn, dry_run=True)
    content = redact_content_operations(conn, dry_run=True)

    assert unaddressable["mcp_events.payload_preview"] == 7  # includes the 3 operational rows
    assert content["mcp_events.payload_preview"] == 5  # content operations only
