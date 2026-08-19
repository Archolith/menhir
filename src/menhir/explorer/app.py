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

# Usage-dashboard time windows -> lookback hours (None = all-time history).
FEATURE_WINDOWS: dict[str, int | None] = {"7d": 168, "30d": 720, "all": None}


class QueryFilteredPacketRequest(BaseModel):
    """Bounded request for a production-selected, typed benchmark packet."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=20)
    max_chars: int = Field(default=6_000, ge=2_000, le=16_000)
    max_general: int = Field(default=4, ge=0, le=10)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
STATIC_DIR = Path(__file__).with_name("static")


def _settings_from_env() -> MemorySettings:
    return MemorySettings.from_env()


def _repo_from_settings(settings: MemorySettings) -> Neo4jRepository:
    return Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )


def _scope_badge(scope: str | None) -> str:
    return (scope or "unknown").lower()


def _node_kind(labels: list[str]) -> str:
    if "Episodic" in labels:
        return "episode"
    if "Entity" in labels:
        return "entity"
    return (labels[0] if labels else "node").lower()


def _graph_node_class(labels: list[str], scope: str | None) -> str:
    return f"{_node_kind(labels)} {_scope_badge(scope)}"


def _recent_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            toString(e.created_at) AS created_at,
            left(coalesce(e.content, ''), 140) AS preview,
            coalesce(e.enriched_nodes_touched, 0) AS nodes_touched,
            coalesce(e.enriched_edges_touched, 0) AS edges_touched
        ORDER BY e.created_at DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _queued_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state IN ['PENDING', 'ENRICHING']
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_state AS processing_state,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_steps_total), 0) AS processing_steps_total,
            coalesce(toInteger(e.processing_steps_completed), 0) AS processing_steps_completed,
            coalesce(toInteger(e.processing_llm_tasks_attempt), 0) AS processing_llm_tasks_attempt,
            coalesce(toInteger(e.processing_llm_tasks_total), 0) AS processing_llm_tasks_total,
            toString(e.processing_llm_last_task_at) AS processing_llm_last_task_at,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_started_at, e.queued_at, e.created_at)) AS queued_at,
            toString(e.processing_heartbeat_at) AS processing_heartbeat_at,
            e.processing_error AS processing_error,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_started_at, e.queued_at, e.created_at) ASC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _failed_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'FAILED'
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_state AS processing_state,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.processing_started_at, e.queued_at, e.created_at)) AS failed_at,
            e.processing_error AS processing_error,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.processing_started_at, e.queued_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _recovered_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'READY' AND toInteger(e.processing_attempts) > 1
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.created_at)) AS recovered_at,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _successful_episodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (e:Episodic)
        WHERE e.processing_state = 'READY' AND coalesce(toInteger(e.processing_attempts), 0) <= 1
        RETURN
            e.uuid AS uuid,
            coalesce(e.name, e.uuid) AS name,
            e.session_id AS session_id,
            e.source AS source,
            e.processing_stage AS processing_stage,
            coalesce(toFloat(e.processing_progress), 0.0) AS processing_progress,
            coalesce(toInteger(e.processing_attempts), 0) AS processing_attempts,
            toString(coalesce(e.processing_completed_at, e.created_at)) AS completed_at,
            left(coalesce(e.content, ''), 140) AS preview
        ORDER BY coalesce(e.processing_completed_at, e.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _recent_sessions(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n)
        WHERE n.session_id IS NOT NULL
        RETURN
            n.session_id AS session_id,
            count(DISTINCT n) AS node_count,
            sum(CASE WHEN n:Episodic THEN 1 ELSE 0 END) AS episode_count,
            max(toString(n.created_at)) AS last_seen
        ORDER BY last_seen DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _flagged_nodes(repo: Neo4jRepository, limit: int = 12) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n)
        WHERE coalesce(n.user_flagged, false) = true
        RETURN
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS name,
            labels(n) AS labels,
            n.scope AS scope,
            n.session_id AS session_id
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        params={"limit": limit},
    )


def _candidates(repo: Neo4jRepository, limit: int = 50) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n:Entity)
        WHERE n.scope = 'CANDIDATE'
        RETURN n.uuid AS uuid, n.name AS name, n.content AS content, n.source AS source,
               n.candidate_cluster_id AS cluster_id, n.candidate_kind AS kind,
               n.candidate_type AS type, n.candidate_label AS label,
               n.candidate_evidence_strength AS evidence_strength,
               n.candidate_distinct_sessions AS distinct_sessions,
               n.candidate_first_seen AS first_seen, n.candidate_last_seen AS last_seen,
               n.candidate_notes AS notes, n.created_at AS created_at
        ORDER BY coalesce(n.candidate_last_seen, n.created_at) DESC
        LIMIT $limit
        """,
        params={"limit": int(limit)},
    )


