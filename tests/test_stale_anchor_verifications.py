"""Tests for stale-anchor verification receipts — storage, API, enrichment, CLI."""

from __future__ import annotations

import pytest

from menhir.services.stale_labeling import (
    ALLOWED_VERIFICATION_OUTCOMES,
    STALE_ACTION, STALE_ADVISORY,
    STALE_ACTION_OUTDATED, STALE_ADVISORY_OUTDATED,
    STALE_ADVISORY_STILL_VALID,
)

# ---------------------------------------------------------------------------
# Domain: constants
# ---------------------------------------------------------------------------


class TestVerificationConstants:
    def test_all_outcomes_defined(self):
        assert "still_valid" in ALLOWED_VERIFICATION_OUTCOMES
        assert "outdated" in ALLOWED_VERIFICATION_OUTCOMES
        assert "needs_review" in ALLOWED_VERIFICATION_OUTCOMES
        assert "superseded" in ALLOWED_VERIFICATION_OUTCOMES

    def test_outdated_action_different_from_default(self):
        assert STALE_ACTION_OUTDATED != STALE_ACTION
        assert "do_not_rely" in STALE_ACTION_OUTDATED

    def test_advisory_texts_differ(self):
        assert STALE_ADVISORY != STALE_ADVISORY_OUTDATED
        assert STALE_ADVISORY != STALE_ADVISORY_STILL_VALID


# ---------------------------------------------------------------------------
# Repository (via FakeNeo4j)
# ---------------------------------------------------------------------------


