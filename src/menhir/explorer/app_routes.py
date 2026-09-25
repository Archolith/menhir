"""Explorer dashboard partials, detail views, candidate actions, and privacy toggle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from menhir.explorer.app_privacy import _redact_detail, _redact_graph_elements, _redact_session, _reveal
from menhir.explorer.app_queries import (
    _candidates,
    _failed_episodes,
    _flagged_nodes,
    _graph_elements,
    _node_detail,
    _queued_episodes,
    _recent_episodes,
    _recent_sessions,
    _recovered_episodes,
    _search_entities,
    _session_detail,
    _session_graph_elements,
    _successful_episodes,
)
from menhir.explorer.app_runtime import _require_explorer_agent
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.mcp.telemetry import telemetry_store
from menhir.privacy import redact_rows

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
STATIC_DIR = Path(__file__).with_name("static")


def register_dashboard_routes(router: APIRouter) -> None:
    """Register the dashboard partial, detail, candidate, and privacy routes."""
    @router.get("/explorer/partials/queue", response_class=HTMLResponse)
    async def queue_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_queue.html",
            {"queued_episodes": redact_rows(await asyncio.to_thread(_queued_episodes, request.app.state.repo), reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/failed", response_class=HTMLResponse)
    async def failed_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_failed.html",
            {"failed_episodes": redact_rows(await asyncio.to_thread(_failed_episodes, request.app.state.repo), reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/successful", response_class=HTMLResponse)
    async def successful_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_successful.html",
            {"successful_episodes": redact_rows(await asyncio.to_thread(_successful_episodes, request.app.state.repo), reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/recovered", response_class=HTMLResponse)
    async def recovered_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_recovered.html",
            {"recovered_episodes": redact_rows(await asyncio.to_thread(_recovered_episodes, request.app.state.repo), reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/pending_actions", response_class=HTMLResponse)
    async def pending_actions_partial(request: Request) -> HTMLResponse:
        pending = await asyncio.to_thread(PendingActionStore().fetch_pending, limit=50)
        return TEMPLATES.TemplateResponse(
            request,
            "_pending_actions.html",
            {"pending_actions": redact_rows(pending, reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/mcp_events", response_class=HTMLResponse)
    async def mcp_events_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_mcp_events.html",
            {"mcp_events": redact_rows(telemetry_store.fetch_recent(limit=50), reveal=_reveal(request))},
        )

    @router.get("/explorer/partials/episodes", response_class=HTMLResponse)
    async def episodes_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_episodes.html", {"episodes": redact_rows(await asyncio.to_thread(_recent_episodes, request.app.state.repo), reveal=_reveal(request))})

    @router.get("/explorer/partials/sessions", response_class=HTMLResponse)
    async def sessions_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_sessions.html", {"sessions": redact_rows(await asyncio.to_thread(_recent_sessions, request.app.state.repo), reveal=_reveal(request))})

    @router.get("/explorer/partials/flagged", response_class=HTMLResponse)
    async def flagged_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_flagged.html", {"flagged": redact_rows(await asyncio.to_thread(_flagged_nodes, request.app.state.repo), reveal=_reveal(request))})

    @router.get("/explorer/partials/candidates", response_class=HTMLResponse)
    async def candidates_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_candidates.html", {"candidates": redact_rows(await asyncio.to_thread(_candidates, request.app.state.repo), reveal=_reveal(request))})

    @router.post("/explorer/candidates/{uuid}/approve", response_class=JSONResponse)
    async def approve_candidate(request: Request, uuid: str) -> JSONResponse:
        # Above the router-level readonly floor: mutates graph state by promoting a candidate
        # into the memory graph.
        _require_explorer_agent()
        result = await request.app.state.candidate_service.approve(uuid)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Candidate not found")
        return JSONResponse(result)

    @router.post("/explorer/candidates/{uuid}/reject", response_class=JSONResponse)
    async def reject_candidate(request: Request, uuid: str) -> JSONResponse:
        # Above the router-level readonly floor: mutates graph state by rejecting a candidate,
        # which is durable and not reversible from this UI.
        _require_explorer_agent()
        result = await request.app.state.candidate_service.reject(uuid)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Candidate not found")
        return JSONResponse(result)

    @router.get("/explorer/partials/entities", response_class=HTMLResponse)
    async def entities_partial(request: Request, query: str = Query(default="", max_length=120)) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_entities.html",
            {"entities": redact_rows(await asyncio.to_thread(_search_entities, request.app.state.repo, query), reveal=_reveal(request)), "query": query},
        )

    @router.get("/explorer/partials/node/{uuid}", response_class=HTMLResponse)
    async def node_detail_partial(request: Request, uuid: str) -> HTMLResponse:
        detail = await asyncio.to_thread(_node_detail, request.app.state.repo, uuid)
        if detail is None:
            raise HTTPException(status_code=404, detail="Node not found")
        detail = _redact_detail(detail, reveal=_reveal(request))
        return TEMPLATES.TemplateResponse(request, "_detail.html", {"detail": detail})

    @router.get("/explorer/partials/session/{session_id}", response_class=HTMLResponse)
    async def session_detail_partial(request: Request, session_id: str) -> HTMLResponse:
        detail = await asyncio.to_thread(_session_detail, request.app.state.repo, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        detail = _redact_session(detail, reveal=_reveal(request))
        return TEMPLATES.TemplateResponse(request, "_session_detail.html", {"session": detail})

    @router.get("/explorer/api/graph/{uuid}", response_class=JSONResponse)
    async def graph_api(request: Request, uuid: str, depth: int = Query(default=1, ge=1, le=2)) -> JSONResponse:
        if await asyncio.to_thread(_node_detail, request.app.state.repo, uuid) is None:
            raise HTTPException(status_code=404, detail="Node not found")
        elements = await asyncio.to_thread(_graph_elements, request.app.state.repo, uuid, depth)
        return JSONResponse({"elements": _redact_graph_elements(elements, reveal=_reveal(request))})

    @router.get("/explorer/api/session/{session_id}", response_class=JSONResponse)
    async def session_graph_api(request: Request, session_id: str) -> JSONResponse:
        detail = await asyncio.to_thread(_session_detail, request.app.state.repo, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        elements = await asyncio.to_thread(_session_graph_elements, request.app.state.repo, session_id)
        return JSONResponse({"elements": _redact_graph_elements(elements, reveal=_reveal(request))})

    @router.post("/explorer/privacy/{state}", response_class=JSONResponse)
    async def set_privacy(request: Request, state: str) -> JSONResponse:
        """Per-browser privacy toggle via the ``menhir_reveal`` cookie.

        ``state=hide`` sets reveal=0 (redact); ``state=reveal`` sets reveal=1 (show, honored
        only on a loopback bind by ``_reveal``). Returns the effective state.

        DELIBERATELY left at the router's readonly floor despite being a POST. It mutates a
        per-browser cookie, not server state, and the direction that matters -- reveal -- is
        independently gated: ``_reveal`` honours the cookie only on a loopback bind, so setting
        it from a proxied request grants nothing. Escalating this to agent would restrict
        turning redaction ON, which is the safe direction.
        """
        if state not in ("hide", "reveal"):
            raise HTTPException(status_code=400, detail="state must be 'hide' or 'reveal'")
        resp = JSONResponse({"redact": state == "hide"})
        resp.set_cookie("menhir_reveal", "1" if state == "reveal" else "0", httponly=False, samesite="strict")
        return resp