def _search_entities(repo: Neo4jRepository, query: str, limit: int = 25) -> list[dict[str, Any]]:
    return repo.execute(
        """
        MATCH (n:Entity)
        WHERE $search_term = ''
           OR toLower(coalesce(n.name, '')) CONTAINS toLower($search_term)
           OR toLower(coalesce(n.summary, '')) CONTAINS toLower($search_term)
        RETURN
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS name,
            n.scope AS scope,
            n.session_id AS session_id,
            n.source AS source,
            coalesce(n.user_flagged, false) AS user_flagged,
            toString(n.last_accessed) AS last_accessed,
            COUNT { (n)-[]-() } AS connection_count,
            left(coalesce(n.summary, ''), 80) AS summary_preview
        ORDER BY connection_count DESC, coalesce(n.user_flagged, false) DESC, n.name ASC
        LIMIT $limit
        """,
        params={"search_term": query.strip(), "limit": limit},
    )


def _node_detail(repo: Neo4jRepository, uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        """
        MATCH (n {uuid: $uuid})
        OPTIONAL MATCH (n)-[r]-(neighbor)
        WITH n, collect(DISTINCT {
            uuid: neighbor.uuid,
            name: coalesce(neighbor.name, neighbor.uuid),
            labels: labels(neighbor),
            scope: neighbor.scope,
            rel_type: type(r)
        })[0..12] AS neighbors
        OPTIONAL MATCH (e:Episodic)-[:MENTIONS]-(n)
        WITH n, neighbors, collect(DISTINCT {
            uuid: e.uuid,
            name: coalesce(e.name, e.uuid),
            created_at: toString(e.created_at),
            session_id: e.session_id
        })[0..10] AS episodes
        RETURN {
            uuid: n.uuid,
            name: coalesce(n.name, n.uuid),
            labels: labels(n),
            scope: n.scope,
            session_id: n.session_id,
            user_id: n.user_id,
            source: n.source,
            user_flagged: coalesce(n.user_flagged, false),
            freshness: n.freshness,
            sharpness: n.sharpness,
            emotions: n.emotions,
            nodes_touched: n.enriched_nodes_touched,
            edges_touched: n.enriched_edges_touched,
            created_at: toString(n.created_at),
            last_accessed: toString(n.last_accessed),
            content: n.content,
            summary: n.summary,
            neighbors: [entry IN neighbors WHERE entry.uuid IS NOT NULL],
            episodes: [entry IN episodes WHERE entry.uuid IS NOT NULL]
        } AS detail
        """,
        params={"uuid": uuid},
    )
    if not rows:
        return None
    return rows[0]["detail"]


def _session_detail(repo: Neo4jRepository, session_id: str) -> dict[str, Any] | None:
    rows = repo.execute(
        """
        MATCH (n)
        WHERE n.session_id = $session_id
        RETURN
            $session_id AS session_id,
            count(DISTINCT n) AS node_count,
            sum(CASE WHEN n:Episodic THEN 1 ELSE 0 END) AS episode_count,
            collect(DISTINCT {
                uuid: n.uuid,
                name: coalesce(n.name, n.uuid),
                labels: labels(n),
                scope: n.scope,
                source: n.source
            })[0..30] AS nodes
        """,
        params={"session_id": session_id},
    )
    if not rows or rows[0]["node_count"] == 0:
        return None
    return rows[0]


