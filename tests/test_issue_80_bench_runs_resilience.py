"""#80: a malformed checkpoint score must not 500 the catalog, and new runs must be discovered."""

from __future__ import annotations

import json
from pathlib import Path

from menhir.explorer.bench_runs import BenchRunCatalog, _parse_checkpoint_record, _read_checkpoint_scores


def _make_run(root: Path, name: str) -> None:
    run = root / name
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps([{"namespace": "lme-a", "question_id": "a"}]), encoding="utf-8")


def test_string_score_falls_back_to_the_correct_flag() -> None:
    rec = {"task_id": "a", "correct": True, "score": "not-a-number"}
    assert _parse_checkpoint_record(rec)["score"] == 1.0
    nested = {"arm": "x", "result": {"task_id": "a", "correct": False, "score": "nope"}}
    assert _parse_checkpoint_record(nested)["score"] == 0.0


def test_checkpoint_file_with_bad_score_still_reads(tmp_path: Path) -> None:
    cp = tmp_path / ".checkpoint_x.jsonl"
    cp.write_text(json.dumps({"task_id": "a", "score": "bad"}) + "\n", encoding="utf-8")
    assert len(_read_checkpoint_scores(cp)["lme-a"]) == 1


def test_run_created_after_first_listing_is_discovered(tmp_path: Path) -> None:
    _make_run(tmp_path, "run-one")
    catalog = BenchRunCatalog(results_root=str(tmp_path))
    assert [r.run_id for r in catalog.list_runs()] == ["run-one"]
    _make_run(tmp_path, "run-two")
    assert {r.run_id for r in catalog.list_runs()} == {"run-one", "run-two"}


def test_get_run_finds_a_run_created_after_first_discovery(tmp_path: Path) -> None:
    _make_run(tmp_path, "run-one")
    catalog = BenchRunCatalog(results_root=str(tmp_path))
    catalog.list_runs()
    _make_run(tmp_path, "run-two")
    assert catalog.get_run("run-two") is not None
