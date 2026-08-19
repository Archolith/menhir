import asyncio
import json
import shutil
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from menhir.mcp import lifecycle as mcp_server_lifecycle
from menhir.mcp import resources as mcp_resources
from menhir.mcp import server as mcp_server
from menhir.mcp.telemetry import McpTelemetryStore
from menhir.mcp.tools.ingest import add_memory as _add_memory_mod
from menhir.mcp.tools.ingest.add_memory import add_memory
from menhir.mcp.tools.ingest.add_memory_and_track import add_memory_and_track
from menhir.mcp.tools.conflict.list_conflicts import list_conflicts
from menhir.mcp.tools.conflict.resolve_conflict import resolve_conflict
from menhir.mcp.tools.ops.force_release_lease import force_release_enrichment_lease
from menhir.mcp.tools.ops.force_scheduler_takeover import force_scheduler_takeover
from menhir.mcp.tools.ops.get_enrichment_status import get_enrichment_status
from menhir.mcp.tools.ops import get_episode_trace as _get_episode_trace_mod
from menhir.mcp.tools.ops.get_episode_trace import get_episode_trace
from menhir.mcp.tools.ops.list_enrichment_queue import list_enrichment_queue
from menhir.mcp.tools.ops.repair_stale_enrichment import repair_stale_enrichment
from menhir.mcp.tools.ops.watch_enrichment import watch_enrichment
from menhir.mcp.tools.recall.read_flagged_memories import read_flagged_memories
from menhir.mcp.tools.recall.recall_context_memories import recall_context_memories


_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".agent" / "test_tmp"


def _make_db_path() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = _TEST_TMP_ROOT / f"mcp-server-{uuid4()}"
    run_dir.mkdir()
    return run_dir / "mcp_telemetry.db"


def _cleanup_db_path(db_path: Path) -> None:
    shutil.rmtree(db_path.parent, ignore_errors=True)