def _graph_elements(repo: Neo4jRepository, uuid: str, depth: int) -> dict[str, list[dict[str, Any]]]:
    clamped_depth = max(1, min(depth, 2))
    node_query = f"""
        MATCH (seed {{uuid: $uuid}})
        OPTIONAL MATCH p=(seed)-[*1..{clamped_depth}]-(neighbor)
        WITH seed, collect(DISTINCT p) AS paths
        UNWIND CASE WHEN size(paths) = 0 THEN [null] ELSE paths END AS path
        UNWIND CASE WHEN path IS NULL THEN [seed] ELSE nodes(path) END AS node
        RETURN DISTINCT
            node.uuid AS uuid,
            coalesce(node.name, node.uuid) AS label,
            labels(node) AS labels,
            node.scope AS scope
    """
    edge_query = f"""
        MATCH (seed {{uuid: $uuid}})
        OPTIONAL MATCH p=(seed)-[*1..{clamped_depth}]-(neighbor)
        WITH collect(DISTINCT p) AS paths
        UNWIND CASE WHEN size(paths) = 0 THEN [null] ELSE paths END AS path
        UNWIND CASE WHEN path IS NULL THEN [] ELSE relationships(path) END AS rel
        RETURN DISTINCT
            rel.uuid AS uuid,
            type(rel) AS rel_type,
            startNode(rel).uuid AS source,
            endNode(rel).uuid AS target,
            rel.scope AS scope
    """
    node_rows = repo.execute(node_query, params={"uuid": uuid})
    edge_rows = repo.execute(edge_query, params={"uuid": uuid})
    return {
        "nodes": [
            {
                "data": {"id": row["uuid"], "label": row["label"], "scope": row["scope"] or "unknown"},
                "classes": _graph_node_class(row["labels"], row["scope"]),
            }
            for row in node_rows
            if row.get("uuid")
        ],
        "edges": [
            {
                "data": {
                    "id": row["uuid"] or f"{row['source']}::{row['target']}::{row['rel_type']}",
                    "source": row["source"],
                    "target": row["target"],
                    "label": row["rel_type"],
                    "scope": row["scope"] or "unknown",
                }
            }
            for row in edge_rows
            if row.get("source") and row.get("target")
        ],
    }


def _session_graph_elements(
    repo: Neo4jRepository,
    session_id: str,
) -> dict[str, list[dict[str, Any]]]:
    node_rows = repo.execute(
        """
        MATCH (n)
        WHERE n.session_id = $session_id
        RETURN DISTINCT
            n.uuid AS uuid,
            coalesce(n.name, n.uuid) AS label,
            labels(n) AS labels,
            n.scope AS scope
        ORDER BY label ASC
        """,
        params={"session_id": session_id},
    )
    edge_rows = repo.execute(
        """
        MATCH (a)-[r]-(b)
        WHERE a.session_id = $session_id AND b.session_id = $session_id
        RETURN DISTINCT
            r.uuid AS uuid,
            type(r) AS rel_type,
            a.uuid AS source,
            b.uuid AS target,
            r.scope AS scope
        """,
        params={"session_id": session_id},
    )
    return {
        "nodes": [
            {
                "data": {"id": row["uuid"], "label": row["label"], "scope": row["scope"] or "unknown"},
                "classes": _graph_node_class(row["labels"], row["scope"]),
            }
            for row in node_rows
            if row.get("uuid")
        ],
        "edges": [
            {
                "data": {
                    "id": row["uuid"] or f"{row['source']}::{row['target']}::{row['rel_type']}",
                    "source": row["source"],
                    "target": row["target"],
                    "label": row["rel_type"],
                    "scope": row["scope"] or "unknown",
                }
            }
            for row in edge_rows
            if row.get("source") and row.get("target")
        ],
    }


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


