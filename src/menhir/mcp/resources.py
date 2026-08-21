"""Read-only MCP resources and URI templates for menhir."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import UUID

from menhir.config import MemorySettings, redact_uri_credentials

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
from menhir.domain.models import NodeScope, ProcessingState
from menhir.domain.recall import QueryPreset
from menhir.domain.utils import decode_json_value, excerpt
from menhir.mcp.contracts import BaseJsonResource
from menhir.mcp.service_access import get_mcp_session

_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")
_BUILD_ID_CACHE: str | None = None


def _resolve_build_id() -> str:
    global _BUILD_ID_CACHE
    if _BUILD_ID_CACHE is not None:
        return _BUILD_ID_CACHE
    explicit = os.getenv("MEMORY_BUILD_ID", "").strip()
    if explicit:
        _BUILD_ID_CACHE = explicit
        return _BUILD_ID_CACHE
    _here = Path(__file__).resolve()
    repo_dir = str(next((p for p in _here.parents if (p / "pyproject.toml").exists()), _here.parents[3]))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
        build_id = result.stdout.strip()
        if build_id:
            _BUILD_ID_CACHE = build_id
            return _BUILD_ID_CACHE
    except (OSError, subprocess.SubprocessError):
        pass
    _BUILD_ID_CACHE = f"dev-{int(os.path.getmtime(__file__))}"
    return _BUILD_ID_CACHE


def _runtime_fingerprint(provider_config: dict[str, Any], session: Any) -> tuple[str, str]:
    payload = "|".join(
        [
            str(provider_config.get("neo4j_uri") or ""),
            str(provider_config.get("neo4j_database") or "neo4j"),
            str(provider_config.get("local_llm_base_url") or ""),
            str(provider_config.get("local_llm_embed_base_url") or ""),
            str(provider_config.get("chat_model") or ""),
            str(provider_config.get("embed_model") or ""),
            str(provider_config.get("chat_provider") or "local"),
            str(provider_config.get("graphiti_provider") or "local"),
            str(provider_config.get("graphiti_embed_provider") or ""),
            session.session_id,
        ]
    )
    config_fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    build_id = _resolve_build_id()
    return build_id, f"memory:{build_id}:{config_fingerprint}:{session.session_id[:8]}"


def _normalize_memory_row(row: dict[str, Any] | None, *, detail: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    compact = {
        "uuid": row.get("uuid"),
        "name": row.get("name"),
        "type": row.get("type"),
        "scope": row.get("scope"),
        "summary": excerpt(row.get("summary") or row.get("content")),
        "freshness": row.get("freshness"),
        "user_flagged": bool(row.get("user_flagged", False)),
        "created_at": row.get("created_at"),
        "last_accessed": row.get("last_accessed"),
    }
    if not detail:
        return compact
    return {
        **compact,
        "labels": row.get("labels") or [],
        "content": row.get("content"),
        "source": row.get("source"),
        "source_confidence": row.get("source_confidence"),
        "session_id": row.get("session_id"),
        "user_id": row.get("user_id"),
        "processing_state": row.get("processing_state"),
        "processing_stage": row.get("processing_stage"),
        "processing_substage": row.get("processing_substage"),
        "processing_substage_started_at": row.get("processing_substage_started_at"),
        "processing_progress": row.get("processing_progress"),
        "processing_steps_total": row.get("processing_steps_total"),
        "processing_steps_completed": row.get("processing_steps_completed"),
        "processing_llm_tasks_attempt": row.get("processing_llm_tasks_attempt"),
        "processing_llm_tasks_total": row.get("processing_llm_tasks_total"),
        "processing_llm_last_task_at": row.get("processing_llm_last_task_at"),
        "processing_llm_active_task": row.get("processing_llm_active_task"),
        "processing_llm_active_kind": row.get("processing_llm_active_kind"),
        "processing_llm_active_model": row.get("processing_llm_active_model"),
        "processing_llm_active_endpoint": row.get("processing_llm_active_endpoint"),
        "processing_attempts": row.get("processing_attempts"),
        "queued_at": row.get("queued_at"),
        "processing_owner": row.get("processing_owner"),
        "processing_lease_expires_at": row.get("processing_lease_expires_at"),
        "processing_heartbeat_at": row.get("processing_heartbeat_at"),
        "processing_started_at": row.get("processing_started_at"),
        "processing_completed_at": row.get("processing_completed_at"),
        "processing_error": row.get("processing_error"),
        "resolved_episode_uuid": row.get("resolved_episode_uuid"),
        "enriched_nodes_touched": row.get("enriched_nodes_touched"),
        "enriched_edges_touched": row.get("enriched_edges_touched"),
    }


def _normalize_scored_result(scored: Any) -> dict[str, Any]:
    if isinstance(scored, dict):
        breakdown = scored.get("breakdown") or {}
        return {
            "uuid": scored.get("uuid"),
            "name": scored.get("name"),
            "type": scored.get("type") or scored.get("memory_type"),
            "scope": scored.get("scope"),
            "summary": excerpt(scored.get("summary") or scored.get("content")),
            "final_score": scored.get("final_score"),
            "breakdown": {
                "semantic_similarity": breakdown.get("semantic_similarity"),
                "adjacency_bonus": breakdown.get("adjacency_bonus"),
                "recency_bonus": breakdown.get("recency_bonus"),
                "prominence_bonus": breakdown.get("prominence_bonus"),
            },
        }
    breakdown = getattr(scored, "breakdown", None)
    return {
        "uuid": getattr(scored, "uuid", None),
        "name": getattr(scored, "name", None),
        "type": getattr(scored, "memory_type", None),
        "scope": getattr(scored, "scope", None),
        "summary": excerpt(getattr(scored, "summary", None) or getattr(scored, "content", None)),
        "final_score": getattr(scored, "final_score", None),
        "breakdown": {
            "semantic_similarity": getattr(breakdown, "semantic_similarity", None),
            "adjacency_bonus": getattr(breakdown, "adjacency_bonus", None),
            "recency_bonus": getattr(breakdown, "recency_bonus", None),
            "prominence_bonus": getattr(breakdown, "prominence_bonus", None),
        },
    }


def _normalize_processing_row(
    row: dict[str, Any],
    *,
    stale_episode_ids: set[str],
    max_attempts: int,
) -> dict[str, Any]:
    state = str(row.get("processing_state") or "")
    attempts = int(row.get("processing_attempts") or 0)
    stale_lease = str(row.get("uuid") or "") in stale_episode_ids
    exhausted_pending = state.upper() == ProcessingState.PENDING and attempts >= max_attempts
    if stale_lease:
        status_hint = "stale_lease"
    elif exhausted_pending:
        status_hint = "exhausted_pending"
    elif state.upper() == ProcessingState.ENRICHING:
        status_hint = "active"
    else:
        status_hint = "queued"
    return {
        "uuid": row.get("uuid"),
        "state": row.get("processing_state"),
        "stage": row.get("processing_stage"),
        "substage": row.get("processing_substage"),
        "progress": row.get("processing_progress"),
        "attempts": attempts,
        "owner": row.get("processing_owner"),
        "lease_expires_at": row.get("processing_lease_expires_at"),
        "heartbeat_at": row.get("processing_heartbeat_at"),
        "started_at": row.get("processing_started_at"),
        "llm_last_task_at": row.get("processing_llm_last_task_at"),
        "llm_active_task": row.get("processing_llm_active_task"),
        "error": row.get("processing_error"),
        "stale_lease": stale_lease,
        "exhausted_pending": exhausted_pending,
        "status_hint": status_hint,
    }


def _normalize_scheduler_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    lease = snapshot.get("lease") or {}
    return {
        "running": snapshot.get("running"),
        "lease_acquired": lease.get("acquired"),
        "lease_blocked_reason": lease.get("blocked_reason"),
    }


def _socket_reachable(host: str, port: int, *, timeout_s: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _neo4j_dependency_snapshot() -> dict[str, Any]:
    settings = MemorySettings.from_env()
    parsed = urlparse(settings.neo4j_uri)
    host = parsed.hostname or "localhost"
    port = int(parsed.port or 7687)
    reachable = _socket_reachable(host, port)
    return {
        # Redacted, not raw: NEO4J_URI is supported in the `neo4j://user:password@host` form,
        # and resources carry no tier requirement (CF-16), so the lowest authenticated caller
        # reads this payload. Host and port above are already derived, so nothing is lost.
        "uri": redact_uri_credentials(settings.neo4j_uri),
        "host": host,
        "port": port,
        "reachable": reachable,
        "hint": None
        if reachable
        else "Neo4j is not reachable. Start Docker Desktop and the yawn-neo4j container.",
    }


def _require_uuid(node_uuid: str) -> str:
    candidate = (node_uuid or "").strip()
    try:
        return str(UUID(candidate))
    except ValueError as exc:
        raise ValueError(f"Invalid memory UUID: {node_uuid}") from exc


def _require_scope(scope: str) -> str:
    try:
        return NodeScope((scope or "").strip().upper()).value
    except ValueError:
        raise ValueError(f"Invalid scope: {scope!r}. Use SESSION, PERSISTENT, or PROMOTED.")


def _require_term(term: str) -> str:
    normalized = (term or "").strip()
    if not normalized:
        raise ValueError("Search term must not be empty.")
    if len(normalized) > 200:
        raise ValueError("Search term must be 200 characters or fewer.")
    return normalized


def _require_type(memory_type: str) -> str:
    normalized = (memory_type or "").strip()
    if not normalized:
        raise ValueError("Memory type must not be empty.")
    if not _TYPE_PATTERN.fullmatch(normalized):
        raise ValueError("Memory type must be 1-64 characters of letters, digits, spaces, underscores, or hyphens.")
    return normalized


class DependencyHealthResource(BaseJsonResource):
    uri = "memory://system/dependency-health"
    name = "memory-system-dependency-health"
    description = "Lightweight dependency health checks that do not require full service init."

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        return {
            "server": "menhir",
            "neo4j": _neo4j_dependency_snapshot(),
        }


class SystemMetadataResource(BaseJsonResource):
    uri = "memory://system/metadata"
    name = "memory-system-metadata"
    description = "Server runtime metadata and high-level graph counts."

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        backend = self.get_backend()
        session = get_mcp_session()
        overview = await backend.fetch_memory_overview()
        provider_config = await backend.get_provider_config()
        build_id, runtime_fingerprint = _runtime_fingerprint(provider_config, session)
        return {
            "server": "menhir",
            "session": {
                "session_id": session.session_id,
                "user_id": session.user_id,
            },
            "runtime": {
                "build_id": build_id,
                "runtime_fingerprint": runtime_fingerprint,
                "neo4j_uri": provider_config.get("neo4j_uri"),
                "neo4j_database": provider_config.get("neo4j_database", "neo4j"),
                "local_llm_base_url": provider_config.get("local_llm_base_url"),
                "scheduler_url": provider_config.get("scheduler_url"),
                "chat_provider": provider_config.get("chat_provider", "local"),
                "graphiti_provider": provider_config.get("graphiti_provider", "local"),
                "graphiti_embed_provider": provider_config.get("graphiti_embed_provider")
                or provider_config.get("graphiti_provider", "local"),
                "graphiti_reranker_provider": provider_config.get("graphiti_reranker_provider")
                or provider_config.get("graphiti_provider", "local"),
                "chat_model": provider_config.get("chat_model", ""),
                "graphiti_llm_chat_model": provider_config.get("graphiti_llm_chat_model", ""),
                "gemini_chat_model": provider_config.get("gemini_chat_model", ""),
                "embed_model": provider_config.get("embed_model", ""),
                "graphiti_embed_model": provider_config.get("graphiti_embed_model", ""),
                "enrichment_queue_depth": await backend.get_queue_depth(),
                "failed_enrichments": await backend.get_failed_enrichment_count(),
                "scheduler": _normalize_scheduler_snapshot(await backend.scheduler_status_snapshot()),
            },
            "overview": overview,
        }


class RecentMemoriesResource(BaseJsonResource):
    uri = "memory://recent"
    name = "memory-recent"
    description = "Most recently accessed or created memory nodes."

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        backend = self.get_backend()
        rows = await backend.fetch_recent_memories(
            limit=10, namespace=self.pinned_namespace()
        )
        return {"count": len(rows), "items": [_normalize_memory_row(row) for row in rows]}


class LifecycleTraceResource(BaseJsonResource):
    uri = "memory://system/lifecycle-trace"
    name = "memory-system-lifecycle-trace"
    description = "Recent lifecycle/debug events captured by the MCP telemetry sidecar."

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        backend = self.get_backend()
        rows = await backend.fetch_recent_lifecycle_events(limit=100)
        return {
            "count": len(rows),
            "items": [
                {
                    "recorded_at": row.get("recorded_at"),
                    "component": row.get("component"),
                    "event": row.get("event"),
                    "state": row.get("state"),
                    "episode_uuid": row.get("episode_uuid"),
                    "details": decode_json_value(row.get("details_json")),
                }
                for row in rows
            ],
        }


class ProcessingQueueResource(BaseJsonResource):
    uri = "memory://system/processing-queue"
    name = "memory-system-processing-queue"
    description = "Compact processing queue snapshot with stale/exhausted hints."

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        backend = self.get_backend()
        rows = await backend.list_episode_processing(states=["PENDING", "ENRICHING"], limit=50)
        stale_rows = await backend.fetch_stale_enriching_episodes(limit=50)
        stale_episode_ids = {str(row.get("uuid") or "") for row in stale_rows}
        max_attempts = await backend.get_max_enrichment_attempts()
        items = [
            _normalize_processing_row(
                row,
                stale_episode_ids=stale_episode_ids,
                max_attempts=max_attempts,
            )
            for row in rows
        ]
        return {
            "count": len(items),
            "queue_depth": await backend.get_queue_depth(),
            "max_attempts": max_attempts,
            "counts": {
                "pending": sum(1 for row in items if row["state"] == "PENDING"),
                "enriching": sum(1 for row in items if row["state"] == "ENRICHING"),
                "stale_enriching": sum(1 for row in items if row["stale_lease"]),
                "exhausted_pending": sum(1 for row in items if row["exhausted_pending"]),
            },
            "items": items,
        }


class MemoryByUuidResource(BaseJsonResource):
    uri = "memory://node/{node_uuid}"
    name = "memory-by-uuid"
    description = "Resolve a single memory node by UUID."

    async def endpoint(self, node_uuid: str) -> dict[str, Any]:
        return await self.build_payload(node_uuid)

    async def build_payload(self, node_uuid: str) -> dict[str, Any]:
        backend = self.get_backend()
        normalized_uuid = _require_uuid(node_uuid)
        row = await backend.fetch_memory_by_uuid(
            normalized_uuid, namespace=self.pinned_namespace()
        )
        return {
            "found": row is not None,
            "memory": _normalize_memory_row(row, detail=True),
        }

    def call_payload(self, node_uuid: str) -> dict[str, Any]:
        return {"node_uuid": node_uuid}


class MemoriesByScopeResource(BaseJsonResource):
    uri = "memory://scope/{scope}"
    name = "memories-by-scope"
    description = "List memories filtered by scope."

    async def endpoint(self, scope: str) -> dict[str, Any]:
        return await self.build_payload(scope)

    async def build_payload(self, scope: str) -> dict[str, Any]:
        backend = self.get_backend()
        normalized_scope = _require_scope(scope)
        rows = await backend.fetch_memories_by_scope(
            normalized_scope, limit=10, namespace=self.pinned_namespace()
        )
        return {
            "scope": normalized_scope,
            "count": len(rows),
            "items": [_normalize_memory_row(row) for row in rows],
        }

    def call_payload(self, scope: str) -> dict[str, Any]:
        return {"scope": scope}


class MemoriesBySearchResource(BaseJsonResource):
    uri = "memory://search/{term}"
    name = "memories-by-search"
    description = "Recall memories for a natural-language search term."

    async def endpoint(self, term: str) -> dict[str, Any]:
        return await self.build_payload(term)

    async def build_payload(self, term: str) -> dict[str, Any]:
        backend = self.get_backend()
        normalized_term = _require_term(term)
        result = await backend.recall(
            normalized_term,
            preset=QueryPreset.KNOWLEDGE,
            limit=5,
            include_session=True,
            wait_for_pending=True,
            namespace=self.pinned_namespace(),
        )
        return {
            "query": normalized_term,
            "preset": QueryPreset.KNOWLEDGE.value,
            "count": len(result.get("results", [])),
            "candidates_evaluated": result.get("candidates_evaluated"),
            "items": [_normalize_scored_result(row) for row in result.get("results", [])],
        }

    def call_payload(self, term: str) -> dict[str, Any]:
        return {"term": term}


class MemoriesByTypeResource(BaseJsonResource):
    uri = "memory://type/{memory_type}"
    name = "memories-by-type"
    description = "List entity memories filtered by type."

    async def endpoint(self, memory_type: str) -> dict[str, Any]:
        return await self.build_payload(memory_type)

    async def build_payload(self, memory_type: str) -> dict[str, Any]:
        backend = self.get_backend()
        normalized_type = _require_type(memory_type)
        rows = await backend.fetch_memories_by_type(
            normalized_type, limit=10, namespace=self.pinned_namespace()
        )
        return {
            "type": normalized_type,
            "count": len(rows),
            "items": [_normalize_memory_row(row) for row in rows],
        }

    def call_payload(self, memory_type: str) -> dict[str, Any]:
        return {"memory_type": memory_type}


RESOURCE_TYPES = [
    DependencyHealthResource,
    SystemMetadataResource,
    RecentMemoriesResource,
    LifecycleTraceResource,
    ProcessingQueueResource,
    MemoryByUuidResource,
    MemoriesByScopeResource,
    MemoriesBySearchResource,
    MemoriesByTypeResource,
]


def register_memory_resources(mcp: FastMCP) -> None:
    """Register concrete resources and URI-template resources for memory reads."""

    for resource_type in RESOURCE_TYPES:
        resource_type().register(mcp)