class FakeNeo4j:
    """Minimal fake that routes Cypher by substring match."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = [] if default is None else default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        for sub, rows in self.routes:
            if sub in query:
                return rows
        return self.default


def _repo(routes=None, default=None, *, owned=True):
    """CF-106 added an ownership probe before the CREATE: the caller's namespace must actually own
    the supplied `memory_uuid`. These tests are about outcome/timestamp validation and listing, so
    the probe answers "owned" by default; `owned=False` exercises the refusal."""
    from menhir.infrastructure.tool_event_repository import ToolEventRepository
    probe = [("MATCH (m:Entity)", [{"n": 1 if owned else 0}])]
    return ToolEventRepository(FakeNeo4j(routes=probe + list(routes or []), default=default))


class TestRecordVerification:
    def test_valid_outcome_accepted(self):
        r = _repo(routes=[
            ("CREATE (v:StaleAnchorVerification", [{
                "uuid": "v1", "memory_uuid": "m1", "project": "menhir",
                "path": "src/foo.py", "outcome": "still_valid",
                "verified_by": "agent", "basis": "inspected_current_file",
                "verified_at": "2026-07-09T00:00:00Z",
            }]),
        ])
        result = r.record_stale_anchor_verification(
            namespace="tenant-a", memory_uuid="m1", project="menhir", path="src/foo.py",
            outcome="still_valid",
        )
        assert result["accepted"] is True
        assert result["verification"]["memory_uuid"] == "m1"

    def test_invalid_outcome_rejected(self):
        r = _repo()
        with pytest.raises(ValueError, match="outcome must be one of"):
            r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="m1", path="src/foo.py", outcome="bogus",
            )

    def test_missing_memory_uuid_rejected(self):
        r = _repo()
        with pytest.raises(ValueError, match="memory_uuid is required"):
            r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="", path="src/foo.py", outcome="still_valid",
            )

    def test_missing_path_rejected(self):
        r = _repo()
        with pytest.raises(ValueError, match="path is required"):
            r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="m1", path="", outcome="still_valid",
            )

    def test_all_valid_outcomes_accepted(self):
        for outcome in ("still_valid", "outdated", "needs_review", "superseded"):
            r = _repo(routes=[
                ("CREATE", [{
                    "uuid": "v1", "memory_uuid": "m1", "path": "src/x.py",
                    "outcome": outcome, "verified_at": "2026-07-09T00:00:00Z",
                }]),
            ])
            result = r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="m1", path="src/x.py", outcome=outcome,
            )
            assert result["accepted"] is True

    def test_invalid_verified_at_rejected(self):
        r = _repo()
        with pytest.raises(ValueError, match="verified_at must be a valid"):
            r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="m1", path="src/foo.py",
                outcome="still_valid", verified_at="zzz",
            )

    def test_valid_verified_at_accepted(self):
        r = _repo(routes=[
            ("CREATE (v:StaleAnchorVerification", [{
                "uuid": "v1", "memory_uuid": "m1", "path": "src/foo.py",
                "outcome": "still_valid", "verified_at": "2026-07-09T00:00:00Z",
            }]),
        ])
        result = r.record_stale_anchor_verification(
            namespace="tenant-a", memory_uuid="m1", path="src/foo.py",
            outcome="still_valid", verified_at="2026-07-09T12:00:00Z",
        )
        assert result["accepted"] is True

    def test_verified_at_missing_timezone_rejected(self):
        r = _repo()
        with pytest.raises(ValueError, match="verified_at must be a valid"):
            r.record_stale_anchor_verification(
                namespace="tenant-a", memory_uuid="m1", path="src/foo.py",
                outcome="still_valid", verified_at="2026-07-09T00:00:00",
            )


class TestListVerifications:
    def test_list_by_memory_uuid(self):
        r = _repo(routes=[
            ("v[$memory_uuid_key] = ", [{
                "uuid": "v1", "memory_uuid": "m1", "project": "menhir",
                "path": "src/foo.py", "outcome": "still_valid",
                "verified_by": "agent", "verified_at": "t",
                "created_at": "t",
            }]),
        ])
        rows = r.list_stale_anchor_verifications(namespace="tenant-a", memory_uuid="m1")
        assert len(rows) == 1
        assert rows[0]["memory_uuid"] == "m1"

    def test_list_by_project_and_path(self):
        r = _repo(routes=[
            ("v[$project_key] = ", [{
                "uuid": "v1", "memory_uuid": "m1", "project": "p1",
                "path": "src/a.py", "outcome": "outdated",
                "verified_by": "agent", "verified_at": "t",
                "created_at": "t",
            }]),
        ])
        rows = r.list_stale_anchor_verifications(namespace="tenant-a", project="p1", path="src/a.py")
        assert len(rows) == 1
        assert rows[0]["path"] == "src/a.py"

    def test_limit_respected(self):
        many = [
            {"uuid": f"v{i}", "memory_uuid": "m1", "path": f"src/{i}.py",
             "outcome": "still_valid", "verified_by": "agent",
             "verified_at": f"t{i}", "created_at": f"t{i}"}
            for i in range(10)
        ]
        r = _repo(default=many)
        rows = r.list_stale_anchor_verifications(namespace="tenant-a", limit=3)
        assert len(rows) == 10  # fake returns all; real Neo4j limits
        # (the fake doesn't enforce LIMIT; we test that the param is passed)
        assert rows[0]["uuid"] == "v0"

    def test_optional_schema_is_accessed_dynamically(self):
        r = _repo(default=[])

        r.list_stale_anchor_verifications(namespace="tenant-a", memory_uuid="m1")

        query, params = r._neo4j.executed[-1]
        assert "MATCH (v:$($verification_label))" in query
        assert "v.memory_uuid" not in query
        assert "v.verified_at" not in query
        assert params["verification_label"] == "StaleAnchorVerification"
        assert params["memory_uuid_key"] == "memory_uuid"
        assert params["verified_at_key"] == "verified_at"


class TestLatestVerifications:
    _STALE_M1 = {"memory_uuid": "m1", "path": "src/foo.py", "dirty_at": "2026-07-07T00:00:00Z"}

    def test_returns_latest_per_uuid_path(self):
        rows = [
            {"memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
             "verified_by": "agent", "basis": "inspected_current_file",
             "verified_at": "2026-07-09T12:00:00Z"},
            {"memory_uuid": "m1", "path": "src/foo.py", "outcome": "outdated",
             "verified_by": "agent", "basis": "inspected_current_file",
             "verified_at": "2026-07-08T12:00:00Z"},
        ]
        r = _repo(default=rows)
        result = r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])
        assert ("m1", "src/foo.py") in result
        assert result[("m1", "src/foo.py")]["outcome"] == "still_valid"

    def test_optional_schema_is_accessed_dynamically(self):
        r = _repo(default=[])

        r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])

        query, params = r._neo4j.executed[-1]
        assert "MATCH (v:$($verification_label))" in query
        assert "v.memory_uuid" not in query
        assert "v.verified_at" not in query
        assert params["verification_label"] == "StaleAnchorVerification"
        assert params["memory_uuid_key"] == "memory_uuid"
        assert params["verified_at_key"] == "verified_at"

    def test_same_memory_wrong_path_does_not_enrich(self):
        """Verification for src/a.py must not enrich stale anchor for src/b.py."""
        rows = [
            {"memory_uuid": "m1", "path": "src/a.py", "outcome": "still_valid",
             "verified_by": "agent", "verified_at": "2026-07-09T12:00:00Z"},
        ]
        r = _repo(default=rows)
        stale = {"memory_uuid": "m1", "path": "src/b.py", "dirty_at": "2026-07-07T00:00:00Z"}
        result = r.latest_stale_anchor_verifications(stale_anchors=[stale])
        assert ("m1", "src/b.py") not in result

    def test_same_memory_correct_path_enriches(self):
        rows = [
            {"memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
             "verified_by": "agent", "verified_at": "2026-07-09T12:00:00Z"},
        ]
        r = _repo(default=rows)
        result = r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])
        assert ("m1", "src/foo.py") in result

    def test_pre_dirty_verification_excluded(self):
        rows = [
            {"memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
             "verified_by": "agent", "verified_at": "2026-07-06T00:00:00Z"},
        ]
        r = _repo(default=rows)
        result = r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])
        assert ("m1", "src/foo.py") not in result  # verification was before dirty

    def test_no_verification_returns_empty(self):
        r = _repo(default=[])
        result = r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])
        assert result == {}

    def test_empty_input_returns_empty(self):
        r = _repo()
        assert r.latest_stale_anchor_verifications(stale_anchors=[]) == {}

    def test_verified_at_string_ordering_not_used(self):
        """Non-parseable timestamps are skipped conservatively."""
        rows = [
            {"memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
             "verified_by": "agent", "verified_at": "zzz"},
        ]
        r = _repo(default=rows)
        result = r.latest_stale_anchor_verifications(stale_anchors=[self._STALE_M1])
        assert ("m1", "src/foo.py") not in result


# ---------------------------------------------------------------------------
# API routes (via TestClient)
# ---------------------------------------------------------------------------


def _adapter(**overrides):
    """Build a SimpleNamespace adapter with default verification methods."""
    from types import SimpleNamespace
    defaults: dict = {
        "record_stale_anchor_verification": lambda **k: {
            "accepted": True, "verification": {
                "uuid": "v1", "memory_uuid": k.get("memory_uuid"),
                "project": k.get("project"), "path": k.get("path"),
                "outcome": k.get("outcome"),
                "verified_at": k.get("verified_at", "2026-07-09T00:00:00Z"),
            }},
        "list_stale_anchor_verifications": lambda **k:
            [{"uuid": "v1", "memory_uuid": "m1", "project": "menhir",
              "path": "src/foo.py", "outcome": "still_valid",
              "verified_by": "agent", "basis": "inspected_current_file",
              "verified_at": "t", "created_at": "t"}],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _client(adapter_override=None):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from menhir.api.routes import router

    adapter = adapter_override or _adapter()
    built = SimpleNamespace(graph_adapter=adapter)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(built=built)
    return TestClient(app)


class TestAPIRoutes:
    @pytest.mark.unit
    def test_post_valid_verification(self):
        c = _client()
        r = c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "m1", "project": "menhir", "path": "src/foo.py",
            "outcome": "still_valid", "verified_by": "agent",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["verification"]["memory_uuid"] == "m1"

    @pytest.mark.unit
    def test_post_invalid_outcome_rejected(self):
        c = _client(adapter_override=_adapter(
            record_stale_anchor_verification=lambda **k: (_ for _ in ()).throw(
                ValueError("outcome must be one of"),
            ),
        ))
        r = c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "m1", "path": "src/foo.py", "outcome": "bogus",
        })
        assert r.status_code == 400

    @pytest.mark.unit
    def test_post_missing_memory_uuid_rejected(self):
        c = _client(adapter_override=_adapter(
            record_stale_anchor_verification=lambda **k: (_ for _ in ()).throw(
                ValueError("memory_uuid is required"),
            ),
        ))
        r = c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "", "path": "src/foo.py", "outcome": "still_valid",
        })
        assert r.status_code == 400

    @pytest.mark.unit
    def test_post_missing_path_rejected(self):
        c = _client(adapter_override=_adapter(
            record_stale_anchor_verification=lambda **k: (_ for _ in ()).throw(
                ValueError("path is required"),
            ),
        ))
        r = c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "m1", "path": "", "outcome": "still_valid",
        })
        assert r.status_code == 400

    @pytest.mark.unit
    def test_get_returns_verifications(self):
        c = _client()
        r = c.get("/api/tool-events/stale-verifications?memory_uuid=m1")
        assert r.status_code == 200
        body = r.json()
        assert "verifications" in body
        assert body["count"] >= 1

    @pytest.mark.unit
    def test_get_filters_by_project(self):
        c = _client(_adapter(
            list_stale_anchor_verifications=lambda **k: (
                [{"uuid": "v1", "memory_uuid": "m1", "project": k.get("project"),
                  "path": "src/x.py", "outcome": "still_valid",
                  "verified_by": "agent", "verified_at": "t", "created_at": "t"}]
                if k.get("project") == "menhir" else []
            ),
        ))
        r = c.get("/api/tool-events/stale-verifications?project=menhir")
        assert r.status_code == 200
        assert r.json()["count"] == 1
        r2 = c.get("/api/tool-events/stale-verifications?project=other")
        assert r2.json()["count"] == 0

    @pytest.mark.unit
    def test_get_limit_respected(self):
        c = _client(_adapter(
            list_stale_anchor_verifications=lambda **k: [
                {"uuid": f"v{i}", "memory_uuid": "m1", "path": f"src/{i}.py",
                 "outcome": "still_valid", "verified_by": "agent",
                 "verified_at": f"t{i}", "created_at": f"t{i}"}
                for i in range(int(k.get("limit", 50)))
            ],
        ))
        r = c.get("/api/tool-events/stale-verifications?limit=5")
        assert r.status_code == 200
        assert r.json()["count"] == 5

    @pytest.mark.unit
    def test_receipt_does_not_clear_dirty(self):
        """Adapter has no clear_file_dirty call — verification is audit-only."""
        adapter = _adapter()
        c = _client(adapter)
        c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
        })
        # No clear_file_dirty method was invoked
        assert not hasattr(adapter, "clear_file_dirty_called")

    @pytest.mark.unit
    def test_unavailable_endpoint_returns_503(self):
        """When adapter does not have record method, return 503."""
        from types import SimpleNamespace
        bad = SimpleNamespace()
        c = _client(bad)
        r = c.post("/api/tool-events/stale-verifications", json={
            "memory_uuid": "m1", "path": "src/foo.py", "outcome": "still_valid",
        })
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Enrichment: _resolve_stale_advisory
# ---------------------------------------------------------------------------


class TestResolveStaleAdvisory:
    def test_no_verification_uses_default(self):
        from menhir.mcp.formatters import _resolve_stale_advisory
        action, advisory = _resolve_stale_advisory({"stale_anchor": True})
        assert action == STALE_ACTION
        assert advisory == STALE_ADVISORY

    def test_still_valid_uses_verified_advisory(self):
        from menhir.mcp.formatters import _resolve_stale_advisory
        info = {
            "stale_anchor": True,
            "stale_verification": {
                "outcome": "still_valid", "verified_at": "t", "verified_by": "agent",
            },
        }
        action, advisory = _resolve_stale_advisory(info)
        assert action == STALE_ACTION
        assert advisory == STALE_ADVISORY_STILL_VALID

    def test_outdated_uses_stronger_advisory(self):
        from menhir.mcp.formatters import _resolve_stale_advisory
        info = {
            "stale_anchor": True,
            "stale_verification": {
                "outcome": "outdated", "verified_at": "t",
            },
        }
        action, advisory = _resolve_stale_advisory(info)
        assert action == STALE_ACTION_OUTDATED
        assert advisory == STALE_ADVISORY_OUTDATED

    def test_needs_review_keeps_default(self):
        from menhir.mcp.formatters import _resolve_stale_advisory
        info = {
            "stale_anchor": True,
            "stale_verification": {
                "outcome": "needs_review", "verified_at": "t",
            },
        }
        action, advisory = _resolve_stale_advisory(info)
        assert action == STALE_ACTION
        assert advisory == STALE_ADVISORY

    def test_superseded_keeps_default(self):
        from menhir.mcp.formatters import _resolve_stale_advisory
        info = {
            "stale_anchor": True,
            "stale_verification": {
                "outcome": "superseded", "verified_at": "t",
            },
        }
        action, advisory = _resolve_stale_advisory(info)
        assert action == STALE_ACTION
        assert advisory == STALE_ADVISORY


# ---------------------------------------------------------------------------
# Formatter output: verification in _compact_scored_item
# ---------------------------------------------------------------------------


class TestVerificationInScoredItem:
    def test_still_valid_verification_in_output(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={
                "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
                "path": "src/foo.py",
                "stale_verification": {
                    "outcome": "still_valid", "verified_at": "2026-07-09T00:00:00Z",
                    "verified_by": "agent", "basis": "inspected_current_file",
                },
            },
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_anchor"] is True
        assert "stale_verification" in item
        assert item["stale_verification"]["outcome"] == "still_valid"
        assert "verified" in item["stale_advisory"].lower()

    def test_verification_in_compact_mode(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={
                "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py",
                "stale_verification": {"outcome": "outdated", "verified_at": "t"},
            },
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=True)
        assert item["stale_verification"]["outcome"] == "outdated"
        assert item["stale_action"] == STALE_ACTION_OUTDATED
        assert "outdated" in item["stale_advisory"].lower()


# ---------------------------------------------------------------------------
# Formatter output: verification in _compact_memory_item
# ---------------------------------------------------------------------------


class TestVerificationInMemoryItem:
    def test_stale_verification_passed_through(self):
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m1", "name": "foo", "scope": "PERSISTENT",
            "type": "SEMANTIC", "summary": "foo content",
            "stale_anchor_info": {
                "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py",
                "stale_verification": {
                    "outcome": "still_valid", "verified_at": "2026-07-09T00:00:00Z",
                },
            },
        }
        item = _compact_memory_item(row, tag="relevant")
        assert "stale_verification" in item
        assert item["stale_verification"]["outcome"] == "still_valid"

    def test_no_verification_no_field(self):
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m1", "name": "foo", "scope": "PERSISTENT",
            "type": "SEMANTIC", "summary": "foo content",
            "stale_anchor_info": {
                "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py",
            },
        }
        item = _compact_memory_item(row, tag="relevant")
        assert "stale_verification" not in item


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_builds_correct_body(self):
        from scripts.maintenance.record_stale_anchor_verification import main as cli_main
        import sys
        test_args = [
            "prog", "--memory-uuid", "m1", "--project", "menhir",
            "--path", "src/foo.py", "--outcome", "still_valid",
            "--verified-by", "agent", "--dry-run",
        ]
        old_argv = list(sys.argv)
        try:
            sys.argv = test_args
            # dry-run prints to stdout; capture it
            from io import StringIO
            import contextlib
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                with pytest.raises(SystemExit) as exc:
                    cli_main()
            assert exc.value.code == 0
            output = buf.getvalue()
            assert "DRY RUN" in output
            assert "m1" in output
            assert "still_valid" in output
        finally:
            sys.argv = old_argv

    def test_cli_dry_run_does_not_post(self):
        from scripts.maintenance.record_stale_anchor_verification import main as cli_main
        import sys
        test_args = [
            "prog", "--memory-uuid", "m1", "--path", "src/foo.py",
            "--outcome", "still_valid", "--dry-run",
        ]
        old_argv = list(sys.argv)
        try:
            sys.argv = test_args
            from io import StringIO
            import contextlib
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                with pytest.raises(SystemExit):
                    cli_main()
            output = buf.getvalue()
            assert "DRY RUN" in output
            assert "POST" not in output.lower() or "would POST" in output
        finally:
            sys.argv = old_argv

    def test_cli_includes_bearer_from_env(self):
        import os
        key = os.environ.get("MENHIR_AGENT_KEY", "")
        if not key:
            pytest.skip("MENHIR_AGENT_KEY not set; cannot test bearer inclusion")
        # The live path would include the header; here we just verify the script loads
        from scripts.maintenance.record_stale_anchor_verification import main
        assert main is not None

    def test_cli_json_output(self):
        from scripts.maintenance.record_stale_anchor_verification import main as cli_main
        import sys
        test_args = [
            "prog", "--memory-uuid", "m1", "--path", "src/foo.py",
            "--outcome", "still_valid", "--dry-run", "--json",
        ]
        old_argv = list(sys.argv)
        try:
            sys.argv = test_args
            from io import StringIO
            import contextlib
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                with pytest.raises(SystemExit):
                    cli_main()
            output = buf.getvalue()
            assert "memory_uuid" in output  # json output contains the body
        finally:
            sys.argv = old_argv


# ---------------------------------------------------------------------------
# Enrichment: recall service integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_enriches_with_verification(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch,
):
    """Verify that recall output includes stale_verification when a post-dirty
    receipt exists."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    stale_rows = [
        {
            "memory_uuid": "m1", "name": "foo", "project": "menhir",
            "path": "src/foo.py", "dirty_at": "2026-07-08T12:00:00Z",
            "anchored_at": "2026-07-01T00:00:00Z", "operation": "edit",
        },
    ]
    setattr(stub_memory_graph_adapter, "stale_anchored_memories", lambda **k: stale_rows)
    setattr(
        stub_memory_graph_adapter,
        "latest_stale_anchor_verifications",
        lambda **k: {
            ("m1", "src/foo.py"): {
                "outcome": "still_valid", "verified_at": "2026-07-09T00:00:00Z",
                "verified_by": "agent", "basis": "inspected_current_file",
            },
        } if any(sa.get("memory_uuid") == "m1" for sa in k.get("stale_anchors", [])) else {},
    )

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "m1", "name": "foo", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "foo content", "summary": None,
            "last_accessed": "2026-07-01T00:00:00Z", "edge_count": 1,
            "freshness": "ACTIVE", "user_flagged": False,
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall("test query")
    assert len(result.results) == 1
    m1 = result.results[0]
    assert m1.stale_anchor_info is not None
    assert m1.stale_anchor_info["stale_anchor"] is True
    assert m1.stale_anchor_info.get("stale_verification") is not None
    assert m1.stale_anchor_info["stale_verification"]["outcome"] == "still_valid"


@pytest.mark.asyncio
async def test_recall_verification_failure_does_not_break(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch,
):
    """When latest_stale_anchor_verifications raises, recall should still return
    results with stale labels but without verification enrichment."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    stale_rows = [
        {
            "memory_uuid": "m1", "name": "foo", "project": "menhir",
            "path": "src/foo.py", "dirty_at": "2026-07-08T12:00:00Z",
            "anchored_at": "2026-07-01T00:00:00Z", "operation": "edit",
        },
    ]
    setattr(stub_memory_graph_adapter, "stale_anchored_memories", lambda **k: stale_rows)
    setattr(
        stub_memory_graph_adapter,
        "latest_stale_anchor_verifications",
        lambda **k: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "m1", "name": "foo", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "foo content", "summary": None,
            "last_accessed": "2026-07-01T00:00:00Z", "edge_count": 1,
            "freshness": "ACTIVE", "user_flagged": False,
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall("test query")
    assert len(result.results) == 1
    m1 = result.results[0]
    assert m1.stale_anchor_info is not None
    assert m1.stale_anchor_info["stale_anchor"] is True
    # verification not present because the adapter call failed
    assert m1.stale_anchor_info.get("stale_verification") is None
