"""Unit tests for the developer explorer app."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from menhir.config import MemorySettings
from menhir.explorer import create_app


@dataclass
class _StubExplorerRepo:
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = params or {}
        self.calls.append({"query": query, "params": params})

        if "MATCH (e:Episodic)" in query and "e.processing_state IN ['PENDING', 'ENRICHING']" in query:
            return [{
                "uuid": "queued-episode-1",
                "name": "queued episode",
                "session_id": "session-1",
                "source": "integration-test",
                "processing_state": "PENDING",
                "processing_stage": "graphiti_extracting",
                "processing_progress": 30.0,
                "processing_steps_total": 5,
                "processing_steps_completed": 1,
                "processing_llm_tasks_attempt": 2,
                "processing_llm_tasks_total": 2,
                "processing_llm_last_task_at": "2026-03-06T12:05:05Z",
                "processing_attempts": 1,
                "queued_at": "2026-03-06T12:05:00Z",
                "processing_heartbeat_at": "2026-03-06T12:05:10Z",
                "processing_error": None,
                "preview": "Queued preview",
            }]
        if "MATCH (e:Episodic)" in query and "e.processing_state = 'READY' AND coalesce(toInteger(e.processing_attempts), 0) <= 1" in query:
            return [{
                "uuid": "ready-episode-1",
                "name": "successful episode",
                "session_id": "session-2",
                "source": "integration-test",
                "processing_stage": "ready",
                "processing_progress": 100.0,
                "processing_steps_total": 5,
                "processing_steps_completed": 5,
                "processing_llm_tasks_attempt": 7,
                "processing_llm_tasks_total": 7,
                "processing_llm_last_task_at": "2026-03-06T12:12:00Z",
                "processing_attempts": 1,
                "completed_at": "2026-03-06T12:12:01Z",
                "processing_heartbeat_at": "2026-03-06T12:12:02Z",
                "preview": "Successful preview",
            }]
        if "MATCH (e:Episodic)" in query and "e.processing_state = 'FAILED'" in query:
            return [{
                "uuid": "failed-episode-1",
                "name": "failed episode",
                "session_id": "session-9",
                "source": "integration-test",
                "processing_state": "FAILED",
                "processing_attempts": 3,
                "failed_at": "2026-03-06T12:15:00Z",
                "processing_error": "llm timeout",
                "preview": "Failed preview",
            }]
        if "MATCH (e:Episodic)" in query and "left(coalesce(e.content" in query:
            return [{"uuid": "episode-1", "name": "episode-one", "session_id": "session-1", "source": "integration-test", "created_at": "2026-03-06T12:00:00Z", "preview": "Episode preview"}]
        if "MATCH (n)\n        WHERE n.session_id IS NOT NULL" in query:
            return [{"session_id": "session-1", "node_count": 2, "episode_count": 1, "last_seen": "2026-03-06T12:00:00Z"}]
        if "coalesce(n.user_flagged, false) = true" in query:
            return [{"uuid": "entity-flagged", "name": "Flagged Entity", "labels": ["Entity"], "scope": "PERSISTENT", "session_id": None}]
        if "MATCH (n:Entity)" in query and "summary" in query:
            return [{"uuid": "entity-1", "name": "Alpha Entity", "scope": "SESSION", "session_id": "session-1", "source": "integration-test", "user_flagged": False, "last_accessed": "2026-03-06T12:00:00Z"}]
        if "RETURN {\n            uuid: n.uuid" in query:
            if params.get("uuid") == "missing":
                return []
            return [{"detail": {"uuid": params["uuid"], "name": "Alpha Entity", "labels": ["Entity"], "scope": "SESSION", "session_id": "session-1", "user_id": "tester", "source": "integration-test", "user_flagged": False, "freshness": None, "created_at": "2026-03-06T12:00:00Z", "last_accessed": "2026-03-06T12:00:00Z", "content": "Alpha content", "neighbors": [{"uuid": "entity-2", "name": "Neighbor Entity", "labels": ["Entity"], "scope": "PERSISTENT", "rel_type": "MENTIONS"}], "episodes": [{"uuid": "episode-1", "name": "episode-one", "created_at": "2026-03-06T12:00:00Z", "session_id": "session-1"}]}}]
        if "RETURN\n            $session_id AS session_id" in query:
            return [{"session_id": params["session_id"], "node_count": 2, "episode_count": 1, "nodes": [{"uuid": "entity-1", "name": "Alpha Entity", "labels": ["Entity"], "scope": "SESSION", "source": "integration-test"}]}]
        if "RETURN DISTINCT\n            node.uuid AS uuid" in query:
            return [{"uuid": "entity-1", "label": "Alpha Entity", "labels": ["Entity"], "scope": "SESSION"}, {"uuid": "episode-1", "label": "episode-one", "labels": ["Episodic"], "scope": "SESSION"}]
        if "RETURN DISTINCT\n            rel.uuid AS uuid" in query:
            return [{"uuid": "edge-1", "rel_type": "MENTIONS", "source": "episode-1", "target": "entity-1", "scope": "SESSION"}]
        return []

    def close(self) -> None:
        return None


@pytest.mark.unit
def test_explorer_home_renders_memory_aware_panels() -> None:
    app = create_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer")
    assert response.status_code == 200
    assert "Enrichment Queue" in response.text
    assert "queued episode" in response.text
    assert "LLM Tasks" in response.text
    assert "Successful Enrichments" in response.text
    assert "successful episode" in response.text
    assert "Failed Enrichments" in response.text
    assert "failed episode" in response.text
    assert "llm timeout" in response.text
    assert "Recent Episodes" in response.text
    assert "Flagged Nodes" in response.text
    assert "Alpha Entity" in response.text


@pytest.mark.unit
def test_explorer_llm_tracking_panels() -> None:
    app = create_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    
    mock_pending = [{"node_uuid": "node-123", "action": "compress", "attempts": 2, "created_at": "2026-03-08T12:00:00Z", "failure_reason": "timeout"}]
    mock_events = [{"operation": "episode_enrichment", "kind": "background", "duration_ms": 1500, "success": True, "error": None, "started_at": "2026-03-08T12:05:00Z"}]
    
    with patch("menhir.explorer.app.PendingActionStore.fetch_pending", return_value=mock_pending):
        with patch("menhir.explorer.app.telemetry_store.fetch_recent", return_value=mock_events):
            with TestClient(app) as client:
                response = client.get("/explorer")
                
    assert response.status_code == 200
    assert "Pending LLM Tasks" in response.text
    assert "compress" in response.text
    assert "node-123" in response.text
    assert "timeout" in response.text
    
    assert "Background Ops" in response.text
    assert "episode_enrichment" in response.text
    assert "background | 1500ms" in response.text


@pytest.mark.unit
def test_explorer_graph_api_returns_cytoscape_elements() -> None:
    app = create_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer/api/graph/entity-1?depth=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["elements"]["nodes"][0]["data"]["id"] == "entity-1"
    assert payload["elements"]["edges"][0]["data"]["label"] == "MENTIONS"


@pytest.mark.unit
def test_explorer_node_partial_returns_404_for_missing_node() -> None:
    app = create_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer/partials/node/missing")
    assert response.status_code == 404


@pytest.mark.unit
def test_explorer_home_redacts_content_when_privacy_on() -> None:
    app = create_app(settings=MemorySettings(privacy_redact=True), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer")
    assert response.status_code == 200
    # memory content (names / previews) masked
    assert "Alpha Entity" not in response.text
    assert "queued episode" not in response.text
    assert "Queued preview" not in response.text
    assert "[hidden]" in response.text
    # structural fields preserved so the view stays navigable
    assert "session-1" in response.text
    assert "queued-episode-1" in response.text


@pytest.mark.unit
def test_explorer_home_reveals_with_cookie_on_loopback() -> None:
    app = create_app(settings=MemorySettings(privacy_redact=True), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        client.cookies.set("menhir_reveal", "1")
        response = client.get("/explorer")
    assert response.status_code == 200
    assert "Alpha Entity" in response.text


@pytest.mark.unit
def test_explorer_graph_api_redacts_node_labels_keeps_edges() -> None:
    app = create_app(settings=MemorySettings(privacy_redact=True), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer/api/graph/entity-1?depth=2")
    assert response.status_code == 200
    payload = response.json()
    labels = [n["data"]["label"] for n in payload["elements"]["nodes"]]
    assert "Alpha Entity" not in labels
    assert "[hidden]" in labels
    # relationship type is structural, preserved
    assert payload["elements"]["edges"][0]["data"]["label"] == "MENTIONS"


@pytest.mark.unit
def test_explorer_privacy_toggle_sets_cookie() -> None:
    app = create_app(settings=MemorySettings(privacy_redact=True), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.post("/explorer/privacy/reveal")
        assert response.status_code == 200
        assert response.json()["redact"] is False
        assert client.cookies.get("menhir_reveal") == "1"
        response = client.post("/explorer/privacy/hide")
        assert response.json()["redact"] is True