def _reveal(request: Request) -> bool:
    """Resolve whether memory content should be shown (True) or redacted (False).

    Baseline comes from ``settings.privacy_redact`` (redact when True). A per-browser
    ``menhir_reveal`` cookie can override: ``"1"`` reveals but ONLY on a loopback bind
    (privacy must not be defeatable by a cookie on a remote bind); ``"0"`` forces redaction.
    """
    settings = getattr(request.app.state, "settings", None)
    base_redact = bool(getattr(settings, "privacy_redact", False))
    reveal = not base_redact
    cookie = request.cookies.get("menhir_reveal")
    loopback = is_loopback_host(getattr(settings, "api_host", "127.0.0.1"))
    if cookie == "1" and loopback:
        reveal = True
    elif cookie == "0":
        reveal = False
    return reveal


def _redact_detail(detail: dict[str, Any] | None, *, reveal: bool) -> dict[str, Any] | None:
    """Redact a node-detail mapping, including nested neighbor/episode names."""
    if reveal or not detail:
        return detail
    out = redact_mapping(detail, reveal=False)
    if isinstance(out.get("neighbors"), list):
        out["neighbors"] = redact_rows(out["neighbors"], reveal=False)
    if isinstance(out.get("episodes"), list):
        out["episodes"] = redact_rows(out["episodes"], reveal=False)
    return out


def _redact_session(session: dict[str, Any] | None, *, reveal: bool) -> dict[str, Any] | None:
    """Redact a session-detail mapping's node names."""
    if reveal or not session:
        return session
    out = dict(session)
    if isinstance(out.get("nodes"), list):
        out["nodes"] = redact_rows(out["nodes"], reveal=False)
    return out


def _redact_graph_elements(
    elements: dict[str, list[dict[str, Any]]], *, reveal: bool
) -> dict[str, list[dict[str, Any]]]:
    """Mask node display labels in a cytoscape elements payload.

    Node ``data.label`` is the memory name (redacted). Edge ``data.label`` is the
    relationship type (structural) and is preserved so the graph stays readable.
    """
    if reveal:
        return elements
    from menhir.privacy import MASK

    for node in elements.get("nodes", []):
        data = node.get("data")
        if isinstance(data, dict) and isinstance(data.get("label"), str) and data["label"].strip():
            data["label"] = MASK
    return elements


def _runtime_recall_service(request: Request) -> object | None:
    """Return the canonical runtime RecallService, never a dashboard-owned copy."""
    direct = getattr(request.app.state, "recall_service", None)
    if direct is not None:
        return direct
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    built = getattr(runtime_ctx, "built", None)
    return getattr(built, "recall_service", None)


def _runtime_llm(request: Request) -> object | None:
    direct = getattr(request.app.state, "llm", None)
    if direct is not None:
        return direct
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    built = getattr(runtime_ctx, "built", None)
    return getattr(built, "llm", None)


def _recall_lab_store(request: Request) -> object:
    return getattr(request.app.state, "recall_lab_store", None) or telemetry_store


def _extraction_lab_store(request: Request) -> object:
    return getattr(request.app.state, "extraction_lab_store", None) or telemetry_store


def _require_explorer_readonly() -> None:
    """Router-level floor for every explorer route.

    The explorer mounts into the SAME FastAPI app as the API and sits behind the same bearer
    middleware, so a caller needed a valid token -- but tier was never consulted, so any
    authenticated token reached all 36 routes regardless of its rank. Reuses the API's own
    `_require_tier` so the two surfaces cannot drift apart in how they rank tiers or shape the
    403.
    """
    from menhir.api.routes_support import _require_tier

    _require_tier("readonly")


def _require_explorer_agent() -> None:
    """Floor for explorer routes that MUTATE or spend real resources.

    Applied in the route body rather than as a second router dependency, because it applies to
    six routes rather than to the router. Read the individual routes for why each one is here.
    """
    from menhir.api.routes_support import _require_tier

    _require_tier("agent")


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
