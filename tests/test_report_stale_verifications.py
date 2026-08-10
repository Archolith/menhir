"""Tests for stale verification diagnostics report — classification, report building, CLI."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

from scripts.maintenance.report_stale_verifications import (
    _parse_iso_utc,
    _resolve_tool_events_base,
    classify_receipt,
    build_report_items,
    build_counts,
    build_full_report,
    IGNORED_REASON_WRONG_MEMORY,
    IGNORED_REASON_WRONG_PROJECT,
    IGNORED_REASON_WRONG_PATH,
    IGNORED_REASON_PRE_DIRTY,
    IGNORED_REASON_TIMESTAMP_ERROR,
    IGNORED_REASON_ANCHOR_TIMESTAMP_ERROR,
)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_M1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_M2 = "ffffffff-gggg-hhhh-iiii-jjjjjjjjjjjj"

ANCHOR_VALID = {
    "memory_uuid": _M1, "name": "test-memory", "project": "test-project",
    "path": "src/foo.py",
    "dirty_at": "2026-07-09T01:55:28Z",
    "anchored_at": "2026-07-01T00:00:00Z",
}

ANCHOR_NO_RECEIPT = {
    "memory_uuid": _M2, "name": "no-receipt-memory", "project": "test-project",
    "path": "src/missing.py",
    "dirty_at": "2026-07-09T01:55:28Z",
    "anchored_at": "2026-07-01T00:00:00Z",
}

RECEIPT_VALID = {
    "uuid": "v1", "memory_uuid": _M1, "project": "test-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "basis": "inspected_current_file",
    "verified_at": "2026-07-09T02:00:28Z",
}

RECEIPT_OUTDATED = {
    "uuid": "v2", "memory_uuid": _M1, "project": "test-project",
    "path": "src/foo.py", "outcome": "outdated",
    "verified_by": "smoke-agent", "basis": "inspected_current_file",
    "verified_at": "2026-07-10T00:00:00Z",
}

RECEIPT_WRONG_PATH = {
    "uuid": "v3", "memory_uuid": _M1, "project": "test-project",
    "path": "src/other.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "verified_at": "2026-07-09T02:00:28Z",
}

RECEIPT_PRE_DIRTY = {
    "uuid": "v4", "memory_uuid": _M1, "project": "test-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "verified_at": "2026-07-08T01:55:28Z",
}

RECEIPT_MALFORMED_TS = {
    "uuid": "v5", "memory_uuid": _M1, "project": "test-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "verified_at": "zzz",
}

RECEIPT_WRONG_PROJECT = {
    "uuid": "v6", "memory_uuid": _M1, "project": "other-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "verified_at": "2026-07-09T02:00:28Z",
}

RECEIPT_WRONG_MEMORY = {
    "uuid": "v7", "memory_uuid": "some-other-uuid", "project": "test-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent", "verified_at": "2026-07-09T02:00:28Z",
}

RECEIPT_MISSING_VERIFIED_AT = {
    "uuid": "v8", "memory_uuid": _M1, "project": "test-project",
    "path": "src/foo.py", "outcome": "still_valid",
    "verified_by": "smoke-agent",
}


# ---------------------------------------------------------------------------
# _parse_iso_utc
# ---------------------------------------------------------------------------


class TestParseIsoUtc:
    def test_valid_z_suffix(self):
        dt = _parse_iso_utc("2026-07-09T01:55:28Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.day == 9

    def test_valid_offset(self):
        dt = _parse_iso_utc("2026-07-09T01:55:28+00:00")
        assert dt is not None

    def test_naive_rejected(self):
        dt = _parse_iso_utc("2026-07-09T01:55:28")
        assert dt is None

    def test_none_returns_none(self):
        assert _parse_iso_utc(None) is None

    def test_empty_returns_none(self):
        assert _parse_iso_utc("") is None

    def test_garbage_returns_none(self):
        assert _parse_iso_utc("not-a-date") is None


# ---------------------------------------------------------------------------
# classify_receipt
# ---------------------------------------------------------------------------


class TestClassifyReceipt:
    def test_valid_same_path_post_dirty_is_valid(self):
        is_valid, reason = classify_receipt(RECEIPT_VALID, ANCHOR_VALID)
        assert is_valid is True
        assert reason is None

    def test_wrong_path_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_WRONG_PATH, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_WRONG_PATH

    def test_wrong_project_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_WRONG_PROJECT, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_WRONG_PROJECT

    def test_wrong_memory_uuid_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_WRONG_MEMORY, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_WRONG_MEMORY

    def test_pre_dirty_receipt_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_PRE_DIRTY, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_PRE_DIRTY

    def test_malformed_timestamp_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_MALFORMED_TS, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_TIMESTAMP_ERROR

    def test_missing_verified_at_is_ignored(self):
        is_valid, reason = classify_receipt(RECEIPT_MISSING_VERIFIED_AT, ANCHOR_VALID)
        assert is_valid is False
        assert reason == IGNORED_REASON_TIMESTAMP_ERROR

    def test_same_timestamp_edge_case(self):
        """verified_at == dirty_at should be valid (>= comparison)."""
        receipt = dict(RECEIPT_VALID)
        receipt["verified_at"] = "2026-07-09T01:55:28Z"
        is_valid, reason = classify_receipt(receipt, ANCHOR_VALID)
        assert is_valid is True
        assert reason is None

    def test_anchor_missing_dirty_at_conservative(self):
        """When anchor dirty_at is missing, receipt is ignored (cannot prove >=)."""
        anchor = dict(ANCHOR_VALID)
        anchor["dirty_at"] = None
        is_valid, reason = classify_receipt(RECEIPT_VALID, anchor)
        assert is_valid is False
        assert reason == IGNORED_REASON_ANCHOR_TIMESTAMP_ERROR

    def test_anchor_malformed_dirty_at_conservative(self):
        """When anchor dirty_at is malformed, receipt is ignored."""
        anchor = dict(ANCHOR_VALID)
        anchor["dirty_at"] = "not-a-timestamp"
        is_valid, reason = classify_receipt(RECEIPT_VALID, anchor)
        assert is_valid is False
        assert reason == IGNORED_REASON_ANCHOR_TIMESTAMP_ERROR


# ---------------------------------------------------------------------------
# build_report_items — integration of classification
# ---------------------------------------------------------------------------


class TestBuildReportItems:
    def test_valid_receipt_produces_valid_still_valid(self):
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_VALID]},
        )
        assert len(items) == 1
        assert items[0]["status"] == "valid_still_valid"
        assert items[0]["latest_valid_receipt"] is not None
        assert items[0]["latest_valid_receipt"]["outcome"] == "still_valid"

    def test_no_receipt_produces_no_receipt_status(self):
        items = build_report_items(
            [ANCHOR_NO_RECEIPT],
            {},
        )
        assert len(items) == 1
        assert items[0]["status"] == "no_receipt"
        assert items[0]["latest_valid_receipt"] is None
        assert items[0]["ignored_receipts"] == []

    def test_only_wrong_path_produces_only_ignored(self):
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_WRONG_PATH]},
        )
        assert len(items) == 1
        assert items[0]["status"] == "only_ignored_receipts"
        assert items[0]["latest_valid_receipt"] is None
        assert len(items[0]["ignored_receipts"]) == 1
        assert items[0]["ignored_receipts"][0]["reason"] == IGNORED_REASON_WRONG_PATH

    def test_only_pre_dirty_produces_only_ignored(self):
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_PRE_DIRTY]},
        )
        assert items[0]["status"] == "only_ignored_receipts"
        assert items[0]["latest_valid_receipt"] is None
        assert items[0]["ignored_receipts"][0]["reason"] == IGNORED_REASON_PRE_DIRTY

    def test_latest_valid_wins_by_verified_at(self):
        """Among valid receipts, the one with the latest verified_at wins."""
        earlier = dict(RECEIPT_VALID)
        later = dict(RECEIPT_OUTDATED)
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [earlier, later]},
        )
        assert items[0]["status"] == "valid_outdated"
        assert items[0]["latest_valid_receipt"]["outcome"] == "outdated"
        assert items[0]["latest_valid_receipt"]["verified_at"] == "2026-07-10T00:00:00Z"

    def test_mixed_valid_and_ignored(self):
        """Valid + ignored receipts: valid ones are counted, ignored are separated."""
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_VALID, RECEIPT_WRONG_PATH, RECEIPT_PRE_DIRTY]},
        )
        assert items[0]["status"] == "valid_still_valid"
        assert items[0]["latest_valid_receipt"] is not None
        assert len(items[0]["ignored_receipts"]) == 2
        reasons = {r["reason"] for r in items[0]["ignored_receipts"]}
        assert IGNORED_REASON_WRONG_PATH in reasons
        assert IGNORED_REASON_PRE_DIRTY in reasons

    def test_malformed_timestamp_in_ignored_list(self):
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_MALFORMED_TS]},
        )
        assert items[0]["status"] == "only_ignored_receipts"
        assert len(items[0]["ignored_receipts"]) == 1
        assert items[0]["ignored_receipts"][0]["reason"] == IGNORED_REASON_TIMESTAMP_ERROR

    def test_multiple_anchors(self):
        items = build_report_items(
            [ANCHOR_VALID, ANCHOR_NO_RECEIPT],
            {_M1: [RECEIPT_VALID]},
        )
        assert len(items) == 2
        statuses = {it["memory_uuid"]: it["status"] for it in items}
        assert statuses[_M1] == "valid_still_valid"
        assert statuses[_M2] == "no_receipt"

    def test_ignored_receipts_have_trimmed_fields(self):
        """Ignored receipts in output should only contain outcome, path, verified_at, reason."""
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_WRONG_PATH]},
        )
        ignored = items[0]["ignored_receipts"][0]
        assert set(ignored.keys()) == {"outcome", "path", "verified_at", "reason"}
        assert ignored["reason"] == IGNORED_REASON_WRONG_PATH

    def test_latest_valid_receipt_has_required_fields(self):
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_VALID]},
        )
        lvr = items[0]["latest_valid_receipt"]
        assert set(lvr.keys()) == {"outcome", "verified_at", "verified_by", "basis"}
        assert lvr["outcome"] == "still_valid"


# ---------------------------------------------------------------------------
# build_counts
# ---------------------------------------------------------------------------


class TestBuildCounts:
    def test_counts_match_items(self):
        items = build_report_items(
            [ANCHOR_VALID, ANCHOR_NO_RECEIPT],
            {_M1: [RECEIPT_VALID]},
        )
        counts = build_counts(items)
        assert counts["stale_anchors"] == 2
        assert counts["with_valid_receipt"] == 1
        assert counts["without_valid_receipt"] == 1
        assert counts["ignored_receipts"] == 0
        assert counts["valid_still_valid"] == 1
        assert counts["valid_outdated"] == 0

    def test_counts_with_ignored(self):
        items = build_report_items(
            [ANCHOR_VALID, ANCHOR_NO_RECEIPT],
            {_M1: [RECEIPT_VALID, RECEIPT_WRONG_PATH, RECEIPT_PRE_DIRTY]},
        )
        counts = build_counts(items)
        assert counts["stale_anchors"] == 2
        assert counts["ignored_receipts"] == 2

    def test_empty_items(self):
        counts = build_counts([])
        assert counts["stale_anchors"] == 0
        assert counts["with_valid_receipt"] == 0
        assert counts["without_valid_receipt"] == 0
        assert counts["ignored_receipts"] == 0


# ---------------------------------------------------------------------------
# build_full_report
# ---------------------------------------------------------------------------


class TestBuildFullReport:
    def test_report_has_counts_and_items(self):
        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        assert "counts" in report
        assert "items" in report
        assert report["counts"]["stale_anchors"] == 1

    def test_report_json_roundtrip(self):
        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        serialized = json.dumps(report, indent=2)
        parsed = json.loads(serialized)
        assert parsed["counts"]["stale_anchors"] == 1
        assert len(parsed["items"]) == 1
        assert parsed["items"][0]["status"] == "valid_still_valid"

    def test_report_json_output_is_parseable(self):
        """JSON output from build_full_report must be valid parseable JSON."""
        report = build_full_report(
            [ANCHOR_VALID, ANCHOR_NO_RECEIPT],
            {_M1: [RECEIPT_VALID, RECEIPT_WRONG_PATH, RECEIPT_PRE_DIRTY]},
        )
        blob = json.dumps(report, indent=2)
        data = json.loads(blob)
        assert "counts" in data
        assert "items" in data
        assert len(data["items"]) == 2
        assert data["counts"]["ignored_receipts"] == 2
        assert data["counts"]["valid_still_valid"] == 1


# ---------------------------------------------------------------------------
# Safety: no file content fields
# ---------------------------------------------------------------------------


class TestSafetyNoFileContent:
    def test_no_file_content_in_report_items(self):
        """The report must not contain file content fields."""
        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        blob = json.dumps(report)
        assert "content" not in blob
        assert "file_content" not in blob
        assert "transcript" not in blob

    def test_no_file_content_in_receipt_fields(self):
        """Receipt slim output must not contain content/hash fields."""
        items = build_report_items([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        for it in items:
            if it["latest_valid_receipt"]:
                for key in it["latest_valid_receipt"]:
                    assert "content" not in key
                    assert "hash" not in key
                    assert "transcript" not in key

    def test_ignored_receipts_no_content(self):
        items = build_report_items([ANCHOR_VALID], {_M1: [RECEIPT_WRONG_PATH]})
        for it in items:
            for ir in it["ignored_receipts"]:
                for key in ir:
                    assert "content" not in key
                    assert "hash" not in key
                    assert "transcript" not in key


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------


class TestHumanOutput:
    def test_human_output_includes_counts(self, capsys):
        from scripts.maintenance.report_stale_verifications import print_human

        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        print_human(report, "test-project")
        captured = capsys.readouterr()
        assert "Stale verification diagnostics" in captured.out
        assert "Project: test-project" in captured.out
        assert "stale anchors:" in captured.out
        assert "with valid receipt:" in captured.out

    def test_human_output_includes_item_status(self, capsys):
        from scripts.maintenance.report_stale_verifications import print_human

        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        print_human(report, None)
        captured = capsys.readouterr()
        assert "src/foo.py" in captured.out
        assert "valid_still_valid" in captured.out
        assert _M1[:8] in captured.out

    def test_human_output_empty(self, capsys):
        from scripts.maintenance.report_stale_verifications import print_human

        report = build_full_report([], {})
        print_human(report, None)
        captured = capsys.readouterr()
        assert "stale anchors:" in captured.out
        assert "0" in captured.out.split("stale anchors:")[1].strip()

    def test_human_output_no_project(self, capsys):
        from scripts.maintenance.report_stale_verifications import print_human

        report = build_full_report([ANCHOR_VALID], {_M1: [RECEIPT_VALID]})
        print_human(report, None)
        captured = capsys.readouterr()
        assert "Project:" not in captured.out


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestURLResolution:
    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
        monkeypatch.delenv("MENHIR_URL", raising=False)
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8090/api/tool-events"

    def test_menhir_url(self, monkeypatch):
        monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
        monkeypatch.setenv("MENHIR_URL", "http://127.0.0.1:8099")
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_tool_events_base(self, monkeypatch):
        monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL", "http://127.0.0.1:8099/api/tool-events")
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_tool_events_ending_dirty(self, monkeypatch):
        monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL",
                           "http://127.0.0.1:8099/api/tool-events/dirty")
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_tool_events_ending_stale(self, monkeypatch):
        monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL",
                           "http://127.0.0.1:8099/api/tool-events/stale")
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_tool_events_ending_stale_verifications(self, monkeypatch):
        monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL",
                           "http://127.0.0.1:8099/api/tool-events/stale-verifications")
        base = _resolve_tool_events_base()
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_explicit_url_override(self, monkeypatch):
        monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
        monkeypatch.delenv("MENHIR_URL", raising=False)
        base = _resolve_tool_events_base(url_override="http://127.0.0.1:8099")
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_explicit_url_already_tool_events(self, monkeypatch):
        monkeypatch.delenv("MENHIR_TOOL_EVENTS_URL", raising=False)
        monkeypatch.delenv("MENHIR_URL", raising=False)
        base = _resolve_tool_events_base(url_override="http://127.0.0.1:8099/api/tool-events")
        assert base == "http://127.0.0.1:8099/api/tool-events"

    def test_menhir_url_takes_second_precedence(self, monkeypatch):
        """MENHIR_TOOL_EVENTS_URL wins over MENHIR_URL."""
        monkeypatch.setenv("MENHIR_TOOL_EVENTS_URL", "http://a:1/api/tool-events")
        monkeypatch.setenv("MENHIR_URL", "http://b:2")
        base = _resolve_tool_events_base()
        assert base == "http://a:1/api/tool-events"

    def test_stale_url_construction(self):
        from scripts.maintenance.report_stale_verifications import _resolve_stale_url
        url = _resolve_stale_url("http://h:8090/api/tool-events", "my-project")
        assert url == "http://h:8090/api/tool-events/stale?project=my-project"

    def test_stale_url_no_project(self):
        from scripts.maintenance.report_stale_verifications import _resolve_stale_url
        url = _resolve_stale_url("http://h:8090/api/tool-events", None)
        assert url == "http://h:8090/api/tool-events/stale"

    def test_verifications_url_construction(self):
        from scripts.maintenance.report_stale_verifications import _resolve_verifications_url
        url = _resolve_verifications_url("http://h:8090/api/tool-events", "uuid-1")
        assert url == "http://h:8090/api/tool-events/stale-verifications?memory_uuid=uuid-1&limit=200"


# ---------------------------------------------------------------------------
# CLI — main() behavior
# ---------------------------------------------------------------------------


def _mock_urlopen_success(data: dict):
    """Return a urllib.response-compatible mock that returns JSON data."""
    import io
    body = json.dumps(data).encode("utf-8")

    class FakeResponse(io.BytesIO):
        def __init__(self):
            super().__init__(body)
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return FakeResponse()


class TestCLI:
    def test_network_failure_exits_nonzero(self, monkeypatch):
        """Network/API failure must exit nonzero with useful error on stderr."""
        def _fail(*args, **kwargs):
            raise Exception("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _fail)
        monkeypatch.setattr("sys.argv", [
            "prog", "--project", "test", "--url", "http://127.0.0.1:1",
        ])
        with pytest.raises(SystemExit) as exc:
            from scripts.maintenance.report_stale_verifications import main
            main()
        assert exc.value.code == 1

    def test_json_mode_output_is_parseable(self, monkeypatch, capsys):
        """JSON mode must print parseable JSON with correct classification."""
        stale_response = {"stale_anchors": [ANCHOR_VALID], "count": 1}
        verif_response = {"verifications": [RECEIPT_VALID], "count": 1}

        def _fake_urlopen(req, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/stale-verifications" in url:
                return _mock_urlopen_success(verif_response)
            if "/stale" in url:
                return _mock_urlopen_success(stale_response)
            return _mock_urlopen_success({})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("sys.argv", [
            "prog", "--project", "test-project", "--url", "http://127.0.0.1:9999",
            "--json",
        ])

        from scripts.maintenance.report_stale_verifications import main
        rc = main()
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["counts"]["with_valid_receipt"] == 1
        assert data["items"][0]["status"] == "valid_still_valid"

    def test_json_output_only_to_stdout(self, monkeypatch, capsys):
        """JSON mode must send JSON to stdout, errors to stderr."""
        def _fail(*args, **kwargs):
            raise Exception("timeout")

        monkeypatch.setattr("urllib.request.urlopen", _fail)
        monkeypatch.setattr("sys.argv", [
            "prog", "--project", "test", "--url", "http://127.0.0.1:1",
            "--json",
        ])
        with pytest.raises(SystemExit) as exc:
            from scripts.maintenance.report_stale_verifications import main
            main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""  # no JSON on stdout on failure
        assert captured.err != ""  # error on stderr

    def test_no_file_content_urls(self):
        """URLs used by the script must not request file content."""
        from scripts.maintenance.report_stale_verifications import (
            _resolve_stale_url, _resolve_verifications_url,
        )
        base = "http://127.0.0.1:8090/api/tool-events"
        stale_url = _resolve_stale_url(base, "proj")
        verif_url = _resolve_verifications_url(base, "uuid-1")
        assert "content" not in stale_url
        assert "transcript" not in stale_url
        assert "content" not in verif_url
        assert "transcript" not in verif_url

    def test_empty_stale_anchors_still_ok(self, monkeypatch, capsys):
        """When there are no stale anchors, the script should still produce a valid report."""
        def _fake_urlopen(req, **kw):
            return _mock_urlopen_success({"stale_anchors": [], "count": 0})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr("sys.argv", [
            "prog", "--project", "empty-project", "--url", "http://127.0.0.1:9999",
            "--json",
        ])
        from scripts.maintenance.report_stale_verifications import main
        rc = main()
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["counts"]["stale_anchors"] == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_duplicate_memory_uuids_not_fetched_twice(self):
        """Duplicate memory_uuids in stale_anchors should be deduplicated."""
        anchor_a = dict(ANCHOR_VALID)
        anchor_b = dict(ANCHOR_VALID)
        anchor_b["path"] = "src/bar.py"

        items = build_report_items(
            [anchor_a, anchor_b],
            {_M1: [RECEIPT_VALID]},
        )
        assert len(items) == 2
        # Both share the same memory_uuid and project, but different paths
        # Only anchor_a's path matches, so:
        status_a = next(it["status"] for it in items if it["path"] == "src/foo.py")
        status_b = next(it["status"] for it in items if it["path"] == "src/bar.py")
        assert status_a == "valid_still_valid"
        assert status_b == "only_ignored_receipts"

    def test_outdated_outcome_status(self):
        """A valid receipt with 'outdated' outcome should produce valid_outdated status."""
        items = build_report_items(
            [ANCHOR_VALID],
            {_M1: [RECEIPT_OUTDATED]},
        )
        assert items[0]["status"] == "valid_outdated"
        assert items[0]["latest_valid_receipt"]["outcome"] == "outdated"

    def test_needs_review_outcome_status(self):
        """A valid receipt with 'needs_review' outcome should produce valid_unclear status."""
        receipt = dict(RECEIPT_VALID)
        receipt["outcome"] = "needs_review"
        receipt["verified_at"] = "2026-07-09T02:00:28Z"
        items = build_report_items([ANCHOR_VALID], {_M1: [receipt]})
        assert items[0]["status"] == "valid_unclear"

    def test_superseded_outcome_status(self):
        receipt = dict(RECEIPT_VALID)
        receipt["outcome"] = "superseded"
        receipt["verified_at"] = "2026-07-09T02:00:28Z"
        items = build_report_items([ANCHOR_VALID], {_M1: [receipt]})
        assert items[0]["status"] == "valid_unclear"