class StubGraphAdapter:
    def __init__(self) -> None:
        self.scope_calls: list[str] = []
        self.type_calls: list[str] = []
        self.conflict_resolve_calls: list[dict[str, object]] = []
        self.flagged_version = "1:stubflagged"
        self.episode_rows: dict[str, dict[str, object]] = {
            "123e4567-e89b-12d3-a456-426614174998": {
                "uuid": "123e4567-e89b-12d3-a456-426614174998",
                "processing_state": "ENRICHING",
                "processing_attempts": 2,
                "processing_error": None,
                "processing_started_at": "2026-03-06T00:00:01",
                "processing_completed_at": None,
                "processing_lease_expires_at": None,
                "processing_heartbeat_at": "2026-03-06T00:00:10",
                "processing_stage": "graphiti_extracting",
                "processing_substage": "llm_activity_observed",
                "processing_substage_started_at": "2026-03-06T00:00:08",
                "processing_progress": 22.5,
                "processing_steps_total": 5,
                "processing_steps_completed": 1,
                "processing_llm_tasks_attempt": 3,
                "processing_llm_tasks_total": 9,
                "processing_llm_last_task_at": "2026-03-06T00:00:09",
                "processing_llm_active_task": "memory: graphiti add_episode",
                "processing_llm_active_kind": "chat",
                "processing_llm_active_model": "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf",
                "processing_llm_active_endpoint": "chat.completions.create",
                "processing_owner": None,
                "resolved_episode_uuid": None,
                "queued_at": "2026-03-06T00:00:00",
                "created_at": "2026-03-06T00:00:00",
                "session_id": "session-123",
                "source": "claude-code",
            },
            "123e4567-e89b-12d3-a456-426614174999": {
                "uuid": "123e4567-e89b-12d3-a456-426614174999",
                "processing_state": "READY",
                "processing_attempts": 1,
                "processing_error": None,
                "processing_started_at": "2026-03-06T00:00:01",
                "processing_completed_at": "2026-03-06T00:00:02",
                "processing_lease_expires_at": "2026-03-06T00:01:00",
                "processing_heartbeat_at": "2026-03-06T00:00:02",
                "processing_stage": "ready",
                "processing_substage": "complete",
                "processing_substage_started_at": "2026-03-06T00:00:02",
                "processing_progress": 100.0,
                "processing_steps_total": 5,
                "processing_steps_completed": 5,
                "processing_llm_tasks_attempt": 4,
                "processing_llm_tasks_total": 4,
                "processing_llm_last_task_at": "2026-03-06T00:00:02",
                "processing_llm_active_task": None,
                "processing_llm_active_kind": None,
                "processing_llm_active_model": None,
                "processing_llm_active_endpoint": None,
                "processing_owner": None,
                "resolved_episode_uuid": "episode-graphiti-1",
                "enriched_nodes_touched": 2,
                "enriched_edges_touched": 2,
                "queued_at": "2026-03-06T00:00:00",
                "created_at": "2026-03-06T00:00:00",
                "session_id": "session-123",
                "source": "claude-code",
            }
        }
        self.conflict_groups: list[dict[str, object]] = [
            {
                "group_id": "group-1",
                "created_at": "2026-03-10T12:00:00Z",
                "members": [
                    {
                        "uuid": "123e4567-e89b-12d3-a456-426614174111",
                        "name": "Older fact",
                        "content": "cth.mcp.memory uses scheduler at 8082.",
                        "status": "unresolved",
                        "scope": "PERSISTENT",
                        "node_created_at": "2026-03-10T11:00:00Z",
                    },
                    {
                        "uuid": "123e4567-e89b-12d3-a456-426614174112",
                        "name": "Newer fact",
                        "content": "cth.mcp.memory uses LiteLLM on 4000.",
                        "status": "unresolved",
                        "scope": "PERSISTENT",
                        "node_created_at": "2026-03-10T11:30:00Z",
                    },
                ],
            }
        ]

    def fetch_memory_overview(self):
        return {
            "total_memories": 3,
            "entity_count": 2,
            "episode_count": 1,
            "flagged_count": 1,
            "session_count": 1,
            "persistent_count": 1,
            "promoted_count": 0,
        }

    def fetch_recent_memories(self, limit: int = 10):
        return [
            {
                "uuid": "123e4567-e89b-12d3-a456-426614174000",
                "labels": ["Entity"],
                "name": "Miso",
                "type": "pet",
                "scope": "PERSISTENT",
                "content": "Dog named Miso",
                "summary": "Dog named Miso",
                "source": "claude-code",
                "source_confidence": 0.9,
                "user_flagged": True,
                "session_id": None,
                "user_id": "claude-code",
                "created_at": "2026-03-06T00:00:00",
                "last_accessed": "2026-03-06T01:00:00",
                "freshness": "ACTIVE",
                "processing_state": "READY",
                "processing_stage": "ready",
                "processing_substage": "complete",
                "processing_substage_started_at": "2026-03-06T00:00:02",
                "processing_progress": 100.0,
                "processing_steps_total": 5,
                "processing_steps_completed": 5,
                "processing_llm_tasks_attempt": 4,
                "processing_llm_tasks_total": 4,
                "processing_llm_last_task_at": "2026-03-06T00:00:02",
                "processing_llm_active_task": None,
                "processing_llm_active_kind": None,
                "processing_llm_active_model": None,
                "processing_llm_active_endpoint": None,
                "processing_attempts": 1,
                "queued_at": "2026-03-06T00:00:00",
                "processing_started_at": "2026-03-06T00:00:01",
                "processing_completed_at": "2026-03-06T00:00:02",
                "processing_heartbeat_at": "2026-03-06T00:00:02",
                "processing_error": None,
                "resolved_episode_uuid": "episode-graphiti-1",
                "enriched_nodes_touched": 2,
                "enriched_edges_touched": 2,
            }
        ]

    def fetch_flagged_memories(self, limit: int = 10):
        rows = [row for row in self.fetch_recent_memories(limit=limit) if bool(row.get("user_flagged", False))]
        return rows[:limit]

    def fetch_flagged_memory_bootstrap_version(self) -> str:
        return self.flagged_version

    def fetch_memory_by_uuid(self, node_uuid: str):
        if node_uuid == "123e4567-e89b-12d3-a456-426614174000":
            return self.fetch_recent_memories()[0]
        return None

    def fetch_memories_by_scope(self, scope: str, limit: int = 10):
        self.scope_calls.append(scope)
        return [dict(self.fetch_recent_memories()[0], scope=scope)]

    def fetch_memories_by_type(self, memory_type: str, limit: int = 10):
        self.type_calls.append(memory_type)
        return [dict(self.fetch_recent_memories()[0], type=memory_type)]

    def fetch_episode_processing(self, episode_uuid: str):
        return self.episode_rows.get(episode_uuid)

    def list_episode_processing(
        self,
        *,
        processing_states: list[str] | None = None,
        limit: int = 25,
        namespace: str | None = None,
    ):
        states = {state.upper() for state in (processing_states or [])}
        rows = []
        for row in self.episode_rows.values():
            state = str(row.get("processing_state") or "").upper()
            if states and state not in states:
                continue
            rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("uuid") or ""))
        return rows[:limit]

    def fetch_stale_enriching_episodes(self, *, include_missing_lease: bool = True, limit: int = 100):
        rows = []
        for row in self.episode_rows.values():
            if row.get("processing_state") != "ENRICHING":
                continue
            if row.get("processing_lease_expires_at") is None and not include_missing_lease:
                continue
            rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("uuid") or ""))
        return rows[:limit]

    def force_release_episode_lease(self, episode_uuid: str, *, max_attempts: int) -> bool:
        row = self.episode_rows.get(episode_uuid)
        if row is None or row.get("processing_state") != "ENRICHING":
            return False
        attempts = int(row.get("processing_attempts") or 0)
        exhausted = attempts >= max_attempts
        row["processing_state"] = "FAILED" if exhausted else "PENDING"
        row["processing_stage"] = "failed" if exhausted else "queued"
        row["processing_substage"] = (
            "lease_released_attempts_exhausted" if exhausted else "lease_released"
        )
        row["processing_error"] = (
            "manual_lease_release_attempts_exhausted" if exhausted else "manual_lease_release"
        )
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        return True

    def list_conflict_groups(
        self, *, status: str | None = "unresolved", limit: int = 25,
        namespace: str | None = None,
    ):
        rows = self.conflict_groups
        if status is not None:
            filtered: list[dict[str, object]] = []
            for row in rows:
                members = row.get("members")
                if not isinstance(members, list):
                    continue
                if any(str(member.get("status") or "") == status for member in members if isinstance(member, dict)):
                    filtered.append(row)
            rows = filtered
        return rows[:limit]

    def resolve_conflict_group(
        self,
        conflict_group_id: str,
        action: str,
        *,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        resolution_status: str = "resolved",
        allow_promoted_removal: bool = False,
    ) -> dict[str, object]:
        self.conflict_resolve_calls.append(
            {
                "conflict_group_id": conflict_group_id,
                "action": action,
                "keep_uuid": keep_uuid,
                "remove_uuid": remove_uuid,
                "resolution_status": resolution_status,
                "allow_promoted_removal": allow_promoted_removal,
            }
        )
        self.conflict_groups = [
            row for row in self.conflict_groups if str(row.get("group_id") or "") != conflict_group_id
        ]
        removed = [remove_uuid] if remove_uuid else [
            "123e4567-e89b-12d3-a456-426614174112"
        ]
        return {
            "action": action,
            "group_id": conflict_group_id,
            "resolved": 1,
            "removed_uuid": removed[0] if removed else None,
            "removed_uuids": removed,
            "bridged_edges": len(removed),
        }


