"""Filesystem catalog of LME benchmark runs.

Extracted from ``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .bench_runs_artifacts import (
    _discover_run_dirs,
    _find_checkpoint_files,
    _get_identity,
    _get_provenance_value,
    _read_checkpoint_scores,
    _read_provenance,
)
from .bench_runs_io import _is_safe_component, _read_json, _safe_int, _safe_resolve
from .bench_runs_models import BenchRun, BenchScore, BenchTask, _primary_outcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Catalog — fail closed when MENHIR_BENCH_RESULTS_ROOT is absent
# ---------------------------------------------------------------------------


class BenchRunCatalog:
    """Filesystem catalog of LME benchmark runs.

    Requires a configured results_root (caller must handle the case where
    MENHIR_BENCH_RESULTS_ROOT env var is not set by returning empty results
    with a clear configuration warning).
    """

    def __init__(self, results_root: str | Path | None, active_run_id: str | None = None):
        if results_root is None:
            self._root = None
        else:
            raw = Path(results_root)
            if raw.is_symlink():
                logger.warning("Benchmark results root is a symlink: %s; treating as unconfigured", raw)
                self._root = None
            else:
                self._root = raw.resolve()
        self._active_run_id = active_run_id
        self._run_dirs: dict[str, Path] = {}

    @property
    def results_root(self) -> Path | None:
        return self._root

    @property
    def active_run_id(self) -> str | None:
        return self._active_run_id

    @property
    def is_configured(self) -> bool:
        return self._root is not None and self._root.is_dir()

    def _ensure_discovered(self) -> None:
        if not self._run_dirs and self._root is not None:
            self._run_dirs = _discover_run_dirs(self._root)

    def _run_dir(self, run_id: str) -> Path | None:
        if self._root is None:
            return None
        if not _is_safe_component(run_id):
            return None
        self._ensure_discovered()
        rel = self._run_dirs.get(run_id)
        if rel is None:
            return None
        return _safe_resolve(self._root, *rel.parts)

    def list_runs(self) -> list[BenchRun]:
        if self._root is None:
            return []
        self._ensure_discovered()
        entries: list[tuple[float, str, Path]] = []
        for rid, rel in self._run_dirs.items():
            run_dir = _safe_resolve(self._root, *rel.parts)
            if run_dir is None or not run_dir.is_dir():
                continue
            mtime = run_dir.stat().st_mtime
            entries.append((mtime, rid, run_dir))
        entries.sort(key=lambda x: x[0], reverse=True)
        runs: list[BenchRun] = []
        for mtime, rid, run_dir in entries:
            manifest = _read_json(run_dir / "manifest.json")
            if not isinstance(manifest, list):
                continue
            run = self._build_run(rid, run_dir, manifest)
            if run is not None:
                self._apply_live_graph_lineage(run)
                runs.append(run)
        return runs

    def get_run(self, run_id: str) -> BenchRun | None:
        if self._root is None:
            return None
        run_dir = self._run_dir(run_id)
        if run_dir is None or not run_dir.is_dir():
            return None
        manifest = _read_json(run_dir / "manifest.json")
        if not isinstance(manifest, list):
            return BenchRun(
                run_id=run_id,
                source=run_dir.parent.name,
                total_items=0, completed_items=0,
                source_warning="Run directory exists but manifest has no task array.",
            )
        run = self._build_run(run_id, run_dir, manifest)
        if run is not None:
            self._apply_live_graph_lineage(run)
        return run

    def get_run_dir(self, run_id: str) -> Path | None:
        """Return the resolved run directory for *run_id*, or None."""
        return self._run_dir(run_id)

    def is_active(self, run_id: str) -> bool:
        return self._active_run_id is not None and run_id == self._active_run_id

    def live_graph_source(self, run: BenchRun) -> str | None:
        """Return the run whose live graph may safely back ``run``.

        Historical recall results may point at the configured active graph only
        when provenance explicitly says the graph was reused without reingest and
        the immutable graph identity matches. Missing identity fields fail closed.
        """
        if run.is_active:
            return run.run_id
        if (
            not run.source_graph_reused
            or run.reingested
            or not run.graph_source_run_id
            or run.graph_source_run_id != self._active_run_id
        ):
            return None

        source = self.get_run(run.graph_source_run_id)
        if source is None or not source.is_active:
            return None

        compared_fields = (
            "graph_container",
            "graph_volume",
            "fixture_sha256",
            "fixture_count",
            "namespace_prefix",
        )
        for field_name in compared_fields:
            historical_value = getattr(run, field_name)
            source_value = getattr(source, field_name)
            if not historical_value or not source_value or historical_value != source_value:
                logger.warning(
                    "Rejecting live graph lineage for run %s: %s mismatch",
                    run.run_id,
                    field_name,
                )
                return None
        return source.run_id

    def _apply_live_graph_lineage(self, run: BenchRun) -> None:
        """Annotate a run after fail-closed graph-lineage resolution."""
        source_run_id = self.live_graph_source(run)
        run.live_graph_source_run_id = source_run_id
        if not source_run_id or source_run_id == run.run_id:
            return
        run.source_warning = (
            f"REUSED ACTIVE GRAPH: historical scores + live graph from {source_run_id}"
        )
        for task in run.tasks:
            task.graph_available = False
            task.source_warning = (
                "REUSED ACTIVE GRAPH CONFIGURED: Historical score artifact; "
                f"live graph projection is sourced from {source_run_id} after identity validation."
            )

    def _build_run(self, run_id: str, run_dir: Path, manifest: list[Any]) -> BenchRun | None:
        is_active = self.is_active(run_id)
        run_source = run_dir.parent.name

        provenance = _read_provenance(run_dir / "run_provenance.json")
        identity = _get_identity(provenance)
        attempt_count = 0
        noncanonical = False
        resumed = False
        phases: list[dict[str, Any]] = []
        harness_exit: int | None = None
        if isinstance(provenance, dict):
            attempt_count = _safe_int(provenance.get("attempt_count"))
            noncanonical = bool(provenance.get("noncanonical", False))
            resumed = bool(provenance.get("resumed", False))
            if isinstance(provenance.get("phases"), list):
                for p in provenance["phases"]:
                    if isinstance(p, dict):
                        phases.append({
                            "phase": str(p.get("phase", "")),
                            "status": str(p.get("status", "")),
                            "completed_at": str(p.get("completed_at", "")),
                        })
            harness_exit = provenance.get("harness_exit")
            if isinstance(harness_exit, int):
                pass
            else:
                harness_exit = None
            latest = provenance.get("latest_attempt")
            if isinstance(latest, dict):
                resumed = resumed or bool(latest.get("resumed", False))

        variant = str(identity.get("variant", identity.get("LONGMEMEVAL_VARIANT", "?")))
        arm = str(identity.get("arm", ""))
        extract_model = str(identity.get("extract_model", ""))
        menhir_commit = str(identity.get("menhir_commit", ""))
        bench_commit = str(identity.get("bench_commit", ""))
        started_at = str(identity.get("started_at", ""))
        graph_source_run_id = str(_get_provenance_value(provenance, "source_run_id") or "") or None
        source_graph_reused = bool(_get_provenance_value(provenance, "source_graph_reused"))
        reingested = bool(_get_provenance_value(provenance, "reingested"))
        graph_container = str(_get_provenance_value(provenance, "container") or "")
        graph_volume = str(_get_provenance_value(provenance, "volume") or "")
        fixture_sha256 = str(_get_provenance_value(provenance, "fixture_sha256") or "")
        fixture_count = _safe_int(_get_provenance_value(provenance, "fixture_count"))
        namespace_prefix = str(_get_provenance_value(provenance, "namespace_prefix") or "")

        manifest_rows = [r for r in manifest if isinstance(r, dict)]
        total_items = len(manifest_rows)
        completed_items = sum(1 for r in manifest_rows if r.get("ready"))

        checkpoint_scores: dict[str, list[dict[str, Any]]] = {}
        for cp in _find_checkpoint_files(run_dir):
            records = _read_checkpoint_scores(cp)
            for ns, recs in records.items():
                checkpoint_scores.setdefault(ns, []).extend(recs)

        tasks: list[BenchTask] = []
        for row in manifest_rows:
            namespace = str(row.get("namespace", ""))
            question_id = str(row.get("question_id", namespace))
            question = str(row.get("question", ""))
            answer = str(row.get("answer", ""))
            question_type = str(row.get("question_type", ""))
            turns = _safe_int(row.get("turns"))
            typed_assertions = _safe_int(row.get("typed_assertions"))
            scalar_views = _safe_int(row.get("scalar_views"))
            turn_evidence = _safe_int(row.get("turn_evidence"))
            scalar_llm_calls = _safe_int(row.get("scalar_llm_calls"))
            scalar_consolidated = bool(row.get("scalar_consolidated", False))
            ready = bool(row.get("ready", False))
            failed_remaining = _safe_int(row.get("failed_remaining"))

            # graph_available starts None for non-active, False for active
            # (only set True after a successful live query in task reader).
            if is_active:
                graph_available = False
                source_warning = (
                    "ACTIVE CONFIGURED: This run is configured as the active benchmark run. "
                    "Live graph projection becomes available when you open a task."
                )
            else:
                graph_available = None
                source_warning = (
                    "ARTIFACT-ONLY: graph data is not available for non-active runs."
                )

            task_scores_raw = checkpoint_scores.get(namespace, [])
            task_scores = [BenchScore(**{k: s.get(k, "") for k in BenchScore.__dataclass_fields__}) for s in task_scores_raw]
            primary_outcome = _primary_outcome(task_scores)
            tasks.append(BenchTask(
                namespace=namespace,
                question_id=question_id,
                question=question,
                answer=answer,
                question_type=question_type,
                turns=turns,
                typed_assertions=typed_assertions,
                scalar_views=scalar_views,
                turn_evidence=turn_evidence,
                scalar_llm_calls=scalar_llm_calls,
                scalar_consolidated=scalar_consolidated,
                ready=ready,
                failed_remaining=failed_remaining,
                graph_available=graph_available,
                scores=task_scores,
                source_warning=source_warning,
                primary_outcome=primary_outcome,
            ))

        arms: dict[str, dict[str, Any]] = {}
        for recs in checkpoint_scores.values():
            for rec in recs:
                arm_name = rec.get("arm", "")
                if arm_name not in arms:
                    arms[arm_name] = {"n": 0, "correct": 0}
                arms[arm_name]["n"] += 1
                if rec.get("correct"):
                    arms[arm_name]["correct"] += 1

        label = run_id
        if arm:
            label = f"{run_id} ({arm})"

        src_warning: str | None = None
        if is_active:
            src_warning = "ACTIVE CONFIGURED"
        else:
            src_warning = "ARTIFACT-ONLY"

        return BenchRun(
            run_id=run_id,
            label=label,
            source=run_source,
            variant=variant,
            model=extract_model,
            total_items=total_items,
            completed_items=completed_items,
            tasks=tasks,
            arms=arms,
            started_at=started_at,
            menhir_commit=menhir_commit,
            bench_commit=bench_commit,
            arm=arm,
            is_active=is_active,
            source_warning=src_warning,
            attempts=attempt_count,
            noncanonical=noncanonical,
            resumed=resumed,
            phases=phases,
            harness_exit=harness_exit,
            graph_source_run_id=graph_source_run_id,
            source_graph_reused=source_graph_reused,
            reingested=reingested,
            graph_container=graph_container,
            graph_volume=graph_volume,
            fixture_sha256=fixture_sha256,
            fixture_count=fixture_count,
            namespace_prefix=namespace_prefix,
        )
