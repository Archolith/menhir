"""Tests for the CF-165 lineage backfill.

The property under test is restraint: derive only what a surviving node proves, and never
invent a namespace for a row that cannot prove one. An unprovable namespace is worse than a
NULL one, because a purge would believe it had covered content it never saw.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_backfill import (
    backfill_merge_audit_namespaces,
    redact_unaddressable_legacy,
    run_backfill,
    survey_unaddressable_legacy,
)

pytestmark = [pytest.mark.unit]


class FakeGraph:
    """Knows the namespace of nodes that still exist. Everything else is simply absent."""

    def __init__(self, namespaces: dict[str, str] | None = None) -> None:
        self.namespaces = namespaces or {}
        self.asked: list[list[str]] = []

    def fetch_node_namespaces(self, uuids: list[str]) -> dict[str, str]:
        self.asked.append(list(uuids))
        return {u: self.namespaces[u] for u in uuids if u in self.namespaces}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    McpTelemetryStore(db_path=path)._ensure_ready()
    return path


def _merge_row(conn, survivor, absorbed, *, namespaces=(None, None)):
    conn.execute(
        "INSERT INTO merge_audit "
        "(recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json, "
        "survivor_namespace, absorbed_namespace) VALUES (?,?,?,?,?,?,?)",
        ("t", survivor, absorbed, 0.9, '{"c":"x"}', namespaces[0], namespaces[1]),
    )


def test_backfill_derives_both_sides_from_a_live_survivor(db):
    """Merge requires a shared namespace, so a live survivor settles the absorbed side too."""
    with sqlite3.connect(db) as conn:
        _merge_row(conn, "surv-1", "abs-1")
        conn.commit()
        graph = FakeGraph({"surv-1": "ns-alpha"})

        report = backfill_merge_audit_namespaces(conn, graph, dry_run=False)
        conn.commit()

        assert report.merge_audit_null == 1
        assert report.merge_audit_written == 1
        row = conn.execute(
            "SELECT survivor_namespace, absorbed_namespace FROM merge_audit"
        ).fetchone()
    assert row == ("ns-alpha", "ns-alpha")


def test_backfill_refuses_to_guess_when_the_survivor_is_gone(db):
    """The restraint that matters: no live survivor means no proof, so the row stays NULL."""
    with sqlite3.connect(db) as conn:
        _merge_row(conn, "surv-gone", "abs-2")
        conn.commit()
        graph = FakeGraph({})

        report = backfill_merge_audit_namespaces(conn, graph, dry_run=False)
        conn.commit()

        assert report.merge_audit_underivable == 1
        assert report.merge_audit_written == 0
        row = conn.execute(
            "SELECT survivor_namespace, absorbed_namespace FROM merge_audit"
        ).fetchone()
    assert row == (None, None)


def test_dry_run_writes_nothing(db):
    with sqlite3.connect(db) as conn:
        _merge_row(conn, "surv-1", "abs-1")
        conn.commit()

        report = backfill_merge_audit_namespaces(conn, FakeGraph({"surv-1": "ns-a"}), dry_run=True)

        assert report.merge_audit_derivable == 1
        assert report.merge_audit_written == 0
        row = conn.execute(
            "SELECT survivor_namespace, absorbed_namespace FROM merge_audit"
        ).fetchone()
    assert row == (None, None)


def test_rows_that_already_have_lineage_are_left_alone(db):
    with sqlite3.connect(db) as conn:
        _merge_row(conn, "surv-1", "abs-1", namespaces=("ns-existing", "ns-existing"))
        conn.commit()
        graph = FakeGraph({"surv-1": "ns-different"})

        report = backfill_merge_audit_namespaces(conn, graph, dry_run=False)
        conn.commit()

        assert report.merge_audit_null == 0
        row = conn.execute(
            "SELECT survivor_namespace, absorbed_namespace FROM merge_audit"
        ).fetchone()
    assert row == ("ns-existing", "ns-existing")


def test_survey_counts_legacy_rows_no_key_can_reach(db):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, "
            "success, payload_preview) VALUES (?,?,?,?,?,?,?)",
            ("s", "e", 1, "op", "k", 1, "legacy secret"),
        )
        conn.execute(
            "INSERT INTO extraction_lab_runs (recorded_at, current_message, arms_json, "
            "request_json, result_json) VALUES (?,?,?,?,?)",
            ("t", "legacy message", "a", "r", "x"),
        )
        conn.commit()

        counts = survey_unaddressable_legacy(conn)

    assert counts["mcp_events"] == 1
    assert counts["extraction_lab_runs"] == 1


def test_redaction_is_opt_in_and_leaves_addressable_rows_untouched(db):
    """A row WITH lineage is reachable by a normal purge and is none of this function's business."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, "
            "success, payload_preview) VALUES (?,?,?,?,?,?,?)",
            ("s", "e", 1, "op", "k", 1, "legacy secret"),
        )
        conn.execute(
            "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, "
            "success, payload_preview, node_uuid, namespace) VALUES (?,?,?,?,?,?,?,?,?)",
            ("s", "e", 1, "op", "k", 1, "addressable", "n-1", "ns-1"),
        )
        conn.commit()

        # Default run reports but does not redact.
        report = run_backfill(conn, FakeGraph({}), dry_run=False)
        assert report.redacted == {}
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_events WHERE payload_preview IS NOT NULL"
        ).fetchone()[0] == 2

        redacted = redact_unaddressable_legacy(conn, dry_run=False)
        conn.commit()

        assert redacted["mcp_events.payload_preview"] == 1
        remaining = conn.execute(
            "SELECT payload_preview FROM mcp_events WHERE payload_preview IS NOT NULL"
        ).fetchall()
    assert remaining == [("addressable",)]


def test_redaction_dry_run_counts_without_erasing(db):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, "
            "success, payload_preview) VALUES (?,?,?,?,?,?,?)",
            ("s", "e", 1, "op", "k", 1, "legacy secret"),
        )
        conn.commit()

        redacted = redact_unaddressable_legacy(conn, dry_run=True)

        assert redacted["mcp_events.payload_preview"] == 1
        assert conn.execute(
            "SELECT payload_preview FROM mcp_events"
        ).fetchone()[0] == "legacy secret"


def test_backfilled_lineage_is_what_reaches_an_orphaned_merge_row(db):
    """The reason the backfill exists, stated as a test.

    A historical merge whose participants are both gone from the graph has no uuid in the set a
    namespace erasure captures, so uuid keys cannot reach it and the row would survive the
    erasure of its own namespace. Backfilled lineage is the durable selector that does reach it.
    """
    from menhir.infrastructure.telemetry.erasure_purge import ErasureSubjects, purge_content

    with sqlite3.connect(db) as conn:
        _merge_row(conn, "surv-orphan", "abs-orphan")
        conn.commit()

        # Erasing the namespace, knowing NO uuids -- both participants are gone from the graph.
        subjects = ErasureSubjects(namespaces=frozenset({"ns-alpha"}))

        before = purge_content(conn, subjects, dry_run=True)
        assert before.rows_affected.get("merge_audit.snapshot_json", 0) == 0

        # The survivor is still present here only as the source of proof for the backfill.
        backfill_merge_audit_namespaces(conn, FakeGraph({"surv-orphan": "ns-alpha"}), dry_run=False)
        conn.commit()

        after = purge_content(conn, subjects, dry_run=True)
        assert after.rows_affected["merge_audit.snapshot_json"] == 1

        purge_content(conn, subjects, dry_run=False)
        conn.commit()
        snapshot = conn.execute("SELECT snapshot_json FROM merge_audit").fetchone()[0]
    assert snapshot == ""