class StubRecallService:
    async def recall(
        self,
        query,
        preset,
        limit,
        include_session=False,
        wait_for_pending=False,
        file_context=None,
        file_context_project=None,
        namespace=None,
        include_invalidated=False,
        include_superseded=False,
        trace=False,
    ):
        from menhir.domain.recall import RecallResult, ScoredMemory
        from menhir.domain.retrieval_trace_models import RelevanceBreakdown

        breakdown = RelevanceBreakdown(
            semantic_similarity=0.91,
            adjacency_bonus=0.05,
            recency_bonus=0.02,
            prominence_bonus=0.0,
            conflict_bonus=0.0,
            type_boost=0.0,
            preset=getattr(preset, "value", preset),
            alpha=0.0,
            beta=0.0,
            gamma=0.0,
            delta=0.0,
        )

        results = [
            ScoredMemory(
                uuid="123e4567-e89b-12d3-a456-426614174000",
                name="Miso",
                memory_type="pet",
                scope="PERSISTENT",
                content="Dog named Miso",
                final_score=0.98,
                breakdown=breakdown,
            )
        ]

        return RecallResult(
            query=query,
            preset=getattr(preset, "value", preset),
            results=results,
            candidates_evaluated=1,
            nodes_touched=0,
            note=None,
            trace=None,
        )


class StubIngestService:
    def __init__(self, graph_adapter: StubGraphAdapter | None = None) -> None:
        self.queued_calls: list[dict[str, str]] = []
        self.pending_enqueue_calls: list[str] = []
        self.graph_adapter = graph_adapter

    async def queue_episode_for_enrichment(self, text: str, session: object, source: str, diff: str | None = None, flagged: bool = False, namespace: str | None = None, occurred_at: str | None = None, turn_evidence_uuid: str | None = None):
        self.queued_calls.append({"text": text, "source": source, "session_id": session.session_id, "diff": diff})
        return {
            "episode_id": "123e4567-e89b-12d3-a456-426614174999",
            "status": "queued",
            "nodes_touched": 1,
            "edges_touched": 0,
        }

    def get_queue_depth(self) -> int:
        return 1

    def get_failed_enrichment_count(self) -> int:
        return 0

    def get_max_enrichment_attempts(self) -> int:
        return 3

    async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
        stale_resets = 0
        if self.graph_adapter is None:
            return 0, 0
        for row in self.graph_adapter.episode_rows.values():
            if row.get("processing_state") != "ENRICHING":
                continue
            if row.get("processing_lease_expires_at") is not None:
                continue
            row["processing_state"] = "PENDING"
            row["processing_error"] = "lease_missing_reset"
            row["processing_owner"] = None
            stale_resets += 1
        queued = sum(
            1 for row in self.graph_adapter.episode_rows.values() if row.get("processing_state") == "PENDING"
        )
        return stale_resets, queued

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        if self.graph_adapter is None:
            return False
        row = self.graph_adapter.episode_rows.get(episode_uuid)
        if row is None or row.get("processing_state") != "FAILED":
            return False
        row["processing_state"] = "PENDING"
        return True

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        if self.graph_adapter is None:
            return False
        row = self.graph_adapter.episode_rows.get(episode_uuid)
        if row is None or row.get("processing_state") != "PENDING":
            return False
        self.pending_enqueue_calls.append(episode_uuid)
        return True

    async def _enqueue_pending_episode(self, episode_uuid: str) -> None:
        if self.graph_adapter is None:
            return
        row = self.graph_adapter.episode_rows.get(episode_uuid)
        if row is not None:
            row["processing_state"] = "PENDING"


class StubScheduler:
    def __init__(self) -> None:
        self.running = True
        self.forced_reasons: list[str] = []

    async def force_takeover(self, reason: str = "manual-troubleshooting") -> bool:
        self.running = True
        self.forced_reasons.append(reason)
        return True

    def status_snapshot(self) -> dict[str, object]:
        return {
            "running": self.running,
            "lease": {
                "acquired": True,
                "blocked_reason": None,
                "active_owner": {"owner_pid": 1234, "owner_id": "stub-owner", "expired": False},
                "last_forced_takeover": {
                    "at": "2026-03-09T00:00:00Z",
                    "reason": self.forced_reasons[-1] if self.forced_reasons else None,
                },
            },
            "jobs": {
                "recover_stale_leases": {"runs": 1},
                "observe_queue_health": {"runs": 1},
            },
        }


