"""Small memory-aware Neo4j explorer."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

import menhir
from menhir.config import MemorySettings
from menhir.config.settings import is_loopback_host
from menhir.explorer.feature_taxonomy import classify, parent_order
from menhir.explorer.recall_lab import DEFAULT_ARMS, RecallLabRequest, run_recall_lab
from menhir.explorer.recall_packet_prototype import (
    QUERY_FILTERED_PACKET_VERSION,
    build_query_filtered_recall_packet,
)
from menhir.explorer.extraction_lab import ExtractionLabRequest, run_extraction_lab
from menhir.explorer.bench_runs import BenchRunCatalog, BenchRunTaskReader, CONTRACT_VERSION, _outcome_counts, _filtered_task_ids, _nav_neighbors, _normalize_outcome, _outcome_from_primary
from menhir.privacy import redact_mapping, redact_rows
from menhir.infrastructure import Neo4jRepository
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.mcp.telemetry import telemetry_store
from menhir.explorer.app_bench_routes import _bench_task_reader, register_bench_routes
from menhir.explorer.app_privacy import _redact_detail, _redact_graph_elements, _redact_session, _reveal
from menhir.explorer.app_queries import (
    _candidates,
    _failed_episodes,
    _flagged_nodes,
    _graph_elements,
    _graph_node_class,
    _node_detail,
    _node_kind,
    _queued_episodes,
    _recent_episodes,
    _recent_sessions,
    _recovered_episodes,
    _scope_badge,
    _search_entities,
    _session_detail,
    _session_graph_elements,
    _successful_episodes,
)
from menhir.explorer.app_routes import STATIC_DIR, TEMPLATES, register_dashboard_routes
from menhir.explorer.app_runtime import (
    _repo_from_settings,
    _require_explorer_agent,
    _require_explorer_readonly,
    _runtime_llm,
    _runtime_recall_service,
    _settings_from_env,
)


# Usage-dashboard time windows -> lookback hours (None = all-time history).
FEATURE_WINDOWS: dict[str, int | None] = {"7d": 168, "30d": 720, "all": None}


class QueryFilteredPacketRequest(BaseModel):
    """Bounded request for a production-selected, typed benchmark packet."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=20)
    max_chars: int = Field(default=6_000, ge=2_000, le=16_000)
    max_general: int = Field(default=4, ge=0, le=10)


def _feature_report(window: str) -> dict[str, Any]:
    """Assemble the feature usage/effectiveness report for a time window.

    Returns per-function rows (each tagged with its parent) plus per-parent
    rollups so the template can group by parent and sort by either dimension.
    """
    since_hours = FEATURE_WINDOWS.get(window, None)
    rows = telemetry_store.fetch_feature_stats(since_hours=since_hours)
    usefulness = telemetry_store.fetch_usefulness_stats(since_hours=since_hours)

    functions: list[dict[str, Any]] = []
    for row in rows:
        parent = classify(row["operation"], row["kind"])
        use = usefulness.get(row["operation"], {})
        functions.append(
            {
                **row,
                "parent": parent,
                "receipts": use.get("receipts", 0),
                "rated": use.get("rated", 0),
                "scored": use.get("scored", 0),
                "rated_pct": use.get("rated_pct", 0.0),
                "avg_score": use.get("avg_score"),
            }
        )

    # Roll up by parent. Latency percentiles do not aggregate cleanly across
    # operations, so the parent card reports a call-weighted typical p50 only.
    order = list(parent_order())
    buckets: dict[str, list[dict[str, Any]]] = {}
    for fn in functions:
        buckets.setdefault(fn["parent"], []).append(fn)

    parents: list[dict[str, Any]] = []
    for name in order:
        members = buckets.get(name)
        if not members:
            continue
        calls = sum(f["total_calls"] for f in members)
        successes = sum(f["successes"] for f in members)
        weighted_p50 = [
            f["p50_ms"] * f["total_calls"] for f in members if f["p50_ms"] is not None
        ]
        p50_calls = sum(f["total_calls"] for f in members if f["p50_ms"] is not None)
        receipts = sum(f["receipts"] for f in members)
        rated = sum(f["rated"] for f in members)
        scored = sum(f["scored"] for f in members)
        score_weight = sum(
            f["avg_score"] * f["scored"] for f in members if f["avg_score"] is not None
        )
        parents.append(
            {
                "name": name,
                "function_count": len(members),
                "total_calls": calls,
                "calls_per_day": round(sum(f["calls_per_day"] for f in members), 2),
                "errors": sum(f["errors"] for f in members),
                "success_rate_pct": round(successes / calls * 100, 1) if calls else 0.0,
                "typical_p50_ms": round(sum(weighted_p50) / p50_calls) if p50_calls else None,
                "receipts": receipts,
                "rated": rated,
                "rated_pct": round(rated / receipts * 100, 1) if receipts else 0.0,
                "avg_score": round(score_weight / scored, 2) if scored else None,
            }
        )

    total_calls = sum(f["total_calls"] for f in functions)
    return {
        "window": window,
        "windows": list(FEATURE_WINDOWS.keys()),
        "parents": parents,
        "functions": functions,
        "total_calls": total_calls,
        "total_functions": len(functions),
    }


