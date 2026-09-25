"""Read-only MCP resources and URI templates for menhir."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from menhir.config import MemorySettings, redact_uri_credentials

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
from menhir.domain.recall import QueryPreset
from menhir.domain.utils import decode_json_value
from menhir.mcp.contracts import BaseJsonResource
from menhir.mcp.resources_helpers import (
    _normalize_memory_row,
    _normalize_processing_row,
    _normalize_scheduler_snapshot,
    _normalize_scored_result,
    _require_scope,
    _require_term,
    _require_type,
    _require_uuid,
    _runtime_fingerprint,
)
from menhir.mcp.service_access import get_mcp_session

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
        else "Neo4j is not reachable. Start Docker and the menhir-neo4j container "
        "(`menhir up --compose-neo4j`).",
    }


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