@pytest.fixture()
def stubbed_mcp_state():
    original = dict(mcp_server._state)
    graph_adapter = StubGraphAdapter()
    ingest_service = StubIngestService(graph_adapter=graph_adapter)
    built = SimpleNamespace(
        settings=SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            llama_base_url="http://localhost:1234/v1",
            llama_chat_model="chat-model",
            llama_embed_model="embed-model",
            chat_provider="openai",
            embed_provider="openai",
            graphiti_provider="openai",
            graphiti_embed_provider="openai",
            graphiti_reranker_provider="openai",
            openai_api_key="test-openai-key",
            openai_chat_model="gpt-4.1-nano",
            openai_embed_model="text-embedding-3-small",
        ),
        graph_adapter=graph_adapter,
        recall_service=StubRecallService(),
        ingest_service=ingest_service,
        scheduler=StubScheduler(),
    )
    session = SimpleNamespace(session_id="session-123", user_id="claude-code")
    mcp_server._state.clear()
    mcp_server._state.update({"built": built, "session": session})
    try:
        yield built, graph_adapter, session
    finally:
        mcp_server._state.clear()
        mcp_server._state.update(original)


def _read_json(uri: str):
    result = asyncio.run(mcp_server.mcp.read_resource(uri))
    contents = result.contents
    assert len(contents) == 1
    return json.loads(contents[0].content)


def _read_text(uri: str) -> str:
    result = asyncio.run(mcp_server.mcp.read_resource(uri))
    contents = result.contents
    assert len(contents) == 1
    return contents[0].content


def _parse_json_text(text: str):
    return json.loads(text)


def test_mcp_resources_and_templates_are_registered():
    resources = asyncio.run(mcp_server.mcp.list_resources())
    templates = asyncio.run(mcp_server.mcp.list_resource_templates())

    assert {str(resource.uri) for resource in resources} >= {
        "memory://system/dependency-health",
        "memory://system/metadata",
        "memory://system/lifecycle-trace",
        "memory://system/processing-queue",
        "memory://recent",
    }
    assert {template.uri_template for template in templates} >= {
        "memory://node/{node_uuid}",
        "memory://scope/{scope}",
        "memory://search/{term}",
        "memory://type/{memory_type}",
    }


def test_system_metadata_and_recent_resources_return_json(stubbed_mcp_state):
    metadata = _read_json("memory://system/metadata")
    recent = _read_json("memory://recent")

    assert metadata["server"] == "menhir"
    assert metadata["session"]["session_id"] == "session-123"
    assert metadata["overview"]["total_memories"] == 3
    assert metadata["runtime"]["enrichment_queue_depth"] == 1
    assert metadata["runtime"]["scheduler"]["running"] is True
    assert metadata["runtime"]["scheduler"]["lease_acquired"] is True

    assert recent["count"] == 1
    assert recent["items"][0]["uuid"] == "123e4567-e89b-12d3-a456-426614174000"
    assert recent["items"][0]["user_flagged"] is True
    assert recent["items"][0]["summary"] == "Dog named Miso"
    assert "processing_state" not in recent["items"][0]


def test_dependency_health_resource_reports_neo4j_hint(stubbed_mcp_state, monkeypatch):
    monkeypatch.setattr(
        mcp_resources,
        "_neo4j_dependency_snapshot",
        lambda: {
            "uri": "bolt://localhost:7687",
            "host": "localhost",
            "port": 7687,
            "reachable": False,
            "hint": "Neo4j is not reachable. Start Docker Desktop and the yawn-neo4j container.",
        },
    )

    payload = _read_json("memory://system/dependency-health")

    assert payload["server"] == "menhir"
    assert payload["neo4j"]["reachable"] is False
    assert "Docker Desktop" in payload["neo4j"]["hint"]


def test_lifecycle_trace_resource_returns_recent_rows(stubbed_mcp_state, monkeypatch):
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store.record_lifecycle_event(
            recorded_at="2026-03-11T00:00:00Z",
            component="runtime_init",
            event="build_memory_services",
            state="completed",
            episode_uuid=None,
            details_json='{"ok":true}',
        )
        monkeypatch.setattr(
            mcp_resources.LifecycleTraceResource,
            "get_backend",
            lambda self: SimpleNamespace(
                fetch_recent_lifecycle_events=AsyncMock(
                    return_value=store.fetch_recent_lifecycle_events(limit=100)
                )
            ),
        )

        payload = _read_json("memory://system/lifecycle-trace")

        assert payload["count"] == 1
        assert payload["items"][0]["component"] == "runtime_init"
        assert payload["items"][0]["event"] == "build_memory_services"
        assert payload["items"][0]["details"] == {"ok": True}
    finally:
        _cleanup_db_path(db_path)