def _recall_lab_store(request: Request) -> object:
    return getattr(request.app.state, "recall_lab_store", None) or telemetry_store


def _extraction_lab_store(request: Request) -> object:
    return getattr(request.app.state, "extraction_lab_store", None) or telemetry_store


def create_explorer_router() -> Any:
    """Create an APIRouter with all explorer routes for mounting onto an app.

    Routes assume app.state.repo and app.state.candidate_service are available.
    """
    from fastapi import APIRouter, Depends

    # Tier enforcement is applied to the ROUTER, not written into each route body.
    # api/routes.py carries 23 explicit `_require_tier(...)` calls across its own endpoints;
    # this router carried 36 routes and zero. Enforcing per-route here would fix the 36 that
    # exist and silently exempt the 37th, which is the failure mode this codebase keeps
    # reproducing. A router-level dependency cannot be forgotten by a new route.
    #
    # readonly is the FLOOR. The mutating routes below re-assert a higher tier in their own
    # bodies; the dependency does not weaken those.
    router = APIRouter(dependencies=[Depends(_require_explorer_readonly)])

    @router.get("/explorer", response_class=HTMLResponse)
    async def explorer_home(request: Request) -> HTMLResponse:
        repo_obj = request.app.state.repo
        rev = _reveal(request)
        # CF-107: the explorer shares its event loop with the API and MCP surfaces, so a page that
        # runs its queries inline parks all three. This handler alone makes nine round trips.
        episodes = await asyncio.to_thread(_recent_episodes, repo_obj)
        pending = await asyncio.to_thread(PendingActionStore().fetch_pending, limit=50)
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "queued_episodes": redact_rows(await asyncio.to_thread(_queued_episodes, repo_obj), reveal=rev),
                "successful_episodes": redact_rows(await asyncio.to_thread(_successful_episodes, repo_obj), reveal=rev),
                "failed_episodes": redact_rows(await asyncio.to_thread(_failed_episodes, repo_obj), reveal=rev),
                "recovered_episodes": redact_rows(await asyncio.to_thread(_recovered_episodes, repo_obj), reveal=rev),
                "episodes": redact_rows(episodes, reveal=rev),
                "sessions": redact_rows(await asyncio.to_thread(_recent_sessions, repo_obj), reveal=rev),
                "flagged": redact_rows(await asyncio.to_thread(_flagged_nodes, repo_obj), reveal=rev),
                "candidates": redact_rows(await asyncio.to_thread(_candidates, repo_obj), reveal=rev),
                "entities": redact_rows(await asyncio.to_thread(_search_entities, repo_obj, ""), reveal=rev),
                "pending_actions": redact_rows(pending, reveal=rev),
                "mcp_events": redact_rows(telemetry_store.fetch_recent(limit=50), reveal=rev),
                "initial_episode_uuid": episodes[0]["uuid"] if episodes else "",
                "privacy_redact": not rev,
            },
        )

    @router.get("/explorer/features", response_class=HTMLResponse)
    async def feature_dashboard(
        request: Request,
        window: str = Query(default="all"),
    ) -> HTMLResponse:
        if window not in FEATURE_WINDOWS:
            window = "all"
        return TEMPLATES.TemplateResponse(
            request,
            "features.html",
            {"report": _feature_report(window)},
        )

    @router.get("/explorer/recall-lab", response_class=HTMLResponse)
    async def recall_lab_dashboard(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "recall_lab.html",
            {
                "default_arms": DEFAULT_ARMS,
                "privacy_redact": not _reveal(request),
            },
        )

    @router.post("/explorer/api/recall-lab/run", response_class=JSONResponse)
    async def recall_lab_api(request: Request, body: RecallLabRequest) -> JSONResponse:
        # Above the router-level readonly floor: runs a real recall against the graph and writes
        # a recall_lab_runs row, spending LLM/embedder budget on demand. Not a read.
        _require_explorer_agent()
        recall_service = _runtime_recall_service(request)
        if recall_service is None:
            raise HTTPException(
                status_code=503,
                detail="Recall Lab requires the full Menhir runtime",
            )
        payload = await run_recall_lab(
            recall_service,
            body,
            reveal=_reveal(request),
            judge_llm=_runtime_llm(request),
        )
        store = _recall_lab_store(request)
        run_id = await asyncio.to_thread(
            store.record_recall_lab_run,
            request_payload=body.model_dump(mode="json"),
            result_payload=payload,
        )
        payload["saved"] = run_id is not None
        payload["saved_run_id"] = run_id
        return JSONResponse(payload)

    @router.get("/explorer/api/recall-lab/history", response_class=JSONResponse)
    async def recall_lab_history(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
        rows = await asyncio.to_thread(
            _recall_lab_store(request).fetch_recall_lab_runs,
            limit=limit,
        )
        return JSONResponse({"runs": rows})

    @router.get("/explorer/api/recall-lab/history/{run_id}", response_class=JSONResponse)
    async def recall_lab_history_detail(request: Request, run_id: int) -> JSONResponse:
        row = await asyncio.to_thread(
            _recall_lab_store(request).fetch_recall_lab_run,
            run_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Recall Lab run not found")
        return JSONResponse(row)

    @router.get("/explorer/extraction-lab", response_class=HTMLResponse)
    async def extraction_lab_dashboard(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "extraction_lab.html",
            {
                "privacy_redact": not _reveal(request),
            },
        )

    @router.post("/explorer/api/extraction-lab/run", response_class=JSONResponse)
    async def extraction_lab_api(request: Request, body: ExtractionLabRequest) -> JSONResponse:
        # Above the router-level readonly floor: a real extraction -- LLM calls plus an
        # extraction_lab_runs row. A readonly token must not be able to spend extraction budget.
        _require_explorer_agent()
        payload = await run_extraction_lab(body)
        result_payload = payload.model_dump(mode="json")
        store = _extraction_lab_store(request)
        run_id = await asyncio.to_thread(
            store.record_extraction_lab_run,
            request_payload=body.model_dump(mode="json"),
            result_payload=result_payload,
        )
        result_payload["saved"] = run_id is not None
        result_payload["saved_run_id"] = run_id
        return JSONResponse(result_payload)

    @router.get("/explorer/api/extraction-lab/history", response_class=JSONResponse)
    async def extraction_lab_history(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
        rows = await asyncio.to_thread(
            _extraction_lab_store(request).fetch_extraction_lab_runs,
            limit=limit,
        )
        return JSONResponse({"runs": rows})

    @router.get("/explorer/api/extraction-lab/history/{run_id}", response_class=JSONResponse)
    async def extraction_lab_history_detail(request: Request, run_id: int) -> JSONResponse:
        row = await asyncio.to_thread(_extraction_lab_store(request).fetch_extraction_lab_run, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Extraction Lab run not found")
        return JSONResponse(row)

    @router.get("/explorer/api/features", response_class=JSONResponse)
    async def feature_dashboard_api(
        request: Request,
        window: str = Query(default="all"),
    ) -> JSONResponse:
        if window not in FEATURE_WINDOWS:
            window = "all"
        return JSONResponse(_feature_report(window))

    register_dashboard_routes(router)

    register_bench_routes(router)

    @router.post(
        "/explorer/api/recall-lab/bench-runs/{run_id}/tasks/{namespace}/recall-packet",
        response_class=JSONResponse,
    )
    async def bench_task_query_filtered_packet_api(
        request: Request,
        run_id: str,
        namespace: str,
        body: QueryFilteredPacketRequest,
    ) -> JSONResponse:
        """Select with production recall, then type and budget the selected evidence."""
        # Above the router-level readonly floor. Runs PRODUCTION recall and budgets evidence --
        # the same resource-spending class as the recall/extraction lab endpoints above.
        _require_explorer_agent()
        reader = _bench_task_reader(request)
        task = reader.get_task_detail(run_id, namespace, reveal=_reveal(request))
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        recall_service = _runtime_recall_service(request)
        if recall_service is None:
            raise HTTPException(
                status_code=503,
                detail="Query-filtered packets require the full Menhir runtime",
            )

        recall_request = RecallLabRequest(
            query=body.query,
            namespace=namespace,
            limit=body.limit,
            candidate_k=max(50, body.limit),
            include_session=True,
            include_superseded=False,
            include_invalidated=True,
            judge=False,
            arms=[DEFAULT_ARMS[0]],
        )
        recall_payload = await run_recall_lab(
            recall_service,
            recall_request,
            reveal=_reveal(request),
            judge_llm=None,
        )
        arms = list(recall_payload.get("arms") or [])
        arm = arms[0] if arms else None
        if not arm or not arm.get("ok") or arm.get("degraded"):
            detail = (arm or {}).get("error") or (arm or {}).get("search_error")
            raise HTTPException(
                status_code=502,
                detail=f"Production recall failed: {detail or 'unknown error'}",
            )

        full_packet = (
            (task.get("live_graph") or {}).get("recall_packet")
            or {"version": None, "sections": []}
        )
        packet = build_query_filtered_recall_packet(
            full_packet,
            arm.get("results") or [],
            body.query,
            authority_layer=arm.get("authority_layer") or [],
            event_authority_layer=arm.get("event_authority_layer") or [],
            max_chars=body.max_chars,
            max_general=body.max_general,
        )
        return JSONResponse(
            {
                "contract": QUERY_FILTERED_PACKET_VERSION,
                "run_id": run_id,
                "namespace": namespace,
                "retrieval": {
                    "arm": arm.get("id"),
                    "result_count": len(arm.get("results") or []),
                    "candidates_evaluated": arm.get("candidates_evaluated"),
                    "elapsed_ms": arm.get("elapsed_ms"),
                    "authority_layer": arm.get("authority_layer"),
                    "event_authority_layer": arm.get("event_authority_layer"),
                },
                "packet": packet,
            }
        )

    return router


def create_app(
    *,
    settings: MemorySettings | None = None,
    repo: Neo4jRepository | None = None,
    recall_service: object | None = None,
    llm: object | None = None,
    recall_lab_store: object | None = None,
    extraction_lab_store: object | None = None,
    bench_run_catalog: BenchRunCatalog | None = None,
) -> FastAPI:
    settings = settings or _settings_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_repo = repo is None
        app.state.settings = settings
        app.state.repo = repo or _repo_from_settings(settings)
        app.state.recall_service = recall_service
        app.state.llm = llm
        app.state.recall_lab_store = recall_lab_store
        app.state.extraction_lab_store = extraction_lab_store
        app.state.bench_run_catalog = bench_run_catalog
        app.state.owned_repo = owned_repo
        # SSOT-05: route candidate approval/rejection through the same canonical,
        # contradiction-checked CandidateService the backend/MCP path uses, instead
        # of Explorer's own local Cypher writers. Explorer has no live Graphiti/LLM
        # client by design (it's a small, mostly-read UI) -- UnavailableGraphitiClient
        # makes the contradiction check a safe no-op (LifecycleService and
        # CandidateService both already treat it as best-effort), so approval still
        # gets the same promotion/consistency guarantees even without live search.
        from menhir.core.bootstrap import UnavailableGraphitiClient
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
        from menhir.services.candidate_service import CandidateService
        from menhir.services.lifecycle_service import LifecycleService

        graph_adapter = MemoryGraphAdapter(neo4j=app.state.repo)
        lifecycle_service = LifecycleService(
            graph_adapter=graph_adapter,
            graphiti_client=UnavailableGraphitiClient("explorer: no live Graphiti client"),
        )
        app.state.candidate_service = CandidateService(
            graph_adapter=graph_adapter, lifecycle_service=lifecycle_service,
        )
        yield
        if app.state.owned_repo:
            app.state.repo.close()

    app = FastAPI(title="menhir explorer", version=menhir.__version__, lifespan=lifespan)
    app.mount("/explorer/static", StaticFiles(directory=str(STATIC_DIR)), name="explorer-static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/explorer")

    # Include the explorer router with all /explorer/* routes
    router = create_explorer_router()
    app.include_router(router)

    return app


# NO module-level `app = create_app()` here, deliberately.
#
# It used to exist, commented "for compatibility (tests only)", and no test used it: every test
# either calls the `create_app` FACTORY or patches `menhir.explorer.app.<attr>`, which goes
# through sys.modules and never touches an instance. What it did do was build a complete FastAPI
# application on EVERY import -- and the import chain runs on every production start, via
# api/server_support.py -> explorer/__init__.py -> this module -- so the `explorer_enabled` gate
# 176 lines later could not prevent it. Settings were also read during import, meaning a config
# error in a disabled subsystem could abort startup.
#
# Construct through `create_app()` or `create_explorer_router()`; both are gated by the caller.
