"""Explorer benchmark-run routes (Recall Lab integration)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from menhir.explorer.app_routes import TEMPLATES
from menhir.explorer.app_privacy import _reveal
from menhir.explorer.bench_runs import (
    CONTRACT_VERSION,
    BenchRunCatalog,
    BenchRunTaskReader,
    _filtered_task_ids,
    _nav_neighbors,
    _normalize_outcome,
    _outcome_counts,
    _outcome_from_primary,
)


# ---- Benchmark Tasks (Recall Lab integration) ----

def _bench_catalog(request: Request) -> BenchRunCatalog:
    catalog = getattr(request.app.state, "bench_run_catalog", None)
    if catalog is None:
        from menhir.explorer.bench_runs import default_bench_run_provider
        catalog = default_bench_run_provider()
        request.app.state.bench_run_catalog = catalog
    return catalog


def _bench_task_reader(request: Request) -> BenchRunTaskReader:
    existing = getattr(request.app.state, "bench_task_reader", None)
    if existing is not None:
        return existing
    catalog = _bench_catalog(request)
    reader = BenchRunTaskReader(
        catalog=catalog,
        repo_provider=lambda: getattr(request.app.state, "repo", None),
    )
    request.app.state.bench_task_reader = reader
    return reader


def register_bench_routes(router: APIRouter) -> None:
    """Register the benchmark-run browsing routes (HTML pages + JSON APIs)."""
    @router.get("/explorer/recall-lab/bench-runs", response_class=HTMLResponse)
    async def bench_runs_list(request: Request) -> HTMLResponse:
        rev = _reveal(request)
        catalog = _bench_catalog(request)
        runs = catalog.list_runs()
        return TEMPLATES.TemplateResponse(
            request,
            "bench_runs.html",
            {"runs": runs, "privacy_redact": not rev, "configured": catalog.is_configured},
        )

    @router.get("/explorer/recall-lab/bench-runs/{run_id}", response_class=HTMLResponse)
    async def bench_run_detail(
        request: Request,
        run_id: str,
        outcome: str = Query(default="all"),
    ) -> HTMLResponse:
        rev = _reveal(request)
        catalog = _bench_catalog(request)
        run = catalog.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Benchmark run not found")
        outcome = _normalize_outcome(outcome)
        outcome_counts = _outcome_counts(run.tasks) if hasattr(run, "tasks") and run.tasks else {}
        return TEMPLATES.TemplateResponse(
            request,
            "bench_run_detail.html",
            {"run": run, "privacy_redact": not rev, "outcome": outcome, "outcome_counts": outcome_counts},
        )

    @router.get("/explorer/recall-lab/bench-runs/{run_id}/tasks/{namespace}", response_class=HTMLResponse)
    async def bench_task_detail(
        request: Request,
        run_id: str,
        namespace: str,
        outcome: str | None = Query(default=None),
    ) -> HTMLResponse:
        rev = _reveal(request)
        reader = _bench_task_reader(request)
        task = reader.get_task_detail(run_id, namespace, reveal=rev)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not outcome or outcome not in ("all", "failed", "passed", "unscored"):
            outcome = _outcome_from_primary(task.get("primary_outcome"))
        # Compute prev/next within the same outcome category
        run_obj = _bench_catalog(request).get_run(run_id)
        prev_ns: str | None = None
        next_ns: str | None = None
        nav_position: int = 0
        nav_total: int = 0
        if run_obj is not None and hasattr(run_obj, "tasks") and run_obj.tasks:
            ordered = _filtered_task_ids(run_obj.tasks, outcome)
            nav_total = len(ordered)
            prev_ns, next_ns = _nav_neighbors(ordered, namespace)
            try:
                nav_position = ordered.index(namespace) + 1
            except ValueError:
                nav_position = 0
        nav_label = {"all": "Task", "failed": "Failed task", "passed": "Passed task", "unscored": "Not scored task"}.get(outcome or "all", "Task")
        return TEMPLATES.TemplateResponse(
            request,
            "bench_task_detail.html",
            {
                "task": task,
                "privacy_redact": not rev,
                "outcome": outcome,
                "prev_task": prev_ns,
                "next_task": next_ns,
                "nav_position": nav_position,
                "nav_total": nav_total,
                "nav_label": nav_label,
            },
        )

    @router.get("/explorer/api/recall-lab/bench-runs", response_class=JSONResponse)
    async def bench_runs_api(request: Request) -> JSONResponse:
        rev = _reveal(request)
        catalog = _bench_catalog(request)
        runs = catalog.list_runs()
        payload = {
            "contract": CONTRACT_VERSION,
            "runs": [
                {
                    "run_id": r.run_id,
                    "label": r.label,
                    "variant": r.variant,
                    "model": r.model,
                    "total_items": r.total_items,
                    "completed_items": r.completed_items,
                    "is_active": r.is_active,
                    "graph_source_run_id": r.live_graph_source_run_id,
                    "source_warning": r.source_warning,
                    "arm": r.arm,
                    "started_at": r.started_at,
                }
                for r in runs
            ],
        }
        if not rev:
            from menhir.explorer.bench_runs import _redact_nested
            payload = _redact_nested(payload)
        return JSONResponse(payload)

    @router.get("/explorer/api/recall-lab/bench-runs/{run_id}", response_class=JSONResponse)
    async def bench_run_api(request: Request, run_id: str) -> JSONResponse:
        rev = _reveal(request)
        catalog = _bench_catalog(request)
        run = catalog.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Benchmark run not found")
        payload = {
            "contract": CONTRACT_VERSION,
            "run_id": run.run_id,
            "label": run.label,
            "variant": run.variant,
            "model": run.model,
            "total_items": run.total_items,
            "completed_items": run.completed_items,
            "is_active": run.is_active,
            "graph_source_run_id": run.live_graph_source_run_id,
            "source_warning": run.source_warning,
            "arm": run.arm,
            "started_at": run.started_at,
            "menhir_commit": run.menhir_commit,
            "bench_commit": run.bench_commit,
            "attempts": run.attempts,
            "noncanonical": run.noncanonical,
            "resumed": run.resumed,
            "phases": run.phases,
            "harness_exit": run.harness_exit,
            "arms": run.arms,
            "tasks": [
                {
                    "namespace": t.namespace,
                    "question_id": t.question_id,
                    "question": t.question,
                    "question_type": t.question_type,
                    "turns": t.turns,
                    "typed_assertions": t.typed_assertions,
                    "scalar_views": t.scalar_views,
                    "graph_available": t.graph_available,
                    "source_warning": t.source_warning,
                    "scalar_consolidated": t.scalar_consolidated,
                    "ready": t.ready,
                }
                for t in run.tasks
            ],
        }
        if not rev:
            from menhir.explorer.bench_runs import _redact_nested
            payload = _redact_nested(payload)
        return JSONResponse(payload)

    @router.get("/explorer/api/recall-lab/bench-runs/{run_id}/tasks/{namespace}", response_class=JSONResponse)
    async def bench_task_api(request: Request, run_id: str, namespace: str) -> JSONResponse:
        reader = _bench_task_reader(request)
        task = reader.get_task_detail(run_id, namespace, reveal=_reveal(request))
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return JSONResponse(task)