def test_processing_queue_resource_returns_compact_queue_state(stubbed_mcp_state):
    _, graph_adapter, _ = stubbed_mcp_state
    graph_adapter.episode_rows["123e4567-e89b-12d3-a456-426614174997"] = {
        "uuid": "123e4567-e89b-12d3-a456-426614174997",
        "processing_state": "PENDING",
        "processing_stage": "queued",
        "processing_substage": "lease_released",
        "processing_progress": 0.0,
        "processing_attempts": 3,
        "processing_owner": None,
        "processing_lease_expires_at": None,
        "processing_heartbeat_at": "2026-03-06T00:00:11",
        "processing_started_at": None,
        "processing_llm_last_task_at": "2026-03-06T00:00:09",
        "processing_llm_active_task": None,
        "processing_error": "lease_expired_reset",
    }

    payload = _read_json("memory://system/processing-queue")

    assert payload["count"] == 2
    assert payload["queue_depth"] == 1
    assert payload["max_attempts"] == 3
    assert payload["counts"] == {
        "pending": 1,
        "enriching": 1,
        "stale_enriching": 1,
        "exhausted_pending": 1,
    }
    items = {item["uuid"]: item for item in payload["items"]}
    assert items["123e4567-e89b-12d3-a456-426614174998"]["stale_lease"] is True
    assert items["123e4567-e89b-12d3-a456-426614174998"]["status_hint"] == "stale_lease"
    assert items["123e4567-e89b-12d3-a456-426614174997"]["exhausted_pending"] is True
    assert items["123e4567-e89b-12d3-a456-426614174997"]["status_hint"] == "exhausted_pending"


def test_resource_templates_return_filtered_data(stubbed_mcp_state):
    _, graph_adapter, _ = stubbed_mcp_state

    by_uuid = _read_json("memory://node/123e4567-e89b-12d3-a456-426614174000")
    by_scope = _read_json("memory://scope/session")
    by_type = _read_json("memory://type/pet")
    by_search = _read_json("memory://search/miso")

    assert by_uuid["found"] is True
    assert by_uuid["memory"]["uuid"] == "123e4567-e89b-12d3-a456-426614174000"

    assert by_scope["scope"] == "SESSION"
    assert graph_adapter.scope_calls == ["SESSION"]

    assert by_type["type"] == "pet"
    assert graph_adapter.type_calls == ["pet"]

    assert by_search["query"] == "miso"
    assert by_search["count"] == 1
    assert by_search["items"][0]["name"] == "Miso"
    assert by_search["items"][0]["summary"] == "Dog named Miso"


def test_resource_template_validation_rejects_bad_inputs(stubbed_mcp_state):
    by_uuid = _read_json("memory://node/not-a-uuid")
    by_scope = _read_json("memory://scope/unknown")
    by_search = _read_json("memory://search/" + ("x" * 201))
    by_type = _read_json("memory://type/bad.type")

    assert by_uuid["ok"] is False
    assert by_uuid["error"]["message"].startswith("ValueError: Invalid memory UUID")
    assert by_scope["error"]["message"].startswith("ValueError: Invalid scope")
    assert by_search["error"]["message"].startswith(
        "ValueError: Search term must be 200 characters or fewer"
    )
    assert by_type["error"]["message"].startswith("ValueError: Memory type must be")


@pytest.mark.asyncio
async def test_mcp_lifespan_requires_backend_url(monkeypatch):
    monkeypatch.setattr(mcp_server_lifecycle, "backend_client_mode_enabled", lambda: False)

    lifespan = mcp_server_lifecycle._mcp_lifespan(mcp_server.mcp)
    with pytest.raises(RuntimeError) as exc:
        async with lifespan:
            pass

    assert "requires menhir_BACKEND_URL" in str(exc.value)


def test_add_memory_queues_episode_for_background_enrichment(stubbed_mcp_state):
    built, _, _ = stubbed_mcp_state

    result = asyncio.run(add_memory("remember this", source="claude-code"))

    assert result == (
        "Queued. episode_id=123e4567-e89b-12d3-a456-426614174999, "
        "state=PENDING, nodes=1, edges=0, "
        "queue_depth=1, active_enriching=1, scheduler=running"
    )
    assert built.ingest_service.queued_calls == [
        {
            "text": "remember this",
            "source": "claude-code",
            "session_id": "session-123",
            "diff": None,
        }
    ]


def test_recall_context_requires_flagged_bootstrap(stubbed_mcp_state):
    result = asyncio.run(
        recall_context_memories(
            reader_id="bot-a",
            query="miso",
            preset="knowledge",
            limit=5,
            recent_limit=5,
        )
    )

    assert "Context bootstrap required." in result
    assert "read_flagged_memories(reader_id='bot-a', workspace='')" in result
    assert "current_flagged_version=1:stubflagged" in result


def test_read_flagged_then_recall_context_flow(stubbed_mcp_state):
    _, graph_adapter, _ = stubbed_mcp_state
    original_recent = graph_adapter.fetch_recent_memories

    def _recent_with_non_flagged(limit: int = 10):
        base = original_recent(limit=limit)
        return base + [
            dict(
                base[0],
                uuid="123e4567-e89b-12d3-a456-426614174110",
                name="legacy-directory",
                content="Directory: src/legacy",
                source="project-scan",
                structure_role=None,
                user_flagged=False,
            ),
            dict(
                base[0],
                uuid="123e4567-e89b-12d3-a456-426614174111",
                name="Daily focus",
                content="Focus on queue health and stale leases.",
                user_flagged=False,
            )
        ]

    graph_adapter.fetch_recent_memories = _recent_with_non_flagged  # type: ignore[method-assign]

    flagged_result = asyncio.run(read_flagged_memories(reader_id="bot-a", limit=10))
    context_result = asyncio.run(
        recall_context_memories(
            reader_id="bot-a",
            query="miso",
            preset="knowledge",
            limit=5,
            recent_limit=5,
        )
    )

    flagged_payload = _parse_json_text(flagged_result)
    context_payload = _parse_json_text(context_result)

    assert flagged_payload["bootstrap_ready"] is True
    assert flagged_payload["flagged_version"] == "1:stubflagged"
    assert flagged_payload["flagged_count"] == 1
    assert flagged_payload["items"][0]["tag"] == "flagged"
    assert context_payload["bootstrap_verified"] is True
    assert context_payload["flagged_version"] == "1:stubflagged"
    assert context_payload["recent_count"] == 1
    assert context_payload["recent"][0]["tag"] == "recent"
    assert context_payload["recent"][0]["name"] == "Daily focus"


