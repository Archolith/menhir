"""Tests for the bench-run explorer (bench_runs.py)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from menhir.explorer.app import create_app
from menhir.explorer.bench_runs import (
    CONTRACT_VERSION,
    BenchRunCatalog,
    BenchRunTaskReader,
    _read_checkpoint_scores,
    _parse_checkpoint_record,
    _discover_run_dirs,
    _is_safe_component,
    _safe_resolve,
    _safe_int,
    _redact_nested,
    default_bench_run_provider,
    _CACHED_CATALOG,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(namespace="lme-item-001", question_id="001", question="What is the capital?", answer="Paris", question_type="single-session-user", ready=True, **kw):
    row = {"namespace": namespace, "question_id": question_id, "question": question, "answer": answer, "question_type": question_type, "turns": 3, "typed_assertions": 2, "scalar_views": 1, "turn_evidence": 2, "scalar_llm_calls": 5, "scalar_consolidated": True, "ready": ready, "failed_remaining": 0}
    row.update(kw)
    return row


def _provenance(**kw):
    data = {"identity": {"variant": "oracle", "arm": "baseline", "extract_model": "gpt-4o-mini", "menhir_commit": "abc123def456", "bench_commit": "789012345678", "started_at": "2026-07-28T10:00:00Z"}, "attempt_count": 1}
    data.update(kw)
    return data


def _configure_reused_graph_run(
    bench_root: Path,
    *,
    run_id: str = "recall-linked",
    source_run_id: str = "lme-ingest",
    recall_overrides: dict[str, object] | None = None,
) -> Path:
    graph_identity = {
        "container": "menhir-lme-test",
        "volume": "menhir-lme-test-data",
        "fixture_sha256": "a" * 64,
        "fixture_count": 2,
        "namespace_prefix": "lme-",
    }
    source_dir = bench_root / source_run_id
    source_provenance = _provenance()
    source_provenance["identity"].update(graph_identity)
    (source_dir / "run_provenance.json").write_text(
        json.dumps(source_provenance),
        encoding="utf-8",
    )

    recall_dir = bench_root / run_id
    recall_dir.mkdir()
    (recall_dir / "manifest.json").write_text(
        (source_dir / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    recall_provenance = _provenance(
        source_run_id=source_run_id,
        source_graph_reused=True,
        reingested=False,
    )
    recall_provenance["identity"].update(graph_identity)
    for key, value in (recall_overrides or {}).items():
        if key in graph_identity:
            recall_provenance["identity"][key] = value
        else:
            recall_provenance[key] = value
    (recall_dir / "run_provenance.json").write_text(
        json.dumps(recall_provenance),
        encoding="utf-8",
    )
    return recall_dir


_can_symlink = sys.platform != "win32"


@pytest.fixture
def bench_root(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    run1 = root / "lme-ingest"
    run1.mkdir()
    (run1 / "manifest.json").write_text(json.dumps([_row(), _row(namespace="lme-item-002", question_id="002", question="When did it happen?", answer="2023", question_type="temporal-reasoning", ready=False)]), encoding="utf-8")
    (run1 / "run_provenance.json").write_text(json.dumps(_provenance()), encoding="utf-8")
    cp = [json.dumps({"arm": "menhir_recall", "result": {"task_id": "lme-item-001", "correct": True}}), json.dumps({"arm": "no_memory", "result": {"task_id": "lme-item-001", "correct": False}})]
    (run1 / ".checkpoint_longmemeval-menhir_oracle_gpt-4o.jsonl").write_text("\n".join(cp), encoding="utf-8")
    hrecall = run1 / "harness_recall"
    hrecall.mkdir()
    (hrecall / ".checkpoint_nested.jsonl").write_text("\n".join([json.dumps({"arm": "test_arm", "result": {"task_id": "lme-item-002", "correct": True}})]), encoding="utf-8")
    group = root / "lme-ku-buildout"
    group.mkdir()
    run2 = group / "ku-run-001"
    run2.mkdir()
    (run2 / "manifest.json").write_text(json.dumps([_row(namespace="lme-item-003", question_id="003", question="User name?", answer="Alice", question_type="knowledge-update")]), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_safe_component_allowed(self):
        assert _is_safe_component("lme-ingest")

    def test_safe_component_rejected(self):
        assert not _is_safe_component("../escape")

    def test_safe_resolve_accepts(self, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        sub = root / "safe"; sub.mkdir()
        assert _safe_resolve(root.resolve(), "safe") == sub.resolve()

    def test_safe_resolve_rejects_escape(self, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        assert _safe_resolve(root.resolve(), "..") is None

    def test_safe_resolve_nonexistent(self, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        assert _safe_resolve(root.resolve(), "nonexistent") is None

    @pytest.mark.skipif(not _can_symlink, reason="symlink not available on Windows")
    def test_safe_resolve_rejects_symlink_inside(self, tmp_path):
        root = tmp_path / "root"; root.mkdir()
        real = tmp_path / "real"; real.mkdir()
        lnk = root / "link"
        lnk.symlink_to(real, target_is_directory=True)
        assert _safe_resolve(root.resolve(), "link") is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_pattern1(self, bench_root):
        assert "lme-ingest" in _discover_run_dirs(bench_root)

    def test_pattern2(self, bench_root):
        assert "ku-run-001" in _discover_run_dirs(bench_root)

    def test_empty_root(self, tmp_path):
        assert _discover_run_dirs(tmp_path) == {}

    def test_duplicate_excluded(self, bench_root):
        dup = bench_root / "other-group" / "ku-run-001"
        dup.mkdir(parents=True)
        (dup / "manifest.json").write_text(json.dumps([_row()]), encoding="utf-8")
        assert "ku-run-001" not in _discover_run_dirs(bench_root)

    @pytest.mark.skipif(not _can_symlink, reason="symlink not available on Windows")
    def test_symlink_skipped(self, bench_root, tmp_path):
        outside = tmp_path / "outside"; outside.mkdir()
        (outside / "manifest.json").write_text(json.dumps([_row()]), encoding="utf-8")
        lnk = bench_root / "evil-link"
        lnk.symlink_to(outside, target_is_directory=True)
        assert "evil-link" not in _discover_run_dirs(bench_root)

    def test_missing_root(self):
        assert _discover_run_dirs(Path("/nonexistent_path_xyz")) == {}


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_parse_nested(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": True}})
        assert p is not None and p["correct"] is True

    def test_parse_flattened(self):
        p = _parse_checkpoint_record({"arm": "t", "task_id": "lme-2", "correct": False})
        assert p is not None and p["correct"] is False

    def test_score_absent_correct(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": True}})
        assert p is not None and p["score"] == 1.0

    def test_score_absent_incorrect(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": False}})
        assert p is not None and p["score"] == 0.0

    def test_null_tokens_ok(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": True, "input_tokens": None, "output_tokens": None}})
        assert p is not None and p["input_tokens"] == 0 and p["output_tokens"] == 0

    def test_malformed_tokens_ok(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": True, "input_tokens": "nan", "output_tokens": float("inf")}})
        assert p is not None and p["input_tokens"] == 0 and p["output_tokens"] == 0

    def test_score_present(self):
        p = _parse_checkpoint_record({"arm": "t", "result": {"task_id": "lme-1", "correct": True, "score": 0.75}})
        assert p is not None and p["score"] == 0.75

    def test_read_jsonl(self, bench_root):
        s = _read_checkpoint_scores(bench_root / "lme-ingest" / ".checkpoint_longmemeval-menhir_oracle_gpt-4o.jsonl")
        assert "lme-item-001" in s and len(s["lme-item-001"]) == 2

    def test_read_nested(self, bench_root):
        s = _read_checkpoint_scores(bench_root / "lme-ingest" / "harness_recall" / ".checkpoint_nested.jsonl")
        assert "lme-item-002" in s

    def test_read_symlink_rejected(self, bench_root, tmp_path):
        outside = tmp_path / "outside_cp.jsonl"
        outside.write_text("{}", encoding="utf-8")
        p = bench_root / "lme-ingest" / "checkpoint_link.jsonl"
        if _can_symlink:
            p.symlink_to(outside)
        assert _read_checkpoint_scores(p) == {}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_list(self, bench_root):
        runs = BenchRunCatalog(results_root=str(bench_root)).list_runs()
        assert len(runs) == 2

    def test_get(self, bench_root):
        run = BenchRunCatalog(results_root=str(bench_root)).get_run("lme-ingest")
        assert run is not None and run.run_id == "lme-ingest"

    def test_not_found(self, bench_root):
        assert BenchRunCatalog(results_root=str(bench_root)).get_run("nope") is None

    def test_unsafe_id_rejected(self, bench_root):
        assert BenchRunCatalog(results_root=str(bench_root)).get_run("../escape") is None

    def test_two_level(self, bench_root):
        run = BenchRunCatalog(results_root=str(bench_root)).get_run("ku-run-001")
        assert run is not None and run.run_id == "ku-run-001"

    def test_active(self, bench_root):
        run = BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest").get_run("lme-ingest")
        assert run is not None and run.is_active is True

    def test_non_active(self, bench_root):
        run = BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest").get_run("ku-run-001")
        assert run is not None and run.is_active is False

    def test_active_warning(self, bench_root):
        run = BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest").get_run("lme-ingest")
        assert run is not None and run.source_warning == "ACTIVE CONFIGURED"

    def test_reused_active_graph_requires_matching_identity(self, bench_root):
        _configure_reused_graph_run(bench_root)
        run = BenchRunCatalog(
            results_root=str(bench_root),
            active_run_id="lme-ingest",
        ).get_run("recall-linked")
        assert run is not None
        assert run.is_active is False
        assert run.graph_source_run_id == "lme-ingest"
        assert run.live_graph_source_run_id == "lme-ingest"
        assert run.source_warning.startswith("REUSED ACTIVE GRAPH:")

    @pytest.mark.parametrize(
        ("field_name", "wrong_value"),
        [
            ("container", "other-container"),
            ("volume", "other-volume"),
            ("fixture_sha256", "b" * 64),
            ("fixture_count", 1),
            ("namespace_prefix", "other-"),
        ],
    )
    def test_reused_active_graph_fails_closed_on_identity_mismatch(
        self,
        bench_root,
        field_name,
        wrong_value,
    ):
        _configure_reused_graph_run(
            bench_root,
            recall_overrides={field_name: wrong_value},
        )
        run = BenchRunCatalog(
            results_root=str(bench_root),
            active_run_id="lme-ingest",
        ).get_run("recall-linked")
        assert run is not None
        assert run.live_graph_source_run_id is None
        assert run.source_warning == "ARTIFACT-ONLY"

    def test_reused_active_graph_rejects_reingested_run(self, bench_root):
        _configure_reused_graph_run(
            bench_root,
            recall_overrides={"reingested": True},
        )
        run = BenchRunCatalog(
            results_root=str(bench_root),
            active_run_id="lme-ingest",
        ).get_run("recall-linked")
        assert run is not None
        assert run.live_graph_source_run_id is None

    def test_unconfigured(self):
        c = BenchRunCatalog(results_root=None)
        assert c.list_runs() == [] and not c.is_configured

    @pytest.mark.skipif(not _can_symlink, reason="symlink not available on Windows")
    def test_root_symlink_unconfigured(self, tmp_path):
        real = tmp_path / "r"; real.mkdir()
        (real / "manifest.json").write_text("[]", encoding="utf-8")
        lnk = tmp_path / "link"; lnk.symlink_to(real, target_is_directory=True)
        c = BenchRunCatalog(results_root=str(lnk))
        assert not c.is_configured and c.list_runs() == []


# ---------------------------------------------------------------------------
# Task reader — artifact mode
# ---------------------------------------------------------------------------


class TestTaskReaderArtifact:
    def test_get(self, bench_root):
        d = BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root))).get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["question"] == "What is the capital?" and d["contract"] == CONTRACT_VERSION

    def test_not_found(self, bench_root):
        r = BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root)))
        assert r.get_task_detail("lme-ingest", "nonexistent") is None
        assert r.get_task_detail("nonexistent", "lme-1") is None

    def test_unsafe_namespace(self, bench_root):
        assert BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root))).get_task_detail("lme-ingest", "../escape") is None

    def test_active_no_repo(self, bench_root):
        d = BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest")).get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["graph_available"] is False

    def test_artifact_no_graph(self, bench_root):
        d = BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root))).get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["graph_available"] is None

    def test_all_tasks_retained(self, bench_root):
        r = BenchRunTaskReader(catalog=BenchRunCatalog(results_root=str(bench_root)))
        assert r.get_task_detail("lme-ingest", "lme-item-002", reveal=True) is not None


# ---------------------------------------------------------------------------
# Live query with fake repo
# ---------------------------------------------------------------------------


class TestLiveQuery:
    @pytest.fixture
    def catalog(self, bench_root):
        return BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest")

    def _build_repo(
        self, evidence=True, assertions=True, views=True, history=True,
        event_assertions=True, event_views=True, facts=True,
    ):
        repo = MagicMock()
        def _exec(q, params=None):
            ns = (params or {}).get("namespace", "")
            if "TurnEvidence" in q:
                if evidence:
                    return [{"id": "ev-1", "role": "user", "text": "my name is alice", "occurred_at": None, "recorded_at": "now", "founds": ["as-1"]}]
                return []
            if "MATCH (a:TypedEventAssertion" in q:
                if event_assertions:
                    return [{
                        "id": "event-as-1", "assertion_key": "event-key-1",
                        "subject": "Alice", "predicate": "purchased",
                        "domain": "bicycles", "object_key": "road-bike",
                        "object_display": "a road bike", "stated_span": "I bought a road bike",
                        "valid_at": "2026-01-02T00:00:00Z", "learned_at": "now",
                        "time_basis": "source", "evidence_tier": "explicit_user",
                        "binding_pending": False, "superseded": False,
                    }]
                return []
            if "EVENT_HISTORY_ENTRY" in q:
                if event_views:
                    return [{
                        "id": "event-vw-1", "current": True,
                        "subject": "Alice", "predicate": "purchased", "domain": "bicycles",
                        "entry_count": 1, "contributor_ids": ["event-key-1"],
                        "valid_at": "2026-01-02T00:00:00Z",
                        "payload": json.dumps([{
                            "when": "2026-01-02T00:00:00Z", "what": "purchased a road bike",
                            "object_key": "road-bike", "quote": "I bought a road bike",
                        }]),
                    }]
                return []
            if "TypedAssertion" in q and "history_assertion" not in q:
                if assertions:
                    return [{"id": "as-1", "subject": "Alice", "attribute": "name", "value": "Alice", "operation": "absolute", "stated_span": "my name is alice", "binding_pending": False, "superseded": False, "evidence_id": "ev-1"}]
                return []
            if "scalar_state" in q and "scalar_history" not in q:
                if views:
                    return [{"id": "vw-1", "current": True, "contributor_ids": ["as-1"]}]
                return []
            if "scalar_history" in q:
                if history:
                    return [{"id": "hv-1", "current": True, "contributor_ids": ["as-1"], "op_counts": '{"absolute":1}', "view_payload": '[]'}]
                return []
            if "r.fact IS NOT NULL" in q:
                if facts:
                    return [{"subject": "Alice", "relation": "has_name", "object": "Alice", "fact": "user's name is alice"}]
                return []
            return []
        repo.execute.side_effect = _exec
        return repo

    def test_nonempty(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["graph_available"] is True
        assert d["source_warning"] == "LIVE BENCHMARK GRAPH"
        lg = d["live_graph"]
        assert len(lg["evidence"]) == 1
        assert len(lg["assertions"]) == 1
        assert len(lg["views"]) == 1
        assert lg["recall_packet"]["mode"] == "inspection_only"
        assert lg["recall_packet"]["production_recall_changed"] is False

    def test_reused_active_graph_queries_live_source(self, bench_root):
        _configure_reused_graph_run(bench_root)
        catalog = BenchRunCatalog(
            results_root=str(bench_root),
            active_run_id="lme-ingest",
        )
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        detail = reader.get_task_detail("recall-linked", "lme-item-001", reveal=True)
        assert detail is not None
        assert detail["graph_available"] is True
        assert detail["graph_source_run_id"] == "lme-ingest"
        assert detail["historical_score_with_live_graph"] is True
        assert detail["source_warning"].startswith(
            "HISTORICAL SCORE + LIVE SOURCE GRAPH:"
        )
        assert len(detail["live_graph"]["evidence"]) == 1
        assert len(detail["live_graph"]["views"]) == 1

    def test_empty(self, catalog):
        repo = self._build_repo(
            evidence=False, assertions=False, views=False, history=False,
            event_assertions=False, event_views=False, facts=False,
        )
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=lambda: repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["graph_available"] is False
        assert "ACTIVE CONFIGURED" in (d.get("source_warning") or "")

    def test_exception(self, catalog):
        repo = MagicMock()
        repo.execute.side_effect = RuntimeError("Neo4j down")
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=lambda: repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        assert d is not None and d["graph_available"] is False
        assert "RuntimeError" in (d.get("source_warning") or "")

    def test_fold_outcomes(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        a = d["live_graph"]["assertions"][0]
        assert "fold_outcome" in a and "state" in a["fold_outcome"]

    def test_source_quote(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        for a in d["live_graph"]["assertions"]:
            assert "source_quote" in a

    def test_memory_inventory(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        views = [i for i in d["live_graph"]["memory_inventory"] if i["memory_type"] == "view"]
        assert len(views) >= 1

    def test_scalar_roles_surface_all_three_view_types(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        roles = d["live_graph"]["scalar_roles"]
        assert [role["id"] for role in roles] == [
            "scalar-state", "scalar-history", "event-history",
        ]
        assert all(role["status"] == "available" for role in roles)
        assert roles[2]["assertion_count"] == 1

    def test_event_history_projection_keeps_grounding_and_drops_raw_payload(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=True)
        live = d["live_graph"]
        assert live["event_assertions"][0]["stated_span"] == "I bought a road bike"
        assert live["event_views"][0]["entries"][0]["object_key"] == "road-bike"
        assert "payload" not in live["event_views"][0]
        event_inventory = [
            item for item in live["memory_inventory"]
            if item.get("view_kind") == "event_history"
        ]
        assert event_inventory[0]["derivation"] == "event"

    def test_privacy(self, catalog):
        reader = BenchRunTaskReader(catalog=catalog, repo_provider=self._build_repo)
        d = reader.get_task_detail("lme-ingest", "lme-item-001", reveal=False)
        assert d is not None and d["question"] == "[hidden]" and d["answer"] == "[hidden]"
        lg = d.get("live_graph", {})
        for a in lg.get("assertions", []):
            assert a.get("source_quote", "") == "[hidden]" or a.get("source_quote", "") == ""
        for hv in lg.get("history_views", []):
            assert "payload" not in hv
        event_assertion = lg.get("event_assertions", [])[0]
        assert event_assertion["object_display"] == "[hidden]"
        assert event_assertion["stated_span"] == "[hidden]"
        event_entry = lg.get("event_views", [])[0]["entries"][0]
        assert event_entry["what"] == "[hidden]"
        assert event_entry["quote"] == "[hidden]"
        assert lg["recall_packet"]["text"] == "[hidden]"
        for section in lg["recall_packet"]["sections"]:
            for entry in section["entries"]:
                assert entry.get("source_quote") in {None, "", "[hidden]"}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestSafeInt:
    def test_none(self):
        assert _safe_int(None) == 0
    def test_int(self):
        assert _safe_int(42) == 42
    def test_float(self):
        assert _safe_int(3.7) == 3
    def test_nan(self):
        assert _safe_int(float("nan")) == 0
    def test_inf(self):
        assert _safe_int(float("inf")) == 0
    def test_neg_inf(self):
        assert _safe_int(float("-inf")) == 0
    def test_string_int(self):
        assert _safe_int("42") == 42
    def test_malformed_string(self):
        assert _safe_int("not-a-number") == 0
    def test_empty_string(self):
        assert _safe_int("") == 0
    def test_nested_legacy(self, bench_root):
        """Run with null/missing attempt_count does not crash."""
        import json as j
        d = bench_root / "legacy-attempts"
        d.mkdir()
        (d / "manifest.json").write_text(j.dumps([{"namespace":"lme-x","question_id":"x","question":"Q","answer":"A","question_type":"t","turns":None,"typed_assertions":None,"scalar_views":None,"turn_evidence":None,"scalar_llm_calls":None}]), encoding="utf-8")
        (d / "run_provenance.json").write_text(j.dumps({"identity":{"variant":"o"},"attempt_count":None}), encoding="utf-8")
        run = BenchRunCatalog(results_root=str(bench_root)).get_run("legacy-attempts")
        assert run is not None and run.attempts == 0


class TestFilteredTaskIds:
    def test_all_returns_all(self):
        from menhir.explorer.bench_runs import BenchTask, _filtered_task_ids
        tasks = [BenchTask(namespace="a", primary_outcome="PASSED"), BenchTask(namespace="b", primary_outcome="FAILED"), BenchTask(namespace="c", primary_outcome=None)]
        assert _filtered_task_ids(tasks, "all") == ["a", "b", "c"]
        assert _filtered_task_ids(tasks, None) == ["a", "b", "c"]

    def test_passed(self):
        from menhir.explorer.bench_runs import BenchTask, _filtered_task_ids
        tasks = [BenchTask(namespace="a", primary_outcome="PASSED"), BenchTask(namespace="b", primary_outcome="FAILED")]
        assert _filtered_task_ids(tasks, "passed") == ["a"]

    def test_failed(self):
        from menhir.explorer.bench_runs import BenchTask, _filtered_task_ids
        tasks = [BenchTask(namespace="a", primary_outcome="PASSED"), BenchTask(namespace="b", primary_outcome="FAILED")]
        assert _filtered_task_ids(tasks, "failed") == ["b"]

    def test_unscored(self):
        from menhir.explorer.bench_runs import BenchTask, _filtered_task_ids
        tasks = [BenchTask(namespace="a", primary_outcome="PASSED"), BenchTask(namespace="b", primary_outcome=None), BenchTask(namespace="c", primary_outcome="NOT SCORED")]
        assert _filtered_task_ids(tasks, "unscored") == ["b", "c"]


class TestNormalizeOutcome:
    def test_valid_values(self):
        from menhir.explorer.bench_runs import _normalize_outcome
        assert _normalize_outcome("all") == "all"
        assert _normalize_outcome("failed") == "failed"
        assert _normalize_outcome("passed") == "passed"
        assert _normalize_outcome("unscored") == "unscored"

    def test_invalid_defaults_to_all(self):
        from menhir.explorer.bench_runs import _normalize_outcome
        assert _normalize_outcome("invalid") == "all"
        assert _normalize_outcome("") == "all"
        assert _normalize_outcome(None) == "all"

class TestOutcomeFromPrimary:
    def test_mapping(self):
        from menhir.explorer.bench_runs import _outcome_from_primary
        assert _outcome_from_primary("PASSED") == "passed"
        assert _outcome_from_primary("FAILED") == "failed"
        assert _outcome_from_primary("NOT SCORED") == "unscored"
        assert _outcome_from_primary(None) == "unscored"
        assert _outcome_from_primary("") == "unscored"


class TestNavNeighbors:
    def test_middle(self):
        from menhir.explorer.bench_runs import _nav_neighbors
        prev, nxt = _nav_neighbors(["a", "b", "c"], "b")
        assert prev == "a" and nxt == "c"

    def test_first(self):
        from menhir.explorer.bench_runs import _nav_neighbors
        prev, nxt = _nav_neighbors(["a", "b"], "a")
        assert prev is None and nxt == "b"

    def test_last(self):
        from menhir.explorer.bench_runs import _nav_neighbors
        prev, nxt = _nav_neighbors(["a", "b"], "b")
        assert prev == "a" and nxt is None

    def test_single(self):
        from menhir.explorer.bench_runs import _nav_neighbors
        prev, nxt = _nav_neighbors(["a"], "a")
        assert prev is None and nxt is None

    def test_not_found(self):
        from menhir.explorer.bench_runs import _nav_neighbors
        prev, nxt = _nav_neighbors(["a", "b"], "z")
        assert prev is None and nxt is None


class TestOutcomeCounts:
    def test_counts(self):
        from menhir.explorer.bench_runs import BenchTask, _outcome_counts
        tasks = [BenchTask(primary_outcome="PASSED"), BenchTask(primary_outcome="PASSED"), BenchTask(primary_outcome="FAILED"), BenchTask(primary_outcome=None)]
        c = _outcome_counts(tasks)
        assert c == {"total": 4, "passed": 2, "failed": 1, "unscored": 1}


class TestRouteFilters:
    def test_hidden_card_css_exists(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        with TestClient(create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))) as c:
            r = c.get("/explorer/recall-lab/bench-runs/lme-ingest")
            assert ".task-card[hidden]" in r.text
            assert "display: none" in r.text

    def test_filter_bar_present(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest?outcome=failed")
            assert r.status_code == 200
            assert "outcome=failed" in r.text
            assert 'data-outcome="failed"' in r.text or "data-outcome='failed'" in r.text

    def test_route_tasks_have_data_outcome(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest")
            assert r.status_code == 200
            assert "data-outcome=" in r.text

    def test_task_detail_has_primary_outcome(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001?outcome=passed")
            assert r.status_code == 200
            assert "PASSED" in r.text
            assert "outcome=passed" in r.text

    def test_nav_position(self, bench_root):
        """First task in a 2-item category shows 1 of 2, second shows 2 of 2."""
        d = bench_root / "navpos-run"
        d.mkdir()
        rows = [
            {"namespace": "lme-n1", "question_id": "n1", "question": "Q1", "answer": "A1", "question_type": "t"},
            {"namespace": "lme-n2", "question_id": "n2", "question": "Q2", "answer": "A2", "question_type": "t"},
        ]
        (d / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        cp = ['{"arm":"menhir_recall","result":{"task_id":"lme-n1","correct":false}}', '{"arm":"menhir_recall","result":{"task_id":"lme-n2","correct":false}}']
        (d / ".checkpoint_nav.jsonl").write_text("\n".join(cp), encoding="utf-8")
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        with TestClient(create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))) as c:
            r1 = c.get("/explorer/recall-lab/bench-runs/navpos-run/tasks/lme-n1?outcome=failed")
            assert r1.status_code == 200 and "1 of 2" in r1.text
            r2 = c.get("/explorer/recall-lab/bench-runs/navpos-run/tasks/lme-n2?outcome=failed")
            assert r2.status_code == 200 and "2 of 2" in r2.text

    def test_direct_failed_task_infers_outcome(self, bench_root):
        """Direct access to a failed task without ?outcome= infers failed."""
        d = bench_root / "inf-run"
        d.mkdir()
        rows = [{"namespace": "lme-inf1", "question_id": "inf1", "question": "Q", "answer": "A", "question_type": "t"}]
        (d / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        (d / ".checkpoint_inf.jsonl").write_text('{"arm":"menhir_recall","result":{"task_id":"lme-inf1","correct":false}}', encoding="utf-8")
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/inf-run/tasks/lme-inf1")
            assert r.status_code == 200
            assert "Failed task" in r.text
            assert "1 of 1" in r.text
            assert "outcome=failed" in r.text

    def test_nav_position_and_total(self, bench_root):
        """Task within a multi-item category shows position/total."""
        d = bench_root / "pos-run"
        d.mkdir()
        rows = [
            {"namespace": "lme-p1", "question_id": "p1", "question": "Q1", "answer": "A1", "question_type": "t"},
            {"namespace": "lme-p2", "question_id": "p2", "question": "Q2", "answer": "A2", "question_type": "t"},
        ]
        (d / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        cp = ['{"arm":"menhir_recall","result":{"task_id":"lme-p1","correct":true}}', '{"arm":"menhir_recall","result":{"task_id":"lme-p2","correct":true}}']
        (d / ".checkpoint_pos.jsonl").write_text("\n".join(cp), encoding="utf-8")
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/pos-run/tasks/lme-p2?outcome=passed")
            assert r.status_code == 200
            assert "Passed task" in r.text
            assert "2 of 2" in r.text

    def test_boundary_disabled_controls(self, bench_root):
        """First task in a category shows disabled Previous."""
        d = bench_root / "bnd-run"
        d.mkdir()
        rows = [{"namespace": "lme-b1", "question_id": "b1", "question": "Q", "answer": "A", "question_type": "t"}]
        (d / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        (d / ".checkpoint_bnd.jsonl").write_text('{"arm":"menhir_recall","result":{"task_id":"lme-b1","correct":false}}', encoding="utf-8")
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/bnd-run/tasks/lme-b1?outcome=failed")
            assert r.status_code == 200
            assert "disabled" in r.text
            assert "Previous" in r.text  # rendered as disabled span

    def test_nav_label_all_outcome(self, bench_root):
        """outcome=all uses Task label, not Failed task."""
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        with TestClient(create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))) as c:
            r = c.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001?outcome=all")
            assert r.status_code == 200
            assert "Task " in r.text
            assert "Failed task" not in r.text

    def test_invalid_outcome_normalized_to_all(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest?outcome=invalid")
            assert r.status_code == 200
            assert 'aria-pressed="true"' in r.text  # some filter is active
            assert 'aria-pressed="true"' in r.text  # at least one pressed

    def test_route_tasks_exact_outcome_attributes(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest")
            assert r.status_code == 200
            assert 'data-outcome="PASSED"' in r.text or "data-outcome='PASSED'" in r.text

    def test_back_link_preserves_outcome(self, bench_root):
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001?outcome=passed")
            assert r.status_code == 200
            assert "outcome=passed" in r.text

class TestRedaction:
    def test_simple(self):
        r = _redact_nested({"question": "x", "correct": True})
        assert r["question"] == "[hidden]" and r["correct"] is True

    def test_scores(self):
        r = _redact_nested({"scores": [{"response_text": "x", "arm": "v"}]})
        assert r["scores"][0]["response_text"] == "[hidden]"

    def test_nested_live(self):
        r = _redact_nested({"live_graph": {"evidence": [{"text": "s", "role": "u"}]}})
        assert r["live_graph"]["evidence"][0]["text"] == "[hidden]"

    def test_preserves_structural(self):
        r = _redact_nested({"contract": "v1", "score": 0.5})
        assert r["contract"] == "v1"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestPrimaryOutcome:
    def test_menhir_recall_passed(self):
        from menhir.explorer.bench_runs import BenchScore, _primary_outcome
        scores = [BenchScore(arm="no_memory", correct=False), BenchScore(arm="menhir_recall", correct=True)]
        assert _primary_outcome(scores) == "PASSED"

    def test_menhir_recall_failed(self):
        from menhir.explorer.bench_runs import BenchScore, _primary_outcome
        scores = [BenchScore(arm="no_memory", correct=True), BenchScore(arm="menhir_recall", correct=False)]
        assert _primary_outcome(scores) == "FAILED"

    def test_fallback_arm(self):
        from menhir.explorer.bench_runs import BenchScore, _primary_outcome
        scores = [BenchScore(arm="candidate", correct=True)]
        assert _primary_outcome(scores) == "PASSED"

    def test_no_memory_only(self):
        from menhir.explorer.bench_runs import BenchScore, _primary_outcome
        scores = [BenchScore(arm="no_memory", correct=True)]
        assert _primary_outcome(scores) == "NOT SCORED"

    def test_empty_scores(self):
        from menhir.explorer.bench_runs import _primary_outcome
        assert _primary_outcome([]) == "NOT SCORED"

    def test_route_passed_card(self, bench_root):
        """Run detail route shows PASSED pill for a scored task."""
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest")
            assert r.status_code == 200
            assert "PASSED" in r.text

    def test_route_unscored_card(self, bench_root):
        """Task without checkpoint scores shows NOT SCORED."""
        d = bench_root / "unscored-run"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps([{"namespace": "lme-u1", "question_id": "u1", "question": "Q?", "answer": "A", "question_type": "t"}]), encoding="utf-8")
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/unscored-run")
            assert r.status_code == 200
            assert "NOT SCORED" in r.text
            # Check no card body contains PASSED/FAILED (CSS selectors are in <style>)
            body_start = r.text.find(">", r.text.find("<main"))
            body_end = r.text.find("</main>")
            body_text = r.text[body_start:body_end] if body_start > 0 and body_end > 0 else r.text
            assert "NOT SCORED" in body_text
            assert "PASSED" not in body_text
            assert "FAILED" not in body_text


class TestEdgeCases:
    def test_empty_root(self, tmp_path):
        assert BenchRunCatalog(results_root=str(tmp_path)).list_runs() == []

    def test_unconfigured(self):
        assert BenchRunCatalog(results_root=None).list_runs() == []

    def test_empty_manifest(self, bench_root):
        d = bench_root / "e"; d.mkdir()
        (d / "manifest.json").write_text("[]", encoding="utf-8")
        run = BenchRunCatalog(results_root=str(bench_root)).get_run("e")
        assert run is not None and run.total_items == 0

    def test_legacy_null_counts_dont_crash(self, bench_root):
        """Legacy manifest rows with null/missing numeric fields must not raise."""
        legacy = bench_root / "legacy-run"
        legacy.mkdir()
        rows = [
            {"namespace": "lme-001", "question_id": "001", "question": "Q1", "answer": "A1", "question_type": "temporal-reasoning", "turns": None, "typed_assertions": None, "scalar_views": None, "turn_evidence": None, "scalar_llm_calls": None, "scalar_consolidated": False, "ready": None, "failed_remaining": None},
            {"namespace": "lme-002", "question_id": "002", "question": "Q2", "answer": "A2", "question_type": "temporal-reasoning"},
        ]
        (legacy / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        catalog = BenchRunCatalog(results_root=str(bench_root))
        runs = catalog.list_runs()
        leg = next((r for r in runs if r.run_id == "legacy-run"), None)
        assert leg is not None, "legacy-run must appear in list_runs"
        assert len(leg.tasks) == 2
        for task in leg.tasks:
            assert task.turns == 0
            assert task.typed_assertions == 0
            assert task.scalar_views == 0
            assert task.turn_evidence == 0
        from fastapi.testclient import TestClient
        from menhir.explorer.app import create_app
        app = create_app(bench_run_catalog=catalog)
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs")
            assert r.status_code == 200
            assert "legacy-run" in r.text


# ---------------------------------------------------------------------------
# Default provider
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset _CACHED_CATALOG before each test."""
    import menhir.explorer.bench_runs as m
    m._CACHED_CATALOG = None


