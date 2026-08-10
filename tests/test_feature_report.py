"""Unit tests for _feature_report assembly (parent rollups + usefulness merge)."""

from __future__ import annotations

from unittest.mock import patch

from menhir.explorer.app import _feature_report


def _fs_row(operation, kind="tool", total=10, successes=10, p50=100, p95=200,
            avg_result=500, calls_per_day=5.0):
    return {
        "operation": operation, "kind": kind, "total_calls": total,
        "successes": successes, "errors": total - successes,
        "success_rate_pct": round(successes / total * 100, 1) if total else 0.0,
        "calls_per_day": calls_per_day, "p50_ms": p50, "p95_ms": p95,
        "avg_result_size": avg_result, "avg_input_size": 10,
        "first_seen": "2026-07-01T00:00:00+00:00", "last_seen": "2026-07-03T00:00:00+00:00",
    }


def _run(feature_rows, usefulness):
    with patch("menhir.explorer.app.telemetry_store") as store:
        store.fetch_feature_stats.return_value = feature_rows
        store.fetch_usefulness_stats.return_value = usefulness
        return _feature_report("all")


def test_empty_report_does_not_crash():
    rep = _run([], {})
    assert rep["total_functions"] == 0
    assert rep["total_calls"] == 0
    assert rep["parents"] == []
    assert rep["functions"] == []
    assert rep["window"] == "all"


def test_functions_tagged_with_parent_and_usefulness():
    rows = [_fs_row("recall_memories"), _fs_row("add_memory")]
    useful = {"recall_memories": {"receipts": 4, "rated": 3, "scored": 2, "rated_pct": 75.0, "avg_score": 0.75}}
    rep = _run(rows, useful)
    fns = {f["operation"]: f for f in rep["functions"]}
    assert fns["recall_memories"]["parent"] == "retrieve"
    assert fns["recall_memories"]["avg_score"] == 0.75
    assert fns["recall_memories"]["rated_pct"] == 75.0
    # a function with no receipts gets safe defaults
    assert fns["add_memory"]["parent"] == "write"
    assert fns["add_memory"]["receipts"] == 0
    assert fns["add_memory"]["avg_score"] is None


def test_parent_rollup_weights_usefulness_by_scored_count():
    # two retrieve functions with different scored counts and averages
    rows = [_fs_row("recall_memories", total=10), _fs_row("read_flagged_memories", total=10)]
    useful = {
        "recall_memories": {"receipts": 4, "rated": 4, "scored": 4, "rated_pct": 100.0, "avg_score": 1.0},
        "read_flagged_memories": {"receipts": 2, "rated": 2, "scored": 2, "rated_pct": 100.0, "avg_score": 0.0},
    }
    rep = _run(rows, useful)
    retrieve = {p["name"]: p for p in rep["parents"]}["retrieve"]
    # weighted mean = (1.0*4 + 0.0*2) / (4+2) = 0.667
    assert retrieve["avg_score"] == 0.67
    assert retrieve["receipts"] == 6
    assert retrieve["rated"] == 6
    assert retrieve["function_count"] == 2


def test_parent_rollup_no_hit_rate_field():
    rep = _run([_fs_row("recall_memories")], {})
    p = rep["parents"][0]
    assert "hit_rate_pct" not in p
    assert "is_retrieval" not in p