def test_recall_context_includes_similarity_for_relevant_hits(stubbed_mcp_state):
    built, graph_adapter, _ = stubbed_mcp_state

    original_fetch = graph_adapter.fetch_memory_by_uuid

    def _fetch_non_flagged(node_uuid: str):
        if node_uuid == "123e4567-e89b-12d3-a456-426614174222":
            return {
                "uuid": node_uuid,
                "name": "Queue health",
                "content": "Track queue depth and stale leases.",
                "summary": "Track queue health and stale leases.",
                "scope": "PERSISTENT",
                "type": "ops",
                "user_flagged": False,
            }
        return original_fetch(node_uuid)

    graph_adapter.fetch_memory_by_uuid = _fetch_non_flagged  # type: ignore[method-assign]

    async def _recall(*args, **kwargs):
        return {
            "results": [
                {
                    "uuid": "123e4567-e89b-12d3-a456-426614174222",
                    "name": "Queue health",
                    "memory_type": "ops",
                    "scope": "PERSISTENT",
                    "content": "Track queue depth and stale leases.",
                    "breakdown": {"semantic_similarity": 0.91},
                }
            ]
        }

    built.recall_service.recall = _recall

    asyncio.run(read_flagged_memories(reader_id="bot-a", limit=10))
    result = asyncio.run(
        recall_context_memories(
            reader_id="bot-a",
            query="queue",
            preset="knowledge",
            limit=5,
            recent_limit=5,
        )
    )

    payload = _parse_json_text(result)

    assert payload["relevant_count"] == 1
    assert payload["relevant"][0]["uuid"] == "123e4567-e89b-12d3-a456-426614174222"
    assert payload["relevant"][0]["similarity"] == 0.91


def test_recall_context_requires_rebootstrap_when_flagged_version_changes(stubbed_mcp_state):
    _, graph_adapter, _ = stubbed_mcp_state

    flagged_result = asyncio.run(read_flagged_memories(reader_id="bot-a", limit=10))
    assert _parse_json_text(flagged_result)["flagged_version"] == "1:stubflagged"

    graph_adapter.flagged_version = "2:changed"
    context_result = asyncio.run(
        recall_context_memories(
            reader_id="bot-a",
            query="miso",
            preset="knowledge",
            limit=5,
            recent_limit=5,
        )
    )

    assert "Context bootstrap required." in context_result
    assert "current_flagged_version=2:changed" in context_result