class TestDefaultProvider:
    def test_no_env(self, monkeypatch):
        monkeypatch.delenv("MENHIR_BENCH_RESULTS_ROOT", raising=False)
        p = default_bench_run_provider()
        assert not p.is_configured

    def test_with_env(self, bench_root, monkeypatch):
        monkeypatch.setenv("MENHIR_BENCH_RESULTS_ROOT", str(bench_root))
        p = default_bench_run_provider()
        assert p.is_configured

    def test_cache_reused(self, bench_root, monkeypatch):
        monkeypatch.setenv("MENHIR_BENCH_RESULTS_ROOT", str(bench_root))
        p1 = default_bench_run_provider()
        p2 = default_bench_run_provider()
        assert p1 is p2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, bench_root):
        app = create_app(bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)))
        with TestClient(app) as c:
            yield c

    def test_list_html(self, client):
        r = client.get("/explorer/recall-lab/bench-runs")
        assert r.status_code == 200 and "lme-ingest" in r.text

    def test_list_api(self, client):
        r = client.get("/explorer/api/recall-lab/bench-runs")
        assert r.status_code == 200
        assert len(r.json()["runs"]) == 2

    def test_run_html(self, client):
        r = client.get("/explorer/recall-lab/bench-runs/lme-ingest")
        assert r.status_code == 200

    def test_run_api(self, client):
        r = client.get("/explorer/api/recall-lab/bench-runs/lme-ingest")
        assert r.status_code == 200 and r.json()["run_id"] == "lme-ingest"

    def test_task_html(self, client):
        r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001")
        assert r.status_code == 200 and "What is the capital?" in r.text

    def test_task_html_has_single_panel_tabs_in_requested_order(self):
        template = (
            Path(__file__).parents[1]
            / "src"
            / "menhir"
            / "explorer"
            / "templates"
            / "bench_task_detail.html"
        ).read_text(encoding="utf-8")

        expected_tabs = [
            ("#turns", "Turns"),
            ("#assertions", "Assertions"),
            ("#scalar-roles", "Scalar Roles"),
            ("#scalar-state", "Scalar State"),
            ("#scalar-history", "Scalar History"),
            ("#event-history", "Event History"),
            ("#memory", "Memory"),
            ("#recall-packet", "Recall Packet"),
            ("#answer-path", "Answer Path"),
        ]
        positions = []
        for href, label in expected_tabs:
            marker = f"href='{href}'"
            assert marker in template
            assert f">{label}</a>" in template
            assert f"id='{href[1:]}'" in template
            positions.append(template.index(marker))

        assert positions == sorted(positions)
        assert "position: sticky" in template
        assert "role='tablist'" in template
        assert "role='tab'" in template
        assert "aria-label='Task detail sections'" in template
        assert "aria-selected='true'" in template
        assert ".task-tab-section[hidden] { display: none; }" in template
        assert "section.hidden = section !== activeSection" in template
        assert "shell.scrollTop = preservedScrollTop" in template
        assert "scrollIntoView" not in template
        assert "updateActiveFromScroll" not in template
        assert "class='turn-stream'" in template
        assert "role-{{ turn.role | lower }}" in template
        assert ".evidence-card.role-user { align-self: flex-end" in template
        assert ".evidence-card.role-assistant { align-self: flex-start" in template
        assert "data-packet-view='pretty'" in template
        assert "data-packet-view='raw'" in template
        assert ">Raw LLM packet</button>" in template
        assert "packetViewPanels.forEach" in template
        assert "panel.id !== `packet-view-${requestedView}`" in template
        assert "exact text emitted by the prototype formatter" in template

    def test_task_api(self, client):
        r = client.get("/explorer/api/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001")
        assert r.status_code == 200 and r.json()["namespace"] == "lme-item-001"

    def test_query_filtered_packet_requires_runtime(self, client):
        r = client.post(
            "/explorer/api/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001/recall-packet",
            json={"query": "What is the capital?"},
        )
        assert r.status_code == 503

    def test_query_filtered_packet_uses_production_results(self, bench_root):
        app = create_app(
            recall_service=object(),
            bench_run_catalog=BenchRunCatalog(results_root=str(bench_root)),
        )
        recall_payload = {
            "arms": [
                {
                    "id": "production",
                    "ok": True,
                    "degraded": False,
                    "elapsed_ms": 12.5,
                    "candidates_evaluated": 7,
                    "authority_layer": None,
                    "event_authority_layer": None,
                    "results": [
                        {
                            "rank": 1,
                            "uuid": "memory-paris",
                            "name": "capital",
                            "content": "The capital is Paris.",
                            "memory_type": "content",
                            "temporal_facts": [],
                        }
                    ],
                }
            ]
        }
        with patch(
            "menhir.explorer.app.run_recall_lab",
            new=AsyncMock(return_value=recall_payload),
        ) as run_mock:
            with TestClient(app) as client:
                r = client.post(
                    "/explorer/api/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001/recall-packet",
                    json={"query": "What is the capital?", "max_chars": 2500},
                )
        assert r.status_code == 200
        payload = r.json()
        assert payload["contract"] == "typed-recall-packet/query-filtered-v1"
        assert payload["retrieval"]["arm"] == "production"
        assert payload["packet"]["budget"]["actual_chars"] <= 2500
        assert "The capital is Paris" in payload["packet"]["text"]
        request = run_mock.await_args.args[1]
        assert request.namespace == "lme-item-001"
        assert [arm.id for arm in request.arms] == ["production"]
        assert request.judge is False

    def test_404(self, client):
        assert client.get("/explorer/api/recall-lab/bench-runs/nonexistent/tasks/x").status_code == 404
        assert client.get("/explorer/api/recall-lab/bench-runs/lme-ingest/tasks/x").status_code == 404

    def test_unsafe_id_404(self, client):
        assert client.get("/explorer/recall-lab/bench-runs/../escape").status_code == 404

    def test_privacy_hidden_task(self, client):
        r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001", cookies={"menhir_reveal": "0"})
        assert r.status_code == 200 and "[hidden]" in r.text

    def test_privacy_reveal_task(self, client):
        r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001", cookies={"menhir_reveal": "1"})
        assert r.status_code == 200 and "What is the capital?" in r.text

    def test_privacy_hidden_run_api(self, client):
        r = client.get("/explorer/api/recall-lab/bench-runs/lme-ingest", cookies={"menhir_reveal": "0"})
        data = r.json()
        # Task questions should be redacted
        for t in data["tasks"]:
            assert "[hidden]" in str(t.get("question", ""))

    def test_privacy_reveal_run_api(self, client):
        r = client.get("/explorer/api/recall-lab/bench-runs/lme-ingest", cookies={"menhir_reveal": "1"})
        data = r.json()
        qs = [t["question"] for t in data["tasks"]]
        assert "What is the capital?" in qs

    def test_live_query_audit_not_exposed(self, bench_root):
        """Raw audit events with distribution content must not leak in live_graph."""
        repo = MagicMock()
        repo.execute.return_value = [{"id": "ev-1", "role": "user", "text": "hello", "occurred_at": None, "recorded_at": "now", "founds": []}]
        app = create_app(
            repo=repo,
            bench_run_catalog=BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest"),
        )
        with TestClient(app) as client:
            r = client.get("/explorer/api/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001", cookies={"menhir_reveal": "1"})
            data = r.json()
            lg = data.get("live_graph", {})
            assert "audit" not in lg, "Raw audit events must not be exposed"
            assert "audit_pass_id" in lg or "audit_warning" in lg

    def test_live_query_route(self, bench_root):
        repo = MagicMock()
        repo.execute.return_value = [{"id": "ev-1", "role": "user", "text": "hello", "occurred_at": None, "recorded_at": "now", "founds": []}]
        app = create_app(
            repo=repo,
            bench_run_catalog=BenchRunCatalog(results_root=str(bench_root), active_run_id="lme-ingest"),
        )
        with TestClient(app) as client:
            r = client.get("/explorer/recall-lab/bench-runs/lme-ingest/tasks/lme-item-001")
            assert r.status_code == 200