def test_add_memory_and_track_returns_status_history(stubbed_mcp_state):
    result = asyncio.run(
        add_memory_and_track(
            "remember this",
            source="claude-code",
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
    )

    assert "queued_summary: queue_depth=1, active_enriching=1, scheduler=running" in result
    assert "episode_id: 123e4567-e89b-12d3-a456-426614174999" in result
    assert "status: READY" in result
    assert "history:" in result


def test_watch_enrichment_returns_delta_history(stubbed_mcp_state):
    result = asyncio.run(
        watch_enrichment(
            "123e4567-e89b-12d3-a456-426614174999",
            timeout_s=0.1,
            poll_interval_s=0.01,
        )
    )

    assert "episode_id: 123e4567-e89b-12d3-a456-426614174999" in result
    assert "deltas:" in result
    assert "updates: 1" in result
    assert "llm_tasks=4/4" in result
    assert "substage=complete" in result


def test_watch_enrichment_rejects_bad_uuid(stubbed_mcp_state):
    result = asyncio.run(watch_enrichment("not-a-uuid", timeout_s=0.1, poll_interval_s=0.01))

    assert result.startswith("Error: ValueError: Invalid episode UUID")


def test_get_enrichment_status_returns_episode_snapshot(stubbed_mcp_state):
    result = asyncio.run(
        get_enrichment_status("123e4567-e89b-12d3-a456-426614174999")
    )

    assert "episode_id: 123e4567-e89b-12d3-a456-426614174999" in result
    assert "status: READY" in result
    assert "substage: complete" in result
    assert "attempts: 1" in result
    assert "llm_tasks_attempt: 4" in result


def test_get_enrichment_status_rejects_bad_uuid(stubbed_mcp_state):
    result = asyncio.run(get_enrichment_status("not-a-uuid"))

    assert result.startswith("Error: ValueError: Invalid episode UUID")


def test_force_scheduler_takeover_reports_owner_state(stubbed_mcp_state):
    built, _, _ = stubbed_mcp_state

    result = asyncio.run(force_scheduler_takeover(reason="manual-debug"))

    assert "takeover_applied: True" in result
    assert "scheduler_running: True" in result
    assert "last_forced_takeover_reason: manual-debug" in result
    assert built.scheduler.forced_reasons == ["manual-debug"]


def test_list_enrichment_queue_reports_stale_rows(stubbed_mcp_state):
    result = asyncio.run(list_enrichment_queue(state="enriching", limit=10))

    assert "filter: ENRICHING" in result
    assert "count: 1" in result
    assert "uuid=123e4567-e89b-12d3-a456-426614174998" in result
    assert "stage=" in result
    assert "steps=" in result
    assert "llm_tasks=" in result
    assert "stale=missing_lease" in result


def test_repair_stale_enrichment_dry_run_lists_candidates(stubbed_mcp_state):
    result = asyncio.run(repair_stale_enrichment(dry_run=True, limit=10))

    assert "dry_run: True" in result
    assert "stale_detected: 1" in result
    assert "uuid=123e4567-e89b-12d3-a456-426614174998" in result


def test_repair_stale_enrichment_resets_and_requeues(stubbed_mcp_state):
    result = asyncio.run(repair_stale_enrichment(dry_run=False, limit=10))

    assert "dry_run: False" in result
    assert "stale_reset: 1" in result
    assert "stale_remaining: 0" in result


def test_force_release_enrichment_lease_requeues_episode(stubbed_mcp_state):
    result = asyncio.run(
        force_release_enrichment_lease(
            "123e4567-e89b-12d3-a456-426614174998",
            requeue=True,
        )
    )

    assert "lease_release: ok" in result
    assert "queued: True" in result
    assert "queue_result: queued" in result
    assert "state: PENDING" in result
    assert "error: manual_lease_release" in result


def test_force_release_enrichment_lease_fails_when_attempts_exhausted(stubbed_mcp_state):
    built, graph_adapter, _ = stubbed_mcp_state
    episode_uuid = "123e4567-e89b-12d3-a456-426614174998"
    graph_adapter.episode_rows[episode_uuid]["processing_attempts"] = built.ingest_service.get_max_enrichment_attempts()

    result = asyncio.run(
        force_release_enrichment_lease(
            episode_uuid,
            requeue=True,
        )
    )

    assert "lease_release: ok" in result
    assert "queued: False" in result
    assert "queue_result: skipped_state_failed" in result
    assert "state: FAILED" in result
    assert "error: manual_lease_release_attempts_exhausted" in result


def test_get_episode_trace_returns_compact_debug_bundle(stubbed_mcp_state, monkeypatch):
    db_path = _make_db_path()
    store = McpTelemetryStore(db_path=db_path)
    try:
        store.record_episode_task_event(
            recorded_at="2026-03-11T00:00:01Z",
            episode_uuid="123e4567-e89b-12d3-a456-426614174998",
            parent_task=None,
            child_task=None,
            phase="started",
            kind="chat",
            model="model-a",
            endpoint="chat.completions.create",
            scheduler_task="memory: graphiti add_episode",
            details_json='{"attempt":1}',
        )
        store.record_failure(
            recorded_at="2026-03-11T00:00:02Z",
            operation="episode_enrichment",
            episode_uuid="123e4567-e89b-12d3-a456-426614174998",
            failure_stage="graphiti_exception",
            classification="retryable",
            retryable=True,
            processing_attempt=2,
            queue_depth=1,
            worker_id="worker-1",
            error_type="TimeoutError",
            error="timeout",
            details_json='{"duration_ms":1000,"traceback":"line1\\nline2","traceback_preview":"line1\\nline2"}',
        )
        store.record_lifecycle_event(
            recorded_at="2026-03-11T00:00:03Z",
            component="ingest_worker",
            event="graphiti_add_episode",
            state="started",
            episode_uuid="123e4567-e89b-12d3-a456-426614174998",
            details_json='{"worker_id":"worker-1"}',
        )
        monkeypatch.setattr(
            _get_episode_trace_mod.GetEpisodeTraceTool,
            "get_backend",
            lambda self: SimpleNamespace(
                fetch_episode_processing=AsyncMock(
                    return_value=stubbed_mcp_state[1].fetch_episode_processing(
                        "123e4567-e89b-12d3-a456-426614174998"
                    )
                ),
                fetch_episode_task_events=AsyncMock(
                    return_value=store.fetch_episode_task_events(
                        episode_uuid="123e4567-e89b-12d3-a456-426614174998",
                        limit=10,
                    )
                ),
                fetch_recent_failures=AsyncMock(
                    return_value=store.fetch_recent_failures(
                        limit=10,
                        episode_uuid="123e4567-e89b-12d3-a456-426614174998",
                    )
                ),
                fetch_recent_lifecycle_events=AsyncMock(
                    return_value=store.fetch_recent_lifecycle_events(
                        limit=10,
                        episode_uuid="123e4567-e89b-12d3-a456-426614174998",
                    )
                ),
            ),
        )

        payload = _parse_json_text(
            asyncio.run(
                get_episode_trace("123e4567-e89b-12d3-a456-426614174998", limit=10)
            )
        )

        assert payload["episode_uuid"] == "123e4567-e89b-12d3-a456-426614174998"
        assert payload["current"]["state"] == "ENRICHING"
        assert payload["task_events"][0]["phase"] == "started"
        assert payload["failure_events"][0]["failure_stage"] == "graphiti_exception"
        assert payload["failure_events"][0]["traceback_preview"] == "line1\nline2"
        assert payload["failure_events"][0]["details"]["traceback"] == "line1\nline2"
        assert payload["lifecycle_events"][0]["event"] == "graphiti_add_episode"
    finally:
        _cleanup_db_path(db_path)


def test_list_conflicts_returns_grouped_summary(stubbed_mcp_state):
    payload = _parse_json_text(asyncio.run(list_conflicts(status="unresolved", limit=10)))

    assert payload["status"] == "unresolved"
    assert payload["count"] == 1
    assert payload["groups"][0]["group_id"] == "group-1"
    assert payload["groups"][0]["status"] == "unresolved"
    assert payload["groups"][0]["members"][0]["tag"] == "older"
    assert payload["groups"][0]["members"][1]["tag"] == "newer"
    assert payload["groups"][0]["suggested_resolution"]["tool"] == "resolve_conflict"


def test_list_conflicts_rejects_invalid_status(stubbed_mcp_state):
    payload = _parse_json_text(asyncio.run(list_conflicts(status="bad", limit=10)))

    assert payload["ok"] is False
    assert payload["tool"] == "list_conflicts"
    assert payload["error"]["message"] == "Invalid status. Use: unresolved, resolved, auto-resolved, all."


def test_list_conflicts_truncates_long_member_summaries(stubbed_mcp_state):
    _, graph_adapter, _ = stubbed_mcp_state
    graph_adapter.conflict_groups[0]["members"][0]["content"] = "x" * 140

    payload = _parse_json_text(asyncio.run(list_conflicts(status="unresolved", limit=10)))

    assert payload["groups"][0]["members"][0]["summary"] == ("x" * 117) + "..."


def test_resolve_conflict_dry_run_previews_mutation(stubbed_mcp_state):
    payload = _parse_json_text(asyncio.run(
        resolve_conflict(
            group_id="group-1",
            action="replace",
            keep_uuid="123e4567-e89b-12d3-a456-426614174111",
            dry_run=True,
        )
    ))

    assert payload["dry_run"] is True
    assert payload["nodes_gone"] == 1
    assert payload["group_clear_count"] == 1
    assert payload["would_remove_uuids"] == ["123e4567-e89b-12d3-a456-426614174112"]


def test_resolve_conflict_apply_calls_adapter(stubbed_mcp_state):
    built, _, _ = stubbed_mcp_state
    payload = _parse_json_text(asyncio.run(
        resolve_conflict(
            group_id="group-1",
            action="replace",
            keep_uuid="123e4567-e89b-12d3-a456-426614174111",
            remove_uuid="123e4567-e89b-12d3-a456-426614174112",
            allow_promoted_removal=True,
        )
    ))

    assert payload["action"] == "replace"
    assert payload["removed_uuids"] == ["123e4567-e89b-12d3-a456-426614174112"]
    assert payload["group_cleared"] is True
    assert payload["remaining_unresolved_in_group"] == 0
    assert payload["journal_op_id"] is None
    assert len(built.graph_adapter.conflict_resolve_calls) == 1
    call = built.graph_adapter.conflict_resolve_calls[0]
    assert call["conflict_group_id"] == "group-1"
    assert call["action"] == "replace"
    assert call["keep_uuid"] == "123e4567-e89b-12d3-a456-426614174111"
    assert call["remove_uuid"] == "123e4567-e89b-12d3-a456-426614174112"
    assert call["allow_promoted_removal"] is True


def test_resolve_conflict_rejects_invalid_membership(stubbed_mcp_state):
    payload = _parse_json_text(asyncio.run(
        resolve_conflict(
            group_id="group-1",
            action="replace",
            keep_uuid="not-a-member",
        )
    ))

    assert payload["ok"] is False
    assert payload["tool"] == "resolve_conflict"
    assert "is not a member of group_id=group-1" in payload["error"]["message"]


def test_resolve_conflict_keep_one_without_remove_uuid_reports_confirmation(stubbed_mcp_state):
    payload = _parse_json_text(asyncio.run(
        resolve_conflict(
            group_id="group-1",
            action="replace",
            keep_uuid="123e4567-e89b-12d3-a456-426614174111",
        )
    ))

    assert payload["confirmation"] == "Removed 1 node(s) as non-keep members of group."
    assert payload["group_cleared"] is True


@pytest.mark.asyncio
async def test_mcp_lifespan_yields_backend_client_state(monkeypatch):
    health = {
        "ok": True,
        "url": "http://localhost:8090",
        "status": "degraded",
        "startup_mode": "degraded_reads_only",
    }

    monkeypatch.setattr(mcp_server_lifecycle, "backend_client_mode_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_server_lifecycle,
        "probe_backend_health",
        AsyncMock(return_value=health),
    )

    async with mcp_server_lifecycle._mcp_lifespan(mcp_server.mcp) as state:
        assert state == {"mode": "backend_client", "backend": health}


def test_add_memory_returns_error_when_backend_resolution_fails(monkeypatch):
    def fake_get_backend(self):
        raise RuntimeError(
            "MCP stdio now requires menhir_BACKEND_URL. "
            "Start `yawn-memory serve` and point stdio MCP at that backend."
        )

    monkeypatch.setattr(_add_memory_mod.AddMemoryTool, "get_backend", fake_get_backend)

    result = asyncio.run(add_memory("remember this", source="claude-code"))

    assert result.startswith("Error: RuntimeError: MCP stdio now requires menhir_BACKEND_URL.")
